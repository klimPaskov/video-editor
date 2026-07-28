from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import ChromaKeyConfig, FFmpegAdapter
from videoedit.adapters.process import ProcessResult
from videoedit.errors import ForegroundValidationError, VideoeditError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)

ALPHA_PIX_FMT_PREFIXES = ("yuva", "rgba", "argb", "abgr", "gbrap", "ayuv", "ya")
FRAME_DURATION_FALLBACK_US = 100_000
FOREGROUND_IMPLEMENTATION_VERSION = f"{__version__}:foreground-v2"


@dataclass(frozen=True, slots=True)
class AlphaStatistics:
    minimum: float
    maximum: float
    mean: float


@dataclass(frozen=True, slots=True)
class ForegroundValidation:
    video: dict[str, Any]
    alpha: dict[str, Any]
    validation: dict[str, str]
    warnings: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return all(value == "pass" for value in self.validation.values()) and bool(
            self.alpha.get("polarity") == "mixed"
        )


def parse_alpha_statistics(result: ProcessResult | str) -> AlphaStatistics:
    """Parse signalstats values from the alpha-extraction diagnostic."""

    text = (
        "\n".join(value for value in (result.stdout, result.stderr) if value)
        if isinstance(result, ProcessResult)
        else str(result)
    )
    values: dict[str, float] = {}
    for name in ("YMIN", "YMAX", "YAVG"):
        match = re.search(
            rf"(?:lavfi\.)?signalstats\.{name}\s*=\s*(-?(?:\d+(?:\.\d*)?|\.\d+))",
            text,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise ForegroundValidationError(f"alpha diagnostic is missing signalstats {name}")
        try:
            value = float(match.group(1))
        except ValueError as exc:
            raise ForegroundValidationError(
                f"alpha diagnostic contains an invalid signalstats {name}"
            ) from exc
        if not math.isfinite(value):
            raise ForegroundValidationError(f"alpha diagnostic {name} is not finite")
        values[name] = value
    return AlphaStatistics(
        minimum=values["YMIN"],
        maximum=values["YMAX"],
        mean=values["YAVG"],
    )


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


def _has_alpha_pixel_format(pixel_format: str) -> bool:
    normalized = pixel_format.lower()
    return normalized.startswith(ALPHA_PIX_FMT_PREFIXES)


def _rational_equal(left: object, right: object) -> bool:
    return (
        isinstance(left, Mapping)
        and isinstance(right, Mapping)
        and (
            int(left.get("numerator", 0)) == int(right.get("numerator", -1))
            and int(left.get("denominator", 0)) == int(right.get("denominator", -1))
        )
    )


def validate_foreground_output(
    source_probe: Mapping[str, Any],
    output_probe: Mapping[str, Any],
    *,
    source_frame_count: int | None,
    output_frame_count: int | None,
    alpha_samples: Sequence[AlphaStatistics],
    crop: tuple[int, int, int, int] | None = None,
    full_decode_ok: bool = True,
) -> ForegroundValidation:
    """Return independent media and alpha checks for a transparent foreground."""

    source_video = _first_stream(source_probe, "video")
    output_video = _first_stream(output_probe, "video")
    if source_video is None or output_video is None:
        raise ForegroundValidationError("foreground validation requires source and output video")

    source_rate = parse_rate(source_video.get("avg_frame_rate")) or parse_rate(
        source_video.get("r_frame_rate")
    )
    output_rate = parse_rate(output_video.get("avg_frame_rate")) or parse_rate(
        output_video.get("r_frame_rate")
    )
    source_width = int(source_video.get("width") or 0)
    source_height = int(source_video.get("height") or 0)
    expected_width = crop[2] if crop is not None else source_width
    expected_height = crop[3] if crop is not None else source_height
    output_width = int(output_video.get("width") or 0)
    output_height = int(output_video.get("height") or 0)
    source_duration_us = _stream_duration_us(source_probe, source_video)
    output_duration_us = _stream_duration_us(output_probe, output_video)
    frame_duration_us = (
        round(1_000_000 * output_rate["denominator"] / output_rate["numerator"])
        if output_rate is not None
        else FRAME_DURATION_FALLBACK_US
    )

    dimensions_pass = (
        output_width == expected_width
        and output_height == expected_height
        and output_width > 0
        and output_height > 0
    )
    frame_count_pass = (
        source_frame_count is not None
        and output_frame_count is not None
        and source_frame_count > 0
        and output_frame_count == source_frame_count
    )
    frame_rate_pass = _rational_equal(source_rate, output_rate)
    duration_pass = (
        source_duration_us is not None
        and output_duration_us is not None
        and abs(output_duration_us - source_duration_us)
        <= max(FRAME_DURATION_FALLBACK_US, frame_duration_us * 2)
    )
    alpha_values = [
        value for sample in alpha_samples for value in (sample.minimum, sample.maximum, sample.mean)
    ]
    alpha_range_pass = bool(alpha_values) and all(
        math.isfinite(value) and 0 <= value <= 255 for value in alpha_values
    )
    alpha_minimum = min((sample.minimum for sample in alpha_samples), default=0.0)
    alpha_maximum = max((sample.maximum for sample in alpha_samples), default=0.0)
    alpha_mean = (
        sum(sample.mean for sample in alpha_samples) / len(alpha_samples) if alpha_samples else 0.0
    )
    if not alpha_range_pass:
        polarity = "unknown"
    elif alpha_minimum < alpha_maximum:
        polarity = "mixed"
    elif alpha_minimum >= 255 and alpha_maximum >= 255:
        polarity = "opaque"
    elif alpha_minimum <= 0 and alpha_maximum <= 0:
        polarity = "transparent"
    else:
        polarity = "unknown"
    alpha_plane_pass = _has_alpha_pixel_format(str(output_video.get("pix_fmt") or "")) and bool(
        alpha_samples
    )
    warnings: list[str] = []
    if crop is not None:
        warnings.append("foreground_output_is_cropped")
    if output_frame_count is not None and source_frame_count is not None:
        if output_frame_count != source_frame_count:
            warnings.append(f"frame_count_mismatch:{source_frame_count}:{output_frame_count}")

    return ForegroundValidation(
        video={
            "codec": str(output_video.get("codec_name") or "unknown"),
            "width": output_width,
            "height": output_height,
            "frame_rate": output_rate or {"numerator": 0, "denominator": 1},
            "pixel_format": str(output_video.get("pix_fmt") or "unknown"),
            "frame_count": int(output_frame_count or 0),
            "duration_us": int(output_duration_us or 0),
        },
        alpha={
            "min": alpha_minimum,
            "max": alpha_maximum,
            "mean": alpha_mean,
            "polarity": polarity,
            "sampled_frames": len(alpha_samples),
        },
        validation={
            "full_decode": "pass" if full_decode_ok else "fail",
            "alpha_plane": "pass" if alpha_plane_pass else "fail",
            "alpha_range": "pass" if alpha_range_pass else "fail",
            "dimensions": "pass" if dimensions_pass else "fail",
            "frame_count": "pass" if frame_count_pass else "fail",
            "frame_rate": "pass" if frame_rate_pass else "fail",
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


def _cached_foreground(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, Any] | None,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping) or not all(
        name in artifacts for name in ("foreground_manifest", "output_media")
    ):
        return None
    if not all(_stage_file_ref_valid(layout, value) for value in artifacts.values()):
        return None
    manifest_path = Path(str(artifacts["foreground_manifest"]["path"])).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        validate_artifact(package_root, "foreground_manifest", payload)
    except (OSError, json.JSONDecodeError, ValueError, KeyError):
        return None
    return manifest_path


def _safe_output_path(layout: ProjectLayout, output: Path) -> Path:
    resolved = output.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise ForegroundValidationError("foreground output must stay inside the project") from exc
    try:
        resolved.relative_to(layout.raw.resolve())
    except ValueError:
        return resolved
    raise ForegroundValidationError("foreground output must not be written under raw sources")


def _promote_media(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != sha256_file(staged):
            raise ForegroundValidationError(
                f"immutable foreground target already exists with another hash: {target}"
            )
        staged.unlink(missing_ok=True)
        return
    os.replace(staged, target)


def render_chroma_key_foreground(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    output: Path | None = None,
    *,
    config: ChromaKeyConfig | None = None,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    """Render and validate a hash-bound, alpha-bearing local foreground."""

    source = source.expanduser().resolve()
    if not source.is_file():
        raise ForegroundValidationError(f"chroma-key source does not exist: {source}")
    selected_config = config or ChromaKeyConfig()
    selected_adapter = adapter or FFmpegAdapter()
    source_hash = sha256_file(source)
    adapter_version = str(selected_adapter.version()) or "unknown"
    stage_key = make_stage_key(
        "chroma_key",
        FOREGROUND_IMPLEMENTATION_VERSION,
        [source_hash],
        {
            "revision_id": revision_id,
            "configuration": asdict(selected_config),
            "adapter_version": adapter_version,
            "requested_output": str(output.expanduser().resolve()) if output else None,
        },
    )
    with ProjectLock(layout, stage="chroma_key", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "chroma_key", revision_id)
        cached = _cached_foreground(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"chroma-key-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="chroma_key",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        final_output = (
            _safe_output_path(layout, output)
            if output is not None
            else layout.work / "foreground" / revision_id / stage_key / "foreground.mov"
        )
        if final_output == source:
            error = ForegroundValidationError("foreground output must differ from its source")
            fail_stage(package_root, layout, state, code=error.code, message=error.message)
            raise error
        staged_output = stage_dir / "foreground.part.mov"
        manifest_path = layout.artifacts / f"foreground-{stage_key[:16]}.json"
        commands: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            source_probe = selected_adapter.probe(source)
            source_video = _first_stream(source_probe, "video")
            if source_video is None:
                raise ForegroundValidationError("chroma-key source has no video stream")
            source_frame_count = selected_adapter.probe_frame_count(source)
            if source_frame_count is None or source_frame_count <= 0:
                raise ForegroundValidationError("chroma-key source has no decoded frame count")
            selected_adapter.chroma_key_foreground(
                source,
                staged_output,
                config=selected_config,
            )
            output_probe = selected_adapter.probe(staged_output)
            output_video = _first_stream(output_probe, "video")
            if output_video is None:
                raise ForegroundValidationError("chroma-key output has no video stream")
            if _first_stream(output_probe, "audio") is not None:
                raise ForegroundValidationError("chroma-key foreground must not contain audio")
            output_frame_count = selected_adapter.probe_frame_count(staged_output)
            if output_frame_count is None or output_frame_count <= 0:
                raise ForegroundValidationError("chroma-key output has no decoded frame count")
            output_rate = parse_rate(output_video.get("avg_frame_rate")) or parse_rate(
                output_video.get("r_frame_rate")
            )
            if output_rate is None:
                raise ForegroundValidationError("chroma-key output has no rational frame rate")
            sample_indices = sorted({0, source_frame_count // 2, source_frame_count - 1})
            alpha_samples: list[AlphaStatistics] = []
            for frame_index in sample_indices:
                alpha_result = selected_adapter.measure_alpha(
                    staged_output,
                    frame_index=frame_index,
                )
                commands.append(
                    _command_record(alpha_result, staged_output.parent, adapter_version)
                )
                alpha_samples.append(parse_alpha_statistics(alpha_result))
            decode_result = selected_adapter.full_decode_check(staged_output)
            commands.append(_command_record(decode_result, staged_output.parent, adapter_version))
            validation = validate_foreground_output(
                source_probe,
                output_probe,
                source_frame_count=source_frame_count,
                output_frame_count=output_frame_count,
                alpha_samples=alpha_samples,
                crop=selected_config.crop,
                full_decode_ok=decode_result.exit_code == 0,
            )
            if not validation.is_valid:
                failed_checks = [
                    name for name, status in validation.validation.items() if status != "pass"
                ]
                if validation.alpha.get("polarity") != "mixed":
                    failed_checks.append(
                        f"alpha_polarity={validation.alpha.get('polarity', 'unknown')}"
                    )
                raise ForegroundValidationError(
                    "foreground validation failed: " + ", ".join(failed_checks)
                )
            warnings.extend(validation.warnings)
            _promote_media(staged_output, final_output)
            payload: dict[str, Any] = {
                "schema_name": "foreground_manifest",
                "schema_version": "1.0.0",
                "artifact_id": "art_foreground",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("chroma-key", "ffmpeg", adapter_version),
                "inputs": [artifact_input("art_source", source)],
                "config_sha256": config_sha256(layout),
                "status": "complete",
                "source": {
                    "path": str(source),
                    "sha256": source_hash,
                    "size_bytes": source.stat().st_size,
                },
                "output": {
                    "path": str(final_output),
                    "sha256": sha256_file(final_output),
                    "size_bytes": final_output.stat().st_size,
                },
                "configuration": asdict(selected_config),
                "video": validation.video,
                "alpha": validation.alpha,
                "validation": validation.validation,
                "commands": commands,
                "warnings": list(dict.fromkeys(warnings)),
            }
            write_validated_artifact(package_root, "foreground_manifest", manifest_path, payload)
            alias = layout.artifacts / "foreground.json"
            if not alias.exists():
                write_validated_artifact(package_root, "foreground_manifest", alias, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"foreground_manifest": manifest_path, "output_media": final_output},
                warnings=list(dict.fromkeys(warnings)),
            )
            return manifest_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="foreground_failed", message=message)
            raise ForegroundValidationError(message) from exc
