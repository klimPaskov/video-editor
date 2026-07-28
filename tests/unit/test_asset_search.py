from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.asset_search import search_local_assets
from videoedit.services.project import ProjectLayout, sha256_file


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _catalog(tmp_path: Path) -> tuple[ProjectLayout, Path, Path]:
    layout = ProjectLayout(tmp_path / "project")
    root = layout.root / "assets"
    root.mkdir(parents=True)
    asset = root / "apple.png"
    asset.write_bytes(b"replacement fixture")
    payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_search_fixture",
        "created_at": "2026-07-24T10:00:00Z",
        "updated_at": "2026-07-24T10:00:00Z",
        "root_path": str(root.resolve()),
        "assets": [
            {
                "asset_id": "asset_apple",
                "asset_type": "replacement_object",
                "file": {
                    "path": asset.name,
                    "sha256": sha256_file(asset),
                    "size_bytes": asset.stat().st_size,
                    "mime_type": "image/png",
                    "width": 50,
                    "height": 50,
                    "duration_us": None,
                },
                "description": "Red apple replacement object",
                "tags": ["apple", "fruit", "replacement"],
                "source": "Owned local fixture",
                "licence_status": "owned",
                "licence_reference": "license_search_fixture",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
            }
        ],
    }
    catalog_path = layout.work / "asset-catalog.json"
    write_validated_artifact(package_root(), "asset_catalog", catalog_path, payload)
    return layout, catalog_path, asset


def test_search_local_assets_ranks_current_provenance_bound_matches_idempotently(
    tmp_path: Path,
) -> None:
    layout, catalog, _asset = _catalog(tmp_path)
    first = search_local_assets(
        package_root(),
        layout,
        catalog,
        query="replace the tracked object",
        effect_intent="tracked object replacement",
        asset_type="replacement_object",
        required_tags=("replacement",),
        limit=5,
    )
    first_hash = sha256_file(first)
    second = search_local_assets(
        package_root(),
        layout,
        catalog,
        query="replace the tracked object",
        effect_intent="tracked object replacement",
        asset_type="replacement_object",
        required_tags=("replacement",),
        limit=5,
    )
    payload = json.loads(first.read_text(encoding="utf-8"))

    assert second == first
    assert sha256_file(second) == first_hash
    assert payload["results"][0]["asset_id"] == "asset_apple"
    assert payload["results"][0]["licence_reference"] == "license_search_fixture"
    assert "replacement" in payload["results"][0]["matched_terms"]


def test_search_local_assets_rejects_stale_file_and_records_no_match(tmp_path: Path) -> None:
    layout, catalog, asset = _catalog(tmp_path)
    asset.write_bytes(b"changed fixture")
    with pytest.raises(PlanningValidationError, match="hash is stale"):
        search_local_assets(package_root(), layout, catalog, query="replacement")

    layout, catalog, _asset = _catalog(tmp_path / "no-match")
    result = search_local_assets(package_root(), layout, catalog, query="spaceship")
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["results"] == []
    assert payload["warnings"] == ["no_catalog_match"]
