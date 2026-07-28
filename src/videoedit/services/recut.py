from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.errors import PlanningValidationError, RenderOutputError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p10-04b"
DURATION_TOLERANCE_US = 100_000
_REMOVAL_WORDS = re.compile(r"\b(remove|delete|cut|drop|trim|duplicate|repeated|repetition)\b")
_NEGATED_REMOVAL = re.compile(
    r"\b(?:do not|don't|preserve|keep)\s+(?:remove|delete|cut|drop|trim)\b"
)


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _marker_range(marker: Mapping[str, Any]) -> tuple[int, int]:
    value = marker.get("range_us")
    if not isinstance(value, Mapping):
        raise PlanningValidationError("review marker range_us must be an object")
    try:
        start_us = int(str(value["start_us"]))
        end_us = int(str(value["end_us"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError("review marker range_us has invalid bounds") from exc
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError("review marker range_us must be a positive half-open range")
    return start_us, end_us


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = []
    for start_us, end_us in ordered:
        if not merged or start_us > merged[-1][1]:
            merged.append((start_us, end_us))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_us))
    return merged


def _subtract_ranges(
    ranges: Sequence[tuple[int, int]], exclusions: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    remaining = list(ranges)
    for exclusion_start, exclusion_end in exclusions:
        next_ranges: list[tuple[int, int]] = []
        for start_us, end_us in remaining:
            if exclusion_end <= start_us or exclusion_start >= end_us:
                next_ranges.append((start_us, end_us))
                continue
            if start_us < exclusion_start:
                next_ranges.append((start_us, exclusion_start))
            if exclusion_end < end_us:
                next_ranges.append((exclusion_end, end_us))
        remaining = next_ranges
    return [(start_us, end_us) for start_us, end_us in remaining if end_us > start_us]


def _is_removable_fix(instruction: str) -> bool:
    normalized = instruction.lower()
    return bool(_REMOVAL_WORDS.search(normalized)) and not _NEGATED_REMOVAL.search(normalized)


def derive_recut_ranges(
    markers: Sequence[Mapping[str, Any]], source_duration_us: int
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], list[str]]:
    """Derive explicit removals and complementary keep ranges from approved markers."""

    if source_duration_us <= 0:
        raise PlanningValidationError("source duration must be positive")
    removal_ranges: list[tuple[int, int]] = []
    keep_overrides: list[tuple[int, int]] = []
    warnings: list[str] = []
    for marker in markers:
        kind = str(marker.get("kind", ""))
        instruction = str(marker.get("instruction", ""))
        marker_range = _marker_range(marker)
        if marker_range[1] > source_duration_us:
            raise PlanningValidationError(
                f"marker {marker.get('marker_id', '<unknown>')} exceeds source duration"
            )
        if kind == "KEEP":
            keep_overrides.append(marker_range)
        elif kind == "REMOVE" or (kind == "FIX" and _is_removable_fix(instruction)):
            removal_ranges.append(marker_range)
        elif kind == "FIX":
            warnings.append(f"fix_not_applied:{marker.get('marker_id', '<unknown>')}")

    merged_removals = _merge_ranges(removal_ranges)
    if keep_overrides:
        merged_removals = _subtract_ranges(merged_removals, _merge_ranges(keep_overrides))
    if not merged_removals:
        warnings.append("no_explicit_removable_ranges")
    keep_ranges = _subtract_ranges([(0, source_duration_us)], merged_removals)
    if not keep_ranges:
        raise PlanningValidationError("approved markers remove the complete source")
    return merged_removals, keep_ranges, warnings


def source_to_output_mapping(
    keep_ranges: Sequence[tuple[int, int]],
) -> list[dict[str, int]]:
    output_cursor_us = 0
    mapping: list[dict[str, int]] = []
    for start_us, end_us in keep_ranges:
        duration_us = end_us - start_us
        mapping.append(
            {
                "source_start_us": start_us,
                "source_end_us": end_us,
                "output_start_us": output_cursor_us,
                "output_end_us": output_cursor_us + duration_us,
            }
        )
        output_cursor_us += duration_us
    return mapping


def _source_duration_us(adapter: FFmpegAdapter, source: Path) -> int:
    probe = adapter.probe(source)
    duration_us = seconds_to_us(probe.get("format", {}).get("duration"))
    if duration_us is not None and duration_us > 0:
        return duration_us
    durations = [
        duration
        for stream in probe.get("streams", [])
        if isinstance(stream, Mapping)
        for duration in [seconds_to_us(stream.get("duration"))]
        if duration is not None and duration > 0
    ]
    if durations:
        return max(durations)
    raise PlanningValidationError(f"ffprobe did not provide a positive duration: {source}")


def _validate_request(
    package_root: Path,
    layout: ProjectLayout,
    request_path: Path,
) -> dict[str, Any]:
    request = _read_object(request_path, "revision request")
    validate_artifact(package_root, "revision_request", request)
    if request["project_id"] != layout.root.name:
        raise PlanningValidationError("revision request belongs to another project")
    target_revision_id = str(request["revision_id"])
    expected_path = layout.revision_root(target_revision_id) / "revision-request.json"
    if request_path != expected_path.resolve():
        raise PlanningValidationError("revision request path does not match its revision")
    marker_ref = request["source_markers"]
    marker_path = _owned_path(layout, Path(str(marker_ref["path"])), "review marker artifact")
    if not marker_path.is_file() or sha256_file(marker_path) != marker_ref["sha256"]:
        raise PlanningValidationError("review marker artifact hash is stale")
    markers = _read_object(marker_path, "review marker artifact")
    validate_artifact(package_root, "review_markers", markers)
    if markers["revision_id"] != request["parent_revision_id"]:
        raise PlanningValidationError("review markers and revision request parent differ")
    return request


def _output_is_current(
    package_root: Path,
    manifest_path: Path,
    source_sha256: str,
    marker_sha256: str,
    output_path: Path,
) -> bool:
    if not manifest_path.is_file() or not output_path.is_file():
        return False
    manifest = _read_object(manifest_path, "revision media manifest")
    validate_artifact(package_root, "revision_media_manifest", manifest)
    output_ref = manifest["output"]
    return bool(
        manifest["source"]["sha256"] == source_sha256
        and manifest["source_markers"]["sha256"] == marker_sha256
        and output_ref["path"] == str(output_path.resolve())
        and output_ref["sha256"] == sha256_file(output_path)
    )


def recut_revision(
    package_root: Path,
    layout: ProjectLayout,
    revision_request_path: Path,
    source_path: Path,
    *,
    adapter: FFmpegAdapter | None = None,
    video_codec: str | None = None,
    audio_codec: str = "aac",
    qp: int | None = None,
    preset: str = "medium",
    strict_decode: bool = False,
) -> Path:
    """Render approved revision removals and persist a hash-bound media manifest.

    The default profile remains the historical review-recut profile. Production
    repairs can explicitly request the lossless H.264/PCM profile so a repair
    made from an already-promoted lossless candidate does not introduce a
    lossy intermediate.
    """

    selected_request = _owned_path(layout, revision_request_path, "revision request")
    selected_source = _owned_path(layout, source_path, "recut source")
    if not selected_request.is_file():
        raise PlanningValidationError(f"revision request does not exist: {selected_request}")
    if not selected_source.is_file():
        raise PlanningValidationError(f"recut source does not exist: {selected_source}")

    request = _validate_request(package_root, layout, selected_request)
    target_revision_id = str(request["revision_id"])
    target_root = layout.revision_root(target_revision_id)
    if not target_root.is_dir():
        raise PlanningValidationError(f"revision directory does not exist: {target_root}")
    marker_items = [dict(item) for item in request["markers"]]
    selected_adapter = adapter or FFmpegAdapter()

    with ProjectLock(layout, stage="revision_recut", revision_id=target_revision_id):
        source_sha256 = sha256_file(selected_source)
        marker_sha256 = str(request["source_markers"]["sha256"])
        source_duration_us = _source_duration_us(selected_adapter, selected_source)
        removed_ranges, keep_ranges, warnings = derive_recut_ranges(
            marker_items, source_duration_us
        )
        stage_key = make_stage_key(
            "revision-recut",
            IMPLEMENTATION_VERSION,
            [source_sha256, marker_sha256, sha256_file(selected_request)],
            {
                "revision_id": target_revision_id,
                "source_duration_us": source_duration_us,
                "removed_ranges": removed_ranges,
                "keep_ranges": keep_ranges,
            },
        )
        output_path = target_root / "outputs" / "recut.mp4"
        manifest_path = target_root / "revision-media.json"
        if _output_is_current(
            package_root, manifest_path, source_sha256, marker_sha256, output_path
        ):
            return manifest_path
        if output_path.exists() or manifest_path.exists():
            raise StateConflictError(
                "revision recut output exists but is not bound to the current source or markers"
            )

        staging_root = layout.staging / "revisions" / target_revision_id / f"recut-{stage_key[:16]}"
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)
        staged_output = staging_root / "recut.mp4"
        selected_adapter.render_keep_ranges(
            selected_source,
            keep_ranges,
            staged_output,
            video_codec=video_codec,
            audio_codec=audio_codec,
            crf=18,
            preset=preset,
            qp=qp,
        )
        if not staged_output.is_file() or staged_output.stat().st_size <= 0:
            raise RenderOutputError("recut adapter did not create a non-empty output")
        decode_result = selected_adapter.full_decode_check(staged_output, strict=strict_decode)
        if decode_result.exit_code != 0:
            raise RenderOutputError("recut output failed full decode")
        output_probe = selected_adapter.probe(staged_output)
        streams = output_probe.get("streams", [])
        if not isinstance(streams, list):
            streams = []
        if not any(
            isinstance(item, Mapping) and item.get("codec_type") == "video" for item in streams
        ):
            raise RenderOutputError("recut output has no video stream")
        if not any(
            isinstance(item, Mapping) and item.get("codec_type") == "audio" for item in streams
        ):
            raise RenderOutputError("recut output has no audio stream")
        output_duration_us = seconds_to_us(output_probe.get("format", {}).get("duration"))
        if output_duration_us is None or output_duration_us <= 0:
            raise RenderOutputError("recut output has no positive duration")
        expected_duration_us = sum(end_us - start_us for start_us, end_us in keep_ranges)
        if abs(output_duration_us - expected_duration_us) > DURATION_TOLERANCE_US:
            warnings.append("output_duration_rounding_exceeds_tolerance")
        if sha256_file(selected_source) != source_sha256:
            raise PlanningValidationError("source media changed while recut was rendering")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_output, output_path)
        output_sha256 = sha256_file(output_path)
        manifest_payload: dict[str, Any] = {
            "schema_name": "revision_media_manifest",
            "schema_version": "1.0.0",
            "artifact_id": f"art_revision_media_{target_revision_id}",
            "project_id": layout.root.name,
            "revision_id": target_revision_id,
            "parent_revision_id": str(request["parent_revision_id"]),
            "created_at": now_iso(),
            "producer": producer(
                "revision-recut", selected_adapter.__class__.__name__, selected_adapter.version()
            ),
            "source_markers": {
                "artifact_id": str(request["source_markers"]["artifact_id"]),
                "path": str(
                    _owned_path(
                        layout,
                        Path(str(request["source_markers"]["path"])),
                        "review marker artifact",
                    )
                ),
                "sha256": marker_sha256,
            },
            "source": {
                "artifact_id": "source_media",
                "path": str(selected_source),
                "sha256": source_sha256,
            },
            "output": {
                "artifact_id": "revision_recut_media",
                "path": str(output_path.resolve()),
                "sha256": output_sha256,
            },
            "source_duration_us": source_duration_us,
            "output_duration_us": output_duration_us,
            "removed_ranges": [
                {"start_us": start_us, "end_us": end_us} for start_us, end_us in removed_ranges
            ],
            "keep_ranges": [
                {"start_us": start_us, "end_us": end_us} for start_us, end_us in keep_ranges
            ],
            "source_to_output_mapping": source_to_output_mapping(keep_ranges),
            "warnings": sorted(set(warnings)),
            "status": "warning" if warnings else "complete",
        }
        write_validated_artifact(
            package_root,
            "revision_media_manifest",
            staging_root / "revision-media.json",
            manifest_payload,
        )
        os.replace(staging_root / "revision-media.json", manifest_path)
        return manifest_path
