from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import yaml

from videoedit import __version__
from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import (
    artifact_input,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout

TransitionPurpose = str
TransitionType = str

ALLOWED_PURPOSES = frozenset(
    {
        "new_point",
        "new_chapter",
        "mode_change",
        "talking_head_to_major_demo",
        "major_demo_to_talking_head",
        "comparison",
        "before_after",
        "location_change",
        "return_from_visual_explanation",
    }
)
ALLOWED_TRANSITION_TYPES = frozenset(
    {
        "hard_cut",
        "j_cut",
        "l_cut",
        "short_crossfade",
        "dip_to_color",
        "swipe_left",
        "swipe_right",
        "push_left",
        "push_right",
        "blur_swipe",
        "chapter_transition",
    }
)
MOTION_TRANSITION_TYPES = frozenset(
    {
        "dip_to_color",
        "swipe_left",
        "swipe_right",
        "push_left",
        "push_right",
        "blur_swipe",
        "chapter_transition",
    }
)
_PURPOSE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\bchapter\b|\bpart\s+[0-9a-z]+\b", "new_chapter"),
    (r"before\s+and\s+after|\bversus\b|\bvs\.?\b|compared\s+with", "comparison"),
    (r"before\s+we|after\s+we|before\s+and|after\s+the", "before_after"),
    (
        r"now\s+let'?s|next\s+(?:we|up)|moving\s+on|another\s+point|the\s+key\s+is",
        "new_point",
    ),
    (r"let'?s\s+(?:look|see|demonstrate|walk)\b|here'?s\s+how", "new_point"),
)
_PURPOSE_TRANSITION_DEFAULTS: dict[str, str] = {
    "new_point": "swipe_left",
    "new_chapter": "chapter_transition",
    "mode_change": "blur_swipe",
    "talking_head_to_major_demo": "push_left",
    "major_demo_to_talking_head": "push_right",
    "comparison": "short_crossfade",
    "before_after": "push_left",
    "location_change": "dip_to_color",
    "return_from_visual_explanation": "push_right",
}
_DEFAULT_DIRECTION: dict[str, str] = {
    "swipe_left": "left",
    "swipe_right": "right",
    "push_left": "left",
    "push_right": "right",
    "blur_swipe": "left",
    "chapter_transition": "none",
    "dip_to_color": "none",
    "short_crossfade": "none",
    "j_cut": "none",
    "l_cut": "none",
    "hard_cut": "none",
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class TransitionPolicy:
    policy_id: str = "purposeful_structural"
    version: int = 1
    default_fallback: str = "hard_cut"
    routine_cleanup_transition: str = "hard_cut"
    require_structural_boundary: bool = True
    maximum_motion_transitions_per_minute: int = 1
    minimum_motion_transition_spacing_us: int = 12_000_000
    minimum_dialogue_clearance_us: int = 60_000
    auto_eligible_min: float = 0.95
    review_min: float = 0.82
    timing_easing: str = "smooth_ease_in_out"
    require_full_frame_coverage: bool = True
    preserve_first_readable_incoming_frame: bool = True
    styles: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.policy_id):
            raise ValueError("transition policy id is not a valid identifier")
        if self.version < 1:
            raise ValueError("transition policy version must be positive")
        if self.default_fallback not in {"hard_cut", "j_cut", "l_cut", "short_crossfade"}:
            raise ValueError("transition policy fallback must be a clean-cut strategy")
        if self.maximum_motion_transitions_per_minute < 0:
            raise ValueError("maximum transition frequency must be nonnegative")
        if self.minimum_motion_transition_spacing_us < 0:
            raise ValueError("transition spacing must be nonnegative")
        if self.minimum_dialogue_clearance_us < 0:
            raise ValueError("dialogue clearance must be nonnegative")
        if not 0 <= self.review_min <= self.auto_eligible_min <= 1:
            raise ValueError("transition confidence thresholds must be ordered in [0, 1]")

    @classmethod
    def from_yaml(cls, path: Path) -> TransitionPolicy:
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PlanningValidationError(f"transition policy is unreadable: {path}") from exc
        if not isinstance(value, Mapping):
            raise PlanningValidationError("transition policy YAML must contain an object")
        raw_policy = value.get("transition_policy")
        if not isinstance(raw_policy, Mapping):
            raise PlanningValidationError("transition policy YAML lacks transition_policy")
        timing = value.get("timing")
        timing_value = timing if isinstance(timing, Mapping) else {}
        confidence = value.get("confidence")
        confidence_value = confidence if isinstance(confidence, Mapping) else {}
        styles_value = value.get("styles")
        styles = (
            {
                str(key): item
                for key, item in styles_value.items()
                if isinstance(key, str) and isinstance(item, Mapping)
            }
            if isinstance(styles_value, Mapping)
            else {}
        )
        return cls(
            policy_id=str(raw_policy.get("id", "purposeful_structural")),
            version=_int(raw_policy.get("version", 1), 1),
            default_fallback=str(raw_policy.get("default_fallback", "hard_cut")),
            routine_cleanup_transition=str(
                raw_policy.get("routine_cleanup_transition", "hard_cut")
            ),
            require_structural_boundary=bool(raw_policy.get("require_structural_boundary", True)),
            maximum_motion_transitions_per_minute=_int(
                raw_policy.get("maximum_motion_transitions_per_minute", 1), 1
            ),
            minimum_motion_transition_spacing_us=round(
                _number(raw_policy.get("minimum_motion_transition_spacing_seconds"), 12.0)
                * 1_000_000
            ),
            minimum_dialogue_clearance_us=round(
                _number(timing_value.get("minimum_dialogue_clearance_ms"), 60.0) * 1_000
            ),
            auto_eligible_min=_number(confidence_value.get("auto_eligible_min"), 0.95),
            review_min=_number(confidence_value.get("review_min"), 0.82),
            timing_easing=str(timing_value.get("easing", "smooth_ease_in_out")),
            require_full_frame_coverage=bool(timing_value.get("require_full_frame_coverage", True)),
            preserve_first_readable_incoming_frame=bool(
                timing_value.get("preserve_first_readable_incoming_frame", True)
            ),
            styles=styles,
        )

    def style(self, transition_type: str) -> Mapping[str, object]:
        value = self.styles.get(transition_type, {})
        return value if isinstance(value, Mapping) else {}

    def requires_sound(self, transition_type: str) -> bool:
        return self.style(transition_type).get("sound") == "required"


def _number(value: object, default: float) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: object, default: float) -> float:
    return max(0.0, min(1.0, _number(value, default)))


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _segment_values(segments: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for segment in segments:
        segment_id = _text(segment.get("segment_id"))
        start_us = _int(segment.get("start_us"), -1)
        end_us = _int(segment.get("end_us"), -1)
        if (
            not segment_id
            or not _IDENTIFIER.fullmatch(segment_id)
            or start_us < 0
            or end_us <= start_us
        ):
            continue
        normalized.append(
            {
                **dict(segment),
                "segment_id": segment_id,
                "start_us": start_us,
                "end_us": end_us,
            }
        )
    return sorted(normalized, key=lambda item: (_int(item["start_us"]), str(item["segment_id"])))


def _word_edges(
    transcript: Mapping[str, object] | None,
) -> tuple[dict[str, int], dict[str, int]]:
    first_by_segment: dict[str, int] = {}
    last_by_segment: dict[str, int] = {}
    if transcript is None:
        return first_by_segment, last_by_segment
    words_value = transcript.get("words", [])
    if not isinstance(words_value, list):
        return first_by_segment, last_by_segment
    for word in words_value:
        if not isinstance(word, Mapping):
            continue
        segment_id = _text(word.get("segment_id"))
        start_us = _int(word.get("start_us"), -1)
        end_us = _int(word.get("end_us"), -1)
        if not segment_id or start_us < 0 or end_us <= start_us:
            continue
        first_by_segment[segment_id] = min(start_us, first_by_segment.get(segment_id, start_us))
        last_by_segment[segment_id] = max(end_us, last_by_segment.get(segment_id, end_us))
    return first_by_segment, last_by_segment


def _mode(segment: Mapping[str, object]) -> str:
    for key in ("visual_mode", "mode", "segment_type", "coverage_type"):
        value = _text(segment.get(key)).casefold()
        if value:
            return value
    return ""


def _mode_purpose(outgoing: str, incoming: str) -> str | None:
    if not outgoing or not incoming or outgoing == incoming:
        return None
    outgoing_demo = outgoing in {"demo", "major_demo", "screen_demo", "visual_explanation"}
    incoming_demo = incoming in {"demo", "major_demo", "screen_demo", "visual_explanation"}
    if outgoing in {"talking_head", "interview", "host"} and incoming_demo:
        return "talking_head_to_major_demo"
    if outgoing_demo and incoming in {"talking_head", "interview", "host"}:
        return "major_demo_to_talking_head"
    if "location" in outgoing or "location" in incoming:
        return "location_change"
    return "mode_change"


def _marker_purpose(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    for pattern, purpose in _PURPOSE_MARKERS:
        if re.search(pattern, normalized):
            return purpose
    return None


def _default_components(
    *,
    structural: float,
    timing: float,
    visual: float,
    dialogue: float,
    sound: float,
) -> dict[str, float]:
    return {
        "structural_boundary": round(_clamp(structural, 0.0), 4),
        "timing": round(_clamp(timing, 0.0), 4),
        "visual_fit": round(_clamp(visual, 0.0), 4),
        "dialogue_safety": round(_clamp(dialogue, 0.0), 4),
        "sound_sync": round(_clamp(sound, 0.0), 4),
    }


def _components(value: object, defaults: Mapping[str, float]) -> dict[str, float]:
    raw = value if isinstance(value, Mapping) else {}
    return {key: round(_clamp(raw.get(key), default), 4) for key, default in defaults.items()}


def _confidence(components: Mapping[str, float]) -> float:
    return round(min(components.values()), 4) if components else 0.0


def _preferred_type(purpose: str, value: object) -> str:
    selected = _text(value)
    if selected in ALLOWED_TRANSITION_TYPES:
        return selected
    return _PURPOSE_TRANSITION_DEFAULTS.get(purpose, "hard_cut")


def _evidence_ids(value: object, fallback: str) -> list[str]:
    if isinstance(value, list):
        selected = [str(item) for item in value if _IDENTIFIER.fullmatch(str(item))]
        if selected:
            return selected
    return [fallback]


def _make_boundary(
    raw: Mapping[str, object],
    *,
    index: int,
    outgoing: Mapping[str, object] | None,
    incoming: Mapping[str, object] | None,
    first_by_segment: Mapping[str, int],
    last_by_segment: Mapping[str, int],
    policy: TransitionPolicy,
    explicit: bool,
    inferred_purpose: str | None = None,
    inferred_transcript_evidence: str = "",
    inferred_visual_evidence: str = "",
) -> dict[str, object] | None:
    outgoing_id = _text(raw.get("outgoing_segment_id")) or (
        _text(outgoing.get("segment_id")) if outgoing is not None else ""
    )
    incoming_id = _text(raw.get("incoming_segment_id")) or (
        _text(incoming.get("segment_id")) if incoming is not None else ""
    )
    boundary_us = _int(
        raw.get("boundary_us"),
        _int(incoming.get("start_us"), -1) if incoming is not None else -1,
    )
    purpose = _text(raw.get("purpose")) or (inferred_purpose or "")
    if (
        not _IDENTIFIER.fullmatch(outgoing_id)
        or not _IDENTIFIER.fullmatch(incoming_id)
        or boundary_us < 0
        or purpose not in ALLOWED_PURPOSES
    ):
        return None
    boundary_id = _text(raw.get("boundary_id")) or f"bnd_{index:06d}"
    if not _IDENTIFIER.fullmatch(boundary_id):
        boundary_id = f"bnd_{index:06d}"
    first_incoming = _int(raw.get("first_incoming_word_us"), first_by_segment.get(incoming_id, -1))
    last_outgoing = _int(raw.get("last_outgoing_word_us"), last_by_segment.get(outgoing_id, -1))
    first_incoming_value: int | None = first_incoming if first_incoming >= 0 else None
    last_outgoing_value: int | None = last_outgoing if last_outgoing >= 0 else None
    transcript_evidence = _text(raw.get("transcript_evidence")) or inferred_transcript_evidence
    visual_evidence = _text(raw.get("visual_evidence")) or inferred_visual_evidence
    if not transcript_evidence:
        transcript_evidence = "Explicit operator-supplied structural boundary evidence."
    if not visual_evidence:
        visual_evidence = "No visual state evidence was supplied; motion remains review-bound."
    if explicit:
        defaults = _default_components(
            structural=0.98,
            timing=0.96,
            visual=0.92,
            dialogue=0.9 if first_incoming_value is not None else 0.65,
            sound=0.7,
        )
    else:
        defaults = _default_components(
            structural=0.92,
            timing=0.94,
            visual=0.82,
            dialogue=0.86 if first_incoming_value is not None else 0.6,
            sound=0.55,
        )
    confidence_components = _components(raw.get("confidence_components"), defaults)
    confidence = _clamp(raw.get("confidence"), _confidence(confidence_components))
    status = "verified" if confidence >= policy.review_min else "review_required"
    preferred = _preferred_type(purpose, raw.get("preferred_transition_type"))
    direction = _text(raw.get("direction")) or _DEFAULT_DIRECTION.get(preferred, "none")
    if direction not in {"none", "left", "right", "up", "down"}:
        direction = _DEFAULT_DIRECTION.get(preferred, "none")
    style = policy.style(preferred)
    default_duration = _int(style.get("minimum_duration_ms"), 0) * 1_000
    desired_duration_us = _int(raw.get("desired_duration_us"), default_duration)
    easing = _text(raw.get("easing")) or policy.timing_easing
    if easing not in {"linear", "ease_in", "ease_out", "ease_in_out", "smooth_ease_in_out"}:
        easing = policy.timing_easing
    return {
        "boundary_id": boundary_id,
        "boundary_us": boundary_us,
        "outgoing_segment_id": outgoing_id,
        "incoming_segment_id": incoming_id,
        "purpose": purpose,
        "transcript_evidence": transcript_evidence,
        "visual_evidence": visual_evidence,
        "evidence_ids": _evidence_ids(raw.get("evidence_ids"), f"ev_boundary_{index:06d}"),
        "first_incoming_word_us": first_incoming_value,
        "last_outgoing_word_us": last_outgoing_value,
        "preferred_transition_type": preferred,
        "direction": direction,
        "desired_duration_us": max(0, desired_duration_us),
        "easing": easing,
        "full_frame_coverage": bool(raw.get("full_frame_coverage", False)),
        "confidence_components": confidence_components,
        "confidence": round(confidence, 4),
        "explicit": explicit,
        "status": status,
    }


def detect_structural_boundaries(
    segments: Sequence[Mapping[str, object]],
    transcript: Mapping[str, object] | None = None,
    *,
    explicit_boundaries: Sequence[Mapping[str, object]] = (),
    policy: TransitionPolicy | None = None,
) -> list[dict[str, object]]:
    """Detect only evidence-backed boundaries between ordered output segments."""

    selected_policy = policy or TransitionPolicy()
    normalized_segments = _segment_values(segments)
    first_by_segment, last_by_segment = _word_edges(transcript)
    by_id = {str(segment["segment_id"]): segment for segment in normalized_segments}
    boundaries: list[dict[str, object]] = []
    explicit_keys: set[tuple[str, str, int]] = set()
    next_index = 1
    for raw in explicit_boundaries:
        if not isinstance(raw, Mapping):
            continue
        outgoing = by_id.get(_text(raw.get("outgoing_segment_id")))
        incoming = by_id.get(_text(raw.get("incoming_segment_id")))
        boundary = _make_boundary(
            raw,
            index=next_index,
            outgoing=outgoing,
            incoming=incoming,
            first_by_segment=first_by_segment,
            last_by_segment=last_by_segment,
            policy=selected_policy,
            explicit=True,
        )
        if boundary is None:
            continue
        next_index += 1
        boundaries.append(boundary)
        explicit_keys.add(
            (
                str(boundary["outgoing_segment_id"]),
                str(boundary["incoming_segment_id"]),
                _int(boundary["boundary_us"]),
            )
        )
    for outgoing, incoming in pairwise(normalized_segments):
        outgoing_id = str(outgoing["segment_id"])
        incoming_id = str(incoming["segment_id"])
        boundary_us = _int(incoming["start_us"])
        key = (outgoing_id, incoming_id, boundary_us)
        if key in explicit_keys:
            continue
        outgoing_mode = _mode(outgoing)
        incoming_mode = _mode(incoming)
        mode_purpose = _mode_purpose(outgoing_mode, incoming_mode)
        incoming_text = _text(incoming.get("text"))
        marker_purpose = _marker_purpose(incoming_text)
        purpose = mode_purpose or marker_purpose
        if purpose is None:
            continue
        if mode_purpose is not None:
            visual_evidence = (
                f"Approved coverage mode changes from {outgoing_mode} to {incoming_mode}."
            )
        else:
            visual_evidence = "No coverage mode change was supplied; motion requires review."
        transcript_evidence = (
            f"Incoming segment text contains evidence for {purpose}: {incoming_text[:240]}"
            if marker_purpose
            else f"Incoming segment coverage supports {purpose}."
        )
        raw = {
            "boundary_us": boundary_us,
            "purpose": purpose,
            "transcript_evidence": transcript_evidence,
            "visual_evidence": visual_evidence,
            "full_frame_coverage": mode_purpose is not None,
        }
        boundary = _make_boundary(
            raw,
            index=next_index,
            outgoing=outgoing,
            incoming=incoming,
            first_by_segment=first_by_segment,
            last_by_segment=last_by_segment,
            policy=selected_policy,
            explicit=False,
            inferred_purpose=purpose,
            inferred_transcript_evidence=transcript_evidence,
            inferred_visual_evidence=visual_evidence,
        )
        if boundary is not None:
            boundaries.append(boundary)
            next_index += 1
    return sorted(
        boundaries,
        key=lambda item: (
            _int(item["boundary_us"]),
            str(item["outgoing_segment_id"]),
            str(item["incoming_segment_id"]),
        ),
    )


def _style_duration(policy: TransitionPolicy, transition_type: str, desired: int) -> int:
    style = policy.style(transition_type)
    minimum = _int(style.get("minimum_duration_ms"), 0) * 1_000
    maximum = _int(style.get("maximum_duration_ms"), 0) * 1_000
    if maximum <= 0:
        return 0
    return max(minimum, min(maximum, desired if desired > 0 else minimum))


def _sound_for_boundary(
    boundary: Mapping[str, object],
    transition_id: str,
    sound_cues: Sequence[Mapping[str, object]],
) -> dict[str, object] | None:
    boundary_us = _int(boundary.get("boundary_us"))
    boundary_id = _text(boundary.get("boundary_id"))
    selected: Mapping[str, object] | None = None
    for cue in sound_cues:
        linked = _text(cue.get("linked_transition_id"))
        start_us = _int(cue.get("start_us"), -1)
        end_us = _int(cue.get("end_us"), -1)
        if linked in {transition_id, boundary_id} or (
            start_us >= 0 and end_us > start_us and start_us <= boundary_us <= end_us
        ):
            selected = cue
            break
    if selected is None:
        return None
    asset_id = _text(selected.get("asset_id"))
    asset_sha256 = _text(selected.get("asset_sha256"))
    license_id = _text(selected.get("license_id"))
    if (
        not _IDENTIFIER.fullmatch(_text(selected.get("cue_id")))
        or not _IDENTIFIER.fullmatch(asset_id)
        or not _SHA256.fullmatch(asset_sha256)
        or not license_id
        or selected.get("approval_state") != "approved"
    ):
        return None
    if selected.get("qa_status") != "pass":
        return None
    start_us = _int(selected.get("start_us"), -1)
    end_us = _int(selected.get("end_us"), -1)
    sync_peak_us = _int(selected.get("sync_peak_us"), -1)
    if start_us < 0 or end_us <= start_us or sync_peak_us < start_us or sync_peak_us > end_us:
        return None
    sound: dict[str, object] = {
        "cue_id": _text(selected.get("cue_id")),
        "asset_id": asset_id,
        "asset_sha256": asset_sha256,
        "license_id": license_id,
        "start_us": start_us,
        "end_us": end_us,
        "sync_peak_us": sync_peak_us,
        "gain_db": _number(selected.get("gain_db"), -16.0),
        "fade_in_us": max(0, _int(selected.get("fade_in_us"), 15_000)),
        "fade_out_us": max(0, _int(selected.get("fade_out_us"), 70_000)),
        "duck_speech": bool(selected.get("duck_speech", True)),
    }
    speech_protection = selected.get("speech_protection")
    if isinstance(speech_protection, Mapping) and speech_protection.get("status") == "fail":
        return None
    if selected.get("transient_alignment_status") == "pass" and (
        not isinstance(speech_protection, Mapping) or speech_protection.get("status") == "pass"
    ):
        sound["qa_status"] = "pass"
    for key in (
        "transient_alignment_tolerance_us",
        "speech_clearance_us",
        "mix_policy",
        "qa_status",
    ):
        if key in selected and selected[key] is not None:
            sound[key] = selected[key]
    return sound


def _dialogue_protection(
    boundary: Mapping[str, object],
    start_us: int,
    end_us: int,
    policy: TransitionPolicy,
) -> dict[str, object]:
    first_value = boundary.get("first_incoming_word_us")
    last_value = boundary.get("last_outgoing_word_us")
    first = _int(first_value, -1) if first_value is not None else -1
    last = _int(last_value, -1) if last_value is not None else -1
    before_clearance = start_us - last if last >= 0 else policy.minimum_dialogue_clearance_us
    after_clearance = first - end_us if first >= 0 else policy.minimum_dialogue_clearance_us
    minimum_clearance = max(0, min(before_clearance, after_clearance))
    speech_overlaps = (last >= 0 and last > start_us) or (first >= 0 and first < end_us)
    if speech_overlaps or minimum_clearance < policy.minimum_dialogue_clearance_us:
        risk = "high" if speech_overlaps else "medium"
    else:
        risk = "low"
    return {
        "speech_overlaps": speech_overlaps,
        "first_incoming_word_us": first if first >= 0 else None,
        "last_outgoing_word_us": last if last >= 0 else None,
        "minimum_clearance_us": minimum_clearance,
        "intelligibility_risk": risk,
    }


def _transition_components(
    boundary: Mapping[str, object],
    dialogue: Mapping[str, object],
    *,
    sound_ok: bool,
    motion: bool,
    policy: TransitionPolicy,
) -> dict[str, float]:
    raw = boundary.get("confidence_components")
    defaults = _default_components(
        structural=_clamp(boundary.get("confidence"), 0.0),
        timing=1.0,
        visual=1.0 if bool(boundary.get("full_frame_coverage")) else 0.55,
        dialogue=(1.0 if str(dialogue.get("intelligibility_risk")) == "low" else 0.35),
        sound=1.0 if sound_ok else 0.0,
    )
    components = _components(raw, defaults)
    if not motion:
        components["sound_sync"] = 1.0
    if str(dialogue.get("intelligibility_risk")) == "low":
        components["dialogue_safety"] = max(
            components["dialogue_safety"],
            min(
                1.0,
                policy.minimum_dialogue_clearance_us / max(1, policy.minimum_dialogue_clearance_us),
            ),
        )
    return components


def _plan_transitions(
    boundaries: Sequence[Mapping[str, object]],
    policy: TransitionPolicy,
    sound_cues: Sequence[Mapping[str, object]],
    *,
    output_duration_us: int | None,
) -> tuple[list[dict[str, object]], list[str]]:
    planned: list[dict[str, object]] = []
    warnings: list[str] = []
    motion_times: list[int] = []
    sorted_boundaries = sorted(
        boundaries,
        key=lambda item: (_int(item.get("boundary_us")), _text(item.get("boundary_id"))),
    )
    for boundary in sorted_boundaries:
        boundary_id = _text(boundary.get("boundary_id")) or f"bnd_{len(planned) + 1:06d}"
        transition_id = f"trn_{boundary_id}"
        boundary_us = _int(boundary.get("boundary_us"), -1)
        purpose = _text(boundary.get("purpose"))
        if purpose not in ALLOWED_PURPOSES or boundary_us < 0:
            warnings.append(f"fallback_invalid_boundary:{boundary_id}")
            continue
        preferred = _text(
            boundary.get("preferred_transition_type")
        ) or _PURPOSE_TRANSITION_DEFAULTS.get(purpose, "hard_cut")
        if preferred not in ALLOWED_TRANSITION_TYPES:
            warnings.append(f"fallback_unknown_transition_type:{boundary_id}")
            preferred = "hard_cut"
        wants_motion = preferred in MOTION_TRANSITION_TYPES
        dialogue = _dialogue_protection(boundary, boundary_us, boundary_us, policy)
        sound = _sound_for_boundary(boundary, transition_id, sound_cues) if wants_motion else None
        fallback_reasons: list[str] = []
        if policy.require_structural_boundary and boundary.get("status") == "rejected":
            fallback_reasons.append("boundary_rejected")
        if _clamp(boundary.get("confidence"), 0.0) < policy.review_min:
            fallback_reasons.append("boundary_confidence_below_review_threshold")
        if (
            wants_motion
            and policy.require_full_frame_coverage
            and not bool(boundary.get("full_frame_coverage", False))
        ):
            fallback_reasons.append("full_frame_coverage_not_verified")
        if (
            wants_motion
            and policy.preserve_first_readable_incoming_frame
            and boundary.get("first_incoming_word_us") is None
        ):
            fallback_reasons.append("first_incoming_word_not_verified")
        if wants_motion and policy.requires_sound(preferred) and sound is None:
            fallback_reasons.append("approved_licensed_sound_missing")
        if wants_motion and any(
            boundary_us - previous < policy.minimum_motion_transition_spacing_us
            for previous in motion_times
        ):
            fallback_reasons.append("minimum_motion_spacing_not_met")
        if wants_motion and policy.maximum_motion_transitions_per_minute >= 0:
            recent = [previous for previous in motion_times if boundary_us - previous < 60_000_000]
            if len(recent) >= policy.maximum_motion_transitions_per_minute:
                fallback_reasons.append("maximum_motion_frequency_reached")
        desired_duration = _int(boundary.get("desired_duration_us"), 0)
        duration_us = _style_duration(policy, preferred, desired_duration) if wants_motion else 0
        start_us = boundary_us
        end_us = start_us + duration_us
        first_incoming = _int(boundary.get("first_incoming_word_us"), -1)
        last_outgoing = _int(boundary.get("last_outgoing_word_us"), -1)
        if output_duration_us is not None and end_us > output_duration_us:
            fallback_reasons.append("transition_range_out_of_bounds")
        if wants_motion and first_incoming >= 0 and end_us > first_incoming:
            fallback_reasons.append("incoming_content_not_readable_in_time")
        if wants_motion and last_outgoing >= 0 and start_us < last_outgoing:
            fallback_reasons.append("outgoing_dialogue_not_clear")
        if wants_motion:
            dialogue = _dialogue_protection(boundary, start_us, end_us, policy)
            if (
                dialogue["speech_overlaps"]
                or _int(dialogue["minimum_clearance_us"]) < policy.minimum_dialogue_clearance_us
            ):
                fallback_reasons.append("dialogue_clearance_not_met")
        if fallback_reasons:
            transition_type = "hard_cut"
            start_us = boundary_us
            end_us = boundary_us
            duration_us = 0
            direction = "none"
            easing = "linear"
            sound = None
            sound_sync_status = "not_required"
            policy_result = "fallback_only"
            reason = f"Clean-cut fallback at {boundary_id}; motion was not safe: " + ", ".join(
                sorted(set(fallback_reasons))
            )
            warnings.extend(
                f"{reason_code}:{boundary_id}" for reason_code in sorted(set(fallback_reasons))
            )
        else:
            transition_type = preferred
            direction = _text(boundary.get("direction")) or _DEFAULT_DIRECTION.get(
                preferred, "none"
            )
            easing = _text(boundary.get("easing")) or policy.timing_easing
            sound_sync_status = (
                "pass" if sound is not None and sound.get("qa_status") == "pass" else "planned"
            )
            components_for_result = _transition_components(
                boundary,
                dialogue,
                sound_ok=sound is not None,
                motion=True,
                policy=policy,
            )
            confidence = _confidence(components_for_result)
            policy_result = (
                "auto_eligible" if confidence >= policy.auto_eligible_min else "review_required"
            )
            if policy_result == "review_required":
                warnings.append(f"transition_review_required:{boundary_id}")
            reason = (
                f"Purpose-bound {purpose} transition supported by structural evidence "
                f"at {boundary_id}."
            )
            motion_times.append(boundary_us)
        dialogue = _dialogue_protection(boundary, start_us, end_us, policy)
        components = _transition_components(
            boundary,
            dialogue,
            sound_ok=sound is not None,
            motion=transition_type in MOTION_TRANSITION_TYPES,
            policy=policy,
        )
        confidence = _confidence(components)
        if transition_type == "hard_cut":
            policy_result = "fallback_only"
        raw_evidence_ids = boundary.get("evidence_ids", [])
        evidence_ids = (
            [str(item) for item in raw_evidence_ids] if isinstance(raw_evidence_ids, list) else []
        )
        planned.append(
            {
                "transition_id": transition_id,
                "outgoing_segment_id": _text(boundary.get("outgoing_segment_id")),
                "incoming_segment_id": _text(boundary.get("incoming_segment_id")),
                "purpose": purpose,
                "transition_type": transition_type,
                "range": {"start_us": start_us, "end_us": end_us},
                "duration_us": duration_us,
                "direction": direction,
                "easing": easing,
                "full_frame_coverage": (
                    bool(boundary.get("full_frame_coverage", False))
                    if transition_type in MOTION_TRANSITION_TYPES
                    else True
                ),
                "incoming_first_readable_frame_us": (
                    first_incoming if first_incoming >= 0 else None
                ),
                "sound_sync_status": sound_sync_status,
                "reason": reason,
                "evidence_ids": evidence_ids,
                "dialogue_protection": dialogue,
                "sound": sound,
                "confidence_components": components,
                "confidence": confidence,
                "policy_result": policy_result,
                "approval_required": True,
                "fallback": policy.default_fallback,
            }
        )
    return planned, list(dict.fromkeys(warnings))


def plan_transitions(
    boundaries: Sequence[Mapping[str, object]],
    *,
    policy: TransitionPolicy | None = None,
    sound_cues: Sequence[Mapping[str, object]] = (),
    output_duration_us: int | None = None,
) -> list[dict[str, object]]:
    """Plan purpose-bound transitions; unsafe motion becomes a clean cut."""

    selected_policy = policy or TransitionPolicy()
    transitions, _warnings = _plan_transitions(
        boundaries,
        selected_policy,
        sound_cues,
        output_duration_us=output_duration_us,
    )
    return transitions


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object")
    return value


def _segments_from_transcript(value: Mapping[str, object]) -> list[Mapping[str, object]]:
    segments = value.get("segments", [])
    if not isinstance(segments, list) or any(not isinstance(item, Mapping) for item in segments):
        raise PlanningValidationError("transcript segments must be an array of objects")
    return [item for item in segments if isinstance(item, Mapping)]


def _cues_from_sound_plan(value: Mapping[str, object]) -> list[Mapping[str, object]]:
    cues = value.get("cues", [])
    if not isinstance(cues, list) or any(not isinstance(item, Mapping) for item in cues):
        raise PlanningValidationError("sound plan cues must be an array of objects")
    return [item for item in cues if isinstance(item, Mapping)]


def write_structural_boundaries(
    package_root: Path,
    layout: ProjectLayout,
    transcript_path: Path,
    *,
    explicit_boundaries_path: Path | None = None,
    policy_path: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    transcript_file = transcript_path.expanduser().resolve()
    transcript = _read_object(transcript_file, "transcript")
    validate_artifact(package_root, "transcript", transcript)
    selected_policy_path = (
        policy_path or package_root / "config" / "transitions.example.yaml"
    ).resolve()
    policy = TransitionPolicy.from_yaml(selected_policy_path)
    explicit: list[Mapping[str, object]] = []
    inputs = [artifact_input(str(transcript["artifact_id"]), transcript_file)]
    if explicit_boundaries_path is not None:
        selected_explicit_path = explicit_boundaries_path.expanduser().resolve()
        explicit_payload = _read_object(selected_explicit_path, "explicit boundaries")
        validate_artifact(package_root, "structural_boundaries", explicit_payload)
        explicit_value = explicit_payload.get("boundaries", [])
        if isinstance(explicit_value, list):
            explicit = [item for item in explicit_value if isinstance(item, Mapping)]
        inputs.append(artifact_input(str(explicit_payload["artifact_id"]), selected_explicit_path))
    segments = _segments_from_transcript(transcript)
    boundaries = detect_structural_boundaries(
        segments,
        transcript,
        explicit_boundaries=explicit,
        policy=policy,
    )
    inputs.append(artifact_input("art_transition_policy", selected_policy_path))
    payload: dict[str, object] = {
        "schema_name": "structural_boundaries",
        "schema_version": "1.0.0",
        "artifact_id": "art_structural_boundaries",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("boundary-detection", "deterministic-transcript", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        "timeline_basis": "output",
        "boundaries": boundaries,
        "warnings": [],
    }
    output = layout.artifacts / "structural-boundaries.json"
    write_validated_artifact(package_root, "structural_boundaries", output, payload)
    return output


def write_transition_plan(
    package_root: Path,
    layout: ProjectLayout,
    transcript_path: Path,
    *,
    boundaries_path: Path | None = None,
    sound_plan_path: Path | None = None,
    policy_path: Path | None = None,
    output_duration_us: int | None = None,
    revision_id: str = "rev_001",
) -> Path:
    transcript_file = transcript_path.expanduser().resolve()
    transcript = _read_object(transcript_file, "transcript")
    validate_artifact(package_root, "transcript", transcript)
    selected_policy_path = (
        policy_path or package_root / "config" / "transitions.example.yaml"
    ).resolve()
    policy = TransitionPolicy.from_yaml(selected_policy_path)
    inputs = [artifact_input(str(transcript["artifact_id"]), transcript_file)]
    if boundaries_path is not None:
        boundary_file = boundaries_path.expanduser().resolve()
        boundary_payload = _read_object(boundary_file, "structural boundaries")
        validate_artifact(package_root, "structural_boundaries", boundary_payload)
        boundary_values = boundary_payload.get("boundaries", [])
        boundaries: Sequence[Mapping[str, object]] = (
            [item for item in boundary_values if isinstance(item, Mapping)]
            if isinstance(boundary_values, list)
            else []
        )
        inputs.append(artifact_input(str(boundary_payload["artifact_id"]), boundary_file))
    else:
        boundaries = detect_structural_boundaries(
            _segments_from_transcript(transcript),
            transcript,
            policy=policy,
        )
    sound_cues: list[Mapping[str, object]] = []
    if sound_plan_path is not None:
        sound_file = sound_plan_path.expanduser().resolve()
        sound_plan = _read_object(sound_file, "sound plan")
        validate_artifact(package_root, "sound_plan", sound_plan)
        sound_cues = _cues_from_sound_plan(sound_plan)
        inputs.append(artifact_input(str(sound_plan["artifact_id"]), sound_file))
    inputs.append(artifact_input("art_transition_policy", selected_policy_path))
    selected_duration = output_duration_us or _int(
        transcript.get("output_duration_us"), _int(transcript.get("source_duration_us"), 0)
    )
    transitions, warnings = _plan_transitions(
        boundaries,
        policy,
        sound_cues,
        output_duration_us=selected_duration if selected_duration > 0 else None,
    )
    payload: dict[str, object] = {
        "schema_name": "transition_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_transition_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("transition-planning", "deterministic-boundary-policy", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        "timeline_basis": "output",
        "transitions": transitions,
        "warnings": warnings,
    }
    output = layout.artifacts / "transition-plan.json"
    write_validated_artifact(package_root, "transition_plan", output, payload)
    return output


__all__ = [
    "ALLOWED_PURPOSES",
    "MOTION_TRANSITION_TYPES",
    "TransitionPolicy",
    "detect_structural_boundaries",
    "plan_transitions",
    "write_structural_boundaries",
    "write_transition_plan",
]
