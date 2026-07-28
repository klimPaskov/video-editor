from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    canonical_sha256,
    now_iso,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_lock import _owned_path

_GATE2_LOCK_BOUND_KEYS = frozenset(
    {
        "preview_sha256",
        "transcript_comparison_sha256",
        "effect_assets_sha256",
        "composition_bundle_sha256",
        "qa_report_sha256",
        "review_package_sha256",
        "visual_qa_sha256",
        "segment_qa_sha256",
    }
)
_GATE2_LOCK_BOUND_KEYS_WITH_OVERRIDE = _GATE2_LOCK_BOUND_KEYS | {"qa_override_sha256"}


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _validate_plan_bundle(
    package_root: Path,
    layout: ProjectLayout,
    plan_paths: Mapping[str, Path],
    revision_id: str,
) -> tuple[dict[str, str], dict[str, Path]]:
    if not plan_paths:
        raise PlanningValidationError("Gate 3 requires at least one current plan")
    hashes: dict[str, str] = {}
    resolved: dict[str, Path] = {}
    for schema_name, raw_path in sorted(plan_paths.items()):
        path = _owned_path(layout, raw_path, f"{schema_name} plan")
        if not path.is_file():
            raise PlanningValidationError(f"{schema_name} plan does not exist: {path}")
        value = _read_object(path, f"{schema_name} plan")
        validate_artifact(package_root, schema_name, value)
        if value.get("project_id") != layout.root.name:
            raise PlanningValidationError(f"{schema_name} plan belongs to another project")
        if value.get("revision_id") not in (None, revision_id):
            raise PlanningValidationError(f"{schema_name} plan belongs to another revision")
        hashes[schema_name] = sha256_file(path)
        resolved[schema_name] = path
    return hashes, resolved


def _validate_gate2_lock(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    *,
    revision_id: str,
) -> str:
    """Validate a lock's immutable Gate 2 decision before binding it at Gate 3."""

    selected = _owned_path(layout, path, "Gate 2 segment lock")
    lock = _read_object(selected, "Gate 2 segment lock")
    validate_artifact(package_root, "segment_lock", lock)
    if lock["project_id"] != layout.root.name or lock["revision_id"] != revision_id:
        raise PlanningValidationError("Gate 2 lock belongs to another project or revision")
    if lock["locked"] is not True or lock["status"] != "complete":
        raise PlanningValidationError("Gate 3 cannot bind an unlocked Gate 2 segment")
    if set(lock["bound_hashes"]) not in {
        _GATE2_LOCK_BOUND_KEYS,
        _GATE2_LOCK_BOUND_KEYS_WITH_OVERRIDE,
    }:
        raise PlanningValidationError("Gate 2 lock is missing or has unexpected bound hashes")

    review_ref = lock["review"]
    review_path = _owned_path(layout, Path(str(review_ref["path"])), "Gate 2 decision")
    if not review_path.is_file() or sha256_file(review_path) != review_ref["sha256"]:
        raise PlanningValidationError("Gate 2 lock review reference is stale")
    review = _read_object(review_path, "Gate 2 decision")
    validate_artifact(package_root, "segment_review", review)
    if (
        review["project_id"] != layout.root.name
        or review["revision_id"] != revision_id
        or review["segment_id"] != lock["segment_id"]
    ):
        raise PlanningValidationError(
            "Gate 2 lock review belongs to another project, revision, or segment"
        )
    if review["decision"] != "approved" or review["locked"] is not False:
        raise PlanningValidationError(
            "Gate 2 lock does not reference an approved unlocked decision"
        )
    if review["bound_hashes"] != lock["bound_hashes"]:
        raise PlanningValidationError("Gate 2 lock bound hashes do not match its decision")
    has_override_hash = "qa_override_sha256" in lock["bound_hashes"]
    has_override_ref = "qa_override" in review
    if has_override_hash != has_override_ref:
        raise PlanningValidationError("Gate 2 QA override binding is incomplete")
    if has_override_ref:
        override_ref = review["qa_override"]
        if not isinstance(override_ref, Mapping):
            raise PlanningValidationError("Gate 2 QA override reference is invalid")
        override_path = _owned_path(layout, Path(str(override_ref["path"])), "QA override")
        if (
            not override_path.is_file()
            or override_ref["sha256"] != sha256_file(override_path)
            or lock["bound_hashes"]["qa_override_sha256"] != sha256_file(override_path)
        ):
            raise PlanningValidationError("Gate 2 QA override binding is stale")
    return sha256_file(selected)


def approve_gate3(
    package_root: Path,
    layout: ProjectLayout,
    final_qa_path: Path,
    watchthrough_path: Path,
    asset_manifest_path: Path,
    composition_bundle_path: Path,
    delivery_profile_path: Path,
    plan_paths: Mapping[str, Path],
    gate2_paths: Sequence[Path],
    *,
    actor: str,
    role: str,
    decision: str = "approved",
    notes: str = "",
    revision_id: str = "rev_001",
) -> Path:
    """Create an immutable human Gate 3 decision bound to the final candidate."""

    if not actor.strip() or not role.strip():
        raise PlanningValidationError("Gate 3 actor and role are required")
    if decision not in {"approved", "changes_requested", "rejected"}:
        raise PlanningValidationError("Gate 3 decision is invalid")
    selected_qa = _owned_path(layout, final_qa_path, "final QA report")
    selected_watch = _owned_path(layout, watchthrough_path, "watch-through record")
    selected_asset = _owned_path(layout, asset_manifest_path, "asset manifest")
    selected_composition = _owned_path(layout, composition_bundle_path, "composition bundle")
    selected_profile = _owned_path(layout, delivery_profile_path, "delivery profile")
    qa = _read_object(selected_qa, "final QA report")
    watch = _read_object(selected_watch, "watch-through record")
    asset = _read_object(selected_asset, "asset manifest")
    validate_artifact(package_root, "final_qa_report", qa)
    validate_artifact(package_root, "watchthrough_record", watch)
    validate_artifact(package_root, "asset_manifest", asset)
    for path, description in (
        (selected_composition, "composition bundle"),
        (selected_profile, "delivery profile"),
    ):
        if not path.is_file():
            raise PlanningValidationError(f"{description} does not exist: {path}")
    if qa["project_id"] != layout.root.name or watch["project_id"] != layout.root.name:
        raise PlanningValidationError("Gate 3 evidence belongs to another project")
    if qa["revision_id"] != revision_id or watch["revision_id"] != revision_id:
        raise PlanningValidationError("Gate 3 evidence belongs to another revision")
    if asset["project_id"] != layout.root.name or asset["revision_id"] != revision_id:
        raise PlanningValidationError(
            "Gate 3 asset manifest belongs to another project or revision"
        )
    candidate_hash = str(qa["candidate"]["sha256"])
    if str(watch["candidate"]["sha256"]) != candidate_hash:
        raise PlanningValidationError("watch-through is bound to a different final candidate")
    plan_hashes, _resolved_plans = _validate_plan_bundle(
        package_root, layout, plan_paths, revision_id
    )
    if not gate2_paths:
        raise PlanningValidationError("Gate 3 requires at least one locked Gate 2 segment")
    lock_hashes = [
        _validate_gate2_lock(package_root, layout, raw_path, revision_id=revision_id)
        for raw_path in gate2_paths
    ]
    bound_hashes = {
        "candidate_sha256": candidate_hash,
        "final_qa_sha256": sha256_file(selected_qa),
        "watchthrough_sha256": sha256_file(selected_watch),
        "plan_bundle_sha256": canonical_sha256(plan_hashes),
        "asset_manifest_sha256": sha256_file(selected_asset),
        "composition_bundle_sha256": sha256_file(selected_composition),
        "delivery_profile_sha256": sha256_file(selected_profile),
        "gate2_approval_set_sha256": canonical_sha256(sorted(lock_hashes)),
    }
    if decision == "approved":
        blockers: list[str] = []
        if not qa["final_ready"]:
            blockers.append("final QA is not final-ready")
        if watch["status"] != "complete" or watch["decision"] != "pass":
            blockers.append("watch-through is not complete and passing")
        if blockers:
            raise PlanningValidationError("Gate 3 approval blocked: " + "; ".join(blockers))
    approval_key = make_stage_key(
        "gate3-approval",
        "p11-04a",
        list(bound_hashes.values()),
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "actor": actor,
            "role": role,
            "decision": decision,
            "notes": notes,
            "plans": plan_hashes,
            "locks": sorted(lock_hashes),
        },
    )
    output = layout.review / "gate3" / f"{approval_key[:16]}-gate3-approval.json"
    payload: dict[str, Any] = {
        "schema_name": "gate3_approval",
        "schema_version": "1.0.0",
        "artifact_id": "art_gate3_approval",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "bound_hashes": bound_hashes,
        "decision": decision,
        "reviewer": {"actor": actor, "role": role, "reviewed_at": now_iso()},
        "notes": notes,
    }
    with ProjectLock(layout, stage="gate3_approval", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "Gate 3 approval")
            validate_artifact(package_root, "gate3_approval", current)
            if current.get("bound_hashes") == bound_hashes and current.get("decision") == decision:
                return output
            raise StateConflictError("Gate 3 approval exists with different bindings")
        write_validated_artifact(package_root, "gate3_approval", output, payload)
    return output


__all__ = ["approve_gate3"]
