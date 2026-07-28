from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.transcription import (
    TranscriptionAdapter,
    TranscriptionResult,
    WhisperAdapter,
)
from videoedit.errors import PlanningValidationError, StateConflictError, TranscriptionOutputError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.transcription import normalize_whisper_result, validate_transcript_timing

IMPLEMENTATION_VERSION = "p10-05c"
SEGMENT_COMPARISON_IMPLEMENTATION_VERSION = "p10-05e"
TRANSCRIPT_CLOCK_TOLERANCE_US = 100_000
_TOKEN_PUNCTUATION = re.compile(r"^[^\w']+|[^\w']+$")


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


def _file_ref(artifact_id: str, path: Path) -> dict[str, str]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
    }


def _declared_model_identity(transcriber: object) -> str:
    """Return the model identity available before a transcription starts."""

    declared_hash = getattr(transcriber, "model_sha256", None)
    if isinstance(declared_hash, str):
        normalized = declared_hash.casefold()
        if len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        ):
            return normalized

    model_path_value = getattr(transcriber, "model_path", None)
    if isinstance(model_path_value, (str, Path)):
        try:
            model_path = Path(model_path_value).expanduser().resolve()
            if model_path.is_file():
                return sha256_file(model_path)
        except (OSError, RuntimeError):
            return "unavailable"
    return "none"


def _media_manifest(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    revision_id: str,
) -> dict[str, Any]:
    manifest = _read_object(path, "revision media manifest")
    validate_artifact(package_root, "revision_media_manifest", manifest)
    if manifest["project_id"] != layout.root.name or manifest["revision_id"] != revision_id:
        raise PlanningValidationError("revision media manifest belongs to another project/revision")
    output_ref = manifest["output"]
    output_path = _owned_path(layout, Path(str(output_ref["path"])), "revision output")
    if not output_path.is_file() or sha256_file(output_path) != output_ref["sha256"]:
        raise PlanningValidationError("revision output hash is stale")
    source_ref = manifest["source"]
    source_path = _owned_path(layout, Path(str(source_ref["path"])), "revision source")
    if not source_path.is_file() or sha256_file(source_path) != source_ref["sha256"]:
        raise PlanningValidationError("revision source hash is stale")
    return manifest


def _intended_transcript(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    source_duration_us: int,
) -> dict[str, Any]:
    transcript = _read_object(path, "intended transcript")
    validate_artifact(package_root, "transcript", transcript)
    validate_transcript_timing(transcript)
    if transcript["project_id"] != layout.root.name:
        raise PlanningValidationError("intended transcript belongs to another project")
    transcript_source_duration_us = int(transcript["source_duration_us"])
    if abs(transcript_source_duration_us - source_duration_us) <= TRANSCRIPT_CLOCK_TOLERANCE_US:
        return transcript

    # A chained revision may use the already-retimed parent output as its
    # source. In that case the parent rendered transcript keeps the original
    # source duration but explicitly records the parent output duration and
    # source-to-output mapping. Its word timestamps are in that output clock,
    # which is the correct intended clock for the child revision's new cuts.
    transcript_output_duration = transcript.get("output_duration_us")
    mapping = transcript.get("source_to_output_mapping")
    if (
        isinstance(transcript_output_duration, int)
        and abs(transcript_output_duration - source_duration_us) <= TRANSCRIPT_CLOCK_TOLERANCE_US
        and isinstance(mapping, list)
        and mapping
    ):
        return transcript
    raise PlanningValidationError("intended transcript duration differs from revision source")
    return transcript


def _range(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, Mapping):
        raise PlanningValidationError("source-to-output mapping item must be an object")
    try:
        start_us = int(str(value["source_start_us"]))
        end_us = int(str(value["source_end_us"]))
        output_start_us = int(str(value["output_start_us"]))
        output_end_us = int(str(value["output_end_us"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError("source-to-output mapping has invalid bounds") from exc
    if end_us <= start_us or output_end_us <= output_start_us:
        raise PlanningValidationError("source-to-output mapping must be half-open")
    return start_us, end_us, output_start_us, output_end_us


def _map_source_time(source_time_us: int, mapping: Sequence[Mapping[str, Any]]) -> int | None:
    for item in mapping:
        source_start_us, source_end_us, output_start_us, _output_end_us = _range(item)
        if source_start_us <= source_time_us <= source_end_us:
            return output_start_us + (source_time_us - source_start_us)
    return None


def _overlap(
    start_us: int,
    end_us: int,
    ranges: Sequence[Mapping[str, Any]],
) -> tuple[bool, bool]:
    any_overlap = False
    full_overlap = False
    for item in ranges:
        removal_start_us = int(str(item["start_us"]))
        removal_end_us = int(str(item["end_us"]))
        overlap = min(end_us, removal_end_us) - max(start_us, removal_start_us)
        if overlap > 0:
            any_overlap = True
            full_overlap = start_us >= removal_start_us and end_us <= removal_end_us
    return any_overlap, full_overlap


def _token(value: object) -> str:
    normalized = str(value).strip().lower()
    normalized = _TOKEN_PUNCTUATION.sub("", normalized)
    return normalized


def _ordered_count_difference(
    expected: Sequence[str], actual: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    missing: list[str] = []
    unexpected: list[str] = []
    duplicate: list[str] = []
    for word in expected:
        if actual_counts[word] < expected_counts[word] and word not in missing:
            missing.append(word)
    for word in actual:
        if actual_counts[word] > expected_counts[word] and word not in unexpected:
            unexpected.append(word)
        if actual_counts[word] > expected_counts[word] and word not in duplicate:
            duplicate.append(word)
    return missing, unexpected, duplicate


def build_segment_transcript_comparison(
    global_comparison: Mapping[str, Any],
    *,
    project_id: str,
    revision_id: str,
    segment_id: str,
    start_us: int,
    end_us: int,
    producer_value: Mapping[str, str],
) -> dict[str, Any]:
    """Slice the current rendered comparison into one review-segment scope.

    The revision-wide Whisper comparison already contains the canonical expected
    output words and the rendered output words. Slicing that immutable evidence
    keeps segment review hash-bound without re-running Whisper once per contact
    sheet, while preserving absolute output-clock timestamps for join review.
    """

    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError("segment comparison range must be half-open")

    def overlapping_words(key: str) -> list[dict[str, Any]]:
        values = global_comparison.get(key, [])
        if not isinstance(values, list):
            raise PlanningValidationError(f"global comparison {key} must be an array")
        selected: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise PlanningValidationError(f"global comparison {key} item must be an object")
            if key == "expected_words":
                word_start = int(str(value["output_start_us"]))
                word_end = int(str(value["output_end_us"]))
            else:
                word_start = int(str(value["start_us"]))
                word_end = int(str(value["end_us"]))
            if min(word_end, end_us) - max(word_start, start_us) > 0:
                selected.append(dict(value))
        return selected

    expected_words = overlapping_words("expected_words")
    rendered_words = overlapping_words("rendered_words")
    expected_sequence = [_token(item["text"]) for item in expected_words if _token(item["text"])]
    rendered_sequence = [_token(item["text"]) for item in rendered_words if _token(item["text"])]
    missing_words, unexpected_words, duplicate_words = _ordered_count_difference(
        expected_sequence, rendered_sequence
    )
    warnings: list[str] = []
    if not expected_words:
        warnings.append("no_expected_words_in_segment_range")
    if not rendered_words:
        warnings.append("no_rendered_words_in_segment_range")
    ordering_match = expected_sequence == rendered_sequence
    if not ordering_match:
        warnings.append("word_sequence_mismatch")
    sequence_status = (
        "pass" if ordering_match and not missing_words and not unexpected_words else "fail"
    )
    return {
        "schema_name": "segment_transcript_comparison",
        "schema_version": "1.0.0",
        "artifact_id": f"art_segment_transcript_compare_{segment_id}_{revision_id}",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": dict(producer_value),
        "scope": {
            "segment_id": segment_id,
            "source_range": {"start_us": start_us, "end_us": end_us},
        },
        "intended_transcript": dict(global_comparison["intended_transcript"]),
        "rendered_transcript": dict(global_comparison["rendered_transcript"]),
        "revision_media": dict(global_comparison["revision_media"]),
        "expected_words": expected_words,
        "rendered_words": rendered_words,
        "expected_sequence": expected_sequence,
        "rendered_sequence": rendered_sequence,
        "missing_words": missing_words,
        "unexpected_words": unexpected_words,
        "duplicate_words": duplicate_words,
        "ordering_match": ordering_match,
        "sequence_status": sequence_status,
        "warnings": list(dict.fromkeys(warnings)),
        "status": "complete" if not warnings and sequence_status == "pass" else "warning",
    }


def compare_transcripts(
    intended: Mapping[str, Any],
    rendered: Mapping[str, Any],
    media_manifest: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    list[str],
    list[str],
    bool,
    list[str],
]:
    """Return expected rebased words and rendered words with sequence diagnostics."""

    mapping_value = media_manifest.get("source_to_output_mapping")
    removed_value = media_manifest.get("removed_ranges")
    if not isinstance(mapping_value, list) or not isinstance(removed_value, list):
        raise PlanningValidationError("revision media manifest lacks source mapping or removals")
    expected_words: list[dict[str, Any]] = []
    warnings: list[str] = []
    for word_value in intended.get("words", []):
        if not isinstance(word_value, Mapping):
            raise PlanningValidationError("intended transcript word must be an object")
        word_id = str(word_value.get("word_id", ""))
        try:
            source_start_us = int(str(word_value["start_us"]))
            source_end_us = int(str(word_value["end_us"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningValidationError(f"intended word has invalid bounds: {word_id}") from exc
        any_overlap, full_overlap = _overlap(source_start_us, source_end_us, removed_value)
        if full_overlap:
            continue
        if any_overlap:
            warnings.append(f"partially_removed_word:{word_id}")
            continue
        output_start_us = _map_source_time(source_start_us, mapping_value)
        output_end_us = _map_source_time(source_end_us, mapping_value)
        if output_start_us is None or output_end_us is None:
            warnings.append(f"unmapped_intended_word:{word_id}")
            continue
        expected_words.append(
            {
                "source_word_id": word_id,
                "text": str(word_value.get("text", "")).strip(),
                "source_start_us": source_start_us,
                "source_end_us": source_end_us,
                "output_start_us": output_start_us,
                "output_end_us": output_end_us,
            }
        )

    rendered_words: list[dict[str, Any]] = []
    for word_value in rendered.get("words", []):
        if not isinstance(word_value, Mapping):
            raise PlanningValidationError("rendered transcript word must be an object")
        rendered_words.append(
            {
                "word_id": str(word_value.get("word_id", "")),
                "text": str(word_value.get("text", "")).strip(),
                "start_us": int(str(word_value["start_us"])),
                "end_us": int(str(word_value["end_us"])),
            }
        )
    expected_sequence = [_token(item["text"]) for item in expected_words if _token(item["text"])]
    rendered_sequence = [_token(item["text"]) for item in rendered_words if _token(item["text"])]
    missing, unexpected, duplicate = _ordered_count_difference(expected_sequence, rendered_sequence)
    ordering_match = expected_sequence == rendered_sequence
    if not ordering_match:
        warnings.append("word_sequence_mismatch")
    return (
        expected_words,
        rendered_words,
        expected_sequence,
        rendered_sequence,
        missing,
        unexpected,
        duplicate,
        ordering_match,
        warnings,
    )


def _comparison_payload(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    intended_path: Path,
    rendered_path: Path,
    media_path: Path,
    intended: Mapping[str, Any],
    rendered: Mapping[str, Any],
    media_manifest: Mapping[str, Any],
    producer_value: dict[str, str],
) -> dict[str, Any]:
    (
        expected_words,
        rendered_words,
        expected_sequence,
        rendered_sequence,
        missing_words,
        unexpected_words,
        duplicate_words,
        ordering_match,
        warnings,
    ) = compare_transcripts(intended, rendered, media_manifest)
    source_duration_us = int(media_manifest["source_duration_us"])
    payload: dict[str, Any] = {
        "schema_name": "segment_transcript_comparison",
        "schema_version": "1.0.0",
        "artifact_id": f"art_segment_transcript_compare_{revision_id}",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer_value,
        "scope": {
            "segment_id": "segment_revision",
            "source_range": {"start_us": 0, "end_us": source_duration_us},
        },
        "intended_transcript": _file_ref("intended_transcript", intended_path),
        "rendered_transcript": _file_ref("rendered_transcript", rendered_path),
        "revision_media": _file_ref("revision_media_manifest", media_path),
        "expected_words": expected_words,
        "rendered_words": rendered_words,
        "expected_sequence": expected_sequence,
        "rendered_sequence": rendered_sequence,
        "missing_words": missing_words,
        "unexpected_words": unexpected_words,
        "duplicate_words": duplicate_words,
        "ordering_match": ordering_match,
        "sequence_status": "pass"
        if ordering_match and not missing_words and not unexpected_words
        else "fail",
        "warnings": sorted(set(warnings)),
        "status": "complete"
        if ordering_match and not missing_words and not unexpected_words and not warnings
        else "warning",
    }
    validate_artifact(package_root, "segment_transcript_comparison", payload)
    return payload


def _cached_comparison(
    package_root: Path,
    project_id: str,
    revision_id: str,
    comparison_path: Path,
    intended_path: Path,
    rendered_path: Path,
    media_path: Path,
    *,
    model_name: str,
    adapter_id: str,
    adapter_version: str,
    device: str,
    model_identity: str,
    config_hash: str,
) -> Path | None:
    if not comparison_path.is_file() or not rendered_path.is_file():
        return None
    comparison = _read_object(comparison_path, "transcript comparison")
    validate_artifact(package_root, "segment_transcript_comparison", comparison)
    if comparison["project_id"] != project_id or comparison["revision_id"] != revision_id:
        return None
    refs = {
        "intended_transcript": intended_path,
        "rendered_transcript": rendered_path,
        "revision_media": media_path,
    }
    for name, path in refs.items():
        reference = comparison[name]
        if reference["path"] != str(path.resolve()) or reference["sha256"] != sha256_file(path):
            return None

    rendered = _read_object(rendered_path, "rendered transcript")
    validate_artifact(package_root, "transcript", rendered)
    if rendered["project_id"] != project_id or rendered["revision_id"] != revision_id:
        return None
    rendered_producer = rendered["producer"]
    if rendered_producer["adapter"] != adapter_id:
        return None
    if adapter_version != "unknown" and rendered_producer["adapter_version"] != adapter_version:
        return None
    if rendered["device"] != device:
        return None
    if rendered["model"] != f"whisper-{model_name}":
        return None
    if rendered["config_sha256"] != config_hash:
        return None
    stored_model_sha = rendered.get("model_sha256")
    if model_identity == "unavailable":
        return None
    if model_identity == "none":
        if stored_model_sha is not None:
            return None
    elif stored_model_sha != model_identity:
        return None
    return comparison_path


def retranscribe_revision(
    package_root: Path,
    layout: ProjectLayout,
    revision_media_path: Path,
    intended_transcript_path: Path,
    *,
    model_name: str = "small",
    adapter: FFmpegAdapter | None = None,
    transcriber: TranscriptionAdapter | None = None,
) -> Path:
    """Re-transcribe revision media locally and compare speech after approved cuts."""

    selected_media = _owned_path(layout, revision_media_path, "revision media manifest")
    selected_intended = _owned_path(layout, intended_transcript_path, "intended transcript")
    if not selected_media.is_file() or not selected_intended.is_file():
        raise PlanningValidationError("re-transcription inputs must exist")
    media_manifest = _read_object(selected_media, "revision media manifest")
    revision_id = str(media_manifest.get("revision_id", ""))
    if not revision_id:
        raise PlanningValidationError("revision media manifest has no revision id")
    media_manifest = _media_manifest(package_root, layout, selected_media, revision_id)
    intended = _intended_transcript(
        package_root,
        layout,
        selected_intended,
        int(media_manifest["source_duration_us"]),
    )
    selected_adapter = adapter or FFmpegAdapter()
    selected_transcriber = transcriber or WhisperAdapter()
    output_path = Path(str(media_manifest["output"]["path"])).resolve()
    rendered_path = layout.revision_root(revision_id) / "rendered-transcript.json"
    comparison_path = layout.revision_root(revision_id) / "transcript-comparison.json"
    media_hash = sha256_file(selected_media)
    intended_hash = sha256_file(selected_intended)
    output_hash = sha256_file(output_path)
    config_hash = config_sha256(layout)
    adapter_id = str(getattr(selected_transcriber, "adapter_id", "transcription-adapter"))
    adapter_version = str(getattr(selected_transcriber, "adapter_version", "unknown"))
    device = str(getattr(selected_transcriber, "device", "unknown"))
    model_identity = _declared_model_identity(selected_transcriber)

    with ProjectLock(layout, stage="segment_retranscription", revision_id=revision_id):
        if _cached_comparison(
            package_root,
            layout.root.name,
            revision_id,
            comparison_path,
            selected_intended,
            rendered_path,
            selected_media,
            model_name=model_name,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            device=device,
            model_identity=model_identity,
            config_hash=config_hash,
        ):
            return comparison_path
        if rendered_path.exists() or comparison_path.exists():
            raise StateConflictError("re-transcription outputs exist but are stale")
        stage_key = make_stage_key(
            "segment-retranscription",
            IMPLEMENTATION_VERSION,
            [media_hash, intended_hash, output_hash],
            {
                "revision_id": revision_id,
                "model_name": model_name,
                "adapter_id": adapter_id,
                "adapter_version": adapter_version,
                "device": device,
                "model_identity": model_identity,
                "config_sha256": config_hash,
            },
        )
        staging_root = layout.staging / "retranscription" / f"{revision_id}-{stage_key[:16]}"
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)
        speech_proxy = staging_root / "speech.wav"
        selected_adapter.create_speech_proxy(output_path, speech_proxy)
        result = selected_transcriber.transcribe(speech_proxy, model_name)
        if not isinstance(result, TranscriptionResult):
            raise TranscriptionOutputError(
                "transcription adapter did not return TranscriptionResult"
            )
        rendered = normalize_whisper_result(
            result=result.raw_result,
            project_id=layout.root.name,
            revision_id=revision_id,
            duration_us=int(media_manifest["output_duration_us"]),
            model_name=model_name,
            source_input=artifact_input("revision_media", output_path),
            config_hash=config_hash,
            model_identifier=result.model_identifier,
            device=result.device,
            adapter_id=result.adapter_id,
            adapter_version=result.adapter_version,
            model_sha256=(
                result.model_sha256 or (model_identity if model_identity != "none" else None)
            ),
        )
        rendered["artifact_id"] = f"art_rendered_transcript_{revision_id}"
        rendered["source_duration_us"] = int(media_manifest["source_duration_us"])
        rendered["output_duration_us"] = int(media_manifest["output_duration_us"])
        rendered["rebased_from_sha256"] = intended_hash
        rendered["source_to_output_mapping"] = media_manifest["source_to_output_mapping"]
        rendered["inputs"].append({"artifact_id": "intended_transcript", "sha256": intended_hash})
        validate_transcript_timing(rendered)
        staged_rendered_path = staging_root / "rendered-transcript.json"
        write_validated_artifact(package_root, "transcript", staged_rendered_path, rendered)
        comparison = _comparison_payload(
            package_root,
            layout,
            revision_id,
            selected_intended,
            staged_rendered_path,
            selected_media,
            intended,
            rendered,
            media_manifest,
            producer("segment-transcript-comparison", result.adapter_id, result.adapter_version),
        )
        staged_comparison_path = staging_root / "transcript-comparison.json"
        write_validated_artifact(
            package_root,
            "segment_transcript_comparison",
            staged_comparison_path,
            comparison,
        )
        comparison["rendered_transcript"]["path"] = str(rendered_path.resolve())
        comparison["rendered_transcript"]["sha256"] = sha256_file(staged_rendered_path)
        write_validated_artifact(
            package_root,
            "segment_transcript_comparison",
            staged_comparison_path,
            comparison,
        )
        rendered_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_rendered_path, rendered_path)
        os.replace(staged_comparison_path, comparison_path)
        return comparison_path


def write_segment_transcript_comparisons(
    package_root: Path,
    layout: ProjectLayout,
    preview_plan_path: Path,
    comparison_path: Path,
) -> list[Path]:
    """Write immutable transcript-comparison slices for current review segments."""

    selected_plan = _owned_path(layout, preview_plan_path, "segment preview plan")
    selected_comparison = _owned_path(layout, comparison_path, "transcript comparison")
    if not selected_plan.is_file() or not selected_comparison.is_file():
        raise PlanningValidationError("segment comparison inputs must exist")
    plan = _read_object(selected_plan, "segment preview plan")
    validate_artifact(package_root, "segment_preview", plan)
    global_comparison = _read_object(selected_comparison, "transcript comparison")
    validate_artifact(package_root, "segment_transcript_comparison", global_comparison)
    revision_id = str(plan["revision_id"])
    if (
        plan["project_id"] != layout.root.name
        or global_comparison["project_id"] != layout.root.name
    ):
        raise PlanningValidationError("segment comparison inputs belong to another project")
    if global_comparison["revision_id"] != revision_id:
        raise PlanningValidationError("transcript comparison belongs to another revision")
    for name in ("intended_transcript", "rendered_transcript", "revision_media"):
        reference = global_comparison[name]
        reference_path = _owned_path(layout, Path(str(reference["path"])), name)
        if not reference_path.is_file() or sha256_file(reference_path) != reference["sha256"]:
            raise PlanningValidationError(f"transcript comparison {name} reference is stale")

    plan_hash = sha256_file(selected_plan)
    comparison_hash = sha256_file(selected_comparison)
    segments = plan.get("segments")
    if not isinstance(segments, list):
        raise PlanningValidationError("segment preview plan segments must be an array")
    outputs: list[Path] = []
    with ProjectLock(layout, stage="segment_transcript_comparison_slices", revision_id=revision_id):
        for segment_value in segments:
            if not isinstance(segment_value, Mapping):
                raise PlanningValidationError("segment preview plan item must be an object")
            segment_id = str(segment_value["segment_id"])
            source_range = segment_value.get("source_range")
            if not isinstance(source_range, Mapping):
                raise PlanningValidationError(f"segment {segment_id} has no source range")
            start_us = int(str(source_range["start_us"]))
            end_us = int(str(source_range["end_us"]))
            preview_hash = str(segment_value["preview_sha256"])
            stage_key = make_stage_key(
                "segment-transcript-comparison",
                SEGMENT_COMPARISON_IMPLEMENTATION_VERSION,
                [plan_hash, comparison_hash, preview_hash],
                {
                    "project_id": layout.root.name,
                    "revision_id": revision_id,
                    "segment_id": segment_id,
                    "source_range": {"start_us": start_us, "end_us": end_us},
                },
            )
            output_path = (
                layout.revision_root(revision_id)
                / "segment-transcript-comparisons"
                / segment_id
                / f"{stage_key[:16]}.json"
            )
            producer_value = producer(
                "segment-transcript-slice",
                "revision-whisper-comparison",
                f"{SEGMENT_COMPARISON_IMPLEMENTATION_VERSION}:{comparison_hash[:16]}:{plan_hash[:16]}:{preview_hash[:16]}",
            )
            if output_path.is_file():
                current = _read_object(output_path, "segment transcript comparison")
                validate_artifact(package_root, "segment_transcript_comparison", current)
                if current.get("producer") == producer_value:
                    outputs.append(output_path)
                    continue
                raise StateConflictError(
                    f"segment transcript comparison exists with stale contents: {output_path}"
                )

            payload = build_segment_transcript_comparison(
                global_comparison,
                project_id=layout.root.name,
                revision_id=revision_id,
                segment_id=segment_id,
                start_us=start_us,
                end_us=end_us,
                producer_value=producer_value,
            )
            staging_root = (
                layout.staging
                / "segment-transcript-comparisons"
                / f"{revision_id}-{segment_id}-{stage_key[:16]}"
            )
            if staging_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed")
                if failed_root.exists():
                    failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
                os.replace(staging_root, failed_root)
            staging_root.mkdir(parents=True, exist_ok=False)
            staged_path = staging_root / output_path.name
            write_validated_artifact(
                package_root, "segment_transcript_comparison", staged_path, payload
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_path, output_path)
            outputs.append(output_path)
    return outputs
