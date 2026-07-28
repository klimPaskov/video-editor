from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p10-07c"
VISUAL_QA_PRODUCER_VERSION = f"{__version__}:{IMPLEMENTATION_VERSION}"


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


def _file_ref(artifact_id: str, path: Path) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _finding(
    finding_id: str,
    check_code: str,
    status: str,
    severity: str,
    message: str,
    evidence: dict[str, Any],
    required: bool,
    repair_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "check_code": check_code,
        "status": status,
        "severity": severity,
        "message": message,
        "time_range": None,
        "evidence": evidence,
        "required": required,
        "repair_hint": repair_hint,
    }


def _verify_package_ref(layout: ProjectLayout, value: object, description: str) -> Path:
    if not isinstance(value, Mapping):
        raise PlanningValidationError(f"{description} reference is missing")
    path = _owned_path(layout, Path(str(value.get("path", ""))), description)
    if not path.is_file() or sha256_file(path) != value.get("sha256"):
        raise PlanningValidationError(f"{description} reference is stale")
    return path


def _is_pip_candidate(layer: Mapping[str, Any]) -> bool:
    """Identify bounded media overlays without treating middle text as PIP.

    The visual-timeline schema does not expose a dedicated PIP discriminator.
    An explicit ``pip`` source marker remains an opt-in signal. Otherwise,
    only image/video layers in the middle pass with explicit dimensions are
    inferred as bounded overlays; full-frame media and text plates are not.
    """

    if str(layer.get("kind", "")) not in {"image", "video"}:
        return False
    source = str(layer.get("src", "")).lower()
    if "pip" in source:
        return True
    if str(layer.get("role", "")) != "middle":
        return False
    transform = layer.get("transform")
    return isinstance(transform, Mapping) and (
        transform.get("width") is not None and transform.get("height") is not None
    )


def _load_package(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    package = _read_object(path, "segment review package")
    validate_artifact(package_root, "segment_review_package", package)
    if package["project_id"] != layout.root.name:
        raise PlanningValidationError("segment review package belongs to another project")
    for name in (
        "preview",
        "contact_sheet",
        "transcript_excerpt",
        "transcript_markdown",
        "effect_summary",
        "diagnostics",
        "fixes_template",
    ):
        _verify_package_ref(layout, package[name], f"review package {name}")
    effect_summary_path = _verify_package_ref(layout, package["effect_summary"], "effect summary")
    diagnostics_path = _verify_package_ref(layout, package["diagnostics"], "segment diagnostics")
    effect_summary = _read_object(effect_summary_path, "segment effect summary")
    validate_artifact(package_root, "segment_effect_summary", effect_summary)
    diagnostics = _read_object(diagnostics_path, "segment diagnostics")
    validate_artifact(package_root, "segment_diagnostics", diagnostics)
    return package, effect_summary, diagnostics


def _timeline_checks(timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    duration_frames = int(str(timeline.get("duration_frames", 0)))
    layers = timeline.get("layers", [])
    if not isinstance(layers, list):
        layers = []
    out_of_bounds: list[str] = []
    frame_ranges: list[tuple[int, int, int, str]] = []
    z_order: dict[int, list[tuple[int, int, str]]] = {}
    pip_candidates: list[Mapping[str, Any]] = []
    screen_candidates: list[str] = []
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        layer_id = str(layer.get("id", "<unknown>"))
        start_frame = int(str(layer.get("start_frame", 0)))
        end_frame = start_frame + int(str(layer.get("duration_frames", 0)))
        if start_frame < 0 or end_frame > duration_frames or end_frame <= start_frame:
            out_of_bounds.append(layer_id)
        z_index = int(str(layer.get("z_index", 0)))
        frame_ranges.append((start_frame, end_frame, z_index, layer_id))
        z_order.setdefault(z_index, []).append((start_frame, end_frame, layer_id))
        source = str(layer.get("src", "")).lower()
        if _is_pip_candidate(layer):
            pip_candidates.append(layer)
        if any(value in source for value in ("screen", "display", "desktop")):
            screen_candidates.append(layer_id)
    z_collisions: list[tuple[int, str, str]] = []
    for z_index, ranges in z_order.items():
        for index, (start_a, end_a, layer_a) in enumerate(ranges):
            for start_b, end_b, layer_b in ranges[index + 1 :]:
                if start_a < end_b and start_b < end_a:
                    z_collisions.append((z_index, layer_a, layer_b))
    findings.append(
        _finding(
            "finding_timeline_bounds",
            "TIMELINE_BOUNDS",
            "fail" if out_of_bounds else "pass",
            "high" if out_of_bounds else "info",
            "Visual layers stay within the composition duration"
            if not out_of_bounds
            else "Visual layers extend outside the composition duration",
            {"out_of_bounds_layer_ids": out_of_bounds, "duration_frames": duration_frames},
            True,
        )
    )
    findings.append(
        _finding(
            "finding_z_order",
            "Z_ORDER",
            "fail" if z_collisions else "pass",
            "high" if z_collisions else "info",
            "Overlapping layers have deterministic z-order"
            if not z_collisions
            else "Overlapping layers share a z-index",
            {"collisions": z_collisions},
            True,
        )
    )
    safe_area = timeline.get("caption_safe_area")
    captions = timeline.get("captions", [])
    caption_check = safe_area is not None or not isinstance(captions, list) or not captions
    findings.append(
        _finding(
            "finding_safe_area",
            "SAFE_AREA",
            "pass" if caption_check else "fail",
            "info" if caption_check else "high",
            "Caption safe-area geometry is declared"
            if caption_check
            else "Captions are present without a declared safe area",
            {
                "caption_count": len(captions) if isinstance(captions, list) else 0,
                "safe_area": safe_area,
            },
            True,
        )
    )
    if pip_candidates:
        pip_out_of_bounds: list[str] = []
        for layer in pip_candidates:
            transform = layer.get("transform")
            if not isinstance(transform, Mapping):
                pip_out_of_bounds.append(str(layer.get("id", "<unknown>")))
                continue
            x = float(str(transform.get("x", 0)))
            y = float(str(transform.get("y", 0)))
            width = transform.get("width")
            height = transform.get("height")
            if width is None or height is None:
                pip_out_of_bounds.append(str(layer.get("id", "<unknown>")))
                continue
            scale = float(str(transform.get("scale", 1)))
            if x < 0 or y < 0 or x + float(str(width)) * scale > float(timeline["width"]):
                pip_out_of_bounds.append(str(layer.get("id", "<unknown>")))
            elif y + float(str(height)) * scale > float(timeline["height"]):
                pip_out_of_bounds.append(str(layer.get("id", "<unknown>")))
        findings.append(
            _finding(
                "finding_pip_framing",
                "PIP_FRAMING",
                "fail" if pip_out_of_bounds else "pass",
                "high" if pip_out_of_bounds else "info",
                "Picture-in-picture layers fit the composition"
                if not pip_out_of_bounds
                else "Picture-in-picture layers exceed the composition bounds",
                {
                    "candidate_layer_ids": [str(layer.get("id")) for layer in pip_candidates],
                    "out_of_bounds": pip_out_of_bounds,
                },
                True,
            )
        )
    else:
        findings.append(
            _finding(
                "finding_pip_framing",
                "PIP_FRAMING",
                "skipped",
                "low",
                "No picture-in-picture layer was declared",
                {},
                False,
            )
        )
    findings.append(
        _finding(
            "finding_hidden_screen_content",
            "HIDDEN_SCREEN_CONTENT",
            "warning" if screen_candidates else "pass",
            "medium" if screen_candidates else "info",
            "Screen-content layers require operator visibility review"
            if screen_candidates
            else "No screen-content layer was declared",
            {"screen_layer_ids": screen_candidates},
            bool(screen_candidates),
            "Inspect the screen-content preview frame by frame" if screen_candidates else None,
        )
    )
    return findings


def qa_visual_segment(
    package_root: Path,
    layout: ProjectLayout,
    review_package_path: Path,
    *,
    visual_timeline_path: Path | None = None,
) -> Path:
    """Validate hash-bound visual evidence and timeline structure for one segment."""

    selected_package_path = _owned_path(layout, review_package_path, "segment review package")
    if not selected_package_path.is_file():
        raise PlanningValidationError(
            f"segment review package does not exist: {selected_package_path}"
        )
    package, effect_summary, diagnostics = _load_package(
        package_root, layout, selected_package_path
    )
    selected_timeline = (
        _owned_path(layout, visual_timeline_path, "visual timeline")
        if visual_timeline_path is not None
        else None
    )
    timeline = None
    if selected_timeline is not None:
        if not selected_timeline.is_file():
            raise PlanningValidationError(f"visual timeline does not exist: {selected_timeline}")
        timeline = _read_object(selected_timeline, "visual timeline")
        validate_artifact(package_root, "visual_timeline", timeline)
        if timeline.get("project_id") != layout.root.name:
            raise PlanningValidationError("visual timeline belongs to another project")
    report_path = (
        layout.revision_root(str(package["revision_id"]))
        / f"visual-qa-{package['segment_id']}.json"
    )
    with ProjectLock(layout, stage="segment_visual_qa", revision_id=str(package["revision_id"])):
        if report_path.is_file():
            current = _read_object(report_path, "segment visual QA report")
            validate_artifact(package_root, "segment_visual_qa_report", current)
            current_producer = current.get("producer")
            if not isinstance(current_producer, Mapping) or (
                current_producer.get("adapter") != "review-contract"
                or current_producer.get("adapter_version") != VISUAL_QA_PRODUCER_VERSION
            ):
                raise StateConflictError("segment visual QA report exists with stale contents")
            if current["review_package"]["sha256"] == sha256_file(selected_package_path) and (
                (selected_timeline is None and current["visual_timeline"] is None)
                or (
                    selected_timeline is not None
                    and isinstance(current["visual_timeline"], Mapping)
                    and current["visual_timeline"]["sha256"] == sha256_file(selected_timeline)
                )
            ):
                return report_path
            raise StateConflictError("segment visual QA report exists but is stale")
        input_hashes = [sha256_file(selected_package_path)]
        if selected_timeline is not None:
            input_hashes.append(sha256_file(selected_timeline))
        stage_key = make_stage_key(
            "segment-visual-qa",
            IMPLEMENTATION_VERSION,
            input_hashes,
            {"segment_id": package["segment_id"]},
        )
        staging_root = (
            layout.staging / "segment-visual-qa" / f"{package['segment_id']}-{stage_key[:16]}"
        )
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)

        findings = [
            _finding(
                "finding_review_evidence",
                "REVIEW_EVIDENCE",
                "pass",
                "info",
                "Contact sheet, transcript, effects, diagnostics, and fixes template are "
                "hash-bound",
                {"package_key": package["package_key"]},
                True,
            )
        ]
        mask_items = diagnostics.get("mask_matte", [])
        if not isinstance(mask_items, list) or not mask_items:
            for code, message in (
                ("MASK_CONTINUITY", "No mask or segmentation diagnostic is present"),
                ("MATTE_FLICKER", "No person matte diagnostic is present"),
            ):
                findings.append(
                    _finding(f"finding_{code.lower()}", code, "skipped", "low", message, {}, False)
                )
        else:
            failures = [
                item
                for item in mask_items
                if isinstance(item, Mapping) and item.get("status") == "fail"
            ]
            warnings = [
                item
                for item in mask_items
                if isinstance(item, Mapping) and item.get("status") == "warning"
            ]
            status = "fail" if failures else ("warning" if warnings else "pass")
            for code in ("MASK_CONTINUITY", "MATTE_FLICKER"):
                findings.append(
                    _finding(
                        f"finding_{code.lower()}",
                        code,
                        status,
                        "high"
                        if status == "fail"
                        else ("medium" if status == "warning" else "info"),
                        "Mask/matte diagnostics passed"
                        if status == "pass"
                        else "Mask/matte diagnostics require repair or review",
                        {"diagnostics": mask_items},
                        True,
                        "Repair the mask or matte before Gate 2" if status != "pass" else None,
                    )
                )
        effects = effect_summary.get("effects", [])
        if not isinstance(effects, list) or not effects:
            findings.append(
                _finding(
                    "finding_effect_boundaries",
                    "EFFECT_BOUNDARIES",
                    "skipped",
                    "low",
                    "No segment effects were declared",
                    {},
                    False,
                )
            )
            findings.append(
                _finding(
                    "finding_asset_provenance",
                    "ASSET_PROVENANCE",
                    "skipped",
                    "low",
                    "No effect assets were declared",
                    {},
                    False,
                )
            )
        else:
            source_range = package["source_range"]
            invalid_boundaries: list[str] = []
            missing_licences: list[str] = []
            for effect in effects:
                if not isinstance(effect, Mapping):
                    invalid_boundaries.append("<invalid>")
                    continue
                effect_range = effect.get("source_range")
                if not isinstance(effect_range, Mapping) or int(effect_range["start_us"]) < int(
                    source_range["start_us"]
                ):
                    invalid_boundaries.append(str(effect.get("effect_id", "<unknown>")))
                for asset in effect.get("asset_refs", []):
                    if isinstance(asset, Mapping) and not asset.get("licence_reference"):
                        missing_licences.append(str(asset.get("asset_id", "<unknown>")))
            findings.append(
                _finding(
                    "finding_effect_boundaries",
                    "EFFECT_BOUNDARIES",
                    "fail" if invalid_boundaries else "pass",
                    "high" if invalid_boundaries else "info",
                    "Effect ranges stay inside the reviewed segment"
                    if not invalid_boundaries
                    else "Effect ranges escape the reviewed segment",
                    {"invalid_effect_ids": invalid_boundaries},
                    True,
                )
            )
            findings.append(
                _finding(
                    "finding_asset_provenance",
                    "ASSET_PROVENANCE",
                    "fail" if missing_licences else "pass",
                    "high" if missing_licences else "info",
                    "Effect assets carry licence references"
                    if not missing_licences
                    else "Effect assets are missing licence references",
                    {"asset_ids": missing_licences},
                    True,
                )
            )
        if timeline is None:
            findings.extend(
                _finding(
                    f"finding_{code.lower()}",
                    code,
                    "skipped",
                    "low",
                    message,
                    {},
                    False,
                )
                for code, message in (
                    ("TIMELINE_BOUNDS", "No visual timeline was supplied"),
                    ("Z_ORDER", "No visual timeline was supplied"),
                    ("SAFE_AREA", "No visual timeline was supplied"),
                    ("PIP_FRAMING", "No visual timeline was supplied"),
                    ("HIDDEN_SCREEN_CONTENT", "No visual timeline was supplied"),
                )
            )
        else:
            findings.extend(_timeline_checks(timeline))
        required_failures = sum(
            1 for item in findings if item["required"] and item["status"] != "pass"
        )
        summary = {
            "total": len(findings),
            "passed": sum(item["status"] == "pass" for item in findings),
            "warnings": sum(item["status"] == "warning" for item in findings),
            "failed": sum(item["status"] == "fail" for item in findings),
            "skipped": sum(item["status"] == "skipped" for item in findings),
            "required_failures": required_failures,
        }
        overall_status = (
            "fail" if summary["failed"] else ("warning" if required_failures else "pass")
        )
        report: dict[str, Any] = {
            "schema_name": "segment_visual_qa_report",
            "schema_version": "1.0.0",
            "artifact_id": f"art_segment_visual_qa_{package['segment_id']}",
            "project_id": layout.root.name,
            "revision_id": str(package["revision_id"]),
            "created_at": now_iso(),
            "producer": producer(
                "segment-visual-qa", "review-contract", VISUAL_QA_PRODUCER_VERSION
            ),
            "inputs": [artifact_input("segment_review_package", selected_package_path)],
            "scope": package["source_range"],
            "review_package": _file_ref("segment_review_package", selected_package_path),
            "visual_timeline": _file_ref("visual_timeline", selected_timeline)
            if selected_timeline is not None
            else None,
            "findings": findings,
            "summary": summary,
            "overall_status": overall_status,
            "final_ready": required_failures == 0,
        }
        if selected_timeline is not None:
            report["inputs"].append(artifact_input("visual_timeline", selected_timeline))
        write_validated_artifact(
            package_root,
            "segment_visual_qa_report",
            staging_root / report_path.name,
            report,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root / report_path.name, report_path)
        return report_path
