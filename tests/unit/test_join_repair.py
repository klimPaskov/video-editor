from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import config_sha256, write_validated_artifact
from videoedit.services.join_repair import (
    plan_applied_joins,
    repair_join_plan,
    write_retimed_join_plan,
)
from videoedit.services.project import initialize_project


def _proposal(
    proposal_id: str,
    start_us: int,
    end_us: int,
    *,
    strategy: str = "hard_cut_with_micro_audio_crossfade",
    policy_result: str = "auto_eligible",
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "proposal_type": "filler_word",
        "proposed_cut_range": {"start_us": start_us, "end_us": end_us},
        "join_strategy": strategy,
        "policy_result": policy_result,
        "reason": "Fixture micro edit",
        "handles": {"pre_handle_us": 80_000, "post_handle_us": 110_000},
    }


def test_every_applied_cut_gets_a_bounded_strategy_and_output_join() -> None:
    joins = plan_applied_joins(
        [
            _proposal("prp_a", 2_000_000, 2_500_000),
            _proposal("prp_b", 3_000_000, 3_200_000, strategy="adjusted_handles"),
        ],
        [
            {"proposal_id": "prp_a", "decision": "approve"},
            {
                "proposal_id": "prp_b",
                "decision": "modify",
                "modified_cut_range": {"start_us": 3_100_000, "end_us": 3_400_000},
            },
            {"proposal_id": "prp_ignored", "decision": "reject"},
        ],
        [
            {
                "segment_id": "keep_a",
                "source_start_us": 0,
                "source_end_us": 2_000_000,
                "output_start_us": 0,
                "output_end_us": 2_000_000,
            },
            {
                "segment_id": "keep_b",
                "source_start_us": 2_500_000,
                "source_end_us": 3_100_000,
                "output_start_us": 2_000_000,
                "output_end_us": 2_600_000,
            },
            {
                "segment_id": "keep_c",
                "source_start_us": 3_400_000,
                "source_end_us": 5_000_000,
                "output_start_us": 2_600_000,
                "output_end_us": 4_200_000,
            },
        ],
        4_200_000,
    )

    assert [item["join_id"] for item in joins] == ["join_000001", "join_000002"]
    assert joins[0]["output_join_us"] == 2_000_000
    assert joins[1]["source_cut_range"] == {"start_us": 3_100_000, "end_us": 3_400_000}
    assert joins[1]["output_join_us"] == 2_600_000
    assert joins[0]["repair_order"][-1] == "hard_cut"
    assert joins[0]["preview_range"] == {"start_us": 0, "end_us": 4_000_000}


def test_join_repair_advances_deterministically_and_requires_review() -> None:
    join = plan_applied_joins(
        [_proposal("prp_a", 2_000_000, 2_500_000)],
        [{"proposal_id": "prp_a", "decision": "approve"}],
        [
            {
                "source_start_us": 0,
                "source_end_us": 2_000_000,
                "output_start_us": 0,
                "output_end_us": 2_000_000,
            },
            {
                "source_start_us": 2_500_000,
                "source_end_us": 5_000_000,
                "output_start_us": 2_000_000,
                "output_end_us": 4_500_000,
            },
        ],
        4_500_000,
    )[0]

    repaired = repair_join_plan(join, ["click_or_pop"])
    assert repaired["join_strategy"] == "adjusted_handles"
    assert repaired["status"] == "repair_required"
    assert repaired["review_required"] is True
    assert repaired["repair_attempt"] == 1

    room_tone = repair_join_plan(repaired, ["room_tone_jump"])
    assert room_tone["join_strategy"] == "room_tone"
    fallback = repair_join_plan(room_tone, ["missing_word"])
    assert fallback["join_strategy"] == "hard_cut"
    assert fallback["status"] == "fallback_required"


def test_blocked_proposals_cannot_enter_an_applied_join_plan() -> None:
    with pytest.raises(PlanningValidationError, match="blocked"):
        plan_applied_joins(
            [_proposal("prp_blocked", 2_000_000, 2_500_000, policy_result="blocked")],
            [{"proposal_id": "prp_blocked", "decision": "approve"}],
            [
                {
                    "source_start_us": 0,
                    "source_end_us": 2_000_000,
                    "output_start_us": 0,
                    "output_end_us": 2_000_000,
                },
                {
                    "source_start_us": 2_500_000,
                    "source_end_us": 5_000_000,
                    "output_start_us": 2_000_000,
                    "output_end_us": 4_500_000,
                },
            ],
            4_500_000,
        )


def test_retimed_join_plan_rebases_output_boundary_and_preview(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "join_rebase_demo")
    join_plan = json.loads(
        (package_root / "examples" / "join_plan.example.json").read_text(encoding="utf-8")
    )
    join_plan.update(
        {
            "project_id": layout.root.name,
            "config_sha256": config_sha256(layout),
            "output_duration_us": 10_000_000,
        }
    )
    join_plan["joins"][0].update(
        {
            "source_cut_range": {"start_us": 6_000_000, "end_us": 6_500_000},
            "output_join_us": 6_000_000,
            "preview_range": {"start_us": 4_000_000, "end_us": 8_000_000},
        }
    )
    join_plan_path = layout.artifacts / "join-plan.json"
    write_validated_artifact(package_root, "join_plan", join_plan_path, join_plan)

    timeline = json.loads(
        (package_root / "examples" / "retimed_timeline.example.json").read_text(encoding="utf-8")
    )
    timeline.update(
        {
            "project_id": layout.root.name,
            "source_duration_us": 10_000_000,
            "output_duration_us": 8_500_000,
        }
    )
    timeline["segments"] = [
        {
            "segment_id": "retime_keep_001",
            "operation": "keep",
            "source_range": {"start_us": 0, "end_us": 4_000_000},
            "output_range": {"start_us": 0, "end_us": 4_000_000},
            "playback_rate": 1,
            "audio_mode": "source",
            "speedup_id": None,
            "command_strategy": "passthrough",
            "boundary_confidence": None,
        },
        {
            "segment_id": "retime_speed_001",
            "operation": "prompt_speedup",
            "source_range": {"start_us": 4_000_000, "end_us": 6_000_000},
            "output_range": {"start_us": 4_000_000, "end_us": 5_000_000},
            "playback_rate": 2,
            "audio_mode": "audible_pitch_preserved",
            "speedup_id": "speed_prompt_write",
            "command_strategy": "ffmpeg_trim_setpts_atempo",
            "boundary_confidence": 0.98,
        },
        {
            "segment_id": "retime_keep_002",
            "operation": "keep",
            "source_range": {"start_us": 6_500_000, "end_us": 10_000_000},
            "output_range": {"start_us": 5_000_000, "end_us": 8_500_000},
            "playback_rate": 1,
            "audio_mode": "source",
            "speedup_id": None,
            "command_strategy": "passthrough",
            "boundary_confidence": None,
        },
    ]
    timeline_path = layout.artifacts / "retimed-timeline.json"
    write_validated_artifact(package_root, "retimed_timeline", timeline_path, timeline)

    output = write_retimed_join_plan(
        package_root,
        layout,
        join_plan_path,
        timeline_path,
    )
    rebased = json.loads(output.read_text(encoding="utf-8"))
    assert rebased["output_duration_us"] == 8_500_000
    assert rebased["joins"][0]["output_join_us"] == 5_000_000
    assert rebased["joins"][0]["preview_range"] == {
        "start_us": 3_000_000,
        "end_us": 7_000_000,
    }
    assert rebased["joins"][0]["source_preview_ranges"] == [
        {"start_us": 3_000_000, "end_us": 6_000_000},
        {"start_us": 6_500_000, "end_us": 8_500_000},
    ]
    assert [item["artifact_id"] for item in rebased["inputs"]] == [
        "art_join_plan",
        "art_retimed_timeline",
    ]
