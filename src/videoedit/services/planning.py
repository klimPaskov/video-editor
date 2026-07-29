from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import (
    ApprovalRequiredError,
    PlanningValidationError,
    StaleApprovalError,
)
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
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
    source_from_manifest,
    validate_transcript_timing,
)

SUPPORTED_EFFECT_KINDS = {
    "cut",
    "caption",
    "motion_graphic",
    "sound_effect",
    "broll",
    "picture_in_picture",
    "screen_focus",
}
SUPPORTED_RENDERERS = {"ffmpeg", "remotion", "provider"}
NEGATION_WORDS = {
    "no",
    "not",
    "never",
    "nothing",
    "neither",
    "nor",
    "without",
    "cannot",
    "can't",
    "dont",
    "don't",
    "didnt",
    "didn't",
    "isnt",
    "isn't",
    "wont",
    "won't",
}
CTA_WORDS = {
    "buy",
    "call",
    "click",
    "contact",
    "download",
    "follow",
    "join",
    "register",
    "subscribe",
    "sign",
    "visit",
}
HOUSEKEEPING_WORDS = {
    "camera",
    "microphone",
    "mic",
    "recording",
    "technical",
    "audio",
    "lighting",
}
FILLER_WORDS = {"um", "uh", "er", "erm"}
FILLER_PHRASES = {
    ("you", "know"),
    ("i", "mean"),
    ("kind", "of"),
    ("sort", "of"),
    ("you", "know", "like"),
}
CORRECTION_MARKERS = {
    ("actually",),
    ("rather",),
    ("sorry",),
    ("i", "mean"),
    ("let", "me", "rephrase"),
}
ABANDONED_TRAILING_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "because",
    "but",
    "for",
    "if",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "was",
    "were",
    "with",
}
SEMANTIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "so",
    "that",
    "the",
    "this",
    "to",
    "was",
    "we",
    "with",
}
NUMBER_PATTERN = re.compile(r"(?:\d|[$€£]|%|\b(?:am|pm)\b)", re.IGNORECASE)

AUTO_CONFIDENCE_THRESHOLDS = {
    "filler_word": 0.95,
    "filler_phrase": 0.96,
    "stutter": 0.96,
    "false_start": 0.95,
    "abandoned_phrase": 0.97,
    "self_correction": 1.01,
    "exact_repetition": 0.96,
    "near_repetition": 1.01,
    "semantic_repetition": 1.01,
    "duplicate_take": 1.01,
    "weak_take": 1.01,
    "leading_silence": 0.97,
    "trailing_silence": 0.97,
    "long_pause": 0.97,
    "dead_air": 0.98,
    "accidental_noise": 0.95,
    "housekeeping": 1.01,
}
REVIEW_CONFIDENCE_THRESHOLDS = {
    "filler_word": 0.78,
    "filler_phrase": 0.82,
    "stutter": 0.82,
    "false_start": 0.82,
    "abandoned_phrase": 0.85,
    "self_correction": 0.82,
    "exact_repetition": 0.82,
    "near_repetition": 0.0,
    "semantic_repetition": 0.0,
    "duplicate_take": 0.0,
    "weak_take": 0.0,
    "leading_silence": 0.82,
    "trailing_silence": 0.82,
    "long_pause": 0.82,
    "dead_air": 0.85,
    "accidental_noise": 0.80,
    "housekeeping": 0.0,
}


@dataclass(frozen=True, slots=True)
class EditingPolicy:
    policy_id: str = "pol_conservative"
    policy_version: int = 1
    minimum_silence_us: int = 650_000
    pre_handle_us: int = 120_000
    post_handle_us: int = 160_000
    minimum_cut_us: int = 180_000
    minimum_kept_segment_us: int = 500_000
    protected_padding_us: int = 200_000
    low_confidence_threshold: float = 0.6
    profile: str = "conservative"
    dead_air_threshold_us: int = 2_000_000
    stutter_max_gap_us: int = 35_000

    @classmethod
    def smart_dense(cls) -> EditingPolicy:
        return cls(
            policy_id="pol_smart_dense",
            policy_version=3,
            minimum_silence_us=420_000,
            pre_handle_us=80_000,
            post_handle_us=110_000,
            minimum_cut_us=90_000,
            minimum_kept_segment_us=360_000,
            protected_padding_us=120_000,
            low_confidence_threshold=0.72,
            profile="smart_dense",
            dead_air_threshold_us=1_200_000,
            stutter_max_gap_us=35_000,
        )

    def __post_init__(self) -> None:
        for name in (
            "minimum_silence_us",
            "pre_handle_us",
            "post_handle_us",
            "minimum_cut_us",
            "minimum_kept_segment_us",
            "protected_padding_us",
            "dead_air_threshold_us",
            "stutter_max_gap_us",
        ):
            if getattr(self, name) < 0:
                raise PlanningValidationError(f"editing policy {name} must be nonnegative")
        if not 0 <= self.low_confidence_threshold <= 1:
            raise PlanningValidationError("editing policy confidence threshold must be in [0, 1]")
        if self.profile not in {"conservative", "smart_dense"}:
            raise PlanningValidationError(
                "editing policy profile must be conservative or smart_dense"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "minimum_silence_us": self.minimum_silence_us,
            "pre_handle_us": self.pre_handle_us,
            "post_handle_us": self.post_handle_us,
            "minimum_cut_us": self.minimum_cut_us,
            "minimum_kept_segment_us": self.minimum_kept_segment_us,
            "protected_padding_us": self.protected_padding_us,
            "low_confidence_threshold": self.low_confidence_threshold,
            "profile": self.profile,
            "dead_air_threshold_us": self.dead_air_threshold_us,
            "stutter_max_gap_us": self.stutter_max_gap_us,
            "auto_confidence_thresholds": dict(sorted(AUTO_CONFIDENCE_THRESHOLDS.items())),
            "review_confidence_thresholds": dict(sorted(REVIEW_CONFIDENCE_THRESHOLDS.items())),
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PlanOutputs:
    proposals_path: Path
    effect_plan_path: Path
    decision_template_path: Path
    markdown_path: Path
    stage_key: str


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be a JSON object: {path}")
    return value


def _owned_path(layout: ProjectLayout, value: object, description: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PlanningValidationError(f"{description} path is missing")
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} escapes the project: {path}") from exc
    return path


def _stage_ref_valid(layout: ProjectLayout, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        path = _owned_path(layout, value.get("path"), "stage artifact")
    except PlanningValidationError:
        return False
    return (
        path.is_file()
        and path.stat().st_size == int(value.get("size_bytes", -1))
        and sha256_file(path) == value.get("sha256")
    )


def _load_transcript(package_root: Path, layout: ProjectLayout) -> tuple[Path, dict[str, Any]]:
    path = layout.artifacts / "transcript.json"
    if not path.is_file():
        raise PlanningValidationError("transcript.json is missing; run transcription first")
    value = _read_object(path, "transcript")
    validate_artifact(package_root, "transcript", value)
    validate_transcript_timing(value)
    return path, value


def _load_silence(package_root: Path, layout: ProjectLayout) -> tuple[Path, dict[str, Any]]:
    path = layout.artifacts / "silence-intervals.json"
    if not path.is_file():
        raise PlanningValidationError(
            "silence-intervals.json is missing; run silence detection first"
        )
    value = _read_object(path, "silence intervals")
    validate_artifact(package_root, "silence_intervals", value)
    return path, value


def _time_range(
    start_us: object, end_us: object, duration_us: int, description: str
) -> dict[str, int]:
    if not isinstance(start_us, int) or not isinstance(end_us, int):
        raise PlanningValidationError(f"{description} must use integer microseconds")
    if start_us < 0 or end_us <= start_us or end_us > duration_us:
        raise PlanningValidationError(f"{description} is outside source bounds")
    return {"start_us": start_us, "end_us": end_us}


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start_us, end_us in sorted(ranges):
        if end_us <= start_us:
            continue
        if not merged or start_us > merged[-1][1]:
            merged.append((start_us, end_us))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_us))
    return merged


def _protected_range_overrides(
    decisions: Mapping[str, Any], duration_us: int
) -> list[tuple[int, int]]:
    """Validate explicit operator overrides before applying them to protected ranges."""

    raw_overrides = decisions.get("protected_range_overrides", [])
    if raw_overrides is None:
        return []
    if not isinstance(raw_overrides, list):
        raise PlanningValidationError("protected_range_overrides must be an array")
    overrides: list[tuple[int, int]] = []
    for index, raw in enumerate(raw_overrides, start=1):
        if not isinstance(raw, Mapping):
            raise PlanningValidationError(f"protected range override {index} must be an object")
        if not str(raw.get("reason", "")).strip():
            raise PlanningValidationError(
                f"protected range override {index} requires an operator reason"
            )
        value = _time_range(
            raw.get("start_us"),
            raw.get("end_us"),
            duration_us,
            f"protected range override {index}",
        )
        overrides.append((value["start_us"], value["end_us"]))
    return _merge_ranges(overrides)


def _remove_protected_overrides(
    protected: Sequence[tuple[int, int]],
    overrides: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Subtract explicit operator ranges from the protected-content guard."""

    remaining = list(protected)
    for override_start, override_end in overrides:
        next_ranges: list[tuple[int, int]] = []
        for protected_start, protected_end in remaining:
            if override_end <= protected_start or override_start >= protected_end:
                next_ranges.append((protected_start, protected_end))
                continue
            if protected_start < override_start:
                next_ranges.append((protected_start, override_start))
            if override_end < protected_end:
                next_ranges.append((override_end, protected_end))
        remaining = next_ranges
    return _merge_ranges(remaining)


def _word_token(value: object) -> str:
    text = str(value or "").casefold()
    return re.sub(r"(^[^\w']+|[^\w']+$)", "", text)


def _word_list(transcript: Mapping[str, Any]) -> list[dict[str, Any]]:
    words = transcript.get("words")
    if not isinstance(words, list):
        raise PlanningValidationError("transcript words must be an array")
    return [word for word in words if isinstance(word, dict)]


def _range_overlaps(start_us: int, end_us: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start_us < range_end and end_us > range_start for range_start, range_end in ranges)


def compile_smart_dense_policy(
    proposals: Sequence[Mapping[str, Any]],
    policy: EditingPolicy,
    protected: Sequence[tuple[int, int]],
    protected_reasons: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Compile candidate evidence into an execution class with a safe fallback."""

    categories = sorted({reason.split(":", 1)[1] for reason in protected_reasons if ":" in reason})
    compiled: list[dict[str, Any]] = []
    for raw_proposal in proposals:
        proposal = dict(raw_proposal)
        proposal_type = str(proposal.get("proposal_type", ""))
        confidence = float(proposal.get("confidence", 0.0))
        meaning_risk = str(proposal.get("meaning_risk", "high"))
        continuity_risk = str(proposal.get("continuity_risk", "high"))
        cut_range = proposal.get("proposed_cut_range")
        if not isinstance(cut_range, Mapping):
            raise PlanningValidationError(
                f"proposal {proposal.get('proposal_id')} has no cut range"
            )
        cut_start_us = int(cut_range.get("start_us", -1))
        cut_end_us = int(cut_range.get("end_us", -1))
        overlaps_protected = _range_overlaps(cut_start_us, cut_end_us, protected)
        protected_check = {
            "passed": not overlaps_protected,
            "categories": categories if overlaps_protected else [],
            "notes": (
                "The proposed cut overlaps protected content."
                if overlaps_protected
                else "No protected content overlaps the proposed cut."
            ),
        }
        proposal["protected_content_check"] = protected_check
        proposal["safe_fallback"] = "keep_original"
        auto_threshold = AUTO_CONFIDENCE_THRESHOLDS.get(proposal_type, 1.01)
        review_threshold = REVIEW_CONFIDENCE_THRESHOLDS.get(proposal_type, 0.0)
        always_review = proposal_type in {
            "self_correction",
            "near_repetition",
            "semantic_repetition",
            "duplicate_take",
            "weak_take",
            "housekeeping",
        }
        if overlaps_protected or str(proposal.get("policy_result")) == "blocked":
            result = "blocked"
            action = "do_not_apply"
            prompt = "Keep the original range; protected or uncertain evidence blocks this cut."
        elif (
            not always_review
            and meaning_risk == "low"
            and continuity_risk in {"low", "medium"}
            and confidence >= auto_threshold
        ):
            result = "auto_eligible"
            action = "apply_only_under_approved_policy_and_render_join_qa"
            prompt = None
        elif confidence >= review_threshold:
            result = "review_required"
            action = "batch_review_before_apply"
            prompt = (
                f"Review the proposed {proposal_type} cut with its context and rendered join; "
                "keep the original range if uncertain."
            )
        else:
            result = "blocked"
            action = "do_not_apply"
            prompt = "Keep the original range because confidence is below the review threshold."
        proposal["policy_result"] = result
        proposal["recommended_action"] = action
        proposal["review_prompt"] = prompt
        proposal["approval_required"] = True
        compiled.append(proposal)
    return compiled


def protected_ranges(
    transcript: Mapping[str, Any],
    policy: EditingPolicy,
    duration_us: int,
) -> tuple[list[tuple[int, int]], list[str]]:
    words = _word_list(transcript)
    summary = transcript.get("confidence_summary")
    multi_speaker = isinstance(summary, dict) and int(summary.get("speaker_count", 0)) > 1
    ranges: list[tuple[int, int]] = []
    reasons: list[str] = []
    for index, word in enumerate(words):
        text = str(word.get("text", "")).strip()
        token = _word_token(text)
        probability = word.get("probability")
        protected_reason: str | None = None
        if multi_speaker:
            protected_reason = "multiple_speakers"
        elif word.get("timing_status") == "uncertain":
            protected_reason = "uncertain_timing"
        elif (
            isinstance(probability, (int, float)) and probability < policy.low_confidence_threshold
        ):
            protected_reason = "low_confidence"
        elif NUMBER_PATTERN.search(text):
            protected_reason = "number_or_figure"
        elif token in NEGATION_WORDS:
            protected_reason = "negation"
        elif token in CTA_WORDS:
            protected_reason = "call_to_action"
        elif index > 0 and text[:1].isupper():
            protected_reason = "possible_name"
        if protected_reason is None:
            continue
        try:
            start_us = int(word["start_us"])
            end_us = int(word["end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningValidationError("transcript word has invalid timing") from exc
        ranges.append(
            (
                max(0, start_us - policy.protected_padding_us),
                min(duration_us, end_us + policy.protected_padding_us),
            )
        )
        reasons.append(f"{word.get('word_id', 'unknown')}:{protected_reason}")
    return _merge_ranges(ranges), reasons


def _nearest_word_ids(
    interval: Mapping[str, Any], words_by_id: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    ids: list[str] = []
    for key in ("nearest_word_before", "nearest_word_after"):
        value = interval.get(key)
        if isinstance(value, str) and value in words_by_id and value not in ids:
            ids.append(value)
    return ids


def _context_for_range(
    words: Sequence[Mapping[str, Any]], start_us: int, end_us: int
) -> tuple[str, str, str]:
    before = [str(word.get("text", "")) for word in words if int(word.get("end_us", 0)) <= start_us]
    inside = [
        str(word.get("text", ""))
        for word in words
        if int(word.get("start_us", 0)) < end_us and int(word.get("end_us", 0)) > start_us
    ]
    after = [str(word.get("text", "")) for word in words if int(word.get("start_us", 0)) >= end_us]
    return " ".join(before[-3:]), " ".join(inside), " ".join(after[:3])


def _base_proposal(
    *,
    proposal_type: str,
    source_start_us: int,
    source_end_us: int,
    cut_start_us: int,
    cut_end_us: int,
    words: Sequence[Mapping[str, Any]],
    word_ids: Sequence[str],
    evidence_ids: Sequence[str],
    reason: str,
    confidence: float,
    meaning_risk: str,
    continuity_risk: str,
    policy_result: str,
    policy: EditingPolicy,
    alternative: str | None,
) -> dict[str, Any]:
    before, inside, after = _context_for_range(words, cut_start_us, cut_end_us)
    excerpt = " ".join(part for part in (before, f"[cut: {inside or 'silence'}]", after) if part)
    return {
        "proposal_id": "pending",
        "proposal_type": proposal_type,
        "source_range": {"start_us": source_start_us, "end_us": source_end_us},
        "proposed_cut_range": {"start_us": cut_start_us, "end_us": cut_end_us},
        "word_ids": list(dict.fromkeys(word_ids)),
        "excerpt": excerpt,
        "transcript_before": before,
        "transcript_inside": inside,
        "transcript_after": after,
        "handles": {
            "pre_handle_us": policy.pre_handle_us,
            "post_handle_us": policy.post_handle_us,
        },
        "reason": reason,
        "confidence": max(0.0, min(1.0, confidence)),
        "meaning_risk": meaning_risk,
        "continuity_risk": continuity_risk,
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "policy_result": policy_result,
        "approval_required": True,
        "alternative": alternative,
        "join_strategy": "hard_cut_with_micro_audio_crossfade",
        "join_preview_required": True,
    }


def _mechanical_proposals(
    silence: Mapping[str, Any],
    transcript: Mapping[str, Any],
    policy: EditingPolicy,
    duration_us: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    words = _word_list(transcript)
    words_by_id = {str(word.get("word_id")): word for word in words}
    proposals: list[dict[str, Any]] = []
    warnings: list[str] = []
    protected, protected_reasons = protected_ranges(transcript, policy, duration_us)
    if protected_reasons:
        warnings.extend(f"protected_content:{reason}" for reason in protected_reasons)
    noise_events = silence.get("noise_events", [])
    if noise_events is not None and not isinstance(noise_events, list):
        raise PlanningValidationError("silence noise_events must be an array")
    for event in noise_events or []:
        if not isinstance(event, dict):
            warnings.append("invalid_noise_event_skipped")
            continue
        event_id = str(event.get("event_id", "unknown"))
        try:
            start_us = int(event["start_us"])
            end_us = int(event["end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningValidationError(f"noise event has invalid timing: {event_id}") from exc
        if start_us < 0 or end_us <= start_us or end_us > duration_us:
            warnings.append(f"invalid_noise_event:{event_id}")
            continue
        if _range_overlaps(start_us, end_us, protected):
            warnings.append(f"noise_event_overlaps_protected_content:{event_id}")
            continue
        confidence_value = event.get("confidence", 0.0)
        confidence = float(confidence_value) if isinstance(confidence_value, (int, float)) else 0.0
        confidence = max(0.0, min(1.0, confidence))
        proposals.append(
            _base_proposal(
                proposal_type="accidental_noise",
                source_start_us=start_us,
                source_end_us=end_us,
                cut_start_us=start_us,
                cut_end_us=end_us,
                words=words,
                word_ids=_nearest_word_ids(event, words_by_id),
                evidence_ids=[event_id],
                reason=f"Remove an isolated detected {event.get('kind', 'noise')} event.",
                confidence=confidence,
                meaning_risk="low",
                continuity_risk="medium",
                policy_result="auto_eligible" if confidence >= 0.95 else "review_required",
                policy=policy,
                alternative=(
                    "Keep the original audio if the event is intentional or the join is audible."
                ),
            )
        )
    intervals = silence.get("intervals")
    if not isinstance(intervals, list):
        raise PlanningValidationError("silence intervals must be an array")
    for interval in intervals:
        if not isinstance(interval, dict):
            warnings.append("invalid_silence_interval_skipped")
            continue
        interval_id = str(interval.get("interval_id", "unknown"))
        start_us = int(interval.get("start_us", -1))
        end_us = int(interval.get("end_us", -1))
        if end_us <= start_us or start_us < 0 or end_us > duration_us:
            warnings.append(f"invalid_silence_interval:{interval_id}")
            continue
        if end_us - start_us < policy.minimum_silence_us:
            warnings.append(f"silence_below_policy_threshold:{interval_id}")
            continue
        classification = str(interval.get("classification", "uncertain"))
        if classification == "leading":
            cut_start_us, cut_end_us = 0, end_us - policy.post_handle_us
        elif classification == "trailing":
            cut_start_us, cut_end_us = start_us + policy.pre_handle_us, end_us
        elif classification == "inter_word":
            cut_start_us, cut_end_us = (
                start_us + policy.pre_handle_us,
                end_us - policy.post_handle_us,
            )
        else:
            warnings.append(f"silence_classification_not_eligible:{interval_id}")
            continue
        cut_start_us = max(0, min(cut_start_us, duration_us))
        cut_end_us = max(0, min(cut_end_us, duration_us))
        if cut_end_us - cut_start_us < policy.minimum_cut_us:
            warnings.append(f"cut_below_minimum_duration:{interval_id}")
            continue
        if _range_overlaps(cut_start_us, cut_end_us, protected):
            warnings.append(f"mechanical_cut_overlaps_protected_content:{interval_id}")
            continue
        boundary_ids = _nearest_word_ids(interval, words_by_id)
        proposal_type = {
            "leading": "leading_silence",
            "trailing": "trailing_silence",
            "inter_word": (
                "dead_air" if end_us - start_us >= policy.dead_air_threshold_us else "long_pause"
            ),
        }[classification]
        reason = (
            "Remove dead air while preserving speech handles"
            if proposal_type == "dead_air"
            else "Remove excess silence while preserving speech handles"
        )
        proposals.append(
            _base_proposal(
                proposal_type=proposal_type,
                source_start_us=start_us,
                source_end_us=end_us,
                cut_start_us=cut_start_us,
                cut_end_us=cut_end_us,
                words=words,
                word_ids=boundary_ids,
                evidence_ids=[interval_id],
                reason=reason,
                confidence=0.98,
                meaning_risk="low",
                continuity_risk="medium" if classification == "inter_word" else "low",
                policy_result="auto_eligible",
                policy=policy,
                alternative=(
                    "Keep the complete pause or modify the cut after listening to the handles"
                ),
            )
        )
    return proposals, warnings


def _smart_dense_mechanical_proposals(
    silence: Mapping[str, Any],
    transcript: Mapping[str, Any],
    policy: EditingPolicy,
    duration_us: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Emit every qualifying low-risk mechanical candidate from both evidence streams."""

    proposals, warnings = _mechanical_proposals(silence, transcript, policy, duration_us)
    words = _word_list(transcript)
    protected, protected_reasons = protected_ranges(transcript, policy, duration_us)
    if protected_reasons:
        warnings.extend(f"protected_content:{reason}" for reason in protected_reasons)

    for word in words:
        word_id = str(word.get("word_id", ""))
        token = _word_token(word.get("text"))
        if token not in FILLER_WORDS or not word_id:
            continue
        try:
            start_us = int(word["start_us"])
            end_us = int(word["end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanningValidationError(f"filler word has invalid timing: {word_id}") from exc
        if end_us <= start_us or start_us < 0 or end_us > duration_us:
            warnings.append(f"invalid_filler_word:{word_id}")
            continue
        if word.get("timing_status") == "uncertain":
            warnings.append(f"filler_word_uncertain_timing:{word_id}")
            continue
        if _range_overlaps(start_us, end_us, protected):
            warnings.append(f"filler_word_overlaps_protected_content:{word_id}")
            continue
        cut_duration_us = end_us - start_us
        if cut_duration_us < policy.minimum_cut_us:
            warnings.append(f"filler_word_below_minimum_duration:{word_id}")
            continue
        probability = word.get("probability")
        confidence = float(probability) if isinstance(probability, (int, float)) else 0.96
        confidence = max(0.0, min(1.0, confidence))
        policy_result = "auto_eligible" if confidence >= 0.95 else "review_required"
        proposal = _base_proposal(
            proposal_type="filler_word",
            source_start_us=start_us,
            source_end_us=end_us,
            cut_start_us=start_us,
            cut_end_us=end_us,
            words=words,
            word_ids=[word_id],
            evidence_ids=[word_id],
            reason="Remove an isolated filler word while preserving surrounding speech.",
            confidence=confidence,
            meaning_risk="low",
            continuity_risk="low",
            policy_result=policy_result,
            policy=policy,
            alternative="Keep the filler if the rendered cadence or emphasis is better with it.",
        )
        proposal.update(
            {
                "density_class": "micro",
                "join_strategy": "hard_cut_with_micro_audio_crossfade",
                "join_preview_required": True,
                "pacing_impact": "tightens",
                "protected_content_check": {
                    "passed": True,
                    "categories": [],
                    "notes": "No protected content overlaps the filler word.",
                },
            }
        )
        proposals.append(proposal)

    for proposal in proposals:
        proposal.setdefault("density_class", "micro")
        proposal.setdefault("join_strategy", "hard_cut_with_micro_audio_crossfade")
        proposal.setdefault("join_preview_required", True)
        proposal.setdefault("pacing_impact", "tightens")
        proposal.setdefault(
            "protected_content_check",
            {
                "passed": True,
                "categories": [],
                "notes": "Mechanical candidate does not overlap protected content.",
            },
        )
    proposals.sort(
        key=lambda item: (
            int(item["source_range"]["start_us"]),
            int(item["source_range"]["end_us"]),
            str(item["proposal_type"]),
        )
    )
    return proposals, list(dict.fromkeys(warnings))


def _semantic_proposals(
    transcript: Mapping[str, Any],
    policy: EditingPolicy,
    duration_us: int,
    silence: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    words = _word_list(transcript)
    protected, protected_reasons = protected_ranges(transcript, policy, duration_us)
    proposals: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add(
        proposal_type: str,
        start_us: int,
        end_us: int,
        word_ids: Sequence[str],
        reason: str,
        confidence: float,
        evidence_ids: Sequence[str],
        *,
        cut_start_us: int | None = None,
        cut_end_us: int | None = None,
        meaning_risk: str = "medium",
        continuity_risk: str = "medium",
        alternative: str = "Keep the words and ask the reviewer to confirm the intended meaning",
    ) -> None:
        if end_us <= start_us:
            return
        selected_start_us = start_us if cut_start_us is None else cut_start_us
        selected_end_us = end_us if cut_end_us is None else cut_end_us
        if selected_end_us <= selected_start_us:
            return
        blocked = _range_overlaps(selected_start_us, selected_end_us, protected)
        proposals.append(
            _base_proposal(
                proposal_type=proposal_type,
                source_start_us=start_us,
                source_end_us=end_us,
                cut_start_us=selected_start_us,
                cut_end_us=selected_end_us,
                words=words,
                word_ids=word_ids,
                evidence_ids=evidence_ids,
                reason=reason,
                confidence=confidence if not blocked else min(confidence, 0.35),
                meaning_risk="high" if blocked else meaning_risk,
                continuity_risk=continuity_risk,
                policy_result="blocked" if blocked else "review_required",
                policy=policy,
                alternative=alternative,
            )
        )

    tokens = [_word_token(word.get("text")) for word in words]
    for index in range(len(words) - 1):
        if tokens[index] and tokens[index] == tokens[index + 1]:
            first, second = words[index], words[index + 1]
            gap_us = int(second["start_us"]) - int(first["end_us"])
            is_stutter = 0 <= gap_us <= policy.stutter_max_gap_us
            add(
                "stutter" if is_stutter else "immediate_repetition",
                int(first["start_us"]),
                int(second["end_us"]),
                [str(first["word_id"]), str(second["word_id"])],
                "Tightly repeated word start may be a stutter"
                if is_stutter
                else "Adjacent duplicate words may be a repeated take",
                0.9 if is_stutter else 0.78,
                [str(first["word_id"]), str(second["word_id"])],
                cut_start_us=int(first["start_us"]) if is_stutter else None,
                cut_end_us=int(first["end_us"]) if is_stutter else None,
                meaning_risk="low" if is_stutter else "medium",
                continuity_risk="low" if is_stutter else "medium",
                alternative=(
                    "Keep both word starts if the repetition is intentional."
                    if is_stutter
                    else "Keep both words and review the intended emphasis."
                ),
            )

    for phrase in sorted(FILLER_PHRASES, key=len, reverse=True):
        for index in range(len(tokens) - len(phrase) + 1):
            if tuple(tokens[index : index + len(phrase)]) != phrase:
                continue
            selected = words[index : index + len(phrase)]
            add(
                "filler_phrase",
                int(selected[0]["start_us"]),
                int(selected[-1]["end_us"]),
                [str(word["word_id"]) for word in selected],
                "Filler phrase may be removable without changing the point.",
                0.84,
                [str(word["word_id"]) for word in selected],
                meaning_risk="low",
                continuity_risk="low",
                alternative=(
                    "Keep the phrase if it carries emphasis or a natural conversational beat."
                ),
            )

    for index, word in enumerate(words):
        token = tokens[index]
        if token in FILLER_WORDS:
            add(
                "false_start",
                int(word["start_us"]),
                int(word["end_us"]),
                [str(word["word_id"])],
                "Filler or abandoned start may be removable, subject to meaning review",
                0.62,
                [str(word["word_id"])],
            )
        probability = word.get("probability")
        if isinstance(probability, (int, float)) and (
            probability < policy.low_confidence_threshold
        ):
            add(
                "weak_take",
                int(word["start_us"]),
                int(word["end_us"]),
                [str(word["word_id"])],
                (
                    "Low-confidence speech may indicate a weak take but must not be removed "
                    "automatically"
                ),
                0.45,
                [str(word["word_id"])],
            )

    seen_exact_pairs: set[tuple[int, int]] = set()
    for size in (3, 2):
        for index in range(len(words) - (size * 2) + 1):
            first_tokens = tuple(tokens[index : index + size])
            second_index = index + size
            second_tokens = tuple(tokens[second_index : second_index + size])
            if not first_tokens or first_tokens != second_tokens:
                continue
            first_words = words[index : index + size]
            second_words = words[second_index : second_index + size]
            gap_us = int(second_words[0]["start_us"]) - int(first_words[-1]["end_us"])
            if gap_us < 0 or gap_us > 1_500_000:
                continue
            pair = (index, second_index)
            if pair in seen_exact_pairs:
                continue
            seen_exact_pairs.add(pair)
            add(
                "exact_repetition",
                int(first_words[0]["start_us"]),
                int(second_words[-1]["end_us"]),
                [str(word["word_id"]) for word in [*first_words, *second_words]],
                "The same short phrase is repeated in a nearby take.",
                0.86,
                [str(word["word_id"]) for word in [*first_words, *second_words]],
                cut_start_us=int(first_words[0]["start_us"]),
                cut_end_us=int(first_words[-1]["end_us"]),
                continuity_risk="medium",
                alternative="Keep both phrases if the second repetition adds emphasis or clarity.",
            )

    for index in range(len(words)):
        for size in (4, 3):
            if index + size >= len(words):
                continue
            first_content = [
                token
                for token in tokens[index : index + size]
                if token and token not in SEMANTIC_STOPWORDS
            ]
            if len(first_content) < 2:
                continue
            for second_index in range(index + size, min(len(words) - size + 1, index + 22)):
                gap_us = int(words[second_index]["start_us"]) - int(
                    words[index + size - 1]["end_us"]
                )
                if gap_us < 0 or gap_us > 5_000_000:
                    continue
                second_content = [
                    token
                    for token in tokens[second_index : second_index + size]
                    if token and token not in SEMANTIC_STOPWORDS
                ]
                if len(second_content) < 2:
                    continue
                if tuple(tokens[index : index + size]) == tuple(
                    tokens[second_index : second_index + size]
                ):
                    continue
                sequence_ratio = SequenceMatcher(
                    None, first_content, second_content, autojunk=False
                ).ratio()
                overlap = len(set(first_content) & set(second_content)) / max(
                    1, len(set(first_content) | set(second_content))
                )
                if sequence_ratio >= 0.72:
                    proposal_type = "near_repetition"
                    confidence = 0.78
                    reason = "Nearby phrases substantially overlap and may repeat the same wording."
                elif overlap >= 0.6:
                    proposal_type = "semantic_repetition"
                    confidence = 0.68
                    reason = (
                        "Nearby phrases share the same content words and may repeat the same point."
                    )
                else:
                    continue
                first_range = words[index : index + size]
                second_range = words[second_index : second_index + size]
                add(
                    proposal_type,
                    int(first_range[0]["start_us"]),
                    int(second_range[-1]["end_us"]),
                    [str(word["word_id"]) for word in [*first_range, *second_range]],
                    reason,
                    confidence,
                    [str(word["word_id"]) for word in [*first_range, *second_range]],
                    cut_start_us=int(first_range[0]["start_us"]),
                    cut_end_us=int(first_range[-1]["end_us"]),
                    continuity_risk="medium",
                    alternative=(
                        "Keep both phrases and review whether the second adds a distinct fact."
                    ),
                )
                break

    marker_phrases = {
        ("by", "the", "way"): "Tangent marker may introduce removable side material",
        ("speaking", "of"): "Tangent marker may introduce removable side material",
        ("on", "another", "note"): "Tangent marker may introduce removable side material",
    }
    for marker, reason in marker_phrases.items():
        for index in range(len(tokens) - len(marker) + 1):
            if tuple(tokens[index : index + len(marker)]) != marker:
                continue
            marker_words = words[index : min(len(words), index + 8)]
            if not marker_words:
                continue
            add(
                "tangent",
                int(marker_words[0]["start_us"]),
                int(marker_words[-1]["end_us"]),
                [str(word["word_id"]) for word in marker_words],
                reason,
                0.52,
                [str(word["word_id"]) for word in marker_words],
            )
            break

    for marker in sorted(CORRECTION_MARKERS, key=len, reverse=True):
        for index in range(len(tokens) - len(marker) + 1):
            if tuple(tokens[index : index + len(marker)]) != marker or index == 0:
                continue
            previous = words[index - 1]
            marker_words = words[index : index + len(marker)]
            add(
                "self_correction",
                int(previous["start_us"]),
                int(marker_words[-1]["end_us"]),
                [str(word["word_id"]) for word in [previous, *marker_words]],
                "A correction marker suggests the preceding wording was abandoned.",
                0.76,
                [str(word["word_id"]) for word in [previous, *marker_words]],
                cut_start_us=int(previous["start_us"]),
                cut_end_us=int(previous["end_us"]),
                alternative="Keep the preceding wording if the correction is not a true self-edit.",
            )

    if silence is not None:
        word_by_id = {str(word.get("word_id")): word for word in words}
        intervals = silence.get("intervals", [])
        if isinstance(intervals, list):
            for interval in intervals:
                if not isinstance(interval, dict):
                    continue
                if str(interval.get("classification")) != "inter_word":
                    continue
                try:
                    interval_start = int(interval["start_us"])
                    interval_end = int(interval["end_us"])
                except (KeyError, TypeError, ValueError):
                    continue
                if interval_end - interval_start < policy.minimum_silence_us:
                    continue
                previous_id = interval.get("nearest_word_before")
                previous_word = word_by_id.get(str(previous_id))
                if (
                    previous_word is None
                    or _word_token(previous_word.get("text")) not in ABANDONED_TRAILING_WORDS
                ):
                    continue
                previous_id_text = str(previous_word["word_id"])
                add(
                    "abandoned_phrase",
                    int(previous_word["start_us"]),
                    int(previous_word["end_us"]),
                    [previous_id_text],
                    "A phrase ends on an unfinished function word before a long pause.",
                    0.7,
                    [str(interval.get("interval_id", "unknown")), previous_id_text],
                    meaning_risk="medium",
                    alternative=(
                        "Keep the fragment if the pause is intentional or the sentence "
                        "resumes naturally."
                    ),
                )

    segments = transcript.get("segments", [])
    if isinstance(segments, list):
        word_by_id = {str(word.get("word_id")): word for word in words}

        def segment_words(segment: Mapping[str, Any]) -> list[dict[str, Any]]:
            ids = segment.get("word_ids", [])
            if isinstance(ids, list):
                selected = [word_by_id[str(item)] for item in ids if str(item) in word_by_id]
                if selected:
                    return selected
            start = int(segment.get("start_us", 0))
            end = int(segment.get("end_us", 0))
            return [
                word
                for word in words
                if int(word.get("start_us", 0)) < end and int(word.get("end_us", 0)) > start
            ]

        normalized_segments = [
            segment
            for segment in segments
            if isinstance(segment, dict) and str(segment.get("text", "")).strip()
        ]
        for index, first_segment in enumerate(normalized_segments):
            first_text = tuple(_word_token(item) for item in str(first_segment["text"]).split())
            if not first_text:
                continue
            for second_segment in normalized_segments[index + 1 : index + 5]:
                second_text = tuple(
                    _word_token(item) for item in str(second_segment["text"]).split()
                )
                if not second_text:
                    continue
                ratio = SequenceMatcher(None, first_text, second_text, autojunk=False).ratio()
                if ratio < 0.92:
                    continue
                first_end = int(first_segment.get("end_us", 0))
                second_start = int(second_segment.get("start_us", 0))
                if second_start < first_end or second_start - first_end > 8_000_000:
                    continue
                first_score = float(first_segment.get("average_log_probability") or -10.0)
                second_score = float(second_segment.get("average_log_probability") or -10.0)
                selected_segment = first_segment if first_score < second_score else second_segment
                selected_words = segment_words(selected_segment)
                if not selected_words:
                    continue
                selected_ids = [str(word["word_id"]) for word in selected_words]
                add(
                    "duplicate_take",
                    int(selected_segment["start_us"]),
                    int(selected_segment["end_us"]),
                    selected_ids,
                    "Nearby transcript segments are near-duplicate takes; keep the stronger take.",
                    0.84,
                    [
                        str(first_segment["segment_id"]),
                        str(second_segment["segment_id"]),
                    ],
                    meaning_risk="high",
                    continuity_risk="high",
                    alternative=(
                        "Keep both takes or choose one only after visual and factual review."
                    ),
                )

    for index, token in enumerate(tokens):
        if token in HOUSEKEEPING_WORDS:
            word = words[index]
            add(
                "housekeeping",
                int(word["start_us"]),
                int(word["end_us"]),
                [str(word["word_id"])],
                "Recording or production housekeeping may be removable",
                0.5,
                [str(word["word_id"])],
            )

    if protected_reasons:
        warnings.extend(f"protected_content:{reason}" for reason in protected_reasons)
    return proposals, warnings


def build_edit_proposals(
    *,
    package_root: Path,
    layout: ProjectLayout,
    source_manifest_path: Path,
    source_manifest: Mapping[str, Any],
    transcript_path: Path,
    transcript: Mapping[str, Any],
    silence_path: Path,
    silence: Mapping[str, Any],
    policy: EditingPolicy,
    revision_id: str,
) -> dict[str, Any]:
    duration_us = int(source_manifest.get("media_duration_us", 0))
    if duration_us <= 0:
        raise PlanningValidationError("source manifest has no positive duration")
    if int(transcript.get("source_duration_us", -1)) != duration_us:
        raise PlanningValidationError("transcript duration does not match source duration")
    if int(silence.get("source_duration_us", -1)) != duration_us:
        raise PlanningValidationError("silence duration does not match source duration")
    mechanical_builder = (
        _smart_dense_mechanical_proposals
        if policy.profile == "smart_dense"
        else _mechanical_proposals
    )
    mechanical, mechanical_warnings = mechanical_builder(silence, transcript, policy, duration_us)
    semantic, semantic_warnings = _semantic_proposals(
        transcript, policy, duration_us, silence=silence
    )
    protected, protected_reasons = protected_ranges(transcript, policy, duration_us)
    proposals = compile_smart_dense_policy(
        mechanical + semantic,
        policy,
        protected,
        protected_reasons,
    )
    proposals.sort(
        key=lambda item: (
            int(item["source_range"]["start_us"]),
            int(item["source_range"]["end_us"]),
            str(item["proposal_type"]),
        )
    )
    for index, proposal in enumerate(proposals, start=1):
        proposal["proposal_id"] = f"prp_{index:06d}"
    payload: dict[str, Any] = {
        "schema_name": "edit_proposals",
        "schema_version": "1.0.0",
        "artifact_id": "art_proposals",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("edit-planning", "deterministic-policy", __version__),
        "inputs": [
            artifact_input("art_source", source_manifest_path),
            artifact_input("art_transcript", transcript_path),
            artifact_input("art_silence", silence_path),
        ],
        "config_sha256": config_sha256(layout),
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "policy_sha256": policy.sha256,
        "source_duration_us": duration_us,
        "proposals": proposals,
        "total_proposed_cut_us": sum(
            int(item["proposed_cut_range"]["end_us"]) - int(item["proposed_cut_range"]["start_us"])
            for item in proposals
        ),
        "warnings": list(dict.fromkeys(mechanical_warnings + semantic_warnings)),
        "protected_ranges": [
            {"start_us": start_us, "end_us": end_us} for start_us, end_us in protected
        ],
    }
    validate_artifact(package_root, "edit_proposals", payload)
    return payload


def _find_trigger_word_ids(
    transcript: Mapping[str, Any], quote: str, start_us: int, end_us: int
) -> list[str]:
    wanted = [_word_token(part) for part in quote.split()]
    wanted = [part for part in wanted if part]
    words = _word_list(transcript)
    tokens = [_word_token(word.get("text")) for word in words]
    for index in range(len(tokens) - len(wanted) + 1):
        if tokens[index : index + len(wanted)] != wanted:
            continue
        selected = words[index : index + len(wanted)]
        if int(selected[0]["start_us"]) >= start_us and int(selected[-1]["end_us"]) <= end_us:
            return [str(word["word_id"]) for word in selected]
    return []


def _default_effect_fallback(kind: str) -> str:
    if kind in {"picture_in_picture", "screen_focus"}:
        return "Keep the original source layer if the approved target or timing cannot be validated"
    if kind in {"broll", "picture_in_picture"}:
        return "Omit the optional insert and preserve the approved base edit"
    if kind == "sound_effect":
        return "Render without the cue if timing, licence, or mix validation fails"
    return "Preserve the approved base edit if the effect cannot be rendered or reviewed"


def build_effect_plan(
    *,
    package_root: Path,
    layout: ProjectLayout,
    source_manifest_path: Path,
    source_manifest: Mapping[str, Any],
    transcript_path: Path | None,
    transcript: Mapping[str, Any] | None,
    policy: EditingPolicy,
    effect_specs: Sequence[Mapping[str, Any]],
    revision_id: str,
) -> dict[str, Any]:
    duration_us = int(source_manifest.get("media_duration_us", 0))
    source_sha256 = str(source_manifest.get("sha256", ""))
    if duration_us <= 0 or not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise PlanningValidationError("source manifest cannot identify effect-plan source")
    normalized: list[dict[str, Any]] = []
    known_word_ids = {
        str(word.get("word_id")) for word in _word_list(transcript or {}) if word.get("word_id")
    }
    for index, raw_spec in enumerate(effect_specs, start=1):
        spec = dict(raw_spec)
        kind = str(spec.get("kind", ""))
        renderer = str(spec.get("renderer", ""))
        if kind not in SUPPORTED_EFFECT_KINDS:
            raise PlanningValidationError(f"unsupported effect kind: {kind}")
        if renderer not in SUPPORTED_RENDERERS:
            raise PlanningValidationError(f"unsupported effect renderer: {renderer}")
        start_us = spec.get("start_us")
        end_us = spec.get("end_us")
        if not isinstance(start_us, int) or not isinstance(end_us, int):
            raise PlanningValidationError(f"effect {index} must use integer microseconds")
        _time_range(start_us, end_us, duration_us, f"effect {index} range")
        trigger_quote = spec.get("trigger_quote")
        word_ids = [str(value) for value in spec.get("word_ids", [])]
        if not isinstance(trigger_quote, (str, type(None))):
            raise PlanningValidationError(f"effect {index} trigger_quote must be a string or null")
        if trigger_quote and transcript is not None:
            matched = _find_trigger_word_ids(transcript, trigger_quote, start_us, end_us)
            if not matched:
                raise PlanningValidationError(
                    f"effect {index} trigger quote is not found in its source range"
                )
            word_ids = matched
        if any(value not in known_word_ids for value in word_ids):
            raise PlanningValidationError(f"effect {index} references an unknown transcript word")
        if not bool(spec.get("requires_approval", True)):
            raise PlanningValidationError("every effect requires explicit Gate 1 approval")
        asset_refs = spec.get("asset_refs", [])
        if not isinstance(asset_refs, list):
            raise PlanningValidationError(f"effect {index} asset_refs must be an array")
        cleaned_assets: list[dict[str, str]] = []
        for asset in asset_refs:
            if not isinstance(asset, dict):
                raise PlanningValidationError(f"effect {index} asset reference must be an object")
            asset_id = str(asset.get("asset_id", ""))
            asset_sha256 = str(asset.get("sha256", ""))
            if not asset_id or not re.fullmatch(r"[a-f0-9]{64}", asset_sha256):
                raise PlanningValidationError(f"effect {index} asset reference is not hash-bound")
            clean_asset = {"asset_id": asset_id, "sha256": asset_sha256}
            if "path" in asset:
                path = Path(str(asset["path"])).expanduser().resolve()
                if not path.is_file() or sha256_file(path) != asset_sha256:
                    raise PlanningValidationError(
                        f"effect {index} asset hash does not match its file"
                    )
                clean_asset["path"] = str(path)
            cleaned_assets.append(clean_asset)
        fallback = spec.get("fallback") or _default_effect_fallback(kind)
        risk = str(
            spec.get("risk")
            or ("high" if renderer in {"sam3", "matanyone2", "provider"} else "medium")
        )
        if risk not in {"low", "medium", "high"}:
            raise PlanningValidationError(f"effect {index} risk must be low, medium, or high")
        effect_id = str(spec.get("id") or f"fx_{index:06d}")
        normalized_effect: dict[str, Any] = {
            "id": effect_id,
            "kind": kind,
            "start_us": start_us,
            "end_us": end_us,
            "trigger_quote": trigger_quote,
            "target_prompt": spec.get("target_prompt"),
            "renderer": renderer,
            "fallback": str(fallback),
            "word_ids": list(dict.fromkeys(word_ids)),
            "evidence_ids": list(
                dict.fromkeys(str(value) for value in spec.get("evidence_ids", word_ids))
            ),
            "asset_refs": cleaned_assets,
            "risk": risk,
            "parameters": dict(spec.get("parameters", {})),
            "requires_approval": True,
        }
        normalized.append(normalized_effect)
    inputs = [artifact_input("art_source", source_manifest_path)]
    if transcript_path is not None and transcript is not None:
        inputs.append(artifact_input("art_transcript", transcript_path))
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "project_id": layout.root.name,
        "source_sha256": source_sha256,
        "artifact_id": "art_effect_plan",
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("effect-planning", "deterministic-effect-contract", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        "policy_sha256": policy.sha256,
        "effects": normalized,
        "status": "complete",
        "warnings": [],
        "notes": [
            "All effect requests are proposals and require explicit Gate 1 approval.",
            (
                "Worker and provider renderers remain disabled until their separate "
                "prerequisites and approvals pass."
            ),
        ],
    }
    validate_artifact(package_root, "effect_plan", payload)
    return payload


def effect_plan_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Effect plan review",
        "",
        f"- Project: {payload['project_id']}",
        f"- Source SHA-256: {payload['source_sha256']}",
        "- Gate 1 approval: required for every effect",
        "",
    ]
    effects = payload.get("effects", [])
    if not effects:
        lines.extend(["No effect requests were generated.", ""])
        return "\n".join(lines)
    for effect in effects:
        lines.extend(
            [
                f"## {effect['id']} | {effect['kind']}",
                "",
                (
                    f"- Source: {int(effect['start_us']) / 1_000_000:.3f}s to "
                    f"{int(effect['end_us']) / 1_000_000:.3f}s"
                ),
                f"- Renderer: {effect['renderer']}",
                f"- Trigger: {effect.get('trigger_quote') or 'explicit time range'}",
                f"- Word IDs: {', '.join(effect.get('word_ids', [])) or 'none'}",
                f"- Risk: {effect.get('risk', 'medium')}",
                f"- Fallback: {effect.get('fallback') or 'none recorded'}",
                "- Decision: approval required",
                "",
            ]
        )
    return "\n".join(lines)


def _edit_plan_markdown(proposals: Mapping[str, Any], effect_plan: Mapping[str, Any]) -> str:
    lines = [
        "# Edit plan review",
        "",
        (
            "The JSON decision file is the review contract. Proposal and effect "
            "artifacts are immutable."
        ),
        "Every proposal defaults to reject until an operator imports an explicit decision.",
        "",
        "## Cut proposals",
        "",
    ]
    proposal_items = proposals.get("proposals", [])
    if not proposal_items:
        lines.extend(["No cut proposals were generated.", ""])
    for item in proposal_items:
        source = item["source_range"]
        cut = item["proposed_cut_range"]
        saved = int(cut["end_us"]) - int(cut["start_us"])
        lines.extend(
            [
                f"### {item['proposal_id']} | {item['proposal_type']}",
                "",
                (
                    f"- Source evidence: {int(source['start_us']) / 1_000_000:.3f}s to "
                    f"{int(source['end_us']) / 1_000_000:.3f}s"
                ),
                (
                    f"- Proposed cut: {int(cut['start_us']) / 1_000_000:.3f}s to "
                    f"{int(cut['end_us']) / 1_000_000:.3f}s"
                ),
                f"- Duration saved: {saved / 1_000_000:.3f}s",
                f"- Transcript before: {item.get('transcript_before', '')}",
                f"- Transcript inside: {item.get('transcript_inside', '')}",
                f"- Transcript after: {item.get('transcript_after', '')}",
                f"- Evidence IDs: {', '.join(item.get('evidence_ids', []))}",
                f"- Reason: {item['reason']}",
                (
                    f"- Confidence/risk: {item['confidence']} / "
                    f"{item['meaning_risk']} meaning, {item['continuity_risk']} continuity"
                ),
                f"- Policy result: {item['policy_result']}",
                "- Decision options: approve, reject, modify",
                "",
            ]
        )
    lines.extend(["## Effect requests", "", effect_plan_markdown(effect_plan)])
    return "\n".join(lines)


def _locate_proposals(
    layout: ProjectLayout, proposal_set_sha256: str
) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(layout.artifacts.glob("edit-proposals*.json"))
    for path in candidates:
        try:
            payload = _read_object(path, "edit proposals")
        except PlanningValidationError:
            continue
        if sha256_file(path) == proposal_set_sha256:
            return path, payload
    raise PlanningValidationError("the decision file references a missing proposal set")


def augment_edit_proposals(
    package_root: Path,
    layout: ProjectLayout,
    base_proposals_path: Path,
    instructions_path: Path,
    *,
    output: Path | None = None,
) -> Path:
    """Append explicit operator cuts without mutating the model proposal artifact."""

    base_path = base_proposals_path.expanduser().resolve()
    instructions_file = instructions_path.expanduser().resolve()
    for path, label in (
        (base_path, "base edit proposals"),
        (instructions_file, "operator instructions"),
    ):
        try:
            path.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise PlanningValidationError(f"{label} must be inside the project") from exc
        if not path.is_file():
            raise PlanningValidationError(f"{label} is missing: {path}")

    base = _read_object(base_path, "base edit proposals")
    validate_artifact(package_root, "edit_proposals", base)
    instructions = _read_object(instructions_file, "operator edit instructions")
    validate_artifact(package_root, "operator_edit_instructions", instructions)
    if base["project_id"] != layout.root.name or instructions["project_id"] != layout.root.name:
        raise PlanningValidationError("operator edit inputs belong to another project")
    if base["revision_id"] != instructions["revision_id"]:
        raise PlanningValidationError("operator instructions belong to another revision")
    source_manifest = _read_object(layout.artifacts / "source-manifest.json", "source manifest")
    validate_artifact(package_root, "source_manifest", source_manifest)
    if source_manifest.get("sha256") != instructions["source_sha256"]:
        raise StaleApprovalError("operator instructions are bound to a different source")

    transcript = _read_object(layout.artifacts / "transcript.json", "transcript")
    words = _word_list(transcript)
    existing_ids = {str(item["proposal_id"]) for item in base.get("proposals", [])}
    additions: list[dict[str, Any]] = []
    for raw in instructions["edits"]:
        if not isinstance(raw, Mapping):
            raise PlanningValidationError("operator edit instruction must be an object")
        proposal_id = str(raw["edit_id"])
        if proposal_id in existing_ids:
            raise PlanningValidationError(f"duplicate operator edit identifier: {proposal_id}")
        start_us = int(raw["start_us"])
        end_us = int(raw["end_us"])
        _time_range(start_us, end_us, int(base["source_duration_us"]), proposal_id)
        before, inside, after = _context_for_range(words, start_us, end_us)
        overlapping_word_ids = [
            str(word["word_id"])
            for word in words
            if int(word.get("start_us", 0)) < end_us
            and int(word.get("end_us", 0)) > start_us
            and word.get("word_id")
        ]
        additions.append(
            {
                "proposal_id": proposal_id,
                "proposal_type": "housekeeping",
                "source_range": {"start_us": start_us, "end_us": end_us},
                "proposed_cut_range": {"start_us": start_us, "end_us": end_us},
                "word_ids": overlapping_word_ids,
                "excerpt": " ".join(
                    part for part in (before, f"[cut: {inside or 'silence'}]", after) if part
                ),
                "transcript_before": before,
                "transcript_inside": inside,
                "transcript_after": after,
                "handles": {"pre_handle_us": 0, "post_handle_us": 0},
                "reason": str(raw["reason"]),
                "confidence": 1.0,
                "meaning_risk": "medium",
                "continuity_risk": "medium",
                "evidence_ids": [f"{proposal_id}_operator_request"],
                "policy_result": "review_required",
                "approval_required": True,
                "alternative": "Keep the original phrase if the rendered join is not natural.",
                "edit_intent": "semantic_tightening",
                "density_class": "standard",
                "join_strategy": "hard_cut_with_micro_audio_crossfade",
                "join_preview_required": True,
                "pacing_impact": "tightens",
                "protected_content_check": {
                    "passed": True,
                    "categories": [],
                    "notes": (
                        "Explicit operator instruction is bound to the source and reviewed "
                        "at Gate 1."
                    ),
                },
            }
        )
        existing_ids.add(proposal_id)

    payload = dict(base)
    payload["artifact_id"] = "art_production_proposals"
    payload["created_at"] = now_iso()
    payload["producer"] = producer(
        "edit-planning-operator-augmentation", "deterministic-operator-contract", __version__
    )
    payload["inputs"] = [
        *[dict(item) for item in base.get("inputs", []) if isinstance(item, Mapping)],
        artifact_input("art_parent_proposals", base_path),
        artifact_input("art_operator_instructions", instructions_file),
    ]
    payload["proposals"] = sorted(
        [*[dict(item) for item in base.get("proposals", [])], *additions],
        key=lambda item: (
            int(item["source_range"]["start_us"]),
            int(item["source_range"]["end_us"]),
            str(item["proposal_id"]),
        ),
    )
    payload["total_proposed_cut_us"] = sum(
        int(item["proposed_cut_range"]["end_us"]) - int(item["proposed_cut_range"]["start_us"])
        for item in payload["proposals"]
    )
    payload["warnings"] = [
        *[str(item) for item in base.get("warnings", [])],
        "operator_explicit_cuts_are_bound_to_operator_edit_instructions",
    ]
    selected_output = (
        (output or layout.artifacts / "edit-proposals-production.json").expanduser().resolve()
    )
    try:
        selected_output.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(
            "augmented proposal output must be inside the project"
        ) from exc
    write_validated_artifact(package_root, "edit_proposals", selected_output, payload)
    return selected_output


def materialize_operator_edit_decisions(
    package_root: Path,
    layout: ProjectLayout,
    production_proposals_path: Path,
    smart_dense_batch_path: Path,
    instructions_path: Path,
    *,
    output: Path | None = None,
    safe_fallback_only: bool = False,
    revision_id: str | None = None,
) -> Path:
    """Create a complete decision artifact from reviewed and explicit cuts.

    ``safe_fallback_only`` is the repair path after an operator rejects a rendered
    candidate. It keeps the hash-bound operator edits while rejecting every
    policy-authorized automatic cut, so a failed dense edit cannot be silently
    carried into the next revision.
    """

    target_revision_id = str(revision_id or "")
    if target_revision_id and re.fullmatch(r"rev_[0-9]{3,}", target_revision_id) is None:
        raise PlanningValidationError(f"invalid decision revision id: {target_revision_id}")

    proposal_path = production_proposals_path.expanduser().resolve()
    batch_path = smart_dense_batch_path.expanduser().resolve()
    instruction_path = instructions_path.expanduser().resolve()
    for path, label in (
        (proposal_path, "production edit proposals"),
        (batch_path, "smart-dense review batch"),
        (instruction_path, "operator instructions"),
    ):
        try:
            path.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise PlanningValidationError(f"{label} must be inside the project") from exc
        if not path.is_file():
            raise PlanningValidationError(f"{label} is missing: {path}")

    proposals = _read_object(proposal_path, "production edit proposals")
    validate_artifact(package_root, "edit_proposals", proposals)
    batch = _read_object(batch_path, "smart-dense review batch")
    validate_artifact(package_root, "edit_review_batch", batch)
    instructions = _read_object(instruction_path, "operator edit instructions")
    validate_artifact(package_root, "operator_edit_instructions", instructions)
    if proposals["project_id"] != layout.root.name or batch["project_id"] != layout.root.name:
        raise PlanningValidationError("decision inputs belong to another project")
    if (
        proposals["revision_id"] != batch["revision_id"]
        or proposals["revision_id"] != instructions["revision_id"]
    ):
        raise PlanningValidationError("decision inputs belong to another revision")

    parent_hash = next(
        (
            str(item["sha256"])
            for item in proposals.get("inputs", [])
            if isinstance(item, Mapping) and item.get("artifact_id") == "art_parent_proposals"
        ),
        None,
    )
    if parent_hash is None or batch.get("proposal_set_sha256") != parent_hash:
        raise StaleApprovalError("smart-dense batch is bound to a different proposal parent")
    base_path, _base = _locate_proposals(layout, parent_hash)
    if sha256_file(base_path) != str(batch["proposal_set_sha256"]):
        raise StaleApprovalError("smart-dense batch proposal hash is stale")

    batch_by_id: dict[str, Mapping[str, Any]] = {
        str(item["proposal_id"]): item
        for item in batch.get("policy_batch", [])
        if isinstance(item, Mapping)
    }
    explicit_by_id = {
        str(item["edit_id"]): item
        for item in instructions.get("edits", [])
        if isinstance(item, Mapping)
    }
    protected_values = proposals.get("protected_ranges", [])
    protected = [
        (int(item["start_us"]), int(item["end_us"]))
        for item in protected_values
        if isinstance(item, Mapping)
    ]
    explicit_ranges = {
        item_id: (int(item["start_us"]), int(item["end_us"]))
        for item_id, item in explicit_by_id.items()
    }
    decisions: list[dict[str, Any]] = []
    for proposal in proposals.get("proposals", []):
        proposal_id = str(proposal["proposal_id"])
        proposal_hash = canonical_sha256(proposal)
        decision = "reject"
        reason = (
            "Safe fallback after operator rejection: retain the original range; automatic "
            "policy cuts are disabled for this repair."
            if safe_fallback_only
            else "Safe fallback: retain the original range because it was not "
            "policy-authorized or explicitly requested."
        )
        if proposal_id in explicit_by_id:
            decision = "approve"
            reason = str(explicit_by_id[proposal_id]["reason"])
        elif not safe_fallback_only and proposal_id in batch_by_id:
            batch_item = batch_by_id[proposal_id]
            if str(batch_item.get("proposal_sha256")) != proposal_hash:
                raise StaleApprovalError(f"smart-dense proposal hash is stale: {proposal_id}")
            cut = proposal["proposed_cut_range"]
            cut_start = int(cut["start_us"])
            cut_end = int(cut["end_us"])
            covered_by_explicit = next(
                (
                    item_id
                    for item_id, (start_us, end_us) in explicit_ranges.items()
                    if _range_overlaps(cut_start, cut_end, [(start_us, end_us)])
                ),
                None,
            )
            if covered_by_explicit is None:
                decision = "approve"
                reason = (
                    "Approved under the current hash-bound smart_dense policy; "
                    "join preview and QA remain required."
                )
            else:
                reason = (
                    "Retained because the explicit operator cut "
                    f"{covered_by_explicit} covers this candidate."
                )
        decisions.append(
            {
                "proposal_id": proposal_id,
                "proposal_sha256": proposal_hash,
                "decision": decision,
                "modified_cut_range": None,
                "reason": reason,
            }
        )

    overrides: list[dict[str, Any]] = []
    for edit_id, (start_us, end_us) in explicit_ranges.items():
        if _range_overlaps(start_us, end_us, protected):
            overrides.append(
                {
                    "start_us": start_us,
                    "end_us": end_us,
                    "reason": (
                        f"Explicit operator instruction {edit_id} authorizes removal of this "
                        "non-meaning-bearing phrase despite the conservative protection padding."
                    ),
                }
            )

    payload: dict[str, Any] = {
        "schema_name": "edit_review_decisions",
        "schema_version": "1.0.0",
        "artifact_id": "art_production_edit_review",
        "project_id": layout.root.name,
        # A repair decision may be prepared for a new immutable revision while
        # retaining its hash-bound parent proposal set and operator request.
        "revision_id": target_revision_id or str(proposals["revision_id"]),
        "created_at": now_iso(),
        "reviewer": {
            "actor": str(instructions["operator"]["actor"]),
            "role": str(instructions["operator"]["role"]),
        },
        "proposal_set_artifact_id": str(proposals["artifact_id"]),
        "proposal_set_sha256": sha256_file(proposal_path),
        "decisions": decisions,
        "protected_range_overrides": overrides,
    }
    selected_output = (
        (
            output
            or layout.review
            / (
                f"edit-decisions-{target_revision_id}.json"
                if target_revision_id
                else "edit-decisions-production.json"
            )
        )
        .expanduser()
        .resolve()
    )
    try:
        selected_output.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError("decision output must be inside the project") from exc
    write_validated_artifact(package_root, "edit_review_decisions", selected_output, payload)
    return selected_output


def _decision_template(
    proposals: Mapping[str, Any], proposal_path: Path, revision_id: str
) -> dict[str, Any]:
    return {
        "schema_name": "edit_review_decisions",
        "schema_version": "1.0.0",
        "artifact_id": "art_edit_review",
        "project_id": str(proposals["project_id"]),
        "revision_id": revision_id,
        "created_at": now_iso(),
        "reviewer": {"actor": "replace-with-reviewer", "role": "editor"},
        "proposal_set_artifact_id": str(proposals["artifact_id"]),
        "proposal_set_sha256": sha256_file(proposal_path),
        "decisions": [
            {
                "proposal_id": str(item["proposal_id"]),
                "proposal_sha256": canonical_sha256(item),
                "decision": "reject",
                "modified_cut_range": None,
                "reason": "Safe default. Change only after review.",
            }
            for item in proposals.get("proposals", [])
        ],
    }


def _write_alias_if_absent(
    path: Path, payload: Mapping[str, Any], schema_name: str, package_root: Path
) -> None:
    if not path.exists():
        write_validated_artifact(package_root, schema_name, path, dict(payload))


def _mark_project_planning(package_root: Path, layout: ProjectLayout) -> None:
    manifest_path = layout.state / "project-manifest.json"
    if not manifest_path.is_file():
        return
    value = _read_object(manifest_path, "project manifest")
    value["updated_at"] = now_iso()
    value["state"] = "awaiting_edit_approval"
    active_artifacts = value.setdefault("active_artifacts", {})
    if not isinstance(active_artifacts, dict):
        raise PlanningValidationError("project manifest active_artifacts must be an object")
    active_artifacts["edit_proposals"] = "art_proposals"
    active_artifacts["effect_plan"] = "art_effect_plan"
    write_validated_artifact(package_root, "project_manifest", manifest_path, value)


def _cached_plan(
    package_root: Path,
    layout: ProjectLayout,
    state: Mapping[str, Any] | None,
    stage_key: str,
) -> PlanOutputs | None:
    if not state or state.get("status") != "complete" or state.get("stage_key") != stage_key:
        return None
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    required = {"edit_proposals", "effect_plan", "decision_template", "review_markdown"}
    if not required.issubset(artifacts):
        return None
    if not all(_stage_ref_valid(layout, artifacts[name]) for name in required):
        return None
    paths = {name: _owned_path(layout, artifacts[name]["path"], name) for name in required}
    proposals = _read_object(paths["edit_proposals"], "edit proposals")
    effect_plan = _read_object(paths["effect_plan"], "effect plan")
    template = _read_object(paths["decision_template"], "decision template")
    validate_artifact(package_root, "edit_proposals", proposals)
    validate_artifact(package_root, "effect_plan", effect_plan)
    validate_artifact(package_root, "edit_review_decisions", template)
    return PlanOutputs(
        proposals_path=paths["edit_proposals"],
        effect_plan_path=paths["effect_plan"],
        decision_template_path=paths["decision_template"],
        markdown_path=paths["review_markdown"],
        stage_key=stage_key,
    )


def plan_review_package(
    package_root: Path,
    layout: ProjectLayout,
    *,
    revision_id: str = "rev_001",
    policy: EditingPolicy | None = None,
    effect_specs: Sequence[Mapping[str, Any]] = (),
) -> PlanOutputs:
    selected_policy = policy or EditingPolicy.smart_dense()
    source_path, source_manifest_path, source_manifest = source_from_manifest(layout)
    del source_path
    transcript_path, transcript = _load_transcript(package_root, layout)
    silence_path, silence = _load_silence(package_root, layout)
    serialized_effects = json.loads(json.dumps([dict(spec) for spec in effect_specs]))
    stage_key = make_stage_key(
        "edit_plan",
        __version__,
        [
            str(source_manifest["sha256"]),
            sha256_file(transcript_path),
            sha256_file(silence_path),
        ],
        {
            "schema_versions": ["edit_proposals:1.0.0", "effect_plan:1.0"],
            "config_sha256": config_sha256(layout),
            "policy": selected_policy.as_dict(),
            "effect_specs": serialized_effects,
            "revision_id": revision_id,
        },
    )
    with ProjectLock(layout, stage="edit_plan", revision_id=revision_id):
        previous = load_stage_state(package_root, layout, "edit_plan", revision_id)
        cached = _cached_plan(package_root, layout, previous, stage_key)
        if cached is not None:
            return cached
        attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
        stage_dir = layout.staging / f"edit-plan-{stage_key[:16]}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state = begin_stage(
            package_root,
            layout,
            project_id=layout.root.name,
            revision_id=revision_id,
            stage="edit_plan",
            stage_key=stage_key,
            staging_paths=[stage_dir],
            previous=previous,
        )
        try:
            proposals = build_edit_proposals(
                package_root=package_root,
                layout=layout,
                source_manifest_path=source_manifest_path,
                source_manifest=source_manifest,
                transcript_path=transcript_path,
                transcript=transcript,
                silence_path=silence_path,
                silence=silence,
                policy=selected_policy,
                revision_id=revision_id,
            )
            effect_plan = build_effect_plan(
                package_root=package_root,
                layout=layout,
                source_manifest_path=source_manifest_path,
                source_manifest=source_manifest,
                transcript_path=transcript_path,
                transcript=transcript,
                policy=selected_policy,
                effect_specs=serialized_effects,
                revision_id=revision_id,
            )
            proposals_path = layout.artifacts / f"edit-proposals-{stage_key[:16]}.json"
            effect_plan_path = layout.artifacts / f"effect-plan-{stage_key[:16]}.json"
            write_validated_artifact(package_root, "edit_proposals", proposals_path, proposals)
            write_validated_artifact(package_root, "effect_plan", effect_plan_path, effect_plan)
            _write_alias_if_absent(
                layout.artifacts / "edit-proposals.json", proposals, "edit_proposals", package_root
            )
            _write_alias_if_absent(
                layout.artifacts / "effect-plan.json", effect_plan, "effect_plan", package_root
            )
            template = _decision_template(proposals, proposals_path, revision_id)
            template_path = layout.review / f"edit-decisions-{stage_key[:16]}.json"
            write_validated_artifact(package_root, "edit_review_decisions", template_path, template)
            _write_alias_if_absent(
                layout.review / "edit-decisions.json",
                template,
                "edit_review_decisions",
                package_root,
            )
            markdown_path = layout.review / f"edit-plan-{stage_key[:16]}.md"
            write_text_atomically(markdown_path, _edit_plan_markdown(proposals, effect_plan))
            if not (layout.review / "edit-plan.md").exists():
                write_text_atomically(
                    layout.review / "edit-plan.md", _edit_plan_markdown(proposals, effect_plan)
                )
            warnings = list(proposals.get("warnings", [])) + list(effect_plan.get("warnings", []))
            complete_stage(
                package_root,
                layout,
                state,
                artifacts={
                    "edit_proposals": proposals_path,
                    "effect_plan": effect_plan_path,
                    "decision_template": template_path,
                    "review_markdown": markdown_path,
                },
                warnings=list(dict.fromkeys(str(item) for item in warnings)),
            )
            _mark_project_planning(package_root, layout)
            return PlanOutputs(
                proposals_path=proposals_path,
                effect_plan_path=effect_plan_path,
                decision_template_path=template_path,
                markdown_path=markdown_path,
                stage_key=stage_key,
            )
        except Exception as exc:
            if isinstance(
                exc, (ApprovalRequiredError, StaleApprovalError, PlanningValidationError)
            ):
                code, message = exc.code, exc.message
            else:
                code, message = "planning_failed", str(exc)
            fail_stage(package_root, layout, state, code=code, message=message)
            if isinstance(exc, Exception):
                raise
            raise


def import_edit_decisions(
    package_root: Path,
    layout: ProjectLayout,
    decisions_path: Path,
    *,
    revision_id: str = "rev_001",
    require_complete: bool = True,
) -> Path:
    decisions_path = decisions_path.expanduser().resolve()
    try:
        decisions_path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError("decision file must be inside the project") from exc
    decisions = _read_object(decisions_path, "edit decisions")
    validate_artifact(package_root, "edit_review_decisions", decisions)
    if (
        decisions.get("project_id") != layout.root.name
        or decisions.get("revision_id") != revision_id
    ):
        raise PlanningValidationError(
            "decision project or revision does not match the active project"
        )
    _proposal_path, proposals = _locate_proposals(layout, str(decisions["proposal_set_sha256"]))
    validate_artifact(package_root, "edit_proposals", proposals)
    if decisions["proposal_set_artifact_id"] != proposals["artifact_id"]:
        raise PlanningValidationError("decision proposal set artifact does not match")
    proposal_by_id = {str(item["proposal_id"]): item for item in proposals.get("proposals", [])}
    seen: set[str] = set()
    protected_values = proposals.get("protected_ranges", [])
    if isinstance(protected_values, list):
        protected = [
            (int(item["start_us"]), int(item["end_us"]))
            for item in protected_values
            if isinstance(item, dict)
        ]
    else:
        protected, _ = protected_ranges(
            _read_object(layout.artifacts / "transcript.json", "transcript"),
            EditingPolicy(),
            int(proposals["source_duration_us"]),
        )
    protected = _remove_protected_overrides(
        protected,
        _protected_range_overrides(decisions, int(proposals["source_duration_us"])),
    )
    for decision in decisions.get("decisions", []):
        proposal_id = str(decision["proposal_id"])
        if proposal_id in seen:
            raise PlanningValidationError(f"duplicate decision for {proposal_id}")
        seen.add(proposal_id)
        proposal = proposal_by_id.get(proposal_id)
        if proposal is None:
            raise PlanningValidationError(f"unknown proposal identifier: {proposal_id}")
        if decision["proposal_sha256"] != canonical_sha256(proposal):
            raise StaleApprovalError(f"proposal hash mismatch for {proposal_id}")
        selected = decision.get("modified_cut_range")
        if decision["decision"] == "modify":
            if not isinstance(selected, dict):
                raise PlanningValidationError(f"modified decision lacks a range: {proposal_id}")
            selected_range = _time_range(
                selected.get("start_us"),
                selected.get("end_us"),
                int(proposals["source_duration_us"]),
                f"modified range for {proposal_id}",
            )
            if _range_overlaps(selected_range["start_us"], selected_range["end_us"], protected):
                raise PlanningValidationError(
                    f"modified range for {proposal_id} overlaps protected content"
                )
        elif selected is not None:
            raise PlanningValidationError(
                f"only modify decisions may contain a modified range: {proposal_id}"
            )
    missing = sorted(set(proposal_by_id) - seen)
    if require_complete and missing:
        raise PlanningValidationError(f"missing decisions for proposals: {', '.join(missing)}")
    output_hash = sha256_file(decisions_path)
    imported_path = layout.artifacts / f"edit-review-decisions-{output_hash[:16]}.json"
    write_validated_artifact(package_root, "edit_review_decisions", imported_path, decisions)
    return imported_path


def _bundle_inputs(
    package_root: Path,
    layout: ProjectLayout,
    decisions_path: Path,
    effect_plan_path: Path,
    asset_hashes: Mapping[str, str],
    focus_pacing_plan_path: Path | None = None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    decisions = _read_object(decisions_path, "edit decisions")
    proposal_path, proposals = _locate_proposals(layout, str(decisions["proposal_set_sha256"]))
    validate_artifact(package_root, "edit_proposals", proposals)
    _source, source_manifest_path, source_manifest = source_from_manifest(layout)
    transcript_path, _transcript = _load_transcript(package_root, layout)
    policy_hash = str(
        proposals.get("policy_sha256")
        or canonical_sha256(
            {
                "policy_id": proposals["policy_id"],
                "policy_version": proposals["policy_version"],
            }
        )
    )
    bound: dict[str, str] = {
        "art_source": str(source_manifest["sha256"]),
        "art_source_manifest": sha256_file(source_manifest_path),
        "art_transcript": sha256_file(transcript_path),
        "art_policy": policy_hash,
        "art_edit_proposals": sha256_file(proposal_path),
        "art_edit_decisions": sha256_file(decisions_path),
        "art_effect_plan": sha256_file(effect_plan_path),
    }
    if focus_pacing_plan_path is not None:
        bound["art_focus_pacing"] = sha256_file(focus_pacing_plan_path)
    bound.update({f"asset_{asset_id}": digest for asset_id, digest in sorted(asset_hashes.items())})
    inputs = [{"artifact_id": key, "sha256": digest} for key, digest in sorted(bound.items())]
    return bound, inputs


def _effect_asset_hashes(effect_plan: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for effect in effect_plan.get("effects", []):
        if not isinstance(effect, dict):
            continue
        for asset in effect.get("asset_refs", []):
            if not isinstance(asset, dict):
                continue
            asset_id = str(asset.get("asset_id", ""))
            digest = str(asset.get("sha256", ""))
            if not asset_id or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise PlanningValidationError("effect plan contains an invalid asset hash")
            previous = found.get(asset_id)
            if previous is not None and previous != digest:
                raise PlanningValidationError(f"asset {asset_id} has conflicting hashes")
            found[asset_id] = digest
    return found


def _merge_asset_hashes(
    effect_plan: Mapping[str, Any], selected: Mapping[str, str] | None
) -> dict[str, str]:
    merged = _effect_asset_hashes(effect_plan)
    for asset_id, digest in (selected or {}).items():
        digest = str(digest)
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PlanningValidationError(f"asset {asset_id} is not hash-bound")
        if asset_id in merged and merged[asset_id] != digest:
            raise StaleApprovalError(f"selected asset hash changed for {asset_id}")
        merged[str(asset_id)] = digest
    return merged


def _effect_plan_for_approval(layout: ProjectLayout, approval_path: Path) -> Path:
    approval = _read_object(approval_path, "Gate 1 approval")
    expected_hash = next(
        (
            str(item["sha256"])
            for item in approval.get("inputs", [])
            if isinstance(item, dict) and item.get("artifact_id") == "art_effect_plan"
        ),
        None,
    )
    candidates = sorted(layout.artifacts.glob("effect-plan*.json"))
    if expected_hash is not None:
        for candidate in candidates:
            if sha256_file(candidate) == expected_hash:
                return candidate
        raise StaleApprovalError("Gate 1 approval references a missing effect plan")
    alias = layout.artifacts / "effect-plan.json"
    if alias.is_file():
        return alias
    if candidates:
        return candidates[-1]
    raise PlanningValidationError("effect plan is missing")


def create_gate1_approval(
    package_root: Path,
    layout: ProjectLayout,
    decisions_path: Path,
    effect_plan_path: Path,
    *,
    actor: str,
    role: str = "editor",
    reason: str = "Gate 1 approved after edit and effect plan review",
    asset_hashes: Mapping[str, str] | None = None,
    revision_id: str = "rev_001",
    focus_pacing_plan_path: Path | None = None,
) -> Path:
    imported_decisions = import_edit_decisions(
        package_root, layout, decisions_path, revision_id=revision_id, require_complete=True
    )
    effect_plan_path = effect_plan_path.expanduser().resolve()
    try:
        effect_plan_path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError("effect plan must be inside the project") from exc
    effect_plan = _read_object(effect_plan_path, "effect plan")
    validate_artifact(package_root, "effect_plan", effect_plan)
    if (
        effect_plan.get("project_id") != layout.root.name
        or effect_plan.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("effect plan project or revision does not match")
    selected_focus_plan: Path | None = None
    if focus_pacing_plan_path is not None:
        selected_focus_plan = focus_pacing_plan_path.expanduser().resolve()
        try:
            selected_focus_plan.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise PlanningValidationError("focus pacing plan must be inside the project") from exc
        focus_plan = _read_object(selected_focus_plan, "focus pacing plan")
        validate_artifact(package_root, "focus_pacing_plan", focus_plan)
        if (
            focus_plan.get("project_id") != layout.root.name
            or focus_plan.get("revision_id") != revision_id
        ):
            raise PlanningValidationError("focus pacing plan project or revision does not match")
    decisions = _read_object(imported_decisions, "imported edit decisions")
    _proposal_path, proposals = _locate_proposals(layout, str(decisions["proposal_set_sha256"]))
    protected = [
        (int(item["start_us"]), int(item["end_us"]))
        for item in proposals.get("protected_ranges", [])
        if isinstance(item, dict)
    ]
    _selected_cut_ranges(proposals, decisions, protected)
    selected_asset_hashes = _merge_asset_hashes(effect_plan, asset_hashes)
    bound, inputs = _bundle_inputs(
        package_root,
        layout,
        imported_decisions,
        effect_plan_path,
        selected_asset_hashes,
        selected_focus_plan,
    )
    bundle_hash = canonical_sha256(bound)
    payload = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": "art_gate1_approval",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("gate1-approval", "human-review", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        "approval_id": f"apr_gate1_{bundle_hash[:16]}",
        "approval_type": "edit",
        "actor": actor,
        "role": role,
        "decision": "approved",
        "reason": reason,
        "approved_item_type": "gate1_plan_bundle",
        "approved_item_sha256": bundle_hash,
        "expires_at": None,
        "budget": None,
    }
    validate_artifact(package_root, "approval_record", payload)
    output = layout.review / f"gate1-approval-{bundle_hash[:16]}.json"
    if output.is_file():
        existing = _read_object(output, "Gate 1 approval")
        validate_artifact(package_root, "approval_record", existing)
        if existing.get("approved_item_sha256") != bundle_hash:
            raise StaleApprovalError("existing Gate 1 approval path contains a different bundle")
    else:
        write_validated_artifact(package_root, "approval_record", output, payload)
    return output


def validate_gate1_approval(
    package_root: Path,
    layout: ProjectLayout,
    approval_path: Path,
    decisions_path: Path,
    effect_plan_path: Path,
    *,
    asset_hashes: Mapping[str, str] | None = None,
    revision_id: str = "rev_001",
    focus_pacing_plan_path: Path | None = None,
) -> dict[str, Any]:
    approval_path = approval_path.expanduser().resolve()
    try:
        approval_path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError("Gate 1 approval must be inside the project") from exc
    imported_decisions = import_edit_decisions(
        package_root,
        layout,
        decisions_path,
        revision_id=revision_id,
        require_complete=True,
    )
    decisions_path = imported_decisions
    approval = _read_object(approval_path, "Gate 1 approval")
    validate_artifact(package_root, "approval_record", approval)
    if approval.get("approval_type") != "edit" or approval.get("decision") != "approved":
        raise ApprovalRequiredError("Gate 1 approval is not an approved edit approval")
    if approval.get("project_id") != layout.root.name or approval.get("revision_id") != revision_id:
        raise StaleApprovalError("Gate 1 approval project or revision does not match")
    effect_plan = _read_object(effect_plan_path, "effect plan")
    validate_artifact(package_root, "effect_plan", effect_plan)
    if (
        effect_plan.get("project_id") != layout.root.name
        or effect_plan.get("revision_id") != revision_id
    ):
        raise StaleApprovalError("effect plan project or revision does not match")
    selected_focus_plan: Path | None = None
    if focus_pacing_plan_path is not None:
        selected_focus_plan = focus_pacing_plan_path.expanduser().resolve()
        try:
            selected_focus_plan.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise StaleApprovalError("focus pacing plan must be inside the project") from exc
        focus_plan = _read_object(selected_focus_plan, "focus pacing plan")
        validate_artifact(package_root, "focus_pacing_plan", focus_plan)
        if (
            focus_plan.get("project_id") != layout.root.name
            or focus_plan.get("revision_id") != revision_id
        ):
            raise StaleApprovalError("focus pacing plan project or revision does not match")
    selected_asset_hashes = _merge_asset_hashes(effect_plan, asset_hashes)
    _bundle, inputs = _bundle_inputs(
        package_root,
        layout,
        decisions_path,
        effect_plan_path,
        selected_asset_hashes,
        selected_focus_plan,
    )
    bound = {item["artifact_id"]: item["sha256"] for item in inputs}
    expected = canonical_sha256(bound)
    if approval.get("approved_item_sha256") != expected:
        raise StaleApprovalError("Gate 1 approval is stale for the current source and plans")
    if approval.get("inputs") != inputs:
        raise StaleApprovalError("Gate 1 approval inputs do not match current artifact hashes")
    return approval


def _selected_cut_ranges(
    proposals: Mapping[str, Any],
    decisions: Mapping[str, Any],
    protected: Sequence[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    protected = _remove_protected_overrides(
        protected,
        _protected_range_overrides(decisions, int(proposals["source_duration_us"])),
    )
    proposal_by_id = {str(item["proposal_id"]): item for item in proposals.get("proposals", [])}
    selected: list[tuple[int, int, str]] = []
    for decision in decisions.get("decisions", []):
        if decision["decision"] == "reject":
            continue
        proposal = proposal_by_id.get(str(decision["proposal_id"]))
        if proposal is None:
            raise PlanningValidationError(f"unknown proposal {decision['proposal_id']}")
        selected_value = (
            decision.get("modified_cut_range")
            if decision["decision"] == "modify"
            else proposal["proposed_cut_range"]
        )
        if not isinstance(selected_value, dict):
            raise PlanningValidationError(
                f"approved decision lacks a cut range: {decision['proposal_id']}"
            )
        start_us = int(selected_value["start_us"])
        end_us = int(selected_value["end_us"])
        _time_range(start_us, end_us, int(proposals["source_duration_us"]), "approved cut")
        if _range_overlaps(start_us, end_us, protected):
            raise PlanningValidationError(
                f"approved cut overlaps protected content: {decision['proposal_id']}"
            )
        selected.append((start_us, end_us, str(decision["proposal_id"])))
    ordered = sorted(selected)
    for previous, current in pairwise(ordered):
        if current[0] < previous[1]:
            raise PlanningValidationError(
                f"approved cut ranges overlap: {previous[2]} and {current[2]}"
            )
    return ordered


def _compile_keep_ranges(
    duration_us: int, selected: Sequence[tuple[int, int, str]], approval_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    keep_ranges: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    cursor = 0
    output_cursor = 0
    for index, (start_us, end_us, proposal_id) in enumerate(selected, start=1):
        if start_us > cursor:
            length = start_us - cursor
            keep_ranges.append(
                {
                    "segment_id": f"keep_{len(keep_ranges) + 1:06d}",
                    "source_start_us": cursor,
                    "source_end_us": start_us,
                    "output_start_us": output_cursor,
                    "output_end_us": output_cursor + length,
                }
            )
            output_cursor += length
        deletions.append(
            {
                "deletion_id": f"del_{index:06d}",
                "source_start_us": start_us,
                "source_end_us": end_us,
                "proposal_ids": [proposal_id],
                "approval_ids": [approval_id],
                "reason": "Approved Gate 1 edit decision",
            }
        )
        cursor = end_us
    if cursor < duration_us:
        length = duration_us - cursor
        keep_ranges.append(
            {
                "segment_id": f"keep_{len(keep_ranges) + 1:06d}",
                "source_start_us": cursor,
                "source_end_us": duration_us,
                "output_start_us": output_cursor,
                "output_end_us": output_cursor + length,
            }
        )
        output_cursor += length
    if not keep_ranges:
        raise PlanningValidationError("approved cuts remove the entire source")
    return keep_ranges, deletions, output_cursor


def compile_approved_edl(
    package_root: Path,
    layout: ProjectLayout,
    decisions_path: Path,
    gate1_approval_path: Path,
    *,
    revision_id: str = "rev_001",
    asset_hashes: Mapping[str, str] | None = None,
    focus_pacing_plan_path: Path | None = None,
) -> Path:
    imported = import_edit_decisions(
        package_root, layout, decisions_path, revision_id=revision_id, require_complete=True
    )
    decisions = _read_object(imported, "imported edit decisions")
    proposal_path, proposals = _locate_proposals(layout, str(decisions["proposal_set_sha256"]))
    validate_artifact(package_root, "edit_proposals", proposals)
    effect_plan_path = _effect_plan_for_approval(layout, gate1_approval_path)
    validate_gate1_approval(
        package_root,
        layout,
        gate1_approval_path,
        imported,
        effect_plan_path,
        asset_hashes=asset_hashes,
        revision_id=revision_id,
        focus_pacing_plan_path=focus_pacing_plan_path,
    )
    protected_values = proposals.get("protected_ranges", [])
    protected = [
        (int(item["start_us"]), int(item["end_us"]))
        for item in protected_values
        if isinstance(item, dict)
    ]
    selected = _selected_cut_ranges(proposals, decisions, protected)
    approval = _read_object(gate1_approval_path, "Gate 1 approval")
    keep_ranges, deletions, output_duration_us = _compile_keep_ranges(
        int(proposals["source_duration_us"]), selected, str(approval["approval_id"])
    )
    mapping = [
        {
            "source_start_us": int(item["source_start_us"]),
            "source_end_us": int(item["source_end_us"]),
            "output_start_us": int(item["output_start_us"]),
            "output_end_us": int(item["output_end_us"]),
        }
        for item in keep_ranges
    ]
    payload = {
        "schema_name": "edit_decision_list",
        "schema_version": "1.0.0",
        "artifact_id": "art_edl",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("edl-compile", "gate1-review-decision-compiler", __version__),
        "inputs": [
            artifact_input("art_proposals", proposal_path),
            artifact_input("art_edit_decisions", imported),
            artifact_input("art_gate1_approval", gate1_approval_path),
        ],
        "config_sha256": config_sha256(layout),
        "source_duration_us": int(proposals["source_duration_us"]),
        "expected_output_duration_us": output_duration_us,
        "keep_ranges": keep_ranges,
        "source_to_output_mapping": mapping,
        "deletions": deletions,
        "approval_record_ids": [str(approval["approval_id"])],
        "policy_id": proposals["policy_id"],
        "policy_version": proposals["policy_version"],
    }
    validate_artifact(package_root, "edit_decision_list", payload)
    output = layout.artifacts / "edit-decision-list.json"
    write_validated_artifact(package_root, "edit_decision_list", output, payload)
    return output


def compile_edl(
    package_root: Path,
    layout: ProjectLayout,
    decisions_path: Path,
    revision_id: str = "rev_001",
    gate1_approval_path: Path | None = None,
    focus_pacing_plan_path: Path | None = None,
) -> Path:
    """Compatibility entry point; approved cuts still require Gate 1."""

    if gate1_approval_path is None:
        decisions = _read_object(decisions_path, "edit decisions")
        has_approved_cut = any(
            item.get("decision") in {"approve", "modify"}
            for item in decisions.get("decisions", [])
            if isinstance(item, dict)
        )
        if has_approved_cut:
            raise ApprovalRequiredError("approved cuts require an explicit Gate 1 approval")
        imported = import_edit_decisions(
            package_root, layout, decisions_path, revision_id=revision_id, require_complete=True
        )
        proposal_path, proposals = _locate_proposals(
            layout, str(_read_object(imported, "imported decisions")["proposal_set_sha256"])
        )
        duration_us = int(proposals["source_duration_us"])
        keep_ranges, deletions, output_duration_us = _compile_keep_ranges(
            duration_us, [], "art_edit_review"
        )
        mapping = [
            {
                "source_start_us": int(item["source_start_us"]),
                "source_end_us": int(item["source_end_us"]),
                "output_start_us": int(item["output_start_us"]),
                "output_end_us": int(item["output_end_us"]),
            }
            for item in keep_ranges
        ]
        payload = {
            "schema_name": "edit_decision_list",
            "schema_version": "1.0.0",
            "artifact_id": "art_edl",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "created_at": now_iso(),
            "producer": producer("edl-compile", "no-cut-review-decision-compiler", __version__),
            "inputs": [
                artifact_input("art_proposals", proposal_path),
                artifact_input("art_edit_decisions", imported),
            ],
            "config_sha256": config_sha256(layout),
            "source_duration_us": duration_us,
            "expected_output_duration_us": output_duration_us,
            "keep_ranges": keep_ranges,
            "source_to_output_mapping": mapping,
            "deletions": deletions,
            "approval_record_ids": ["art_edit_review"],
            "policy_id": proposals["policy_id"],
            "policy_version": proposals["policy_version"],
        }
        validate_artifact(package_root, "edit_decision_list", payload)
        output = layout.artifacts / "edit-decision-list.json"
        write_validated_artifact(package_root, "edit_decision_list", output, payload)
        return output
    return compile_approved_edl(
        package_root,
        layout,
        decisions_path,
        gate1_approval_path,
        revision_id=revision_id,
        focus_pacing_plan_path=focus_pacing_plan_path,
    )


def plan_silence_edits(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str = "rev_001",
) -> tuple[Path, Path]:
    """Legacy wrapper that now emits the complete P3 review package."""

    outputs = plan_review_package(package_root, layout, revision_id=revision_id)
    return outputs.proposals_path, outputs.decision_template_path
