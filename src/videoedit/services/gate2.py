from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    config_sha256,
    now_iso,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.qa_override import evaluate_qa_override

GATE2_IMPLEMENTATION_VERSION = "p10-08c"


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


def _gate2_cache_binding(payload: dict[str, Any]) -> dict[str, Any]:
    binding = dict(payload)
    binding.pop("created_at", None)
    reviewer = binding.get("reviewer")
    if isinstance(reviewer, dict):
        reviewer_binding = dict(reviewer)
        reviewer_binding.pop("reviewed_at", None)
        binding["reviewer"] = reviewer_binding
    return binding


def _validate_file_artifact(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    schema_name: str,
    description: str,
) -> dict[str, Any]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    value = _read_object(selected, description)
    validate_artifact(package_root, schema_name, value)
    if value.get("project_id") != layout.root.name:
        raise PlanningValidationError(f"{description} belongs to another project")
    return value


def _verify_package(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
) -> dict[str, Any]:
    package = _validate_file_artifact(
        package_root, layout, path, "segment_review_package", "segment review package"
    )
    for name in (
        "preview",
        "contact_sheet",
        "transcript_excerpt",
        "transcript_markdown",
        "effect_summary",
        "diagnostics",
        "fixes_template",
    ):
        value = package[name]
        if not isinstance(value, dict):
            raise PlanningValidationError(f"review package {name} reference is missing")
        child = _owned_path(layout, Path(str(value["path"])), f"review package {name}")
        if not child.is_file() or sha256_file(child) != value["sha256"]:
            raise PlanningValidationError(f"review package {name} reference is stale")
    return package


def approve_segment_gate2(
    package_root: Path,
    layout: ProjectLayout,
    review_package_path: Path,
    transcript_comparison_path: Path,
    segment_qa_path: Path,
    visual_qa_path: Path,
    composition_bundle_path: Path,
    *,
    actor: str,
    role: str,
    decision: str = "approved",
    notes: str = "",
    fixes: list[dict[str, Any]] | None = None,
    qa_override_path: Path | None = None,
) -> Path:
    """Create a human Gate 2 decision bound to every current segment artifact."""

    if not actor.strip() or not role.strip():
        raise PlanningValidationError("Gate 2 actor and role are required")
    if decision not in {"approved", "changes_requested", "rejected"}:
        raise PlanningValidationError("Gate 2 decision is invalid")
    selected_package = _owned_path(layout, review_package_path, "segment review package")
    package = _verify_package(package_root, layout, selected_package)
    revision_id = str(package["revision_id"])
    segment_id = str(package["segment_id"])
    selected_comparison = _owned_path(layout, transcript_comparison_path, "transcript comparison")
    selected_segment_qa = _owned_path(layout, segment_qa_path, "segment QA report")
    selected_visual_qa = _owned_path(layout, visual_qa_path, "segment visual QA report")
    selected_composition = _owned_path(layout, composition_bundle_path, "composition code bundle")
    comparison = _validate_file_artifact(
        package_root,
        layout,
        selected_comparison,
        "segment_transcript_comparison",
        "transcript comparison",
    )
    segment_qa = _validate_file_artifact(
        package_root, layout, selected_segment_qa, "segment_qa_report", "segment QA report"
    )
    visual_qa = _validate_file_artifact(
        package_root,
        layout,
        selected_visual_qa,
        "segment_visual_qa_report",
        "segment visual QA report",
    )
    if comparison["revision_id"] != revision_id or segment_qa["revision_id"] != revision_id:
        raise PlanningValidationError("Gate 2 artifacts are bound to different revisions")
    if visual_qa["revision_id"] != revision_id:
        raise PlanningValidationError("visual QA is bound to a different revision")
    comparison_scope = comparison.get("scope")
    if not isinstance(comparison_scope, dict):
        raise PlanningValidationError("transcript comparison has no segment scope")
    if comparison_scope.get("segment_id") != segment_id:
        raise PlanningValidationError("transcript comparison belongs to another segment")
    if comparison_scope.get("source_range") != package.get("source_range"):
        raise PlanningValidationError(
            "transcript comparison range does not match the review package"
        )
    if not selected_composition.is_file():
        raise PlanningValidationError(
            f"composition code bundle does not exist: {selected_composition}"
        )

    selected_override: Path | None = None
    override_status: str | None = None
    override_ref: dict[str, Any] | None = None
    if qa_override_path is not None:
        selected_override = _owned_path(layout, qa_override_path, "QA override")
        override_result = evaluate_qa_override(
            package_root, layout, selected_segment_qa, selected_override
        )
        override_status = str(override_result["status"])
        if override_status != "ready":
            raise PlanningValidationError(
                "QA override does not cover every required non-pass segment finding"
            )
        override_payload = _read_object(selected_override, "QA override")
        override_ref = {
            "artifact_id": str(override_payload["artifact_id"]),
            "path": str(selected_override),
            "sha256": sha256_file(selected_override),
            "size_bytes": selected_override.stat().st_size,
        }
    if decision == "approved":
        blockers: list[str] = []
        if comparison.get("sequence_status") != "pass" and override_status != "ready":
            blockers.append("transcript comparison is not passing")
        if not segment_qa.get("final_ready", False) and override_status != "ready":
            blockers.append("segment QA is not final-ready")
        if not visual_qa.get("final_ready", False):
            blockers.append("visual QA is not final-ready")
        if blockers:
            raise PlanningValidationError("Gate 2 approval blocked: " + "; ".join(blockers))

    effect_summary_path = _owned_path(
        layout, Path(str(package["effect_summary"]["path"])), "effect summary"
    )
    bound_hashes = {
        "preview_sha256": str(package["preview"]["sha256"]),
        "transcript_comparison_sha256": sha256_file(selected_comparison),
        "effect_assets_sha256": sha256_file(effect_summary_path),
        "composition_bundle_sha256": sha256_file(selected_composition),
        "qa_report_sha256": sha256_file(selected_segment_qa),
        "review_package_sha256": sha256_file(selected_package),
        "visual_qa_sha256": sha256_file(selected_visual_qa),
        "segment_qa_sha256": sha256_file(selected_segment_qa),
    }
    if selected_override is not None:
        bound_hashes["qa_override_sha256"] = sha256_file(selected_override)
    selected_fixes = fixes or []
    review_key = make_stage_key(
        "segment-gate2",
        GATE2_IMPLEMENTATION_VERSION,
        list(bound_hashes.values()),
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "segment_id": segment_id,
            "config_sha256": config_sha256(layout),
            "actor": actor,
            "role": role,
            "decision": decision,
            "notes": notes,
            "fixes": selected_fixes,
        },
    )
    output_path = (
        layout.review
        / "gate2"
        / segment_id
        / revision_id
        / f"{review_key[:16]}-segment-review.json"
    )
    with ProjectLock(layout, stage="gate2_approval", revision_id=revision_id):
        if output_path.is_file():
            current = _read_object(output_path, "Gate 2 decision")
            validate_artifact(package_root, "segment_review", current)
            payload_binding = {
                "schema_name": "segment_review",
                "schema_version": "1.0.0",
                "artifact_id": f"art_segment_review_{segment_id}_{revision_id}",
                "project_id": layout.root.name,
                "revision_id": revision_id,
                "segment_id": segment_id,
                "bound_hashes": bound_hashes,
                "decision": decision,
                "reviewer": {"actor": actor, "role": role},
                "notes": notes,
                "fixes": selected_fixes,
                "locked": False,
            }
            if override_ref is not None:
                payload_binding["qa_override"] = override_ref
            if _gate2_cache_binding(current) == payload_binding:
                return output_path
            raise StateConflictError("Gate 2 decision path exists with stale contents")
        staging_root = layout.staging / "gate2" / review_key
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)
        payload: dict[str, Any] = {
            "schema_name": "segment_review",
            "schema_version": "1.0.0",
            "artifact_id": f"art_segment_review_{segment_id}_{revision_id}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "segment_id": segment_id,
            "created_at": now_iso(),
            "bound_hashes": bound_hashes,
            "decision": decision,
            "reviewer": {"actor": actor, "role": role, "reviewed_at": now_iso()},
            "notes": notes,
            "fixes": selected_fixes,
            "locked": False,
        }
        if override_ref is not None:
            payload["qa_override"] = override_ref
        write_validated_artifact(
            package_root, "segment_review", staging_root / output_path.name, payload
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root / output_path.name, output_path)
        return output_path
