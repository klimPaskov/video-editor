from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    now_iso,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.stage_state import load_stage_state, stage_state_path, write_stage_state

DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024


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


def _relative_path(layout: ProjectLayout, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(layout.root.resolve()))
    except ValueError:
        return str(path.resolve())


def _source_integrity(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    warnings: list[str],
) -> str:
    path = layout.artifacts / "source-manifest.json"
    if not path.is_file():
        warnings.append("source_manifest_missing")
        return "unknown"
    try:
        source = _read_object(path, "source manifest")
        validate_artifact(package_root, "source_manifest", source)
    except (PlanningValidationError, ValueError) as exc:
        warnings.append(f"source_manifest_invalid: {exc}")
        return "fail"
    if source.get("project_id") != layout.root.name:
        warnings.append("source_manifest_project_mismatch")
        return "fail"
    ingest_mode = source.get("ingest_mode")
    selected_value = (
        source.get("managed_path") if ingest_mode == "copy" else source.get("source_path")
    )
    if not isinstance(selected_value, str) or not selected_value:
        warnings.append("source_path_missing")
        return "fail"
    selected = Path(selected_value).expanduser().resolve()
    if not selected.is_file():
        warnings.append(f"source_file_missing: {selected}")
        return "fail"
    actual = sha256_file(selected)
    if actual != source["sha256"]:
        warnings.append("source_hash_mismatch")
        return "fail"
    return "pass"


def _gate1_approval_snapshot(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    warnings: list[str],
) -> None:
    """Add a fail-closed status warning when the active revision lacks Gate 1."""

    for approval_path in sorted(layout.review.glob("gate1-approval-*.json")):
        try:
            approval = _read_object(approval_path, "Gate 1 approval")
            validate_artifact(package_root, "approval_record", approval)
        except (PlanningValidationError, ValueError) as exc:
            warnings.append(f"invalid_gate1_approval {approval_path.name}: {exc}")
            continue
        if (
            approval.get("project_id") == layout.root.name
            and approval.get("revision_id") == revision_id
            and approval.get("approval_type") == "edit"
            and approval.get("decision") == "approved"
        ):
            return
    warnings.append("gate1_approval_missing_or_stale")


def _stage_snapshot(
    package_root: Path,
    layout: ProjectLayout,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    stages: list[dict[str, Any]] = []
    state_hashes: list[str] = []
    if not layout.stage_state.is_dir():
        return stages, state_hashes
    for path in sorted(layout.stage_state.glob("*.json")):
        try:
            state = _read_object(path, "stage state")
            validate_artifact(package_root, "stage_state", state)
        except (PlanningValidationError, ValueError) as exc:
            warnings.append(f"invalid_stage_state {path.name}: {exc}")
            continue
        error = state.get("error")
        error_message = error.get("message") if isinstance(error, dict) else None
        stages.append(
            {
                "stage": str(state["stage"]),
                "revision_id": str(state["revision_id"]),
                "status": str(state["status"]),
                "attempt": int(state["attempt"]),
                "stage_key": str(state["stage_key"]),
                "error": str(error_message) if error_message else None,
            }
        )
        state_hashes.append(sha256_file(path))
        if state["status"] == "running":
            warnings.append(f"stage_running_requires_operator_recovery: {state['stage']}")
    return stages, state_hashes


def _delivery_snapshot(
    package_root: Path,
    layout: ProjectLayout,
    revision_id: str,
    warnings: list[str],
) -> tuple[list[str], list[str]]:
    manifest_path = layout.artifacts / "delivery-manifest.json"
    paths: list[str] = []
    hashes: list[str] = []
    if manifest_path.is_file():
        try:
            manifest = _read_object(manifest_path, "delivery manifest")
            validate_artifact(package_root, "delivery_manifest", manifest)
            if (
                manifest.get("project_id") != layout.root.name
                or manifest.get("revision_id") != revision_id
            ):
                warnings.append("delivery_manifest_project_or_revision_mismatch")
            for output in manifest["outputs"]:
                file_ref = output["file"]
                output_path = Path(str(file_ref["path"])).expanduser().resolve()
                paths.append(_relative_path(layout, output_path))
                if not output_path.is_file():
                    warnings.append(f"delivery_output_missing: {output_path}")
                elif sha256_file(output_path) != file_ref["sha256"]:
                    warnings.append(f"delivery_output_hash_mismatch: {output_path}")
            hashes.append(sha256_file(manifest_path))
        except (PlanningValidationError, ValueError) as exc:
            warnings.append(f"delivery_manifest_invalid: {exc}")
    delivery_root = layout.output / "delivery" / revision_id
    if delivery_root.is_dir():
        for path in sorted(delivery_root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = _relative_path(layout, path)
                if relative not in paths:
                    paths.append(relative)
    return sorted(set(paths)), hashes


def read_project_status(
    package_root: Path,
    layout: ProjectLayout,
    *,
    revision_id: str | None = None,
    minimum_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> dict[str, Any]:
    """Return a non-mutating, schema-shaped snapshot of resumable project state."""

    manifest_path = layout.state / "project-manifest.json"
    if not manifest_path.is_file():
        raise PlanningValidationError(
            "project-manifest.json is missing; initialize the project first"
        )
    manifest = _read_object(manifest_path, "project manifest")
    validate_artifact(package_root, "project_manifest", manifest)
    project_id = str(manifest["project_id"])
    if project_id != layout.root.name:
        raise PlanningValidationError("project manifest does not belong to the requested project")
    active_revision_id = str(revision_id or manifest["active_revision_id"])
    warnings: list[str] = []
    source_state = _source_integrity(package_root, layout, active_revision_id, warnings)
    _gate1_approval_snapshot(package_root, layout, active_revision_id, warnings)
    stages, state_hashes = _stage_snapshot(package_root, layout, warnings)

    qa_ready = False
    final_qa_paths = [layout.artifacts / "final-qa.json"]
    final_qa_paths.extend(layout.artifacts.glob("final-qa-*.json"))
    final_qa_paths.extend(layout.review.glob("final-qa-*.json"))
    selected_final_qa: dict[str, Any] | None = None
    valid_final_qa_count = 0
    for final_qa_path in sorted(
        {path for path in final_qa_paths if path.is_file() and "superseded" not in path.name},
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    ):
        try:
            final_qa = _read_object(final_qa_path, "final QA report")
            validate_artifact(package_root, "final_qa_report", final_qa)
        except (PlanningValidationError, ValueError) as exc:
            warnings.append(f"final_qa_invalid: {exc}")
            continue
        valid_final_qa_count += 1
        if (
            final_qa.get("project_id") == project_id
            and final_qa.get("revision_id") == active_revision_id
        ):
            selected_final_qa = final_qa
            break
    if selected_final_qa is not None:
        qa_ready = bool(selected_final_qa["final_ready"])
        if not qa_ready:
            warnings.append("final_qa_not_ready")
    elif valid_final_qa_count:
        warnings.append("final_qa_project_or_revision_mismatch")
    else:
        warnings.append("final_qa_missing")

    delivery_paths, delivery_hashes = _delivery_snapshot(
        package_root, layout, active_revision_id, warnings
    )
    if not delivery_paths:
        warnings.append("delivery_not_published")
    if layout.lock_path.is_file():
        warnings.append("project_lock_present")
    try:
        free_bytes = shutil.disk_usage(layout.root).free
        if free_bytes < minimum_free_bytes:
            warnings.append(f"free_disk_below_threshold: {free_bytes} < {minimum_free_bytes} bytes")
    except OSError as exc:
        warnings.append(f"disk_quota_unavailable: {exc}")

    return {
        "schema_name": "operation_status",
        "schema_version": "1.0.0",
        "project_id": project_id,
        "active_revision_id": active_revision_id,
        "source_integrity": source_state,
        "stages": stages,
        "qa_ready": qa_ready,
        "delivery_paths": delivery_paths,
        "warnings": list(dict.fromkeys(warnings)),
        "_state_hashes": state_hashes,
        "_delivery_hashes": delivery_hashes,
    }


def write_project_status(
    package_root: Path,
    layout: ProjectLayout,
    *,
    revision_id: str | None = None,
    minimum_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> Path:
    """Persist the current status snapshot atomically; internal hashes are not persisted."""

    payload = read_project_status(
        package_root,
        layout,
        revision_id=revision_id,
        minimum_free_bytes=minimum_free_bytes,
    )
    payload.pop("_state_hashes", None)
    payload.pop("_delivery_hashes", None)
    output = layout.artifacts / "operation-status.json"
    write_validated_artifact(package_root, "operation_status", output, payload)
    return output


def request_stage_retry(
    package_root: Path,
    layout: ProjectLayout,
    stage: str,
    *,
    revision_id: str = "rev_001",
    reason: str,
) -> Path:
    """Persist a retry request without starting work or resubmitting a provider job."""

    if not reason.strip():
        raise PlanningValidationError("retry reason is required")
    with ProjectLock(layout, stage="retry_request", revision_id=revision_id):
        state = load_stage_state(package_root, layout, stage, revision_id)
        if state is None:
            raise PlanningValidationError(f"stage state is missing: {stage}/{revision_id}")
        status = str(state["status"])
        if status not in {"failed", "cancelled"}:
            raise PlanningValidationError(
                f"stage {stage} is not retryable from status {status}; "
                "only failed or cancelled stages may retry"
            )
        stage_key = str(state["stage_key"])
        request_key = make_stage_key(
            "retry-request",
            __version__,
            [sha256_file(stage_state_path(layout, stage, revision_id))],
            {"stage": stage, "revision_id": revision_id, "reason": reason.strip()},
        )
        output = layout.state / "retries" / f"{stage}-{revision_id}-{request_key[:16]}.json"
        payload: dict[str, Any] = {
            "schema_name": "retry_request",
            "schema_version": "1.0.0",
            "artifact_id": f"art_retry_{request_key[:16]}",
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "created_at": now_iso(),
            "stage": stage,
            "previous_status": status,
            "stage_key": stage_key,
            "retryable": True,
            "reason": reason.strip(),
        }
        if output.is_file():
            current = _read_object(output, "retry request")
            validate_artifact(package_root, "retry_request", current)
            return output
        write_validated_artifact(package_root, "retry_request", output, payload)
        return output


def cancel_stage(
    package_root: Path,
    layout: ProjectLayout,
    stage: str,
    *,
    revision_id: str = "rev_001",
    reason: str,
    remove_partial: bool = True,
) -> Path:
    """Mark an inactive running stage cancelled and remove only declared staging files.

    A live stage normally owns the project lock, so this operation refuses to race it. The
    process adapter remains responsible for signalling the child process before this record is
    written.
    """

    if not reason.strip():
        raise PlanningValidationError("cancellation reason is required")
    state_path = stage_state_path(layout, stage, revision_id)
    with ProjectLock(layout, stage="cancel_stage", revision_id=revision_id):
        state = load_stage_state(package_root, layout, stage, revision_id)
        if state is None:
            raise PlanningValidationError(f"stage state is missing: {stage}/{revision_id}")
        if state["status"] != "running":
            raise PlanningValidationError(
                f"stage {stage} cannot be cancelled from status {state['status']}"
            )
        removed: list[str] = []
        if remove_partial:
            for raw_path in state["staging_paths"]:
                path = _owned_path(layout, Path(str(raw_path)), "declared staging path")
                if not (path == layout.staging or layout.staging in path.parents):
                    raise PlanningValidationError(
                        "cancellation may remove only files under state/staging"
                    )
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed.append(str(path))
        timestamp = now_iso()
        state["status"] = "cancelled"
        state["updated_at"] = timestamp
        state["completed_at"] = timestamp
        state["error"] = {"code": "OPERATOR_CANCELLED", "message": reason.strip()}
        state["warnings"] = [
            *list(state.get("warnings", [])),
            "partial_outputs_removed" if remove_partial else "partial_outputs_retained",
        ]
        write_stage_state(package_root, layout, state)
    return state_path


def recover_crashed_stage(
    package_root: Path,
    layout: ProjectLayout,
    stage: str,
    *,
    revision_id: str = "rev_001",
    reason: str,
) -> Path:
    """Convert an orphaned running state to a retryable failure after operator confirmation."""

    if not reason.strip():
        raise PlanningValidationError("crash-recovery reason is required")
    with ProjectLock(layout, stage="recover_stage", revision_id=revision_id):
        state = load_stage_state(package_root, layout, stage, revision_id)
        if state is None:
            raise PlanningValidationError(f"stage state is missing: {stage}/{revision_id}")
        if state["status"] != "running":
            raise PlanningValidationError(
                f"stage {stage} is not an orphaned running stage: {state['status']}"
            )
        timestamp = now_iso()
        state["status"] = "failed"
        state["updated_at"] = timestamp
        state["completed_at"] = timestamp
        state["error"] = {"code": "CRASH_RECOVERY_REQUIRED", "message": reason.strip()}
        state["warnings"] = [
            *list(state.get("warnings", [])),
            "orphaned_running_state_recovered_by_operator",
        ]
        write_stage_state(package_root, layout, state)
    return stage_state_path(layout, stage, revision_id)


__all__ = [
    "cancel_stage",
    "read_project_status",
    "recover_crashed_stage",
    "request_stage_retry",
    "write_project_status",
]
