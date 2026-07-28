from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from videoedit.errors import PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    canonical_sha256,
    validate_artifact,
    write_validated_artifact,
)
from videoedit.services.asset_manifest import write_project_asset_manifest
from videoedit.services.cue_planning import approve_cue_plan_bundle
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


def _path_ref(artifact_id: str, path: Path) -> dict[str, str]:
    return {"artifact_id": artifact_id, "path": str(path.resolve()), "sha256": sha256_file(path)}


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path, Path, Path]:
    layout = initialize_project(tmp_path, "asset_manifest_fixture")
    asset_root = layout.root / "assets"
    asset_root.mkdir(parents=True, exist_ok=True)
    broll_file = asset_root / "workflow.png"
    sound_file = asset_root / "whoosh.wav"
    broll_file.write_bytes(b"workflow-image")
    sound_file.write_bytes(b"whoosh-audio")
    catalog_path = layout.work / "asset-catalog.json"
    catalog_payload: dict[str, Any] = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_asset_manifest_fixture",
        "created_at": "2026-07-24T10:00:00Z",
        "updated_at": "2026-07-24T10:00:00Z",
        "root_path": str(asset_root.resolve()),
        "assets": [
            {
                "asset_id": "asset_fixture_workflow",
                "asset_type": "image",
                "file": {
                    "path": broll_file.name,
                    "sha256": sha256_file(broll_file),
                    "size_bytes": broll_file.stat().st_size,
                    "mime_type": "image/png",
                    "width": 1280,
                    "height": 720,
                    "duration_us": None,
                },
                "description": "Fixture workflow diagram",
                "tags": ["broll", "workflow"],
                "source": "Owned local fixture",
                "licence_status": "owned",
                "licence_reference": "license_asset_manifest_fixture",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
            },
            {
                "asset_id": "snd_fixture_whoosh",
                "asset_type": "sound_effect",
                "file": {
                    "path": sound_file.name,
                    "sha256": sha256_file(sound_file),
                    "size_bytes": sound_file.stat().st_size,
                    "mime_type": "audio/wav",
                    "width": None,
                    "height": None,
                    "duration_us": 300000,
                },
                "description": "Fixture speech-safe whoosh",
                "tags": ["sound", "whoosh"],
                "source": "Owned local fixture",
                "licence_status": "owned",
                "licence_reference": "license_asset_manifest_fixture",
                "permitted_uses": ["fixture video"],
                "attribution": None,
                "sensitive_content": [],
                "usage_history": [],
            },
        ],
    }
    write_validated_artifact(ROOT, "asset_catalog", catalog_path, catalog_payload)

    broll = json.loads((ROOT / "examples" / "broll_plan.example.json").read_text(encoding="utf-8"))
    broll["project_id"] = layout.root.name
    broll["artifact_id"] = "art_broll_manifest_fixture"
    request = broll["requests"][0]
    request.update(
        {
            "asset_id": "asset_fixture_workflow",
            "asset_sha256": sha256_file(broll_file),
            "license_id": "license_asset_manifest_fixture",
            "start_us": 0,
            "end_us": 1000000,
            "duration_us": 1000000,
            "fallback": "base_video",
            "provider": None,
            "model": None,
            "estimated_cost": None,
        }
    )
    broll_path = layout.artifacts / "broll-plan.json"
    write_validated_artifact(ROOT, "broll_plan", broll_path, broll)

    sound = json.loads((ROOT / "examples" / "sound_plan.example.json").read_text(encoding="utf-8"))
    sound["project_id"] = layout.root.name
    sound["artifact_id"] = "art_sound_manifest_fixture"
    sound["catalog_id"] = "catalog_asset_manifest_fixture"
    sound["cues"][0].update(
        {
            "asset_id": "snd_fixture_whoosh",
            "asset_sha256": sha256_file(sound_file),
            "license_id": "license_asset_manifest_fixture",
        }
    )
    sound_path = layout.artifacts / "sound-plan.json"
    write_validated_artifact(ROOT, "sound_plan", sound_path, sound)

    motion = json.loads(
        (ROOT / "examples" / "motion_plan.example.json").read_text(encoding="utf-8")
    )
    motion["project_id"] = layout.root.name
    motion["artifact_id"] = "art_motion_manifest_fixture"
    motion["cues"] = []
    motion_path = layout.artifacts / "motion-plan.json"
    write_validated_artifact(ROOT, "motion_plan", motion_path, motion)

    dependencies = [
        _path_ref("catalog_asset_manifest_fixture", catalog_path),
        _path_ref("art_broll_manifest_fixture", broll_path),
        _path_ref("art_motion_manifest_fixture", motion_path),
        _path_ref("art_sound_manifest_fixture", sound_path),
    ]
    plans = {
        "broll": _path_ref("art_broll_manifest_fixture", broll_path),
        "motion": _path_ref("art_motion_manifest_fixture", motion_path),
        "sound": _path_ref("art_sound_manifest_fixture", sound_path),
    }
    binding = {
        "schema_name": "cue_plan_bundle",
        "schema_version": "1.0.0",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "planning_key": "a" * 64,
        "timeline_duration_us": 60000000,
        "dependencies": dependencies,
        "plans": plans,
        "density_policy": {
            "maximum_broll_coverage_percent": 20,
            "minimum_broll_spacing_us": 15000000,
            "maximum_same_asset_uses_per_project": 1,
            "maximum_motion_cues_per_minute": 1,
            "minimum_motion_spacing_us": 12000000,
            "maximum_sound_cues_per_minute": 3,
        },
        "metrics": {
            "broll_count": 1,
            "broll_coverage_us": 1000000,
            "broll_coverage_percent": 1.6667,
            "motion_count": 0,
            "sound_count": 1,
            "collision_status": "pass",
            "collision_warnings": [],
        },
        "config_sha256": "0" * 64,
    }
    bundle: dict[str, Any] = {
        "schema_name": "cue_plan_bundle",
        "schema_version": "1.0.0",
        "artifact_id": "art_cue_manifest_fixture",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-24T10:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "inputs": [
            {"artifact_id": item["artifact_id"], "sha256": item["sha256"]} for item in dependencies
        ],
        "config_sha256": "0" * 64,
        "planning_key": "a" * 64,
        "timeline_duration_us": 60000000,
        "dependencies": dependencies,
        "plans": plans,
        "density_policy": binding["density_policy"],
        "metrics": binding["metrics"],
        "approval_required": True,
        "approval_state": "proposed",
        "bundle_sha256": canonical_sha256(binding),
        "warnings": [],
    }
    bundle_path = layout.artifacts / "cue-plan-bundle.json"
    write_validated_artifact(ROOT, "cue_plan_bundle", bundle_path, bundle)
    approval_path = approve_cue_plan_bundle(
        ROOT,
        layout,
        bundle_path,
        actor="fixture-reviewer",
        reason="Asset manifest fixture approval",
    )
    return layout, catalog_path, bundle_path, approval_path, broll_file


def test_asset_manifest_records_current_cue_assets_and_reuses_binding(tmp_path: Path) -> None:
    layout, catalog, bundle, approval, _broll_file = _fixture(tmp_path)
    output = layout.artifacts / "asset-manifest.json"
    manifest_path = write_project_asset_manifest(
        ROOT,
        layout,
        catalog,
        cue_bundle_path=bundle,
        cue_approval_path=approval,
        output=output,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "asset_manifest", payload)
    assert {item["asset_id"] for item in payload["assets"]} == {
        "asset_fixture_workflow",
        "snd_fixture_whoosh",
    }
    assert all(
        item["licence_reference"] == "license_asset_manifest_fixture" for item in payload["assets"]
    )
    assert payload["selection_key"]
    assert (
        write_project_asset_manifest(
            ROOT,
            layout,
            catalog,
            cue_bundle_path=bundle,
            cue_approval_path=approval,
            output=output,
        )
        == manifest_path
    )


def test_asset_manifest_requires_current_approval_and_asset_hash(tmp_path: Path) -> None:
    layout, catalog, bundle, approval, broll_file = _fixture(tmp_path)
    with pytest.raises(PlanningValidationError, match="together"):
        write_project_asset_manifest(ROOT, layout, catalog, cue_bundle_path=bundle)

    broll_file.write_bytes(b"changed-workflow-image")
    with pytest.raises(StaleApprovalError, match="asset file hash is stale"):
        write_project_asset_manifest(
            ROOT,
            layout,
            catalog,
            cue_bundle_path=bundle,
            cue_approval_path=approval,
            output=layout.artifacts / "stale-asset-manifest.json",
        )
