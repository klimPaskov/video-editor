from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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

BACKUP_VERIFICATION_IMPLEMENTATION_VERSION = "p11-07c"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _cache_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic portion of a backup report for cache reuse."""

    binding = dict(payload)
    binding.pop("created_at", None)
    items = binding.get("items")
    if isinstance(items, list):
        binding["items"] = [
            {key: value for key, value in item.items() if key != "verified_at"}
            if isinstance(item, Mapping)
            else item
            for item in items
        ]
    return binding


def verify_backup_targets(
    package_root: Path,
    layout: ProjectLayout,
    targets: Sequence[Mapping[str, object]],
    *,
    revision_id: str = "rev_001",
) -> Path:
    """Compare named source/backup pairs by size and SHA-256 without copying or deleting."""

    if not targets:
        raise PlanningValidationError("backup verification requires at least one target")
    items: list[dict[str, Any]] = []
    source_hashes: list[str] = []
    for raw in targets:
        role = str(raw.get("role", ""))
        if role not in {"source", "master", "sidecar", "manifest"}:
            raise PlanningValidationError(f"unsupported backup role: {role}")
        source_path = Path(str(raw.get("source_path", ""))).expanduser().resolve()
        backup_path = Path(str(raw.get("backup_path", ""))).expanduser().resolve()
        if not source_path.is_file():
            items.append(
                {
                    "role": role,
                    "source_path": str(source_path),
                    "source_sha256": "0" * 64,
                    "source_size_bytes": 0,
                    "backup_path": str(backup_path),
                    "backup_sha256": "0" * 64,
                    "backup_size_bytes": 0,
                    "verified_at": now_iso(),
                    "status": "fail",
                    "message": "source file is missing",
                }
            )
            continue
        source_sha = sha256_file(source_path)
        source_size = source_path.stat().st_size
        backup_exists = backup_path.is_file()
        backup_sha = sha256_file(backup_path) if backup_exists else "0" * 64
        backup_size = backup_path.stat().st_size if backup_exists else 0
        passed = backup_exists and source_sha == backup_sha and source_size == backup_size
        source_hashes.append(source_sha)
        items.append(
            {
                "role": role,
                "source_path": str(source_path),
                "source_sha256": source_sha,
                "source_size_bytes": source_size,
                "backup_path": str(backup_path),
                "backup_sha256": backup_sha,
                "backup_size_bytes": backup_size,
                "verified_at": now_iso(),
                "status": "pass" if passed else "fail",
                "message": None if passed else "backup hash or size does not match",
            }
        )
    status = "pass" if all(item["status"] == "pass" for item in items) else "fail"
    payload: dict[str, Any] = {
        "schema_name": "backup_verification",
        "schema_version": "1.0.0",
        "artifact_id": "art_backup_verification",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "status": status,
        "items": items,
    }
    key = make_stage_key(
        "backup-verification",
        BACKUP_VERIFICATION_IMPLEMENTATION_VERSION,
        source_hashes + [str(item["backup_sha256"]) for item in items],
        {
            "revision_id": revision_id,
            "config_sha256": config_sha256(layout),
            "targets": [
                {
                    "role": item["role"],
                    "source_path": item["source_path"],
                    "backup_path": item["backup_path"],
                }
                for item in items
            ],
        },
    )
    output = layout.artifacts / f"backup-verification-{key[:16]}.json"
    alias = layout.artifacts / "backup-verification.json"
    with ProjectLock(layout, stage="backup_verification", revision_id=revision_id):
        if output.is_file():
            current = _read_object(output, "backup verification")
            validate_artifact(package_root, "backup_verification", current)
            if _cache_binding(current) != _cache_binding(payload):
                raise StateConflictError("backup verification exists with stale contents")
            return output
        write_validated_artifact(package_root, "backup_verification", output, payload)
        write_validated_artifact(package_root, "backup_verification", alias, payload)
    return output


__all__ = ["verify_backup_targets"]
