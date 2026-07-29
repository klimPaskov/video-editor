from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import now_iso, validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.qa_override import evaluate_qa_override


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = layout.root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _file_ref(layout: ProjectLayout, path: Path, description: str) -> dict[str, str]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    return {
        "artifact_id": description.replace(" ", "_").lower(),
        "path": str(selected),
        "sha256": sha256_file(selected),
    }


def _verify_package(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
) -> dict[str, Any]:
    package_path = _owned_path(layout, path, "segment review package")
    package = _read_object(package_path, "segment review package")
    validate_artifact(package_root, "segment_review_package", package)
    if package["project_id"] != layout.root.name:
        raise PlanningValidationError("segment review package belongs to another project")
    for key in (
        "preview",
        "contact_sheet",
        "transcript_excerpt",
        "transcript_markdown",
        "effect_summary",
        "diagnostics",
        "fixes_template",
    ):
        value = package[key]
        child = _owned_path(layout, Path(str(value["path"])), f"review package {key}")
        if not child.is_file() or sha256_file(child) != value["sha256"]:
            raise PlanningValidationError(f"review package {key} reference is stale")
    return package


def _validated_report(
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


def lock_segment_revision(
    package_root: Path,
    layout: ProjectLayout,
    review_path: Path,
    review_package_path: Path,
    transcript_comparison_path: Path,
    segment_qa_path: Path,
    visual_qa_path: Path,
    composition_bundle_path: Path,
) -> Path:
    """Persist an immutable lock for an approved segment without freezing future revisions."""

    selected_review = _owned_path(layout, review_path, "Gate 2 decision")
    review = _read_object(selected_review, "Gate 2 decision")
    validate_artifact(package_root, "segment_review", review)
    if review["decision"] != "approved":
        raise PlanningValidationError("only an approved Gate 2 decision can be locked")
    if review["locked"]:
        raise PlanningValidationError("Gate 2 decision is already marked locked")
    package = _verify_package(package_root, layout, review_package_path)
    comparison_path = _owned_path(layout, transcript_comparison_path, "transcript comparison")
    segment_qa_path = _owned_path(layout, segment_qa_path, "segment QA report")
    visual_qa_path = _owned_path(layout, visual_qa_path, "segment visual QA report")
    composition_path = _owned_path(layout, composition_bundle_path, "composition code bundle")
    comparison = _validated_report(
        package_root,
        layout,
        comparison_path,
        "segment_transcript_comparison",
        "transcript comparison",
    )
    segment_qa = _validated_report(
        package_root, layout, segment_qa_path, "segment_qa_report", "segment QA report"
    )
    visual_qa = _validated_report(
        package_root, layout, visual_qa_path, "segment_visual_qa_report", "segment visual QA report"
    )
    if not composition_path.is_file():
        raise PlanningValidationError(f"composition code bundle does not exist: {composition_path}")
    revision_id = str(review["revision_id"])
    segment_id = str(review["segment_id"])
    if package["revision_id"] != revision_id or package["segment_id"] != segment_id:
        raise PlanningValidationError("Gate 2 decision and review package differ")
    if comparison["revision_id"] != revision_id:
        raise PlanningValidationError("transcript comparison is bound to another revision")
    comparison_scope = comparison.get("scope")
    if not isinstance(comparison_scope, dict):
        raise PlanningValidationError("transcript comparison has no segment scope")
    if comparison_scope.get("segment_id") != segment_id:
        raise PlanningValidationError("transcript comparison belongs to another segment")
    if comparison_scope.get("source_range") != package.get("source_range"):
        raise PlanningValidationError(
            "transcript comparison range does not match the review package"
        )
    if segment_qa["revision_id"] != revision_id or visual_qa["revision_id"] != revision_id:
        raise PlanningValidationError("QA reports are bound to another revision")
    override_ref = review.get("qa_override")
    override_status: str | None = None
    selected_override: Path | None = None
    if override_ref is not None:
        if not isinstance(override_ref, dict):
            raise PlanningValidationError("Gate 2 QA override reference is invalid")
        selected_override = _owned_path(layout, Path(str(override_ref["path"])), "QA override")
        if (
            not selected_override.is_file()
            or override_ref["sha256"] != sha256_file(selected_override)
            or override_ref["size_bytes"] != selected_override.stat().st_size
        ):
            raise PlanningValidationError("Gate 2 QA override reference is stale")
        override_status = str(
            evaluate_qa_override(package_root, layout, segment_qa_path, selected_override)["status"]
        )
        if override_status != "ready":
            raise PlanningValidationError("cannot lock a segment with an incomplete QA override")
    if comparison.get("sequence_status") != "pass" and override_status != "ready":
        raise PlanningValidationError("cannot lock a segment with a failing transcript comparison")
    if (
        not segment_qa.get("final_ready", False) and override_status != "ready"
    ) or not visual_qa.get("final_ready", False):
        raise PlanningValidationError("cannot lock a segment with non-final-ready QA")

    expected_hashes = {
        "preview_sha256": str(package["preview"]["sha256"]),
        "transcript_comparison_sha256": sha256_file(comparison_path),
        "effect_assets_sha256": sha256_file(
            _owned_path(layout, Path(str(package["effect_summary"]["path"])), "effect summary")
        ),
        "composition_bundle_sha256": sha256_file(composition_path),
        "qa_report_sha256": sha256_file(segment_qa_path),
        "review_package_sha256": sha256_file(review_package_path),
        "visual_qa_sha256": sha256_file(visual_qa_path),
        "segment_qa_sha256": sha256_file(segment_qa_path),
    }
    if selected_override is not None:
        expected_hashes["qa_override_sha256"] = sha256_file(selected_override)
    # The decision must still bind exactly to the artifacts being locked.
    if review["bound_hashes"] != expected_hashes:
        raise PlanningValidationError("Gate 2 decision bindings are stale")

    lock_path = layout.revision_root(revision_id) / "locks" / f"{segment_id}.lock.json"
    with ProjectLock(layout, stage="segment_lock", revision_id=revision_id):
        if lock_path.is_file():
            current = _read_object(lock_path, "segment lock")
            validate_artifact(package_root, "segment_lock", current)
            if current["review"]["sha256"] == sha256_file(selected_review):
                return lock_path
            raise StateConflictError("segment lock already exists with different evidence")
        staging_root = layout.staging / "segment-lock" / f"{revision_id}-{segment_id}"
        if staging_root.exists():
            failed_root = staging_root.with_name(f"{staging_root.name}.failed")
            if failed_root.exists():
                failed_root = staging_root.with_name(f"{staging_root.name}.failed-2")
            os.replace(staging_root, failed_root)
        staging_root.mkdir(parents=True, exist_ok=False)
        payload = {
            "schema_name": "segment_lock",
            "schema_version": "1.0.0",
            "artifact_id": f"art_segment_lock_{segment_id}_{revision_id}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "segment_id": segment_id,
            "created_at": now_iso(),
            "review": {
                "artifact_id": str(review["artifact_id"]),
                "path": str(selected_review),
                "sha256": sha256_file(selected_review),
            },
            "bound_hashes": expected_hashes,
            "locked": True,
            "status": "complete",
        }
        write_validated_artifact(
            package_root, "segment_lock", staging_root / lock_path.name, payload
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root / lock_path.name, lock_path)
        return lock_path


def is_segment_locked(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    segment_id: str,
) -> bool:
    lock_path = layout.revision_root(revision_id) / "locks" / f"{segment_id}.lock.json"
    if not lock_path.is_file():
        return False
    lock = _read_object(lock_path, "segment lock")
    validate_artifact(package_root, "segment_lock", lock)
    return bool(lock["locked"])
