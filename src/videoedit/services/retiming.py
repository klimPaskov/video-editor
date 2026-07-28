from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.errors import RenderOutputError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.focus_pacing import PromptSpeedup, TimeRange
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

RETIMED_SCHEMA_VERSION = "1.0.0"
ZERO_SHA256 = "0" * 64


class RetimingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetimedSegment(RetimingModel):
    segment_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    operation: Literal["keep", "prompt_speedup"]
    source_range: TimeRange
    output_range: TimeRange
    playback_rate: float = Field(ge=1, le=8)
    audio_mode: Literal["source", "audible_pitch_preserved", "audible", "muted"]
    speedup_id: str | None = None
    command_strategy: Literal["passthrough", "ffmpeg_trim_setpts_atempo"]
    boundary_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_operation(self) -> RetimedSegment:
        if self.operation == "keep":
            if (
                self.playback_rate != 1
                or self.audio_mode != "source"
                or self.speedup_id is not None
                or self.command_strategy != "passthrough"
                or self.boundary_confidence is not None
            ):
                raise ValueError("keep segment has inconsistent retiming fields")
            if self.output_range.end_us - self.output_range.start_us != (
                self.source_range.end_us - self.source_range.start_us
            ):
                raise ValueError("keep segment must preserve duration")
        else:
            if self.playback_rate <= 1 or self.speedup_id is None:
                raise ValueError("speed-up segment requires rate greater than one and an id")
            if self.audio_mode == "source":
                raise ValueError("speed-up segment cannot use source audio mode")
            if self.command_strategy != "ffmpeg_trim_setpts_atempo":
                raise ValueError("speed-up segment must use the deterministic ffmpeg strategy")
            if self.boundary_confidence is None:
                raise ValueError("speed-up segment requires boundary confidence")
            expected = _scaled_duration_us(
                self.source_range.end_us - self.source_range.start_us,
                self.playback_rate,
            )
            actual = self.output_range.end_us - self.output_range.start_us
            if actual != expected:
                raise ValueError(
                    "speed-up output duration does not match its rational playback rate"
                )
        return self


class RetimedTimeline(RetimingModel):
    schema_name: Literal["retimed_timeline"] = "retimed_timeline"
    schema_version: Literal["1.0.0"] = "1.0.0"
    artifact_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    revision_id: str = Field(pattern=r"^rev_[0-9]{3,}$")
    created_at: str
    producer: dict[str, str]
    inputs: list[dict[str, str]]
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    edit_decision_list_artifact_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    edit_decision_list_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    focus_pacing_plan_artifact_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    focus_pacing_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_duration_us: int = Field(ge=0)
    output_duration_us: int = Field(ge=0)
    segments: list[RetimedSegment] = Field(min_length=1)
    warnings: list[str]

    @model_validator(mode="after")
    def validate_mapping(self) -> RetimedTimeline:
        if self.output_duration_us <= 0:
            raise ValueError("retimed timeline must have positive output duration")
        output_cursor = 0
        previous_source_end = -1
        for segment in self.segments:
            if segment.source_range.end_us > self.source_duration_us:
                raise ValueError("retimed segment exceeds source duration")
            if segment.source_range.start_us < previous_source_end:
                raise ValueError("retimed source segments overlap or are out of order")
            if segment.output_range.start_us != output_cursor:
                raise ValueError("retimed output segments must be contiguous from zero")
            previous_source_end = segment.source_range.end_us
            output_cursor = segment.output_range.end_us
        if output_cursor != self.output_duration_us:
            raise ValueError("retimed output duration does not match the final segment")
        return self


def _scaled_duration_us(duration_us: int, playback_rate: float) -> int:
    if duration_us < 0 or playback_rate <= 0:
        raise ValueError("duration and playback rate must be positive")
    rate = Fraction(str(playback_rate))
    return int(Fraction(duration_us, 1) / rate + Fraction(1, 2))


def _coerce_range(value: Mapping[str, Any] | Sequence[int], label: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if "source_range" in value and isinstance(value["source_range"], Mapping):
            value = value["source_range"]
        try:
            if "source_start_us" in value:
                start = int(value["source_start_us"])
                end = int(value["source_end_us"])
            else:
                start = int(value["start_us"])
                end = int(value["end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{label} must contain an integer source range") from exc
    else:
        if len(value) != 2:
            raise ValueError(f"{label} must contain exactly two values")
        start, end = int(value[0]), int(value[1])
    if start < 0 or end <= start:
        raise ValueError(f"{label} must be a positive half-open range")
    return start, end


def _coerce_keep_ranges(
    source_duration_us: int,
    keep_ranges: Sequence[Mapping[str, Any] | Sequence[int]] | None,
) -> list[tuple[int, int]]:
    ranges = keep_ranges or [(0, source_duration_us)]
    normalized = [_coerce_range(value, "keep range") for value in ranges]
    previous_end = -1
    for start, end in normalized:
        if end > source_duration_us:
            raise ValueError("keep range exceeds source duration")
        if start < previous_end:
            raise ValueError("keep ranges overlap or are out of order")
        previous_end = end
    return normalized


def _coerce_speedup(value: PromptSpeedup | Mapping[str, Any]) -> PromptSpeedup:
    return value if isinstance(value, PromptSpeedup) else PromptSpeedup.model_validate(dict(value))


def compile_retimed_timeline(
    *,
    package_root: Path,
    project_id: str,
    revision_id: str,
    source_duration_us: int,
    keep_ranges: Sequence[Mapping[str, Any] | Sequence[int]] | None,
    speedups: Sequence[PromptSpeedup | Mapping[str, Any]],
    edit_decision_list_sha256: str = ZERO_SHA256,
    focus_pacing_plan_sha256: str = ZERO_SHA256,
    edit_decision_list_artifact_id: str = "art_edl",
    focus_pacing_plan_artifact_id: str = "art_focus_pacing",
    config_hash: str = ZERO_SHA256,
    approved_speedup_ids: set[str] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    if source_duration_us <= 0:
        raise ValueError("source duration must be positive")
    for label, digest in (
        ("edit_decision_list_sha256", edit_decision_list_sha256),
        ("focus_pacing_plan_sha256", focus_pacing_plan_sha256),
        ("config_sha256", config_hash),
    ):
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"{label} must be a lowercase SHA-256 hash")
    selected_keep = _coerce_keep_ranges(source_duration_us, keep_ranges)
    candidates = [_coerce_speedup(value) for value in speedups]
    selected: list[PromptSpeedup] = []
    skipped: list[str] = []
    for candidate in candidates:
        if candidate.policy_result in {"blocked", "skipped"}:
            skipped.append(candidate.speedup_id)
            continue
        if approved_speedup_ids is None:
            if candidate.policy_result != "auto_eligible":
                skipped.append(candidate.speedup_id)
                continue
        elif candidate.speedup_id not in approved_speedup_ids:
            skipped.append(candidate.speedup_id)
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item.source_range.start_us)
    previous_speed_end = -1
    for candidate in selected:
        start = candidate.source_range.start_us
        end = candidate.source_range.end_us
        if start < previous_speed_end:
            raise ValueError("approved speed-up ranges overlap")
        if not any(
            max(start, keep_start) < min(end, keep_end) for keep_start, keep_end in selected_keep
        ):
            raise ValueError("approved speed-up does not intersect a kept edit range")
        previous_speed_end = end

    segments: list[RetimedSegment] = []
    output_cursor = 0
    speed_index = 0
    for keep_start, keep_end in selected_keep:
        cursor = keep_start
        local_speedups = [
            (
                max(keep_start, item.source_range.start_us),
                min(keep_end, item.source_range.end_us),
                item,
            )
            for item in selected
            if max(keep_start, item.source_range.start_us) < min(keep_end, item.source_range.end_us)
        ]
        local_speedups.sort(key=lambda item: (item[0], item[1], item[2].speedup_id))
        for speed_start, speed_end, speedup in local_speedups:
            if speed_start > cursor:
                duration = speed_start - cursor
                segments.append(
                    RetimedSegment(
                        segment_id=f"retime_keep_{len(segments) + 1:03d}",
                        operation="keep",
                        source_range=TimeRange(start_us=cursor, end_us=speed_start),
                        output_range=TimeRange(
                            start_us=output_cursor,
                            end_us=output_cursor + duration,
                        ),
                        playback_rate=1,
                        audio_mode="source",
                        speedup_id=None,
                        command_strategy="passthrough",
                        boundary_confidence=None,
                    )
                )
                output_cursor += duration
            speed_duration = _scaled_duration_us(speed_end - speed_start, speedup.playback_rate)
            segments.append(
                RetimedSegment(
                    segment_id=f"retime_speed_{speed_index + 1:03d}",
                    operation="prompt_speedup",
                    source_range=TimeRange(start_us=speed_start, end_us=speed_end),
                    output_range=TimeRange(
                        start_us=output_cursor,
                        end_us=output_cursor + speed_duration,
                    ),
                    playback_rate=speedup.playback_rate,
                    audio_mode=speedup.audio_mode,
                    speedup_id=speedup.speedup_id,
                    command_strategy="ffmpeg_trim_setpts_atempo",
                    boundary_confidence=speedup.boundary_confidence,
                )
            )
            speed_index += 1
            output_cursor += speed_duration
            cursor = speed_end
        if cursor < keep_end:
            duration = keep_end - cursor
            segments.append(
                RetimedSegment(
                    segment_id=f"retime_keep_{len(segments) + 1:03d}",
                    operation="keep",
                    source_range=TimeRange(start_us=cursor, end_us=keep_end),
                    output_range=TimeRange(
                        start_us=output_cursor,
                        end_us=output_cursor + duration,
                    ),
                    playback_rate=1,
                    audio_mode="source",
                    speedup_id=None,
                    command_strategy="passthrough",
                    boundary_confidence=None,
                )
            )
            output_cursor += duration
    all_warnings = list(
        dict.fromkeys([*warnings, *[f"speedup_fallback_normal_speed:{item}" for item in skipped]])
    )
    payload = {
        "schema_name": "retimed_timeline",
        "schema_version": RETIMED_SCHEMA_VERSION,
        "artifact_id": "art_retimed_timeline",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("timeline-retime", "deterministic-piecewise-map", __version__),
        "inputs": [
            {"artifact_id": edit_decision_list_artifact_id, "sha256": edit_decision_list_sha256},
            {"artifact_id": focus_pacing_plan_artifact_id, "sha256": focus_pacing_plan_sha256},
        ],
        "config_sha256": config_hash,
        "edit_decision_list_artifact_id": edit_decision_list_artifact_id,
        "edit_decision_list_sha256": edit_decision_list_sha256,
        "focus_pacing_plan_artifact_id": focus_pacing_plan_artifact_id,
        "focus_pacing_plan_sha256": focus_pacing_plan_sha256,
        "source_duration_us": source_duration_us,
        "output_duration_us": output_cursor,
        "segments": [item.model_dump(mode="json") for item in segments],
        "warnings": all_warnings,
    }
    validate_retimed_timeline(package_root, payload)
    return payload


def validate_retimed_timeline(package_root: Path, payload: Mapping[str, Any]) -> RetimedTimeline:
    value = dict(payload)
    validate_artifact(package_root, "retimed_timeline", value)
    return RetimedTimeline.model_validate(value)


def write_retimed_timeline(
    package_root: Path,
    layout: ProjectLayout,
    payload: Mapping[str, Any],
) -> Path:
    validated = validate_retimed_timeline(package_root, payload)
    value = validated.model_dump(mode="json")
    digest = canonical_sha256(value)
    output = layout.artifacts / f"retimed-timeline-{digest[:16]}.json"
    write_validated_artifact(package_root, "retimed_timeline", output, value)
    write_validated_artifact(
        package_root,
        "retimed_timeline",
        layout.artifacts / "retimed-timeline.json",
        value,
    )
    return output


def read_retimed_timeline(package_root: Path, path: Path) -> RetimedTimeline:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("retimed timeline must be a JSON object")
    return validate_retimed_timeline(package_root, value)


def map_source_time_us(
    timeline: RetimedTimeline, time_us: int, *, edge: Literal["start", "end"] = "start"
) -> int:
    if time_us < 0 or time_us > timeline.source_duration_us:
        raise ValueError("source time is outside the retimed timeline")
    for segment in timeline.segments:
        if segment.source_range.start_us <= time_us < segment.source_range.end_us:
            offset = time_us - segment.source_range.start_us
            return segment.output_range.start_us + _scaled_duration_us(
                offset, segment.playback_rate
            )
        if time_us == segment.source_range.end_us:
            return segment.output_range.end_us
        if time_us < segment.source_range.start_us:
            return segment.output_range.start_us
    return timeline.output_duration_us


def map_source_range(
    timeline: RetimedTimeline,
    start_us: int,
    end_us: int,
) -> dict[str, int] | None:
    if start_us < 0 or end_us <= start_us or end_us > timeline.source_duration_us:
        raise ValueError("source range is outside the retimed timeline")
    mapped: list[tuple[int, int]] = []
    for segment in timeline.segments:
        overlap_start = max(start_us, segment.source_range.start_us)
        overlap_end = min(end_us, segment.source_range.end_us)
        if overlap_end <= overlap_start:
            continue
        mapped.append(
            (
                segment.output_range.start_us
                + _scaled_duration_us(
                    overlap_start - segment.source_range.start_us,
                    segment.playback_rate,
                ),
                segment.output_range.start_us
                + _scaled_duration_us(
                    overlap_end - segment.source_range.start_us,
                    segment.playback_rate,
                ),
            )
        )
    if not mapped:
        return None
    start = mapped[0][0]
    end = mapped[-1][1]
    if end <= start:
        end = start + 1
    return {"start_us": start, "end_us": end}


def rebase_time_range(
    timeline: RetimedTimeline,
    value: Mapping[str, Any],
    *,
    start_key: str = "start_us",
    end_key: str = "end_us",
) -> dict[str, Any] | None:
    start = int(value[start_key])
    end = int(value[end_key])
    mapped = map_source_range(timeline, start, end)
    if mapped is None:
        return None
    output = dict(value)
    output[start_key] = mapped["start_us"]
    output[end_key] = mapped["end_us"]
    return output


def rebase_items(
    timeline: RetimedTimeline,
    items: Sequence[Mapping[str, Any]],
    *,
    start_key: str = "start_us",
    end_key: str = "end_us",
) -> list[dict[str, Any]]:
    rebased: list[dict[str, Any]] = []
    for item in items:
        mapped = rebase_time_range(timeline, item, start_key=start_key, end_key=end_key)
        if mapped is not None:
            rebased.append(mapped)
    return rebased


def rebase_focus_pacing_plan(
    timeline: RetimedTimeline,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    value = cast(dict[str, Any], json.loads(json.dumps(plan)))
    for key in ("zooms", "speedups"):
        retained: list[dict[str, Any]] = []
        for item in value.get(key, []):
            if not isinstance(item, dict):
                continue
            source_range = item.get("source_range")
            if not isinstance(source_range, Mapping):
                continue
            mapped = rebase_time_range(timeline, source_range)
            if mapped is None:
                continue
            item["source_range"] = mapped
            if key == "zooms":
                visible = item.get("target_visible_range")
                if isinstance(visible, Mapping):
                    visible_mapped = rebase_time_range(timeline, visible)
                    if visible_mapped is None:
                        continue
                    item["target_visible_range"] = visible_mapped
                track = item.get("target_track")
                if isinstance(track, list):
                    item["target_track"] = [
                        {
                            **sample,
                            "time_us": map_source_time_us(timeline, int(sample["time_us"])),
                        }
                        for sample in track
                        if isinstance(sample, Mapping)
                    ]
                for field in ("zoom_in_end_us", "zoom_out_start_us"):
                    item[field] = map_source_time_us(timeline, int(item[field]))
            retained.append(item)
        value[key] = retained
    return value


def rebase_transcript(
    timeline: RetimedTimeline,
    transcript: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebase canonical word/segment timing through the piecewise retime map."""

    value = cast(dict[str, Any], json.loads(json.dumps(transcript)))
    words_value = value.get("words")
    segments_value = value.get("segments")
    if not isinstance(words_value, list) or not isinstance(segments_value, list):
        raise ValueError("transcript must contain canonical words and segments arrays")
    warnings = [str(item) for item in value.get("warnings", [])]
    mapped_words: list[dict[str, Any]] = []
    words_by_segment: dict[str, list[dict[str, Any]]] = {}
    for raw_word in words_value:
        if not isinstance(raw_word, Mapping):
            raise ValueError("transcript word must be an object")
        word = dict(raw_word)
        source_start = int(word["start_us"])
        source_end = int(word["end_us"])
        mapped = map_source_range(timeline, source_start, source_end)
        if mapped is None:
            warnings.append(f"dropped_word_outside_retimed_ranges:{word.get('word_id')}")
            continue
        word["source_start_us"] = source_start
        word["source_end_us"] = source_end
        word["start_us"] = mapped["start_us"]
        word["end_us"] = mapped["end_us"]
        if source_start != mapped["start_us"] or source_end != mapped["end_us"]:
            word["timing_status"] = "adjusted"
        mapped_words.append(word)
        words_by_segment.setdefault(str(word.get("segment_id")), []).append(word)

    mapped_segments: list[dict[str, Any]] = []
    for raw_segment in segments_value:
        if not isinstance(raw_segment, Mapping):
            raise ValueError("transcript segment must be an object")
        segment = dict(raw_segment)
        segment_id = str(segment["segment_id"])
        segment_words = sorted(
            words_by_segment.get(segment_id, []),
            key=lambda item: (int(item["start_us"]), str(item["word_id"])),
        )
        if segment_words:
            segment["start_us"] = min(int(item["start_us"]) for item in segment_words)
            segment["end_us"] = max(int(item["end_us"]) for item in segment_words)
            segment["word_ids"] = [str(item["word_id"]) for item in segment_words]
            segment["text"] = " ".join(str(item["text"]) for item in segment_words)
        else:
            mapped = map_source_range(
                timeline,
                int(segment["start_us"]),
                int(segment["end_us"]),
            )
            if mapped is None:
                warnings.append(f"dropped_segment_outside_retimed_ranges:{segment_id}")
                continue
            segment["start_us"] = mapped["start_us"]
            segment["end_us"] = mapped["end_us"]
            segment["word_ids"] = []
        mapped_segments.append(segment)

    mapped_segments.sort(key=lambda item: (int(item["start_us"]), str(item["segment_id"])))
    mapped_words.sort(key=lambda item: (int(item["start_us"]), str(item["word_id"])))
    value["output_duration_us"] = timeline.output_duration_us
    value["source_to_output_mapping"] = [
        {
            "source_start_us": segment.source_range.start_us,
            "source_end_us": segment.source_range.end_us,
            "output_start_us": segment.output_range.start_us,
            "output_end_us": segment.output_range.end_us,
        }
        for segment in timeline.segments
    ]
    value["segments"] = mapped_segments
    value["words"] = mapped_words
    value["text"] = " ".join(str(item["text"]) for item in mapped_segments).strip()
    value["warnings"] = list(dict.fromkeys(warnings))
    confidence = value.get("confidence_summary")
    if isinstance(confidence, dict):
        probabilities = [
            float(item["probability"])
            for item in mapped_words
            if item.get("probability") is not None
        ]
        confidence["word_count"] = len(mapped_words)
        confidence["mean_word_probability"] = (
            sum(probabilities) / len(probabilities) if probabilities else None
        )
        confidence["minimum_word_probability"] = min(probabilities) if probabilities else None
        confidence["low_confidence_word_ids"] = [
            str(item["word_id"])
            for item in mapped_words
            if item.get("probability") is not None and float(item["probability"]) < 0.6
        ]
        confidence["uncertain_word_count"] = sum(
            item.get("timing_status") == "uncertain" for item in mapped_words
        )
    value["status"] = "warning" if value["warnings"] else "complete"
    return value


def rebase_timecoded_items(
    timeline: RetimedTimeline,
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rebase effects, captions, B-roll, sound, and review ranges uniformly."""

    return rebase_items(timeline, items)


def render_retimed_timeline(
    package_root: Path,
    layout: ProjectLayout,
    source: Path,
    timeline_path: Path,
    *,
    output: Path | None = None,
    adapter: FFmpegAdapter | None = None,
    video_codec: str | None = None,
    audio_codec: str = "aac",
    qp: int | None = None,
    preset: str = "medium",
    audio_edge_fade_us: int = 0,
    strict_decode: bool = False,
) -> Path:
    """Render a validated retime map through a staging path and write a manifest."""

    source_path = source.expanduser().resolve()
    selected_timeline_path = timeline_path.expanduser().resolve()
    for candidate, label in (
        (source_path, "source"),
        (selected_timeline_path, "retimed timeline"),
    ):
        try:
            candidate.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise RenderOutputError(f"{label} must be inside the project") from exc
    if not source_path.is_file() or not selected_timeline_path.is_file():
        raise RenderOutputError("retimed render input is missing")
    timeline = read_retimed_timeline(package_root, selected_timeline_path)
    if timeline.project_id != layout.root.name:
        raise RenderOutputError("retimed timeline project does not match the project")
    adapter = adapter or FFmpegAdapter()
    source_probe = adapter.probe(source_path)
    source_video = next(
        (
            item
            for item in source_probe.get("streams", [])
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(source_video, dict):
        raise RenderOutputError("retimed source has no video stream")
    source_frame_rate_record = parse_rate(source_video.get("r_frame_rate")) or parse_rate(
        source_video.get("avg_frame_rate")
    )
    if source_frame_rate_record is None:
        raise RenderOutputError("retimed source has no rational frame rate")
    source_frame_rate = Fraction(
        source_frame_rate_record["numerator"], source_frame_rate_record["denominator"]
    )
    encoder_identity = dict(adapter_encoder_identity(adapter))
    encoder_identity["video_codec"] = video_codec or str(encoder_identity["video_codec"])
    encoder_identity["audio_codec"] = audio_codec
    encoder_identity["qp"] = qp
    encoder_identity["preset"] = preset
    encoder_identity["audio_edge_fade_us"] = audio_edge_fade_us
    encoder_identity["duration_us"] = timeline.output_duration_us
    encoder_identity["frame_rate"] = source_frame_rate_record
    encoder_label = str(encoder_identity["video_codec"])
    selected_output = (
        output.expanduser().resolve()
        if output is not None
        else layout.output / f"retimed-{timeline.revision_id}-{encoder_label}.mp4"
    )
    try:
        selected_output.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise RenderOutputError("retimed output must be inside the project") from exc
    if selected_output == source_path:
        raise RenderOutputError("retimed output cannot overwrite immutable source media")
    selected_output.parent.mkdir(parents=True, exist_ok=True)
    timeline_hash = sha256_file(selected_timeline_path)
    stage_key = make_stage_key(
        "retimed-render",
        "p11-02b",
        [sha256_file(source_path), timeline_hash],
        {
            "revision_id": timeline.revision_id,
            "config_sha256": timeline.config_sha256,
            "encoder": encoder_identity,
        },
    )
    stage_dir = layout.staging / f"retimed-render-{stage_key[:16]}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged_output = stage_dir / "render.mp4"
    with ProjectLock(layout, stage="retimed_render", revision_id=timeline.revision_id):
        result = adapter.render_retimed_segments(
            source_path,
            [item.model_dump(mode="json") for item in timeline.segments],
            staged_output,
            video_codec=video_codec,
            audio_codec=audio_codec,
            qp=qp,
            preset=preset,
            audio_edge_fade_us=audio_edge_fade_us,
            duration_us=timeline.output_duration_us,
            frame_rate=source_frame_rate,
        )
        probe = adapter.probe(staged_output)
        streams = probe.get("streams", [])
        video = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        audio = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "audio"
            ),
            None,
        )
        if not isinstance(video, dict) or not isinstance(audio, dict):
            raise RenderOutputError("retimed render must contain video and production audio")
        video_duration = seconds_to_us(video.get("duration"))
        audio_duration = seconds_to_us(audio.get("duration"))
        if video_duration is None:
            video_duration = seconds_to_us(probe.get("format", {}).get("duration"))
        if audio_duration is None:
            audio_duration = seconds_to_us(probe.get("format", {}).get("duration"))
        if video_duration is None or audio_duration is None:
            raise RenderOutputError("retimed render durations are missing")
        duration_drift = max(
            abs(video_duration - timeline.output_duration_us),
            abs(audio_duration - timeline.output_duration_us),
        )
        av_drift = abs(video_duration - audio_duration)
        decode = adapter.full_decode_check(staged_output, strict=strict_decode)
        if decode.exit_code != 0:
            raise RenderOutputError("retimed render failed full decode validation")
        if duration_drift > 100_000 or av_drift > 100_000:
            raise RenderOutputError("retimed render duration or A/V sync exceeds 100 ms")
        frame_rate = parse_rate(video.get("avg_frame_rate")) or parse_rate(
            video.get("r_frame_rate")
        )
        if frame_rate is None:
            raise RenderOutputError("retimed render has no rational frame rate")
        if frame_rate != source_frame_rate_record:
            raise RenderOutputError("retimed render changed the source frame rate")
        os.replace(staged_output, selected_output)
        payload = {
            "schema_name": "retimed_render_manifest",
            "schema_version": "1.0.0",
            "artifact_id": "art_retimed_render",
            "project_id": layout.root.name,
            "revision_id": timeline.revision_id,
            "created_at": now_iso(),
            "producer": producer("retimed-render", "ffmpeg", adapter.version()),
            "inputs": [
                artifact_input("art_source", source_path),
                artifact_input("art_retimed_timeline", selected_timeline_path),
            ],
            "config_sha256": timeline.config_sha256,
            "expected_duration_us": timeline.output_duration_us,
            "output": {
                "path": str(selected_output),
                "sha256": sha256_file(selected_output),
                "size_bytes": selected_output.stat().st_size,
            },
            "video": {
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "frame_rate": frame_rate,
                "duration_us": video_duration,
            },
            "audio": {
                "duration_us": audio_duration,
                "sample_rate_hz": int(audio.get("sample_rate") or 0),
                "channels": int(audio.get("channels") or 0),
            },
            "validation": {"full_decode": "pass", "duration": "pass", "av_sync": "pass"},
            "command": {
                "executable": adapter.ffmpeg_path,
                "arguments": list(result.arguments),
            },
        }
        validate_artifact(package_root, "retimed_render_manifest", payload)
        manifest_hash = canonical_sha256(payload)
        manifest_path = layout.artifacts / f"retimed-render-manifest-{manifest_hash[:16]}.json"
        write_validated_artifact(package_root, "retimed_render_manifest", manifest_path, payload)
        write_validated_artifact(
            package_root,
            "retimed_render_manifest",
            layout.artifacts / "retimed-render-manifest.json",
            payload,
        )
    return manifest_path


__all__ = [
    "RetimedSegment",
    "RetimedTimeline",
    "compile_retimed_timeline",
    "map_source_range",
    "map_source_time_us",
    "read_retimed_timeline",
    "rebase_focus_pacing_plan",
    "rebase_items",
    "rebase_time_range",
    "rebase_timecoded_items",
    "rebase_transcript",
    "render_retimed_timeline",
    "validate_retimed_timeline",
    "write_retimed_timeline",
]
