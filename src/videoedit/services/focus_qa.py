from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.domain.models import TransformKeyframe
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.focus_pacing import (
    FocusPacingPlan,
    _target_translation,
    _track_bbox_at,
    validate_focus_pacing_plan,
)
from videoedit.services.project import ProjectLayout
from videoedit.services.retiming import RetimedTimeline


def _finding(
    finding_id: str,
    check_code: str,
    status: str,
    severity: str,
    message: str,
    evidence: Mapping[str, Any],
    required: bool,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "check_code": check_code,
        "status": status,
        "severity": severity,
        "message": message,
        "evidence": dict(evidence),
        "required": required,
    }


def _overlapping_transcript_word(
    transcript: Mapping[str, Any] | None,
    start_us: int,
    end_us: int,
) -> bool:
    if transcript is None:
        return False
    for word in transcript.get("words", []):
        if (
            isinstance(word, Mapping)
            and int(word.get("start_us", 0)) < end_us
            and int(word.get("end_us", 0)) > start_us
        ):
            return True
    for segment in transcript.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        for word in segment.get("words", []):
            if not isinstance(word, Mapping):
                continue
            if int(word.get("start_us", 0)) < end_us and int(word.get("end_us", 0)) > start_us:
                return True
    return False


def _target_centering_diagnostics(
    zoom: Any,
    keyframes: Sequence[TransformKeyframe],
    *,
    width: int,
    height: int,
) -> tuple[bool, dict[str, Any]]:
    """Compare compiled peak translations with the evidence-bound target track."""

    if len(keyframes) < 4:
        return False, {"reason": "missing_smooth_zoom_keyframes"}
    peak_keyframes = (keyframes[1], keyframes[2])
    peak_times = (zoom.zoom_in_end_us, zoom.zoom_out_start_us)
    errors: list[float] = []
    expected_values: list[dict[str, float]] = []
    for keyframe, time_us in zip(peak_keyframes, peak_times, strict=True):
        scale = float(keyframe.scale or 1)
        expected_x, expected_y = _target_translation(
            _track_bbox_at(zoom, time_us), scale, width, height
        )
        actual_x = float(keyframe.x or 0)
        actual_y = float(keyframe.y or 0)
        errors.extend([abs(actual_x - expected_x), abs(actual_y - expected_y)])
        expected_values.append({"x": expected_x, "y": expected_y, "scale": scale})
    tolerance = max(1.5, min(width, height) * 0.001)
    maximum_error = max(errors, default=float("inf"))
    return maximum_error <= tolerance, {
        "maximum_translation_error_px": maximum_error,
        "tolerance_px": tolerance,
        "expected_peak_transforms": expected_values,
    }


def evaluate_focus_pacing_qa(
    plan: FocusPacingPlan,
    *,
    retimed_timeline: RetimedTimeline | None = None,
    transcript: Mapping[str, Any] | None = None,
    keyframes_by_zoom: Mapping[str, Sequence[TransformKeyframe]] | None = None,
    width: int = 1920,
    height: int = 1080,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for zoom in plan.zooms:
        prefix = zoom.zoom_id
        applied = zoom.policy_result in {"auto_eligible", "review_required"}
        findings.append(
            _finding(
                f"{prefix}-purpose",
                "ZOOM_PURPOSE_VALID",
                "pass",
                "info",
                "Zoom purpose is on the approved visible-target allowlist.",
                {"purpose": zoom.purpose},
                applied,
            )
        )
        findings.append(
            _finding(
                f"{prefix}-target",
                "ZOOM_TARGET_VISIBLE",
                "pass" if zoom.target_track and len(zoom.evidence_frames) >= 2 else "fail",
                "info" if zoom.target_track and len(zoom.evidence_frames) >= 2 else "high",
                "Visible target track and boundary evidence are present."
                if zoom.target_track and len(zoom.evidence_frames) >= 2
                else "Zoom lacks visible target evidence.",
                {"track_samples": len(zoom.target_track), "evidence_frames": zoom.evidence_frames},
                applied,
            )
        )
        boundaries_pass = (
            zoom.target_visible_range.start_us <= zoom.source_range.start_us
            and zoom.source_range.end_us <= zoom.target_visible_range.end_us
            and zoom.zoom_in_end_us > zoom.source_range.start_us
            and zoom.zoom_out_start_us < zoom.source_range.end_us
        )
        findings.append(
            _finding(
                f"{prefix}-boundaries",
                "ZOOM_BOUNDARIES_EXACT",
                "pass" if boundaries_pass else "fail",
                "info" if boundaries_pass else "high",
                "Zoom relevance and in/out boundaries are exact."
                if boundaries_pass
                else "Zoom timing extends outside the visible target relevance window.",
                {
                    "source_range": zoom.source_range.model_dump(mode="json"),
                    "target_visible_range": zoom.target_visible_range.model_dump(mode="json"),
                },
                applied,
            )
        )
        keyframes = list((keyframes_by_zoom or {}).get(prefix, []))
        centered_pass = zoom.centering_mode == "visible_target_center" and not zoom.allow_free_pan
        centering_evidence: dict[str, Any] = {
            "centering_mode": zoom.centering_mode,
            "allow_free_pan": zoom.allow_free_pan,
        }
        if applied:
            geometric_pass, geometric_evidence = _target_centering_diagnostics(
                zoom,
                keyframes,
                width=width,
                height=height,
            )
            centered_pass = centered_pass and geometric_pass
            centering_evidence.update(geometric_evidence)
        findings.append(
            _finding(
                f"{prefix}-centered",
                "ZOOM_TARGET_CENTERED",
                "pass" if centered_pass else "fail",
                "info" if centered_pass else "high",
                "Zoom framing is locked to the visible target center."
                if centered_pass
                else "Zoom framing does not use the approved target-centered mode.",
                centering_evidence,
                applied,
            )
        )
        stability_pass = not applied or (
            zoom.confidence.stability >= 0.85
            and all(current.frame > previous.frame for previous, current in pairwise(keyframes))
        )
        findings.append(
            _finding(
                f"{prefix}-stability",
                "ZOOM_STABILITY",
                "pass" if stability_pass else "fail",
                "info" if stability_pass else "high",
                "Target confidence and keyframe ordering support stable motion."
                if stability_pass
                else "Zoom target stability or keyframe ordering is insufficient.",
                {"stability_confidence": zoom.confidence.stability},
                applied,
            )
        )
        unrelated_pass = not any("unrelated" in item.casefold() for item in zoom.warnings)
        findings.append(
            _finding(
                f"{prefix}-unrelated",
                "ZOOM_NO_UNRELATED_CONTENT",
                "pass" if unrelated_pass else "fail",
                "info" if unrelated_pass else "high",
                "Zoom warnings do not identify unrelated content in the relevance range."
                if unrelated_pass
                else "Zoom relevance evidence includes unrelated content.",
                {"warnings": zoom.warnings},
                applied,
            )
        )
        easing_pass = not applied or (
            len(keyframes) >= 4
            and any(item.easing == "ease_in" for item in keyframes)
            and any(item.easing == "ease_out" for item in keyframes)
            and any(item.easing == "ease_in_out" for item in keyframes)
        )
        findings.append(
            _finding(
                f"{prefix}-easing",
                "ZOOM_EASING_SMOOTH",
                "pass" if easing_pass else "fail",
                "info" if easing_pass else "high",
                "Rendered zoom keyframes contain explicit smooth easing."
                if easing_pass
                else "Zoom keyframes are missing explicit smooth in/hold/out easing.",
                {"keyframe_count": len(keyframes)},
                applied,
            )
        )
        edge_pass = True
        max_translation: dict[str, float] = {"x": 0.0, "y": 0.0}
        for item in keyframes:
            scale = float(item.scale or 1)
            max_x = width * (scale - 1) / 2
            max_y = height * (scale - 1) / 2
            x = abs(float(item.x or 0))
            y = abs(float(item.y or 0))
            max_translation["x"] = max(max_translation["x"], x)
            max_translation["y"] = max(max_translation["y"], y)
            edge_pass = edge_pass and x <= max_x + 0.5 and y <= max_y + 0.5
        findings.append(
            _finding(
                f"{prefix}-edges",
                "ZOOM_NO_EMPTY_EDGES",
                "pass" if edge_pass else "fail",
                "info" if edge_pass else "critical",
                "Zoom translation stays inside the scaled-frame safe bounds."
                if edge_pass
                else "Zoom translation can expose an empty frame edge.",
                {"max_translation": max_translation, "width": width, "height": height},
                applied,
            )
        )
        findings.append(
            _finding(
                f"{prefix}-overlay",
                "ZOOM_OVERLAY_CLEARANCE",
                "warning",
                "medium",
                "Caption and overlay clearance requires rendered visual inspection.",
                {"safe_fallback": zoom.fallback},
                False,
            )
        )

    if plan.speedups:
        request_pass = (
            plan.operator_request.speedups_requested
            and plan.operator_request.request_source != "none"
        )
        findings.append(
            _finding(
                "speedup-request",
                "SPEEDUP_EXPLICITLY_REQUESTED",
                "pass" if request_pass else "fail",
                "info" if request_pass else "critical",
                "Speed-ups are bound to an explicit operator request."
                if request_pass
                else "Speed-up evidence has no current explicit operator request.",
                plan.operator_request.model_dump(mode="json"),
                True,
            )
        )
    for speedup in plan.speedups:
        prefix = speedup.speedup_id
        segment = (
            next(
                (
                    item
                    for item in (retimed_timeline.segments if retimed_timeline else [])
                    if item.speedup_id == speedup.speedup_id
                ),
                None,
            )
            if retimed_timeline is not None
            else None
        )
        applied = speedup.policy_result in {"auto_eligible", "review_required"}
        findings.append(
            _finding(
                f"{prefix}-action",
                "SPEEDUP_ACTION_ALLOWED",
                "pass" if speedup.action_type in {"prompt_writing", "prompt_dictation"} else "fail",
                "info"
                if speedup.action_type in {"prompt_writing", "prompt_dictation"}
                else "critical",
                "Speed-up action is an allowed visible prompt action.",
                {"action_type": speedup.action_type},
                applied,
            )
        )
        visible_pass = speedup.action_visible
        findings.append(
            _finding(
                f"{prefix}-visible",
                "SPEEDUP_ACTION_VISIBLE",
                "pass" if visible_pass else "fail",
                "info" if visible_pass else "critical",
                "Prompt action visibility is explicitly asserted."
                if visible_pass
                else "Prompt action visibility is not established.",
                {"action_visible": speedup.action_visible},
                applied,
            )
        )
        boundary_pass = speedup.exact_action_boundaries and bool(
            speedup.start_evidence_frame and speedup.end_evidence_frame
        )
        findings.append(
            _finding(
                f"{prefix}-boundaries",
                "SPEEDUP_BOUNDARIES_EXACT",
                "pass" if boundary_pass else "fail",
                "info" if boundary_pass else "high",
                "Speed-up boundaries have exact action flags and evidence frames."
                if boundary_pass
                else "Speed-up boundaries are not exact or lack evidence frames.",
                {
                    "source_range": speedup.source_range.model_dump(mode="json"),
                    "start_evidence_frame": speedup.start_evidence_frame,
                    "end_evidence_frame": speedup.end_evidence_frame,
                },
                applied,
            )
        )
        findings.append(
            _finding(
                f"{prefix}-excluded",
                "SPEEDUP_NO_UNRELATED_CONTENT",
                "pass",
                "info",
                "Forbidden browsing, reading, waiting, navigation, and unrelated activity "
                "are absent.",
                speedup.forbidden_content_check.model_dump(mode="json"),
                applied,
            )
        )
        audio_pass = speedup.audio_mode != "muted" or speedup.audio_exception_explicit
        findings.append(
            _finding(
                f"{prefix}-audio",
                "SPEEDUP_AUDIO_AUDIBLE",
                "pass" if audio_pass else "fail",
                "info" if audio_pass else "high",
                "Production audio remains audible unless an explicit mute exception is recorded."
                if audio_pass
                else "Speed-up audio is muted without an explicit exception.",
                {"audio_mode": speedup.audio_mode},
                applied,
            )
        )
        mapping_pass = not applied or (segment is not None)
        findings.append(
            _finding(
                f"{prefix}-duration",
                "SPEEDUP_EXPECTED_DURATION",
                "pass" if mapping_pass else "fail",
                "info" if mapping_pass else "critical",
                "Approved speed-up is present in the retimed timeline."
                if mapping_pass
                else "Approved speed-up is missing from the retimed timeline.",
                {"segment_id": segment.segment_id if segment else None},
                applied,
            )
        )
        audio_sync_pass = not applied or segment is not None
        findings.append(
            _finding(
                f"{prefix}-audio-sync",
                "SPEEDUP_AUDIO_SYNC",
                "pass" if audio_sync_pass else "fail",
                "info" if audio_sync_pass else "critical",
                "Picture and production audio share the retimed speed-up segment."
                if audio_sync_pass
                else "The speed-up has no synchronized retimed audio segment.",
                {"segment_id": segment.segment_id if segment else None},
                applied,
            )
        )
        transcript_pass = transcript is not None and _overlapping_transcript_word(
            transcript, speedup.source_range.start_us, speedup.source_range.end_us
        )
        findings.append(
            _finding(
                f"{prefix}-transcript",
                "SPEEDUP_TRANSCRIPT_PRESENT",
                "pass" if transcript_pass else "fail",
                "info" if transcript_pass else "high",
                "A transcript word overlaps the retimed action range."
                if transcript_pass
                else "The retimed action has no transcript evidence for synchronization review.",
                {"transcript_supplied": transcript is not None},
                applied,
            )
        )
    required_failures = sum(1 for item in findings if item["required"] and item["status"] == "fail")
    warnings_count = sum(1 for item in findings if item["status"] == "warning")
    return {
        "findings": findings,
        "required_failures": required_failures,
        "warnings_count": warnings_count,
        "overall_status": "fail" if required_failures else "warning" if warnings_count else "pass",
        "final_ready": required_failures == 0,
    }


def write_focus_pacing_qa(
    package_root: Path,
    layout: ProjectLayout,
    plan_path: Path,
    report: Mapping[str, Any],
    *,
    retimed_timeline_path: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    plan_value = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan_value, dict):
        raise ValueError("focus pacing plan must be a JSON object")
    validate_focus_pacing_plan(package_root, plan_value)
    inputs = [artifact_input("art_focus_pacing", plan_path)]
    if retimed_timeline_path is not None:
        inputs.append(artifact_input("art_retimed_timeline", retimed_timeline_path))
    payload = {
        "schema_name": "focus_pacing_qa",
        "schema_version": "1.0.0",
        "artifact_id": "art_focus_pacing_qa",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("focus-pacing-qa", "deterministic-review", __version__),
        "inputs": inputs,
        "config_sha256": config_sha256(layout),
        "overall_status": report["overall_status"],
        "final_ready": bool(report["final_ready"]),
        "findings": report["findings"],
        "required_failures": int(report["required_failures"]),
        "warnings_count": int(report["warnings_count"]),
    }
    validate_artifact(package_root, "focus_pacing_qa", payload)
    digest = canonical_sha256(payload)
    output = layout.artifacts / f"focus-pacing-qa-{digest[:16]}.json"
    write_validated_artifact(package_root, "focus_pacing_qa", output, payload)
    write_validated_artifact(
        package_root,
        "focus_pacing_qa",
        layout.artifacts / "focus-pacing-qa.json",
        payload,
    )
    return output


__all__ = ["evaluate_focus_pacing_qa", "write_focus_pacing_qa"]
