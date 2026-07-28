from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.cue_planning import authorize_cue_plan_bundle
from videoedit.services.project import ProjectLayout, sha256_file

ASSET_MANIFEST_IMPLEMENTATION_VERSION = f"{__version__}:asset-manifest-v1"
_ALLOWED_LICENCE_STATUSES = frozenset(
    {"owned", "licensed", "public_domain", "generated_with_recorded_terms"}
)
_BROLL_ASSET_TYPES = frozenset({"broll", "image", "background"})


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object: {path}")
    return value


def _owned(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} must be inside the project: {resolved}") from exc
    return resolved


def _int(value: object, label: str, *, default: int | None = None) -> int:
    if isinstance(value, bool):
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        if default is not None:
            return default
        raise PlanningValidationError(f"{label} must be an integer") from exc


def _string_list(value: object, label: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        if required:
            raise PlanningValidationError(f"{label} must be an array")
        return []
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise PlanningValidationError(f"{label} contains an empty value")
    return result


def _catalog_root(layout: ProjectLayout, catalog_path: Path, catalog: Mapping[str, Any]) -> Path:
    root_value = str(catalog.get("root_path", "")).strip()
    if not root_value:
        raise PlanningValidationError("asset catalog root_path is missing")
    root = Path(root_value).expanduser()
    root = root.resolve() if root.is_absolute() else (catalog_path.parent / root).resolve()
    try:
        root.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"asset catalog root escapes the project: {root}") from exc
    if not root.is_dir():
        raise PlanningValidationError(f"asset catalog root does not exist: {root}")
    return root


def _read_catalog(
    package_root: Path,
    layout: ProjectLayout,
    catalog_path: Path,
) -> tuple[dict[str, Any], Path, str, dict[str, Mapping[str, Any]]]:
    catalog_file = _owned(layout, catalog_path, "asset catalog")
    catalog = _read_object(catalog_file, "asset catalog")
    validate_artifact(package_root, "asset_catalog", catalog)
    root = _catalog_root(layout, catalog_file, catalog)
    assets_value = catalog.get("assets")
    if not isinstance(assets_value, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    assets: dict[str, Mapping[str, Any]] = {}
    for raw_asset in assets_value:
        if not isinstance(raw_asset, Mapping):
            raise PlanningValidationError("asset catalog entry must be an object")
        asset_id = str(raw_asset.get("asset_id", "")).strip()
        if not asset_id or asset_id in assets:
            raise PlanningValidationError(
                f"asset catalog has invalid or duplicate asset: {asset_id}"
            )
        assets[asset_id] = raw_asset
    return catalog, root, sha256_file(catalog_file), assets


def _catalog_asset_file(
    root: Path,
    asset: Mapping[str, Any],
    *,
    asset_id: str,
) -> tuple[Path, Mapping[str, Any]]:
    file_value = asset.get("file")
    if not isinstance(file_value, Mapping):
        raise PlanningValidationError(f"asset file reference is missing: {asset_id}")
    relative_value = str(file_value.get("path", "")).strip()
    relative = Path(relative_value).expanduser()
    if not relative_value or relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise PlanningValidationError(f"asset path must stay inside the catalog root: {asset_id}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PlanningValidationError(f"asset path escapes the catalog root: {asset_id}") from exc
    if not path.is_file():
        raise PlanningValidationError(f"asset file is missing: {path}")
    if str(file_value.get("sha256", "")) != sha256_file(path):
        raise StaleApprovalError(f"asset file hash is stale: {asset_id}")
    if _int(file_value.get("size_bytes"), "asset size", default=-1) != path.stat().st_size:
        raise StaleApprovalError(f"asset file size is stale: {asset_id}")
    if str(asset.get("licence_status", "")) not in _ALLOWED_LICENCE_STATUSES:
        raise PlanningValidationError(f"asset licence status is not allowed: {asset_id}")
    if not str(asset.get("licence_reference", "")).strip():
        raise PlanningValidationError(f"asset licence reference is missing: {asset_id}")
    if not _string_list(asset.get("permitted_uses"), "asset permitted_uses", required=True):
        raise PlanningValidationError(f"asset permitted_uses is missing: {asset_id}")
    return path, file_value


def _range(start_us: object, end_us: object, label: str) -> dict[str, int]:
    start = _int(start_us, f"{label} start")
    end = _int(end_us, f"{label} end")
    if start < 0 or end <= start:
        raise PlanningValidationError(f"{label} must be a positive half-open range")
    return {"start_us": start, "end_us": end}


def _source_range(file_value: Mapping[str, Any]) -> dict[str, int] | None:
    duration = file_value.get("duration_us")
    if duration in (None, ""):
        return None
    duration_us = _int(duration, "asset duration")
    if duration_us <= 0:
        return None
    return {"start_us": 0, "end_us": duration_us}


def _selection(
    asset: Mapping[str, Any],
    file_value: Mapping[str, Any],
    *,
    role: str,
    output_range: dict[str, int] | None,
    effect_ids: Sequence[str],
    approval_ids: Sequence[str],
) -> dict[str, Any]:
    asset_id = str(asset.get("asset_id", "")).strip()
    if not asset_id:
        raise PlanningValidationError("selected asset has no asset_id")
    normalized_effects = [str(value).strip() for value in effect_ids]
    normalized_approvals = [str(value).strip() for value in approval_ids]
    if not normalized_effects or any(not value for value in normalized_effects):
        raise PlanningValidationError(f"selected asset has no effect ID: {asset_id}")
    if not normalized_approvals or any(not value for value in normalized_approvals):
        raise PlanningValidationError(f"selected asset has no approval ID: {asset_id}")
    tags = _string_list(asset.get("tags"), f"asset tags {asset_id}", required=True)
    permitted_uses = _string_list(
        asset.get("permitted_uses"), f"asset permitted_uses {asset_id}", required=True
    )
    sensitive_content = _string_list(asset.get("sensitive_content"), "asset sensitive_content")
    usage_history = asset.get("usage_history")
    if not isinstance(usage_history, list):
        raise PlanningValidationError(f"asset usage_history is missing: {asset_id}")
    if any(not isinstance(item, Mapping) for item in usage_history):
        raise PlanningValidationError(f"asset usage_history contains an invalid entry: {asset_id}")
    attribution = asset.get("attribution")
    if attribution is not None and not isinstance(attribution, str):
        raise PlanningValidationError(f"asset attribution is invalid: {asset_id}")
    return {
        "asset_id": asset_id,
        "asset_sha256": str(file_value["sha256"]),
        "role": role,
        "source_range_us": _source_range(file_value),
        "output_range_us": output_range,
        "effect_ids": normalized_effects,
        "approval_ids": normalized_approvals,
        "licence_reference": str(asset["licence_reference"]),
        "attribution_text": attribution,
        "asset_type": str(asset["asset_type"]),
        "licence_status": str(asset["licence_status"]),
        "source": str(asset["source"]),
        "description": str(asset["description"]),
        "tags": tags,
        "permitted_uses": permitted_uses,
        "sensitive_content": sensitive_content,
        "usage_history": [dict(item) for item in usage_history],
    }


def _verify_plan_ref(
    package_root: Path,
    layout: ProjectLayout,
    ref: Mapping[str, Any],
    *,
    schema_name: str,
    revision_id: str,
) -> tuple[Path, dict[str, Any]]:
    path = _owned(layout, Path(str(ref.get("path", ""))), f"{schema_name} plan")
    expected_hash = str(ref.get("sha256", ""))
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise StaleApprovalError(f"{schema_name} plan is stale or missing: {path}")
    payload = _read_object(path, f"{schema_name} plan")
    validate_artifact(package_root, schema_name, payload)
    if payload.get("artifact_id") != ref.get("artifact_id"):
        raise StaleApprovalError(f"{schema_name} plan artifact identity changed")
    if payload.get("project_id") != layout.root.name or payload.get("revision_id") != revision_id:
        raise StaleApprovalError(f"{schema_name} plan project or revision is stale")
    return path, payload


def _catalog_dependency_matches(
    bundle: Mapping[str, Any], catalog_file: Path, catalog_id: str, catalog_hash: str
) -> bool:
    dependencies = bundle.get("dependencies")
    if not isinstance(dependencies, list):
        return False
    resolved = catalog_file.resolve()
    return any(
        isinstance(item, Mapping)
        and item.get("artifact_id") == catalog_id
        and Path(str(item.get("path", ""))).expanduser().resolve() == resolved
        and item.get("sha256") == catalog_hash
        for item in dependencies
    )


def _cue_selections(
    package_root: Path,
    layout: ProjectLayout,
    bundle: Mapping[str, Any],
    catalog: Mapping[str, Any],
    catalog_root: Path,
    assets: Mapping[str, Mapping[str, Any]],
    *,
    revision_id: str,
    approval_id: str,
) -> list[dict[str, Any]]:
    plans = bundle.get("plans")
    if not isinstance(plans, Mapping):
        raise StaleApprovalError("cue plan bundle has no plan references")
    selections: list[dict[str, Any]] = []
    plan_specs = (
        ("broll", "broll_plan", "requests", "broll"),
        ("sound", "sound_plan", "cues", "sound_effect"),
    )
    for plan_name, schema_name, items_key, role in plan_specs:
        ref = plans.get(plan_name)
        if not isinstance(ref, Mapping):
            raise StaleApprovalError(f"cue plan bundle is missing the {plan_name} plan")
        _plan_path, plan = _verify_plan_ref(
            package_root,
            layout,
            ref,
            schema_name=schema_name,
            revision_id=revision_id,
        )
        if plan_name == "sound" and plan.get("catalog_id") != catalog.get("catalog_id"):
            raise StaleApprovalError("sound plan is stale for the current asset catalog")
        raw_items = plan.get(items_key)
        if not isinstance(raw_items, list):
            raise PlanningValidationError(f"{plan_name} plan {items_key} must be an array")
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise PlanningValidationError(f"{plan_name} selection must be an object")
            asset_value = raw_item.get("asset_id")
            if asset_value is None:
                continue
            asset_id = str(asset_value).strip()
            asset = assets.get(asset_id)
            if asset is None:
                raise PlanningValidationError(f"selected asset is not in the catalog: {asset_id}")
            if role == "broll":
                if asset.get("asset_type") not in _BROLL_ASSET_TYPES:
                    raise PlanningValidationError(
                        f"B-roll selection has an incompatible asset: {asset_id}"
                    )
                effect_id = str(raw_item.get("request_id", "")).strip()
            else:
                if asset.get("asset_type") != "sound_effect":
                    raise PlanningValidationError(
                        f"sound selection has an incompatible asset: {asset_id}"
                    )
                effect_id = str(raw_item.get("cue_id", "")).strip()
            if raw_item.get("provider") not in (None, ""):
                raise PlanningValidationError(
                    f"selected cue requires a disabled provider: {effect_id}"
                )
            expected_hash = str(raw_item.get("asset_sha256", "")).strip()
            expected_license = str(raw_item.get("license_id", "")).strip()
            asset_file, file_value = _catalog_asset_file(catalog_root, asset, asset_id=asset_id)
            _ = asset_file
            if expected_hash != str(file_value.get("sha256", "")):
                raise StaleApprovalError(f"selected cue asset hash is stale: {asset_id}")
            if expected_license != str(asset.get("licence_reference", "")):
                raise StaleApprovalError(f"selected cue licence is stale: {asset_id}")
            output_range = _range(
                raw_item.get("start_us"), raw_item.get("end_us"), f"selected cue {effect_id}"
            )
            selections.append(
                _selection(
                    asset,
                    file_value,
                    role=role,
                    output_range=output_range,
                    effect_ids=[effect_id],
                    approval_ids=[approval_id],
                )
            )
    return selections


def _replacement_selections(
    package_root: Path,
    layout: ProjectLayout,
    replacement_paths: Sequence[Path],
    catalog_file: Path,
    catalog_hash: str,
    catalog_root: Path,
    assets: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    selections: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    for raw_path in replacement_paths:
        path = _owned(layout, raw_path, "object replacement manifest")
        payload = _read_object(path, "object replacement manifest")
        validate_artifact(package_root, "object_replacement_manifest", payload)
        catalog_ref = payload.get("asset_catalog")
        if not isinstance(catalog_ref, Mapping) or catalog_ref.get("sha256") != catalog_hash:
            raise StaleApprovalError(
                f"replacement manifest is stale for the current catalog: {path}"
            )
        if Path(str(catalog_ref.get("path", ""))).expanduser().resolve() != catalog_file.resolve():
            raise StaleApprovalError(f"replacement manifest catalog path is stale: {path}")
        asset_payload = payload.get("asset")
        if not isinstance(asset_payload, Mapping):
            raise PlanningValidationError(f"replacement manifest has no asset: {path}")
        asset_id = str(asset_payload.get("asset_id", "")).strip()
        asset = assets.get(asset_id)
        if asset is None or asset.get("asset_type") != "replacement_object":
            raise PlanningValidationError(
                f"replacement asset is not current in the catalog: {asset_id}"
            )
        asset_file, file_value = _catalog_asset_file(catalog_root, asset, asset_id=asset_id)
        _ = asset_file
        replacement_file = asset_payload.get("file")
        if not isinstance(replacement_file, Mapping):
            raise PlanningValidationError(f"replacement manifest asset file is missing: {path}")
        if replacement_file.get("sha256") != file_value.get("sha256"):
            raise StaleApprovalError(f"replacement asset hash is stale: {asset_id}")
        if asset_payload.get("licence_reference") != asset.get("licence_reference"):
            raise StaleApprovalError(f"replacement asset licence is stale: {asset_id}")
        approval_ids = asset_payload.get("approval_ids")
        normalized_approvals = _string_list(approval_ids, "replacement approval_ids", required=True)
        selections.append(
            _selection(
                asset,
                file_value,
                role="tracked_object_replacement",
                output_range=None,
                effect_ids=[str(payload["artifact_id"])],
                approval_ids=normalized_approvals,
            )
        )
        inputs.append(artifact_input(str(payload["artifact_id"]), path))
    return selections, inputs


def _append_unique_input(
    inputs: list[dict[str, str]], seen: set[tuple[str, str]], artifact_id: str, path: Path
) -> None:
    value = (artifact_id, sha256_file(path))
    if value not in seen:
        inputs.append(artifact_input(artifact_id, path))
        seen.add(value)


def write_project_asset_manifest(
    package_root: Path,
    layout: ProjectLayout,
    catalog_path: Path,
    *,
    cue_bundle_path: Path | None = None,
    cue_approval_path: Path | None = None,
    replacement_manifest_paths: Sequence[Path] = (),
    output: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    """Persist current selected local assets and licence provenance for a revision."""

    catalog_file = _owned(layout, catalog_path, "asset catalog")
    catalog, catalog_root, catalog_hash, assets = _read_catalog(package_root, layout, catalog_file)
    selections: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    seen_inputs: set[tuple[str, str]] = set()
    _append_unique_input(inputs, seen_inputs, str(catalog["catalog_id"]), catalog_file)
    warnings: list[str] = []

    if (cue_bundle_path is None) != (cue_approval_path is None):
        raise PlanningValidationError("cue bundle and cue approval must be supplied together")
    if cue_bundle_path is not None and cue_approval_path is not None:
        bundle_file = _owned(layout, cue_bundle_path, "cue plan bundle")
        approval_file = _owned(layout, cue_approval_path, "cue plan approval")
        approval = authorize_cue_plan_bundle(
            package_root, layout, bundle_file, approval_file, revision_id=revision_id
        )
        approval_payload = _read_object(approval_file, "cue plan approval")
        validate_artifact(package_root, "approval_record", approval_payload)
        bundle = _read_object(bundle_file, "cue plan bundle")
        validate_artifact(package_root, "cue_plan_bundle", bundle)
        if bundle.get("project_id") != layout.root.name or bundle.get("revision_id") != revision_id:
            raise StaleApprovalError("cue plan bundle project or revision is stale")
        if not _catalog_dependency_matches(
            bundle, catalog_file, str(catalog["catalog_id"]), catalog_hash
        ):
            raise StaleApprovalError("cue plan bundle is stale for the current asset catalog")
        selections.extend(
            _cue_selections(
                package_root,
                layout,
                bundle,
                catalog,
                catalog_root,
                assets,
                revision_id=revision_id,
                approval_id=str(approval["approval_id"]),
            )
        )
        _append_unique_input(inputs, seen_inputs, str(bundle["artifact_id"]), bundle_file)
        _append_unique_input(
            inputs, seen_inputs, str(approval_payload["artifact_id"]), approval_file
        )
        plans = bundle.get("plans")
        if isinstance(plans, Mapping):
            for plan_name in ("broll", "motion", "sound"):
                ref = plans.get(plan_name)
                if isinstance(ref, Mapping):
                    plan_path = _owned(layout, Path(str(ref.get("path", ""))), f"{plan_name} plan")
                    _append_unique_input(
                        inputs, seen_inputs, str(ref.get("artifact_id", "")), plan_path
                    )

    replacement_selections, replacement_inputs = _replacement_selections(
        package_root,
        layout,
        replacement_manifest_paths,
        catalog_file,
        catalog_hash,
        catalog_root,
        assets,
    )
    selections.extend(replacement_selections)
    for item in replacement_inputs:
        key = (item["artifact_id"], item["sha256"])
        if key not in seen_inputs:
            inputs.append(item)
            seen_inputs.add(key)
    if not selections:
        warnings.append("no_external_assets_selected")

    selection_key = canonical_sha256(
        {
            "implementation": ASSET_MANIFEST_IMPLEMENTATION_VERSION,
            "project_id": layout.root.name,
            "revision_id": revision_id,
            "catalog_sha256": catalog_hash,
            "inputs": inputs,
            "assets": selections,
        }
    )
    destination = _owned(
        layout,
        output or layout.artifacts / f"asset-manifest-{selection_key[:16]}.json",
        "asset manifest output",
    )
    if destination.is_file():
        existing = _read_object(destination, "existing asset manifest")
        validate_artifact(package_root, "asset_manifest", existing)
        if existing.get("selection_key") == selection_key:
            return destination
        raise StaleApprovalError(
            f"asset manifest output contains a different binding: {destination}"
        )

    payload: dict[str, Any] = {
        "schema_name": "asset_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_asset_manifest",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "catalog_sha256": catalog_hash,
        "catalog_id": str(catalog["catalog_id"]),
        "selection_key": selection_key,
        "producer": producer(
            "asset-manifest", "licensed-local-catalog", ASSET_MANIFEST_IMPLEMENTATION_VERSION
        ),
        "inputs": inputs,
        "assets": selections,
        "warnings": warnings,
    }
    return write_validated_artifact(package_root, "asset_manifest", destination, payload)


__all__ = ["write_project_asset_manifest"]
