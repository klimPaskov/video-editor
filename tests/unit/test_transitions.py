from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import initialize_project
from videoedit.services.transitions import (
    TransitionPolicy,
    detect_structural_boundaries,
    plan_transitions,
    write_transition_plan,
)

ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


def _policy() -> TransitionPolicy:
    return TransitionPolicy.from_yaml(ROOT / "config" / "transitions.example.yaml")


def _segments() -> list[dict[str, object]]:
    return [
        {
            "segment_id": "seg_intro",
            "start_us": 0,
            "end_us": 2_400_000,
            "text": "We set up the idea.",
            "visual_mode": "demo",
        },
        {
            "segment_id": "seg_demo",
            "start_us": 2_400_000,
            "end_us": 6_000_000,
            "text": "Now let's demonstrate the result.",
            "visual_mode": "screen_recording",
        },
    ]


def _sound_cue() -> dict[str, object]:
    return {
        "cue_id": "sfx_001",
        "start_us": 2_400_000,
        "end_us": 2_700_000,
        "asset_id": "snd_soft_whoosh",
        "asset_sha256": HASH,
        "license_id": "license_internal_001",
        "purpose": "Support a key point card entrance",
        "gain_db": -16.0,
        "fade_in_us": 15_000,
        "fade_out_us": 70_000,
        "duck_speech": True,
        "approval_state": "approved",
        "linked_transition_id": "trn_bnd_000001",
        "sync_peak_us": 2_520_000,
        "speech_clearance_us": 90_000,
        "transient_alignment_status": "pass",
        "transient_alignment_tolerance_us": 30_000,
        "speech_protection": {
            "minimum_clearance_us": 90_000,
            "first_important_word_us": 2_790_000,
            "status": "pass",
        },
        "qa_status": "pass",
    }


def test_detector_requires_evidence_and_classifies_major_mode_change() -> None:
    transcript = {
        "words": [
            {
                "segment_id": "seg_intro",
                "start_us": 1_800_000,
                "end_us": 2_310_000,
            },
            {
                "segment_id": "seg_demo",
                "start_us": 2_790_000,
                "end_us": 3_200_000,
            },
        ]
    }

    boundaries = detect_structural_boundaries(_segments(), transcript, policy=_policy())

    assert len(boundaries) == 1
    assert boundaries[0]["purpose"] == "mode_change"
    assert boundaries[0]["boundary_us"] == 2_400_000
    assert boundaries[0]["full_frame_coverage"] is True
    assert boundaries[0]["status"] == "review_required"


def test_verified_boundary_with_approved_sound_gets_purpose_bound_motion() -> None:
    boundary = {
        "boundary_id": "bnd_000001",
        "boundary_us": 2_400_000,
        "outgoing_segment_id": "seg_intro",
        "incoming_segment_id": "seg_demo",
        "purpose": "new_point",
        "transcript_evidence": "The speaker introduces the first major point.",
        "visual_evidence": "The incoming full-frame demo is approved.",
        "evidence_ids": ["ev_boundary_001"],
        "first_incoming_word_us": 2_790_000,
        "last_outgoing_word_us": 2_310_000,
        "preferred_transition_type": "swipe_left",
        "direction": "left",
        "desired_duration_us": 300_000,
        "easing": "smooth_ease_in_out",
        "full_frame_coverage": True,
        "confidence_components": {
            "structural_boundary": 0.98,
            "timing": 0.97,
            "visual_fit": 0.96,
            "dialogue_safety": 0.99,
            "sound_sync": 0.97,
        },
        "confidence": 0.96,
        "explicit": True,
        "status": "verified",
    }

    transitions = plan_transitions(
        [boundary],
        policy=_policy(),
        sound_cues=[_sound_cue()],
        output_duration_us=6_000_000,
    )

    assert transitions[0]["transition_type"] == "swipe_left"
    assert transitions[0]["range"] == {"start_us": 2_400_000, "end_us": 2_700_000}
    assert transitions[0]["sound"] is not None
    assert transitions[0]["sound_sync_status"] == "pass"
    assert transitions[0]["dialogue_protection"]["speech_overlaps"] is False  # type: ignore[index]
    assert transitions[0]["approval_required"] is True


def test_missing_sound_or_weak_boundary_falls_back_to_hard_cut() -> None:
    boundary = {
        "boundary_id": "bnd_weak_001",
        "boundary_us": 2_400_000,
        "outgoing_segment_id": "seg_intro",
        "incoming_segment_id": "seg_demo",
        "purpose": "new_point",
        "transcript_evidence": "A weakly inferred point marker.",
        "visual_evidence": "No verified visual state change.",
        "evidence_ids": ["ev_boundary_weak"],
        "first_incoming_word_us": 2_790_000,
        "last_outgoing_word_us": 2_310_000,
        "preferred_transition_type": "swipe_left",
        "direction": "left",
        "desired_duration_us": 300_000,
        "easing": "smooth_ease_in_out",
        "full_frame_coverage": False,
        "confidence_components": {
            "structural_boundary": 0.7,
            "timing": 0.7,
            "visual_fit": 0.5,
            "dialogue_safety": 0.9,
            "sound_sync": 0.2,
        },
        "confidence": 0.7,
        "explicit": False,
        "status": "review_required",
    }

    transitions = plan_transitions(
        [boundary],
        policy=_policy(),
        output_duration_us=6_000_000,
    )

    assert transitions[0]["transition_type"] == "hard_cut"
    assert transitions[0]["range"] == {"start_us": 2_400_000, "end_us": 2_400_000}
    assert transitions[0]["sound"] is None
    assert transitions[0]["fallback"] == "hard_cut"


def test_transition_plan_writer_binds_boundary_and_sound_inputs(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "transition_plan_demo")
    plan_path = write_transition_plan(
        ROOT,
        layout,
        ROOT / "examples" / "transcript.example.json",
        boundaries_path=ROOT / "examples" / "structural_boundaries.example.json",
        sound_plan_path=ROOT / "examples" / "sound_plan.example.json",
        output_duration_us=12_000_000,
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "transition_plan", plan)
    assert plan["transitions"][0]["transition_type"] == "swipe_left"
    assert {item["artifact_id"] for item in plan["inputs"]} >= {
        "art_transcript",
        "art_structural_boundaries",
        "art_sound_plan",
        "art_transition_policy",
    }
