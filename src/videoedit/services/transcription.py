from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.adapters.transcription import (
    TranscriptionAdapter,
    TranscriptionResult,
    WhisperAdapter,
)
from videoedit.errors import SourceIntegrityError, TranscriptionOutputError, VideoeditError
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
from videoedit.services.media import CANONICAL_TIME_CONVERSION_VERSION, parse_seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)


def seconds_to_us(value: object) -> int:
    """Convert a provider timestamp to nonnegative canonical microseconds."""

    parsed = parse_seconds_to_us(value)
    return max(0, parsed) if parsed is not None else 0


def source_from_manifest(layout: ProjectLayout) -> tuple[Path, Path, dict[str, Any]]:
    manifest_path = layout.artifacts / "source-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("source-manifest.json is missing. Run ingest first.")
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_value, dict):
        raise TranscriptionOutputError("source-manifest.json must contain an object")
    selected_value = manifest_value.get("managed_path") or manifest_value.get("source_path")
    if not isinstance(selected_value, str) or not selected_value:
        raise TranscriptionOutputError("source manifest has no usable source path")
    source_path = Path(selected_value).expanduser().resolve()
    if not source_path.is_file():
        raise SourceIntegrityError(f"registered source does not exist: {source_path}")
    expected_hash = manifest_value.get("sha256")
    if isinstance(expected_hash, str) and sha256_file(source_path) != expected_hash:
        raise SourceIntegrityError(f"registered source hash mismatch: {source_path}")
    return source_path, manifest_path, manifest_value


def speech_proxy_from_manifest(
    package_root: Path,
    layout: ProjectLayout,
) -> tuple[Path, Path, dict[str, Any]]:
    _source, _source_manifest_path, source_value = source_from_manifest(layout)
    proxy_manifest_path = layout.artifacts / "media-proxy-speech.json"
    if not proxy_manifest_path.is_file():
        raise TranscriptionOutputError(
            "speech proxy manifest is missing; ingest a source with an audio stream first"
        )
    proxy_value = json.loads(proxy_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(proxy_value, dict):
        raise TranscriptionOutputError("speech proxy manifest must contain an object")
    try:
        validate_artifact(package_root, "media_proxy", proxy_value)
    except ValueError as exc:
        raise TranscriptionOutputError(f"speech proxy manifest is invalid: {exc}") from exc
    if proxy_value.get("kind") != "speech":
        raise TranscriptionOutputError("media-proxy-speech.json does not describe a speech proxy")
    if proxy_value.get("source_sha256") != source_value.get("sha256"):
        raise TranscriptionOutputError("speech proxy source hash does not match source manifest")
    output_value = proxy_value.get("output")
    if not isinstance(output_value, dict) or not isinstance(output_value.get("path"), str):
        raise TranscriptionOutputError("speech proxy output reference is missing")
    output_path = Path(str(output_value["path"])).expanduser().resolve()
    try:
        output_path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise TranscriptionOutputError("speech proxy path escapes the project") from exc
    if not output_path.is_file():
        raise TranscriptionOutputError(f"speech proxy output does not exist: {output_path}")
    if output_path.stat().st_size != int(output_value.get("size_bytes", -1)):
        raise TranscriptionOutputError("speech proxy size does not match its manifest")
    if sha256_file(output_path) != output_value.get("sha256"):
        raise TranscriptionOutputError("speech proxy hash does not match its manifest")
    return output_path, proxy_manifest_path, proxy_value


def _raw_seconds_to_us(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return parse_seconds_to_us(value)


def _bounded_interval(
    start_value: object,
    end_value: object,
    duration_us: int,
    label: str,
    warnings: list[str],
) -> tuple[int, int, str]:
    start_raw = _raw_seconds_to_us(start_value)
    end_raw = _raw_seconds_to_us(end_value)
    status = "original"
    if start_raw is None:
        warnings.append(f"invalid_start_time:{label}")
        start_raw = 0
        status = "uncertain"
    if end_raw is None:
        warnings.append(f"invalid_end_time:{label}")
        end_raw = start_raw + 1
        status = "uncertain"
    if start_raw < 0:
        warnings.append(f"negative_start_time:{label}")
        start_raw = 0
        status = "adjusted" if status == "original" else status
    if end_raw < 0:
        warnings.append(f"negative_end_time:{label}")
        end_raw = 0
        status = "adjusted" if status == "original" else status
    start = min(max(start_raw, 0), duration_us)
    end = min(max(end_raw, 0), duration_us)
    if start != start_raw or end != end_raw:
        warnings.append(f"out_of_bounds_time:{label}")
        status = "adjusted" if status == "original" else status
    if end <= start:
        warnings.append(f"reversed_or_zero_duration:{label}")
        status = "uncertain"
        if duration_us <= 0:
            raise TranscriptionOutputError("source duration must be positive")
        if start >= duration_us:
            start = max(0, duration_us - 1)
            end = duration_us
        else:
            end = min(duration_us, start + 1)
    return start, end, status


def _probability(value: object, label: str, warnings: list[str]) -> float | None:
    if value is None:
        return None
    try:
        probability = float(str(value))
    except (TypeError, ValueError):
        warnings.append(f"invalid_probability:{label}")
        return None
    if not math.isfinite(probability) or probability < 0 or probability > 1:
        warnings.append(f"out_of_range_probability:{label}")
        return None
    return probability


def _optional_float(value: object, label: str, warnings: list[str]) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        warnings.append(f"invalid_numeric_evidence:{label}")
        return None
    if not math.isfinite(number):
        warnings.append(f"invalid_numeric_evidence:{label}")
        return None
    return number


def normalize_whisper_result(
    result: dict[str, Any],
    project_id: str,
    revision_id: str,
    duration_us: int,
    model_name: str,
    source_input: dict[str, str],
    config_hash: str,
    *,
    model_identifier: str | None = None,
    device: str = "unknown",
    adapter_id: str = "openai-whisper-local",
    adapter_version: str = "unknown",
    model_sha256: str | None = None,
    low_confidence_threshold: float = 0.6,
) -> dict[str, Any]:
    if duration_us <= 0:
        raise TranscriptionOutputError("transcript source duration must be positive")
    if not 0 <= low_confidence_threshold <= 1:
        raise ValueError("low_confidence_threshold must be between 0 and 1")
    if model_sha256 is not None:
        if len(model_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in model_sha256
        ):
            raise TranscriptionOutputError("model_sha256 must be a lowercase SHA-256 digest")
    raw_segments = result.get("segments", [])
    if raw_segments is None:
        raw_segments = []
    if not isinstance(raw_segments, list):
        raise TranscriptionOutputError("transcription segments must be an array")

    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    warnings: list[str] = []
    word_counter = 0
    previous_word_start: int | None = None
    previous_word_end: int | None = None
    previous_segment_start: int | None = None
    probabilities: list[float] = []
    low_confidence_word_ids: list[str] = []
    speakers: set[str] = set()

    for segment_index, raw_segment_value in enumerate(raw_segments, start=1):
        if not isinstance(raw_segment_value, dict):
            raise TranscriptionOutputError(f"segment {segment_index} is not an object")
        raw_segment: dict[str, Any] = raw_segment_value
        segment_id = f"seg_{segment_index:06d}"
        segment_start, segment_end, _segment_status = _bounded_interval(
            raw_segment.get("start", 0),
            raw_segment.get("end", duration_us / 1_000_000),
            duration_us,
            segment_id,
            warnings,
        )
        if previous_segment_start is not None and segment_start < previous_segment_start:
            warnings.append(f"nonmonotonic_segment_order:{segment_id}")
        previous_segment_start = segment_start

        raw_words = raw_segment.get("words", [])
        if raw_words is None:
            raw_words = []
        if not isinstance(raw_words, list):
            raise TranscriptionOutputError(f"words for {segment_id} must be an array")
        word_ids: list[str] = []
        segment_words: list[str] = []
        for raw_word_value in raw_words:
            if not isinstance(raw_word_value, dict):
                raise TranscriptionOutputError(f"word in {segment_id} is not an object")
            raw_word: dict[str, Any] = raw_word_value
            text = str(raw_word.get("word", "")).strip()
            if not text:
                warnings.append(f"empty_word_skipped:{segment_id}")
                continue
            word_counter += 1
            word_id = f"wrd_{word_counter:06d}"
            start_value = raw_word.get("start", raw_segment.get("start", 0))
            end_value = raw_word.get("end", raw_segment.get("end", duration_us / 1_000_000))
            start_us, end_us, timing_status = _bounded_interval(
                start_value,
                end_value,
                duration_us,
                word_id,
                warnings,
            )
            if previous_word_start is not None and start_us < previous_word_start:
                warnings.append(f"nonmonotonic_word_order:{word_id}")
            if previous_word_end is not None and start_us < previous_word_end:
                warnings.append(f"overlapping_word_timing:{word_id}")
            if start_us < segment_start or end_us > segment_end:
                warnings.append(f"word_outside_segment:{word_id}")
            previous_word_start = start_us
            previous_word_end = end_us
            probability = _probability(raw_word.get("probability"), word_id, warnings)
            if probability is not None:
                probabilities.append(probability)
                if probability < low_confidence_threshold:
                    low_confidence_word_ids.append(word_id)
            speaker = raw_word.get("speaker")
            if speaker not in (None, ""):
                speakers.add(str(speaker))
            words.append(
                {
                    "word_id": word_id,
                    "segment_id": segment_id,
                    "text": text,
                    "start_us": start_us,
                    "end_us": end_us,
                    "probability": probability,
                    "timing_status": timing_status,
                }
            )
            word_ids.append(word_id)
            segment_words.append(text)

        segment_text = str(raw_segment.get("text", "")).strip()
        if not segment_text and segment_words:
            segment_text = " ".join(segment_words)
            warnings.append(f"segment_text_reconstructed:{segment_id}")
        no_speech_probability = _probability(
            raw_segment.get("no_speech_prob"), f"{segment_id}:no_speech", warnings
        )
        average_log_probability = _optional_float(
            raw_segment.get("avg_logprob"), f"{segment_id}:avg_logprob", warnings
        )
        if no_speech_probability is not None and no_speech_probability >= 0.6:
            warnings.append(f"high_no_speech_probability:{segment_id}")
        segment_speaker = raw_segment.get("speaker")
        if segment_speaker not in (None, ""):
            speakers.add(str(segment_speaker))
        segments.append(
            {
                "segment_id": segment_id,
                "text": segment_text,
                "start_us": segment_start,
                "end_us": segment_end,
                "word_ids": word_ids,
                "average_log_probability": average_log_probability,
                "no_speech_probability": no_speech_probability,
            }
        )

    if not words:
        warnings.append("no_speech_detected")
    if result.get("text") is None and segments:
        warnings.append("transcript_text_missing")
    transcript_text = str(result.get("text", "")).strip()
    if not transcript_text and segments:
        transcript_text = " ".join(
            str(segment["text"]) for segment in segments if str(segment["text"])
        ).strip()
        if transcript_text:
            warnings.append("transcript_text_reconstructed")
    if len(speakers) > 1:
        warnings.append("multiple_speakers_detected")
    if low_confidence_word_ids:
        warnings.append(f"low_confidence_words:{len(low_confidence_word_ids)}")

    confidence_summary = {
        "word_count": len(words),
        "mean_word_probability": (
            sum(probabilities) / len(probabilities) if probabilities else None
        ),
        "minimum_word_probability": min(probabilities) if probabilities else None,
        "low_confidence_word_ids": low_confidence_word_ids,
        "uncertain_word_count": sum(word["timing_status"] == "uncertain" for word in words),
        "speaker_count": len(speakers),
    }
    payload = {
        "schema_name": "transcript",
        "schema_version": "1.0.0",
        "artifact_id": "art_transcript",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("transcription", adapter_id, adapter_version),
        "inputs": [source_input],
        "config_sha256": config_hash,
        "source_duration_us": duration_us,
        "language": str(result.get("language") or "und"),
        "model": f"whisper-{model_name}",
        "model_identifier": model_identifier or f"whisper-{model_name}",
        "device": device,
        "text": transcript_text,
        "segments": segments,
        "words": words,
        "warnings": list(dict.fromkeys(warnings)),
        "confidence_summary": confidence_summary,
        "raw_result": json.loads(json.dumps(result)),
        "status": "warning" if warnings else "complete",
    }
    if model_sha256 is not None:
        payload["model_sha256"] = model_sha256
    validate_transcript_timing(payload)
    return payload


def validate_transcript_timing(payload: dict[str, Any]) -> None:
    source_duration_us = payload.get("source_duration_us")
    duration_us = payload.get("output_duration_us", source_duration_us)
    if not isinstance(source_duration_us, int) or source_duration_us <= 0:
        raise TranscriptionOutputError("transcript duration must be a positive integer")
    if not isinstance(duration_us, int) or duration_us <= 0:
        raise TranscriptionOutputError("transcript timeline duration must be a positive integer")
    if duration_us > source_duration_us:
        raise TranscriptionOutputError("transcript output duration exceeds source duration")
    mapping = payload.get("source_to_output_mapping")
    if mapping is not None:
        if not isinstance(mapping, list):
            raise TranscriptionOutputError("transcript source-to-output mapping must be an array")
        previous_source_end = -1
        previous_output_end = -1
        for item in mapping:
            if not isinstance(item, dict):
                raise TranscriptionOutputError("transcript mapping item must be an object")
            source_start = item.get("source_start_us")
            source_end = item.get("source_end_us")
            output_start = item.get("output_start_us")
            output_end = item.get("output_end_us")
            if not (
                isinstance(source_start, int)
                and isinstance(source_end, int)
                and isinstance(output_start, int)
                and isinstance(output_end, int)
            ):
                raise TranscriptionOutputError("transcript mapping uses non-integer timing")
            if (
                source_start < 0
                or source_end <= source_start
                or source_end > source_duration_us
                or output_start < 0
                or output_end <= output_start
                or output_end > duration_us
                or source_start < previous_source_end
                or output_start < previous_output_end
            ):
                raise TranscriptionOutputError("transcript mapping is unordered or out of bounds")
            previous_source_end = source_end
            previous_output_end = output_end
    segments = payload.get("segments")
    words = payload.get("words")
    if not isinstance(segments, list) or not isinstance(words, list):
        raise TranscriptionOutputError("transcript segments and words must be arrays")
    segment_ids: set[str] = set()
    referenced_word_ids: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise TranscriptionOutputError("transcript segment must be an object")
        segment_id = str(segment.get("segment_id"))
        if segment_id in segment_ids:
            raise TranscriptionOutputError(f"duplicate segment id: {segment_id}")
        segment_ids.add(segment_id)
        start_us = segment.get("start_us")
        end_us = segment.get("end_us")
        if not isinstance(start_us, int) or not isinstance(end_us, int):
            raise TranscriptionOutputError(f"segment {segment_id} has non-integer timing")
        if start_us < 0 or end_us > duration_us or end_us <= start_us:
            raise TranscriptionOutputError(f"segment {segment_id} timing is out of bounds")
    word_ids: set[str] = set()
    word_segment_ids: dict[str, str] = {}
    previous_start = -1
    for word in words:
        if not isinstance(word, dict):
            raise TranscriptionOutputError("transcript word must be an object")
        word_id = str(word.get("word_id"))
        if word_id in word_ids:
            raise TranscriptionOutputError(f"duplicate word id: {word_id}")
        word_ids.add(word_id)
        segment_id = str(word.get("segment_id"))
        if segment_id not in segment_ids:
            raise TranscriptionOutputError(f"word {word_id} references an unknown segment")
        start_us = word.get("start_us")
        end_us = word.get("end_us")
        if not isinstance(start_us, int) or not isinstance(end_us, int):
            raise TranscriptionOutputError(f"word {word_id} has non-integer timing")
        if start_us < 0 or end_us > duration_us or end_us <= start_us:
            raise TranscriptionOutputError(f"word {word_id} timing is out of bounds")
        source_start_us = word.get("source_start_us")
        source_end_us = word.get("source_end_us")
        if source_start_us is not None or source_end_us is not None:
            if (
                not isinstance(source_start_us, int)
                or not isinstance(source_end_us, int)
                or source_start_us < 0
                or source_end_us <= source_start_us
                or source_end_us > source_duration_us
            ):
                raise TranscriptionOutputError(f"word {word_id} source timing is out of bounds")
        if start_us < previous_start:
            raise TranscriptionOutputError(f"word order is not monotonic at {word_id}")
        previous_start = start_us
        word_segment_ids[word_id] = segment_id
    for segment in segments:
        referenced = segment.get("word_ids", [])
        if (
            not isinstance(referenced, list)
            or not all(isinstance(word_id, str) for word_id in referenced)
            or any(word_id not in word_ids for word_id in referenced)
        ):
            raise TranscriptionOutputError(
                f"segment {segment['segment_id']} references an unknown word"
            )
        if len(set(referenced)) != len(referenced):
            raise TranscriptionOutputError(
                f"segment {segment['segment_id']} references a word more than once"
            )
        if any(word_segment_ids[word_id] != segment["segment_id"] for word_id in referenced):
            raise TranscriptionOutputError(
                f"segment {segment['segment_id']} references a word from another segment"
            )
        referenced_word_ids.update(referenced)
    if referenced_word_ids != word_ids:
        raise TranscriptionOutputError("transcript contains a word not referenced by a segment")


def _format_time_us(value: int) -> str:
    total_milliseconds = value // 1_000
    milliseconds = total_milliseconds % 1_000
    total_seconds = total_milliseconds // 1_000
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3_600
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def transcript_markdown(payload: dict[str, Any]) -> str:
    duration_us = int(payload.get("output_duration_us", payload["source_duration_us"]))
    lines = [
        "# Transcript",
        "",
        f"- Language: {payload['language']}",
        f"- Model: {payload.get('model_identifier', payload['model'])}",
        f"- Device: {payload.get('device', 'unknown')}",
        f"- Duration: {_format_time_us(duration_us)}",
        f"- Status: {payload.get('status', 'complete')}",
        "",
    ]
    if payload.get("model_sha256"):
        lines.insert(4, f"- Model SHA-256: {payload['model_sha256']}")
    warnings = payload.get("warnings", [])
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(["## Segments", ""])
    for segment in payload["segments"]:
        lines.append(
            f"[{_format_time_us(int(segment['start_us']))} - "
            f"{_format_time_us(int(segment['end_us']))}] {segment['text']}"
        )
        word_ids = ", ".join(str(word_id) for word_id in segment["word_ids"])
        if word_ids:
            lines.append(f"  Word IDs: {word_ids}")
    lines.append("")
    return "\n".join(lines)


def _stage_file_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return False
    path = Path(str(value["path"])).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise TranscriptionOutputError("transcription stage artifact escapes the project") from exc
    return (
        path.is_file()
        and path.stat().st_size == int(value.get("size_bytes", -1))
        and sha256_file(path) == value.get("sha256")
    )


def _cached_transcript(
    package_root: Path,
    layout: ProjectLayout,
    state: dict[str, Any] | None,
    stage_key: str,
) -> Path | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    if not all(name in artifacts for name in ("transcript", "transcript_markdown")):
        return None
    if not all(_stage_file_ref_valid(layout, artifacts[name]) for name in artifacts):
        return None
    transcript_path = Path(str(artifacts["transcript"]["path"])).expanduser().resolve()
    transcript_value = json.loads(transcript_path.read_text(encoding="utf-8"))
    if not isinstance(transcript_value, dict):
        return None
    try:
        validate_artifact(package_root, "transcript", transcript_value)
        validate_transcript_timing(transcript_value)
    except (ValueError, TranscriptionOutputError):
        return None
    return transcript_path


def _mark_project_transcribed(
    package_root: Path,
    layout: ProjectLayout,
    transcript_path: Path,
) -> None:
    manifest_path = layout.state / "project-manifest.json"
    if not manifest_path.is_file():
        return
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TranscriptionOutputError("project manifest is not an object")
    value["updated_at"] = now_iso()
    value["state"] = "analyzed"
    active_artifacts = value.setdefault("active_artifacts", {})
    if not isinstance(active_artifacts, dict):
        raise TranscriptionOutputError("project manifest active_artifacts is not an object")
    active_artifacts["transcript"] = "art_transcript"
    write_validated_artifact(package_root, "project_manifest", manifest_path, value)


def transcribe_project(
    package_root: Path,
    layout: ProjectLayout,
    model_name: str,
    revision_id: str = "rev_001",
    adapter: TranscriptionAdapter | None = None,
) -> Path:
    if not model_name.strip():
        raise ValueError("model_name must not be empty")
    source, source_manifest_path, source_manifest = source_from_manifest(layout)
    del source
    speech_proxy, speech_manifest_path, speech_manifest = speech_proxy_from_manifest(
        package_root, layout
    )
    source_duration_value = source_manifest.get("media_duration_us")
    if not isinstance(source_duration_value, int) or source_duration_value <= 0:
        raise TranscriptionOutputError("source manifest has no positive media duration")
    selected_adapter: TranscriptionAdapter = adapter or WhisperAdapter()
    adapter_id = str(getattr(selected_adapter, "adapter_id", "transcription-adapter"))
    adapter_version = str(getattr(selected_adapter, "adapter_version", "unknown"))
    device = str(getattr(selected_adapter, "device", "unknown"))
    model_path_value = getattr(selected_adapter, "model_path", None)
    model_identity = "none"
    if isinstance(model_path_value, Path) and model_path_value.is_file():
        model_identity = sha256_file(model_path_value)
    speech_hash = str(speech_manifest["output"]["sha256"])
    source_hash = str(source_manifest["sha256"])
    stage_key = make_stage_key(
        "transcribe",
        __version__,
        [speech_hash, source_hash],
        {
            "schema_version": "1.0.0",
            "config_sha256": config_sha256(layout),
            "model_name": model_name,
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "device": device,
            "model_identity": model_identity,
            "canonical_time_conversion": CANONICAL_TIME_CONVERSION_VERSION,
        },
    )
    with ProjectLock(layout, stage="transcribe", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "transcribe", revision_id)
        cached = _cached_transcript(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"transcribe-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="transcribe",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        try:
            result = selected_adapter.transcribe(speech_proxy, model_name)
            if not isinstance(result, TranscriptionResult):
                raise TranscriptionOutputError(
                    "transcription adapter did not return TranscriptionResult"
                )
            payload = normalize_whisper_result(
                result=result.raw_result,
                project_id=layout.root.name,
                revision_id=revision_id,
                duration_us=source_duration_value,
                model_name=model_name,
                source_input=artifact_input("art_source", source_manifest_path),
                config_hash=config_sha256(layout),
                model_identifier=result.model_identifier,
                device=result.device,
                adapter_id=result.adapter_id,
                adapter_version=result.adapter_version,
                model_sha256=(
                    result.model_sha256 or (model_identity if model_identity != "none" else None)
                ),
            )
            payload["inputs"].append(artifact_input("art_proxy_speech", speech_manifest_path))
            validate_transcript_timing(payload)
            output = layout.artifacts / "transcript.json"
            write_validated_artifact(package_root, "transcript", output, payload)
            markdown_path = layout.review / "transcript.md"
            write_text_atomically(markdown_path, transcript_markdown(payload))
            _mark_project_transcribed(package_root, layout, output)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={"transcript": output, "transcript_markdown": markdown_path},
                warnings=list(payload["warnings"]),
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
                code="transcription_failed",
                message=message,
            )
            raise TranscriptionOutputError(message) from exc
