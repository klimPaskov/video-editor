from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from videoedit.errors import ApprovalRequiredError, PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import sha256_file

REPLACEMENT_MANIFEST_VERSION = "1.0.0"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object")
    return value


def _file_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise PlanningValidationError(f"replacement input is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _require_current_ref(reference: object, path: Path, label: str) -> None:
    if not isinstance(reference, Mapping):
        raise StaleApprovalError(f"{label} reference is missing")
    current = _file_ref(path)
    if any(reference.get(name) != current[name] for name in ("path", "sha256", "size_bytes")):
        raise StaleApprovalError(f"{label} is stale for the current file")


def _catalog_asset_path(
    catalog_path: Path, catalog: Mapping[str, Any], asset: Mapping[str, Any]
) -> Path:
    root_value = str(catalog.get("root_path", ""))
    root = Path(root_value).expanduser()
    if not root.is_absolute():
        root = catalog_path.parent / root
    root = root.resolve()
    file_value = asset.get("file")
    if not isinstance(file_value, Mapping):
        raise PlanningValidationError("catalog asset is missing its file reference")
    relative_value = str(file_value.get("path", ""))
    relative = Path(relative_value)
    if not relative_value or relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise PlanningValidationError("catalog asset path must stay inside the catalog root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PlanningValidationError("catalog asset path escapes the catalog root") from exc
    return path


def _keyframe_inputs(
    package_root: Path,
    keyframes_path: Path,
) -> dict[str, Any]:
    keyframes_path = keyframes_path.resolve()
    keyframes = _read_object(keyframes_path, "object-track keyframes")
    validate_artifact(package_root, "object_track_keyframes", keyframes)
    if keyframes.get("status") != "complete":
        raise PlanningValidationError("object replacement requires complete object-track keyframes")
    references = (
        ("source_result", "object-track result"),
        ("segmentation_validation", "object-track validation"),
        ("track_review", "track review"),
    )
    for field, label in references:
        reference = keyframes.get(field)
        if not isinstance(reference, Mapping):
            raise StaleApprovalError(f"{label} reference is missing from keyframes")
        _require_current_ref(reference, Path(str(reference["path"])), label)
    review_path = Path(str(keyframes["track_review"]["path"])).resolve()
    review = _read_object(review_path, "object-track review")
    validate_artifact(package_root, "object_track_review", review)
    if review.get("decision") != "approved":
        raise ApprovalRequiredError("object replacement requires an approved object-track review")
    return keyframes


def write_object_replacement_manifest(
    package_root: Path,
    keyframes_path: Path,
    asset_catalog_path: Path,
    asset_manifest_path: Path,
    asset_id: str,
    output_path: Path | None = None,
    *,
    layer_id: str = "tracked-replacement",
    z_index: int = 30,
    fit: str = "contain",
) -> Path:
    """Bind an approved track to a licensed local replacement asset.

    The function only records provenance and never submits a network request. The
    original shot remains the fallback when the keyframe opacity is zero.
    """

    keyframes_path = keyframes_path.resolve()
    catalog_path = asset_catalog_path.resolve()
    manifest_path = asset_manifest_path.resolve()
    keyframes = _keyframe_inputs(package_root, keyframes_path)
    catalog = _read_object(catalog_path, "asset catalog")
    manifest = _read_object(manifest_path, "asset manifest")
    validate_artifact(package_root, "asset_catalog", catalog)
    validate_artifact(package_root, "asset_manifest", manifest)
    if not asset_id:
        raise PlanningValidationError("replacement asset_id must not be empty")
    if manifest.get("catalog_sha256") != sha256_file(catalog_path):
        raise StaleApprovalError("asset manifest is stale for the current asset catalog")
    if manifest.get("project_id") != keyframes.get("project_id"):
        raise StaleApprovalError("asset manifest project does not match object-track keyframes")
    if manifest.get("revision_id") != keyframes.get("revision_id"):
        raise StaleApprovalError("asset manifest revision does not match object-track keyframes")
    catalog_assets = catalog.get("assets")
    if not isinstance(catalog_assets, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    catalog_asset = next(
        (
            item
            for item in catalog_assets
            if isinstance(item, Mapping) and item.get("asset_id") == asset_id
        ),
        None,
    )
    if not isinstance(catalog_asset, Mapping):
        raise PlanningValidationError(f"replacement asset is not in the catalog: {asset_id}")
    if catalog_asset.get("asset_type") != "replacement_object":
        raise PlanningValidationError("selected asset is not a replacement_object")
    selected_path = _catalog_asset_path(catalog_path, catalog, catalog_asset)
    catalog_file = catalog_asset.get("file")
    if not isinstance(catalog_file, Mapping):
        raise PlanningValidationError("catalog replacement asset has no file reference")
    if not selected_path.is_file():
        raise PlanningValidationError(f"replacement asset file is missing: {selected_path}")
    selected_ref = _file_ref(selected_path)
    if any(catalog_file.get(name) != selected_ref[name] for name in ("sha256", "size_bytes")):
        raise StaleApprovalError("catalog replacement asset hash does not match its file")
    selections = manifest.get("assets")
    if not isinstance(selections, list):
        raise PlanningValidationError("asset manifest assets must be an array")
    selection = next(
        (
            item
            for item in selections
            if isinstance(item, Mapping) and item.get("asset_id") == asset_id
        ),
        None,
    )
    if not isinstance(selection, Mapping):
        raise ApprovalRequiredError(
            "replacement asset is not selected in the project asset manifest"
        )
    approval_ids = selection.get("approval_ids")
    if not isinstance(approval_ids, list) or not approval_ids:
        raise ApprovalRequiredError("replacement asset selection has no approval IDs")
    if selection.get("role") != "tracked_object_replacement":
        raise PlanningValidationError("asset selection role is not tracked_object_replacement")
    if selection.get("asset_sha256") != selected_ref["sha256"]:
        raise StaleApprovalError("asset selection hash does not match the catalog file")
    licence_reference = str(catalog_asset.get("licence_reference", ""))
    if not licence_reference or selection.get("licence_reference") != licence_reference:
        raise PlanningValidationError("replacement asset licence reference is missing or stale")
    permitted_uses = catalog_asset.get("permitted_uses")
    if not isinstance(permitted_uses, list) or not permitted_uses:
        raise PlanningValidationError("replacement asset has no permitted uses")
    source_range = keyframes["source_range"]
    if not isinstance(source_range, Mapping):
        raise PlanningValidationError("object-track keyframes have no source range")
    start_frame = int(source_range["start_frame"])
    end_frame = int(source_range["end_frame"])
    if not layer_id.strip() or fit not in {"contain", "cover", "fill"}:
        raise ValueError("replacement layer id or fit is invalid")
    payload: dict[str, Any] = {
        "schema_name": "object_replacement_manifest",
        "schema_version": REPLACEMENT_MANIFEST_VERSION,
        "artifact_id": f"art_object_replacement_{asset_id}",
        "project_id": str(keyframes["project_id"]),
        "revision_id": str(keyframes["revision_id"]),
        "created_at": now_iso(),
        "producer": producer("object-replacement", "core-assets"),
        "keyframes": _file_ref(keyframes_path),
        "asset_catalog": _file_ref(catalog_path),
        "asset_manifest": _file_ref(manifest_path),
        "asset": {
            "asset_id": asset_id,
            "asset_type": "replacement_object",
            "file": selected_ref,
            "licence_status": catalog_asset["licence_status"],
            "licence_reference": licence_reference,
            "permitted_uses": [str(value) for value in permitted_uses],
            "approval_ids": [str(value) for value in approval_ids],
        },
        "layer": {
            "layer_id": layer_id,
            "z_index": z_index,
            "fit": fit,
            "start_frame": start_frame,
            "duration_frames": end_frame - start_frame,
        },
        "fallback": {"mode": "original_shot", "on_uncertain": "keep_original"},
        "status": "complete",
        "warnings": [
            "replacement is hidden whenever the approved keyframe opacity is zero",
            "original_shot_fallback_remains_under_replacement_layer",
        ],
    }
    validate_artifact(package_root, "object_replacement_manifest", payload)
    output = (
        output_path.resolve()
        if output_path is not None
        else keyframes_path.parent
        / f"object-replacement-{sha256_file(keyframes_path)[:16]}-{asset_id}.json"
    )
    if output.is_file():
        existing = _read_object(output, "existing object replacement manifest")
        validate_artifact(package_root, "object_replacement_manifest", existing)
        stable_fields = (
            "project_id",
            "revision_id",
            "keyframes",
            "asset_catalog",
            "asset_manifest",
            "asset",
            "layer",
            "fallback",
            "status",
            "warnings",
        )
        if any(existing.get(field) != payload.get(field) for field in stable_fields):
            raise StaleApprovalError("object replacement output contains a different binding")
        return output
    return write_validated_artifact(package_root, "object_replacement_manifest", output, payload)
