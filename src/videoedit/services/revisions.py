from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import (
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

INVALIDATION_BY_KIND: dict[str, tuple[str, ...]] = {
    "FIX": ("edit", "transcript", "caption", "composition", "render", "preview", "qa", "delivery"),
    "KEEP": ("review",),
    "REMOVE": (
        "edit",
        "transcript",
        "caption",
        "composition",
        "render",
        "preview",
        "qa",
        "delivery",
    ),
    "RETIME": ("effect", "composition", "render", "preview", "qa", "delivery"),
    "MASK": ("mask", "matte", "effect", "composition", "render", "preview", "qa", "delivery"),
    "TEXT": ("caption", "composition", "render", "preview", "qa", "delivery"),
    "AUDIO": ("audio", "composition", "render", "preview", "qa", "delivery"),
    "ZOOM": ("focus_pacing", "composition", "render", "preview", "qa", "delivery"),
    "SPEED": (
        "retiming",
        "transcript",
        "caption",
        "composition",
        "render",
        "preview",
        "qa",
        "delivery",
    ),
}
REVISION_ID_PATTERN = re.compile(r"^rev_(\d{3,})$")


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _next_revision_id(layout: ProjectLayout) -> str:
    highest = 0
    for path in layout.revisions.glob("rev_*"):
        match = REVISION_ID_PATTERN.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"rev_{highest + 1:03d}"


def invalidated_stages(markers: list[dict[str, Any]]) -> list[str]:
    stages: set[str] = set()
    for marker in markers:
        kind = str(marker.get("kind", ""))
        if kind not in INVALIDATION_BY_KIND:
            raise PlanningValidationError(f"cannot invalidate for unsupported marker kind: {kind}")
        stages.update(INVALIDATION_BY_KIND[kind])
    return sorted(stages)


def _current_inputs(layout: ProjectLayout) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for root in (layout.raw, layout.work, layout.artifacts):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".json", ".mp4", ".mov", ".wav"}:
                continue
            if path.name in {"project.yaml", "project.lock"}:
                continue
            refs.append(
                {
                    "artifact_id": f"preserved_{re.sub(r'[^a-z0-9_-]+', '_', path.stem.lower())}",
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                }
            )
            if len(refs) >= 32:
                return refs
    return refs


def _revision_payload(
    layout: ProjectLayout,
    revision_id: str,
    parent_revision_id: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_name": "project_revision",
        "schema_version": "1.0.0",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "parent_revision_id": parent_revision_id,
        "created_at": created_at,
        "active": True,
        "directories": {
            "artifacts": str(layout.artifacts),
            "review": str(layout.review),
            "work": str(layout.work),
            "output": str(layout.output),
        },
    }


def apply_review_markers(
    package_root: Path,
    layout: ProjectLayout,
    markers_path: Path,
    *,
    new_revision_id: str | None = None,
) -> Path:
    selected_markers = markers_path.expanduser().resolve()
    try:
        selected_markers.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError("review markers must be inside the project") from exc
    with ProjectLock(layout, stage="revision_request", revision_id="rev_001"):
        markers = _read_object(selected_markers, "review marker artifact")
        validate_artifact(package_root, "review_markers", markers)
        parent_revision_id = str(markers["revision_id"])
        if markers["project_id"] != layout.root.name:
            raise PlanningValidationError("review markers belong to another project")
        marker_items = [dict(item) for item in markers["markers"]]
        if not marker_items:
            raise PlanningValidationError("cannot create a revision from an empty marker artifact")
        marker_sha256 = sha256_file(selected_markers)
        for request_path in sorted(layout.revisions.glob("rev_*/revision-request.json")):
            prior_request = _read_object(request_path, "prior revision request")
            if (
                prior_request.get("parent_revision_id") == parent_revision_id
                and isinstance(prior_request.get("source_markers"), dict)
                and prior_request["source_markers"].get("sha256") == marker_sha256
            ):
                validate_artifact(package_root, "revision_request", prior_request)
                return request_path
        target_revision_id = new_revision_id or _next_revision_id(layout)
        if REVISION_ID_PATTERN.fullmatch(target_revision_id) is None:
            raise PlanningValidationError(f"invalid new revision id: {target_revision_id}")
        if target_revision_id == parent_revision_id:
            raise StateConflictError("new revision must not overwrite its parent")
        target_root = layout.revision_root(target_revision_id)
        if target_root.exists():
            raise StateConflictError(f"revision already exists: {target_revision_id}")
        created_at = now_iso()
        preserved_inputs = _current_inputs(layout)
        request_payload: dict[str, Any] = {
            "schema_name": "revision_request",
            "schema_version": "1.0.0",
            "artifact_id": f"art_revision_request_{target_revision_id}",
            "project_id": layout.root.name,
            "revision_id": target_revision_id,
            "parent_revision_id": parent_revision_id,
            "created_at": created_at,
            "producer": producer("revision-request", "review-markers", __version__),
            "source_markers": {
                "artifact_id": str(markers["artifact_id"]),
                "path": str(selected_markers),
                "sha256": marker_sha256,
            },
            "markers": [
                {
                    "marker_id": item["marker_id"],
                    "kind": item["kind"],
                    "instruction": item["instruction"],
                    "range_us": item["range_us"],
                }
                for item in marker_items
            ],
            "invalidated_stages": invalidated_stages(marker_items),
            "preserved_inputs": preserved_inputs,
            "status": "created",
        }
        validate_artifact(package_root, "revision_request", request_payload)
        staging_root = layout.staging / "revisions" / target_revision_id
        if staging_root.exists():
            raise StateConflictError(f"revision staging directory already exists: {staging_root}")
        staging_root.mkdir(parents=True, exist_ok=False)
        write_validated_artifact(
            package_root,
            "project_revision",
            staging_root / "revision.json",
            _revision_payload(layout, target_revision_id, parent_revision_id, created_at),
        )
        write_validated_artifact(
            package_root,
            "revision_request",
            staging_root / "revision-request.json",
            request_payload,
        )
        target_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_root, target_root)

        parent_file = layout.revision_root(parent_revision_id) / "revision.json"
        if parent_file.is_file():
            parent = _read_object(parent_file, "parent revision")
            validate_artifact(package_root, "project_revision", parent)
            parent["active"] = False
            write_validated_artifact(package_root, "project_revision", parent_file, parent)
        project_manifest_path = layout.state / "project-manifest.json"
        project_manifest = _read_object(project_manifest_path, "project manifest")
        validate_artifact(package_root, "project_manifest", project_manifest)
        project_manifest["active_revision_id"] = target_revision_id
        project_manifest["updated_at"] = created_at
        write_validated_artifact(
            package_root, "project_manifest", project_manifest_path, project_manifest
        )
        return target_root / "revision-request.json"
