from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from videoedit.services.join_qa import evaluate_join_qa
from videoedit.services.planning import EditingPolicy, build_edit_proposals
from videoedit.services.project import ProjectLayout
from videoedit.services.takes import rank_take_candidates
from videoedit.services.transitions import TransitionPolicy, plan_transitions

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "u22_required_cases.json"


def _catalog() -> dict[str, Any]:
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    assert isinstance(value, list)
    return [_mapping(item) for item in value]


def test_u22_fixture_catalog_is_versioned_and_names_required_cases() -> None:
    catalog = _catalog()

    assert catalog["fixture_catalog_id"] == "u22_required_cases"
    assert catalog["fixture_catalog_version"] == "1.0.0"
    assert _mapping(catalog["provenance"])["media_required"] is True

    dense = _mapping(catalog["dense_edit"])
    assert {
        "filler_word",
        "filler_phrase",
        "stutter",
        "false_start",
        "exact_repetition",
        "semantic_repetition",
        "duplicate_take",
    } <= set(cast(list[str], dense["expected_proposal_types"]))

    join_case_ids = {str(_mapping(item)["case_id"]) for item in _list_of_mappings(catalog["joins"])}
    assert {
        "clean_join",
        "broken_join_duplicate_word",
        "screen_state_understandable",
        "screen_state_unexplained",
    } <= join_case_ids

    transition_case_ids = {
        str(_mapping(item)["case_id"]) for item in _list_of_mappings(catalog["transitions"])
    }
    assert {
        "good_structural_transition",
        "bad_random_transition",
        "masked_dialogue",
    } <= transition_case_ids


def test_dense_fixture_emits_named_candidates_and_blocks_protected_meaning(
    tmp_path: Path,
) -> None:
    dense = _mapping(_catalog()["dense_edit"])
    transcript = _mapping(dense["transcript"])
    silence = _mapping(dense["silence"])
    duration_us = int(dense["source_duration_us"])
    transcript_path = tmp_path / "transcript.json"
    silence_path = tmp_path / "silence.json"
    source_manifest_path = tmp_path / "source-manifest.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    silence_path.write_text(json.dumps(silence), encoding="utf-8")
    source_manifest_path.write_text("{}", encoding="utf-8")

    payload = build_edit_proposals(
        package_root=PACKAGE_ROOT,
        layout=ProjectLayout(tmp_path),
        source_manifest_path=source_manifest_path,
        source_manifest={"media_duration_us": duration_us},
        transcript_path=transcript_path,
        transcript=transcript,
        silence_path=silence_path,
        silence=silence,
        policy=EditingPolicy.smart_dense(),
        revision_id="rev_u22_fixture",
    )

    proposals = _list_of_mappings(payload["proposals"])
    expected_types = set(cast(list[str], dense["expected_proposal_types"]))
    assert expected_types <= {str(item["proposal_type"]) for item in proposals}

    protected_ids = set(cast(list[str], dense["protected_word_ids"]))
    protected_proposals = [
        item for item in proposals if protected_ids & set(cast(list[str], item.get("word_ids", [])))
    ]
    blocked_proposals = [
        item
        for item in protected_proposals
        if _mapping(item["protected_content_check"])["passed"] is False
    ]
    assert blocked_proposals
    assert all(item["policy_result"] == "blocked" for item in blocked_proposals)
    assert all(
        item["join_strategy"] == "hard_cut_with_micro_audio_crossfade"
        and item["join_preview_required"] is True
        for item in proposals
    )


def test_duplicate_take_fixture_requires_review() -> None:
    selection = _mapping(_catalog()["take_selection"])
    result = rank_take_candidates(_list_of_mappings(selection["candidates"]))

    assert result["recommended_take_id"] == selection["expected_recommendation"]
    assert result["requires_review"] is selection["expected_requires_review"]
    assert result["warnings"] == ["take_scores_are_too_close_for_auto_selection"]


def _join_for_case(case_id: str) -> dict[str, object]:
    return {
        "join_id": f"join_{case_id}",
        "proposal_ids": ["prp_u22_fixture"],
        "source_cut_range": {"start_us": 5_900_000, "end_us": 6_100_000},
        "output_join_us": 6_000_000,
        "preview_range": {"start_us": 4_000_000, "end_us": 8_000_000},
        "join_strategy": "hard_cut_with_micro_audio_crossfade",
        "repair_order": [
            "hard_cut_with_micro_audio_crossfade",
            "adjusted_handles",
            "room_tone",
            "hard_cut",
        ],
        "fallback": "hard_cut",
        "handles": {"pre_handle_us": 80_000, "post_handle_us": 110_000},
        "reason": "U22 fixture join case.",
        "repair_attempt": 0,
        "review_required": False,
    }


def _checked_evidence(value: object, *, failure: bool) -> dict[str, object]:
    evidence = dict(_mapping(value))
    evidence["status"] = "fail" if failure else "pass"
    return evidence


@pytest.mark.parametrize(
    "case_id",
    [
        "clean_join",
        "broken_join_duplicate_word",
        "screen_state_understandable",
        "screen_state_unexplained",
    ],
)
def test_join_fixture_matrix_routes_clean_broken_and_screen_state_cases(case_id: str) -> None:
    cases = {
        str(_mapping(item)["case_id"]): _mapping(item)
        for item in _list_of_mappings(_catalog()["joins"])
    }
    case = cases[case_id]
    visual = _mapping(case["visual_check"])
    audio = _mapping(case["audio_check"])
    audio_failure = any(
        bool(audio.get(key)) for key in ("clipped_syllable", "click_or_pop", "room_tone_jump")
    )
    visual_failure = (
        bool(visual.get("black_flash"))
        or bool(visual.get("freeze"))
        or bool(visual.get("duplicate_frame"))
        or visual.get("face_or_body_jump") == "distracting"
        or visual.get("screen_state_jump") == "unexplained"
    )
    result = evaluate_join_qa(
        _join_for_case(case_id),
        [_join_for_case(case_id)],
        output_duration_us=12_000_000,
        approved_text=str(case["approved_text"]),
        rendered_text=str(case["rendered_text"]),
        audio_check=_checked_evidence(audio, failure=audio_failure),
        visual_check=_checked_evidence(visual, failure=visual_failure),
    )

    assert result["status"] == case["expected_status"]
    expected_repair = case.get("expected_repair_action_contains")
    if expected_repair is not None:
        assert str(expected_repair) in str(result["repair_action"])
    else:
        assert result["repair_action"] is None


@pytest.mark.parametrize(
    "case_id",
    ["good_structural_transition", "bad_random_transition", "masked_dialogue"],
)
def test_transition_fixture_matrix_allows_good_motion_and_rejects_unsafe_motion(
    case_id: str,
) -> None:
    cases = {
        str(_mapping(item)["case_id"]): _mapping(item)
        for item in _list_of_mappings(_catalog()["transitions"])
    }
    case = cases[case_id]
    policy = TransitionPolicy.from_yaml(PACKAGE_ROOT / "config" / "transitions.example.yaml")
    transitions = plan_transitions(
        [_mapping(case["boundary"])],
        policy=policy,
        sound_cues=_list_of_mappings(case["sound_cues"]),
        output_duration_us=12_000_000,
    )

    assert len(transitions) == 1
    transition = transitions[0]
    assert transition["transition_type"] == case["expected_transition_type"]
    assert transition["sound_sync_status"] == case["expected_sound_sync_status"]
    if case_id != "good_structural_transition":
        assert transition["transition_type"] == "hard_cut"
        assert "Clean-cut fallback" in str(transition["reason"])
    if case_id == "masked_dialogue":
        dialogue = _mapping(transition["dialogue_protection"])
        assert dialogue["speech_overlaps"] is True
        assert dialogue["intelligibility_risk"] == "high"
