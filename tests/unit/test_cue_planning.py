from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import write_validated_artifact
from videoedit.services.asset_search import search_local_assets
from videoedit.services.cue_planning import (
    approve_cue_plan_bundle,
    authorize_cue_plan_bundle,
    write_cue_plan_bundle,
)
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


def _asset(
    root: Path,
    name: str,
    asset_id: str,
    asset_type: str,
    description: str,
    tags: list[str],
    *,
    duration_us: int | None = None,
    audio_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    path = root / name
    path.write_bytes(f"fixture-{asset_id}".encode())
    value: dict[str, object] = {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "file": {
            "path": name,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "mime_type": "audio/wav" if asset_type == "sound_effect" else "image/png",
            "width": None if asset_type == "sound_effect" else 1280,
            "height": None if asset_type == "sound_effect" else 720,
            "duration_us": duration_us,
        },
        "description": description,
        "tags": tags,
        "source": "Owned local cue-planning fixture",
        "licence_status": "owned",
        "licence_reference": f"license_{asset_id}",
        "permitted_uses": ["fixture video"],
        "attribution": None,
        "sensitive_content": [],
        "usage_history": [],
    }
    if audio_metadata is not None:
        value["audio_metadata"] = audio_metadata
    return value


def _fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path, Path]:
    layout = initialize_project(tmp_path, "cue_fixture")
    asset_root = layout.root / "assets"
    asset_root.mkdir()
    catalog_payload = {
        "schema_name": "asset_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "catalog_cue_fixture",
        "created_at": "2026-07-24T10:00:00Z",
        "updated_at": "2026-07-24T10:00:00Z",
        "root_path": str(asset_root.resolve()),
        "assets": [
            _asset(
                asset_root,
                "workflow.png",
                "asset_workflow",
                "broll",
                "A clean workflow diagram for a local editing pipeline",
                ["workflow", "diagram", "broll"],
            ),
            _asset(
                asset_root,
                "whoosh.wav",
                "snd_whoosh",
                "sound_effect",
                "Subtle speech-safe transition whoosh",
                ["whoosh", "transition", "speech-safe"],
                duration_us=300_000,
                audio_metadata={
                    "integrated_loudness_lufs": -28.0,
                    "true_peak_dbtp": -6.0,
                    "transient_peak_offset_us": 120_000,
                    "intended_transition_types": ["swipe_left"],
                    "intensity": "subtle",
                    "speech_safe": True,
                    "minimum_reuse_interval_us": 45_000_000,
                    "brand_contexts": ["demo"],
                },
            ),
        ],
    }
    catalog_path = layout.artifacts / "asset-catalog.json"
    write_validated_artifact(ROOT, "asset_catalog", catalog_path, catalog_payload)

    transition_payload = json.loads(
        (ROOT / "examples" / "transition_plan.example.json").read_text(encoding="utf-8")
    )
    transition_payload["project_id"] = layout.root.name
    transition_path = layout.artifacts / "transition-plan.json"
    write_validated_artifact(ROOT, "transition_plan", transition_path, transition_payload)
    search_path = search_local_assets(
        ROOT,
        layout,
        catalog_path,
        query="workflow diagram",
        effect_intent="show the editing pipeline",
        asset_type="broll",
    )
    return layout, catalog_path, transition_path, search_path


def test_cue_plan_bundle_is_schema_valid_proposed_and_idempotent(tmp_path: Path) -> None:
    layout, catalog, transition, search = _fixture(tmp_path)
    windows = [
        {
            "start_us": 0,
            "end_us": 1_000_000,
            "transcript_context": "workflow diagram",
            "rationale": "Clarify the named pipeline while it is discussed.",
        }
    ]
    first = write_cue_plan_bundle(
        ROOT,
        layout,
        transition,
        catalog,
        search_result_path=search,
        timeline_duration_us=60_000_000,
        broll_windows=windows,
    )
    first_hash = sha256_file(first)
    second = write_cue_plan_bundle(
        ROOT,
        layout,
        transition,
        catalog,
        search_result_path=search,
        timeline_duration_us=60_000_000,
        broll_windows=windows,
    )
    bundle = json.loads(first.read_text(encoding="utf-8"))
    broll = json.loads((layout.artifacts / "broll-plan.json").read_text(encoding="utf-8"))
    motion = json.loads((layout.artifacts / "motion-plan.json").read_text(encoding="utf-8"))
    sound = json.loads((layout.artifacts / "sound-plan.json").read_text(encoding="utf-8"))

    assert second == first
    assert sha256_file(second) == first_hash
    assert bundle["approval_required"] is True
    assert bundle["approval_state"] == "proposed"
    assert bundle["metrics"]["collision_status"] == "pass"
    assert bundle["metrics"]["broll_count"] == 1
    assert broll["requests"][0]["approval_state"] == "proposed"
    assert broll["requests"][0]["asset_id"] == "asset_workflow"
    assert motion["cues"][0]["approval_state"] == "proposed"
    assert sound["cues"][0]["approval_state"] == "proposed"


def test_cue_plan_approval_is_human_bound_and_stale_on_dependency_change(tmp_path: Path) -> None:
    layout, catalog, transition, search = _fixture(tmp_path)
    bundle = write_cue_plan_bundle(
        ROOT,
        layout,
        transition,
        catalog,
        search_result_path=search,
        timeline_duration_us=60_000_000,
        broll_windows=[{"start_us": 0, "end_us": 1_000_000}],
    )
    approval = approve_cue_plan_bundle(
        ROOT,
        layout,
        bundle,
        actor="fixture-reviewer",
        reason="Fixture approval boundary test",
    )
    authorization = authorize_cue_plan_bundle(ROOT, layout, bundle, approval)
    assert authorization["approval_id"].startswith("apr_cue_")

    catalog.write_text(catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(StaleApprovalError, match="dependency is stale"):
        authorize_cue_plan_bundle(ROOT, layout, bundle, approval)


def test_broll_is_omitted_when_it_collides_with_motion_and_external_paths_fail_closed(
    tmp_path: Path,
) -> None:
    layout, catalog, transition, search = _fixture(tmp_path)
    bundle = write_cue_plan_bundle(
        ROOT,
        layout,
        transition,
        catalog,
        search_result_path=search,
        timeline_duration_us=60_000_000,
        broll_windows=[{"start_us": 2_400_000, "end_us": 2_700_000}],
    )
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    broll = json.loads((layout.artifacts / "broll-plan.json").read_text(encoding="utf-8"))
    assert broll["requests"] == []
    assert "broll_collision_with_motion:1" in broll["warnings"]
    assert payload["metrics"]["collision_status"] == "warning"

    with pytest.raises(PlanningValidationError, match="must be inside the project"):
        write_cue_plan_bundle(
            ROOT,
            layout,
            transition,
            catalog,
            search_result_path=ROOT / "examples" / "asset_search_result.example.json",
        )
