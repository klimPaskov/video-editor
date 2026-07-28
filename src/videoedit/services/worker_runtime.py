from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoedit.errors import (
    ApprovalRequiredError,
    PlanningValidationError,
    StaleApprovalError,
    StateConflictError,
)
from videoedit.services.artifacts import (
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

WORKER_RUNTIME_IMPLEMENTATION_VERSION = "p7-p8-runtime-approval-v1"

_WORKER_SPECS: dict[str, dict[str, str]] = {
    "sam3": {
        "upstream_repository": "https://github.com/facebookresearch/sam3",
        "license_id": "meta-sam-2025-11-19",
        "python": "3.12",
    },
    "matanyone2": {
        "upstream_repository": "https://github.com/pq-yang/MatAnyone2",
        "license_id": "ntu-s-lab-1.0",
        "python": "3.10",
    },
}


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} must be inside the project") from exc
    return resolved


def _required_text(value: object, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise PlanningValidationError(f"{label} is required")
    return text


def _identity(
    *,
    worker: str,
    upstream_commit: str,
    checkpoint_id: str,
    checkpoint_sha256: str,
    pytorch: str,
    cuda: str,
    device: str,
) -> dict[str, str]:
    spec = _WORKER_SPECS.get(worker)
    if spec is None:
        raise PlanningValidationError("worker must be sam3 or matanyone2")
    normalized_commit = _required_text(upstream_commit, "upstream_commit").lower()
    if re.fullmatch(r"[a-f0-9]{40}", normalized_commit) is None:
        raise PlanningValidationError("upstream_commit must be a 40-character lowercase SHA-1")
    normalized_checkpoint_hash = _required_text(checkpoint_sha256, "checkpoint_sha256").lower()
    if re.fullmatch(r"[a-f0-9]{64}", normalized_checkpoint_hash) is None:
        raise PlanningValidationError("checkpoint_sha256 must be a lowercase SHA-256 digest")
    python_version = spec["python"]
    return {
        "worker": worker,
        "upstream_repository": spec["upstream_repository"],
        "upstream_commit": normalized_commit,
        "checkpoint_id": _required_text(checkpoint_id, "checkpoint_id"),
        "checkpoint_sha256": normalized_checkpoint_hash,
        "license_id": spec["license_id"],
        "python": python_version,
        "pytorch": _required_text(pytorch, "pytorch"),
        "cuda": _required_text(cuda, "cuda"),
        "device": _required_text(device, "device"),
    }


def runtime_identity_sha256(identity: Mapping[str, str]) -> str:
    return canonical_sha256(dict(identity))


def approve_worker_runtime(
    package_root: Path,
    layout: ProjectLayout,
    *,
    worker: str,
    upstream_commit: str,
    checkpoint_id: str,
    checkpoint_sha256: str,
    pytorch: str,
    cuda: str,
    device: str,
    actor: str,
    role: str,
    reason: str,
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Persist an explicit human acceptance for one immutable worker runtime."""

    identity = _identity(
        worker=worker,
        upstream_commit=upstream_commit,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        pytorch=pytorch,
        cuda=cuda,
        device=device,
    )
    actor_value = _required_text(actor, "actor")
    role_value = _required_text(role, "role")
    reason_value = _required_text(reason, "reason")
    identity_hash = runtime_identity_sha256(identity)
    destination = _owned_path(
        layout,
        output or layout.review / f"worker-runtime-approval-{worker}-{identity_hash[:16]}.json",
        "worker runtime approval output",
    )
    payload: dict[str, Any] = {
        "schema_name": "worker_runtime_approval",
        "schema_version": "1.0.0",
        "artifact_id": f"art_worker_runtime_approval_{worker}",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "worker-runtime-approval",
            "human-review",
            WORKER_RUNTIME_IMPLEMENTATION_VERSION,
        ),
        "approval_id": f"apr_worker_runtime_{worker}_{identity_hash[:16]}",
        **identity,
        "actor": actor_value,
        "role": role_value,
        "decision": "approved",
        "reason": reason_value,
        "identity_sha256": identity_hash,
        "expires_at": None,
        "config_sha256": config_sha256(layout),
    }
    # Keep the identity hash as the only mutable binding. The timestamp is audit
    # metadata and must not make an identical approval non-idempotent.
    with ProjectLock(layout, stage="worker_runtime_approval", revision_id=revision_id):
        if destination.is_file():
            current = _read_object(destination, "worker runtime approval")
            validate_artifact(package_root, "worker_runtime_approval", current)
            current_binding = {key: value for key, value in current.items() if key != "created_at"}
            payload_binding = {key: value for key, value in payload.items() if key != "created_at"}
            if current_binding == payload_binding:
                return destination
            raise StateConflictError(
                "worker runtime approval exists with different identity or reviewer details"
            )
        write_validated_artifact(package_root, "worker_runtime_approval", destination, payload)
    return destination


def validate_worker_runtime_approval(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    *,
    worker: str,
    upstream_commit: str,
    checkpoint_id: str,
    checkpoint_sha256: str,
    pytorch: str,
    cuda: str,
    device: str,
    revision_id: str = "rev_001",
) -> dict[str, str]:
    """Validate and return the job reference for a current human runtime approval."""

    selected = _owned_path(layout, path, "worker runtime approval")
    if not selected.is_file():
        raise ApprovalRequiredError(f"worker runtime approval is missing: {selected}")
    approval = _read_object(selected, "worker runtime approval")
    validate_artifact(package_root, "worker_runtime_approval", approval)
    if approval["project_id"] != layout.root.name or approval["revision_id"] != revision_id:
        raise StaleApprovalError("worker runtime approval belongs to another project or revision")
    if approval["config_sha256"] != config_sha256(layout):
        raise StaleApprovalError("worker runtime approval is stale for project configuration")
    identity = _identity(
        worker=worker,
        upstream_commit=upstream_commit,
        checkpoint_id=checkpoint_id,
        checkpoint_sha256=checkpoint_sha256,
        pytorch=pytorch,
        cuda=cuda,
        device=device,
    )
    identity_hash = runtime_identity_sha256(identity)
    if approval["identity_sha256"] != identity_hash:
        raise StaleApprovalError("worker runtime approval is stale for the declared identity")
    for key, expected in identity.items():
        if approval.get(key) != expected:
            raise StaleApprovalError(f"worker runtime approval field is stale: {key}")
    if approval["decision"] != "approved":
        raise ApprovalRequiredError("worker runtime approval is not approved")
    expires_at = approval.get("expires_at")
    if isinstance(expires_at, str):
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(UTC):
                raise StaleApprovalError("worker runtime approval has expired")
        except ValueError as exc:
            raise StaleApprovalError("worker runtime approval has an invalid expiry") from exc
    return {
        "artifact_id": str(approval["artifact_id"]),
        "path": str(selected),
        "sha256": sha256_file(selected),
    }


__all__ = [
    "WORKER_RUNTIME_IMPLEMENTATION_VERSION",
    "approve_worker_runtime",
    "runtime_identity_sha256",
    "validate_worker_runtime_approval",
]
