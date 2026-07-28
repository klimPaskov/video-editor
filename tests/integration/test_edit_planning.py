from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.transcription import FixtureTranscriptionAdapter
from videoedit.errors import PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import config_sha256, validate_artifact, write_validated_artifact
from videoedit.services.editing import (
    compile_approved_edl,
    create_gate1_approval,
    import_edit_decisions,
    plan_review_package,
    validate_gate1_approval,
)
from videoedit.services.focus_pacing import build_focus_pacing_plan, write_focus_pacing_plan
from videoedit.services.join_repair import write_join_plan
from videoedit.services.media import ingest_and_probe
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.silence import detect_project_silence
from videoedit.services.transcription import transcribe_project


@pytest.mark.integration
def test_edit_planning_review_gate_and_mapping_are_hash_bound(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "p3_planning_project")
    source = tmp_path / "recording.mp4"
    ffmpeg = FFmpegAdapter()
    ffmpeg.generate_edit_demo_source(source)
    ingest_and_probe(package_root, layout, source, adapter=ffmpeg)
    transcribe_project(
        package_root,
        layout,
        "fixture",
        adapter=FixtureTranscriptionAdapter(
            {
                "language": "en",
                "text": "before go go buy after",
                "segments": [
                    {
                        "start": 0.5,
                        "end": 1.2,
                        "text": "before",
                        "words": [{"word": "before", "start": 0.5, "end": 1.2, "probability": 1.0}],
                    },
                    {
                        "start": 4.5,
                        "end": 5.6,
                        "text": "go go buy",
                        "words": [
                            {"word": "go", "start": 4.5, "end": 4.7, "probability": 1.0},
                            {"word": "go", "start": 4.8, "end": 5.0, "probability": 1.0},
                            {"word": "buy", "start": 5.1, "end": 5.4, "probability": 1.0},
                        ],
                    },
                ],
            }
        ),
    )
    detect_project_silence(
        package_root,
        layout,
        threshold_db=-45,
        minimum_duration_us=500_000,
        adapter=ffmpeg,
    )

    effect_specs = [
        {
            "id": "fx-buy-caption",
            "kind": "caption",
            "start_us": 5_100_000,
            "end_us": 5_400_000,
            "trigger_quote": "buy",
            "renderer": "remotion",
        }
    ]
    outputs = plan_review_package(
        package_root,
        layout,
        effect_specs=effect_specs,
    )
    cached = plan_review_package(
        package_root,
        layout,
        effect_specs=effect_specs,
    )
    assert cached == outputs

    proposals = json.loads(outputs.proposals_path.read_text(encoding="utf-8"))
    effect_plan = json.loads(outputs.effect_plan_path.read_text(encoding="utf-8"))
    template = json.loads(outputs.decision_template_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "edit_proposals", proposals)
    validate_artifact(package_root, "effect_plan", effect_plan)
    validate_artifact(package_root, "edit_review_decisions", template)
    assert {item["proposal_type"] for item in proposals["proposals"]} >= {
        "dead_air",
        "immediate_repetition",
    }
    assert effect_plan["effects"][0]["word_ids"] == ["wrd_000004"]
    assert all(item["approval_required"] for item in proposals["proposals"])

    reviewed = dict(template)
    reviewed["reviewer"] = {"actor": "fixture-reviewer", "role": "editor"}
    for decision, proposal in zip(reviewed["decisions"], proposals["proposals"], strict=True):
        decision["decision"] = (
            "approve" if proposal["proposal_type"] in {"long_pause", "dead_air"} else "reject"
        )
        decision["reason"] = "Reviewed against the fixture evidence"
    reviewed_path = layout.review / "fixture-reviewed-decisions.json"
    reviewed_path.write_text(json.dumps(reviewed, indent=2), encoding="utf-8")
    imported_path = import_edit_decisions(package_root, layout, reviewed_path)
    focus_plan = build_focus_pacing_plan(
        package_root=package_root,
        project_id=layout.root.name,
        revision_id="rev_001",
        inputs=[
            {
                "artifact_id": "art_source",
                "sha256": sha256_file(layout.artifacts / "source-manifest.json"),
            }
        ],
        config_hash=config_sha256(layout),
    )
    focus_plan_path = write_focus_pacing_plan(package_root, layout, focus_plan)
    approval_path = create_gate1_approval(
        package_root,
        layout,
        reviewed_path,
        outputs.effect_plan_path,
        actor="fixture-reviewer",
        focus_pacing_plan_path=focus_plan_path,
    )
    validate_gate1_approval(
        package_root,
        layout,
        approval_path,
        imported_path,
        outputs.effect_plan_path,
        focus_pacing_plan_path=focus_plan_path,
    )
    edl_path = compile_approved_edl(
        package_root,
        layout,
        reviewed_path,
        approval_path,
        focus_pacing_plan_path=focus_plan_path,
    )
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "edit_decision_list", edl)
    assert edl["expected_output_duration_us"] < edl["source_duration_us"]
    assert edl["source_to_output_mapping"] == [
        {
            "source_start_us": item["source_start_us"],
            "source_end_us": item["source_end_us"],
            "output_start_us": item["output_start_us"],
            "output_end_us": item["output_end_us"],
        }
        for item in edl["keep_ranges"]
    ]
    join_plan_path = write_join_plan(
        package_root,
        layout,
        outputs.proposals_path,
        imported_path,
        edl_path,
    )
    join_plan = json.loads(join_plan_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "join_plan", join_plan)
    assert len(join_plan["joins"]) == 1
    assert join_plan["joins"][0]["join_strategy"] == "hard_cut_with_micro_audio_crossfade"

    stale_effect = dict(effect_plan)
    stale_effect["notes"] = ["changed after Gate 1"]
    stale_effect_path = layout.artifacts / "effect-plan-stale.json"
    write_validated_artifact(package_root, "effect_plan", stale_effect_path, stale_effect)
    with pytest.raises(StaleApprovalError, match="stale"):
        validate_gate1_approval(
            package_root,
            layout,
            approval_path,
            imported_path,
            stale_effect_path,
        )

    incomplete = dict(reviewed)
    incomplete["decisions"] = incomplete["decisions"][:-1]
    incomplete_path = layout.review / "incomplete-decisions.json"
    incomplete_path.write_text(json.dumps(incomplete, indent=2), encoding="utf-8")
    with pytest.raises(PlanningValidationError, match="missing decisions"):
        import_edit_decisions(package_root, layout, incomplete_path)

    overlapping = dict(reviewed)
    overlapping["decisions"] = [dict(item) for item in reviewed["decisions"]]
    for decision, proposal in zip(overlapping["decisions"], proposals["proposals"], strict=True):
        if proposal["proposal_type"] == "immediate_repetition":
            decision["decision"] = "modify"
            decision["modified_cut_range"] = {"start_us": 2_500_000, "end_us": 3_000_000}
            decision["reason"] = "Fixture overlap rejection"
    overlapping_path = layout.review / "overlapping-decisions.json"
    overlapping_path.write_text(json.dumps(overlapping, indent=2), encoding="utf-8")
    with pytest.raises(PlanningValidationError, match="overlap"):
        create_gate1_approval(
            package_root,
            layout,
            overlapping_path,
            outputs.effect_plan_path,
            actor="fixture-reviewer",
        )

    out_of_bounds = dict(reviewed)
    out_of_bounds["decisions"] = [dict(item) for item in reviewed["decisions"]]
    for decision, proposal in zip(out_of_bounds["decisions"], proposals["proposals"], strict=True):
        if proposal["proposal_type"] in {"long_pause", "dead_air"}:
            decision["decision"] = "modify"
            decision["modified_cut_range"] = {
                "start_us": 2_000_000,
                "end_us": proposals["source_duration_us"] + 1,
            }
            decision["reason"] = "Fixture bounds rejection"
    out_of_bounds_path = layout.review / "out-of-bounds-decisions.json"
    out_of_bounds_path.write_text(json.dumps(out_of_bounds, indent=2), encoding="utf-8")
    with pytest.raises(PlanningValidationError, match="outside source bounds"):
        import_edit_decisions(package_root, layout, out_of_bounds_path)

    state = json.loads(
        (layout.state / "stages" / "edit_plan-rev_001.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "complete"
    assert state["attempt"] == 1
