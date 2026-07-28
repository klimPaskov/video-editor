from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoedit import __version__
from videoedit.domain.models import TransformKeyframe
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.services.artifacts import (
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout

FOCUS_SCHEMA_VERSION = "1.0.0"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ZOOM_PURPOSES = {
    "opened_window",
    "prompt_box",
    "relevant_cursor_action",
    "important_ui",
}
SPEEDUP_ACTIONS = {"prompt_writing", "prompt_dictation"}
SPEEDUP_REQUEST_SOURCES = {"project_brief", "gate1_review", "fix_marker"}
FORBIDDEN_ACTIVITY_FIELDS = (
    "contains_browsing",
    "contains_reading",
    "contains_waiting",
    "contains_other_action",
    "contains_navigation",
    "contains_result_inspection",
    "contains_loading",
    "contains_cursor_wandering",
)


class FocusPacingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(FocusPacingModel):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_half_open(self) -> TimeRange:
        if self.end_us <= self.start_us:
            raise ValueError("time range must be a non-empty half-open interval")
        return self


class NormalizedBBox(FocusPacingModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> NormalizedBBox:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("normalized bounding box must remain inside the frame")
        return self


class FocusConfidence(FocusPacingModel):
    target_visibility: float = Field(ge=0, le=1)
    target_identity: float = Field(ge=0, le=1)
    boundary: float = Field(ge=0, le=1)
    stability: float = Field(ge=0, le=1)
    overall: float = Field(ge=0, le=1)


class TargetSample(FocusPacingModel):
    time_us: int = Field(ge=0)
    bbox: NormalizedBBox


class PurposefulZoom(FocusPacingModel):
    zoom_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    purpose: Literal[
        "opened_window",
        "prompt_box",
        "relevant_cursor_action",
        "important_ui",
    ]
    source_range: TimeRange
    target_visible_range: TimeRange
    zoom_in_end_us: int = Field(ge=0)
    zoom_out_start_us: int = Field(ge=0)
    target_description: str = Field(min_length=1)
    target_track: list[TargetSample] = Field(min_length=1)
    start_scale: float = Field(default=1.0, ge=1.0, le=1.0)
    peak_scale: float = Field(gt=1, le=2.5)
    centering_mode: Literal["visible_target_center"] = "visible_target_center"
    easing: Literal["smooth_ease_in_out"] = "smooth_ease_in_out"
    stabilization: Literal["locked_smooth_target"] = "locked_smooth_target"
    allow_free_pan: Literal[False] = False
    reason: str = Field(min_length=1)
    confidence: FocusConfidence
    policy_result: Literal["auto_eligible", "review_required", "skipped", "blocked"]
    approval_required: bool
    fallback: Literal["no_zoom"] = "no_zoom"
    evidence_frames: list[str] = Field(min_length=2)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_timing_and_policy(self) -> PurposefulZoom:
        if self.target_visible_range.start_us > self.source_range.start_us:
            raise ValueError("zoom source range must start after the target becomes visible")
        if self.source_range.end_us > self.target_visible_range.end_us:
            raise ValueError("zoom source range must end before target relevance ends")
        if not self.source_range.start_us <= self.zoom_in_end_us <= self.source_range.end_us:
            raise ValueError("zoom_in_end_us must be inside the source range")
        if not self.source_range.start_us <= self.zoom_out_start_us <= self.source_range.end_us:
            raise ValueError("zoom_out_start_us must be inside the source range")
        if self.zoom_in_end_us <= self.source_range.start_us:
            raise ValueError("zoom-in must have a visible motion interval")
        if self.zoom_out_start_us >= self.source_range.end_us:
            raise ValueError("zoom-out must finish before the source range ends")
        if self.zoom_out_start_us < self.zoom_in_end_us:
            raise ValueError("zoom-out cannot begin before zoom-in completes")
        for sample in self.target_track:
            if not (
                self.target_visible_range.start_us
                <= sample.time_us
                <= self.target_visible_range.end_us
            ):
                raise ValueError("target track sample lies outside target_visible_range")
        if self.policy_result == "auto_eligible" and self.approval_required:
            raise ValueError("auto-eligible zooms do not require a separate item approval")
        if self.policy_result == "review_required" and not self.approval_required:
            raise ValueError("review-required zooms must require approval")
        return self


class ForbiddenContentCheck(FocusPacingModel):
    contains_browsing: Literal[False] = False
    contains_reading: Literal[False] = False
    contains_waiting: Literal[False] = False
    contains_other_action: Literal[False] = False
    contains_navigation: Literal[False] = False
    contains_result_inspection: Literal[False] = False
    contains_loading: Literal[False] = False
    contains_cursor_wandering: Literal[False] = False


class PromptSpeedup(FocusPacingModel):
    speedup_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    action_type: Literal["prompt_writing", "prompt_dictation"]
    source_range: TimeRange
    request_source: Literal["project_brief", "gate1_review", "fix_marker"]
    request_text: str = Field(min_length=1)
    playback_rate: float = Field(gt=1, le=8)
    audio_mode: Literal["audible_pitch_preserved", "audible", "muted"]
    audio_exception_explicit: bool
    action_visible: Literal[True] = True
    exact_action_boundaries: Literal[True] = True
    forbidden_content_check: ForbiddenContentCheck
    start_evidence_frame: str = Field(min_length=1)
    end_evidence_frame: str = Field(min_length=1)
    action_visibility_confidence: float = Field(ge=0, le=1)
    boundary_confidence: float = Field(ge=0, le=1)
    overall_confidence: float = Field(ge=0, le=1)
    policy_result: Literal["auto_eligible", "review_required", "skipped", "blocked"]
    approval_required: bool
    fallback: Literal["normal_speed"] = "normal_speed"
    reason: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_audio_and_policy(self) -> PromptSpeedup:
        if self.audio_mode == "muted" and not self.audio_exception_explicit:
            raise ValueError("muted speed-up audio requires an explicit exception")
        if self.audio_mode != "muted" and self.audio_exception_explicit:
            raise ValueError("audible speed-up audio cannot carry a mute exception")
        checks = self.forbidden_content_check.model_dump()
        if any(bool(checks[name]) for name in FORBIDDEN_ACTIVITY_FIELDS):
            raise ValueError("speed-up contains forbidden unrelated activity")
        if self.policy_result == "auto_eligible" and self.approval_required:
            raise ValueError("auto-eligible speed-ups do not require a separate item approval")
        if self.policy_result == "review_required" and not self.approval_required:
            raise ValueError("review-required speed-ups must require approval")
        return self


class OperatorRequest(FocusPacingModel):
    speedups_requested: bool
    request_source: Literal["none", "project_brief", "gate1_review", "fix_marker"]
    request_text: str | None

    @model_validator(mode="after")
    def validate_request(self) -> OperatorRequest:
        if self.speedups_requested:
            if self.request_source == "none" or not self.request_text:
                raise ValueError("requested speed-ups require a source and request text")
        elif self.request_source != "none" or self.request_text is not None:
            raise ValueError("an absent speed-up request must use none and null")
        return self


class ReviewPolicy(FocusPacingModel):
    batch_uncertain_items: Literal[True] = True
    maximum_questions_per_round: int = Field(ge=1, le=5)
    use_safe_fallbacks: Literal[True] = True
    include_recommendation: Literal[True] = True


class FocusPacingPlan(FocusPacingModel):
    schema_name: Literal["focus_pacing_plan"] = "focus_pacing_plan"
    schema_version: Literal["1.0.0"] = "1.0.0"
    artifact_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    revision_id: str = Field(pattern=r"^rev_[0-9]{3,}$")
    created_at: str
    producer: dict[str, str]
    inputs: list[dict[str, str]]
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator_request: OperatorRequest
    zooms: list[PurposefulZoom]
    speedups: list[PromptSpeedup]
    skipped_zoom_candidates: list[dict[str, Any]]
    review_policy: ReviewPolicy
    warnings: list[str]

    @model_validator(mode="after")
    def validate_plan(self) -> FocusPacingPlan:
        if not self.operator_request.speedups_requested and self.speedups:
            raise ValueError("speed-ups cannot be planned without an operator request")
        zoom_ids = [item.zoom_id for item in self.zooms]
        if len(zoom_ids) != len(set(zoom_ids)):
            raise ValueError("zoom identifiers must be unique")
        speedup_ids = [item.speedup_id for item in self.speedups]
        if len(speedup_ids) != len(set(speedup_ids)):
            raise ValueError("speed-up identifiers must be unique")
        for item in self.inputs:
            if not SHA256_PATTERN.fullmatch(str(item.get("sha256", ""))):
                raise ValueError("focus plan inputs must be hash-bound")
        return self


class FocusPacingPolicy:
    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        zoom = values.get("purposeful_zoom", {}) if values else {}
        speed = values.get("prompt_speedup", {}) if values else {}
        self.allowed_purposes = set(zoom.get("allowed_purposes", ZOOM_PURPOSES))
        self.zoom_min_scale = float(zoom.get("minimum_peak_scale", 1.05))
        self.zoom_max_scale = float(zoom.get("maximum_peak_scale", 2.5))
        self.zoom_auto_min = float(zoom.get("auto_eligible_min_confidence", 0.96))
        self.zoom_review_min = float(zoom.get("review_min_confidence", 0.85))
        self.speed_min_rate = float(speed.get("minimum_playback_rate", 1.1))
        self.speed_max_rate = float(speed.get("maximum_playback_rate", 8.0))
        self.speed_auto_min = float(speed.get("auto_eligible_min_boundary_confidence", 0.97))
        self.speed_review_min = float(speed.get("review_min_confidence", 0.85))


def _require_hash(value: object, label: str) -> str:
    digest = str(value)
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError(f"{label} must be a lowercase SHA-256 hash")
    return digest


def _as_time_range(value: Mapping[str, Any], label: str) -> dict[str, int]:
    try:
        start = int(value["start_us"])
        end = int(value["end_us"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain integer start_us and end_us") from exc
    return TimeRange(start_us=start, end_us=end).model_dump(mode="json")


def _policy_result(confidence: float, auto_min: float, review_min: float) -> str:
    if confidence >= auto_min:
        return "auto_eligible"
    if confidence >= review_min:
        return "review_required"
    return "skipped"


def classify_zoom_candidate(
    candidate: Mapping[str, Any],
    *,
    policy: FocusPacingPolicy | None = None,
) -> PurposefulZoom:
    selected = policy or FocusPacingPolicy()
    raw = dict(candidate)
    purpose = str(raw.get("purpose", ""))
    if purpose not in selected.allowed_purposes or purpose not in ZOOM_PURPOSES:
        raise ValueError(f"zoom purpose is not allowed: {purpose}")
    source_range = _as_time_range(raw.get("source_range", {}), "zoom source_range")
    target_range = _as_time_range(
        raw.get("target_visible_range", raw.get("source_range", {})),
        "zoom target_visible_range",
    )
    track_value = raw.get("target_track", [])
    if not isinstance(track_value, list) or not track_value:
        raise ValueError("zoom candidate requires a visible target track")
    track: list[dict[str, Any]] = []
    for sample in track_value:
        if not isinstance(sample, Mapping):
            raise ValueError("zoom target track samples must be objects")
        bbox = sample.get("bbox")
        if not isinstance(bbox, Mapping):
            raise ValueError("zoom target track sample requires a bbox")
        track.append(
            {
                "time_us": int(sample["time_us"]),
                "bbox": NormalizedBBox.model_validate(dict(bbox)).model_dump(mode="json"),
            }
        )
    confidence_raw = raw.get("confidence", {})
    if not isinstance(confidence_raw, Mapping):
        raise ValueError("zoom candidate confidence must be an object")
    confidence = FocusConfidence.model_validate(dict(confidence_raw))
    overall = confidence.overall
    result = _policy_result(overall, selected.zoom_auto_min, selected.zoom_review_min)
    warning_values = [str(item) for item in raw.get("warnings", [])]
    if result == "skipped":
        warning_values.append("confidence_below_review_threshold")
    zoom = PurposefulZoom(
        zoom_id=str(raw.get("zoom_id", "")),
        purpose=purpose,  # type: ignore[arg-type]
        source_range=TimeRange.model_validate(source_range),
        target_visible_range=TimeRange.model_validate(target_range),
        zoom_in_end_us=int(raw.get("zoom_in_end_us", source_range["start_us"])),
        zoom_out_start_us=int(raw.get("zoom_out_start_us", source_range["end_us"])),
        target_description=str(raw.get("target_description", "")),
        target_track=[TargetSample.model_validate(item) for item in track],
        peak_scale=float(raw.get("peak_scale", selected.zoom_min_scale)),
        reason=str(raw.get("reason", "")),
        confidence=confidence,
        policy_result=result,  # type: ignore[arg-type]
        approval_required=result == "review_required",
        evidence_frames=[str(item) for item in raw.get("evidence_frames", [])],
        warnings=warning_values,
    )
    if zoom.peak_scale < selected.zoom_min_scale or zoom.peak_scale > selected.zoom_max_scale:
        raise ValueError("zoom peak scale is outside the configured policy range")
    return zoom


def _forbidden_check(value: object) -> ForbiddenContentCheck:
    if not isinstance(value, Mapping):
        raise ValueError("speed-up forbidden_content_check must be an object")
    checks = {name: bool(value.get(name, False)) for name in FORBIDDEN_ACTIVITY_FIELDS}
    if any(checks.values()):
        raise ValueError("forbidden unrelated activity is present in the speed-up range")
    return ForbiddenContentCheck.model_validate(checks)


def classify_speedup_candidate(
    candidate: Mapping[str, Any],
    *,
    operator_request: OperatorRequest | Mapping[str, Any],
    policy: FocusPacingPolicy | None = None,
) -> PromptSpeedup:
    selected = policy or FocusPacingPolicy()
    request = (
        operator_request
        if isinstance(operator_request, OperatorRequest)
        else OperatorRequest.model_validate(dict(operator_request))
    )
    if not request.speedups_requested:
        raise ValueError("speed-up candidates require an explicit operator request")
    action = str(candidate.get("action_type", ""))
    if action not in SPEEDUP_ACTIONS:
        raise ValueError(f"speed-up action is not allowed: {action}")
    request_source = str(candidate.get("request_source", request.request_source))
    if request_source not in SPEEDUP_REQUEST_SOURCES:
        raise ValueError("speed-up request source is not an approved operator source")
    request_text = str(candidate.get("request_text", request.request_text or ""))
    source_range = _as_time_range(candidate.get("source_range", {}), "speed-up source_range")
    forbidden_raw = candidate.get("forbidden_content_check", {})
    forbidden = _forbidden_check(forbidden_raw)
    forbidden_values = forbidden.model_dump()
    if any(forbidden_values.values()):
        result = "blocked"
        warnings = ["forbidden_unrelated_activity"]
    else:
        visibility = float(candidate.get("action_visibility_confidence", 0))
        boundary = float(candidate.get("boundary_confidence", 0))
        overall = float(candidate.get("overall_confidence", min(visibility, boundary)))
        confidence = min(visibility, boundary, overall)
        result = _policy_result(confidence, selected.speed_auto_min, selected.speed_review_min)
        warnings = ["confidence_below_review_threshold"] if result == "skipped" else []
    rate = float(candidate.get("playback_rate", selected.speed_min_rate))
    if not selected.speed_min_rate <= rate <= selected.speed_max_rate:
        raise ValueError("speed-up playback rate is outside the configured policy range")
    audio_mode = str(candidate.get("audio_mode", "audible_pitch_preserved"))
    if audio_mode not in {"audible_pitch_preserved", "audible", "muted"}:
        raise ValueError(f"unsupported speed-up audio mode: {audio_mode}")
    mute_exception = bool(candidate.get("audio_exception_explicit", False))
    if audio_mode == "muted" and not mute_exception:
        raise ValueError("muting a speed-up requires an explicit reviewed exception")
    if audio_mode != "muted" and mute_exception:
        raise ValueError("audible speed-up cannot carry a mute exception")
    return PromptSpeedup(
        speedup_id=str(candidate.get("speedup_id", "")),
        action_type=action,  # type: ignore[arg-type]
        source_range=TimeRange.model_validate(source_range),
        request_source=request_source,  # type: ignore[arg-type]
        request_text=request_text,
        playback_rate=rate,
        audio_mode=audio_mode,  # type: ignore[arg-type]
        audio_exception_explicit=mute_exception,
        forbidden_content_check=forbidden,
        start_evidence_frame=str(candidate.get("start_evidence_frame", "")),
        end_evidence_frame=str(candidate.get("end_evidence_frame", "")),
        action_visibility_confidence=float(candidate.get("action_visibility_confidence", 0)),
        boundary_confidence=float(candidate.get("boundary_confidence", 0)),
        overall_confidence=float(candidate.get("overall_confidence", 0)),
        policy_result=result,  # type: ignore[arg-type]
        approval_required=result in {"review_required", "blocked"},
        reason=str(candidate.get("reason", "")),
        warnings=[*warnings, *(str(item) for item in candidate.get("warnings", []))],
    )


def _normalize_skipped(candidate: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "candidate_id": str(candidate.get("candidate_id", "")),
        "source_range": _as_time_range(candidate.get("source_range", {}), "skipped zoom range"),
        "reason": str(candidate.get("reason", "")),
        "fallback": "no_zoom",
    }
    if not value["candidate_id"] or not value["reason"]:
        raise ValueError("skipped zoom candidates require an identifier and reason")
    return value


def build_focus_pacing_plan(
    *,
    package_root: Path,
    project_id: str,
    revision_id: str,
    inputs: Sequence[Mapping[str, str]],
    zoom_candidates: Sequence[Mapping[str, Any]] = (),
    speedup_candidates: Sequence[Mapping[str, Any]] = (),
    operator_request: Mapping[str, Any] | None = None,
    skipped_zoom_candidates: Sequence[Mapping[str, Any]] = (),
    config_hash: str | None = None,
    policy_values: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    request = OperatorRequest.model_validate(
        dict(
            operator_request
            or {
                "speedups_requested": False,
                "request_source": "none",
                "request_text": None,
            }
        )
    )
    selected_policy = FocusPacingPolicy(policy_values)
    zooms = [classify_zoom_candidate(item, policy=selected_policy) for item in zoom_candidates]
    speedups: list[PromptSpeedup] = []
    speedup_warnings: list[str] = []
    for item in speedup_candidates:
        try:
            speedups.append(
                classify_speedup_candidate(
                    item,
                    operator_request=request,
                    policy=selected_policy,
                )
            )
        except ValueError as exc:
            if "forbidden unrelated activity" not in str(exc):
                raise
            candidate_id = str(item.get("speedup_id", "unknown"))
            speedup_warnings.append(f"speedup_fallback_normal_speed:{candidate_id}")
    normalized_inputs = [
        {"artifact_id": str(item["artifact_id"]), "sha256": _require_hash(item["sha256"], "input")}
        for item in inputs
    ]
    payload = {
        "schema_name": "focus_pacing_plan",
        "schema_version": FOCUS_SCHEMA_VERSION,
        "artifact_id": "art_focus_pacing",
        "project_id": project_id,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("focus-pacing-plan", "deterministic-policy", __version__),
        "inputs": normalized_inputs,
        "config_sha256": _require_hash(
            config_hash or config_sha256_for_values(policy_values), "config_sha256"
        ),
        "operator_request": request.model_dump(mode="json"),
        "zooms": [item.model_dump(mode="json") for item in zooms],
        "speedups": [item.model_dump(mode="json") for item in speedups],
        "skipped_zoom_candidates": [_normalize_skipped(item) for item in skipped_zoom_candidates],
        "review_policy": ReviewPolicy(
            maximum_questions_per_round=int(
                (policy_values or {}).get("review", {}).get("maximum_questions_per_round", 5)
            )
        ).model_dump(mode="json"),
        "warnings": list(dict.fromkeys([str(item) for item in warnings] + speedup_warnings)),
    }
    validate_focus_pacing_plan(package_root, payload)
    return payload


def config_sha256_for_values(values: Mapping[str, Any] | None) -> str:
    if values is None:
        return "0" * 64
    return canonical_sha256(values)


def validate_focus_pacing_plan(package_root: Path, payload: Mapping[str, Any]) -> FocusPacingPlan:
    value = dict(payload)
    validate_artifact(package_root, "focus_pacing_plan", value)
    return FocusPacingPlan.model_validate(value)


def write_focus_pacing_plan(
    package_root: Path,
    layout: ProjectLayout,
    payload: Mapping[str, Any],
) -> Path:
    validated = validate_focus_pacing_plan(package_root, payload)
    value = validated.model_dump(mode="json")
    digest = canonical_sha256(value)
    output = layout.artifacts / f"focus-pacing-plan-{digest[:16]}.json"
    write_validated_artifact(package_root, "focus_pacing_plan", output, value)
    write_validated_artifact(
        package_root,
        "focus_pacing_plan",
        layout.artifacts / "focus-pacing-plan.json",
        value,
    )
    return output


def read_focus_pacing_plan(package_root: Path, path: Path) -> FocusPacingPlan:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("focus pacing plan must be a JSON object")
    return validate_focus_pacing_plan(package_root, value)


def review_batch(plan: FocusPacingPlan) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for zoom in plan.zooms:
        if zoom.policy_result in {"review_required", "blocked"}:
            items.append(
                {
                    "item_id": zoom.zoom_id,
                    "kind": "zoom",
                    "recommendation": "apply purposeful centered zoom"
                    if zoom.policy_result == "review_required"
                    else "keep original; no_zoom fallback",
                    "fallback": "no_zoom",
                    "confidence": zoom.confidence.model_dump(mode="json"),
                    "evidence_frames": zoom.evidence_frames,
                    "reason": zoom.reason,
                }
            )
    for speedup in plan.speedups:
        if speedup.policy_result in {"review_required", "blocked"}:
            items.append(
                {
                    "item_id": speedup.speedup_id,
                    "kind": "speedup",
                    "recommendation": "apply requested speed-up"
                    if speedup.policy_result == "review_required"
                    else "keep normal speed; split or reject mixed activity",
                    "fallback": "normal_speed",
                    "confidence": {
                        "action_visibility": speedup.action_visibility_confidence,
                        "boundary": speedup.boundary_confidence,
                        "overall": speedup.overall_confidence,
                    },
                    "evidence_frames": [
                        speedup.start_evidence_frame,
                        speedup.end_evidence_frame,
                    ],
                    "reason": speedup.reason,
                }
            )
    return items[: plan.review_policy.maximum_questions_per_round]


def _track_bbox_at(zoom: PurposefulZoom, time_us: int) -> NormalizedBBox:
    samples = sorted(zoom.target_track, key=lambda item: item.time_us)
    if time_us <= samples[0].time_us:
        return samples[0].bbox
    if time_us >= samples[-1].time_us:
        return samples[-1].bbox
    for previous, following in pairwise(samples):
        if previous.time_us <= time_us <= following.time_us:
            span = max(1, following.time_us - previous.time_us)
            amount = (time_us - previous.time_us) / span
            return NormalizedBBox(
                x=previous.bbox.x + (following.bbox.x - previous.bbox.x) * amount,
                y=previous.bbox.y + (following.bbox.y - previous.bbox.y) * amount,
                width=previous.bbox.width + (following.bbox.width - previous.bbox.width) * amount,
                height=previous.bbox.height
                + (following.bbox.height - previous.bbox.height) * amount,
            )
    return samples[-1].bbox


def _target_translation(
    bbox: NormalizedBBox,
    scale: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    center_x = bbox.x + bbox.width / 2
    center_y = bbox.y + bbox.height / 2
    # A full-frame layer scaled around its center has finite translation room.
    # Clamp the requested target-centering translation so no blank edge can enter.
    x = (0.5 - center_x) * width * scale
    y = (0.5 - center_y) * height * scale
    max_x = width * (scale - 1) / 2
    max_y = height * (scale - 1) / 2
    return max(-max_x, min(max_x, x)), max(-max_y, min(max_y, y))


def build_zoom_keyframes(
    zoom: PurposefulZoom | Mapping[str, Any],
    *,
    fps_numerator: int,
    fps_denominator: int = 1,
    width: int,
    height: int,
    layer_start_frame: int = 0,
) -> list[TransformKeyframe]:
    selected = (
        zoom if isinstance(zoom, PurposefulZoom) else PurposefulZoom.model_validate(dict(zoom))
    )
    if fps_numerator <= 0 or fps_denominator <= 0 or width <= 0 or height <= 0:
        raise ValueError("zoom keyframe dimensions and frame rate must be positive")
    start_frame = (
        microseconds_to_frame(
            selected.source_range.start_us,
            fps_numerator,
            fps_denominator,
        )
        - layer_start_frame
    )
    zoom_in_frame = (
        microseconds_to_frame(selected.zoom_in_end_us, fps_numerator, fps_denominator)
        - layer_start_frame
    )
    zoom_out_frame = (
        microseconds_to_frame(selected.zoom_out_start_us, fps_numerator, fps_denominator)
        - layer_start_frame
    )
    end_frame = (
        microseconds_to_frame(selected.source_range.end_us, fps_numerator, fps_denominator)
        - layer_start_frame
    )
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("zoom keyframe range is outside the target layer")
    if end_frame - start_frame < 4:
        raise ValueError("zoom requires at least four frames for smooth in/hold/out motion")
    zoom_in_frame = max(start_frame + 1, min(zoom_in_frame, end_frame - 2))
    zoom_out_frame = max(zoom_in_frame + 1, min(zoom_out_frame, end_frame - 1))
    peak_bbox_in = _track_bbox_at(selected, selected.zoom_in_end_us)
    peak_bbox_out = _track_bbox_at(selected, selected.zoom_out_start_us)
    in_x, in_y = _target_translation(peak_bbox_in, selected.peak_scale, width, height)
    out_x, out_y = _target_translation(peak_bbox_out, selected.peak_scale, width, height)
    return [
        TransformKeyframe(frame=start_frame, x=0, y=0, scale=1, easing="linear"),
        TransformKeyframe(
            frame=zoom_in_frame,
            x=in_x,
            y=in_y,
            scale=selected.peak_scale,
            easing="ease_in",
        ),
        TransformKeyframe(
            frame=zoom_out_frame,
            x=out_x,
            y=out_y,
            scale=selected.peak_scale,
            easing="ease_in_out",
        ),
        TransformKeyframe(frame=end_frame, x=0, y=0, scale=1, easing="ease_out"),
    ]


__all__ = [
    "FocusPacingPlan",
    "FocusPacingPolicy",
    "ForbiddenContentCheck",
    "NormalizedBBox",
    "OperatorRequest",
    "PromptSpeedup",
    "PurposefulZoom",
    "TargetSample",
    "TimeRange",
    "build_focus_pacing_plan",
    "build_zoom_keyframes",
    "classify_speedup_candidate",
    "classify_zoom_candidate",
    "read_focus_pacing_plan",
    "review_batch",
    "validate_focus_pacing_plan",
    "write_focus_pacing_plan",
]
