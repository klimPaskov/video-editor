from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoedit.services.takes import TakeRankingPolicy, rank_take_candidates


def _candidate(take_id: str, score: float, start_us: int) -> dict[str, object]:
    return {
        "take_id": take_id,
        "source_range": {"start_us": start_us, "end_us": start_us + 1_000_000},
        "components": {
            "completeness": score,
            "pronunciation": score,
            "factual_correctness": score,
            "delivery": score,
            "audio_quality": score,
            "gesture_continuity": score,
            "screen_state_continuity": score,
        },
        "evidence_ids": [f"ev_{take_id}"],
    }


def test_take_ranking_uses_all_quality_dimensions_and_returns_reviewable_recommendation() -> None:
    result = rank_take_candidates(
        [
            _candidate("take_b", 0.8, 2_000_000),
            _candidate("take_a", 0.99, 1_000_000),
        ]
    )

    assert result["recommended_take_id"] == "take_a"
    assert result["requires_review"] is False
    ranked = result["ranked_takes"]
    assert ranked[0]["rank"] == 1
    assert ranked[0]["recommendation"] == "recommended"
    assert ranked[0]["overall_score"] == 0.99
    assert set(ranked[0]["components"]) == {
        "completeness",
        "pronunciation",
        "factual_correctness",
        "delivery",
        "audio_quality",
        "gesture_continuity",
        "screen_state_continuity",
    }


def test_close_take_scores_are_batched_for_review_not_auto_selected() -> None:
    policy = TakeRankingPolicy(auto_selection_margin=0.18)
    result = rank_take_candidates(
        [_candidate("take_a", 0.9, 1_000_000), _candidate("take_b", 0.85, 2_000_000)],
        policy=policy,
    )

    assert result["recommended_take_id"] is None
    assert result["requires_review"] is True
    assert result["warnings"] == ["take_scores_are_too_close_for_auto_selection"]
    assert {item["recommendation"] for item in result["ranked_takes"]} == {"review_required"}


def test_take_ranking_rejects_duplicate_ids_and_incomplete_evidence() -> None:
    candidate = _candidate("take_a", 0.9, 1_000_000)
    with pytest.raises(ValueError, match="unique"):
        rank_take_candidates([candidate, candidate])

    incomplete = _candidate("take_a", 0.9, 1_000_000)
    del incomplete["components"]["factual_correctness"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        rank_take_candidates([incomplete])
