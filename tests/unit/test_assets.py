from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.assets import index_local_asset_catalog
from videoedit.services.project import ProjectLayout, sha256_file


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


class _FakeProbeAdapter:
    def probe(self, path: Path) -> dict[str, object]:
        assert path.is_file()
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "png",
                    "width": 50,
                    "height": 50,
                    "avg_frame_rate": "1/1",
                    "duration": "1.000000",
                }
            ],
            "format": {"duration": "1.000000"},
        }


def _catalog(layout: ProjectLayout, root: Path, asset: Path) -> Path:
    payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_fixture",
        "created_at": "2026-07-24T10:00:00Z",
        "updated_at": "2026-07-24T10:00:00Z",
        "root_path": str(root.resolve()),
        "assets": [
            {
                "asset_id": "asset_fixture",
                "asset_type": "replacement_object",
                "file": {
                    "path": asset.name,
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                    "mime_type": None,
                    "width": None,
                    "height": None,
                    "duration_us": None,
                },
                "description": "Fixture replacement object",
                "tags": ["fixture", "replacement"],
                "source": "Project-owned fixture",
                "licence_status": "owned",
                "licence_reference": "license_fixture_internal",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
            }
        ],
    }
    path = layout.work / "asset-catalog-metadata.json"
    write_validated_artifact(package_root(), "asset_catalog", path, payload)
    return path


def test_index_local_asset_catalog_hashes_probes_and_is_idempotent(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "project")
    root = layout.root / "assets"
    root.mkdir(parents=True)
    asset = root / "replacement.png"
    asset.write_bytes(b"fixture asset")
    metadata = _catalog(layout, root, asset)
    output = layout.work / "asset-catalog.json"

    first = index_local_asset_catalog(
        package_root(), layout, root, metadata, output, adapter=_FakeProbeAdapter()
    )
    first_hash = sha256_file(first)
    second = index_local_asset_catalog(
        package_root(), layout, root, metadata, output, adapter=_FakeProbeAdapter()
    )
    payload = json.loads(first.read_text(encoding="utf-8"))
    file_payload = payload["assets"][0]["file"]

    assert second == first
    assert sha256_file(second) == first_hash
    assert file_payload["sha256"] == sha256_file(asset)
    assert file_payload["size_bytes"] == asset.stat().st_size
    assert file_payload["width"] == 50
    assert file_payload["height"] == 50
    assert file_payload["duration_us"] == 1_000_000


def test_index_local_asset_catalog_rejects_assets_outside_root(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "project")
    root = layout.root / "assets"
    root.mkdir(parents=True)
    outside = layout.root / "outside.png"
    outside.write_bytes(b"outside")
    metadata = _catalog(layout, root, root / "replacement.png")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["assets"][0]["file"]["path"] = "../outside.png"
    write_validated_artifact(package_root(), "asset_catalog", metadata, payload)

    with pytest.raises(PlanningValidationError, match="escapes"):
        index_local_asset_catalog(
            package_root(), layout, root, metadata, adapter=_FakeProbeAdapter()
        )
