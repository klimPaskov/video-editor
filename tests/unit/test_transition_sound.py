from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import initialize_project
from videoedit.services.transition_sound import (
    TransitionSoundPolicy,
    align_transition_sound,
    select_transition_sound,
    write_transition_sound_plan,
)

ROOT = Path(__file__).resolve().parents[2]
HASH = "d" * 64


def transition() -> dict[str, object]:
    return {
        "transition_id": "trn_new_point_001",
        "transition_type": "swipe_left",
        "range": {"start_us": 2_400_000, "end_us": 2_700_000},
        "direction": "left",
        "easing": "smooth_ease_in_out",
        "full_frame_coverage": True,
        "incoming_first_readable_frame_us": 2_790_000,
        "reason": "A new point begins.",
        "dialogue_protection": {
            "first_incoming_word_us": 2_790_000,
            "last_outgoing_word_us": 2_310_000,
            "minimum_clearance_us": 90_000,
        },
    }


def catalog() -> dict[str, object]:
    return {
        "catalog_id": "catalog_local",
        "assets": [
            {
                "asset_id": "snd_soft_whoosh",
                "asset_type": "sound_effect",
                "file": {
                    "path": "sound-effects/soft-whoosh.wav",
                    "sha256": HASH,
                    "size_bytes": 144000,
                    "duration_us": 300000,
                },
                "description": "Subtle local whoosh",
                "licence_status": "owned",
                "licence_reference": "license_internal_001",
                "audio_metadata": {
                    "transient_peak_offset_us": 120000,
                    "intended_transition_types": ["swipe_left"],
                    "intensity": "subtle",
                    "speech_safe": True,
                    "minimum_reuse_interval_us": 45_000_000,
                    "brand_contexts": ["demo"],
                },
            },
            {
                "asset_id": "snd_unlicensed",
                "asset_type": "sound_effect",
                "file": {
                    "path": "sound-effects/unlicensed.wav",
                    "sha256": "e" * 64,
                    "size_bytes": 144000,
                    "duration_us": 300000,
                },
                "description": "Unlicensed fixture",
                "licence_status": "licensed",
                "licence_reference": "",
                "audio_metadata": {
                    "transient_peak_offset_us": 120000,
                    "intended_transition_types": ["swipe_left"],
                    "intensity": "strong",
                    "speech_safe": True,
                    "minimum_reuse_interval_us": 45_000_000,
                    "brand_contexts": [],
                },
            },
        ],
    }


def test_select_and_align_transition_sound_at_visual_peak() -> None:
    selection = select_transition_sound(transition(), catalog(), brand_context="demo")
    assert selection.asset_id == "snd_soft_whoosh"
    assert selection.license_id == "license_internal_001"

    cue = align_transition_sound(transition(), selection)
    assert cue["start_us"] == 2_400_000
    assert cue["end_us"] == 2_700_000
    assert cue["sync_peak_us"] == 2_520_000
    assert cue["transient_alignment_status"] == "pass"
    assert cue["speech_protection"]["status"] == "pass"  # type: ignore[index]
    assert cue["qa_status"] == "planned"
    assert cue["approval_state"] == "proposed"


def test_transition_sound_selection_enforces_reuse_and_speech_clearance() -> None:
    with pytest.raises(PlanningValidationError, match="no licensed"):
        select_transition_sound(
            transition(),
            catalog(),
            existing_cues=[{"asset_id": "snd_soft_whoosh", "start_us": 2_400_000}],
        )

    no_clearance = copy.deepcopy(transition())
    no_clearance["dialogue_protection"] = {
        "first_incoming_word_us": 2_600_000,
        "minimum_clearance_us": 90_000,
    }
    selection = select_transition_sound(no_clearance, catalog())
    cue = align_transition_sound(no_clearance, selection)
    assert cue["speech_protection"]["status"] == "fail"  # type: ignore[index]
    assert cue["qa_status"] == "fail"


def test_write_transition_sound_plan_is_schema_valid_and_not_self_approved(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "sound_plan_demo")
    plan = json.loads(
        (ROOT / "examples" / "transition_plan.example.json").read_text(encoding="utf-8")
    )
    plan["project_id"] = layout.root.name
    plan_path = layout.artifacts / "transition-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    catalog_path = tmp_path / "asset-catalog.json"
    catalog_value = json.loads(
        (ROOT / "examples" / "asset_catalog.example.json").read_text(encoding="utf-8")
    )
    catalog_path.write_text(json.dumps(catalog_value), encoding="utf-8")

    output = write_transition_sound_plan(ROOT, layout, plan_path, catalog_path)
    payload = json.loads(output.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "sound_plan", payload)
    assert payload["cues"][0]["approval_state"] == "proposed"
    assert "transition_sound_approval_required" in payload["warnings"]


def test_policy_reads_local_asset_mix_settings() -> None:
    policy = TransitionSoundPolicy.from_yaml(ROOT / "config" / "assets.example.yaml")
    assert policy.default_effect_gain_db == -14
    assert policy.maximum_effect_gain_db == -6
    assert policy.default_minimum_reuse_interval_us == 45_000_000
