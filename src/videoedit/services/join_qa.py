from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.transcription import (
    TranscriptionAdapter,
    TranscriptionResult,
    WhisperAdapter,
)
from videoedit.errors import PlanningValidationError, TranscriptionOutputError, VideoeditError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.join_repair import repair_join_plan
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.rendering import parse_clipped_samples
from videoedit.services.stage_state import (
    begin_stage,
    complete_stage,
    fail_stage,
    load_stage_state,
)
from videoedit.services.transcription import normalize_whisper_result

JOIN_QA_IMPLEMENTATION_VERSION = "1.6.1"
TranscriptClock = Literal["output", "source"]
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_BLACK_FRAME_PATTERN = re.compile(r"pblack\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_FREEZE_PATTERN = re.compile(r"freeze_start\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
_TRANSCRIPT_BOUNDARY_GUARD_US = 150_000


def _declared_transcriber_model_identity(transcriber: object) -> str:
    """Return a stable local model identity before a stage starts.

    The Whisper adapter exposes its local model path, while test and isolated
    adapters may expose a precomputed model hash. Unknown adapters retain a
    conservative ``none`` identity and still have their returned hash checked
    and recorded after transcription.
    """

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


@dataclass(frozen=True, slots=True)
class JoinQAPolicy:
    """Thresholds for rendered-join checks.

    Cut density is deliberately a warning signal. It never fails a join by
    itself; transcript, media, or continuity evidence must establish a defect.
    """

    preview_handle_us: int = 2_000_000
    warning_speech_rate_change_percent: float = 12.0
    fail_speech_rate_change_percent: float = 30.0
    warning_cuts_per_minute: float = 30.0
    freeze_duration_us: int = 200_000
    boundary_check_window_us: int = 500_000

    def __post_init__(self) -> None:
        if self.preview_handle_us < 0:
            raise ValueError("preview_handle_us must be nonnegative")
        if (
            not math.isfinite(self.warning_speech_rate_change_percent)
            or self.warning_speech_rate_change_percent < 0
        ):
            raise ValueError("warning speech-rate threshold must be finite and nonnegative")
        if (
            not math.isfinite(self.fail_speech_rate_change_percent)
            or self.fail_speech_rate_change_percent < self.warning_speech_rate_change_percent
        ):
            raise ValueError("fail speech-rate threshold must be >= warning threshold")
        if not math.isfinite(self.warning_cuts_per_minute) or self.warning_cuts_per_minute < 0:
            raise ValueError("warning cut-density threshold must be finite and nonnegative")
        if self.freeze_duration_us <= 0:
            raise ValueError("freeze_duration_us must be positive")
        if self.boundary_check_window_us <= 0:
            raise ValueError("boundary_check_window_us must be positive")


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.casefold().replace(chr(0x2019), "'"))


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _has_adjacent_duplicate(text: str) -> bool:
    tokens = _tokens(text)
    return any(left == right for left, right in pairwise(tokens))


def _int_value(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def compare_transcript_text(approved_text: str, rendered_text: str) -> dict[str, object]:
    """Compare normalized word sequences and classify missing/extra speech.

    Punctuation and casing do not create a false failure. Repeated tokens that
    are present in the approved text are reported separately from unexpected
    words so duplicate phrases can be routed to the join repair stage.
    """

    approved = _tokens(approved_text)
    rendered = _tokens(rendered_text)
    missing: list[str] = []
    unexpected: list[str] = []
    duplicate: list[str] = []
    matcher = SequenceMatcher(a=approved, b=rendered, autojunk=False)
    approved_counts = Counter(approved)
    rendered_counts = Counter(rendered)
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if opcode in {"delete", "replace"}:
            missing.extend(approved[a_start:a_end])
        if opcode in {"insert", "replace"}:
            for token in rendered[b_start:b_end]:
                if rendered_counts[token] > approved_counts[token] and token in approved_counts:
                    duplicate.append(token)
                else:
                    unexpected.append(token)
    for token, count in rendered_counts.items():
        if count > approved_counts[token] and token in approved_counts:
            duplicate.extend([token] * (count - approved_counts[token]))
    missing = _unique(missing)
    unexpected = _unique(unexpected)
    duplicate = _unique(duplicate)
    exact = not missing and not unexpected and not duplicate
    status = "pass" if exact else "fail"
    return {
        "approved_text": approved_text,
        "rendered_text": rendered_text,
        "missing_words": missing,
        "unexpected_words": unexpected,
        "duplicate_words": duplicate,
        "grammar_status": status,
        "meaning_status": status,
    }


def _compare_rendered_transcript_for_qa(
    approved_text: str,
    rendered_text: str,
) -> dict[str, object]:
    """Keep raw ASR differences visible without treating them as proven edit defects."""

    comparison = compare_transcript_text(approved_text, rendered_text)
    if comparison["grammar_status"] == "pass":
        return comparison
    approved_tokens = _tokens(approved_text)
    rendered_tokens = _tokens(rendered_text)
    if not rendered_tokens and approved_tokens:
        return comparison
    if _has_adjacent_duplicate(rendered_text):
        return comparison
    comparison["grammar_status"] = "warning"
    comparison["meaning_status"] = "warning"
    return comparison


def _evidence_bool(
    evidence: Mapping[str, object],
    key: str,
    *,
    default: bool = False,
) -> tuple[bool, bool]:
    value = evidence.get(key, default)
    known_key = f"{key}_known"
    known = bool(evidence.get(known_key, key in evidence))
    if not isinstance(value, bool):
        return default, False
    return value, known


def _audio_check(
    evidence: Mapping[str, object],
    *,
    clipping_known: bool,
    clipped_samples: int,
    decode_ok: bool,
    context_clipped_samples: int | None = None,
) -> dict[str, object]:
    clipped, clipped_known = _evidence_bool(
        evidence,
        "clipped_syllable",
        default=clipped_samples > 0,
    )
    clipped_known = clipped_known or clipping_known
    clipped_is_operator_defect = "clipped_syllable" in evidence and clipped
    clipped_is_unclassified = clipped and "clipped_syllable" not in evidence
    click, click_known = _evidence_bool(evidence, "click_or_pop")
    room_tone, room_known = _evidence_bool(evidence, "room_tone_jump")
    rhythm_value = evidence.get("speech_rhythm", "natural")
    rhythm = (
        rhythm_value
        if rhythm_value
        in {
            "natural",
            "slightly_tight",
            "rushed",
            "broken",
        }
        else "natural"
    )
    rhythm_known = bool(evidence.get("speech_rhythm_known", "speech_rhythm" in evidence))
    failures = clipped_is_operator_defect or click or room_tone or not decode_ok
    unknown = (
        not (clipped_known and click_known and room_known and rhythm_known)
        or clipped_is_unclassified
    )
    status = "fail" if failures else ("warning" if unknown else "pass")
    return {
        "clipped_syllable": clipped,
        "click_or_pop": click,
        "room_tone_jump": room_tone,
        "speech_rhythm": rhythm,
        "status": status,
        "context_clipped_samples": context_clipped_samples,
        "boundary_clipped_samples": max(0, clipped_samples),
        "boundary_clipping_known": clipped_known,
        "_clipped_defect": clipped_is_operator_defect,
    }


def _visual_check(
    evidence: Mapping[str, object],
    *,
    black_flash: bool,
    black_known: bool,
    freeze: bool,
    freeze_known: bool,
    decode_ok: bool,
    context_black_flash: bool | None = None,
    context_freeze: bool | None = None,
) -> dict[str, object]:
    black_value, black_evidence_known = _evidence_bool(
        evidence,
        "black_flash",
        default=black_flash,
    )
    freeze_value, freeze_evidence_known = _evidence_bool(
        evidence,
        "freeze",
        default=freeze,
    )
    black_known = black_known or black_evidence_known
    freeze_known = freeze_known or freeze_evidence_known
    duplicate, duplicate_known = _evidence_bool(evidence, "duplicate_frame")
    body_value = evidence.get("face_or_body_jump", "none")
    body_jump = body_value if body_value in {"none", "acceptable", "distracting"} else "none"
    body_known = bool(evidence.get("face_or_body_jump_known", "face_or_body_jump" in evidence))
    screen_value = evidence.get("screen_state_jump", "none")
    screen_jump = (
        screen_value if screen_value in {"none", "understandable", "unexplained"} else "none"
    )
    screen_known = bool(evidence.get("screen_state_jump_known", "screen_state_jump" in evidence))
    freeze_is_operator_defect = "freeze" in evidence and freeze_value
    freeze_is_unclassified = freeze_value and "freeze" not in evidence
    failures = (
        black_value
        or freeze_is_operator_defect
        or duplicate
        or body_jump == "distracting"
        or screen_jump == "unexplained"
        or not decode_ok
    )
    unknown = (
        not (black_known and freeze_known and duplicate_known and body_known and screen_known)
        or freeze_is_unclassified
    )
    status = "fail" if failures else ("warning" if unknown else "pass")
    return {
        "black_flash": black_value,
        "freeze": freeze_value,
        "duplicate_frame": duplicate,
        "face_or_body_jump": body_jump,
        "screen_state_jump": screen_jump,
        "status": status,
        "context_black_flash": context_black_flash,
        "context_freeze": context_freeze,
        "boundary_black_known": black_known,
        "boundary_freeze_known": freeze_known,
        "_freeze_defect": freeze_is_operator_defect,
    }


def _pacing_check(
    *,
    approved_text: str,
    rendered_text: str,
    kept_before_us: int,
    kept_after_us: int,
    local_cuts_per_minute: float,
    policy: JoinQAPolicy,
) -> dict[str, object]:
    approved_count = len(_tokens(approved_text))
    rendered_count = len(_tokens(rendered_text))
    context_duration_us = max(1, kept_before_us + kept_after_us)
    approved_rate = approved_count / context_duration_us
    rendered_rate = rendered_count / context_duration_us
    if approved_rate <= 0:
        rate_change = 0.0
    else:
        rate_change = ((rendered_rate - approved_rate) / approved_rate) * 100.0
    absolute_change = abs(rate_change)
    if absolute_change > policy.fail_speech_rate_change_percent:
        status = "fail"
    elif (
        absolute_change > policy.warning_speech_rate_change_percent
        or local_cuts_per_minute >= policy.warning_cuts_per_minute
    ):
        status = "warning"
    else:
        status = "pass"
    # The persisted schema deliberately bounds this diagnostic at +500% so a
    # tiny approved fragment cannot make an otherwise valid report invalid.
    # Keep the unbounded value for threshold classification, but clamp only the
    # serialized diagnostic; any value above the failure threshold remains fail.
    serialized_rate_change = max(-100.0, min(500.0, rate_change))
    return {
        "local_cuts_per_minute": round(max(0.0, local_cuts_per_minute), 3),
        "kept_before_us": max(0, kept_before_us),
        "kept_after_us": max(0, kept_after_us),
        "speech_rate_change_percent": round(serialized_rate_change, 3),
        "status": status,
    }


def _semantic_check(
    transcript_check: Mapping[str, object],
    evidence: Mapping[str, object] | None,
) -> dict[str, object]:
    selected = evidence or {}
    protected_removed = bool(selected.get("protected_content_removed", False))
    meaning_changed = bool(selected.get("unintended_meaning_change", False))
    transcript_meaning = str(transcript_check.get("meaning_status", "fail"))
    if protected_removed or meaning_changed or transcript_meaning == "fail":
        meaning_status = "fail"
    elif transcript_meaning == "warning":
        meaning_status = "warning"
    else:
        meaning_status = "pass"
    notes_value = selected.get("notes")
    notes = (
        str(notes_value)
        if notes_value is not None
        else (
            "Transcript comparison is exact and no protected-content defect was supplied."
            if meaning_status == "pass"
            else (
                "ASR transcript differs from the approved excerpt; operator semantic review "
                "is required because the mismatch is not deterministic proof of an edit defect."
                if meaning_status == "warning"
                else "Transcript or protected-content evidence requires semantic review."
            )
        )
    )
    return {
        "protected_content_removed": protected_removed,
        "unintended_meaning_change": meaning_changed,
        "meaning_status": meaning_status,
        "notes": notes,
    }


def _failure_codes(
    transcript_check: Mapping[str, object],
    semantic_check: Mapping[str, object],
    audio_check: Mapping[str, object],
    visual_check: Mapping[str, object],
    pacing_check: Mapping[str, object],
) -> list[str]:
    codes: list[str] = []
    transcript_failed = transcript_check.get("grammar_status") == "fail"
    if transcript_failed:
        for field, code in (
            ("missing_words", "missing_word"),
            ("unexpected_words", "unexpected_word"),
            ("duplicate_words", "duplicate_word"),
        ):
            if transcript_check.get(field):
                codes.append(code)
        codes.append("grammar_change")
    if transcript_check.get("meaning_status") == "fail":
        codes.append("meaning_change")
    if semantic_check.get("meaning_status") == "fail":
        codes.append("semantic_change")
    for field, code in (
        ("click_or_pop", "click_or_pop"),
        ("room_tone_jump", "room_tone_jump"),
    ):
        if audio_check.get(field) is True:
            codes.append(code)
    if audio_check.get("_clipped_defect") is True:
        codes.append("clipped_syllable")
    if audio_check.get("status") == "fail" and not any(
        item in codes for item in ("clipped_syllable", "click_or_pop", "room_tone_jump")
    ):
        codes.append("audio_decode")
    for field, code in (
        ("black_flash", "black_flash"),
        ("duplicate_frame", "duplicate_frame"),
    ):
        if visual_check.get(field) is True:
            codes.append(code)
    if visual_check.get("_freeze_defect") is True:
        codes.append("freeze")
    if visual_check.get("face_or_body_jump") == "distracting":
        codes.append("face_or_body_jump")
    if visual_check.get("screen_state_jump") == "unexplained":
        codes.append("screen_state_jump")
    if visual_check.get("status") == "fail" and not any(
        code in codes
        for code in (
            "black_flash",
            "freeze",
            "duplicate_frame",
            "face_or_body_jump",
            "screen_state_jump",
        )
    ):
        codes.append("visual_decode")
    if pacing_check.get("status") == "fail":
        codes.append("speech_pacing")
    return _unique(codes)


def evaluate_join_qa(
    join: Mapping[str, object],
    joins: Sequence[Mapping[str, object]],
    *,
    output_duration_us: int,
    approved_text: str,
    rendered_text: str,
    audio_check: Mapping[str, object],
    visual_check: Mapping[str, object],
    semantic_evidence: Mapping[str, object] | None = None,
    policy: JoinQAPolicy | None = None,
) -> dict[str, object]:
    """Build one schema-compatible join QA item and route failures to repair."""

    selected_policy = policy or JoinQAPolicy()
    join_id = str(join.get("join_id", "join_unknown"))
    preview_value = join.get("preview_range")
    preview = preview_value if isinstance(preview_value, Mapping) else {}
    output_join_us = _int_value(join.get("output_join_us", 0))
    preview_start = _int_value(
        preview.get("start_us", max(0, output_join_us - selected_policy.preview_handle_us))
    )
    preview_end = _int_value(
        preview.get(
            "end_us", min(output_duration_us, output_join_us + selected_policy.preview_handle_us)
        )
    )
    kept_before_us = max(0, output_join_us - preview_start)
    kept_after_us = max(0, preview_end - output_join_us)
    local_window_start = max(0, output_join_us - 30_000_000)
    local_window_end = min(output_duration_us, output_join_us + 30_000_000)
    local_window_us = max(1, local_window_end - local_window_start)
    nearby_joins = sum(
        1
        for item in joins
        if local_window_start <= _int_value(item.get("output_join_us"), -1) <= local_window_end
    )
    local_cuts_per_minute = nearby_joins * 60_000_000 / local_window_us
    transcript_check = _compare_rendered_transcript_for_qa(approved_text, rendered_text)
    semantic_check = _semantic_check(transcript_check, semantic_evidence)
    pacing_check = _pacing_check(
        approved_text=approved_text,
        rendered_text=rendered_text,
        kept_before_us=kept_before_us,
        kept_after_us=kept_after_us,
        local_cuts_per_minute=local_cuts_per_minute,
        policy=selected_policy,
    )
    try:
        pacing_rate = float(str(pacing_check.get("speech_rate_change_percent", 0.0)))
    except ValueError:
        pacing_rate = 500.0
    extreme_transcript_count_change = len(_tokens(rendered_text)) >= max(
        4, len(_tokens(approved_text)) * 4
    )
    if (
        transcript_check.get("grammar_status") == "warning"
        and pacing_check.get("status") == "fail"
        and abs(pacing_rate) < 300.0
        and not extreme_transcript_count_change
    ):
        pacing_check = dict(pacing_check)
        pacing_check["status"] = "warning"
    failure_codes = _failure_codes(
        transcript_check,
        semantic_check,
        audio_check,
        visual_check,
        pacing_check,
    )
    statuses = [
        str(transcript_check["grammar_status"]),
        str(transcript_check["meaning_status"]),
        str(semantic_check["meaning_status"]),
        str(audio_check["status"]),
        str(visual_check["status"]),
        str(pacing_check["status"]),
    ]
    if failure_codes or "fail" in statuses:
        status = "fail"
    elif "warning" in statuses:
        status = "warning"
    elif _int_value(join.get("repair_attempt", 0)) > 0:
        status = "repaired"
    else:
        status = "pass"
    repair_action: str | None = None
    if failure_codes:
        routed = repair_join_plan(join, failure_codes)
        repair_action = str(routed.get("repair_action"))
    review_required = status != "pass" or bool(join.get("review_required", False))
    warning_count = sum(item == "warning" for item in statuses)
    confidence = max(0.0, min(1.0, 1.0 - 0.18 * len(failure_codes) - 0.08 * warning_count))
    proposal_ids_value = join.get("proposal_ids", [])
    proposal_ids = (
        [str(item) for item in proposal_ids_value] if isinstance(proposal_ids_value, list) else []
    )
    report_visual_check = dict(visual_check)
    report_visual_check.pop("_freeze_defect", None)
    return {
        "join_id": join_id,
        "proposal_ids": proposal_ids,
        "output_join_us": output_join_us,
        "preview_range": {
            "start_us": preview_start,
            "end_us": preview_end,
        },
        "join_strategy": str(join.get("join_strategy", "hard_cut")),
        "transcript_check": transcript_check,
        "semantic_check": semantic_check,
        "audio_check": {
            key: value for key, value in audio_check.items() if key != "_clipped_defect"
        },
        "visual_check": report_visual_check,
        "pacing_check": pacing_check,
        "confidence": round(confidence, 4),
        "status": status,
        "repair_action": repair_action,
        "review_required": review_required,
    }


def _transcript_excerpt(
    transcript: Mapping[str, object],
    start_us: int,
    end_us: int,
    *,
    source_ranges: Sequence[Mapping[str, object]] | None = None,
) -> str:
    words_value = transcript.get("words", [])
    words: list[str] = []
    if isinstance(words_value, list):
        for raw_word in words_value:
            if not isinstance(raw_word, Mapping):
                continue
            try:
                word_start = int(raw_word["start_us"])
                word_end = int(raw_word["end_us"])
            except (KeyError, TypeError, ValueError):
                continue
            # A preview boundary is not evidence that a word was lost.  Only
            # compare words wholly contained in the rendered context; a word
            # crossing the edge is intentionally covered by the visual/audio
            # boundary checks and must not become a false transcript failure.
            if source_ranges is not None:
                in_source_preview = any(
                    _int_value(raw_range.get("start_us"), -1) <= word_start
                    and word_end <= _int_value(raw_range.get("end_us"), -1)
                    for raw_range in source_ranges
                )
            else:
                in_source_preview = word_start >= start_us and word_end <= end_us
            if in_source_preview:
                words.append(str(raw_word.get("text", "")).strip())
    excerpt = " ".join(word for word in words if word)
    if excerpt:
        return excerpt
    value = transcript.get("text")
    return str(value) if isinstance(value, str) else ""


def _rendered_transcript_text(
    transcript: Mapping[str, object],
    *,
    duration_us: int,
    approved_text: str,
) -> str:
    """Trim only likely clipped boundary words from a preview transcription."""

    words_value = transcript.get("words", [])
    entries: list[tuple[str, int, int]] = []
    if isinstance(words_value, list):
        for raw_word in words_value:
            if not isinstance(raw_word, Mapping):
                continue
            try:
                word_start = int(raw_word["start_us"])
                word_end = int(raw_word["end_us"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(raw_word.get("text", "")).strip()
            if text and word_end > word_start:
                entries.append((text, word_start, word_end))
    if not entries:
        value = transcript.get("text")
        return str(value) if isinstance(value, str) else ""

    approved_tokens = _tokens(approved_text)
    while entries and approved_tokens:
        first_text, first_start, _ = entries[0]
        first_tokens = _tokens(first_text)
        if first_start > _TRANSCRIPT_BOUNDARY_GUARD_US or (
            first_tokens and first_tokens[0] == approved_tokens[0]
        ):
            break
        entries.pop(0)
    while entries and approved_tokens:
        last_text, _, last_end = entries[-1]
        last_tokens = _tokens(last_text)
        if last_end < max(0, duration_us - _TRANSCRIPT_BOUNDARY_GUARD_US) or (
            last_tokens and last_tokens[-1] == approved_tokens[-1]
        ):
            break
        entries.pop()
    return " ".join(text for text, _, _ in entries)


def _parse_black_frames(log: str) -> bool:
    return any(float(value) >= 98.0 for value in _BLACK_FRAME_PATTERN.findall(log))


def _parse_freeze_frames(log: str) -> bool:
    return bool(_FREEZE_PATTERN.search(log))


def _nested_evidence(
    evidence: Mapping[str, Mapping[str, object]] | None,
    join_id: str,
    category: str,
) -> Mapping[str, object]:
    if evidence is None:
        return {}
    selected = evidence.get(join_id, {})
    nested = selected.get(category)
    if isinstance(nested, Mapping):
        return nested
    return selected


def _preview_file_ref(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _safe_staging_directory(layout: ProjectLayout, value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value).expanduser().resolve()
    staging_root = layout.staging.resolve()
    try:
        candidate.relative_to(staging_root)
    except ValueError:
        return None
    if candidate == staging_root or candidate.parent != staging_root:
        return None
    return candidate


def _valid_file_ref(layout: ProjectLayout, value: object) -> Path | None:
    if not isinstance(value, Mapping):
        return None
    path_value = value.get("path")
    sha_value = value.get("sha256")
    size_value = value.get("size_bytes")
    if (
        not isinstance(path_value, str)
        or not isinstance(sha_value, str)
        or not isinstance(size_value, int)
        or isinstance(size_value, bool)
    ):
        return None
    path = Path(path_value).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        if sha256_file(path) != sha_value or path.stat().st_size != size_value:
            return None
    except OSError:
        return None
    return path


def _load_join_qa_progress(
    package_root: Path,
    layout: ProjectLayout,
    progress_path: Path,
    *,
    project_id: str,
    revision_id: str,
    stage_key: str,
    expected_join_ids: Sequence[str],
) -> tuple[dict[int, dict[str, object]], dict[str, object] | None]:
    if not progress_path.is_file():
        return {}, None
    try:
        payload_value = json.loads(progress_path.read_text(encoding="utf-8"))
        if not isinstance(payload_value, dict):
            return {}, None
        validate_artifact(package_root, "join_qa_progress", payload_value)
    except (OSError, ValueError, json.JSONDecodeError):
        # A corrupt checkpoint is evidence to preserve, but never a reason to
        # consume unverified join results. Recompute from the next safe point.
        return {}, None
    if (
        payload_value.get("project_id") != project_id
        or payload_value.get("revision_id") != revision_id
        or payload_value.get("stage_key") != stage_key
        or payload_value.get("join_ids") != list(expected_join_ids)
    ):
        return {}, None
    completed_value = payload_value.get("completed", [])
    if not isinstance(completed_value, list):
        return {}, None
    completed: dict[int, dict[str, object]] = {}
    for raw_item in completed_value:
        if not isinstance(raw_item, Mapping):
            continue
        index = _int_value(raw_item.get("index"), 0)
        join_id = raw_item.get("join_id")
        item = raw_item.get("item")
        if (
            index < 1
            or index > len(expected_join_ids)
            or join_id != expected_join_ids[index - 1]
            or not isinstance(item, dict)
            or not isinstance(item.get("preview"), Mapping)
        ):
            continue
        pacing_value = item.get("pacing_check")
        if isinstance(pacing_value, Mapping):
            raw_serialized_rate = pacing_value.get("speech_rate_change_percent")
            try:
                serialized_rate = (
                    float(raw_serialized_rate)
                    if isinstance(raw_serialized_rate, (int, float, str))
                    else None
                )
            except (TypeError, ValueError):
                continue
            if serialized_rate is None or not -100.0 <= serialized_rate <= 500.0:
                # Do not consume a checkpoint item that could not satisfy the
                # current persisted join-report schema; the preview and audio
                # proxy remain available for a targeted recomputation.
                continue
        preview_value = item.get("preview")
        preview_file = preview_value.get("file") if isinstance(preview_value, Mapping) else None
        if _valid_file_ref(layout, preview_file) is None:
            continue
        completed[index] = {
            "index": index,
            "join_id": str(join_id),
            "item": item,
        }
    provenance_value = payload_value.get("transcription_provenance")
    provenance = dict(provenance_value) if isinstance(provenance_value, Mapping) else None
    return completed, provenance


def _write_join_qa_progress(
    package_root: Path,
    progress_path: Path,
    *,
    project_id: str,
    revision_id: str,
    stage_key: str,
    expected_join_ids: Sequence[str],
    completed: Mapping[int, Mapping[str, object]],
    transcription_provenance: Mapping[str, object] | None,
) -> None:
    payload: dict[str, Any] = {
        "schema_name": "join_qa_progress",
        "schema_version": "1.0.0",
        "project_id": project_id,
        "revision_id": revision_id,
        "stage_key": stage_key,
        "join_ids": list(expected_join_ids),
        "completed": [completed[index] for index in sorted(completed)],
    }
    if transcription_provenance is not None:
        payload["transcription_provenance"] = dict(transcription_provenance)
    write_validated_artifact(package_root, "join_qa_progress", progress_path, payload)


def _cached_report(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, object] | None,
    expected_join_ids: Sequence[str],
) -> Path | None:
    if state is None or state.get("status") != "complete":
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    report_value = artifacts.get("report")
    if (
        not isinstance(report_value, Mapping)
        or not isinstance(report_value.get("path"), str)
        or not isinstance(report_value.get("sha256"), str)
        or not isinstance(report_value.get("size_bytes"), int)
        or isinstance(report_value.get("size_bytes"), bool)
    ):
        return None
    report_path = Path(str(report_value["path"])).resolve()
    try:
        report_path.relative_to(layout.root.resolve())
    except ValueError:
        return None
    if not report_path.is_file():
        return None
    try:
        if (
            sha256_file(report_path) != report_value["sha256"]
            or report_path.stat().st_size != report_value["size_bytes"]
        ):
            return None
    except OSError:
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            return None
        validate_artifact(package_root, "join_qa_report", report)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    joins = report.get("joins")
    if not isinstance(joins, list):
        return None
    actual_join_ids: list[str] = []
    for join in joins:
        if not isinstance(join, Mapping):
            return None
        join_id = join.get("join_id")
        if not isinstance(join_id, str):
            return None
        actual_join_ids.append(join_id)
        preview = join.get("preview")
        if not isinstance(preview, Mapping):
            return None
        file_value = preview.get("file")
        if (
            not isinstance(file_value, Mapping)
            or not isinstance(file_value.get("path"), str)
            or not isinstance(file_value.get("sha256"), str)
            or not isinstance(file_value.get("size_bytes"), int)
            or isinstance(file_value.get("size_bytes"), bool)
        ):
            return None
        preview_path = Path(str(file_value["path"])).resolve()
        try:
            preview_path.relative_to(layout.root.resolve())
        except ValueError:
            return None
        if not preview_path.is_file():
            return None
        try:
            if (
                sha256_file(preview_path) != file_value["sha256"]
                or preview_path.stat().st_size != file_value["size_bytes"]
            ):
                return None
        except OSError:
            return None
    if actual_join_ids != list(expected_join_ids):
        return None
    summary = report.get("summary")
    if not isinstance(summary, Mapping) or summary.get("total_joins") != len(expected_join_ids):
        return None
    return report_path


def qa_rendered_joins(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    join_plan_path: Path,
    transcript_path: Path,
    *,
    transcriber: TranscriptionAdapter | None = None,
    model_name: str = "small",
    adapter: FFmpegAdapter | None = None,
    media_evidence: Mapping[str, Mapping[str, object]] | None = None,
    revision_id: str = "rev_001",
    policy: JoinQAPolicy | None = None,
    transcript_clock: TranscriptClock = "output",
) -> Path:
    """Render and QA every applied join in a hash-bound, resumable stage."""

    import json

    selected_policy = policy or JoinQAPolicy()
    if transcript_clock not in {"output", "source"}:
        raise PlanningValidationError("transcript_clock must be either 'output' or 'source'")
    manifest_path = render_manifest_path.expanduser().resolve()
    plan_path = join_plan_path.expanduser().resolve()
    selected_transcript_path = transcript_path.expanduser().resolve()
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_value = json.loads(plan_path.read_text(encoding="utf-8"))
    transcript_value = json.loads(selected_transcript_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_value, dict):
        raise PlanningValidationError("render manifest must be a JSON object")
    if not isinstance(plan_value, dict):
        raise PlanningValidationError("join plan must be a JSON object")
    if not isinstance(transcript_value, dict):
        raise PlanningValidationError("transcript must be a JSON object")
    validate_artifact(package_root, "render_manifest", manifest_value)
    validate_artifact(package_root, "join_plan", plan_value)
    validate_artifact(package_root, "transcript", transcript_value)
    output_value = manifest_value.get("output")
    if not isinstance(output_value, Mapping) or not isinstance(output_value.get("path"), str):
        raise PlanningValidationError("render manifest output path is missing")
    rendered_media = Path(str(output_value["path"])).expanduser().resolve()
    if not rendered_media.is_file():
        raise FileNotFoundError(rendered_media)
    joins_value = plan_value.get("joins", [])
    if not isinstance(joins_value, list) or any(
        not isinstance(item, Mapping) for item in joins_value
    ):
        raise PlanningValidationError("join plan joins must be an array of objects")
    joins = [item for item in joins_value if isinstance(item, Mapping)]
    output_duration_us = int(plan_value.get("output_duration_us", 0))
    if output_duration_us <= 0:
        raise PlanningValidationError("join plan output duration must be positive")
    manifest_duration_us = 0
    for duration_key in ("actual_duration_us", "expected_duration_us"):
        candidate_duration = _int_value(manifest_value.get(duration_key), 0)
        if candidate_duration > 0:
            manifest_duration_us = candidate_duration
            break
    if manifest_duration_us <= 0:
        raise PlanningValidationError("render manifest duration is required for join QA")
    if abs(output_duration_us - manifest_duration_us) > 100_000:
        raise PlanningValidationError(
            "join plan output duration does not match the rendered media duration; "
            "rebase the join plan through the retimed timeline before QA"
        )
    selected_adapter = adapter or FFmpegAdapter()
    ffmpeg_version = selected_adapter.version()
    selected_transcriber = transcriber or WhisperAdapter()
    transcriber_version = str(selected_transcriber.adapter_version)
    declared_model_identity = _declared_transcriber_model_identity(selected_transcriber)
    stage_key = make_stage_key(
        "join_qa",
        JOIN_QA_IMPLEMENTATION_VERSION,
        [
            sha256_file(manifest_path),
            sha256_file(plan_path),
            sha256_file(selected_transcript_path),
        ],
        {
            "revision_id": revision_id,
            "model_name": model_name,
            "ffmpeg_version": ffmpeg_version,
            "transcriber_id": str(selected_transcriber.adapter_id),
            "transcriber_version": transcriber_version,
            "model_identity": declared_model_identity,
            "preview_audio_codec": "pcm_f32le",
            "media_evidence_sha256": canonical_sha256(media_evidence or {}),
            "policy": {
                "preview_handle_us": selected_policy.preview_handle_us,
                "warning_speech_rate_change_percent": (
                    selected_policy.warning_speech_rate_change_percent
                ),
                "fail_speech_rate_change_percent": (
                    selected_policy.fail_speech_rate_change_percent
                ),
                "warning_cuts_per_minute": selected_policy.warning_cuts_per_minute,
                "freeze_duration_us": selected_policy.freeze_duration_us,
                "boundary_check_window_us": selected_policy.boundary_check_window_us,
            },
            "transcript_clock": transcript_clock,
        },
    )
    with ProjectLock(layout, stage="join_qa", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "join_qa", revision_id)
        expected_join_ids = [
            str(join.get("join_id", f"join_{index:06d}"))
            for index, join in enumerate(joins, start=1)
        ]
        if previous is not None and previous.get("stage_key") == stage_key:
            cached = _cached_report(package_root, layout, previous, expected_join_ids)
            if cached is not None:
                return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = None
        if (
            previous is not None
            and previous.get("stage_key") == stage_key
            and previous.get("status") in {"running", "failed"}
        ):
            staging_paths = previous.get("staging_paths")
            if isinstance(staging_paths, list) and staging_paths:
                stage_dir = _safe_staging_directory(layout, staging_paths[0])
        if stage_dir is None:
            stage_dir = layout.staging / f"join-qa-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="join_qa",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        preview_dir = layout.review / "join-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        report_path = layout.artifacts / "join-qa-report.json"
        progress_path = stage_dir / "join-qa-progress.json"
        completed_by_index, transcription_provenance = _load_join_qa_progress(
            package_root,
            layout,
            progress_path,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage_key=stage_key,
            expected_join_ids=expected_join_ids,
        )
        report_joins: list[dict[str, object]] = []
        preview_paths: list[Path] = []
        try:
            for index, join in enumerate(joins, start=1):
                join_id = str(join.get("join_id", f"join_{index:06d}"))
                completed = completed_by_index.get(index)
                if completed is not None:
                    completed_item = completed.get("item")
                    if isinstance(completed_item, dict):
                        completed_preview = completed_item.get("preview")
                        completed_file = (
                            completed_preview.get("file")
                            if isinstance(completed_preview, Mapping)
                            else None
                        )
                        completed_path = _valid_file_ref(layout, completed_file)
                        if completed_path is not None:
                            report_joins.append(completed_item)
                            preview_paths.append(completed_path)
                            continue
                preview_range_value = join.get("preview_range")
                if not isinstance(preview_range_value, Mapping):
                    raise PlanningValidationError(f"{join_id} has no preview range")
                start_us = int(preview_range_value.get("start_us", -1))
                end_us = int(preview_range_value.get("end_us", -1))
                if start_us < 0 or end_us <= start_us or end_us > output_duration_us:
                    raise PlanningValidationError(f"{join_id} preview range is out of bounds")
                staged_preview = stage_dir / f"{join_id}.part.mp4"
                final_preview = preview_dir / f"{join_id}-{stage_key[:16]}.mp4"
                expected_preview_duration = end_us - start_us
                reused_preview = False
                if final_preview.is_file():
                    try:
                        existing_probe = selected_adapter.probe(final_preview)
                        existing_duration_us = seconds_to_us(
                            existing_probe.get("format", {}).get("duration")
                            if isinstance(existing_probe.get("format"), Mapping)
                            else None
                        )
                        reused_preview = (
                            existing_duration_us is not None
                            and abs(existing_duration_us - expected_preview_duration) <= 100_000
                        )
                    except (OSError, RuntimeError, VideoeditError, ValueError):
                        reused_preview = False
                if not reused_preview:
                    selected_adapter.render_keep_ranges(
                        rendered_media,
                        [(start_us, end_us)],
                        staged_preview,
                        audio_codec="pcm_f32le",
                    )
                    os.replace(staged_preview, final_preview)
                full_decode = selected_adapter.full_decode_check(final_preview)
                preview_probe = selected_adapter.probe(final_preview)
                actual_duration_us = seconds_to_us(
                    preview_probe.get("format", {}).get("duration")
                    if isinstance(preview_probe.get("format"), Mapping)
                    else None
                )
                if actual_duration_us is None:
                    raise PlanningValidationError(f"{join_id} preview has no duration")
                expected_preview_duration = end_us - start_us
                if abs(actual_duration_us - expected_preview_duration) > 100_000:
                    raise PlanningValidationError(
                        f"{join_id} preview duration differs from requested range"
                    )
                preview_paths.append(final_preview)

                output_join_us = _int_value(join.get("output_join_us"), -1)
                if output_join_us < start_us or output_join_us > end_us:
                    raise PlanningValidationError(
                        f"{join_id} output join is outside its preview range"
                    )
                boundary_start_us = max(
                    start_us,
                    output_join_us - selected_policy.boundary_check_window_us,
                )
                boundary_end_us = min(
                    end_us,
                    output_join_us + selected_policy.boundary_check_window_us,
                )
                if boundary_end_us <= boundary_start_us:
                    raise PlanningValidationError(f"{join_id} boundary window is empty")
                diagnostic_window = {
                    "start_us": boundary_start_us,
                    "end_us": boundary_end_us,
                }
                boundary_relative_start_us = boundary_start_us - start_us
                boundary_relative_end_us = boundary_end_us - start_us

                audio_evidence = dict(_nested_evidence(media_evidence, join_id, "audio"))
                try:
                    clipping_result = selected_adapter.measure_clipping(final_preview)
                    context_clipped_samples: int | None = parse_clipped_samples(clipping_result)
                    context_clipping_known = True
                except VideoeditError:
                    context_clipped_samples = None
                    context_clipping_known = False

                visual_evidence = dict(_nested_evidence(media_evidence, join_id, "visual"))
                try:
                    black_result = selected_adapter.detect_black_frames(final_preview)
                    context_black_flash: bool | None = _parse_black_frames(black_result.stderr)
                    context_black_known = True
                except VideoeditError:
                    context_black_flash = None
                    context_black_known = False
                try:
                    freeze_result = selected_adapter.detect_freeze_frames(
                        final_preview,
                        minimum_duration_us=selected_policy.freeze_duration_us,
                    )
                    context_freeze: bool | None = _parse_freeze_frames(freeze_result.stderr)
                    context_freeze_known = True
                except VideoeditError:
                    context_freeze = None
                    context_freeze_known = False

                boundary_needed = (
                    not context_clipping_known
                    or (context_clipped_samples or 0) > 0
                    or not context_black_known
                    or context_black_flash is True
                    or not context_freeze_known
                    or context_freeze is True
                )
                boundary_clipped_samples = 0
                boundary_clipping_known = not boundary_needed
                boundary_black_flash = False
                boundary_black_known = not boundary_needed
                boundary_freeze = False
                boundary_freeze_known = not boundary_needed
                if boundary_needed:
                    try:
                        boundary_clipping_result = selected_adapter.measure_clipping(
                            final_preview,
                            start_us=boundary_relative_start_us,
                            end_us=boundary_relative_end_us,
                        )
                        boundary_clipped_samples = parse_clipped_samples(boundary_clipping_result)
                        boundary_clipping_known = True
                    except VideoeditError:
                        boundary_clipping_known = False
                    try:
                        boundary_black_result = selected_adapter.detect_black_frames(
                            final_preview,
                            start_us=boundary_relative_start_us,
                            end_us=boundary_relative_end_us,
                        )
                        boundary_black_flash = _parse_black_frames(boundary_black_result.stderr)
                        boundary_black_known = True
                    except VideoeditError:
                        boundary_black_known = False
                    try:
                        boundary_freeze_result = selected_adapter.detect_freeze_frames(
                            final_preview,
                            minimum_duration_us=selected_policy.freeze_duration_us,
                            start_us=boundary_relative_start_us,
                            end_us=boundary_relative_end_us,
                        )
                        boundary_freeze = _parse_freeze_frames(boundary_freeze_result.stderr)
                        boundary_freeze_known = True
                    except VideoeditError:
                        boundary_freeze_known = False

                audio_check = _audio_check(
                    audio_evidence,
                    clipping_known=boundary_clipping_known,
                    clipped_samples=boundary_clipped_samples,
                    decode_ok=full_decode.exit_code == 0,
                    context_clipped_samples=context_clipped_samples,
                )
                visual_check = _visual_check(
                    visual_evidence,
                    black_flash=boundary_black_flash,
                    black_known=boundary_black_known,
                    freeze=boundary_freeze,
                    freeze_known=boundary_freeze_known,
                    decode_ok=full_decode.exit_code == 0,
                    context_black_flash=context_black_flash,
                    context_freeze=context_freeze,
                )

                speech_proxy = stage_dir / f"{join_id}.wav"
                if not speech_proxy.is_file() or speech_proxy.stat().st_size <= 44:
                    staged_speech_proxy = stage_dir / f"{join_id}.part.wav"
                    selected_adapter.create_speech_proxy(final_preview, staged_speech_proxy)
                    os.replace(staged_speech_proxy, speech_proxy)
                transcription_result: TranscriptionResult = selected_transcriber.transcribe(
                    speech_proxy,
                    model_name,
                )
                model_sha256 = transcription_result.model_sha256
                if (
                    model_sha256 is not None
                    and declared_model_identity not in {"none", "unavailable"}
                    and model_sha256 != declared_model_identity
                ):
                    raise TranscriptionOutputError(
                        "join QA transcription model hash differs from the declared identity"
                    )
                if model_sha256 is None and declared_model_identity not in {
                    "none",
                    "unavailable",
                }:
                    model_sha256 = declared_model_identity
                if transcription_provenance is None:
                    transcription_provenance = {
                        "adapter_id": transcription_result.adapter_id,
                        "adapter_version": transcription_result.adapter_version,
                        "model_identifier": transcription_result.model_identifier,
                        "device": transcription_result.device,
                    }
                    if model_sha256 is not None:
                        transcription_provenance["model_sha256"] = model_sha256
                elif (
                    transcription_provenance.get("adapter_id") != transcription_result.adapter_id
                    or transcription_provenance.get("adapter_version")
                    != transcription_result.adapter_version
                    or transcription_provenance.get("model_identifier")
                    != transcription_result.model_identifier
                    or transcription_provenance.get("device") != transcription_result.device
                    or transcription_provenance.get("model_sha256") != model_sha256
                ):
                    raise TranscriptionOutputError(
                        "join QA transcription adapter returned inconsistent model provenance"
                    )
                normalized = normalize_whisper_result(
                    transcription_result.raw_result,
                    project_id=layout.root.name,
                    revision_id=revision_id,
                    duration_us=expected_preview_duration,
                    model_name=model_name,
                    source_input=artifact_input("art_join_preview", final_preview),
                    config_hash=config_sha256(layout),
                    model_identifier=transcription_result.model_identifier,
                    device=transcription_result.device,
                    adapter_id=transcription_result.adapter_id,
                    adapter_version=transcription_result.adapter_version,
                    model_sha256=model_sha256,
                )
                source_preview_value = join.get("source_preview_ranges")
                source_preview_ranges = (
                    [item for item in source_preview_value if isinstance(item, Mapping)]
                    if isinstance(source_preview_value, list)
                    else None
                )
                if transcript_clock == "output":
                    source_preview_ranges = None
                approved_text = _transcript_excerpt(
                    transcript_value,
                    start_us,
                    end_us,
                    source_ranges=source_preview_ranges,
                )
                rendered_text = _rendered_transcript_text(
                    normalized,
                    duration_us=expected_preview_duration,
                    approved_text=approved_text,
                )
                semantic_evidence = _nested_evidence(media_evidence, join_id, "semantic")
                item = evaluate_join_qa(
                    join,
                    joins,
                    output_duration_us=output_duration_us,
                    approved_text=approved_text,
                    rendered_text=rendered_text,
                    audio_check=audio_check,
                    visual_check=visual_check,
                    semantic_evidence=semantic_evidence,
                    policy=selected_policy,
                )
                item["diagnostic_window"] = diagnostic_window
                item["preview"] = {
                    "file": _preview_file_ref(final_preview),
                    "duration_us": actual_duration_us,
                    "full_decode_status": "pass" if full_decode.exit_code == 0 else "fail",
                }
                report_joins.append(item)
                completed_by_index[index] = {
                    "index": index,
                    "join_id": join_id,
                    "item": item,
                }
                _write_join_qa_progress(
                    package_root,
                    progress_path,
                    project_id=layout.root.name,
                    revision_id=revision_id,
                    stage_key=stage_key,
                    expected_join_ids=expected_join_ids,
                    completed=completed_by_index,
                    transcription_provenance=transcription_provenance,
                )

            count = len(report_joins)
            passed = sum(item["status"] == "pass" for item in report_joins)
            warnings = sum(item["status"] == "warning" for item in report_joins)
            failed = sum(item["status"] == "fail" for item in report_joins)
            repaired = sum(item["status"] == "repaired" for item in report_joins)
            cuts_per_minute = count * 60_000_000 / output_duration_us
            fragments = []
            for item in report_joins:
                pacing_value = item.get("pacing_check")
                if isinstance(pacing_value, Mapping):
                    fragments.append(
                        (
                            _int_value(pacing_value.get("kept_before_us"))
                            + _int_value(pacing_value.get("kept_after_us"))
                        )
                        / 2
                    )
            average_fragment = (
                round(sum(fragments) / len(fragments)) if fragments else output_duration_us
            )
            overall_status = "fail" if failed else ("warning" if warnings else "pass")
            payload: dict[str, Any] = {
                "schema_name": "join_qa_report",
                "schema_version": "1.0.0",
                "artifact_id": "art_join_qa",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "created_at": now_iso(),
                "producer": producer(
                    "join-qa",
                    f"ffmpeg+{selected_transcriber.adapter_id}",
                    f"{ffmpeg_version}+{transcriber_version}",
                ),
                "inputs": [
                    artifact_input(str(manifest_value["artifact_id"]), manifest_path),
                    artifact_input(str(plan_value["artifact_id"]), plan_path),
                    artifact_input(str(transcript_value["artifact_id"]), selected_transcript_path),
                ],
                "config_sha256": config_sha256(layout),
                "render_artifact_id": str(manifest_value["artifact_id"]),
                "summary": {
                    "total_joins": count,
                    "passed": passed,
                    "warnings": warnings,
                    "failed": failed,
                    "repaired": repaired,
                    "cuts_per_minute": round(cuts_per_minute, 3),
                    "average_kept_fragment_us": average_fragment,
                },
                "joins": report_joins,
                "overall_status": overall_status,
            }
            if transcription_provenance is not None:
                payload["transcription_provenance"] = transcription_provenance
            write_validated_artifact(package_root, "join_qa_report", report_path, payload)
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={
                    "report": report_path,
                    "progress": progress_path,
                    **{
                        f"preview_{index:06d}": path
                        for index, path in enumerate(preview_paths, start=1)
                    },
                },
                warnings=(
                    [f"join_qa_overall_status:{overall_status}"] if overall_status != "pass" else []
                ),
            )
            return report_path
        except VideoeditError as exc:
            fail_stage(package_root, layout, state, code=exc.code, message=exc.message)
            raise
        except Exception as exc:
            message = str(exc)[-1000:] or exc.__class__.__name__
            fail_stage(package_root, layout, state, code="join_qa_failed", message=message)
            raise


__all__ = [
    "JOIN_QA_IMPLEMENTATION_VERSION",
    "JoinQAPolicy",
    "compare_transcript_text",
    "evaluate_join_qa",
    "qa_rendered_joins",
]
