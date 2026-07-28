from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.ffmpeg import FFmpegAdapter, adapter_encoder_identity
from videoedit.adapters.process import ProcessResult
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.errors import (
    ApprovalRequiredError,
    LoudnessMeasurementError,
    RenderOutputError,
    VideoeditError,
)
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
from videoedit.services.media import parse_rate, seconds_to_us
from videoedit.services.planning import validate_gate1_approval
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)
from videoedit.services.transcription import (
    source_from_manifest,
    validate_transcript_timing,
)

INTEGRATED_TARGET_LUFS = -16.0
TRUE_PEAK_TARGET_DBFS = -1.5
LOUDNESS_RANGE_TARGET_LU = 11.0
INTEGRATED_TOLERANCE_LU = 1.0
TRUE_PEAK_TOLERANCE_DB = 0.5
AV_SYNC_TOLERANCE_US = 100_000


def _first_stream(probe: Mapping[str, Any], stream_type: str) -> dict[str, Any]:
    streams = probe.get("streams", [])
    if isinstance(streams, list):
        for stream_value in streams:
            if isinstance(stream_value, dict) and stream_value.get("codec_type") == stream_type:
                return dict(stream_value)
    raise RenderOutputError(f"media has no {stream_type} stream")


def _adapter_version(adapter: FFmpegAdapter, executable: str | None = None) -> str:
    version_fn = getattr(adapter, "version", None)
    if not callable(version_fn):
        return "unknown"
    try:
        value = version_fn(executable) if executable is not None else version_fn()
    except TypeError:
        value = version_fn()
    return str(value) or "unknown"


def _command_record(
    result: ProcessResult,
    working_directory: Path,
    version: str,
) -> dict[str, Any]:
    return {
        "executable": result.arguments[0],
        "arguments": list(result.arguments[1:]),
        "working_directory": str(working_directory.resolve()),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "version": version,
    }


def _process_text(result: ProcessResult) -> str:
    return "\n".join(value for value in (result.stderr, result.stdout) if value)


def parse_loudness_measurement(result: ProcessResult | str) -> dict[str, float]:
    """Parse the stable summary emitted by FFmpeg's ebur128 scanner."""

    text = _process_text(result) if isinstance(result, ProcessResult) else str(result)
    integrated_match = re.search(
        r"Integrated\s+loudness:.*?\n\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    threshold_match = re.search(
        r"Integrated\s+loudness:.*?\n\s*Threshold:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    lra_match = re.search(
        r"LRA:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LU",
        text,
        flags=re.IGNORECASE,
    )
    peak_match = re.search(
        r"True\s+peak:.*?\n\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dBFS",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    matches = {
        "integrated_lufs": integrated_match,
        "threshold_lufs": threshold_match,
        "loudness_range_lu": lra_match,
        "true_peak_dbfs": peak_match,
    }
    if any(match is None for match in matches.values()):
        missing = ", ".join(name for name, match in matches.items() if match is None)
        raise LoudnessMeasurementError(f"FFmpeg loudness summary is missing: {missing}")
    try:
        measurement = {name: float(match.group(1)) for name, match in matches.items() if match}
    except (TypeError, ValueError) as exc:
        raise LoudnessMeasurementError("FFmpeg loudness summary contains invalid numbers") from exc
    if not all(math.isfinite(value) for value in measurement.values()):
        raise LoudnessMeasurementError("FFmpeg loudness summary contains non-finite numbers")
    if measurement["loudness_range_lu"] < 0:
        raise LoudnessMeasurementError("FFmpeg loudness range is negative")
    return measurement


def parse_clipped_samples(result: ProcessResult | str) -> int:
    """Read clipping diagnostics, with a conservative peak fallback."""

    text = _process_text(result) if isinstance(result, ProcessResult) else str(result)
    matches = re.findall(r"Number\s+of\s+clipped\s+samples:\s*(\d+)", text, re.IGNORECASE)
    if matches:
        return max(int(value) for value in matches)
    peak_values = re.findall(r"Peak\s+level\s+dB:\s*(-?(?:\d+(?:\.\d+)?|inf))", text)
    if any(float(value) >= -0.1 for value in peak_values):
        return 1
    return 0


def _validate_mapping(
    mapping: Sequence[Mapping[str, Any]],
    source_duration_us: int,
    output_duration_us: int,
) -> list[dict[str, int]]:
    normalized: list[dict[str, int]] = []
    previous_source_end = 0
    previous_output_end = 0
    for index, item in enumerate(mapping, start=1):
        try:
            source_start = int(item["source_start_us"])
            source_end = int(item["source_end_us"])
            output_start = int(item["output_start_us"])
            output_end = int(item["output_end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderOutputError(f"EDL mapping item {index} is invalid") from exc
        if (
            source_start < 0
            or source_end <= source_start
            or source_end > source_duration_us
            or output_start < 0
            or output_end <= output_start
            or output_end > output_duration_us
            or source_start < previous_source_end
            or output_start != previous_output_end
            or output_end - output_start != source_end - source_start
        ):
            raise RenderOutputError(f"EDL mapping item {index} is unordered or out of bounds")
        normalized.append(
            {
                "source_start_us": source_start,
                "source_end_us": source_end,
                "output_start_us": output_start,
                "output_end_us": output_end,
            }
        )
        previous_source_end = source_end
        previous_output_end = output_end
    if not normalized or normalized[-1]["output_end_us"] != output_duration_us:
        raise RenderOutputError("EDL mapping does not cover the expected output duration")
    return normalized


def _load_edl(
    package_root: Path,
    layout: ProjectLayout,
    source_duration_us: int,
) -> tuple[Path, dict[str, Any], list[dict[str, int]]]:
    edl_path = layout.artifacts / "edit-decision-list.json"
    if not edl_path.is_file():
        raise RenderOutputError("edit-decision-list.json is missing; compile approved edits first")
    try:
        value = json.loads(edl_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderOutputError("edit-decision-list.json is unreadable") from exc
    if not isinstance(value, dict):
        raise RenderOutputError("edit-decision-list.json must contain an object")
    validate_artifact(package_root, "edit_decision_list", value)
    edl = dict(value)
    if int(edl["source_duration_us"]) != source_duration_us:
        raise RenderOutputError("EDL source duration does not match the immutable source")
    expected_duration_us = int(edl["expected_output_duration_us"])
    keep_values = edl.get("keep_ranges")
    if not isinstance(keep_values, list) or not keep_values:
        raise RenderOutputError("EDL must contain at least one keep range")
    keep_mapping: list[dict[str, int]] = []
    for item in keep_values:
        if not isinstance(item, dict):
            raise RenderOutputError("EDL keep range must be an object")
        keep_mapping.append(
            {
                "source_start_us": int(item["source_start_us"]),
                "source_end_us": int(item["source_end_us"]),
                "output_start_us": int(item["output_start_us"]),
                "output_end_us": int(item["output_end_us"]),
            }
        )
    declared_mapping = edl.get("source_to_output_mapping")
    mapping = keep_mapping
    if isinstance(declared_mapping, list):
        mapping = [
            {
                "source_start_us": int(item["source_start_us"]),
                "source_end_us": int(item["source_end_us"]),
                "output_start_us": int(item["output_start_us"]),
                "output_end_us": int(item["output_end_us"]),
            }
            for item in declared_mapping
            if isinstance(item, dict)
        ]
    normalized = _validate_mapping(mapping, source_duration_us, expected_duration_us)
    if normalized != keep_mapping:
        raise RenderOutputError("EDL keep ranges and source-to-output mapping disagree")
    return edl_path, edl, normalized


def _path_for_hash(layout: ProjectLayout, expected_hash: str) -> Path | None:
    candidates = sorted(layout.artifacts.glob("*.json")) + sorted(layout.review.glob("*.json"))
    for candidate in candidates:
        if candidate.is_file() and sha256_file(candidate) == expected_hash:
            return candidate
    return None


def _validate_current_gate1(
    package_root: Path,
    layout: ProjectLayout,
    edl: Mapping[str, Any],
    *,
    revision_id: str,
) -> Path:
    approval_ids = {str(value) for value in edl.get("approval_record_ids", [])}
    edl_approval_hash = next(
        (
            str(item["sha256"])
            for item in edl.get("inputs", [])
            if isinstance(item, dict) and item.get("artifact_id") == "art_gate1_approval"
        ),
        None,
    )
    for approval_path in sorted(layout.review.glob("gate1-approval-*.json")):
        try:
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(approval, dict) or str(approval.get("approval_id")) not in approval_ids:
            continue
        if edl_approval_hash is not None and sha256_file(approval_path) != edl_approval_hash:
            continue
        inputs = approval.get("inputs", [])
        if not isinstance(inputs, list):
            continue
        decision_hash = next(
            (
                str(item["sha256"])
                for item in inputs
                if isinstance(item, dict) and item.get("artifact_id") == "art_edit_decisions"
            ),
            None,
        )
        effect_hash = next(
            (
                str(item["sha256"])
                for item in inputs
                if isinstance(item, dict) and item.get("artifact_id") == "art_effect_plan"
            ),
            None,
        )
        focus_hash = next(
            (
                str(item["sha256"])
                for item in inputs
                if isinstance(item, dict) and item.get("artifact_id") == "art_focus_pacing"
            ),
            None,
        )
        decisions_path = _path_for_hash(layout, decision_hash) if decision_hash else None
        effect_plan_path = _path_for_hash(layout, effect_hash) if effect_hash else None
        focus_plan_path = _path_for_hash(layout, focus_hash) if focus_hash else None
        if (
            decisions_path is None
            or effect_plan_path is None
            or (focus_hash and focus_plan_path is None)
        ):
            continue
        validate_gate1_approval(
            package_root,
            layout,
            approval_path,
            decisions_path,
            effect_plan_path,
            revision_id=revision_id,
            focus_pacing_plan_path=focus_plan_path,
        )
        return approval_path
    raise ApprovalRequiredError("current Gate 1 approval is missing or stale for the approved EDL")


def _map_interval(
    start_us: int,
    end_us: int,
    mapping: Sequence[Mapping[str, int]],
) -> tuple[int, int, int, bool] | None:
    candidates: list[tuple[int, int, int, int]] = []
    for index, item in enumerate(mapping):
        overlap_start = max(start_us, int(item["source_start_us"]))
        overlap_end = min(end_us, int(item["source_end_us"]))
        if overlap_end > overlap_start:
            candidates.append((overlap_end - overlap_start, index, overlap_start, overlap_end))
    if not candidates:
        return None
    _overlap, index, overlap_start, overlap_end = max(
        candidates, key=lambda value: (value[0], -value[1])
    )
    item = mapping[index]
    output_start = int(item["output_start_us"]) + overlap_start - int(item["source_start_us"])
    output_end = int(item["output_start_us"]) + overlap_end - int(item["source_start_us"])
    clipped = overlap_start != start_us or overlap_end != end_us
    return output_start, output_end, index, clipped


def rebase_transcript(
    transcript: Mapping[str, Any],
    transcript_sha256: str,
    edl_path: Path,
    mapping: Sequence[Mapping[str, int]],
    output_duration_us: int,
    *,
    project_id: str,
    revision_id: str,
    source_transcript_path: Path,
) -> dict[str, Any]:
    """Map source word/segment timing onto the approved output timeline."""

    words_value = transcript.get("words")
    segments_value = transcript.get("segments")
    if not isinstance(words_value, list) or not isinstance(segments_value, list):
        raise RenderOutputError("source transcript has invalid segments or words")
    source_duration_us = int(transcript["source_duration_us"])
    mapped_words: list[tuple[dict[str, Any], int, int, int, bool]] = []
    warnings = [str(value) for value in transcript.get("warnings", [])]
    for word_value in words_value:
        if not isinstance(word_value, dict):
            raise RenderOutputError("source transcript word is not an object")
        word = dict(word_value)
        result = _map_interval(int(word["start_us"]), int(word["end_us"]), mapping)
        if result is None:
            warnings.append(f"dropped_word_outside_keep_ranges:{word.get('word_id')}")
            continue
        output_start, output_end, mapping_index, clipped = result
        word["source_start_us"] = int(word["start_us"])
        word["source_end_us"] = int(word["end_us"])
        word["start_us"] = output_start
        word["end_us"] = output_end
        if clipped:
            word["timing_status"] = "adjusted"
            warnings.append(f"clipped_word_to_keep_range:{word.get('word_id')}")
        mapped_words.append((word, mapping_index, output_start, output_end, clipped))

    output_segments: list[dict[str, Any]] = []
    output_words: list[dict[str, Any]] = []
    for segment_value in segments_value:
        if not isinstance(segment_value, dict):
            raise RenderOutputError("source transcript segment is not an object")
        segment = dict(segment_value)
        segment_id = str(segment["segment_id"])
        segment_words = [
            item for item in mapped_words if str(item[0].get("segment_id")) == segment_id
        ]
        grouped: dict[int, list[tuple[dict[str, Any], int, int, int, bool]]] = {}
        for item in segment_words:
            grouped.setdefault(item[1], []).append(item)
        if not grouped:
            segment_map = _map_interval(int(segment["start_us"]), int(segment["end_us"]), mapping)
            if segment_map is None:
                warnings.append(f"dropped_segment_outside_keep_ranges:{segment_id}")
                continue
            grouped[segment_map[2]] = []
        for part_number, mapping_index in enumerate(sorted(grouped), start=1):
            items = sorted(
                grouped[mapping_index],
                key=lambda value: (value[2], value[0]["word_id"]),
            )
            part_id = segment_id if part_number == 1 else f"{segment_id}_p{part_number:02d}"
            map_item = mapping[mapping_index]
            if items:
                part_start = min(item[2] for item in items)
                part_end = max(item[3] for item in items)
            else:
                segment_map = _map_interval(
                    int(segment["start_us"]), int(segment["end_us"]), [map_item]
                )
                if segment_map is None:
                    continue
                part_start, part_end = segment_map[0], segment_map[1]
            part_words: list[str] = []
            word_ids: list[str] = []
            for word, _index, _start, _end, _clipped in items:
                word["segment_id"] = part_id
                output_words.append(word)
                word_ids.append(str(word["word_id"]))
                part_words.append(str(word["text"]))
            output_segments.append(
                {
                    "segment_id": part_id,
                    "text": " ".join(part_words) or str(segment.get("text", "")),
                    "start_us": part_start,
                    "end_us": part_end,
                    "word_ids": word_ids,
                    "average_log_probability": segment.get("average_log_probability"),
                    "no_speech_probability": segment.get("no_speech_probability"),
                }
            )
    output_segments.sort(key=lambda value: (int(value["start_us"]), str(value["segment_id"])))
    output_words.sort(key=lambda value: (int(value["start_us"]), str(value["word_id"])))
    payload: dict[str, Any] = {
        "schema_name": "transcript",
        "schema_version": "1.0.0",
        "artifact_id": "art_transcript_output",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("transcription-rebase", "approved-edl-mapper", __version__),
        "inputs": [
            artifact_input("art_transcript", source_transcript_path),
            artifact_input("art_edl", edl_path),
        ],
        "config_sha256": "0" * 64,
        "source_duration_us": source_duration_us,
        "output_duration_us": output_duration_us,
        "rebased_from_sha256": transcript_sha256,
        "source_to_output_mapping": [dict(item) for item in mapping],
        "language": str(transcript.get("language", "und")),
        "model": str(transcript.get("model", "unknown")),
        "model_identifier": str(
            transcript.get("model_identifier", transcript.get("model", "unknown"))
        ),
        "device": str(transcript.get("device", "unknown")),
        "text": " ".join(str(item["text"]) for item in output_segments).strip(),
        "segments": output_segments,
        "words": output_words,
        "warnings": list(dict.fromkeys(warnings)),
        "confidence_summary": {
            "word_count": len(output_words),
            "mean_word_probability": (
                sum(
                    float(word["probability"])
                    for word in output_words
                    if word["probability"] is not None
                )
                / len([word for word in output_words if word["probability"] is not None])
                if any(word["probability"] is not None for word in output_words)
                else None
            ),
            "minimum_word_probability": (
                min(
                    float(word["probability"])
                    for word in output_words
                    if word["probability"] is not None
                )
                if any(word["probability"] is not None for word in output_words)
                else None
            ),
            "low_confidence_word_ids": [
                str(word["word_id"])
                for word in output_words
                if word.get("probability") is not None and float(word["probability"]) < 0.6
            ],
            "uncertain_word_count": sum(
                word.get("timing_status") == "uncertain" for word in output_words
            ),
            "speaker_count": int(transcript.get("confidence_summary", {}).get("speaker_count", 0)),
        },
        "status": "warning" if warnings else "complete",
    }
    validate_transcript_timing(payload)
    return payload


def _expected_frame_count(
    mapping: Sequence[Mapping[str, int]],
    frame_rate: Mapping[str, int],
) -> int:
    numerator = int(frame_rate["numerator"])
    denominator = int(frame_rate["denominator"])
    return sum(
        microseconds_to_frame(int(item["source_end_us"]), numerator, denominator)
        - microseconds_to_frame(int(item["source_start_us"]), numerator, denominator)
        for item in mapping
    )


def _stream_duration_us(stream: Mapping[str, Any], fallback_us: int | None) -> int | None:
    duration = seconds_to_us(stream.get("duration"))
    return duration if duration is not None else fallback_us


def _safe_output_path(layout: ProjectLayout, source: Path, output: Path) -> Path:
    resolved = output.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise RenderOutputError("render output must be inside the project") from exc
    if resolved == source.resolve():
        raise RenderOutputError("render output must not overwrite the immutable source")
    try:
        resolved.relative_to(layout.raw.resolve())
    except ValueError:
        return resolved
    raise RenderOutputError("render output must not be written under the raw source directory")


def _promote_media(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != sha256_file(staged):
            raise RenderOutputError(
                f"immutable render target already exists with another hash: {target}"
            )
        staged.unlink(missing_ok=True)
        return
    os.replace(staged, target)


def _stage_file_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
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


def _cached_render(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, Any] | None,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    if not all(name in artifacts for name in ("render_manifest", "output_media", "transcript")):
        return None
    if not all(_stage_file_ref_valid(layout, artifacts[name]) for name in artifacts):
        return None
    manifest_path = Path(str(artifacts["render_manifest"]["path"])).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        validate_artifact(package_root, "render_manifest", payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return manifest_path


def _mark_project_rendered(package_root: Path, layout: ProjectLayout, artifact_id: str) -> None:
    manifest_path = layout.state / "project-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderOutputError("project manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise RenderOutputError("project manifest must contain an object")
    value["updated_at"] = now_iso()
    value["state"] = "rough_cut_ready"
    active_artifacts = value.setdefault("active_artifacts", {})
    if not isinstance(active_artifacts, dict):
        raise RenderOutputError("project manifest active_artifacts must be an object")
    active_artifacts["render"] = artifact_id
    active_artifacts["transcript_output"] = "art_transcript_output"
    write_validated_artifact(package_root, "project_manifest", manifest_path, value)


def render_base_timeline(
    package_root: Path,
    layout: ProjectLayout,
    output: Path | None = None,
    revision_id: str = "rev_001",
    render_type: str = "rough",
    adapter: FFmpegAdapter | None = None,
) -> Path:
    if render_type not in {"rough", "final"}:
        raise RenderOutputError(f"unsupported base render type: {render_type}")
    source, source_manifest_path, source_manifest = source_from_manifest(layout)
    if source_manifest.get("sha256") != sha256_file(source):
        raise RenderOutputError("immutable source hash does not match source manifest")
    source_duration_us = int(source_manifest["media_duration_us"])
    edl_path, edl, mapping = _load_edl(package_root, layout, source_duration_us)
    transcript_path = layout.artifacts / "transcript.json"
    if not transcript_path.is_file():
        raise RenderOutputError("transcript.json is missing; run transcription before base render")
    try:
        transcript_value = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderOutputError("transcript.json is unreadable") from exc
    if not isinstance(transcript_value, dict):
        raise RenderOutputError("transcript.json must contain an object")
    validate_artifact(package_root, "transcript", transcript_value)
    validate_transcript_timing(transcript_value)
    if int(transcript_value["source_duration_us"]) != source_duration_us:
        raise RenderOutputError("transcript duration does not match the immutable source")
    adapter = adapter or FFmpegAdapter()
    encoder_identity = adapter_encoder_identity(adapter)
    ffmpeg_version = _adapter_version(adapter)
    source_probe = adapter.probe(source)
    source_video = _first_stream(source_probe, "video")
    if not any(
        isinstance(item, dict) and item.get("codec_type") == "audio"
        for item in source_probe.get("streams", [])
        if isinstance(source_probe.get("streams", []), list)
    ):
        raise RenderOutputError("base render requires a production audio stream")
    frame_rate = parse_rate(source_video.get("avg_frame_rate")) or parse_rate(
        source_video.get("r_frame_rate")
    )
    if frame_rate is None:
        raise RenderOutputError("source video has no usable rational frame rate")
    expected_frame_count = _expected_frame_count(mapping, frame_rate)
    has_cuts = (
        len(mapping) != 1
        or mapping[0]["source_start_us"] != 0
        or mapping[0]["source_end_us"] != source_duration_us
    )
    approval_path: Path | None = None
    if has_cuts or edl.get("deletions"):
        approval_path = _validate_current_gate1(package_root, layout, edl, revision_id=revision_id)
    input_hashes = [
        str(source_manifest["sha256"]),
        sha256_file(source_manifest_path),
        sha256_file(edl_path),
        sha256_file(transcript_path),
    ]
    if approval_path is not None:
        input_hashes.append(sha256_file(approval_path))
    stage_key = make_stage_key(
        "base_render",
        __version__,
        input_hashes,
        {
            "revision_id": revision_id,
            "render_type": render_type,
            "frame_rate": frame_rate,
            "expected_duration_us": int(edl["expected_output_duration_us"]),
            "loudness_profile": {
                "integrated_target_lufs": INTEGRATED_TARGET_LUFS,
                "true_peak_target_dbfs": TRUE_PEAK_TARGET_DBFS,
                "loudness_range_target_lu": LOUDNESS_RANGE_TARGET_LU,
                "integrated_tolerance_lu": INTEGRATED_TOLERANCE_LU,
                "true_peak_tolerance_db": TRUE_PEAK_TOLERANCE_DB,
            },
            "requested_output": str(output.expanduser().resolve()) if output else None,
            "ffmpeg_version": ffmpeg_version,
            "encoder": encoder_identity,
        },
    )
    with ProjectLock(layout, stage="base_render", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "base_render", revision_id)
        cached = _cached_render(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"base-render-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="base_render",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        staged_cut = stage_dir / "base-cut.part.mp4"
        staged_normalized = stage_dir / "base-edit.part.mp4"
        staged_bounded = stage_dir / "base-edit-bounded.part.mp4"
        final_output = (
            _safe_output_path(layout, source, output)
            if output is not None
            else layout.work
            / "base-renders"
            / revision_id
            / stage_key
            / f"{render_type}-base-edit.mp4"
        )
        manifest_path = layout.artifacts / f"render-{render_type}-{stage_key[:16]}.json"
        transcript_output_path = layout.artifacts / f"transcript-output-{stage_key[:16]}.json"
        commands: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            cut_result = adapter.render_keep_ranges(
                source,
                [(int(item["source_start_us"]), int(item["source_end_us"])) for item in mapping],
                staged_cut,
            )
            commands.append(_command_record(cut_result, staged_cut.parent, ffmpeg_version))
            input_loudness_result = adapter.measure_loudness(staged_cut)
            input_loudness = parse_loudness_measurement(input_loudness_result)
            commands.append(
                _command_record(input_loudness_result, staged_cut.parent, ffmpeg_version)
            )
            normalize_result = adapter.normalize_loudness(
                staged_cut,
                staged_normalized,
                input_loudness,
                integrated_target_lufs=INTEGRATED_TARGET_LUFS,
                true_peak_target_dbfs=TRUE_PEAK_TARGET_DBFS,
                loudness_range_target_lu=LOUDNESS_RANGE_TARGET_LU,
            )
            commands.append(
                _command_record(normalize_result, staged_normalized.parent, ffmpeg_version)
            )
            normalized_probe = adapter.probe(staged_normalized)
            normalized_video = _first_stream(normalized_probe, "video")
            normalized_format_duration_us = seconds_to_us(
                normalized_probe.get("format", {}).get("duration")
            )
            visual_duration_us = _stream_duration_us(
                normalized_video, normalized_format_duration_us
            )
            if visual_duration_us is None or visual_duration_us <= 0:
                raise RenderOutputError("normalized base edit has no visual duration")
            bound_audio = getattr(adapter, "bound_audio_to_visual_duration", None)
            if not callable(bound_audio):
                raise RenderOutputError(
                    "configured media adapter cannot bind production audio to the visual duration"
                )
            bound_result = bound_audio(
                staged_normalized,
                staged_bounded,
                duration_us=visual_duration_us,
            )
            commands.append(_command_record(bound_result, staged_bounded.parent, ffmpeg_version))
            output_media = staged_bounded
            output_loudness_result = adapter.measure_loudness(output_media)
            output_loudness = parse_loudness_measurement(output_loudness_result)
            commands.append(
                _command_record(output_loudness_result, output_media.parent, ffmpeg_version)
            )
            clipping_result = adapter.measure_clipping(output_media)
            clipped_samples = parse_clipped_samples(clipping_result)
            commands.append(_command_record(clipping_result, output_media.parent, ffmpeg_version))
            if (
                abs(output_loudness["integrated_lufs"] - INTEGRATED_TARGET_LUFS)
                > INTEGRATED_TOLERANCE_LU
                or output_loudness["true_peak_dbfs"]
                > TRUE_PEAK_TARGET_DBFS + TRUE_PEAK_TOLERANCE_DB
            ):
                raise RenderOutputError("normalized dialogue is outside the loudness profile")
            if clipped_samples:
                raise RenderOutputError(
                    f"normalized dialogue contains {clipped_samples} clipped samples"
                )
            decode_result = adapter.full_decode_check(output_media)
            commands.append(_command_record(decode_result, output_media.parent, ffmpeg_version))
            if decode_result.exit_code != 0:
                raise RenderOutputError("full decode of the normalized base edit failed")
            output_probe = adapter.probe(output_media)
            output_video = _first_stream(output_probe, "video")
            output_audio = _first_stream(output_probe, "audio")
            format_duration_us = seconds_to_us(output_probe.get("format", {}).get("duration"))
            if format_duration_us is None:
                raise RenderOutputError("normalized base edit has no container duration")
            video_duration_us = _stream_duration_us(output_video, format_duration_us)
            audio_duration_us = _stream_duration_us(output_audio, format_duration_us)
            if video_duration_us is None or audio_duration_us is None:
                raise RenderOutputError("normalized base edit has no stream durations")
            duration_difference_us = abs(
                format_duration_us - int(edl["expected_output_duration_us"])
            )
            av_drift_us = abs(video_duration_us - audio_duration_us)
            frame_count = adapter.probe_frame_count(staged_normalized)
            if frame_count is None:
                raise RenderOutputError("ffprobe did not return a decoded video frame count")
            duration_tolerance_us = max(
                AV_SYNC_TOLERANCE_US,
                round(1_000_000 * frame_rate["denominator"] / frame_rate["numerator"]),
            )
            if duration_difference_us > duration_tolerance_us:
                raise RenderOutputError(
                    "normalized base edit duration differs from approved EDL beyond tolerance"
                )
            if av_drift_us > AV_SYNC_TOLERANCE_US:
                raise RenderOutputError("picture and production audio drift beyond tolerance")
            frame_status = "pass" if abs(frame_count - expected_frame_count) <= 1 else "fail"
            if frame_status == "fail":
                raise RenderOutputError(
                    "decoded frame count "
                    f"{frame_count} does not match expected {expected_frame_count}"
                )
            _promote_media(output_media, final_output)
            staged_cut.unlink(missing_ok=True)
            rebased = rebase_transcript(
                transcript_value,
                sha256_file(transcript_path),
                edl_path,
                mapping,
                int(edl["expected_output_duration_us"]),
                project_id=layout.root.name,
                revision_id=revision_id,
                source_transcript_path=transcript_path,
            )
            rebased["config_sha256"] = config_sha256(layout)
            validate_transcript_timing(rebased)
            write_validated_artifact(package_root, "transcript", transcript_output_path, rebased)
            if not (layout.artifacts / "transcript-output.json").exists():
                write_validated_artifact(
                    package_root,
                    "transcript",
                    layout.artifacts / "transcript-output.json",
                    rebased,
                )
            transcript_markdown = layout.review / f"transcript-output-{stage_key[:16]}.md"
            write_text_atomically(
                transcript_markdown,
                "\n".join(
                    [
                        "# Rebasing report",
                        "",
                        f"- Source transcript SHA-256: {sha256_file(transcript_path)}",
                        f"- Output duration (microseconds): {rebased['output_duration_us']}",
                        f"- Retained words: {len(rebased['words'])}",
                        f"- Warnings: {len(rebased['warnings'])}",
                        "",
                    ]
                ),
            )
            output_frame_rate = parse_rate(output_video.get("avg_frame_rate")) or frame_rate
            artifact_id = "art_render_rough" if render_type == "rough" else "art_render_final"
            payload: dict[str, Any] = {
                "schema_name": "render_manifest",
                "schema_version": "1.0.0",
                "artifact_id": artifact_id,
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer("base-render", "ffmpeg", ffmpeg_version),
                "inputs": [
                    artifact_input("art_source", source_manifest_path),
                    artifact_input("art_edl", edl_path),
                    artifact_input("art_transcript", transcript_path),
                    *(
                        [artifact_input("art_gate1_approval", approval_path)]
                        if approval_path is not None
                        else []
                    ),
                ],
                "config_sha256": config_sha256(layout),
                "render_type": render_type,
                "composition_artifact_id": "art_edl",
                "expected_duration_us": int(edl["expected_output_duration_us"]),
                "actual_duration_us": format_duration_us,
                "output": {
                    "path": str(final_output),
                    "sha256": sha256_file(final_output),
                    "size_bytes": final_output.stat().st_size,
                },
                "video": {
                    "codec": str(output_video.get("codec_name") or "unknown"),
                    "width": int(output_video.get("width") or 0),
                    "height": int(output_video.get("height") or 0),
                    "frame_rate": output_frame_rate,
                    "pixel_format": str(output_video.get("pix_fmt") or "unknown"),
                },
                "audio": {
                    "codec": str(output_audio.get("codec_name") or "unknown"),
                    "sample_rate_hz": int(output_audio.get("sample_rate") or 48000),
                    "channels": int(output_audio.get("channels") or 2),
                },
                "commands": commands,
                "warnings": warnings,
                "frame_count": int(frame_count),
                "expected_frame_count": expected_frame_count,
                "video_duration_us": video_duration_us,
                "audio_duration_us": audio_duration_us,
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
                },
                "av_sync": {
                    "video_duration_us": video_duration_us,
                    "audio_duration_us": audio_duration_us,
                    "drift_us": av_drift_us,
                    "tolerance_us": AV_SYNC_TOLERANCE_US,
                    "status": "pass",
                },
                "validation": {
                    "full_decode": "pass",
                    "frame_count": frame_status,
                    "duration": "pass",
                    "audio_duration": "pass",
                    "clipping": "pass",
                    "loudness": "pass",
                },
                "source_to_output_mapping": [dict(item) for item in mapping],
            }
            write_validated_artifact(package_root, "render_manifest", manifest_path, payload)
            alias = layout.artifacts / (
                "render-rough.json" if render_type == "rough" else "render-final.json"
            )
            if not alias.exists():
                write_validated_artifact(package_root, "render_manifest", alias, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={
                    "render_manifest": manifest_path,
                    "output_media": final_output,
                    "transcript": transcript_output_path,
                },
                warnings=list(dict.fromkeys(warnings + list(rebased["warnings"]))),
            )
            _mark_project_rendered(package_root, layout, artifact_id)
            return manifest_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="base_render_failed", message=message)
            raise RenderOutputError(message) from exc
