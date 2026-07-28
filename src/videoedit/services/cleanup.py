from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videoedit import __version__
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

CLEANUP_PLAN_IMPLEMENTATION_VERSION = "p11-08b"


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


def _cleanup_cache_binding(payload: dict[str, Any]) -> dict[str, Any]:
    binding = dict(payload)
    binding.pop("created_at", None)
    return binding


def _candidate_files(layout: ProjectLayout, revision_id: str) -> list[tuple[Path, str, str]]:
    roots = [layout.staging, layout.work / "proxies", layout.work / "final-assembly"]
    candidates: list[tuple[Path, str, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            paths = root.rglob("*")
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    path.resolve().relative_to(layout.root.resolve())
                except ValueError:
                    continue
                candidates.append(
                    (
                        path,
                        "cache" if "proxy" in path.parts else "temporary",
                        "derived staging or cache output",
                    )
                )
        except OSError:
            continue
    inactive = layout.revisions
    if inactive.is_dir():
        for revision_root in inactive.iterdir():
            if not revision_root.is_dir() or revision_root.name == revision_id:
                continue
            try:
                paths = revision_root.rglob("*")
                for path in paths:
                    if path.is_file() and not path.is_symlink():
                        try:
                            path.resolve().relative_to(layout.root.resolve())
                        except ValueError:
                            continue
                        candidates.append((path, "inactive_revision", "inactive revision artifact"))
            except OSError:
                continue
    return sorted(
        {(path.resolve(), retention, reason) for path, retention, reason in candidates},
        key=lambda item: str(item[0]),
    )


def plan_cleanup(
    package_root: Path,
    layout: ProjectLayout,
    backup_verification_path: Path,
    *,
    revision_id: str = "rev_001",
) -> Path:
    """Create a dry-run cleanup plan that excludes source and required active artifacts."""

    selected_backup = _owned_path(layout, backup_verification_path, "backup verification")
    backup = _read_object(selected_backup, "backup verification")
    validate_artifact(package_root, "backup_verification", backup)
    if backup["project_id"] != layout.root.name or backup["revision_id"] != revision_id:
        raise PlanningValidationError("backup verification belongs to another project or revision")
    entries: list[dict[str, Any]] = []
    for path, retention, reason in _candidate_files(layout, revision_id):
        digest = sha256_file(path)
        entries.append(
            {
                "artifact_id": f"cleanup_{digest[:24]}",
                "path": str(path),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
                "retention_class": retention,
                "eligible": backup["status"] == "pass",
                "reason": reason
                if backup["status"] == "pass"
                else "backup verification is not passing",
            }
        )
    payload: dict[str, Any] = {
        "schema_name": "cleanup_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_cleanup",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "status": "draft" if backup["status"] == "pass" else "blocked",
        "derived_only": True,
        "source_paths_excluded": True,
        "backup_verification_sha256": sha256_file(selected_backup),
        "entries": entries,
        "approval_id": None,
        "executed_at": None,
    }
    key = make_stage_key(
        "cleanup-plan",
        CLEANUP_PLAN_IMPLEMENTATION_VERSION,
        [sha256_file(selected_backup)],
        {
            "revision_id": revision_id,
            "config_sha256": config_sha256(layout),
            "entries": entries,
        },
    )
    output = layout.artifacts / f"cleanup-plan-{key[:16]}.json"
    with ProjectLock(layout, stage="cleanup_plan", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "cleanup plan")
            validate_artifact(package_root, "cleanup_plan", current)
            if _cleanup_cache_binding(current) != _cleanup_cache_binding(payload):
                raise StateConflictError("cleanup plan exists with stale contents")
            return output
        write_validated_artifact(package_root, "cleanup_plan", output, payload)
        write_validated_artifact(
            package_root, "cleanup_plan", layout.artifacts / "cleanup-plan.json", payload
        )
    return output


def approve_cleanup(
    package_root: Path,
    layout: ProjectLayout,
    cleanup_plan_path: Path,
    *,
    actor: str,
    role: str,
    reason: str,
    revision_id: str = "rev_001",
) -> Path:
    """Create the separate human approval required before cleanup execution."""

    selected_plan = _owned_path(layout, cleanup_plan_path, "cleanup plan")
    plan = _read_object(selected_plan, "cleanup plan")
    validate_artifact(package_root, "cleanup_plan", plan)
    if plan["project_id"] != layout.root.name or plan["revision_id"] != revision_id:
        raise PlanningValidationError("cleanup plan belongs to another project or revision")
    if plan["status"] != "draft" or not any(item["eligible"] for item in plan["entries"]):
        raise PlanningValidationError(
            "cleanup approval is blocked until a passing dry-run has eligible entries"
        )
    if not actor.strip() or not role.strip() or not reason.strip():
        raise PlanningValidationError("cleanup approval actor, role, and reason are required")
    payload: dict[str, Any] = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": "art_approval_cleanup",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("cleanup-approval", "human-review", __version__),
        "inputs": [artifact_input("art_cleanup", selected_plan)],
        "config_sha256": config_sha256(layout),
        "approval_id": f"apr_cleanup_{sha256_file(selected_plan)[:16]}",
        "approval_type": "cleanup",
        "actor": actor,
        "role": role,
        "decision": "approved",
        "reason": reason,
        "approved_item_type": "cleanup_plan",
        "approved_item_sha256": sha256_file(selected_plan),
        "expires_at": None,
        "budget": None,
    }
    output = layout.review / f"cleanup-approval-{sha256_file(selected_plan)[:16]}.json"
    with ProjectLock(layout, stage="cleanup_approval", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "cleanup approval")
            validate_artifact(package_root, "approval_record", current)
            current_binding = {key: value for key, value in current.items() if key != "created_at"}
            payload_binding = {key: value for key, value in payload.items() if key != "created_at"}
            if current_binding == payload_binding:
                return output
            raise StateConflictError(
                "cleanup approval exists with different reviewer, reason, or bindings"
            )
        write_validated_artifact(package_root, "approval_record", output, payload)
    return output


def execute_cleanup(
    package_root: Path,
    layout: ProjectLayout,
    cleanup_plan_path: Path,
    approval_path: Path,
    backup_verification_path: Path,
    *,
    revision_id: str = "rev_001",
) -> Path:
    """Execute only exact eligible derived paths after backup and cleanup approval checks."""

    selected_plan = _owned_path(layout, cleanup_plan_path, "cleanup plan")
    selected_approval = _owned_path(layout, approval_path, "cleanup approval")
    selected_backup = _owned_path(layout, backup_verification_path, "backup verification")
    plan = _read_object(selected_plan, "cleanup plan")
    approval = _read_object(selected_approval, "cleanup approval")
    backup = _read_object(selected_backup, "backup verification")
    validate_artifact(package_root, "cleanup_plan", plan)
    validate_artifact(package_root, "approval_record", approval)
    validate_artifact(package_root, "backup_verification", backup)
    for description, value in (
        ("cleanup plan", plan),
        ("cleanup approval", approval),
        ("backup verification", backup),
    ):
        if value["project_id"] != layout.root.name or value["revision_id"] != revision_id:
            raise PlanningValidationError(f"{description} belongs to another project or revision")
    if plan["status"] != "draft" or backup["status"] != "pass":
        raise PlanningValidationError("cleanup execution requires a passing draft plan and backup")
    if approval["approval_type"] != "cleanup" or approval["decision"] != "approved":
        raise PlanningValidationError("cleanup approval is missing")
    if approval["approved_item_sha256"] != sha256_file(selected_plan):
        raise PlanningValidationError("cleanup approval is stale")
    if plan["backup_verification_sha256"] != sha256_file(selected_backup):
        raise PlanningValidationError("cleanup plan backup verification is stale")
    removed: list[str] = []
    for entry in plan["entries"]:
        if not entry["eligible"]:
            continue
        path = _owned_path(layout, Path(str(entry["path"])), "cleanup entry")
        if path == layout.raw or layout.raw in path.parents:
            raise PlanningValidationError("cleanup path resolves under raw source media")
        if path.is_symlink() or not path.is_file():
            continue
        expected_sha256 = entry.get("sha256")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise PlanningValidationError("cleanup entry hash is missing")
        if sha256_file(path) != expected_sha256:
            raise PlanningValidationError(f"cleanup entry hash changed: {path}")
        path.unlink()
        removed.append(str(path))
    executed = dict(plan)
    executed["status"] = "executed"
    executed["approval_id"] = approval["approval_id"]
    executed["executed_at"] = now_iso()
    executed["entries"] = [
        dict(item, eligible=False, reason="removed by approved cleanup")
        if str(item["path"]) in removed
        else item
        for item in plan["entries"]
    ]
    output = layout.artifacts / "cleanup-plan-executed.json"
    write_validated_artifact(package_root, "cleanup_plan", output, executed)
    return output


__all__ = ["approve_cleanup", "execute_cleanup", "plan_cleanup"]
