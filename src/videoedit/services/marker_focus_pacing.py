from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    config_sha256,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.focus_pacing import (
    build_focus_pacing_plan,
    validate_focus_pacing_plan,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p10-10a"
_NUMBER_PATTERN = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_RATE_PATTERN = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*x\b", re.IGNORECASE)
_BBOX_PATTERN = re.compile(
    r"bbox\s*=\s*\(?\s*(\d*\.??\d+)\s*[,;]\s*(\d*\.??\d+)\s*[,;]\s*(\d*\.??\d+)\s*[,;]\s*(\d*\.??\d+)\s*\)?",
    re.IGNORECASE,
)
_CONFIDENCE_PATTERN = re.compile(r"confidence\s*=\s*(\d*\.??\d+)", re.IGNORECASE)
_FRAME_PATTERN = re.compile(r"(?:start|end)_frame\s*=\s*([^\s,;]+)", re.IGNORECASE)


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


def _purpose(instruction: str) -> str | None:
    normalized = instruction.lower()
    if "prompt" in normalized or "box" in normalized:
        return "prompt_box"
    if "cursor" in normalized:
        return "relevant_cursor_action"
    if "window" in normalized:
        return "opened_window"
    if "ui" in normalized or "interface" in normalized:
        return "important_ui"
    return None


def _bbox(instruction: str) -> dict[str, float] | None:
    match = _BBOX_PATTERN.search(instruction)
    if not match:
        return None
    values = [float(value) for value in match.groups()]
    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _confidence(instruction: str) -> float | None:
    match = _CONFIDENCE_PATTERN.search(instruction)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 1 else None


def _frames(instruction: str) -> tuple[str, str] | None:
    matches = {
        match.group(0).split("=")[0].lower(): match.group(1)
        for match in _FRAME_PATTERN.finditer(instruction)
    }
    start = matches.get("start_frame")
    end = matches.get("end_frame")
    return (start, end) if start and end else None


def _candidate_zoom(marker: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    instruction = str(marker["instruction"])
    marker_range = marker["range_us"]
    start_us = int(marker_range["start_us"])
    end_us = int(marker_range["end_us"])
    purpose = _purpose(instruction)
    bbox = _bbox(instruction)
    confidence = _confidence(instruction)
    frames = _frames(instruction)
    if purpose is None:
        return None, "zoom_marker_missing_allowed_purpose"
    if bbox is None:
        return None, "zoom_marker_missing_normalized_bbox"
    if confidence is None:
        return None, "zoom_marker_missing_confidence"
    if frames is None:
        return None, "zoom_marker_missing_evidence_frames"
    duration_us = end_us - start_us
    if duration_us < 4_000_000:
        return None, "zoom_marker_range_too_short_for_smooth_motion"
    third = duration_us // 3
    peak_scale = 1.25
    scale_match = re.search(r"scale\s*=\s*(\d+(?:\.\d+)?)", instruction, re.IGNORECASE)
    if scale_match:
        peak_scale = float(scale_match.group(1))
    return (
        {
            "zoom_id": f"zoom_{marker['marker_id']}",
            "purpose": purpose,
            "source_range": {"start_us": start_us, "end_us": end_us},
            "target_visible_range": {"start_us": start_us, "end_us": end_us},
            "zoom_in_end_us": start_us + third,
            "zoom_out_start_us": end_us - third,
            "target_description": instruction,
            "target_track": [
                {"time_us": start_us, "bbox": bbox},
                {"time_us": end_us, "bbox": bbox},
            ],
            "peak_scale": peak_scale,
            "reason": instruction,
            "confidence": {
                "target_visibility": confidence,
                "target_identity": confidence,
                "boundary": confidence,
                "stability": confidence,
                "overall": confidence,
            },
            "evidence_frames": [frames[0], frames[1]],
        },
        None,
    )


def _candidate_speedup(
    marker: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    instruction = str(marker["instruction"])
    normalized = instruction.lower()
    if "writing" in normalized or "typing" in normalized:
        action_type = "prompt_writing"
    elif "dictat" in normalized or "speaking" in normalized:
        action_type = "prompt_dictation"
    else:
        return None, "speed_marker_missing_allowed_action_type"
    rate_match = _RATE_PATTERN.search(instruction)
    confidence = _confidence(instruction)
    frames = _frames(instruction)
    if rate_match is None:
        return None, "speed_marker_missing_playback_rate"
    if confidence is None:
        return None, "speed_marker_missing_confidence"
    if frames is None:
        return None, "speed_marker_missing_evidence_frames"
    if "audible" not in normalized:
        return None, "speed_marker_missing_audible_audio_evidence"
    return (
        {
            "speedup_id": f"speed_{marker['marker_id']}",
            "action_type": action_type,
            "source_range": {
                "start_us": int(marker["range_us"]["start_us"]),
                "end_us": int(marker["range_us"]["end_us"]),
            },
            "request_source": "fix_marker",
            "request_text": instruction,
            "playback_rate": float(rate_match.group(1)),
            "audio_mode": "audible_pitch_preserved",
            "audio_exception_explicit": False,
            "forbidden_content_check": {
                "contains_browsing": False,
                "contains_reading": False,
                "contains_waiting": False,
                "contains_other_action": False,
                "contains_navigation": False,
                "contains_result_inspection": False,
                "contains_loading": False,
                "contains_cursor_wandering": False,
            },
            "start_evidence_frame": frames[0],
            "end_evidence_frame": frames[1],
            "action_visibility_confidence": confidence,
            "boundary_confidence": confidence,
            "overall_confidence": confidence,
            "reason": instruction,
        },
        None,
    )


def build_marker_focus_pacing_plan(
    package_root: Path,
    layout: ProjectLayout,
    markers_path: Path,
) -> Path:
    """Translate reviewed focus markers into safe, evidence-required plan candidates."""

    selected_markers = _owned_path(layout, markers_path, "review markers")
    markers = _read_object(selected_markers, "review markers")
    validate_artifact(package_root, "review_markers", markers)
    if markers["project_id"] != layout.root.name:
        raise PlanningValidationError("review markers belong to another project")
    revision_id = str(markers["revision_id"])
    marker_hash = sha256_file(selected_markers)
    plan_path = layout.revision_root(revision_id) / "focus-pacing-plan.json"
    with ProjectLock(layout, stage="marker_focus_pacing", revision_id=revision_id):
        if plan_path.is_file():
            current = _read_object(plan_path, "focus pacing plan")
            validate_artifact(package_root, "focus_pacing_plan", current)
            if any(
                item.get("sha256") == marker_hash
                for item in current.get("inputs", [])
                if isinstance(item, dict)
            ):
                return plan_path
            raise StateConflictError("focus pacing plan exists but is bound to different markers")
        marker_items = [dict(item) for item in markers["markers"]]
        zoom_candidates: list[dict[str, Any]] = []
        speed_candidates: list[dict[str, Any]] = []
        skipped_zoom: list[dict[str, Any]] = []
        warnings: list[str] = []
        speed_marker_texts: list[str] = []
        for marker in marker_items:
            kind = str(marker["kind"])
            if kind == "ZOOM":
                candidate, warning = _candidate_zoom(marker)
                if candidate is not None:
                    zoom_candidates.append(candidate)
                elif warning:
                    skipped_zoom.append(
                        {
                            "candidate_id": f"zoom_{marker['marker_id']}",
                            "source_range": marker["range_us"],
                            "reason": warning,
                        }
                    )
                    warnings.append(f"{warning}:{marker['marker_id']}")
            elif kind == "SPEED":
                speed_marker_texts.append(str(marker["instruction"]))
                candidate, warning = _candidate_speedup(marker)
                if candidate is not None:
                    speed_candidates.append(candidate)
                elif warning:
                    warnings.append(f"{warning}:{marker['marker_id']}")
        operator_request = (
            {
                "speedups_requested": True,
                "request_source": "fix_marker",
                "request_text": " ".join(speed_marker_texts),
            }
            if speed_marker_texts
            else {"speedups_requested": False, "request_source": "none", "request_text": None}
        )
        plan = build_focus_pacing_plan(
            package_root=package_root,
            project_id=layout.root.name,
            revision_id=revision_id,
            inputs=[{"artifact_id": str(markers["artifact_id"]), "sha256": marker_hash}],
            zoom_candidates=zoom_candidates,
            speedup_candidates=speed_candidates,
            operator_request=operator_request,
            skipped_zoom_candidates=skipped_zoom,
            config_hash=config_sha256(layout),
            warnings=warnings,
        )
        validate_focus_pacing_plan(package_root, plan)
        stage_key = make_stage_key(
            "marker-focus-pacing",
            IMPLEMENTATION_VERSION,
            [marker_hash],
            {"revision_id": revision_id, "config_sha256": config_sha256(layout)},
        )
        staging_root = layout.staging / "marker-focus-pacing" / f"{revision_id}-{stage_key[:16]}"
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)
        write_validated_artifact(
            package_root, "focus_pacing_plan", staging_root / plan_path.name, plan
        )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root / plan_path.name, plan_path)
        return plan_path
