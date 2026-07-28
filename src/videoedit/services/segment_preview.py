from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.errors import PlanningValidationError, RenderOutputError, StateConflictError
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

DEFAULT_MAX_SEGMENT_DURATION_US = 10_000_000
DEFAULT_CONTEXT_BEFORE_US = 1_000_000
DEFAULT_CONTEXT_AFTER_US = 1_000_000
DEFAULT_GAP_MERGE_US = 1_000_000
OUTPUT_DURATION_TOLERANCE_US = 100_000
IMPLEMENTATION_VERSION = "p10-01b"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be a JSON object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _positive_range(start_us: object, end_us: object, description: str) -> tuple[int, int]:
    try:
        start = int(str(start_us))
        end = int(str(end_us))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{description} must contain integer bounds") from exc
    if start < 0 or end <= start:
        raise PlanningValidationError(f"{description} must be a positive half-open range")
    return start, end


def _media_info(adapter: FFmpegAdapter, path: Path) -> tuple[int, dict[str, int]]:
    probe = adapter.probe(path)
    format_data = probe.get("format")
    duration_us = (
        seconds_to_us(format_data.get("duration")) if isinstance(format_data, Mapping) else None
    )
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise PlanningValidationError("ffprobe did not return a stream list")
    video = next(
        (
            item
            for item in streams
            if isinstance(item, Mapping) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video, Mapping):
        raise PlanningValidationError("preview source has no video stream")
    rate = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    if rate is None:
        raise PlanningValidationError("preview source has no usable rational frame rate")
    if duration_us is None:
        stream_duration = seconds_to_us(video.get("duration"))
        if stream_duration is None or stream_duration <= 0:
            raise PlanningValidationError("preview source has no usable duration")
        duration_us = stream_duration
    if duration_us <= 0:
        raise PlanningValidationError("preview source duration must be positive")
    return duration_us, rate


def _transcript_media_duration(
    transcript: Mapping[str, Any],
    media_duration_us: int,
) -> int:
    """Select the transcript clock that is bound to the supplied media.

    Rebased revision transcripts retain the immutable source duration and add
    ``output_duration_us``. Segment previews for that revision consume the
    output media, while source previews consume ``source_duration_us``. Pick
    only a duration that already matches the probed media within the existing
    tolerance; never stretch the accepted range to make a mismatch pass.
    """

    candidates: list[tuple[str, int]] = []
    for field in ("source_duration_us", "output_duration_us"):
        value = transcript.get(field)
        if value is None:
            continue
        try:
            duration_us = int(str(value))
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError(f"preview transcript {field} must be an integer") from exc
        if duration_us <= 0:
            raise PlanningValidationError(f"preview transcript {field} must be positive")
        if abs(duration_us - media_duration_us) <= OUTPUT_DURATION_TOLERANCE_US:
            candidates.append((field, duration_us))
    if not candidates:
        source_duration = transcript.get("source_duration_us")
        output_duration = transcript.get("output_duration_us")
        raise PlanningValidationError(
            "preview transcript duration does not match media duration: "
            f"source={source_duration}, output={output_duration}, media={media_duration_us}"
        )
    # A source and output clock can be equal for an unretimed revision. When
    # they differ, matching both would be contradictory input evidence; prefer
    # the output clock only when it is the sole matching clock.
    if len(candidates) > 1 and len({duration for _, duration in candidates}) > 1:
        raise PlanningValidationError(
            f"preview transcript has multiple clocks matching media duration: {candidates}"
        )
    return candidates[0][1]


def _transcript_spans(
    transcript: Mapping[str, Any], duration_us: int
) -> list[tuple[int, int, str, list[str]]]:
    raw_segments = transcript.get("segments")
    if not isinstance(raw_segments, list):
        raise PlanningValidationError("transcript segments must be an array")
    spans: list[tuple[int, int, str, list[str]]] = []
    for index, value in enumerate(raw_segments, start=1):
        if not isinstance(value, Mapping):
            raise PlanningValidationError(f"transcript segment {index} must be an object")
        start_us, end_us = _positive_range(
            value.get("start_us"), value.get("end_us"), f"transcript segment {index}"
        )
        if end_us > duration_us:
            raise PlanningValidationError(
                f"transcript segment {index} ends after media duration: {end_us} > {duration_us}"
            )
        segment_id = str(value.get("segment_id", ""))
        if not segment_id:
            raise PlanningValidationError(f"transcript segment {index} has no segment_id")
        text = " ".join(str(value.get("text", "")).split())
        spans.append((start_us, end_us, text, [segment_id]))
    return sorted(spans, key=lambda item: (item[0], item[1], item[3][0]))


def _fixed_windows(duration_us: int, max_duration_us: int) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    start_us = 0
    ordinal = 1
    while start_us < duration_us:
        end_us = min(duration_us, start_us + max_duration_us)
        windows.append(
            {
                "segment_id": f"segment_{ordinal:06d}",
                "ordinal": ordinal,
                "label": f"Segment {ordinal}",
                "transcript_segment_ids": [],
                "source_range": {"start_us": start_us, "end_us": end_us},
            }
        )
        start_us = end_us
        ordinal += 1
    return windows


def _derive_segments(
    duration_us: int,
    transcript: Mapping[str, Any] | None,
    *,
    max_duration_us: int,
    context_before_us: int,
    context_after_us: int,
    gap_merge_us: int,
) -> tuple[list[dict[str, Any]], list[str], str]:
    if transcript is None:
        return (
            _fixed_windows(duration_us, max_duration_us),
            ["transcript_not_provided"],
            "fixed_windows",
        )
    spans = _transcript_spans(transcript, duration_us)
    if not spans:
        return (
            _fixed_windows(duration_us, max_duration_us),
            ["transcript_has_no_segments"],
            "fixed_windows",
        )

    groups: list[list[tuple[int, int, str, list[str]]]] = []
    for span in spans:
        if not groups:
            groups.append([span])
            continue
        current = groups[-1]
        current_end = current[-1][1]
        proposed_end = span[1]
        if (
            span[0] - current_end <= gap_merge_us
            and proposed_end - current[0][0] <= max_duration_us
        ):
            current.append(span)
        else:
            groups.append([span])

    derived: list[dict[str, Any]] = []
    for group in groups:
        raw_start = group[0][0]
        raw_end = group[-1][1]
        start_us = max(0, raw_start - context_before_us)
        end_us = min(duration_us, raw_end + context_after_us)
        transcript_ids = [segment_id for item in group for segment_id in item[3]]
        label = " ".join(item[2] for item in group if item[2]).strip() or "Untitled segment"
        if end_us - start_us <= max_duration_us:
            derived.append(
                {
                    "label": label,
                    "transcript_segment_ids": transcript_ids,
                    "source_range": {"start_us": start_us, "end_us": end_us},
                }
            )
            continue
        chunk_start = start_us
        while chunk_start < end_us:
            chunk_end = min(end_us, chunk_start + max_duration_us)
            overlapping_ids = [
                segment_id
                for item in group
                if item[1] > chunk_start and item[0] < chunk_end
                for segment_id in item[3]
            ]
            derived.append(
                {
                    "label": label,
                    "transcript_segment_ids": overlapping_ids,
                    "source_range": {"start_us": chunk_start, "end_us": chunk_end},
                }
            )
            chunk_start = chunk_end

    for ordinal, item in enumerate(derived, start=1):
        item["segment_id"] = f"segment_{ordinal:06d}"
        item["ordinal"] = ordinal
    return derived, [], "transcript"


def _probe_preview(
    adapter: FFmpegAdapter,
    path: Path,
    expected_duration_us: int,
) -> tuple[int, dict[str, str]]:
    probe = adapter.probe(path)
    duration_us, _rate = _media_info(adapter, path)
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    has_video = any(
        isinstance(item, Mapping) and item.get("codec_type") == "video" for item in streams
    )
    has_audio = any(
        isinstance(item, Mapping) and item.get("codec_type") == "audio" for item in streams
    )
    decode_result = adapter.full_decode_check(path)
    decode_ok = decode_result.exit_code == 0
    duration_ok = abs(duration_us - expected_duration_us) <= OUTPUT_DURATION_TOLERANCE_US
    diagnostics = {
        "decode": "pass" if decode_ok else "fail",
        "video_stream": "pass" if has_video else "fail",
        "audio_stream": "pass" if has_audio else "fail",
        "duration": "pass" if duration_ok else "fail",
    }
    if not all(value == "pass" for value in diagnostics.values()):
        raise RenderOutputError(
            f"segment preview failed diagnostics for {path.name}: {diagnostics} "
            f"(expected {expected_duration_us}us, actual {duration_us}us)"
        )
    return duration_us, diagnostics


def _failed_stage(path: Path) -> Path:
    failed = path.with_name(f"{path.name}.failed-{uuid.uuid4().hex}")
    os.replace(path, failed)
    return failed


def _existing_plan(path: Path, package_root: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = _read_object(path, "segment preview plan")
    validate_artifact(package_root, "segment_preview", value)
    return value


def write_segment_preview_plan(
    package_root: Path,
    layout: ProjectLayout,
    media_path: Path,
    transcript_path: Path | None = None,
    *,
    output: Path | None = None,
    revision_id: str = "rev_001",
    max_segment_duration_us: int = DEFAULT_MAX_SEGMENT_DURATION_US,
    context_before_us: int = DEFAULT_CONTEXT_BEFORE_US,
    context_after_us: int = DEFAULT_CONTEXT_AFTER_US,
    gap_merge_us: int = DEFAULT_GAP_MERGE_US,
    adapter: FFmpegAdapter | None = None,
) -> Path:
    if max_segment_duration_us <= 0:
        raise PlanningValidationError("max_segment_duration_us must be positive")
    for name, value in (
        ("context_before_us", context_before_us),
        ("context_after_us", context_after_us),
        ("gap_merge_us", gap_merge_us),
    ):
        if value < 0:
            raise PlanningValidationError(f"{name} must be nonnegative")
    source = _owned_path(layout, media_path, "preview media")
    if not source.is_file():
        raise PlanningValidationError(f"preview media does not exist: {source}")
    transcript = (
        _owned_path(layout, transcript_path, "preview transcript") if transcript_path else None
    )
    if transcript is not None and not transcript.is_file():
        raise PlanningValidationError(f"preview transcript does not exist: {transcript}")
    selected_adapter = adapter or FFmpegAdapter()

    with ProjectLock(layout, stage="segment_preview", revision_id=revision_id):
        duration_us, frame_rate = _media_info(selected_adapter, source)
        transcript_value = _read_object(transcript, "preview transcript") if transcript else None
        if transcript_value is not None:
            validate_artifact(package_root, "transcript", transcript_value)
            if str(transcript_value.get("project_id")) != layout.root.name:
                raise PlanningValidationError(
                    "preview transcript belongs to a different project: "
                    f"{transcript_value.get('project_id')} != {layout.root.name}"
                )
            if str(transcript_value.get("revision_id")) != revision_id:
                raise PlanningValidationError(
                    "preview transcript belongs to a different revision: "
                    f"{transcript_value.get('revision_id')} != {revision_id}"
                )
            _transcript_media_duration(transcript_value, duration_us)
        segment_specs, warnings, derived_from = _derive_segments(
            duration_us,
            transcript_value,
            max_duration_us=max_segment_duration_us,
            context_before_us=context_before_us,
            context_after_us=context_after_us,
            gap_merge_us=gap_merge_us,
        )
        media_sha = sha256_file(source)
        transcript_sha = sha256_file(transcript) if transcript else None
        config_hash = config_sha256(layout)
        policy = {
            "max_segment_duration_us": max_segment_duration_us,
            "context_before_us": context_before_us,
            "context_after_us": context_after_us,
            "gap_merge_us": gap_merge_us,
            "derived_from": derived_from,
        }
        planning_key = make_stage_key(
            "segment_preview",
            f"{__version__}:{IMPLEMENTATION_VERSION}",
            [media_sha, *([transcript_sha] if transcript_sha else [])],
            {
                "revision_id": revision_id,
                "duration_us": duration_us,
                "frame_rate": frame_rate,
                "policy": policy,
                "config_sha256": config_hash,
            },
        )
        output_path = _owned_path(
            layout,
            output or layout.artifacts / f"segment-preview-{planning_key[:16]}.json",
            "segment preview plan output",
        )
        prior = _existing_plan(output_path, package_root)
        if prior is not None and prior.get("planning_key") != planning_key:
            raise StateConflictError(
                f"segment preview output already binds a different planning key: {output_path}"
            )
        existing_segments = {
            str(item.get("segment_id")): item
            for item in (prior or {}).get("segments", [])
            if isinstance(item, Mapping)
        }
        segment_records: list[dict[str, Any]] = []
        for spec in segment_specs:
            segment_id = str(spec["segment_id"])
            source_range = spec["source_range"]
            start_us = int(source_range["start_us"])
            end_us = int(source_range["end_us"])
            expected_duration_us = end_us - start_us
            final_path = (
                layout.review / "segments" / segment_id / planning_key[:16] / "preview.mp4"
            ).resolve()
            previous = existing_segments.get(segment_id)
            status = "rendered"
            if final_path.is_file():
                if previous is None or str(previous.get("preview_sha256")) != sha256_file(
                    final_path
                ):
                    raise StateConflictError(
                        f"existing segment preview cannot be replaced safely: {final_path}"
                    )
                actual_duration_us = int(previous["actual_duration_us"])
                diagnostics = dict(previous["diagnostics"])
                preview_sha = sha256_file(final_path)
                status = str(previous.get("status", "rendered"))
            else:
                stage_path = (
                    layout.staging / "segment-previews" / planning_key / segment_id / "preview.mp4"
                ).resolve()
                stage_path.parent.mkdir(parents=True, exist_ok=True)
                if stage_path.is_file():
                    try:
                        actual_duration_us, diagnostics = _probe_preview(
                            selected_adapter, stage_path, expected_duration_us
                        )
                    except (PlanningValidationError, RenderOutputError):
                        _failed_stage(stage_path)
                        raise
                else:
                    selected_adapter.render_keep_ranges(
                        source,
                        [(start_us, end_us)],
                        stage_path,
                        crf=28,
                        preset="veryfast",
                    )
                    try:
                        actual_duration_us, diagnostics = _probe_preview(
                            selected_adapter, stage_path, expected_duration_us
                        )
                    except (PlanningValidationError, RenderOutputError):
                        if stage_path.is_file():
                            _failed_stage(stage_path)
                        raise
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage_path, final_path)
                preview_sha = sha256_file(final_path)
            segment_records.append(
                {
                    "segment_id": segment_id,
                    "ordinal": int(spec["ordinal"]),
                    "label": str(spec["label"]),
                    "transcript_segment_ids": list(spec["transcript_segment_ids"]),
                    "source_range": {"start_us": start_us, "end_us": end_us},
                    "preview_path": str(final_path),
                    "preview_sha256": preview_sha,
                    "expected_duration_us": expected_duration_us,
                    "actual_duration_us": actual_duration_us,
                    "frame_count": max(
                        1,
                        microseconds_to_frame(
                            actual_duration_us,
                            frame_rate["numerator"],
                            frame_rate["denominator"],
                        ),
                    ),
                    "diagnostics": diagnostics,
                    "status": status,
                }
            )

        created_at = str((prior or {}).get("created_at") or now_iso())
        inputs = [artifact_input("segment_preview_media", source)]
        if transcript is not None:
            inputs.append(artifact_input("segment_preview_transcript", transcript))
        payload: dict[str, Any] = {
            "schema_name": "segment_preview",
            "schema_version": "1.0.0",
            "artifact_id": f"art_segment_preview_{planning_key[:12]}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "created_at": created_at,
            "producer": producer("segment-preview", "ffmpeg", selected_adapter.version()),
            "inputs": inputs,
            "config_sha256": config_hash,
            "planning_key": planning_key,
            "source_media": {"path": str(source), "sha256": media_sha},
            "transcript": (
                {
                    "artifact_id": "segment_preview_transcript",
                    "path": str(transcript),
                    "sha256": transcript_sha,
                }
                if transcript is not None
                else None
            ),
            "duration_us": duration_us,
            "frame_rate": frame_rate,
            "policy": policy,
            "segments": segment_records,
            "warnings": warnings,
            "status": "warning" if warnings else "complete",
        }
        return write_validated_artifact(package_root, "segment_preview", output_path, payload)
