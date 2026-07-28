from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from videoedit import __version__
from videoedit.adapters.process import ProcessResult
from videoedit.errors import (
    DiskSpaceError,
    MediaValidationError,
    SourceIntegrityError,
    VideoeditError,
)
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)

if TYPE_CHECKING:
    from videoedit.adapters.ffmpeg import FFmpegAdapter

SUPPORTED_VIDEO_CODECS = {"av1", "h264", "hevc", "mpeg4", "vp8", "vp9"}
SUPPORTED_AUDIO_CODECS = {"aac", "alac", "flac", "mp3", "opus", "pcm_s16le", "vorbis"}
MIN_PROXY_SPACE_BYTES = 64 * 1024 * 1024
CANONICAL_TIME_CONVERSION_VERSION = "canonical-time-v1.1"


def parse_seconds_to_us(value: object) -> int | None:
    """Parse decimal seconds into signed microseconds without binary-float drift."""

    if value is None or isinstance(value, bool) or value in ("N/A", ""):
        return None
    try:
        seconds = Decimal(str(value))
        if not seconds.is_finite():
            return None
        scaled = seconds * Decimal(1_000_000)
        return int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        return None


def seconds_to_us(value: object) -> int | None:
    parsed = parse_seconds_to_us(value)
    return max(0, parsed) if parsed is not None else None


def parse_rate(value: object) -> dict[str, int] | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        fraction = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    if fraction.numerator <= 0 or fraction.denominator <= 0:
        return None
    return {"numerator": fraction.numerator, "denominator": fraction.denominator}


def _int_or_none(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _rotation_degrees(stream: dict[str, Any]) -> float | None:
    candidates: list[object] = []
    tags = stream.get("tags")
    if isinstance(tags, dict):
        candidates.append(tags.get("rotate"))
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict):
                candidates.append(item.get("rotation"))
    for candidate in candidates:
        try:
            value = float(str(candidate))
        except (TypeError, ValueError):
            continue
        normalized = ((value + 180.0) % 360.0) - 180.0
        return 0.0 if abs(normalized) < 0.0001 else normalized
    return None


def normalize_probe(
    probe: dict[str, Any],
    project_id: str,
    revision_id: str,
    source_sha256: str,
    config_hash: str,
    ffprobe_version: str,
) -> dict[str, Any]:
    warnings: list[str] = []
    normalized_streams: list[dict[str, Any]] = []
    allowed_types = {"video", "audio", "subtitle", "data", "attachment"}
    raw_streams = probe.get("streams", [])
    if not isinstance(raw_streams, list):
        raise ValueError("ffprobe streams must be an array")
    for stream_value in raw_streams:
        if not isinstance(stream_value, dict):
            warnings.append("invalid_stream_record")
            continue
        stream: dict[str, Any] = stream_value
        stream_type = str(stream.get("codec_type") or "unknown")
        if stream_type not in allowed_types:
            stream_type = "unknown"
        frame_rate = None
        variable_frame_rate = False
        rotation_degrees = None
        if stream_type == "video":
            average_rate = parse_rate(stream.get("avg_frame_rate"))
            nominal_rate = parse_rate(stream.get("r_frame_rate"))
            frame_rate = average_rate or nominal_rate
            variable_frame_rate = (
                average_rate is not None
                and nominal_rate is not None
                and average_rate != nominal_rate
            )
            if frame_rate is None:
                warnings.append("missing_frame_rate")
            if variable_frame_rate:
                warnings.append("variable_frame_rate")
            rotation_degrees = _rotation_degrees(stream)
            if rotation_degrees not in (None, 0.0):
                warnings.append(f"rotation_metadata:{rotation_degrees:g}")
            codec_name = str(stream.get("codec_name") or "unknown")
            if codec_name not in SUPPORTED_VIDEO_CODECS:
                warnings.append(f"unsupported_video_codec:{codec_name}")
        elif stream_type == "audio":
            codec_name = str(stream.get("codec_name") or "unknown")
            if codec_name not in SUPPORTED_AUDIO_CODECS:
                warnings.append(f"unsupported_audio_codec:{codec_name}")
        else:
            codec_name = str(stream.get("codec_name") or "unknown")
        normalized_stream: dict[str, Any] = {
            "index": _int_or_none(stream.get("index")) or 0,
            "type": stream_type,
            "codec_name": codec_name,
            "time_base": str(stream["time_base"]) if stream.get("time_base") else None,
            "start_time_us": seconds_to_us(stream.get("start_time")),
            "duration_us": seconds_to_us(stream.get("duration")),
            "width": _int_or_none(stream.get("width")),
            "height": _int_or_none(stream.get("height")),
            "frame_rate": frame_rate,
            "sample_rate_hz": _int_or_none(stream.get("sample_rate")),
            "channels": _int_or_none(stream.get("channels")),
        }
        if stream_type == "video":
            normalized_stream.update(
                {
                    "rotation_degrees": rotation_degrees,
                    "pixel_aspect_ratio": (
                        str(stream.get("sample_aspect_ratio"))
                        if stream.get("sample_aspect_ratio") not in (None, "N/A", "0:1")
                        else None
                    ),
                    "color_space": stream.get("color_space"),
                    "color_primaries": stream.get("color_primaries"),
                    "color_transfer": stream.get("color_transfer"),
                    "color_range": stream.get("color_range"),
                    "variable_frame_rate": variable_frame_rate,
                }
            )
        normalized_streams.append(normalized_stream)

    format_data = probe.get("format", {})
    if not isinstance(format_data, dict):
        raise ValueError("ffprobe format must be an object")
    duration_us = seconds_to_us(format_data.get("duration"))
    if duration_us is None:
        stream_durations = [
            item["duration_us"] for item in normalized_streams if item["duration_us"] is not None
        ]
        if not stream_durations:
            raise ValueError("ffprobe did not report a usable media duration")
        duration_us = max(stream_durations)
        warnings.append("missing_container_duration:longest_stream_used")

    video_streams = [item for item in normalized_streams if item["type"] == "video"]
    audio_streams = [item for item in normalized_streams if item["type"] == "audio"]
    if not video_streams:
        warnings.append("missing_video_stream")
    if not audio_streams:
        warnings.append("missing_audio_stream")
    duration_values = [
        item["duration_us"] for item in normalized_streams if item["duration_us"] is not None
    ]
    if duration_values and max(duration_values) - min(duration_values) > 100_000:
        warnings.append("stream_duration_mismatch")
    start_values = [
        item["start_time_us"] for item in normalized_streams if item["start_time_us"] is not None
    ]
    if start_values and max(start_values) - min(start_values) > 100_000:
        warnings.append("stream_start_time_mismatch")

    return {
        "schema_name": "media_probe",
        "schema_version": "1.0.0",
        "artifact_id": "art_probe",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("ingest-probe", "ffprobe", ffprobe_version),
        "inputs": [{"artifact_id": "art_source", "sha256": source_sha256}],
        "config_sha256": config_hash,
        "format_name": str(format_data.get("format_name") or "unknown"),
        "duration_us": duration_us,
        "bit_rate_bps": _int_or_none(format_data.get("bit_rate")),
        "streams": normalized_streams,
        "ffprobe_version": ffprobe_version,
        "warnings": list(dict.fromkeys(warnings)),
        "status": "warning" if warnings else "valid",
    }


def preflight_disk_space(
    path: Path,
    required_bytes: int,
    *,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> int:
    if required_bytes < 0:
        raise ValueError("required_bytes must not be negative")
    usage = disk_usage_fn(path)
    free_value = getattr(usage, "free", None)
    available_bytes = int(free_value if free_value is not None else usage[2])
    if available_bytes < required_bytes:
        raise DiskSpaceError(
            f"insufficient disk space at {path}: need {required_bytes} bytes, "
            f"have {available_bytes} bytes"
        )
    return available_bytes


def _safe_source_name(source: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name).strip("._")
    return name or "source.bin"


def _ensure_managed_source(
    layout: ProjectLayout,
    source: Path,
    digest: str,
    stage_dir: Path,
) -> Path:
    layout.raw.mkdir(parents=True, exist_ok=True)
    target = layout.raw / f"{digest[:12]}-{_safe_source_name(source)}"
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise SourceIntegrityError(f"managed source is not a regular file: {target}")
        if sha256_file(target) != digest:
            raise SourceIntegrityError(f"managed source hash mismatch: {target}")
        return target

    for candidate in sorted(layout.raw.glob(f"{digest[:12]}-*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if sha256_file(candidate) == digest:
            candidate.chmod(
                candidate.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            )
            return candidate

    stage_copy = stage_dir / f"source-{digest}.part"
    stage_copy.parent.mkdir(parents=True, exist_ok=True)
    if stage_copy.exists():
        stage_copy.unlink()
    shutil.copyfile(source, stage_copy)
    if sha256_file(stage_copy) != digest:
        raise SourceIntegrityError("source hash changed while copying to staging")
    if target.exists():
        if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
            raise SourceIntegrityError(f"managed source appeared with a different hash: {target}")
        stage_copy.unlink(missing_ok=True)
        return target
    os.replace(stage_copy, target)
    target.chmod(target.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    if sha256_file(target) != digest:
        raise SourceIntegrityError(f"managed source failed post-promotion verification: {target}")
    return target


def _resolve_owned_path(layout: ProjectLayout, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise MediaValidationError("stage_artifact_invalid", "stage artifact path is missing")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = layout.root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise MediaValidationError(
            "stage_artifact_outside_project", f"stage artifact escapes the project: {value}"
        ) from exc
    return resolved


def _file_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        path = _resolve_owned_path(layout, value.get("path"))
    except MediaValidationError:
        raise
    return (
        path.is_file()
        and path.stat().st_size == int(value.get("size_bytes", -1))
        and sha256_file(path) == value.get("sha256")
    )


def _cached_ingest_result(
    package_root: Path,
    layout: ProjectLayout,
    state: dict[str, Any] | None,
    stage_key: str,
) -> dict[str, Any] | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    required_names = {"source_manifest", "media_probe"}
    if not required_names.issubset(artifacts):
        return None
    for name, reference in artifacts.items():
        if name in {"media_probe", "edit_proxy", "speech_proxy"} or name.endswith("_manifest"):
            if not _file_ref_valid(layout, reference):
                return None
    source_manifest_path = _resolve_owned_path(layout, artifacts["source_manifest"]["path"])
    probe_path = _resolve_owned_path(layout, artifacts["media_probe"]["path"])
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    probe_payload = json.loads(probe_path.read_text(encoding="utf-8"))
    if not isinstance(source_manifest, dict) or not isinstance(probe_payload, dict):
        return None
    validate_artifact(package_root, "source_manifest", source_manifest)
    validate_artifact(package_root, "media_probe", probe_payload)
    return source_manifest


def _first_stream(probe: dict[str, Any], stream_type: str) -> dict[str, Any] | None:
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        return None
    for stream_value in streams:
        if isinstance(stream_value, dict) and stream_value.get("codec_type") == stream_type:
            return stream_value
    return None


def _proxy_duration_us(probe: dict[str, Any]) -> int | None:
    format_data = probe.get("format")
    if isinstance(format_data, dict):
        duration = seconds_to_us(format_data.get("duration"))
        if duration is not None:
            return duration
    durations = [
        seconds_to_us(stream.get("duration"))
        for stream in probe.get("streams", [])
        if isinstance(stream, dict)
    ]
    usable = [value for value in durations if value is not None]
    return max(usable) if usable else None


def _command_record(
    result: ProcessResult,
    working_directory: Path,
    version: str,
    fallback_executable: str,
) -> dict[str, Any]:
    arguments = result.arguments or (fallback_executable,)
    return {
        "executable": arguments[0],
        "arguments": list(arguments[1:]),
        "working_directory": str(working_directory.resolve()),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "version": version or "unknown",
    }


def _proxy_stream_metadata(
    probe: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    video = _first_stream(probe, "video")
    audio = _first_stream(probe, "audio")
    video_metadata = None
    if video is not None:
        frame_rate = parse_rate(video.get("avg_frame_rate")) or parse_rate(
            video.get("r_frame_rate")
        )
        if frame_rate is None:
            raise MediaValidationError("proxy_missing_frame_rate", "proxy video has no frame rate")
        video_metadata = {
            "codec": str(video.get("codec_name") or "unknown"),
            "width": _int_or_none(video.get("width")) or 1,
            "height": _int_or_none(video.get("height")) or 1,
            "frame_rate": frame_rate,
            "pixel_format": str(video.get("pix_fmt") or "unknown"),
        }
    audio_metadata = None
    if audio is not None:
        audio_metadata = {
            "codec": str(audio.get("codec_name") or "unknown"),
            "sample_rate_hz": _int_or_none(audio.get("sample_rate")) or 1,
            "channels": _int_or_none(audio.get("channels")) or 1,
        }
    return video_metadata, audio_metadata


def _validate_proxy(
    adapter: Any,
    path: Path,
    *,
    kind: str,
    source_duration_us: int,
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise MediaValidationError("proxy_missing", f"{kind} proxy was not produced")
    probe_value = adapter.probe(path)
    if not isinstance(probe_value, dict):
        raise MediaValidationError("proxy_probe_invalid", f"{kind} proxy probe is not an object")
    probe: dict[str, Any] = probe_value
    video = _first_stream(probe, "video")
    audio = _first_stream(probe, "audio")
    if kind == "edit" and video is None:
        raise MediaValidationError("proxy_missing_video", "edit proxy has no video stream")
    if kind == "speech" and audio is None:
        raise MediaValidationError("proxy_missing_audio", "speech proxy has no audio stream")
    duration_us = _proxy_duration_us(probe)
    if duration_us is None or duration_us <= 0:
        raise MediaValidationError("proxy_missing_duration", f"{kind} proxy has no duration")
    if abs(duration_us - source_duration_us) > 250_000:
        raise MediaValidationError(
            "proxy_duration_mismatch",
            f"{kind} proxy duration differs from source by more than 250 milliseconds",
        )
    decode_check = getattr(adapter, "full_decode_check", None)
    if callable(decode_check):
        result = decode_check(path)
        if not isinstance(result, ProcessResult) or result.exit_code != 0:
            detail = getattr(result, "stderr", "") if result is not None else ""
            raise MediaValidationError(
                "proxy_decode_failed", f"{kind} proxy failed full decode: {str(detail)[-500:]}"
            )
    return probe


def ingest_and_probe(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    revision_id: str = "rev_001",
    copy_source: bool = True,
    adapter: FFmpegAdapter | Any | None = None,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise MediaValidationError("source_missing", f"source file does not exist: {source}")
    source_size = source.stat().st_size
    if source_size <= 0:
        raise MediaValidationError("source_empty", f"source file is empty: {source}")
    from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity

    adapter = adapter or FFmpegAdapter()
    digest = sha256_file(source)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.stage_state.mkdir(parents=True, exist_ok=True)
    layout.staging.mkdir(parents=True, exist_ok=True)
    config_hash = config_sha256(layout)
    encoder_identity = adapter_encoder_identity(adapter)
    required_bytes = max(MIN_PROXY_SPACE_BYTES, source_size * (4 if copy_source else 3))
    preflight_disk_space(layout.root, required_bytes, disk_usage_fn=disk_usage_fn)
    stage_key = make_stage_key(
        "ingest",
        __version__,
        [digest],
        {
            "copy_source": copy_source,
            "config_sha256": config_hash,
            "edit_proxy_profile": "h264_1280x720",
            "speech_proxy_profile": "pcm_s16le_16000_mono",
            "encoder": encoder_identity,
            "canonical_time_conversion": CANONICAL_TIME_CONVERSION_VERSION,
            "revision_id": revision_id,
        },
    )
    from videoedit.services.project import ProjectLock

    with ProjectLock(layout, stage="ingest", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "ingest", revision_id)
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"ingest-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        managed_path: Path | None = None
        if copy_source:
            managed_path = _ensure_managed_source(layout, source, digest, stage_dir)
            media_path = managed_path
        else:
            media_path = source

        cached = _cached_ingest_result(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached

        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="ingest",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        warnings: list[str] = []
        commands: dict[str, ProcessResult] = {}
        try:
            raw_probe = adapter.probe(media_path)
            if not isinstance(raw_probe, dict):
                raise MediaValidationError("probe_invalid", "ffprobe result must be a JSON object")
            ffprobe_path = str(getattr(adapter, "ffprobe_path", "ffprobe"))
            version_fn = getattr(adapter, "version", None)
            if not callable(version_fn):
                ffprobe_version = "unknown"
            else:
                try:
                    ffprobe_version = str(version_fn(ffprobe_path))
                except TypeError:
                    ffprobe_version = str(version_fn())
            probe_payload = normalize_probe(
                probe=raw_probe,
                project_id=layout.root.name,
                revision_id=revision_id,
                source_sha256=digest,
                config_hash=config_hash,
                ffprobe_version=ffprobe_version or "unknown",
            )
            if _first_stream(raw_probe, "video") is None:
                raise MediaValidationError("missing_video_stream", "source has no video stream")
            warnings.extend(str(value) for value in probe_payload["warnings"])
            source_duration_us = int(probe_payload["duration_us"])
            probe_path = layout.artifacts / "media-probe.json"
            command_working_directories: dict[str, Path] = {}

            create_edit_proxy = getattr(adapter, "create_edit_proxy", None)
            edit_probe: dict[str, Any] | None = None
            edit_proxy_path: Path | None = None
            if callable(create_edit_proxy):
                staged_edit = stage_dir / "edit-proxy.part.mp4"
                edit_result = create_edit_proxy(media_path, staged_edit)
                if not isinstance(edit_result, ProcessResult):
                    raise MediaValidationError(
                        "proxy_command_invalid", "edit proxy adapter did not return ProcessResult"
                    )
                if edit_result.exit_code != 0:
                    raise MediaValidationError(
                        "proxy_generation_failed", "edit proxy command failed"
                    )
                commands["edit"] = edit_result
                command_working_directories["edit"] = staged_edit.parent
                edit_probe = _validate_proxy(
                    adapter, staged_edit, kind="edit", source_duration_us=source_duration_us
                )
                final_dir = layout.work / "proxies" / revision_id / stage_key
                final_dir.mkdir(parents=True, exist_ok=True)
                edit_proxy_path = final_dir / "edit-proxy.mp4"
                if edit_proxy_path.exists():
                    if sha256_file(edit_proxy_path) != sha256_file(staged_edit):
                        raise SourceIntegrityError(
                            f"existing edit proxy has a different hash: {edit_proxy_path}"
                        )
                    staged_edit.unlink(missing_ok=True)
                else:
                    os.replace(staged_edit, edit_proxy_path)
            else:
                warnings.append("proxy_generation_unavailable:edit_adapter")

            speech_probe: dict[str, Any] | None = None
            speech_proxy_path: Path | None = None
            if _first_stream(raw_probe, "audio") is not None:
                create_speech_proxy = getattr(adapter, "create_speech_proxy", None)
                if callable(create_speech_proxy):
                    staged_speech = stage_dir / "speech-proxy.part.wav"
                    speech_result = create_speech_proxy(media_path, staged_speech)
                    if not isinstance(speech_result, ProcessResult):
                        raise MediaValidationError(
                            "proxy_command_invalid",
                            "speech proxy adapter did not return ProcessResult",
                        )
                    if speech_result.exit_code != 0:
                        raise MediaValidationError(
                            "proxy_generation_failed", "speech proxy command failed"
                        )
                    commands["speech"] = speech_result
                    command_working_directories["speech"] = staged_speech.parent
                    speech_probe = _validate_proxy(
                        adapter,
                        staged_speech,
                        kind="speech",
                        source_duration_us=source_duration_us,
                    )
                    final_dir = layout.work / "proxies" / revision_id / stage_key
                    final_dir.mkdir(parents=True, exist_ok=True)
                    speech_proxy_path = final_dir / "speech-proxy.wav"
                    if speech_proxy_path.exists():
                        if sha256_file(speech_proxy_path) != sha256_file(staged_speech):
                            raise SourceIntegrityError(
                                f"existing speech proxy has a different hash: {speech_proxy_path}"
                            )
                        staged_speech.unlink(missing_ok=True)
                    else:
                        os.replace(staged_speech, speech_proxy_path)
                else:
                    warnings.append("proxy_generation_unavailable:speech_adapter")
            else:
                warnings.append("missing_audio_stream")

            if sha256_file(source) != digest:
                raise SourceIntegrityError("source bytes changed during ingest")
            if managed_path is not None and sha256_file(managed_path) != digest:
                raise SourceIntegrityError("managed source bytes changed during ingest")

            write_validated_artifact(package_root, "media_probe", probe_path, probe_payload)
            proxy_artifact_ids: dict[str, str] = {}
            proxy_artifact_paths: dict[str, Path] = {}
            proxy_outputs: dict[str, Path] = {}
            version_fn = getattr(adapter, "version", None)
            if callable(version_fn):
                try:
                    ffmpeg_version = str(version_fn())
                except TypeError:
                    ffmpeg_version = str(version_fn(getattr(adapter, "ffmpeg_path", "ffmpeg")))
            else:
                ffmpeg_version = "unknown"
            for kind, proxy_probe, proxy_path in (
                ("edit", edit_probe, edit_proxy_path),
                ("speech", speech_probe, speech_proxy_path),
            ):
                if proxy_probe is None or proxy_path is None:
                    continue
                video_metadata, audio_metadata = _proxy_stream_metadata(proxy_probe)
                artifact_id = f"art_proxy_{kind}"
                artifact_path = layout.artifacts / f"media-proxy-{kind}.json"
                result = commands[kind]
                proxy_payload = {
                    "schema_name": "media_proxy",
                    "schema_version": "1.0.0",
                    "artifact_id": artifact_id,
                    "project_id": layout.root.name,
                    "revision_id": revision_id,
                    "created_at": now_iso(),
                    "producer": producer("ingest-proxy", "ffmpeg", ffmpeg_version),
                    "inputs": [{"artifact_id": "art_source", "sha256": digest}],
                    "config_sha256": config_hash,
                    "source_sha256": digest,
                    "stage_key": stage_key,
                    "kind": kind,
                    "profile": (
                        "edit_h264_1280x720" if kind == "edit" else "speech_pcm_s16le_16000_mono"
                    ),
                    "duration_us": _proxy_duration_us(proxy_probe),
                    "output": {
                        "path": str(proxy_path),
                        "sha256": sha256_file(proxy_path),
                        "size_bytes": proxy_path.stat().st_size,
                    },
                    "video": video_metadata,
                    "audio": audio_metadata,
                    "commands": [
                        _command_record(
                            result,
                            command_working_directories.get(kind, proxy_path.parent),
                            ffmpeg_version,
                            str(getattr(adapter, "ffmpeg_path", "ffmpeg")),
                        )
                    ],
                    "warnings": [],
                }
                write_validated_artifact(package_root, "media_proxy", artifact_path, proxy_payload)
                proxy_artifact_ids[kind] = artifact_id
                proxy_artifact_paths[kind] = artifact_path
                proxy_outputs[kind] = proxy_path

            source_payload: dict[str, Any] = {
                "schema_name": "source_manifest",
                "schema_version": "1.0.0",
                "artifact_id": "art_source",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("ingest", "local-filesystem"),
                "inputs": [],
                "config_sha256": config_hash,
                "ingest_mode": "copy" if copy_source else "reference",
                "source_path": str(source),
                "managed_path": str(managed_path) if managed_path else None,
                "sha256": digest,
                "size_bytes": source_size,
                "modified_at_ns": source.stat().st_mtime_ns,
                "media_duration_us": source_duration_us,
                "probe_artifact_id": "art_probe",
                "immutable": True,
                "ingest_stage_key": stage_key,
            }
            if proxy_artifact_ids:
                source_payload["proxy_artifact_ids"] = proxy_artifact_ids
            manifest_path = layout.artifacts / "source-manifest.json"
            write_validated_artifact(package_root, "source_manifest", manifest_path, source_payload)

            project_manifest_path = layout.state / "project-manifest.json"
            if project_manifest_path.is_file():
                project_manifest_value = json.loads(
                    project_manifest_path.read_text(encoding="utf-8")
                )
                if not isinstance(project_manifest_value, dict):
                    raise MediaValidationError(
                        "project_manifest_invalid", "project manifest is not an object"
                    )
                project_manifest = project_manifest_value
                project_manifest["updated_at"] = now_iso()
                project_manifest["state"] = "ingested"
                project_manifest["source_mode"] = "copy" if copy_source else "reference"
                project_manifest["source_artifact_id"] = "art_source"
                active_artifacts = project_manifest.setdefault("active_artifacts", {})
                if not isinstance(active_artifacts, dict):
                    raise MediaValidationError(
                        "project_manifest_invalid", "active_artifacts is not an object"
                    )
                active_artifacts["source"] = "art_source"
                active_artifacts["probe"] = "art_probe"
                for kind, artifact_id in proxy_artifact_ids.items():
                    active_artifacts[f"{kind}_proxy"] = artifact_id
                if "speech" not in proxy_artifact_ids:
                    active_artifacts.pop("speech_proxy", None)
                write_validated_artifact(
                    package_root, "project_manifest", project_manifest_path, project_manifest
                )

            state_artifacts: dict[str, Path] = {
                "source_manifest": manifest_path,
                "media_probe": probe_path,
                **{f"{kind}_manifest": path for kind, path in proxy_artifact_paths.items()},
                **{f"{kind}_proxy": path for kind, path in proxy_outputs.items()},
            }
            complete_stage(
                package_root,
                layout,
                state,
                artifacts=state_artifacts,
                warnings=list(dict.fromkeys(warnings)),
            )
            return source_payload
        except VideoeditError as exc:
            fail_stage(
                package_root,
                layout,
                state,
                code=exc.code,
                message=exc.message,
            )
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(
                package_root,
                layout,
                state,
                code="ingest_failed",
                message=message,
            )
            raise MediaValidationError("ingest_failed", message) from exc
