from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import ProcessResult
from videoedit.errors import SilenceDetectionError, VideoeditError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)
from videoedit.services.transcription import (
    seconds_to_us,
    source_from_manifest,
    speech_proxy_from_manifest,
)

SILENCE_START = re.compile(r"silence_start:\s*(\S+)")
SILENCE_END = re.compile(r"silence_end:\s*(\S+)")


@dataclass(frozen=True, slots=True)
class ParsedSilence:
    intervals: list[tuple[int, int]]
    warnings: list[str]


def _parse_seconds(value: str, label: str, warnings: list[str]) -> int | None:
    try:
        seconds = float(value)
    except ValueError:
        warnings.append(f"invalid_detector_time:{label}")
        return None
    if not math.isfinite(seconds):
        warnings.append(f"invalid_detector_time:{label}")
        return None
    if seconds < 0:
        warnings.append(f"negative_detector_time:{label}")
    return seconds_to_us(seconds)


def parse_silencedetect_detailed(log: str, duration_us: int) -> ParsedSilence:
    if duration_us <= 0:
        raise SilenceDetectionError("silence duration must be positive")
    warnings: list[str] = []
    raw_intervals: list[tuple[int, int]] = []
    current_start: int | None = None
    for line_number, line in enumerate(log.splitlines(), start=1):
        start_match = SILENCE_START.search(line)
        if start_match:
            parsed_start = _parse_seconds(
                start_match.group(1), f"start_line_{line_number}", warnings
            )
            if parsed_start is not None:
                if current_start is not None:
                    warnings.append("overlapping_detector_start")
                    current_start = min(current_start, parsed_start)
                else:
                    current_start = parsed_start
        end_match = SILENCE_END.search(line)
        if end_match:
            parsed_end = _parse_seconds(end_match.group(1), f"end_line_{line_number}", warnings)
            if parsed_end is None:
                continue
            if current_start is None:
                warnings.append("detector_end_without_start")
                continue
            start_us = min(max(current_start, 0), duration_us)
            end_us = min(max(parsed_end, 0), duration_us)
            if start_us != current_start or end_us != parsed_end:
                warnings.append("detector_interval_clamped")
            if end_us <= start_us:
                warnings.append("reversed_detector_interval")
            else:
                raw_intervals.append((start_us, end_us))
            current_start = None
    if current_start is not None:
        start_us = min(max(current_start, 0), duration_us)
        if start_us < duration_us:
            raw_intervals.append((start_us, duration_us))
            warnings.append("unterminated_detector_interval_closed_at_duration")
        else:
            warnings.append("unterminated_detector_interval_at_duration")

    intervals: list[tuple[int, int]] = []
    for start_us, end_us in sorted(raw_intervals):
        if intervals and start_us < intervals[-1][1]:
            previous_start, previous_end = intervals[-1]
            intervals[-1] = (previous_start, max(previous_end, end_us))
            warnings.append("overlapping_detector_intervals_merged")
        else:
            intervals.append((start_us, end_us))
    return ParsedSilence(intervals=intervals, warnings=list(dict.fromkeys(warnings)))


def parse_silencedetect(log: str, duration_us: int) -> list[tuple[int, int]]:
    """Backward-compatible interval-only parser."""

    return parse_silencedetect_detailed(log, duration_us).intervals


def nearest_words(
    words: list[dict[str, Any]], start_us: int, end_us: int
) -> tuple[str | None, str | None]:
    ordered = sorted(words, key=lambda word: (int(word["start_us"]), int(word["end_us"])))
    before = [word for word in ordered if int(word["end_us"]) <= start_us]
    after = [word for word in ordered if int(word["start_us"]) >= end_us]
    return (
        str(before[-1]["word_id"]) if before else None,
        str(after[0]["word_id"]) if after else None,
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
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return False
    path = Path(str(value["path"])).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise SilenceDetectionError("silence stage artifact escapes the project") from exc
    return (
        path.is_file()
        and path.stat().st_size == int(value.get("size_bytes", -1))
        and sha256_file(path) == value.get("sha256")
    )


def _cached_silence(
    package_root: Path,
    layout: ProjectLayout,
    state: dict[str, Any] | None,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict) or "silence_intervals" not in artifacts:
        return None
    if not all(_stage_file_ref_valid(layout, value) for value in artifacts.values()):
        return None
    output = Path(str(artifacts["silence_intervals"]["path"])).expanduser().resolve()
    payload = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    try:
        validate_artifact(package_root, "silence_intervals", payload)
    except ValueError:
        return None
    return output


def _mark_project_silence(
    package_root: Path,
    layout: ProjectLayout,
    output: Path,
) -> None:
    manifest_path = layout.state / "project-manifest.json"
    if not manifest_path.is_file():
        return
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SilenceDetectionError("project manifest is not an object")
    value["updated_at"] = now_iso()
    value["state"] = "analyzed"
    active_artifacts = value.setdefault("active_artifacts", {})
    if not isinstance(active_artifacts, dict):
        raise SilenceDetectionError("project manifest active_artifacts is not an object")
    active_artifacts["silence"] = "art_silence"
    write_validated_artifact(package_root, "project_manifest", manifest_path, value)


def detect_project_silence(
    package_root: Path,
    layout: ProjectLayout,
    threshold_db: float = -38.0,
    minimum_duration_us: int = 650_000,
    revision_id: str = "rev_001",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    if threshold_db >= 0:
        raise ValueError("silence threshold must be negative")
    if minimum_duration_us <= 0:
        raise ValueError("minimum silence duration must be positive")
    _source, source_manifest_path, source_manifest = source_from_manifest(layout)
    speech_proxy, speech_manifest_path, speech_manifest = speech_proxy_from_manifest(
        package_root, layout
    )
    duration_value = source_manifest.get("media_duration_us")
    if not isinstance(duration_value, int) or duration_value <= 0:
        raise SilenceDetectionError("source manifest has no positive media duration")
    transcript_path = layout.artifacts / "transcript.json"
    transcript: dict[str, Any] | None = None
    if transcript_path.is_file():
        transcript_value = json.loads(transcript_path.read_text(encoding="utf-8"))
        if not isinstance(transcript_value, dict):
            raise SilenceDetectionError("transcript artifact must contain an object")
        try:
            validate_artifact(package_root, "transcript", transcript_value)
        except ValueError as exc:
            raise SilenceDetectionError(f"transcript artifact is invalid: {exc}") from exc
        transcript = transcript_value
    adapter = adapter or FFmpegAdapter()
    adapter_version = str(adapter.version())
    transcript_hash = sha256_file(transcript_path) if transcript_path.is_file() else ""
    stage_key = make_stage_key(
        "silence",
        __version__,
        [str(source_manifest["sha256"]), str(speech_manifest["output"]["sha256"]), transcript_hash],
        {
            "schema_version": "1.0.0",
            "config_sha256": config_sha256(layout),
            "threshold_db": threshold_db,
            "minimum_duration_us": minimum_duration_us,
            "adapter_version": adapter_version,
        },
    )
    with ProjectLock(layout, stage="silence", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "silence", revision_id)
        cached = _cached_silence(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"silence-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="silence",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        try:
            result = adapter.detect_silence_result(
                speech_proxy,
                threshold_db=threshold_db,
                minimum_duration_us=minimum_duration_us,
            )
            if not isinstance(result, ProcessResult) or result.exit_code != 0:
                raise SilenceDetectionError("silence detector did not complete successfully")
            parsed = parse_silencedetect_detailed(result.stderr, duration_value)
            warnings = list(parsed.warnings)
            words = transcript.get("words", []) if transcript is not None else []
            if transcript is None:
                warnings.append("transcript_missing_for_classification")
            normalized: list[dict[str, Any]] = []
            for index, (start_us, end_us) in enumerate(parsed.intervals, start=1):
                before, after = nearest_words(words, start_us, end_us)
                if start_us <= 50_000:
                    classification = "leading"
                elif end_us >= duration_value - 50_000:
                    classification = "trailing"
                elif before is not None and after is not None:
                    classification = "inter_word"
                else:
                    classification = "uncertain"
                normalized.append(
                    {
                        "interval_id": f"sil_{index:06d}",
                        "start_us": start_us,
                        "end_us": end_us,
                        "classification": classification,
                        "detector_threshold_db": threshold_db,
                        "nearest_word_before": before,
                        "nearest_word_after": after,
                    }
                )

            log_path = layout.logs / "silencedetect" / f"{stage_key}-attempt-{attempt}.log"
            write_text_atomically(log_path, result.stderr)
            detector_version = adapter_version or "unknown"
            inputs = [
                artifact_input("art_source", source_manifest_path),
                artifact_input("art_proxy_speech", speech_manifest_path),
            ]
            if transcript_path.is_file():
                inputs.append(artifact_input("art_transcript", transcript_path))
            payload = {
                "schema_name": "silence_intervals",
                "schema_version": "1.0.0",
                "artifact_id": "art_silence",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("silence-detection", "ffmpeg-silencedetect", detector_version),
                "inputs": inputs,
                "config_sha256": config_sha256(layout),
                "source_duration_us": duration_value,
                "minimum_duration_us": minimum_duration_us,
                "intervals": normalized,
                "detector_log": {
                    "path": str(log_path),
                    "sha256": sha256_file(log_path),
                    "size_bytes": log_path.stat().st_size,
                },
                "detector_command": _command_record(result, speech_proxy.parent, detector_version),
                "detector_version": detector_version,
                "warnings": list(dict.fromkeys(warnings)),
                "status": "warning" if warnings else "complete",
            }
            output = layout.artifacts / "silence-intervals.json"
            write_validated_artifact(package_root, "silence_intervals", output, payload)
            _mark_project_silence(package_root, layout, output)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"silence_intervals": output, "detector_log": log_path},
                warnings=list(dict.fromkeys(warnings)),
            )
            return output
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
                code="silence_detection_failed",
                message=message,
            )
            raise SilenceDetectionError(message) from exc
