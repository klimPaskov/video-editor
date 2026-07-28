from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

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

MOTION_TRANSITION_TYPES = {
    "dip_to_color",
    "swipe_left",
    "swipe_right",
    "push_left",
    "push_right",
    "blur_swipe",
    "chapter_transition",
}
REPETITION_TYPES = {
    "immediate_repetition",
    "exact_repetition",
    "near_repetition",
    "semantic_repetition",
    "duplicate_take",
    "weak_take",
}


def _float_value(value: object, default: float) -> float:
    if value is None:
        return default
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError("edit metrics policy value must be numeric") from exc


@dataclass(frozen=True, slots=True)
class EditMetricsPolicy:
    warning_cuts_per_minute: float = 30.0
    warning_average_kept_fragment_us: int = 700_000
    warning_minimum_kept_fragment_us: int = 360_000
    warning_speech_rate_change_percent: float = 28.0
    maximum_motion_transitions_per_minute: float = 1.0
    minimum_motion_transition_spacing_us: int = 12_000_000

    @classmethod
    def from_yaml(
        cls,
        editing_policy_path: Path,
        transition_policy_path: Path,
    ) -> EditMetricsPolicy:
        try:
            editing_value = yaml.safe_load(
                editing_policy_path.expanduser().resolve().read_text(encoding="utf-8")
            )
            transition_value = yaml.safe_load(
                transition_policy_path.expanduser().resolve().read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            raise PlanningValidationError("edit metrics policy files are unreadable") from exc
        editing = editing_value if isinstance(editing_value, dict) else {}
        pacing = editing.get("pacing_qa", {})
        transition_root = transition_value if isinstance(transition_value, dict) else {}
        transition_policy = transition_root.get("transition_policy", {})
        if not isinstance(pacing, dict) or not isinstance(transition_policy, dict):
            raise PlanningValidationError("edit metrics policy sections must be objects")
        return cls(
            warning_cuts_per_minute=_float_value(pacing.get("warn_when_cuts_per_minute"), 30.0),
            warning_average_kept_fragment_us=int(
                _float_value(
                    pacing.get(
                        "warn_when_average_kept_fragment_ms",
                        pacing.get("warn_when_average_kept_fragment_ms_below", 700.0),
                    ),
                    700.0,
                )
                * 1000
            ),
            warning_minimum_kept_fragment_us=int(
                _float_value(pacing.get("warn_when_minimum_kept_fragment_ms"), 360.0) * 1000
            ),
            warning_speech_rate_change_percent=_float_value(
                pacing.get(
                    "warn_when_speech_rate_increase_percent",
                    pacing.get("warn_when_speech_rate_increase_percent_above", 28.0),
                ),
                28.0,
            ),
            maximum_motion_transitions_per_minute=_float_value(
                transition_policy.get("maximum_motion_transitions_per_minute"), 1.0
            ),
            minimum_motion_transition_spacing_us=int(
                _float_value(
                    transition_policy.get("minimum_motion_transition_spacing_seconds"), 12.0
                )
                * 1_000_000
            ),
        )

    def __post_init__(self) -> None:
        if self.warning_cuts_per_minute < 0:
            raise ValueError("warning_cuts_per_minute must be nonnegative")
        if self.warning_average_kept_fragment_us < 0:
            raise ValueError("warning_average_kept_fragment_us must be nonnegative")
        if self.warning_minimum_kept_fragment_us < 0:
            raise ValueError("warning_minimum_kept_fragment_us must be nonnegative")
        if self.warning_speech_rate_change_percent < 0:
            raise ValueError("warning_speech_rate_change_percent must be nonnegative")
        if self.maximum_motion_transitions_per_minute < 0:
            raise ValueError("maximum_motion_transitions_per_minute must be nonnegative")
        if self.minimum_motion_transition_spacing_us < 0:
            raise ValueError("minimum_motion_transition_spacing_us must be nonnegative")


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


def _int(value: object, description: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{description} must be an integer") from exc
    return result


def _range_length(value: Mapping[str, Any], start_key: str, end_key: str, description: str) -> int:
    start = _int(value.get(start_key), f"{description} start")
    end = _int(value.get(end_key), f"{description} end")
    if start < 0 or end <= start:
        raise PlanningValidationError(f"{description} must be a positive half-open range")
    return end - start


def _keep_ranges(edl: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = edl.get("keep_ranges", [])
    if not isinstance(value, list) or not value:
        raise PlanningValidationError("edit decision list must contain keep ranges")
    ranges = [item for item in value if isinstance(item, Mapping)]
    if len(ranges) != len(value):
        raise PlanningValidationError("edit decision list keep ranges must be objects")
    return ranges


def _deletions(edl: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = edl.get("deletions", [])
    if not isinstance(value, list):
        raise PlanningValidationError("edit decision list deletions must be an array")
    deletions = [item for item in value if isinstance(item, Mapping)]
    if len(deletions) != len(value):
        raise PlanningValidationError("edit decision list deletions must be objects")
    return deletions


def _words(transcript: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = transcript.get("words", [])
    if not isinstance(value, list):
        raise PlanningValidationError("transcript words must be an array")
    words = [item for item in value if isinstance(item, Mapping)]
    if len(words) != len(value):
        raise PlanningValidationError("transcript words must be objects")
    return words


def _word_is_retained(word: Mapping[str, Any], ranges: Sequence[Mapping[str, Any]]) -> bool:
    start = _int(word.get("start_us"), "transcript word start")
    end = _int(word.get("end_us"), "transcript word end")
    return any(
        start >= _int(item.get("source_start_us"), "keep range source start")
        and end <= _int(item.get("source_end_us"), "keep range source end")
        for item in ranges
    )


def _join_pacing_warning_count(join_qa: Mapping[str, Any] | None) -> int:
    if join_qa is None:
        return 0
    joins = join_qa.get("joins", [])
    if not isinstance(joins, list):
        raise PlanningValidationError("join QA joins must be an array")
    count = 0
    for item in joins:
        if not isinstance(item, Mapping):
            continue
        pacing = item.get("pacing_check")
        if isinstance(pacing, Mapping) and pacing.get("status") in {"warning", "fail"}:
            count += 1
    return count


def _transition_values(
    transition_plan: Mapping[str, Any] | None,
    *,
    output_duration_us: int,
    policy: EditMetricsPolicy,
) -> tuple[dict[str, Any], list[str]]:
    if transition_plan is None:
        return (
            {
                "motion_transition_count": 0,
                "transitions_per_minute": 0.0,
                "warning_threshold_per_minute": policy.maximum_motion_transitions_per_minute,
                "minimum_spacing_us": None,
                "configured_minimum_spacing_us": policy.minimum_motion_transition_spacing_us,
                "repeated_transition_type_count": 0,
                "repeated_sound_count": 0,
                "status": "not_measured",
                "signal_only": True,
            },
            ["transition_frequency_not_measured"],
        )
    transitions_value = transition_plan.get("transitions", [])
    if not isinstance(transitions_value, list):
        raise PlanningValidationError("transition plan transitions must be an array")
    transitions = [item for item in transitions_value if isinstance(item, Mapping)]
    motion = [
        item for item in transitions if str(item.get("transition_type")) in MOTION_TRANSITION_TYPES
    ]
    starts: list[int] = []
    transition_types: Counter[str] = Counter()
    sounds: Counter[str] = Counter()
    for item in motion:
        range_value = item.get("range")
        if not isinstance(range_value, Mapping):
            raise PlanningValidationError("motion transition range is missing")
        starts.append(_int(range_value.get("start_us"), "motion transition start"))
        transition_types[str(item.get("transition_type"))] += 1
        sound = item.get("sound")
        if isinstance(sound, Mapping) and sound.get("asset_id"):
            sounds[str(sound["asset_id"])] += 1
    starts.sort()
    spacings = [following - previous for previous, following in pairwise(starts)]
    minimum_spacing = min(spacings) if spacings else None
    repeated_types = sum(max(0, count - 1) for count in transition_types.values())
    repeated_sounds = sum(max(0, count - 1) for count in sounds.values())
    transitions_per_minute = len(motion) * 60_000_000 / max(1, output_duration_us)
    warning = (
        transitions_per_minute > policy.maximum_motion_transitions_per_minute
        or (
            minimum_spacing is not None
            and minimum_spacing < policy.minimum_motion_transition_spacing_us
        )
        or repeated_types > 0
        or repeated_sounds > 0
    )
    warnings: list[str] = []
    if transitions_per_minute > policy.maximum_motion_transitions_per_minute:
        warnings.append("transition_frequency_above_configured_limit")
    if (
        minimum_spacing is not None
        and minimum_spacing < policy.minimum_motion_transition_spacing_us
    ):
        warnings.append("transition_spacing_below_configured_minimum")
    if repeated_types > 0:
        warnings.append("transition_type_reuse_present")
    if repeated_sounds > 0:
        warnings.append("transition_sound_reuse_present")
    return (
        {
            "motion_transition_count": len(motion),
            "transitions_per_minute": round(transitions_per_minute, 3),
            "warning_threshold_per_minute": policy.maximum_motion_transitions_per_minute,
            "minimum_spacing_us": minimum_spacing,
            "configured_minimum_spacing_us": policy.minimum_motion_transition_spacing_us,
            "repeated_transition_type_count": repeated_types,
            "repeated_sound_count": repeated_sounds,
            "status": "warning" if warning else "pass",
            "signal_only": True,
        },
        warnings,
    )


def measure_edit_metrics(
    edl: Mapping[str, Any],
    proposals: Mapping[str, Any],
    transcript: Mapping[str, Any],
    *,
    transition_plan: Mapping[str, Any] | None = None,
    output_transcript: Mapping[str, Any] | None = None,
    join_qa: Mapping[str, Any] | None = None,
    policy: EditMetricsPolicy | None = None,
) -> dict[str, Any]:
    selected_policy = policy or EditMetricsPolicy()
    source_duration_us = _int(edl.get("source_duration_us"), "source duration")
    output_duration_us = _int(edl.get("expected_output_duration_us"), "output duration")
    if source_duration_us <= 0 or output_duration_us <= 0:
        raise PlanningValidationError("edit decision list durations must be positive")
    keep_ranges = _keep_ranges(edl)
    deletions = _deletions(edl)
    fragment_lengths = [
        _range_length(item, "output_start_us", "output_end_us", "retained fragment")
        for item in keep_ranges
    ]
    cut_count = len(deletions)
    cuts_per_minute = cut_count * 60_000_000 / output_duration_us
    cut_density_status = (
        "warning" if cuts_per_minute > selected_policy.warning_cuts_per_minute else "pass"
    )
    retained_status = (
        "warning"
        if (
            sum(fragment_lengths) / len(fragment_lengths)
            < selected_policy.warning_average_kept_fragment_us
            or min(fragment_lengths) < selected_policy.warning_minimum_kept_fragment_us
        )
        else "pass"
    )
    retained = {
        "fragment_count": len(fragment_lengths),
        "minimum_fragment_us": min(fragment_lengths),
        "average_fragment_us": round(sum(fragment_lengths) / len(fragment_lengths)),
        "median_fragment_us": round(float(median(fragment_lengths))),
        "warning_average_fragment_us": selected_policy.warning_average_kept_fragment_us,
        "warning_minimum_fragment_us": selected_policy.warning_minimum_kept_fragment_us,
        "status": retained_status,
        "signal_only": True,
    }

    source_words = _words(transcript)
    output_words = (
        _words(output_transcript)
        if output_transcript is not None
        else [word for word in source_words if _word_is_retained(word, keep_ranges)]
    )
    source_word_count = len(source_words)
    output_word_count = len(output_words)
    source_wpm = source_word_count * 60_000_000 / source_duration_us if source_word_count else None
    output_wpm = output_word_count * 60_000_000 / output_duration_us if output_word_count else None
    if source_wpm is None or output_wpm is None or source_wpm <= 0:
        speech_rate_change: float | None = None
        cadence_status = "not_measured"
    else:
        speech_rate_change = ((output_wpm - source_wpm) / source_wpm) * 100.0
        cadence_status = (
            "warning"
            if abs(speech_rate_change) > selected_policy.warning_speech_rate_change_percent
            else "pass"
        )
    join_pacing_warning_count = _join_pacing_warning_count(join_qa)
    if join_pacing_warning_count > 0 and cadence_status == "pass":
        cadence_status = "warning"
    cadence = {
        "source_word_count": source_word_count,
        "output_word_count": output_word_count,
        "source_wpm": round(source_wpm, 3) if source_wpm is not None else None,
        "output_wpm": round(output_wpm, 3) if output_wpm is not None else None,
        "speech_rate_change_percent": (
            round(speech_rate_change, 3) if speech_rate_change is not None else None
        ),
        "warning_speech_rate_change_percent": selected_policy.warning_speech_rate_change_percent,
        "join_pacing_warning_count": join_pacing_warning_count,
        "status": cadence_status,
        "signal_only": True,
    }

    proposal_values = proposals.get("proposals", [])
    if not isinstance(proposal_values, list):
        raise PlanningValidationError("edit proposals proposals must be an array")
    proposal_items = [item for item in proposal_values if isinstance(item, Mapping)]
    applied_ids: set[str] = set()
    for deletion in deletions:
        ids = deletion.get("proposal_ids", [])
        if isinstance(ids, list):
            applied_ids.update(str(item) for item in ids)
    candidate_counts: Counter[str] = Counter()
    applied_counts: Counter[str] = Counter()
    review_required_count = 0
    for proposal in proposal_items:
        proposal_type = str(proposal.get("proposal_type", ""))
        if proposal_type not in REPETITION_TYPES:
            continue
        candidate_counts[proposal_type] += 1
        if str(proposal.get("proposal_id")) in applied_ids:
            applied_counts[proposal_type] += 1
        if str(proposal.get("policy_result")) in {"review_required", "blocked"}:
            review_required_count += 1
    candidate_count = sum(candidate_counts.values())
    applied_count = sum(applied_counts.values())
    repetition = {
        "candidate_counts": dict(sorted(candidate_counts.items())),
        "applied_counts": dict(sorted(applied_counts.items())),
        "candidate_count": candidate_count,
        "applied_count": applied_count,
        "review_required_count": review_required_count,
        "status": "warning" if candidate_count > 0 else "pass",
        "signal_only": True,
    }
    transition_frequency, transition_warnings = _transition_values(
        transition_plan,
        output_duration_us=output_duration_us,
        policy=selected_policy,
    )
    warnings: list[str] = []
    if cut_density_status == "warning":
        warnings.append("cut_density_above_warning_threshold")
    if retained_status == "warning":
        warnings.append("retained_fragment_cadence_below_warning_threshold")
    if cadence_status == "warning":
        warnings.append("cadence_change_above_warning_threshold")
    if cadence_status == "not_measured":
        warnings.append("cadence_not_measured")
    warnings.extend(transition_warnings)
    if repetition["status"] == "warning":
        warnings.append("repetition_candidates_present")
    statuses = [
        str(cut_density_status),
        str(retained_status),
        str(cadence_status),
        str(transition_frequency["status"]),
        str(repetition["status"]),
    ]
    return {
        "source_duration_us": source_duration_us,
        "output_duration_us": output_duration_us,
        "cut_density": {
            "cut_count": cut_count,
            "cuts_per_minute": round(cuts_per_minute, 3),
            "warning_threshold_cuts_per_minute": selected_policy.warning_cuts_per_minute,
            "status": cut_density_status,
            "signal_only": True,
        },
        "retained_fragment": retained,
        "cadence": cadence,
        "transition_frequency": transition_frequency,
        "repetition": repetition,
        "overall_status": "warning"
        if "warning" in statuses or "not_measured" in statuses
        else "pass",
        "blocking": False,
        "warnings": list(dict.fromkeys(warnings)),
    }


def write_edit_metrics_qa(
    package_root: Path,
    layout: ProjectLayout,
    edl_path: Path,
    proposals_path: Path,
    transcript_path: Path,
    *,
    transition_plan_path: Path | None = None,
    output_transcript_path: Path | None = None,
    join_qa_path: Path | None = None,
    editing_policy_path: Path | None = None,
    transition_policy_path: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    edl_file = _owned_path(layout, edl_path, "edit decision list")
    proposals_file = _owned_path(layout, proposals_path, "edit proposals")
    transcript_file = _owned_path(layout, transcript_path, "transcript")
    edl = _read_object(edl_file, "edit decision list")
    proposals = _read_object(proposals_file, "edit proposals")
    transcript = _read_object(transcript_file, "transcript")
    validate_artifact(package_root, "edit_decision_list", edl)
    validate_artifact(package_root, "edit_proposals", proposals)
    validate_artifact(package_root, "transcript", transcript)
    for value, description in (
        (edl, "edit decision list"),
        (proposals, "edit proposals"),
        (transcript, "transcript"),
    ):
        if value.get("project_id") != layout.root.name or value.get("revision_id") != revision_id:
            raise PlanningValidationError(f"{description} project or revision does not match")
    transition_plan: dict[str, Any] | None = None
    transition_file: Path | None = None
    if transition_plan_path is not None:
        transition_file = _owned_path(layout, transition_plan_path, "transition plan")
        transition_plan = _read_object(transition_file, "transition plan")
        validate_artifact(package_root, "transition_plan", transition_plan)
    output_transcript: dict[str, Any] | None = None
    output_transcript_file: Path | None = None
    if output_transcript_path is not None:
        output_transcript_file = _owned_path(layout, output_transcript_path, "output transcript")
        output_transcript = _read_object(output_transcript_file, "output transcript")
        validate_artifact(package_root, "transcript", output_transcript)
    join_qa: dict[str, Any] | None = None
    join_qa_file: Path | None = None
    if join_qa_path is not None:
        join_qa_file = _owned_path(layout, join_qa_path, "join QA report")
        join_qa = _read_object(join_qa_file, "join QA report")
        validate_artifact(package_root, "join_qa_report", join_qa)
    selected_editing_policy = (
        (editing_policy_path or package_root / "config" / "editing-policy.example.yaml")
        .expanduser()
        .resolve()
    )
    selected_transition_policy = (
        (transition_policy_path or package_root / "config" / "transitions.example.yaml")
        .expanduser()
        .resolve()
    )
    policy = EditMetricsPolicy.from_yaml(selected_editing_policy, selected_transition_policy)
    metrics = measure_edit_metrics(
        edl,
        proposals,
        transcript,
        transition_plan=transition_plan,
        output_transcript=output_transcript,
        join_qa=join_qa,
        policy=policy,
    )
    inputs = [
        artifact_input(str(edl["artifact_id"]), edl_file),
        artifact_input(str(proposals["artifact_id"]), proposals_file),
        artifact_input(str(transcript["artifact_id"]), transcript_file),
    ]
    if transition_plan is not None and transition_file is not None:
        inputs.append(artifact_input(str(transition_plan["artifact_id"]), transition_file))
    if output_transcript is not None and output_transcript_file is not None:
        inputs.append(artifact_input(str(output_transcript["artifact_id"]), output_transcript_file))
    if join_qa is not None and join_qa_file is not None:
        inputs.append(artifact_input(str(join_qa["artifact_id"]), join_qa_file))
    payload: dict[str, Any] = {
        "schema_name": "edit_metrics_qa",
        "schema_version": "1.0.0",
        "artifact_id": "art_edit_metrics_qa",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("edit-metrics-qa", "deterministic-metrics", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        **metrics,
    }
    validate_artifact(package_root, "edit_metrics_qa", payload)
    output = layout.artifacts / "edit-metrics-qa.json"
    write_validated_artifact(package_root, "edit_metrics_qa", output, payload)
    return output


__all__ = [
    "MOTION_TRANSITION_TYPES",
    "REPETITION_TYPES",
    "EditMetricsPolicy",
    "measure_edit_metrics",
    "write_edit_metrics_qa",
]
