from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import now_iso, validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p11-03b"


def _read_object(path: Path, description: str) -> dict[str, object]:
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


def _file_ref(path: Path, artifact_id: str) -> dict[str, object]:
    if not path.is_file():
        raise PlanningValidationError(f"watch-through evidence does not exist: {path}")
    return {
        "artifact_id": artifact_id,
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def record_watchthrough(
    package_root: Path,
    layout: ProjectLayout,
    candidate_path: Path,
    *,
    actor: str,
    role: str,
    protocol: str = "full_watch_through",
    decision: str = "pass",
    notes: str = "",
    evidence_paths: Sequence[Path] = (),
    revision_id: str = "rev_001",
) -> Path:
    """Persist a human watch-through or explicitly approved equivalent record."""

    if not actor.strip() or not role.strip():
        raise PlanningValidationError("watch-through actor and role are required")
    if protocol not in {"full_watch_through", "approved_equivalent"}:
        raise PlanningValidationError("watch-through protocol is invalid")
    if decision not in {"pass", "fail"}:
        raise PlanningValidationError("watch-through decision is invalid")
    if protocol == "approved_equivalent" and not notes.strip():
        raise PlanningValidationError("an approved equivalent review requires an explanation")
    selected_candidate = _owned_path(layout, candidate_path, "final candidate")
    candidate = _file_ref(selected_candidate, "art_final_candidate")
    evidence = [
        _file_ref(
            _owned_path(layout, path, "watch-through evidence"), f"art_watchthrough_{index:03d}"
        )
        for index, path in enumerate(evidence_paths, start=1)
    ]
    stage_key = make_stage_key(
        "watchthrough",
        IMPLEMENTATION_VERSION,
        [str(candidate["sha256"]), *(str(item["sha256"]) for item in evidence)],
        {
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "actor": actor,
            "role": role,
            "protocol": protocol,
            "decision": decision,
            "notes": notes,
        },
    )
    output = layout.review / "gate3" / f"watchthrough-{stage_key[:16]}.json"
    payload = {
        "schema_name": "watchthrough_record",
        "schema_version": "1.0.0",
        "artifact_id": "art_watchthrough",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "candidate": candidate,
        "protocol": protocol,
        "actor": actor,
        "role": role,
        "status": "complete" if decision == "pass" else "failed",
        "decision": decision,
        "notes": notes,
        "evidence": evidence,
    }
    with ProjectLock(layout, stage="watchthrough", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "watch-through record")
            validate_artifact(package_root, "watchthrough_record", current)
            current_binding = {key: value for key, value in current.items() if key != "created_at"}
            payload_binding = {key: value for key, value in payload.items() if key != "created_at"}
            if current_binding == payload_binding:
                return output
            raise StateConflictError("watch-through record already exists with different evidence")
        write_validated_artifact(package_root, "watchthrough_record", output, payload)
    return output


__all__ = ["record_watchthrough"]
