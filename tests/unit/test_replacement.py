from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import ApprovalRequiredError, StaleApprovalError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import sha256_file
from videoedit.services.replacement import write_object_replacement_manifest
from videoedit.services.tracking import write_object_track_keyframes, write_object_track_review


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_result(path: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "job_id": "replacement-fixture",
        "status": "complete",
        "worker": "sam3",
        "input_path": "/fixture/source.mp4",
        "input_sha256": "0" * 64,
        "prompt": {"text": "object", "frame_index": 0},
        "mask_pattern": "/fixture/masks/%06d.png",
        "frame_count": 3,
        "frames": [
            {
                "frame_index": index,
                "combined_mask_path": f"/fixture/masks/{index:06d}.png",
                "objects": [
                    {
                        "object_id": 1,
                        "mask_path": f"/fixture/masks/{index:06d}.png",
                        "visible": True,
                        "area_pixels": 400 + (index * 20),
                        "bbox_xywh": [100 + (index * 5), 100, 20 + index, 20 + index],
                        "centroid_xy": [110.0 + (index * 5), 110.0],
                    }
                ],
            }
            for index in range(3)
        ],
        "software": {"sam3": "fixture", "python": "3.12"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_validation(tmp_path: Path, result: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture source")
    payload = {
        "schema_name": "segmentation_validation",
        "schema_version": "1.0.0",
        "artifact_id": "art_segmentation_validation",
        "project_id": "track_review",
        "revision_id": "rev_001",
        "created_at": "2026-07-24T12:00:00Z",
        "source": {
            "path": str(source.resolve()),
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
        },
        "result": {
            "path": str(result.resolve()),
            "sha256": sha256_file(result),
            "size_bytes": result.stat().st_size,
        },
        "source_range": {"start_frame": 0, "end_frame": 3},
        "status": "pass",
        "validation": {
            "result_schema": "pass",
            "source_identity": "pass",
            "frame_range": "pass",
            "frame_continuity": "pass",
            "mask_files": "pass",
            "mask_dimensions": "pass",
            "mask_format": "pass",
            "geometry": "pass",
        },
        "diagnostics": {
            "missing_frames": [],
            "identity_warnings": [],
            "area_jump_frames": [],
            "centroid_jump_frames": [],
            "leak_warnings": [],
        },
        "review_frame_indices": [0, 1, 2],
        "contact_sheets": [],
        "warnings": [],
    }
    path = tmp_path / "segmentation-validation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_track_artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    result = tmp_path / "segmentation-result.json"
    _write_result(result)
    validation = _write_validation(tmp_path, result)
    review = write_object_track_review(
        package_root(),
        result,
        validation,
        output_path=tmp_path / "object-track-review.json",
        object_id=1,
        actor="fixture-reviewer",
        decision="approved",
        findings={
            "identity": "pass",
            "continuity": "pass",
            "geometry": "pass",
            "occlusion": "pass",
        },
    )
    keyframes = write_object_track_keyframes(
        package_root(),
        result,
        validation,
        review,
        output_path=tmp_path / "object-track-keyframes.json",
        timeline_width=640,
        timeline_height=360,
        object_id=1,
        frame_rate=30,
    )
    return result, keyframes, review


def _write_asset_contracts(tmp_path: Path, keyframes: Path) -> tuple[Path, Path, Path]:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    asset_file = asset_root / "replacement.png"
    asset_file.write_bytes(b"synthetic local replacement asset")
    catalog = tmp_path / "asset-catalog.json"
    catalog_payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_fixture",
        "created_at": "2026-07-24T12:00:00Z",
        "updated_at": "2026-07-24T12:00:00Z",
        "root_path": str(asset_root.resolve()),
        "assets": [
            {
                "asset_id": "asset_fixture_replace",
                "asset_type": "replacement_object",
                "file": {
                    "path": "replacement.png",
                    "sha256": sha256_file(asset_file),
                    "size_bytes": asset_file.stat().st_size,
                },
                "description": "Synthetic local replacement fixture",
                "tags": ["fixture", "replacement"],
                "source": "Synthetic fixture",
                "licence_status": "owned",
                "licence_reference": "license_fixture_internal",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
            }
        ],
    }
    catalog.write_text(json.dumps(catalog_payload), encoding="utf-8")
    manifest = tmp_path / "asset-manifest.json"
    manifest_payload = {
        "schema_name": "asset_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_asset_manifest",
        "project_id": "track_review",
        "revision_id": "rev_001",
        "created_at": "2026-07-24T12:00:00Z",
        "catalog_sha256": sha256_file(catalog),
        "assets": [
            {
                "asset_id": "asset_fixture_replace",
                "asset_sha256": sha256_file(asset_file),
                "role": "tracked_object_replacement",
                "source_range_us": None,
                "output_range_us": None,
                "effect_ids": ["effect_fixture_replace"],
                "approval_ids": ["apr_fixture_replace"],
                "licence_reference": "license_fixture_internal",
                "attribution_text": None,
            }
        ],
    }
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return catalog, manifest, asset_file


def test_replacement_manifest_binds_asset_provenance_and_fallback(tmp_path: Path) -> None:
    _result, keyframes, _review = _write_track_artifacts(tmp_path)
    catalog, manifest, asset_file = _write_asset_contracts(tmp_path, keyframes)

    output = write_object_replacement_manifest(
        package_root(),
        keyframes,
        catalog,
        manifest,
        "asset_fixture_replace",
        output_path=tmp_path / "object-replacement.json",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    validate_artifact(package_root(), "object_replacement_manifest", payload)
    assert payload["asset"]["file"]["sha256"] == sha256_file(asset_file)
    assert payload["fallback"] == {"mode": "original_shot", "on_uncertain": "keep_original"}
    assert payload["layer"]["duration_frames"] == 3


def test_replacement_manifest_requires_approval_selection(tmp_path: Path) -> None:
    _result, keyframes, _review = _write_track_artifacts(tmp_path)
    catalog, manifest, _asset_file = _write_asset_contracts(tmp_path, keyframes)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["assets"][0]["approval_ids"] = []
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(ApprovalRequiredError, match="approval IDs"):
        write_object_replacement_manifest(
            package_root(),
            keyframes,
            catalog,
            manifest,
            "asset_fixture_replace",
            output_path=tmp_path / "object-replacement.json",
        )


def test_replacement_manifest_rejects_changed_asset_file(tmp_path: Path) -> None:
    _result, keyframes, _review = _write_track_artifacts(tmp_path)
    catalog, manifest, asset_file = _write_asset_contracts(tmp_path, keyframes)
    asset_file.write_bytes(b"changed asset")

    with pytest.raises(StaleApprovalError, match="asset hash"):
        write_object_replacement_manifest(
            package_root(),
            keyframes,
            catalog,
            manifest,
            "asset_fixture_replace",
            output_path=tmp_path / "object-replacement.json",
        )
