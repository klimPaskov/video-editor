from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.services.artifacts import now_iso, validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout, sha256_file

STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def stage_state_path(layout: ProjectLayout, stage: str, revision_id: str) -> Path:
    if not STAGE_PATTERN.fullmatch(stage):
        raise ValueError(f"invalid stage name: {stage}")
    return layout.stage_state / f"{stage}-{revision_id}.json"


def load_stage_state(
    package_root: Path,
    layout: ProjectLayout,
    stage: str,
    revision_id: str,
) -> dict[str, Any] | None:
    path = stage_state_path(layout, stage, revision_id)
    if not path.is_file():
        return None
    try:
        payload_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"stage state is unreadable: {path}") from exc
    if not isinstance(payload_value, dict):
        raise ValueError(f"stage state must be a JSON object: {path}")
    validate_artifact(package_root, "stage_state", payload_value)
    return payload_value


def write_stage_state(
    package_root: Path,
    layout: ProjectLayout,
    payload: dict[str, Any],
) -> Path:
    return write_validated_artifact(
        package_root,
        "stage_state",
        stage_state_path(layout, str(payload["stage"]), str(payload["revision_id"])),
        payload,
    )


def begin_stage(
    package_root: Path,
    layout: ProjectLayout,
    *,
    project_id: str,
    revision_id: str,
    stage: str,
    stage_key: str,
    staging_paths: list[Path],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
    timestamp = now_iso()
    payload: dict[str, Any] = {
        "schema_name": "stage_state",
        "schema_version": "1.0.0",
        "stage_run_id": f"run_{stage}_{stage_key[:16]}_{attempt}",
        "project_id": project_id,
        "revision_id": revision_id,
        "stage": stage,
        "stage_key": stage_key,
        "status": "running",
        "attempt": attempt,
        "started_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
        "artifacts": {},
        "staging_paths": [str(path) for path in staging_paths],
        "warnings": [],
        "error": None,
    }
    write_stage_state(package_root, layout, payload)
    return payload


def complete_stage(
    package_root: Path,
    layout: ProjectLayout,
    state: dict[str, Any],
    *,
    artifacts: dict[str, Path],
    warnings: list[str],
) -> dict[str, Any]:
    completed = now_iso()
    state["status"] = "complete"
    state["updated_at"] = completed
    state["completed_at"] = completed
    state["warnings"] = list(warnings)
    state["error"] = None
    state["artifacts"] = {
        name: {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for name, path in artifacts.items()
        if path.is_file()
    }
    write_stage_state(package_root, layout, state)
    return state


def fail_stage(
    package_root: Path,
    layout: ProjectLayout,
    state: dict[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    timestamp = now_iso()
    state["status"] = "failed"
    state["updated_at"] = timestamp
    state["completed_at"] = timestamp
    state["error"] = {"code": code, "message": message}
    write_stage_state(package_root, layout, state)
    return state


def implementation_version() -> str:
    return __version__
