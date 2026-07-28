from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file
from videoedit.services.segment_lock import _owned_path

IMPLEMENTATION_VERSION = "p11-render-manifest-bridge-3"
DURATION_TOLERANCE_US = 100_000


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _validate_project_artifact(
    package_root: Path,
    layout: ProjectLayout,
    path: Path,
    schema_name: str,
    description: str,
) -> tuple[Path, dict[str, Any]]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    value = _read_object(selected, description)
    validate_artifact(package_root, schema_name, value)
    if value.get("project_id") != layout.root.name:
        raise PlanningValidationError(f"{description} belongs to another project")
    return selected, value


def _output_path(layout: ProjectLayout, value: Mapping[str, Any], description: str) -> Path:
    raw_path = value.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise PlanningValidationError(f"{description} has no output path")
    selected = _owned_path(layout, Path(raw_path), description)
    if not selected.is_file():
        raise PlanningValidationError(f"{description} does not exist: {selected}")
    expected_hash = value.get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(selected) != expected_hash:
        raise PlanningValidationError(f"{description} hash is stale")
    return selected


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def write_revision_render_manifest(
    package_root: Path,
    layout: ProjectLayout,
    base_render_manifest_path: Path,
    revision_media_path: Path,
    *,
    revision_id: str,
) -> Path:
    """Bind an already-rendered candidate to its current revision without re-encoding it."""

    base_path, base = _validate_project_artifact(
        package_root,
        layout,
        base_render_manifest_path,
        "render_manifest",
        "base render manifest",
    )
    media_path, media = _validate_project_artifact(
        package_root,
        layout,
        revision_media_path,
        "revision_media_manifest",
        "revision media manifest",
    )
    if media.get("revision_id") != revision_id:
        raise PlanningValidationError("revision media manifest belongs to another revision")
    parent_revision_id = media.get("parent_revision_id")
    if parent_revision_id != base.get("revision_id"):
        raise PlanningValidationError(
            "revision media parent revision does not match the base render manifest"
        )

    base_output = _output_path(layout, base.get("output", {}), "base render output")
    media_output = _output_path(layout, media.get("output", {}), "revision media output")
    candidate_hash = sha256_file(media_output)
    media_output_hash = media.get("output", {}).get("sha256")
    if candidate_hash != media_output_hash:
        raise PlanningValidationError("revision media output hash changed while binding manifest")

    expected_duration_us = int(media["output_duration_us"])
    base_expected_duration_us = int(base["expected_duration_us"])
    media_source_duration_us = int(media["source_duration_us"])
    if (
        abs(base_expected_duration_us - media_source_duration_us) > DURATION_TOLERANCE_US
        and abs(base_expected_duration_us - expected_duration_us) > DURATION_TOLERANCE_US
    ):
        raise PlanningValidationError(
            "base render duration matches neither the revision source nor output duration"
        )

    payload = dict(base)
    payload.update(
        {
            "artifact_id": f"art_render_manifest_{revision_id}",
            "revision_id": revision_id,
            "created_at": now_iso(),
            "producer": producer("render-manifest-revision-bridge", "retimed-render", __version__),
            "inputs": [
                artifact_input("art_parent_render_manifest", base_path),
                artifact_input("art_revision_media", media_path),
            ],
            "expected_duration_us": expected_duration_us,
            "actual_duration_us": expected_duration_us,
            "output": {
                "path": str(media_output.resolve()),
                "sha256": candidate_hash,
                "size_bytes": media_output.stat().st_size,
            },
            "source_to_output_mapping": media["source_to_output_mapping"],
            "warnings": _unique_strings(
                [
                    *(str(value) for value in base.get("warnings", [])),
                    "revision_bound_without_media_rerender",
                    *(["revision_output_path_changed"] if base_output != media_output else []),
                    *(
                        ["revision_duration_changed"]
                        if abs(base_expected_duration_us - expected_duration_us)
                        > DURATION_TOLERANCE_US
                        else []
                    ),
                ]
            ),
        }
    )
    stage_key = make_stage_key(
        "render-manifest-revision-bridge",
        IMPLEMENTATION_VERSION,
        [sha256_file(base_path), sha256_file(media_path), candidate_hash],
        {"project_id": layout.root.name, "revision_id": revision_id},
    )
    output_path = layout.artifacts / f"render-final-production-{revision_id}-{stage_key[:16]}.json"
    alias_path = layout.artifacts / f"render-final-production-{revision_id}.json"

    with ProjectLock(layout, stage="render_manifest_revision_bridge", revision_id=revision_id):
        if output_path.is_file():
            current = _read_object(output_path, "revision-bound render manifest")
            validate_artifact(package_root, "render_manifest", current)
            if any(key != "created_at" and current.get(key) != payload.get(key) for key in payload):
                raise StateConflictError(
                    "revision-bound render manifest exists with stale contents"
                )
            payload = current
        else:
            write_validated_artifact(package_root, "render_manifest", output_path, payload)
        write_validated_artifact(package_root, "render_manifest", alias_path, payload)
    return output_path


def write_revision_retimed_render_manifest(
    package_root: Path,
    layout: ProjectLayout,
    base_retimed_manifest_path: Path,
    revision_media_path: Path,
    *,
    revision_id: str,
) -> Path:
    """Bind a recut candidate to the retimed-render contract without re-encoding it."""

    base_path, base = _validate_project_artifact(
        package_root,
        layout,
        base_retimed_manifest_path,
        "retimed_render_manifest",
        "base retimed render manifest",
    )
    media_path, media = _validate_project_artifact(
        package_root,
        layout,
        revision_media_path,
        "revision_media_manifest",
        "revision media manifest",
    )
    if media.get("revision_id") != revision_id:
        raise PlanningValidationError("revision media manifest belongs to another revision")
    _output_path(layout, base.get("output", {}), "base retimed render output")
    media_output = _output_path(layout, media.get("output", {}), "revision media output")
    expected_duration_us = int(media["output_duration_us"])
    base_expected_duration_us = int(base["expected_duration_us"])
    media_source_duration_us = int(media["source_duration_us"])
    if abs(base_expected_duration_us - media_source_duration_us) > DURATION_TOLERANCE_US:
        raise PlanningValidationError(
            "base retimed render duration does not match the revision source duration"
        )

    payload = dict(base)
    base_video = dict(base.get("video", {}))
    base_audio = dict(base.get("audio", {}))
    base_video["duration_us"] = expected_duration_us
    base_audio["duration_us"] = expected_duration_us
    payload.update(
        {
            "artifact_id": f"art_retimed_render_{revision_id}",
            "revision_id": revision_id,
            "created_at": now_iso(),
            "producer": producer(
                "retimed-render-manifest-revision-bridge", "retimed-render", __version__
            ),
            "inputs": [
                artifact_input("art_parent_retimed_render_manifest", base_path),
                artifact_input("art_revision_media", media_path),
            ],
            "expected_duration_us": expected_duration_us,
            "output": {
                "path": str(media_output.resolve()),
                "sha256": sha256_file(media_output),
                "size_bytes": media_output.stat().st_size,
            },
            "video": base_video,
            "audio": base_audio,
        }
    )
    validate_artifact(package_root, "retimed_render_manifest", payload)
    stage_key = make_stage_key(
        "retimed-render-manifest-revision-bridge",
        IMPLEMENTATION_VERSION,
        [sha256_file(base_path), sha256_file(media_path)],
        {"project_id": layout.root.name, "revision_id": revision_id},
    )
    output_path = layout.artifacts / f"retimed-render-manifest-{revision_id}-{stage_key[:16]}.json"
    alias_path = layout.artifacts / f"retimed-render-manifest-{revision_id}.json"
    with ProjectLock(layout, stage="retimed_manifest_revision_bridge", revision_id=revision_id):
        if output_path.is_file():
            current = _read_object(output_path, "revision-bound retimed render manifest")
            validate_artifact(package_root, "retimed_render_manifest", current)
            if any(key != "created_at" and current.get(key) != payload.get(key) for key in payload):
                raise StateConflictError(
                    "revision-bound retimed render manifest exists with stale contents"
                )
            payload = current
        else:
            write_validated_artifact(package_root, "retimed_render_manifest", output_path, payload)
        write_validated_artifact(package_root, "retimed_render_manifest", alias_path, payload)
    return output_path


__all__ = ["write_revision_render_manifest", "write_revision_retimed_render_manifest"]
