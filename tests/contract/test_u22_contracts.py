from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _schema(name: str) -> dict[str, object]:
    return json.loads(
        (PACKAGE_ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def _example(name: str) -> dict[str, object]:
    return json.loads(
        (PACKAGE_ROOT / "examples" / f"{name}.example.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("example_name", "schema_name"),
    [
        ("edit_proposals", "edit_proposals"),
        ("transition_plan", "transition_plan"),
        ("join_qa_report", "join_qa_report"),
        ("sound_plan", "sound_plan"),
        ("approval_record", "approval_record"),
    ],
)
def test_u22_contract_examples_validate(example_name: str, schema_name: str) -> None:
    Draft202012Validator(_schema(schema_name)).validate(_example(example_name))


def test_cut_taxonomy_and_join_strategies_cover_smart_dense_contract() -> None:
    schema = _schema("edit_proposals")
    proposal = schema["$defs"]["edit_proposal"]  # type: ignore[index]
    properties = proposal["properties"]  # type: ignore[index]
    proposal_types = set(properties["proposal_type"]["enum"])  # type: ignore[index]
    join_strategies = set(properties["join_strategy"]["enum"])  # type: ignore[index]

    assert {
        "filler_word",
        "filler_phrase",
        "stutter",
        "false_start",
        "abandoned_phrase",
        "self_correction",
        "exact_repetition",
        "near_repetition",
        "semantic_repetition",
        "duplicate_take",
        "weak_take",
        "dead_air",
        "accidental_noise",
        "housekeeping",
    } <= proposal_types
    assert {
        "hard_cut",
        "hard_cut_with_micro_audio_crossfade",
        "adjusted_handles",
        "room_tone",
        "j_cut",
        "l_cut",
        "broll_cover",
        "alternate_coverage",
        "purposeful_punch_in",
    } <= join_strategies


def test_motion_transition_requires_sound_and_review_requires_approval() -> None:
    schema = _schema("transition_plan")
    validator = Draft202012Validator(schema)
    transition_plan = _example("transition_plan")

    missing_sound = copy.deepcopy(transition_plan)
    missing_sound["transitions"][0]["sound"] = None  # type: ignore[index]
    with pytest.raises(ValidationError):
        validator.validate(missing_sound)

    missing_approval = copy.deepcopy(transition_plan)
    transition = missing_approval["transitions"][0]  # type: ignore[index]
    transition["policy_result"] = "review_required"  # type: ignore[index]
    transition["approval_required"] = False  # type: ignore[index]
    with pytest.raises(ValidationError):
        validator.validate(missing_approval)


def test_join_and_sound_examples_record_semantic_and_sync_evidence() -> None:
    join = _example("join_qa_report")
    semantic = join["joins"][0]["semantic_check"]  # type: ignore[index]
    assert semantic["protected_content_removed"] is False  # type: ignore[index]
    assert semantic["unintended_meaning_change"] is False  # type: ignore[index]

    sound = _example("sound_plan")
    cue = sound["cues"][0]  # type: ignore[index]
    assert cue["transient_alignment_status"] == "pass"  # type: ignore[index]
    assert cue["speech_protection"]["status"] == "pass"  # type: ignore[index]
    assert cue["reuse_status"] == "pass"  # type: ignore[index]


def test_u22_approval_types_and_configuration_are_explicit() -> None:
    approval_schema = _schema("approval_record")
    approval_types = set(approval_schema["properties"]["approval_type"]["enum"])  # type: ignore[index]
    assert {"smart_dense_policy", "transition", "transition_batch"} <= approval_types

    policy = yaml.safe_load(
        (PACKAGE_ROOT / "config" / "editing-policy.example.yaml").read_text(encoding="utf-8")
    )
    assert policy["policy"]["id"] == "smart_dense"
    taxonomy = policy["cut_taxonomy"]
    assert taxonomy["version"] == 1
    assert "filler_word" in taxonomy["mechanical"]
    assert "semantic_repetition" in taxonomy["semantic"]
    assert "numbers" in taxonomy["protected_content_requires_review"]

    transitions = yaml.safe_load(
        (PACKAGE_ROOT / "config" / "transitions.example.yaml").read_text(encoding="utf-8")
    )
    assert transitions["transition_policy"]["default_fallback"] == "hard_cut"
    assert transitions["styles"]["swipe_left"]["sound"] == "required"
    assert transitions["sound"]["speech_priority"] is True
