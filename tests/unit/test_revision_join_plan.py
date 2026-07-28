from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.join_repair import write_revision_join_plan
from videoedit.services.project import initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


def _join_plan(project_id: str) -> dict[str, object]:
    return {
        "schema_name": "join_plan",
        "schema_version": "1.0.0",
        "artifact_id": "art_join_parent",
        "project_id": project_id,
        "revision_id": "rev_001",
        "created_at": "2026-07-28T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "inputs": [{"artifact_id": "art_source", "sha256": "a" * 64}],
        "config_sha256": "b" * 64,
        "output_duration_us": 10_000_000,
        "joins": [
            {
                "join_id": "join_000001",
                "proposal_ids": ["prp_000001"],
                "source_cut_range": {"start_us": 1_000_000, "end_us": 1_100_000},
                "source_preview_ranges": [
                    {"start_us": 0, "end_us": 2_000_000},
                    {"start_us": 2_100_000, "end_us": 3_000_000},
                ],
                "output_join_us": 4_000_000,
                "preview_range": {"start_us": 3_000_000, "end_us": 5_000_000},
                "join_strategy": "hard_cut",
                "repair_order": ["hard_cut"],
                "fallback": "hard_cut",
                "handles": {"pre_handle_us": 80_000, "post_handle_us": 110_000},
                "reason": "Fixture join",
                "status": "planned",
                "repair_action": None,
                "repair_attempt": 0,
                "review_required": False,
            }
        ],
        "warnings": [],
    }


def _revision_media(project_id: str, output: Path) -> dict[str, object]:
    return {
        "schema_name": "revision_media_manifest",
        "schema_version": "1.0.0",
        "artifact_id": "art_revision_media_rev_002",
        "project_id": project_id,
        "revision_id": "rev_002",
        "parent_revision_id": "rev_001",
        "created_at": "2026-07-28T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "fixture",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "source_markers": {
            "artifact_id": "art_markers",
            "path": "markers.json",
            "sha256": "c" * 64,
        },
        "source": {"artifact_id": "art_source", "path": "source.mp4", "sha256": "d" * 64},
        "output": {
            "artifact_id": "art_output",
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
        },
        "source_duration_us": 10_000_000,
        "output_duration_us": 9_000_000,
        "removed_ranges": [{"start_us": 2_000_000, "end_us": 3_000_000}],
        "keep_ranges": [
            {"start_us": 0, "end_us": 2_000_000},
            {"start_us": 3_000_000, "end_us": 10_000_000},
        ],
        "source_to_output_mapping": [
            {
                "source_start_us": 0,
                "source_end_us": 2_000_000,
                "output_start_us": 0,
                "output_end_us": 2_000_000,
            },
            {
                "source_start_us": 3_000_000,
                "source_end_us": 10_000_000,
                "output_start_us": 2_000_000,
                "output_end_us": 9_000_000,
            },
        ],
        "warnings": [],
        "status": "complete",
    }


def test_revision_join_plan_maps_parent_output_ranges(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "join_rebase_test")
    output = layout.revisions / "rev_002" / "outputs" / "recut.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"revision")
    join_path = layout.artifacts / "join-plan.json"
    media_path = layout.artifacts / "revision-media.json"
    write_validated_artifact(ROOT, "join_plan", join_path, _join_plan(layout.root.name))
    write_validated_artifact(
        ROOT,
        "revision_media_manifest",
        media_path,
        _revision_media(layout.root.name, output),
    )

    result = write_revision_join_plan(ROOT, layout, join_path, media_path, revision_id="rev_002")
    payload = json.loads(result.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "join_plan", payload)
    rebased = payload["joins"][0]
    assert rebased["output_join_us"] == 3_000_000
    assert rebased["preview_range"] == {"start_us": 2_000_000, "end_us": 4_000_000}
    assert payload["output_duration_us"] == 9_000_000
