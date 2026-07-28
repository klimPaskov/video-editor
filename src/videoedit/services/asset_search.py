from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit import __version__
from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file

ASSET_SEARCH_IMPLEMENTATION_VERSION = f"{__version__}:asset-search-v1"
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{label} must be a JSON object")
    return value


def _owned(layout: ProjectLayout, path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{label} escapes the project: {resolved}") from exc
    return resolved


def _tokens(values: Iterable[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for value in values:
        selected.update(
            token for token in TOKEN_PATTERN.findall(value.lower()) if token not in STOP_WORDS
        )
    return tuple(sorted(selected))


def _catalog_root(catalog_path: Path, catalog: Mapping[str, Any]) -> Path:
    root_value = Path(str(catalog["root_path"])).expanduser()
    return (
        root_value.resolve()
        if root_value.is_absolute()
        else (catalog_path.parent / root_value).resolve()
    )


def _validate_catalog(
    package_root: Path,
    layout: ProjectLayout,
    catalog_path: Path,
) -> tuple[dict[str, Any], Path, str]:
    catalog = _read_object(catalog_path, "asset catalog")
    validate_artifact(package_root, "asset_catalog", catalog)
    root = _catalog_root(catalog_path, catalog)
    try:
        root.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"asset catalog root escapes the project: {root}") from exc
    if not root.is_dir():
        raise PlanningValidationError(f"asset catalog root does not exist: {root}")
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise PlanningValidationError("asset catalog assets must be an array")
    seen: set[str] = set()
    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            raise PlanningValidationError("asset catalog entry must be an object")
        asset_id = str(raw_asset["asset_id"])
        if asset_id in seen:
            raise PlanningValidationError(f"asset catalog contains duplicate asset id: {asset_id}")
        seen.add(asset_id)
        file_value = raw_asset.get("file")
        if not isinstance(file_value, Mapping):
            raise PlanningValidationError(f"asset {asset_id} has no file reference")
        candidate = Path(str(file_value["path"])).expanduser()
        file_path = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        try:
            file_path.relative_to(root)
        except ValueError as exc:
            raise PlanningValidationError(f"asset {asset_id} escapes the catalog root") from exc
        if not file_path.is_file():
            raise PlanningValidationError(f"asset file is missing: {file_path}")
        if file_value.get("sha256") != sha256_file(file_path):
            raise PlanningValidationError(f"asset file hash is stale: {asset_id}")
        if int(file_value.get("size_bytes", -1)) != file_path.stat().st_size:
            raise PlanningValidationError(f"asset file size is stale: {asset_id}")
        if not str(raw_asset.get("licence_reference", "")).strip():
            raise PlanningValidationError(f"asset licence reference is missing: {asset_id}")
    return catalog, root, sha256_file(catalog_path)


def _field_tokens(asset: Mapping[str, Any], field: str) -> set[str]:
    value = asset.get(field)
    if isinstance(value, str):
        return set(_tokens((value,)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return set(_tokens(str(item) for item in value))
    return set()


def _score_asset(asset: Mapping[str, Any], terms: Sequence[str]) -> tuple[float, list[str]]:
    weights = (
        ("asset_id", 5.0),
        ("asset_type", 5.0),
        ("description", 4.0),
        ("tags", 3.0),
        ("source", 2.0),
        ("permitted_uses", 1.0),
    )
    matched: set[str] = set()
    score = 0.0
    for field, weight in weights:
        field_terms = _field_tokens(asset, field)
        overlap = field_terms.intersection(terms)
        if overlap:
            score += weight * len(overlap)
            matched.update(overlap)
    return score, sorted(matched)


def _output_path(layout: ProjectLayout, search_hash: str, output: Path | None) -> Path:
    if output is not None:
        return _owned(layout, output, "asset search output")
    return layout.work / f"asset-search-{search_hash[:16]}.json"


def search_local_assets(
    package_root: Path,
    layout: ProjectLayout,
    catalog_path: Path,
    *,
    query: str,
    effect_intent: str = "",
    asset_type: str | None = None,
    required_tags: Sequence[str] = (),
    limit: int = 10,
    revision_id: str = "rev_001",
    output: Path | None = None,
) -> Path:
    """Rank current local catalogue entries using transcript and effect terms."""

    if not query.strip():
        raise PlanningValidationError("asset search query must not be empty")
    if limit < 1 or limit > 50:
        raise PlanningValidationError("asset search limit must be between 1 and 50")
    normalized_type = asset_type.strip() if asset_type else None
    normalized_tags = tuple(sorted({tag.strip().lower() for tag in required_tags if tag.strip()}))
    terms = _tokens((query, effect_intent, *normalized_tags))
    if not terms:
        raise PlanningValidationError("asset search query has no searchable terms")
    resolved_catalog = _owned(layout, catalog_path, "asset catalog")
    catalog, _root, catalog_hash = _validate_catalog(package_root, layout, resolved_catalog)
    search_binding = {
        "catalog_sha256": catalog_hash,
        "query": query,
        "effect_intent": effect_intent,
        "asset_type": normalized_type,
        "required_tags": list(normalized_tags),
        "limit": limit,
        "revision_id": revision_id,
    }
    search_hash = canonical_sha256(search_binding)
    destination = _output_path(layout, search_hash, output).resolve()
    if destination.is_file():
        existing = _read_object(destination, "existing asset search result")
        validate_artifact(package_root, "asset_search_result", existing)
        if (
            existing.get("search_sha256") == search_hash
            and existing.get("catalog_sha256") == catalog_hash
        ):
            return destination
    results: list[dict[str, Any]] = []
    for raw_asset in catalog["assets"]:
        if not isinstance(raw_asset, Mapping):
            continue
        if normalized_type is not None and raw_asset.get("asset_type") != normalized_type:
            continue
        tags = {str(tag).lower() for tag in raw_asset.get("tags", [])}
        if any(tag not in tags for tag in normalized_tags):
            continue
        score, matched_terms = _score_asset(raw_asset, terms)
        if score <= 0:
            continue
        file_value = raw_asset["file"]
        results.append(
            {
                "asset_id": raw_asset["asset_id"],
                "asset_type": raw_asset["asset_type"],
                "asset_sha256": file_value["sha256"],
                "path": file_value["path"],
                "description": raw_asset["description"],
                "tags": list(raw_asset["tags"]),
                "score": score,
                "matched_terms": matched_terms,
                "licence_status": raw_asset["licence_status"],
                "licence_reference": raw_asset["licence_reference"],
            }
        )
    results.sort(key=lambda item: (-float(item["score"]), str(item["asset_id"])))
    warnings = ["no_catalog_match"] if not results else []
    payload: dict[str, Any] = {
        "schema_name": "asset_search_result",
        "schema_version": "1.0.0",
        "artifact_id": "art_asset_search",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer(
            "asset-search", "licensed-local-catalog", ASSET_SEARCH_IMPLEMENTATION_VERSION
        ),
        "inputs": [artifact_input("art_asset_catalog", resolved_catalog)],
        "catalog_sha256": catalog_hash,
        "query": query,
        "effect_intent": effect_intent,
        "asset_type": normalized_type,
        "required_tags": list(normalized_tags),
        "limit": limit,
        "search_sha256": search_hash,
        "status": "complete",
        "results": results[:limit],
        "warnings": warnings,
    }
    validate_artifact(package_root, "asset_search_result", payload)
    return write_validated_artifact(package_root, "asset_search_result", destination, payload)
