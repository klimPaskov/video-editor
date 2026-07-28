from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.planning import (
    EditingPolicy,
    _remove_protected_overrides,
    build_effect_plan,
    materialize_operator_edit_decisions,
    protected_ranges,
)
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.review_batch import write_smart_dense_review_batch


def test_protected_ranges_cover_meaning_bearing_and_uncertain_words() -> None:
    transcript = {
        "confidence_summary": {"speaker_count": 1},
        "words": [
            {
                "word_id": "wrd_000001",
                "text": "not",
                "start_us": 100_000,
                "end_us": 300_000,
                "probability": 1.0,
                "timing_status": "certain",
            },
            {
                "word_id": "wrd_000002",
                "text": "42",
                "start_us": 400_000,
                "end_us": 600_000,
                "probability": 1.0,
                "timing_status": "certain",
            },
            {
                "word_id": "wrd_000003",
                "text": "Buy",
                "start_us": 700_000,
                "end_us": 900_000,
                "probability": 1.0,
                "timing_status": "certain",
            },
            {
                "word_id": "wrd_000004",
                "text": "unclear",
                "start_us": 1_000_000,
                "end_us": 1_200_000,
                "probability": 0.2,
                "timing_status": "uncertain",
            },
        ],
    }

    ranges, reasons = protected_ranges(transcript, EditingPolicy(), 2_000_000)

    assert len(ranges) == 1
    assert ranges[0] == (0, 1_400_000)
    assert {"wrd_000001:negation", "wrd_000002:number_or_figure"}.issubset(reasons)
    assert "wrd_000003:call_to_action" in reasons
    assert "wrd_000004:uncertain_timing" in reasons


def test_explicit_operator_override_subtracts_only_its_requested_range() -> None:
    assert _remove_protected_overrides(
        [(0, 1_000_000), (2_000_000, 3_000_000)],
        [(400_000, 600_000), (2_500_000, 3_500_000)],
    ) == [(0, 400_000), (600_000, 1_000_000), (2_000_000, 2_500_000)]


def test_effect_plan_supports_renderer_contract_and_hash_bound_assets(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    source_manifest_path = tmp_path / "source-manifest.json"
    source_manifest_path.write_text("source", encoding="utf-8")
    asset_path = tmp_path / "broll.png"
    asset_path.write_bytes(b"licensed local asset")
    asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    source_sha256 = "a" * 64
    transcript = {
        "words": [
            {
                "word_id": "wrd_000001",
                "text": "hello",
                "start_us": 100_000,
                "end_us": 300_000,
            }
        ]
    }
    specs = [
        {"id": "fx-caption", "kind": "caption", "renderer": "remotion"},
        {"id": "fx-motion", "kind": "motion_graphic", "renderer": "remotion"},
        {"id": "fx-sound", "kind": "sound_effect", "renderer": "ffmpeg"},
        {
            "id": "fx-broll",
            "kind": "broll",
            "renderer": "remotion",
            "asset_refs": [
                {"asset_id": "asset_broll", "sha256": asset_sha256, "path": str(asset_path)}
            ],
        },
        {"id": "fx-recolor", "kind": "track_recolor", "renderer": "sam3"},
        {"id": "fx-replace", "kind": "track_replace", "renderer": "sam3"},
        {"id": "fx-inpainting", "kind": "inpainting", "renderer": "provider"},
        {"id": "fx-matte", "kind": "person_matte", "renderer": "matanyone2"},
        {"id": "fx-background", "kind": "background_replace", "renderer": "remotion"},
        {
            "id": "fx-behind",
            "kind": "text_between_subject_and_background",
            "renderer": "remotion",
        },
        {"id": "fx-pip", "kind": "picture_in_picture", "renderer": "remotion"},
        {"id": "fx-focus", "kind": "screen_focus", "renderer": "remotion"},
    ]
    specs[0].update(
        {
            "start_us": 100_000,
            "end_us": 300_000,
            "trigger_quote": "hello",
        }
    )
    for spec in specs[1:]:
        spec.update({"start_us": 100_000, "end_us": 300_000})

    payload = build_effect_plan(
        package_root=package_root,
        layout=ProjectLayout(tmp_path),
        source_manifest_path=source_manifest_path,
        source_manifest={"media_duration_us": 1_000_000, "sha256": source_sha256},
        transcript_path=None,
        transcript=transcript,
        policy=EditingPolicy(),
        effect_specs=specs,
        revision_id="rev_001",
    )

    assert len(payload["effects"]) == len(specs)
    assert all(effect["requires_approval"] for effect in payload["effects"])
    assert payload["effects"][0]["word_ids"] == ["wrd_000001"]
    assert payload["effects"][3]["asset_refs"][0]["sha256"] == asset_sha256


def test_effect_plan_rejects_unverifiable_trigger_and_approval_bypass(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    source_manifest_path = tmp_path / "source-manifest.json"
    source_manifest_path.write_text("source", encoding="utf-8")
    layout = ProjectLayout(tmp_path)
    kwargs = {
        "package_root": package_root,
        "layout": layout,
        "source_manifest_path": source_manifest_path,
        "source_manifest": {"media_duration_us": 1_000_000, "sha256": "a" * 64},
        "transcript_path": None,
        "transcript": {
            "words": [
                {
                    "word_id": "wrd_000001",
                    "text": "hello",
                    "start_us": 100_000,
                    "end_us": 300_000,
                }
            ]
        },
        "policy": EditingPolicy(),
        "revision_id": "rev_001",
    }

    with pytest.raises(PlanningValidationError, match="trigger quote"):
        build_effect_plan(
            **kwargs,
            effect_specs=[
                {
                    "kind": "caption",
                    "renderer": "remotion",
                    "start_us": 100_000,
                    "end_us": 300_000,
                    "trigger_quote": "missing",
                }
            ],
        )
    with pytest.raises(PlanningValidationError, match="requires explicit"):
        build_effect_plan(
            **kwargs,
            effect_specs=[
                {
                    "kind": "caption",
                    "renderer": "remotion",
                    "start_us": 100_000,
                    "end_us": 300_000,
                    "requires_approval": False,
                }
            ],
        )
    with pytest.raises(PlanningValidationError, match="unsupported effect kind"):
        build_effect_plan(
            **kwargs,
            effect_specs=[
                {
                    "kind": "unsupported",
                    "renderer": "remotion",
                    "start_us": 100_000,
                    "end_us": 300_000,
                }
            ],
        )
    with pytest.raises(PlanningValidationError, match="outside source bounds"):
        build_effect_plan(
            **kwargs,
            effect_specs=[
                {
                    "kind": "caption",
                    "renderer": "remotion",
                    "start_us": 100_000,
                    "end_us": 1_100_000,
                }
            ],
        )


def test_rejected_candidate_repair_keeps_only_explicit_operator_edits(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = ProjectLayout(tmp_path / "materialize_demo")
    proposals = json.loads(
        (package_root / "examples" / "edit_proposals.example.json").read_text(encoding="utf-8")
    )
    proposals["project_id"] = layout.root.name
    base_path = layout.artifacts / "edit-proposals-base.json"
    write_validated_artifact(package_root, "edit_proposals", base_path, proposals)

    explicit = dict(proposals["proposals"][0])
    explicit["proposal_id"] = "usr_requested"
    explicit["source_range"] = {"start_us": 8_000_000, "end_us": 8_200_000}
    explicit["proposed_cut_range"] = {"start_us": 8_000_000, "end_us": 8_200_000}
    production = dict(proposals)
    production["artifact_id"] = "art_production_proposals"
    production["proposals"] = [*proposals["proposals"], explicit]
    production["total_proposed_cut_us"] = sum(
        int(item["proposed_cut_range"]["end_us"]) - int(item["proposed_cut_range"]["start_us"])
        for item in production["proposals"]
    )
    production["inputs"] = [
        *proposals["inputs"],
        {"artifact_id": "art_parent_proposals", "sha256": sha256_file(base_path)},
    ]
    production_path = layout.artifacts / "edit-proposals-production.json"
    write_validated_artifact(package_root, "edit_proposals", production_path, production)

    instruction_payload = {
        "schema_name": "operator_edit_instructions",
        "schema_version": "1.0.0",
        "artifact_id": "art_operator_instructions",
        "project_id": layout.root.name,
        "revision_id": "rev_001",
        "created_at": "2026-07-22T10:00:00Z",
        "source_sha256": "a" * 64,
        "operator": {"actor": "fixture-reviewer", "role": "editor"},
        "request_text": "Keep one explicit operator edit in the conservative repair.",
        "edits": [
            {
                "edit_id": "usr_requested",
                "label": "Keep explicit repair edit",
                "start_us": 8_000_000,
                "end_us": 8_200_000,
                "reason": "Explicit operator edit for the repair fixture.",
            }
        ],
    }
    instructions_path = layout.review / "operator-edit-instructions.json"
    write_validated_artifact(
        package_root,
        "operator_edit_instructions",
        instructions_path,
        instruction_payload,
    )
    batch_path = write_smart_dense_review_batch(package_root, layout, base_path)

    repair_path = materialize_operator_edit_decisions(
        package_root,
        layout,
        production_path,
        batch_path,
        instructions_path,
        output=layout.review / "edit-decisions-repair.json",
        safe_fallback_only=True,
    )
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "edit_review_decisions", repair)
    repair_by_id = {item["proposal_id"]: item for item in repair["decisions"]}
    assert repair_by_id["usr_requested"]["decision"] == "approve"
    assert all(
        item["decision"] == "reject"
        for item_id, item in repair_by_id.items()
        if item_id != "usr_requested"
    )
    assert "automatic policy cuts are disabled" in repair_by_id["prp_pause_001"]["reason"]

    child_revision_path = materialize_operator_edit_decisions(
        package_root,
        layout,
        production_path,
        batch_path,
        instructions_path,
        revision_id="rev_004",
        safe_fallback_only=True,
    )
    child_revision = json.loads(child_revision_path.read_text(encoding="utf-8"))
    validate_artifact(package_root, "edit_review_decisions", child_revision)
    assert child_revision["revision_id"] == "rev_004"
    assert child_revision["proposal_set_sha256"] == repair["proposal_set_sha256"]

    with pytest.raises(PlanningValidationError, match="invalid decision revision id"):
        materialize_operator_edit_decisions(
            package_root,
            layout,
            production_path,
            batch_path,
            instructions_path,
            revision_id="repair-latest",
            safe_fallback_only=True,
        )

    normal_path = materialize_operator_edit_decisions(
        package_root,
        layout,
        production_path,
        batch_path,
        instructions_path,
        output=layout.review / "edit-decisions-normal.json",
    )
    normal = json.loads(normal_path.read_text(encoding="utf-8"))
    normal_by_id = {item["proposal_id"]: item for item in normal["decisions"]}
    assert normal_by_id["prp_pause_001"]["decision"] == "approve"
