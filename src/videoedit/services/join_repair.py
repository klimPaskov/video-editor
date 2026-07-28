from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from videoedit import __version__
from videoedit.domain.models import StrictModel
from videoedit.errors import PlanningValidationError, StateConflictError
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
from videoedit.services.retiming import map_source_time_us, read_retimed_timeline

JoinStrategy = Literal[
    "hard_cut",
    "hard_cut_with_micro_audio_crossfade",
    "adjusted_handles",
    "room_tone",
    "j_cut",
    "l_cut",
    "broll_cover",
    "alternate_coverage",
    "purposeful_punch_in",
]

DEFAULT_REPAIR_ORDER: tuple[JoinStrategy, ...] = (
    "hard_cut_with_micro_audio_crossfade",
    "adjusted_handles",
    "room_tone",
    "hard_cut",
)
DURATION_TOLERANCE_US = 100_000


class JoinTimeRange(StrictModel):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> JoinTimeRange:
        if self.end_us <= self.start_us:
            raise ValueError("join range end_us must be greater than start_us")
        return self


class JoinRepairPolicy(StrictModel):
    default_strategy: JoinStrategy = "hard_cut_with_micro_audio_crossfade"
    preview_handle_us: int = Field(default=2_000_000, ge=0)
    pre_handle_us: int = Field(default=80_000, ge=0)
    post_handle_us: int = Field(default=110_000, ge=0)


class JoinPlanItem(StrictModel):
    join_id: str = Field(min_length=1)
    proposal_ids: list[str] = Field(min_length=1)
    source_cut_range: JoinTimeRange
    source_preview_ranges: list[JoinTimeRange] = Field(default_factory=list)
    output_join_us: int = Field(ge=0)
    preview_range: JoinTimeRange
    join_strategy: JoinStrategy
    repair_order: list[JoinStrategy] = Field(min_length=1)
    fallback: Literal["hard_cut"] = "hard_cut"
    handles: dict[str, int]
    reason: str = Field(min_length=1)
    status: Literal["planned", "repair_required", "fallback_required", "review_required"] = (
        "planned"
    )
    repair_action: str | None = None
    repair_attempt: int = Field(default=0, ge=0)
    review_required: bool = False

    @model_validator(mode="after")
    def validate_handles(self) -> JoinPlanItem:
        if set(self.handles) != {"pre_handle_us", "post_handle_us"}:
            raise ValueError("join handles must contain pre_handle_us and post_handle_us")
        if any(value < 0 for value in self.handles.values()):
            raise ValueError("join handles must be nonnegative")
        if self.join_strategy not in self.repair_order:
            raise ValueError("join strategy must be present in repair order")
        if self.repair_order[-1] != "hard_cut":
            raise ValueError("join repair order must end with hard_cut")
        return self


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise PlanningValidationError(f"{label} must be an integer microsecond value")
    if not isinstance(value, (str, int, float)):
        raise PlanningValidationError(f"{label} must be an integer microsecond value")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{label} must be an integer microsecond value") from exc


def _output_join_us(
    source_start_us: int,
    source_end_us: int,
    keep_ranges: Sequence[Mapping[str, object]],
    output_duration_us: int,
) -> int:
    before = [
        _as_int(item["output_end_us"], "keep output_end_us")
        for item in keep_ranges
        if _as_int(item["source_end_us"], "keep source_end_us") <= source_start_us
    ]
    after = [
        _as_int(item["output_start_us"], "keep output_start_us")
        for item in keep_ranges
        if _as_int(item["source_start_us"], "keep source_start_us") >= source_end_us
    ]
    if before:
        return max(0, min(output_duration_us, max(before)))
    if after:
        return max(0, min(output_duration_us, min(after)))
    raise PlanningValidationError("applied cut has no adjacent kept output range")


def _source_ranges_for_output_range(
    output_start_us: int,
    output_end_us: int,
    mapping: Sequence[Mapping[str, object]],
) -> list[dict[str, int]]:
    """Invert a contiguous rate-1 source/output mapping into kept source ranges."""

    if output_start_us < 0 or output_end_us <= output_start_us:
        raise PlanningValidationError("output preview range must be a positive half-open range")
    ranges: list[dict[str, int]] = []
    for item in mapping:
        source_start = _as_int(item.get("source_start_us"), "keep source_start_us")
        mapped_start = _as_int(item.get("output_start_us"), "keep output_start_us")
        mapped_end = _as_int(item.get("output_end_us"), "keep output_end_us")
        overlap_start = max(output_start_us, mapped_start)
        overlap_end = min(output_end_us, mapped_end)
        if overlap_end <= overlap_start:
            continue
        source_range = {
            "start_us": source_start + overlap_start - mapped_start,
            "end_us": source_start + overlap_end - mapped_start,
        }
        if ranges and ranges[-1]["end_us"] == source_range["start_us"]:
            ranges[-1]["end_us"] = source_range["end_us"]
        else:
            ranges.append(source_range)
    return ranges


def _inverse_scaled_offset(offset_us: int, playback_rate: float, *, round_up: bool) -> int:
    rate = Fraction(str(playback_rate))
    value = Fraction(offset_us) * rate
    if round_up:
        return math.ceil(value)
    return math.floor(value)


def _source_ranges_for_retimed_output_range(
    timeline: object,
    output_start_us: int,
    output_end_us: int,
) -> list[dict[str, int]]:
    """Invert a retimed output window while preserving disjoint kept source ranges."""

    if output_start_us < 0 or output_end_us <= output_start_us:
        raise PlanningValidationError("output preview range must be a positive half-open range")
    ranges: list[dict[str, int]] = []
    segments = getattr(timeline, "segments", ())
    for segment in segments:
        source_range = segment.source_range
        output_range = segment.output_range
        overlap_start = max(output_start_us, output_range.start_us)
        overlap_end = min(output_end_us, output_range.end_us)
        if overlap_end <= overlap_start:
            continue
        source_start = source_range.start_us + _inverse_scaled_offset(
            overlap_start - output_range.start_us,
            segment.playback_rate,
            round_up=False,
        )
        source_end = source_range.start_us + _inverse_scaled_offset(
            overlap_end - output_range.start_us,
            segment.playback_rate,
            round_up=True,
        )
        source_start = max(source_range.start_us, min(source_range.end_us, source_start))
        source_end = max(source_start, min(source_range.end_us, source_end))
        if source_end <= source_start:
            continue
        source_value = {"start_us": source_start, "end_us": source_end}
        if ranges and ranges[-1]["end_us"] == source_start:
            ranges[-1]["end_us"] = source_end
        else:
            ranges.append(source_value)
    if not ranges:
        raise PlanningValidationError("output preview range has no corresponding source media")
    return ranges


def _repair_order(strategy: JoinStrategy) -> list[JoinStrategy]:
    ordered: list[JoinStrategy] = [strategy]
    for candidate in DEFAULT_REPAIR_ORDER:
        if candidate not in ordered:
            ordered.append(candidate)
    if ordered[-1] != "hard_cut":
        ordered.append("hard_cut")
    return ordered


def _proposal_strategy(proposal: Mapping[str, object], policy: JoinRepairPolicy) -> JoinStrategy:
    value = str(proposal.get("join_strategy", policy.default_strategy))
    if value == "keep_original":
        return policy.default_strategy
    if value not in {
        "hard_cut",
        "hard_cut_with_micro_audio_crossfade",
        "adjusted_handles",
        "room_tone",
        "j_cut",
        "l_cut",
        "broll_cover",
        "alternate_coverage",
        "purposeful_punch_in",
    }:
        return policy.default_strategy
    return value  # type: ignore[return-value]


def plan_applied_joins(
    proposals: Sequence[Mapping[str, object]],
    decisions: Sequence[Mapping[str, object]],
    keep_ranges: Sequence[Mapping[str, object]],
    output_duration_us: int,
    *,
    policy: JoinRepairPolicy | None = None,
) -> list[dict[str, object]]:
    """Create one deterministic ordinary-dialogue join plan per applied cut."""

    selected_policy = policy or JoinRepairPolicy()
    proposal_by_id = {
        str(item.get("proposal_id")): item for item in proposals if item.get("proposal_id")
    }
    applied: list[tuple[Mapping[str, object], Mapping[str, object], JoinTimeRange]] = []
    for decision in decisions:
        decision_type = str(decision.get("decision", "reject"))
        if decision_type not in {"approve", "modify"}:
            continue
        proposal_id = str(decision.get("proposal_id", ""))
        proposal = proposal_by_id.get(proposal_id)
        if proposal is None:
            raise PlanningValidationError(f"join plan references unknown proposal: {proposal_id}")
        if str(proposal.get("policy_result")) == "blocked":
            raise PlanningValidationError(f"blocked proposal cannot be applied: {proposal_id}")
        raw_range = (
            decision.get("modified_cut_range")
            if decision_type == "modify"
            else proposal.get("proposed_cut_range")
        )
        if not isinstance(raw_range, Mapping):
            raise PlanningValidationError(f"applied proposal lacks a cut range: {proposal_id}")
        selected_range = JoinTimeRange(
            start_us=_as_int(raw_range.get("start_us"), f"{proposal_id} start_us"),
            end_us=_as_int(raw_range.get("end_us"), f"{proposal_id} end_us"),
        )
        applied.append((proposal, decision, selected_range))

    plans: list[JoinPlanItem] = []
    ordered_applied = sorted(
        applied,
        key=lambda item: (
            item[2].start_us,
            item[2].end_us,
            str(item[0]["proposal_id"]),
        ),
    )
    for index, (proposal, _decision, selected_range) in enumerate(
        ordered_applied,
        start=1,
    ):
        output_join_us = _output_join_us(
            selected_range.start_us,
            selected_range.end_us,
            keep_ranges,
            output_duration_us,
        )
        preview_range = JoinTimeRange(
            start_us=max(0, output_join_us - selected_policy.preview_handle_us),
            end_us=min(output_duration_us, output_join_us + selected_policy.preview_handle_us),
        )
        raw_handles = proposal.get("handles")
        handles = (
            {
                "pre_handle_us": _as_int(raw_handles["pre_handle_us"], "pre_handle_us"),
                "post_handle_us": _as_int(raw_handles["post_handle_us"], "post_handle_us"),
            }
            if isinstance(raw_handles, Mapping)
            else {
                "pre_handle_us": selected_policy.pre_handle_us,
                "post_handle_us": selected_policy.post_handle_us,
            }
        )
        strategy = _proposal_strategy(proposal, selected_policy)
        plans.append(
            JoinPlanItem(
                join_id=f"join_{index:06d}",
                proposal_ids=[str(proposal["proposal_id"])],
                source_cut_range=selected_range,
                source_preview_ranges=[
                    JoinTimeRange.model_validate(item)
                    for item in _source_ranges_for_output_range(
                        max(0, output_join_us - selected_policy.preview_handle_us),
                        min(output_duration_us, output_join_us + selected_policy.preview_handle_us),
                        keep_ranges,
                    )
                ],
                output_join_us=output_join_us,
                preview_range=preview_range,
                join_strategy=strategy,
                repair_order=_repair_order(strategy),
                handles=handles,
                reason=str(proposal.get("reason", "Applied approved cut")),
            )
        )
    return [item.model_dump(mode="json") for item in plans]


def repair_join_plan(
    join: Mapping[str, object],
    failure_codes: Sequence[str],
) -> dict[str, object]:
    """Advance one join through its deterministic repair order and require re-review."""

    current = JoinPlanItem.model_validate(join)
    if not failure_codes:
        return current.model_dump(mode="json")
    try:
        current_index = current.repair_order.index(current.join_strategy)
    except ValueError as exc:
        raise PlanningValidationError("join strategy is absent from repair order") from exc
    next_index = min(current_index + 1, len(current.repair_order) - 1)
    next_strategy = current.repair_order[next_index]
    at_fallback = next_index == len(current.repair_order) - 1
    action = f"{current.join_strategy}->{next_strategy}:{','.join(sorted(set(failure_codes)))}"
    updated = current.model_copy(
        update={
            "join_strategy": next_strategy,
            "status": "fallback_required" if at_fallback else "repair_required",
            "repair_action": action,
            "repair_attempt": current.repair_attempt + 1,
            "review_required": True,
        }
    )
    return updated.model_dump(mode="json")


def write_retimed_join_plan(
    package_root: Path,
    layout: ProjectLayout,
    join_plan_path: Path,
    retimed_timeline_path: Path,
) -> Path:
    """Rebase applied-cut joins onto the authoritative retimed output clock."""

    source_plan_path = join_plan_path.expanduser().resolve()
    timeline_path = retimed_timeline_path.expanduser().resolve()
    join_plan = _read_json_object(source_plan_path, "join plan")
    validate_artifact(package_root, "join_plan", join_plan)
    timeline = read_retimed_timeline(package_root, timeline_path)
    if join_plan.get("project_id") != layout.root.name:
        raise PlanningValidationError("join plan project does not match the project layout")
    if join_plan.get("revision_id") != timeline.revision_id:
        raise PlanningValidationError("join plan and retimed timeline revisions differ")

    raw_joins = _mapping_list(join_plan.get("joins", []), "join plan joins")
    rebased_joins: list[dict[str, object]] = []
    for raw_join in raw_joins:
        source_range = raw_join.get("source_cut_range")
        if not isinstance(source_range, Mapping):
            raise PlanningValidationError("join plan source cut range is missing")
        source_start = _as_int(source_range.get("start_us"), "join source start_us")
        source_end = _as_int(source_range.get("end_us"), "join source end_us")
        mapped_start = map_source_time_us(timeline, source_start, edge="start")
        mapped_end = map_source_time_us(timeline, source_end, edge="start")
        if mapped_start != mapped_end:
            raise PlanningValidationError(
                f"{raw_join.get('join_id', 'join')} crosses retimed content at its cut boundary"
            )
        mapped_join = max(0, min(timeline.output_duration_us, mapped_start))
        old_preview = raw_join.get("preview_range")
        old_output_join = _as_int(raw_join.get("output_join_us"), "join output_join_us")
        pre_handle = 2_000_000
        post_handle = 2_000_000
        if isinstance(old_preview, Mapping):
            pre_handle = max(
                0, old_output_join - _as_int(old_preview.get("start_us"), "preview start_us")
            )
            post_handle = max(
                0, _as_int(old_preview.get("end_us"), "preview end_us") - old_output_join
            )
        rebased = dict(raw_join)
        rebased["output_join_us"] = mapped_join
        preview_range = {
            "start_us": max(0, mapped_join - pre_handle),
            "end_us": min(timeline.output_duration_us, mapped_join + post_handle),
        }
        rebased["preview_range"] = preview_range
        rebased["source_preview_ranges"] = _source_ranges_for_retimed_output_range(
            timeline,
            preview_range["start_us"],
            preview_range["end_us"],
        )
        rebased_joins.append(rebased)

    warnings_value = join_plan.get("warnings", [])
    existing_warnings = (
        [str(item) for item in warnings_value] if isinstance(warnings_value, list) else []
    )
    payload: dict[str, object] = {
        "schema_name": "join_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_join_plan_retimed",
        "project_id": layout.root.name,
        "revision_id": str(join_plan["revision_id"]),
        "created_at": now_iso(),
        "producer": producer("join-plan-retime", "deterministic-piecewise-map", __version__),
        "inputs": [
            artifact_input(str(join_plan["artifact_id"]), source_plan_path),
            artifact_input("art_retimed_timeline", timeline_path),
        ],
        "config_sha256": config_sha256(layout),
        "output_duration_us": timeline.output_duration_us,
        "joins": rebased_joins,
        "warnings": [
            *existing_warnings,
            "output_join_and_preview_ranges_rebased_through_retimed_timeline",
        ],
    }
    validate_artifact(package_root, "join_plan", payload)
    output = layout.artifacts / "join-plan-retimed.json"
    write_validated_artifact(package_root, "join_plan", output, payload)
    return output


def _map_revision_time_us(
    mappings: Sequence[Mapping[str, object]],
    value_us: int,
) -> int:
    """Map a parent-output timestamp through a revision keep-range mapping."""

    if value_us < 0:
        raise PlanningValidationError("revision timestamp must be nonnegative")
    previous_end: int | None = None
    for item in mappings:
        source_start = _as_int(item.get("source_start_us"), "revision source_start_us")
        source_end = _as_int(item.get("source_end_us"), "revision source_end_us")
        output_start = _as_int(item.get("output_start_us"), "revision output_start_us")
        output_end = _as_int(item.get("output_end_us"), "revision output_end_us")
        if source_end <= source_start or output_end <= output_start:
            raise PlanningValidationError("revision mapping contains an empty range")
        if previous_end is not None and source_start < previous_end:
            raise PlanningValidationError("revision mapping source ranges overlap")
        previous_end = source_end
        if source_start <= value_us <= source_end:
            return output_start + min(value_us - source_start, output_end - output_start)
    raise PlanningValidationError(f"timestamp {value_us}us is outside the revision source clock")


def _map_revision_boundary_us(
    mappings: Sequence[Mapping[str, object]],
    value_us: int,
) -> int:
    """Map a preview boundary, clamping a removed parent interval to its join edge."""

    previous_output_end: int | None = None
    previous_source_end: int | None = None
    for item in mappings:
        source_start = _as_int(item.get("source_start_us"), "revision source_start_us")
        if previous_output_end is not None and value_us < source_start:
            return previous_output_end
        previous_output_end = _as_int(item.get("output_end_us"), "revision output_end_us")
        previous_source_end = _as_int(item.get("source_end_us"), "revision source_end_us")
        if source_start <= value_us <= previous_source_end:
            return _map_revision_time_us(mappings, value_us)
    if previous_output_end is not None and previous_source_end == value_us:
        return previous_output_end
    raise PlanningValidationError(
        f"preview boundary {value_us}us is outside the revision source clock"
    )


def write_revision_join_plan(
    package_root: Path,
    layout: ProjectLayout,
    join_plan_path: Path,
    revision_media_path: Path,
    *,
    revision_id: str,
) -> Path:
    """Rebase rendered join evidence from a parent output clock onto a recut revision."""

    source_plan_path = join_plan_path.expanduser().resolve()
    media_path = revision_media_path.expanduser().resolve()
    join_plan = _read_json_object(source_plan_path, "join plan")
    media = _read_json_object(media_path, "revision media manifest")
    validate_artifact(package_root, "join_plan", join_plan)
    validate_artifact(package_root, "revision_media_manifest", media)
    if join_plan.get("project_id") != layout.root.name:
        raise PlanningValidationError("join plan project does not match the project layout")
    if media.get("project_id") != layout.root.name:
        raise PlanningValidationError("revision media project does not match the project layout")
    if media.get("revision_id") != revision_id:
        raise PlanningValidationError("revision media belongs to another revision")
    source_duration_us = _as_int(media.get("source_duration_us"), "revision source duration")
    output_duration_us = _as_int(media.get("output_duration_us"), "revision output duration")
    parent_duration_us = _as_int(join_plan.get("output_duration_us"), "join plan output duration")
    if source_duration_us <= 0 or output_duration_us <= 0:
        raise PlanningValidationError("revision media durations must be positive")
    if abs(parent_duration_us - source_duration_us) > DURATION_TOLERANCE_US:
        raise PlanningValidationError(
            "join plan duration does not match the revision media parent output duration"
        )
    raw_mappings = media.get("source_to_output_mapping")
    if not isinstance(raw_mappings, list) or any(
        not isinstance(item, Mapping) for item in raw_mappings
    ):
        raise PlanningValidationError("revision media mapping must be an array of objects")
    mappings = [item for item in raw_mappings if isinstance(item, Mapping)]
    if not mappings:
        raise PlanningValidationError("revision media mapping must not be empty")

    raw_joins = _mapping_list(join_plan.get("joins", []), "join plan joins")
    rebased_joins: list[dict[str, object]] = []
    for raw_join in raw_joins:
        output_join_us = _as_int(raw_join.get("output_join_us"), "join output_join_us")
        if output_join_us > source_duration_us:
            if output_join_us - source_duration_us > DURATION_TOLERANCE_US:
                raise PlanningValidationError(
                    "join output timestamp exceeds revision source duration"
                )
            output_join_us = source_duration_us
        preview = raw_join.get("preview_range")
        if not isinstance(preview, Mapping):
            raise PlanningValidationError("join preview range is missing")
        preview_start = _as_int(preview.get("start_us"), "join preview start_us")
        preview_end = _as_int(preview.get("end_us"), "join preview end_us")
        if preview_end <= preview_start:
            raise PlanningValidationError("join preview range must be positive")
        if preview_end > source_duration_us:
            if preview_end - source_duration_us > DURATION_TOLERANCE_US:
                raise PlanningValidationError("join preview range exceeds revision source duration")
            preview_end = source_duration_us
        mapped_join = _map_revision_time_us(mappings, output_join_us)
        mapped_start = _map_revision_boundary_us(mappings, preview_start)
        mapped_end = _map_revision_boundary_us(mappings, preview_end)
        if mapped_end <= mapped_start:
            raise PlanningValidationError(
                f"{raw_join.get('join_id', 'join')} preview is fully removed by the revision"
            )
        rebased = dict(raw_join)
        rebased["output_join_us"] = min(output_duration_us, mapped_join)
        rebased["preview_range"] = {
            "start_us": max(0, mapped_start),
            "end_us": min(output_duration_us, mapped_end),
        }
        raw_source_preview_ranges = raw_join.get("source_preview_ranges", [])
        rebased["source_preview_ranges"] = (
            list(raw_source_preview_ranges) if isinstance(raw_source_preview_ranges, list) else []
        )
        rebased_joins.append(rebased)

    prior_warnings = join_plan.get("warnings", [])
    prior_warning_values = (
        [str(value) for value in prior_warnings] if isinstance(prior_warnings, list) else []
    )
    payload: dict[str, object] = {
        "schema_name": "join_plan",
        "schema_version": "1.0.0",
        "artifact_id": f"art_join_plan_{revision_id}",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "join-plan-revision-rebase", "deterministic-piecewise-map", __version__
        ),
        "inputs": [
            artifact_input(str(join_plan["artifact_id"]), source_plan_path),
            artifact_input(str(media["artifact_id"]), media_path),
        ],
        "config_sha256": config_sha256(layout),
        "output_duration_us": output_duration_us,
        "joins": rebased_joins,
        "warnings": [
            *prior_warning_values,
            "join_output_and_preview_ranges_rebased_through_revision_media",
            "source_preview_ranges_preserved_from_parent_join_plan",
        ],
    }
    validate_artifact(package_root, "join_plan", payload)
    stage_key = make_stage_key(
        "join-plan-revision-rebase",
        "p11-join-revision-rebase-1",
        [sha256_file(source_plan_path), sha256_file(media_path)],
        {"project_id": layout.root.name, "revision_id": revision_id},
    )
    output = layout.artifacts / f"join-plan-rebased-{revision_id}-{stage_key[:16]}.json"
    alias = layout.artifacts / f"join-plan-rebased-{revision_id}.json"
    with ProjectLock(layout, stage="join_plan_revision_rebase", revision_id=revision_id):
        if output.is_file():
            current = _read_json_object(output, "revision-rebased join plan")
            validate_artifact(package_root, "join_plan", current)
            if any(key != "created_at" and current.get(key) != payload.get(key) for key in payload):
                raise StateConflictError("revision-rebased join plan exists with stale contents")
            payload = current
        else:
            write_validated_artifact(package_root, "join_plan", output, payload)
        write_validated_artifact(package_root, "join_plan", alias, payload)
    return output


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object")
    return value


def _mapping_list(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise PlanningValidationError(f"{label} must be an array of objects")
    return [item for item in value if isinstance(item, Mapping)]


def write_join_plan(
    package_root: Path,
    layout: ProjectLayout,
    proposals_path: Path,
    decisions_path: Path,
    edl_path: Path,
    *,
    revision_id: str = "rev_001",
    policy: JoinRepairPolicy | None = None,
) -> Path:
    proposals = _read_json_object(proposals_path, "edit proposals")
    decisions = _read_json_object(decisions_path, "edit decisions")
    edl = _read_json_object(edl_path, "edit decision list")
    validate_artifact(package_root, "edit_proposals", proposals)
    validate_artifact(package_root, "edit_review_decisions", decisions)
    validate_artifact(package_root, "edit_decision_list", edl)
    joins = plan_applied_joins(
        _mapping_list(proposals.get("proposals", []), "edit proposals"),
        _mapping_list(decisions.get("decisions", []), "edit decisions"),
        _mapping_list(edl.get("keep_ranges", []), "edit decision list keep ranges"),
        _as_int(edl["expected_output_duration_us"], "expected_output_duration_us"),
        policy=policy,
    )
    payload: dict[str, object] = {
        "schema_name": "join_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_join_plan",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("join-planning", "deterministic-repair", __version__),
        "inputs": [
            artifact_input("art_proposals", proposals_path),
            artifact_input("art_edit_decisions", decisions_path),
            artifact_input("art_edl", edl_path),
        ],
        "config_sha256": config_sha256(layout),
        "output_duration_us": _as_int(edl["expected_output_duration_us"], "output_duration_us"),
        "joins": joins,
        "warnings": [],
    }
    validate_artifact(package_root, "join_plan", payload)
    output = layout.artifacts / "join-plan.json"
    write_validated_artifact(package_root, "join_plan", output, payload)
    return output


__all__ = [
    "JoinPlanItem",
    "JoinRepairPolicy",
    "plan_applied_joins",
    "repair_join_plan",
    "write_join_plan",
    "write_retimed_join_plan",
]
