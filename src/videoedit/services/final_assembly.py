from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.errors import PlanningValidationError, RenderOutputError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.rendering import (
    AV_SYNC_TOLERANCE_US,
    INTEGRATED_TARGET_LUFS,
    INTEGRATED_TOLERANCE_LU,
    LOUDNESS_RANGE_TARGET_LU,
    TRUE_PEAK_TARGET_DBFS,
    TRUE_PEAK_TOLERANCE_DB,
    _adapter_version,
    _command_record,
    _first_stream,
    _stream_duration_us,
    parse_clipped_samples,
    parse_loudness_measurement,
)
from videoedit.services.segment_lock import _owned_path as _owned_lock_path
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)

IMPLEMENTATION_VERSION = "p11-01c"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    return _owned_lock_path(layout, path, description)


def _file_ref(path: Path, artifact_id: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _range(value: Mapping[str, Any], label: str) -> dict[str, int]:
    try:
        start = int(value["start_us"])
        end = int(value["end_us"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{label} must contain integer bounds") from exc
    if start < 0 or end <= start:
        raise PlanningValidationError(f"{label} must be a positive half-open range")
    return {"start_us": start, "end_us": end}


def _promote_media(staged: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        if sha256_file(final) == sha256_file(staged):
            staged.unlink(missing_ok=True)
            return
        raise StateConflictError(
            f"final assembly output already exists with different bytes: {final}"
        )
    os.replace(staged, final)


def _cached_assembly_matches(
    package_root: Path,
    current: dict[str, Any],
    *,
    layout: ProjectLayout,
    revision_id: str,
    config_hash: str,
    adapter_version: str,
    expected_duration_us: int,
    expected_inputs: list[dict[str, str]],
    expected_segments: list[dict[str, Any]],
    expected_warnings: list[str],
    final_output: Path,
    retained_pre: Path,
) -> bool:
    """Verify a cached assembly manifest and both promoted media references."""

    validate_artifact(package_root, "final_assembly_manifest", current)
    if (
        current["project_id"] != layout.root.name
        or current["revision_id"] != revision_id
        or current["config_sha256"] != config_hash
        or current["expected_duration_us"] != expected_duration_us
        or current["inputs"] != expected_inputs
        or current["segments"] != expected_segments
        or current["warnings"] != expected_warnings
        or current["status"] != "complete"
    ):
        return False
    producer_value = current["producer"]
    if (
        producer_value["adapter"] != "ffmpeg"
        or producer_value["adapter_version"] != adapter_version
    ):
        return False
    if abs(int(current["actual_duration_us"]) - expected_duration_us) > AV_SYNC_TOLERANCE_US:
        return False
    loudness = current["loudness"]
    if loudness["status"] != "pass" or loudness["clipped_samples"] != 0:
        return False
    for key, path in (("output", final_output), ("pre_normalized_output", retained_pre)):
        reference = current[key]
        if reference["path"] != str(path.resolve()) or not path.is_file():
            return False
        if reference["sha256"] != sha256_file(path):
            return False
        if reference["size_bytes"] != path.stat().st_size:
            return False
    return True


def _validate_segment(
    package_root: Path,
    layout: ProjectLayout,
    item: Mapping[str, Any],
    *,
    revision_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    try:
        segment_id = str(item["segment_id"])
        lock_path = _owned_path(layout, Path(str(item["lock_path"])), "segment lock")
        media_manifest_path = _owned_path(
            layout, Path(str(item["media_manifest_path"])), "segment media manifest"
        )
        source_range_value = item["source_range"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError("final assembly segment input is incomplete") from exc
    if not segment_id:
        raise PlanningValidationError("final assembly segment id is empty")
    if not lock_path.is_file() or not media_manifest_path.is_file():
        raise PlanningValidationError(f"final assembly input is missing for {segment_id}")
    lock = _read_object(lock_path, "segment lock")
    validate_artifact(package_root, "segment_lock", lock)
    media_manifest = _read_object(media_manifest_path, "segment media manifest")
    validate_artifact(package_root, "revision_media_manifest", media_manifest)
    if lock["project_id"] != layout.root.name or media_manifest["project_id"] != layout.root.name:
        raise PlanningValidationError(
            f"final assembly input belongs to another project: {segment_id}"
        )
    if lock["revision_id"] != revision_id or media_manifest["revision_id"] != revision_id:
        raise PlanningValidationError(
            f"final assembly input is not bound to {revision_id}: {segment_id}"
        )
    if lock["segment_id"] != segment_id:
        raise PlanningValidationError(f"segment lock id does not match input: {segment_id}")
    if lock["locked"] is not True or lock["status"] != "complete":
        raise PlanningValidationError(f"segment is not locked and complete: {segment_id}")
    media_ref = media_manifest["output"]
    media_path = _owned_path(layout, Path(str(media_ref["path"])), "segment media output")
    if not media_path.is_file() or sha256_file(media_path) != media_ref["sha256"]:
        raise PlanningValidationError(f"segment media output hash is stale: {segment_id}")
    source_range = _range(source_range_value, f"source range for {segment_id}")
    expected_duration = int(media_manifest["output_duration_us"])
    if expected_duration <= 0:
        raise PlanningValidationError(f"segment media duration is not positive: {segment_id}")
    return (
        {
            "segment_id": segment_id,
            "revision_id": revision_id,
            "source_range": source_range,
            "lock": _file_ref(lock_path, str(lock["artifact_id"])),
            "media": _file_ref(media_path, str(media_ref["artifact_id"])),
            "media_duration_us": expected_duration,
            "lock_path": lock_path,
            "media_path": media_path,
        },
        media_manifest,
        source_range,
    )


def assemble_approved_segments(
    package_root: Path,
    layout: ProjectLayout,
    segments: Sequence[Mapping[str, Any]],
    *,
    revision_id: str = "rev_001",
    output: Path | None = None,
    adapter: FFmpegAdapter | None = None,
    normalization: str = "profile",
) -> Path:
    """Assemble locked segment media with the selected loudness policy.

    ``profile`` preserves the historical deterministic loudness pass. ``none``
    is intentionally limited to one already-assembled segment and copies its
    bytes through staging without changing picture or audio.
    """

    if not segments:
        raise PlanningValidationError("final assembly requires at least one locked segment")
    if normalization not in {"profile", "none"}:
        raise PlanningValidationError("normalization must be profile or none")
    validated: list[dict[str, Any]] = []
    media_paths: list[Path] = []
    previous_source_end = -1
    seen_ids: set[str] = set()
    for item in segments:
        segment, _manifest, source_range = _validate_segment(
            package_root, layout, item, revision_id=revision_id
        )
        segment_id = str(segment["segment_id"])
        if segment_id in seen_ids:
            raise PlanningValidationError(f"duplicate final assembly segment: {segment_id}")
        if source_range["start_us"] < previous_source_end:
            raise PlanningValidationError("final assembly source ranges are not ordered")
        seen_ids.add(segment_id)
        previous_source_end = source_range["end_us"]
        validated.append(segment)
        media_paths.append(Path(str(segment["media_path"])))

    expected_duration_us = sum(int(item["media_duration_us"]) for item in validated)
    if normalization == "none" and len(validated) != 1:
        raise PlanningValidationError(
            "normalization=none requires one already-assembled segment to preserve bytes"
        )
    expected_warnings = (
        []
        if normalization == "profile"
        else ["loudness_normalization_disabled_by_delivery_profile"]
    )
    adapter = adapter or FFmpegAdapter()
    encoder_identity = adapter_encoder_identity(adapter)
    adapter_version = _adapter_version(adapter)
    config_hash = config_sha256(layout)
    input_hashes = [str(item["lock"]["sha256"]) for item in validated] + [
        str(item["media"]["sha256"]) for item in validated
    ]
    expected_inputs = [
        {
            "artifact_id": str(item["lock"]["artifact_id"]),
            "sha256": str(item["lock"]["sha256"]),
        }
        for item in validated
    ] + [
        {
            "artifact_id": str(item["media"]["artifact_id"]),
            "sha256": str(item["media"]["sha256"]),
        }
        for item in validated
    ]
    output_cursor_us = 0
    expected_segments: list[dict[str, Any]] = []
    for item in validated:
        output_end_us = output_cursor_us + int(item["media_duration_us"])
        expected_segments.append(
            {
                key: item[key]
                for key in ("segment_id", "revision_id", "source_range", "lock", "media")
            }
            | {"output_range": {"start_us": output_cursor_us, "end_us": output_end_us}}
        )
        output_cursor_us = output_end_us
    stage_key = make_stage_key(
        "final-assembly",
        IMPLEMENTATION_VERSION,
        input_hashes,
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "config_sha256": config_hash,
            "expected_duration_us": expected_duration_us,
            "output": str(output.resolve()) if output else None,
            "encoder": encoder_identity,
            "normalization": normalization,
        },
    )
    manifest_path = layout.artifacts / f"final-assembly-{stage_key[:16]}.json"
    alias_path = layout.artifacts / "final-assembly.json"
    final_output = (
        _owned_path(layout, output, "final assembly output")
        if output is not None
        else layout.output / "candidates" / revision_id / f"final-candidate-{stage_key[:16]}.mp4"
    )
    stage_dir = layout.staging / "final-assembly" / stage_key
    retained_pre = layout.work / "final-assembly" / stage_key / "pre-normalized.mp4"
    staged_pre = stage_dir / "pre-normalized.mp4"
    staged_normalized = stage_dir / "final-candidate.mp4"
    if manifest_path.is_file():
        current = _read_object(manifest_path, "final assembly manifest")
        if _cached_assembly_matches(
            package_root,
            current,
            layout=layout,
            revision_id=revision_id,
            config_hash=config_hash,
            adapter_version=adapter_version,
            expected_duration_us=expected_duration_us,
            expected_inputs=expected_inputs,
            expected_segments=expected_segments,
            expected_warnings=expected_warnings,
            final_output=final_output,
            retained_pre=retained_pre,
        ):
            return manifest_path
        raise StateConflictError("final assembly manifest exists with stale contents")
    with ProjectLock(layout, stage="final_assembly", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "final_assembly", revision_id)
        if (
            previous
            and previous.get("stage_key") == stage_key
            and previous.get("status") == "complete"
        ):
            if manifest_path.is_file():
                return manifest_path
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="final_assembly",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        commands: list[dict[str, Any]] = []
        try:
            if normalization == "none":
                shutil.copyfile(media_paths[0], staged_pre)
                if sha256_file(staged_pre) != sha256_file(media_paths[0]):
                    raise RenderOutputError("preassembled candidate changed during staging copy")
            else:
                concat_result = adapter.concat_media(media_paths, staged_pre)
                commands.append(_command_record(concat_result, stage_dir, adapter_version))
            input_loudness_result = adapter.measure_loudness(staged_pre)
            input_loudness = parse_loudness_measurement(input_loudness_result)
            commands.append(_command_record(input_loudness_result, stage_dir, adapter_version))
            if normalization == "none":
                shutil.copyfile(staged_pre, staged_normalized)
            else:
                normalize_result = adapter.normalize_loudness(
                    staged_pre,
                    staged_normalized,
                    input_loudness,
                    integrated_target_lufs=INTEGRATED_TARGET_LUFS,
                    true_peak_target_dbfs=TRUE_PEAK_TARGET_DBFS,
                    loudness_range_target_lu=LOUDNESS_RANGE_TARGET_LU,
                )
                commands.append(_command_record(normalize_result, stage_dir, adapter_version))
            output_loudness_result = adapter.measure_loudness(staged_normalized)
            output_loudness = parse_loudness_measurement(output_loudness_result)
            commands.append(_command_record(output_loudness_result, stage_dir, adapter_version))
            clipping_result = adapter.measure_clipping(staged_normalized)
            clipped_samples = parse_clipped_samples(clipping_result)
            commands.append(_command_record(clipping_result, stage_dir, adapter_version))
            loudness_pass = (
                sha256_file(staged_normalized) == sha256_file(media_paths[0])
                if normalization == "none"
                else (
                    abs(output_loudness["integrated_lufs"] - INTEGRATED_TARGET_LUFS)
                    <= INTEGRATED_TOLERANCE_LU
                    and output_loudness["true_peak_dbfs"]
                    <= TRUE_PEAK_TARGET_DBFS + TRUE_PEAK_TOLERANCE_DB
                    and clipped_samples == 0
                )
            )
            if not loudness_pass:
                raise RenderOutputError("final loudness pass is outside the delivery profile")
            decode_result = adapter.full_decode_check(staged_normalized)
            commands.append(_command_record(decode_result, stage_dir, adapter_version))
            if decode_result.exit_code != 0:
                raise RenderOutputError("final candidate failed full decode")
            probe = adapter.probe(staged_normalized)
            video = _first_stream(probe, "video")
            audio = _first_stream(probe, "audio")
            format_duration_us = seconds_to_us(probe.get("format", {}).get("duration"))
            if format_duration_us is None:
                raise RenderOutputError("final candidate has no container duration")
            video_duration_us = _stream_duration_us(video, format_duration_us)
            audio_duration_us = _stream_duration_us(audio, format_duration_us)
            if video_duration_us is None or audio_duration_us is None:
                raise RenderOutputError("final candidate has no stream durations")
            if abs(format_duration_us - expected_duration_us) > AV_SYNC_TOLERANCE_US:
                raise RenderOutputError("final candidate duration differs from approved segments")
            if abs(video_duration_us - audio_duration_us) > AV_SYNC_TOLERANCE_US:
                raise RenderOutputError("final candidate picture and audio are not synchronized")
            retained_pre.parent.mkdir(parents=True, exist_ok=True)
            _promote_media(staged_pre, retained_pre)
            _promote_media(staged_normalized, final_output)
            output_payload: dict[str, Any] = {
                "schema_name": "final_assembly_manifest",
                "schema_version": "1.0.0",
                "artifact_id": "art_final_assembly",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("final-assembly", "ffmpeg", adapter_version),
                "inputs": expected_inputs,
                "config_sha256": config_hash,
                "segments": expected_segments,
                "expected_duration_us": expected_duration_us,
                "actual_duration_us": format_duration_us,
                "pre_normalized_output": _file_ref(retained_pre, "art_final_pre_normalized"),
                "output": _file_ref(final_output, "art_final_candidate"),
                "loudness": {
                    "profile": {
                        "integrated_target_lufs": INTEGRATED_TARGET_LUFS,
                        "true_peak_target_dbfs": TRUE_PEAK_TARGET_DBFS,
                        "loudness_range_target_lu": LOUDNESS_RANGE_TARGET_LU,
                        "integrated_tolerance_lu": INTEGRATED_TOLERANCE_LU,
                        "true_peak_tolerance_db": TRUE_PEAK_TOLERANCE_DB,
                    },
                    "input": input_loudness,
                    "output": output_loudness,
                    "clipped_samples": clipped_samples,
                    "status": "pass",
                },
                "commands": commands,
                "warnings": expected_warnings,
                "status": "complete",
            }
            write_validated_artifact(
                package_root, "final_assembly_manifest", manifest_path, output_payload
            )
            write_validated_artifact(
                package_root, "final_assembly_manifest", alias_path, output_payload
            )
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={
                    "final_assembly_manifest": manifest_path,
                    "final_candidate": final_output,
                },
                warnings=expected_warnings,
            )
            return manifest_path
        except Exception as exc:
            fail_stage(package_root, layout, state, code="final_assembly_failed", message=str(exc))
            raise


__all__ = ["assemble_approved_segments"]
