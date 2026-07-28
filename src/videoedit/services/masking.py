from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.adapters.process import ProcessResult
from videoedit.errors import MaskValidationError, VideoeditError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.foreground import AlphaStatistics, parse_alpha_statistics
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)

MASK_IMPLEMENTATION_VERSION = f"{__version__}:mask-v4"
MASK_DURATION_TOLERANCE_US = 100_000
LOSSLESS_MASK_CODECS = {"ffv1", "ffvhuff", "huffyuv", "png", "rawvideo", "qtrle"}
GRAY_PIXEL_FORMAT_PREFIXES = ("gray", "monow")


@dataclass(frozen=True, slots=True)
class MaskValidation:
    source_video: dict[str, Any]
    mask_video: dict[str, Any]
    mask_statistics: dict[str, Any]
    validation: dict[str, str]
    warnings: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return all(value == "pass" for value in self.validation.values())


def _first_stream(probe: Mapping[str, Any], stream_type: str) -> dict[str, Any] | None:
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        return None
    for value in streams:
        if isinstance(value, dict) and value.get("codec_type") == stream_type:
            return dict(value)
    return None


def _stream_duration_us(probe: Mapping[str, Any], stream: Mapping[str, Any]) -> int | None:
    duration = seconds_to_us(stream.get("duration"))
    if duration is not None:
        return duration
    format_value = probe.get("format")
    if isinstance(format_value, Mapping):
        return seconds_to_us(format_value.get("duration"))
    return None


def _rational_equal(left: object, right: object) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    try:
        return int(left.get("numerator", 0)) == int(right.get("numerator", -1)) and int(
            left.get("denominator", 0)
        ) == int(right.get("denominator", -1))
    except (TypeError, ValueError):
        return False


def _video_payload(
    probe: Mapping[str, Any],
    stream: Mapping[str, Any],
    frame_count: int | None,
    *,
    lossless: bool | None = None,
) -> dict[str, Any]:
    rate = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate"))
    duration = _stream_duration_us(probe, stream)
    if rate is None or duration is None or frame_count is None or frame_count <= 0:
        raise MaskValidationError("mask video metadata is incomplete")
    payload: dict[str, Any] = {
        "codec": str(stream.get("codec_name") or "unknown"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_rate": rate,
        "pixel_format": str(stream.get("pix_fmt") or "unknown"),
        "frame_count": frame_count,
        "duration_us": duration,
    }
    if lossless is not None:
        payload["lossless"] = lossless
    return payload


def _is_lossless_mask(stream: Mapping[str, Any]) -> bool:
    codec = str(stream.get("codec_name") or "").lower()
    pixel_format = str(stream.get("pix_fmt") or "").lower()
    return codec in LOSSLESS_MASK_CODECS and pixel_format.startswith(GRAY_PIXEL_FORMAT_PREFIXES)


def _mask_polarity(samples: Sequence[AlphaStatistics]) -> str:
    minimum = min((sample.minimum for sample in samples), default=0.0)
    maximum = max((sample.maximum for sample in samples), default=0.0)
    if minimum >= 255 and maximum >= 255:
        return "opaque"
    if minimum <= 0 and maximum <= 0:
        return "transparent"
    if minimum <= 5 and maximum >= 250:
        return "white_foreground"
    return "unknown"


def validate_mask_alignment(
    source_probe: Mapping[str, Any],
    mask_probe: Mapping[str, Any],
    *,
    source_frame_count: int | None,
    mask_frame_count: int | None,
    mask_samples: Sequence[AlphaStatistics],
    expected_polarity: str = "white_foreground",
    full_decode_ok: bool = True,
) -> MaskValidation:
    if expected_polarity != "white_foreground":
        raise MaskValidationError("only white_foreground mask polarity is supported")
    source_video = _first_stream(source_probe, "video")
    mask_video = _first_stream(mask_probe, "video")
    if source_video is None or mask_video is None:
        raise MaskValidationError("mask validation requires source and mask video streams")
    source_rate = parse_rate(source_video.get("avg_frame_rate")) or parse_rate(
        source_video.get("r_frame_rate")
    )
    mask_rate = parse_rate(mask_video.get("avg_frame_rate")) or parse_rate(
        mask_video.get("r_frame_rate")
    )
    source_duration = _stream_duration_us(source_probe, source_video)
    mask_duration = _stream_duration_us(mask_probe, mask_video)
    mask_lossless = _is_lossless_mask(mask_video)
    mask_pixel_format = str(mask_video.get("pix_fmt") or "").lower()
    dimensions_pass = (
        int(source_video.get("width") or 0) > 0
        and int(source_video.get("height") or 0) > 0
        and int(source_video.get("width") or 0) == int(mask_video.get("width") or 0)
        and int(source_video.get("height") or 0) == int(mask_video.get("height") or 0)
    )
    frame_count_pass = (
        source_frame_count is not None
        and mask_frame_count is not None
        and source_frame_count > 0
        and source_frame_count == mask_frame_count
    )
    frame_rate_pass = _rational_equal(source_rate, mask_rate)
    duration_pass = (
        source_duration is not None
        and mask_duration is not None
        and abs(source_duration - mask_duration) <= MASK_DURATION_TOLERANCE_US
    )
    values = [
        value for sample in mask_samples for value in (sample.minimum, sample.maximum, sample.mean)
    ]
    range_pass = bool(values) and all(
        math.isfinite(value) and 0 <= value <= 255 for value in values
    )
    polarity = _mask_polarity(mask_samples) if range_pass else "unknown"
    polarity_pass = polarity == expected_polarity
    warnings: list[str] = []
    if polarity in {"opaque", "transparent"}:
        warnings.append(f"mask_polarity_is_{polarity}")
    if not mask_lossless:
        warnings.append("mask_codec_or_pixel_format_is_not_lossless_gray")
    return MaskValidation(
        source_video=_video_payload(source_probe, source_video, source_frame_count),
        mask_video=_video_payload(
            mask_probe,
            mask_video,
            mask_frame_count,
            lossless=mask_lossless,
        ),
        mask_statistics={
            "min": min((sample.minimum for sample in mask_samples), default=0.0),
            "max": max((sample.maximum for sample in mask_samples), default=0.0),
            "mean": (
                sum(sample.mean for sample in mask_samples) / len(mask_samples)
                if mask_samples
                else 0.0
            ),
            "polarity": polarity,
            "sampled_frames": len(mask_samples),
        },
        validation={
            "full_decode": "pass" if full_decode_ok else "fail",
            "lossless": "pass" if mask_lossless else "fail",
            "pixel_format": "pass"
            if mask_pixel_format.startswith(GRAY_PIXEL_FORMAT_PREFIXES)
            else "fail",
            "dimensions": "pass" if dimensions_pass else "fail",
            "frame_count": "pass" if frame_count_pass else "fail",
            "frame_rate": "pass" if frame_rate_pass else "fail",
            "range": "pass" if range_pass else "fail",
            "polarity": "pass" if polarity_pass else "fail",
            "duration": "pass" if duration_pass else "fail",
        },
        warnings=tuple(warnings),
    )


def _command_record(result: ProcessResult, working_directory: Path, version: str) -> dict[str, Any]:
    arguments = result.arguments or ("ffmpeg",)
    return {
        "executable": arguments[0],
        "arguments": list(arguments[1:]),
        "working_directory": str(working_directory.resolve()),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "version": version or "unknown",
    }


def _stage_file_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
        return False
    path = Path(str(value["path"])).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError:
        return False
    try:
        return (
            path.is_file()
            and path.stat().st_size == int(value.get("size_bytes", -1))
            and sha256_file(path) == value.get("sha256")
        )
    except (OSError, TypeError, ValueError):
        return False


def _cached_manifest(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, Any] | None,
    stage: str,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping) or "manifest" not in artifacts:
        return None
    if not all(_stage_file_ref_valid(layout, value) for value in artifacts.values()):
        return None
    manifest_path = Path(str(artifacts["manifest"]["path"])).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        validate_artifact(package_root, stage, payload)
    except (OSError, json.JSONDecodeError, ValueError, KeyError):
        return None
    return manifest_path


def _safe_output_path(layout: ProjectLayout, output: Path) -> Path:
    resolved = output.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise MaskValidationError("mask effect output must stay inside the project") from exc
    try:
        resolved.relative_to(layout.raw.resolve())
    except ValueError:
        return resolved
    raise MaskValidationError("mask effect output must not be written under raw sources")


def _promote_media(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != sha256_file(staged):
            raise MaskValidationError(
                f"immutable mask effect target already exists with another hash: {target}"
            )
        staged.unlink(missing_ok=True)
        return
    os.replace(staged, target)


def validate_local_mask(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    mask: Path,
    *,
    expected_polarity: str = "white_foreground",
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    source = source.expanduser().resolve()
    mask = mask.expanduser().resolve()
    if not source.is_file() or not mask.is_file():
        raise MaskValidationError("mask validation requires existing source and mask files")
    selected_adapter = adapter or FFmpegAdapter()
    source_hash = sha256_file(source)
    mask_hash = sha256_file(mask)
    adapter_version = str(selected_adapter.version()) or "unknown"
    stage_key = make_stage_key(
        "mask_validation",
        MASK_IMPLEMENTATION_VERSION,
        [source_hash, mask_hash],
        {
            "revision_id": revision_id,
            "expected_polarity": expected_polarity,
            "adapter_version": adapter_version,
        },
    )
    with ProjectLock(layout, stage="mask_validation", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "mask_validation", revision_id)
        cached = _cached_manifest(
            package_root,
            layout,
            previous,
            "mask_validation",
            stage_key,
        )
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"mask-validation-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="mask_validation",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        commands: list[dict[str, Any]] = []
        try:
            source_probe = selected_adapter.probe(source)
            mask_probe = selected_adapter.probe(mask)
            source_video = _first_stream(source_probe, "video")
            mask_video = _first_stream(mask_probe, "video")
            if source_video is None or mask_video is None:
                raise MaskValidationError("mask validation requires source and mask video")
            if _first_stream(mask_probe, "audio") is not None:
                raise MaskValidationError("mask video must not contain an audio stream")
            source_frame_count = selected_adapter.probe_frame_count(source)
            mask_frame_count = selected_adapter.probe_frame_count(mask)
            if source_frame_count is None or mask_frame_count is None:
                raise MaskValidationError("mask validation requires decoded source and mask frames")
            sample_indices = sorted({0, source_frame_count // 2, source_frame_count - 1})
            samples: list[AlphaStatistics] = []
            for frame_index in sample_indices:
                result = selected_adapter.measure_mask(mask, frame_index=frame_index)
                commands.append(_command_record(result, stage_dir, adapter_version))
                samples.append(parse_alpha_statistics(result))
            decode_result = selected_adapter.full_decode_check(mask)
            commands.append(_command_record(decode_result, stage_dir, adapter_version))
            validation = validate_mask_alignment(
                source_probe,
                mask_probe,
                source_frame_count=source_frame_count,
                mask_frame_count=mask_frame_count,
                mask_samples=samples,
                expected_polarity=expected_polarity,
                full_decode_ok=decode_result.exit_code == 0,
            )
            if not validation.is_valid:
                failed_checks = [
                    name for name, status in validation.validation.items() if status != "pass"
                ]
                raise MaskValidationError("mask validation failed: " + ", ".join(failed_checks))
            if sha256_file(source) != source_hash or sha256_file(mask) != mask_hash:
                raise MaskValidationError("source or mask changed during mask validation")
            manifest_path = layout.artifacts / f"mask-validation-{stage_key[:16]}.json"
            payload: dict[str, Any] = {
                "schema_name": "mask_validation",
                "schema_version": "1.0.0",
                "artifact_id": "art_mask_validation",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("mask-validation", "ffmpeg", adapter_version),
                "inputs": [
                    artifact_input("art_source", source),
                    artifact_input("art_mask", mask),
                ],
                "config_sha256": config_sha256(layout),
                "status": "complete",
                "source": {
                    "path": str(source),
                    "sha256": source_hash,
                    "size_bytes": source.stat().st_size,
                },
                "mask": {
                    "path": str(mask),
                    "sha256": mask_hash,
                    "size_bytes": mask.stat().st_size,
                },
                "source_video": validation.source_video,
                "mask_video": validation.mask_video,
                "configuration": {
                    "expected_polarity": expected_polarity,
                    "sample_frame_indices": sample_indices,
                },
                "mask_statistics": validation.mask_statistics,
                "validation": validation.validation,
                "commands": commands,
                "warnings": list(dict.fromkeys(validation.warnings)),
            }
            write_validated_artifact(package_root, "mask_validation", manifest_path, payload)
            alias = layout.artifacts / "mask-validation.json"
            if not alias.exists():
                write_validated_artifact(package_root, "mask_validation", alias, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"manifest": manifest_path},
                warnings=list(dict.fromkeys(validation.warnings)),
            )
            return manifest_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="mask_validation_failed", message=message)
            raise MaskValidationError(message) from exc


def recolor_local_mask(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    mask: Path,
    output: Path | None = None,
    *,
    hue_degrees: float = 100,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    selected_adapter = adapter or FFmpegAdapter()
    validation_path = validate_local_mask(
        package_root,
        layout,
        source,
        mask,
        revision_id=revision_id,
        adapter=selected_adapter,
    )
    validation_hash = sha256_file(validation_path)
    source = source.expanduser().resolve()
    mask = mask.expanduser().resolve()
    source_hash = sha256_file(source)
    mask_hash = sha256_file(mask)
    adapter_version = str(selected_adapter.version()) or "unknown"
    encoder_identity = adapter_encoder_identity(selected_adapter)
    stage_key = make_stage_key(
        "mask_recolor",
        MASK_IMPLEMENTATION_VERSION,
        [source_hash, mask_hash, validation_hash],
        {
            "revision_id": revision_id,
            "hue_degrees": hue_degrees,
            "adapter_version": adapter_version,
            "encoder": encoder_identity,
            "requested_output": str(output.expanduser().resolve()) if output else None,
        },
    )
    with ProjectLock(layout, stage="mask_recolor", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "mask_recolor", revision_id)
        cached = _cached_manifest(
            package_root,
            layout,
            previous,
            "mask_recolor_manifest",
            stage_key,
        )
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"mask-recolor-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="mask_recolor",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        final_output = (
            _safe_output_path(layout, output)
            if output is not None
            else layout.work / "mask-recolor" / revision_id / stage_key / "recolored.mp4"
        )
        if final_output == source:
            error = MaskValidationError("mask effect output must differ from source")
            fail_stage(package_root, layout, state, code=error.code, message=error.message)
            raise error
        staged_output = stage_dir / "recolored.part.mp4"
        commands: list[dict[str, Any]] = []
        try:
            source_probe = selected_adapter.probe(source)
            source_video = _first_stream(source_probe, "video")
            source_audio = _first_stream(source_probe, "audio")
            if source_video is None or source_audio is None:
                raise MaskValidationError("mask recolor requires source video and production audio")
            source_frame_count = selected_adapter.probe_frame_count(source)
            if source_frame_count is None:
                raise MaskValidationError("mask recolor source frame count is unavailable")
            result = selected_adapter.recolor_with_mask(
                source,
                mask,
                staged_output,
                hue_degrees=hue_degrees,
            )
            commands.append(_command_record(result, stage_dir, adapter_version))
            output_probe = selected_adapter.probe(staged_output)
            output_video = _first_stream(output_probe, "video")
            output_audio = _first_stream(output_probe, "audio")
            if output_video is None or output_audio is None:
                raise MaskValidationError("mask recolor output lost video or production audio")
            output_frame_count = selected_adapter.probe_frame_count(staged_output)
            decode_result = selected_adapter.full_decode_check(staged_output)
            commands.append(_command_record(decode_result, stage_dir, adapter_version))
            source_rate = parse_rate(source_video.get("avg_frame_rate")) or parse_rate(
                source_video.get("r_frame_rate")
            )
            output_rate = parse_rate(output_video.get("avg_frame_rate")) or parse_rate(
                output_video.get("r_frame_rate")
            )
            source_duration = _stream_duration_us(source_probe, source_video)
            output_duration = _stream_duration_us(output_probe, output_video)
            source_audio_duration = _stream_duration_us(source_probe, source_audio)
            output_audio_duration = _stream_duration_us(output_probe, output_audio)
            frame_count_pass = output_frame_count == source_frame_count
            frame_rate_pass = _rational_equal(source_rate, output_rate)
            duration_pass = (
                source_duration is not None
                and output_duration is not None
                and abs(source_duration - output_duration) <= MASK_DURATION_TOLERANCE_US
            )
            audio_pass = (
                source_audio_duration is not None
                and output_audio_duration is not None
                and abs(source_audio_duration - output_audio_duration) <= MASK_DURATION_TOLERANCE_US
            )
            validation = {
                "full_decode": "pass" if decode_result.exit_code == 0 else "fail",
                "frame_count": "pass" if frame_count_pass else "fail",
                "frame_rate": "pass" if frame_rate_pass else "fail",
                "duration": "pass" if duration_pass else "fail",
                "production_audio": "pass" if audio_pass else "fail",
            }
            if any(value != "pass" for value in validation.values()):
                raise MaskValidationError(
                    "mask recolor validation failed: "
                    + ", ".join(name for name, value in validation.items() if value != "pass")
                )
            if sha256_file(source) != source_hash or sha256_file(mask) != mask_hash:
                raise MaskValidationError("source or mask changed during recolor")
            _promote_media(staged_output, final_output)
            manifest_path = layout.artifacts / f"mask-recolor-{stage_key[:16]}.json"
            output_video_payload = _video_payload(output_probe, output_video, output_frame_count)
            payload: dict[str, Any] = {
                "schema_name": "mask_recolor_manifest",
                "schema_version": "1.0.0",
                "artifact_id": "art_mask_recolor",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("mask-recolor", "ffmpeg", adapter_version),
                "inputs": [
                    artifact_input("art_source", source),
                    artifact_input("art_mask_validation", validation_path),
                ],
                "config_sha256": config_sha256(layout),
                "status": "complete",
                "source": {
                    "path": str(source),
                    "sha256": source_hash,
                    "size_bytes": source.stat().st_size,
                },
                "mask": {
                    "path": str(mask),
                    "sha256": mask_hash,
                    "size_bytes": mask.stat().st_size,
                },
                "output": {
                    "path": str(final_output),
                    "sha256": sha256_file(final_output),
                    "size_bytes": final_output.stat().st_size,
                },
                "configuration": {"hue_degrees": hue_degrees},
                "video": output_video_payload,
                "audio": {
                    "codec": str(output_audio.get("codec_name") or "unknown"),
                    "sample_rate_hz": int(output_audio.get("sample_rate") or 0),
                    "channels": int(output_audio.get("channels") or 0),
                    "duration_us": int(output_audio_duration or 0),
                },
                "validation": validation,
                "commands": commands,
                "warnings": [],
            }
            write_validated_artifact(package_root, "mask_recolor_manifest", manifest_path, payload)
            alias = layout.artifacts / "mask-recolor.json"
            if not alias.exists():
                write_validated_artifact(package_root, "mask_recolor_manifest", alias, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"manifest": manifest_path, "output_media": final_output},
                warnings=[],
            )
            return manifest_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="mask_recolor_failed", message=message)
            raise MaskValidationError(message) from exc
