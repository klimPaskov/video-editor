from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, model_validator

from videoedit.domain.models import StrictModel


class TakeRange(StrictModel):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> TakeRange:
        if self.end_us <= self.start_us:
            raise ValueError("take range end_us must be greater than start_us")
        return self


class TakeScoreComponents(StrictModel):
    completeness: float = Field(ge=0, le=1)
    pronunciation: float = Field(ge=0, le=1)
    factual_correctness: float = Field(ge=0, le=1)
    delivery: float = Field(ge=0, le=1)
    audio_quality: float = Field(ge=0, le=1)
    gesture_continuity: float = Field(ge=0, le=1)
    screen_state_continuity: float = Field(ge=0, le=1)


class TakeCandidate(StrictModel):
    take_id: str = Field(min_length=1)
    source_range: TakeRange
    components: TakeScoreComponents
    evidence_ids: list[str] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)


class TakeRankingPolicy(StrictModel):
    policy_id: str = "take_quality_v1"
    completeness_weight: float = Field(default=0.18, ge=0, le=1)
    pronunciation_weight: float = Field(default=0.15, ge=0, le=1)
    factual_correctness_weight: float = Field(default=0.20, ge=0, le=1)
    delivery_weight: float = Field(default=0.15, ge=0, le=1)
    audio_quality_weight: float = Field(default=0.12, ge=0, le=1)
    gesture_continuity_weight: float = Field(default=0.10, ge=0, le=1)
    screen_state_continuity_weight: float = Field(default=0.10, ge=0, le=1)
    auto_selection_margin: float = Field(default=0.18, ge=0, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> TakeRankingPolicy:
        total = sum(
            (
                self.completeness_weight,
                self.pronunciation_weight,
                self.factual_correctness_weight,
                self.delivery_weight,
                self.audio_quality_weight,
                self.gesture_continuity_weight,
                self.screen_state_continuity_weight,
            )
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError("take ranking weights must sum to 1")
        return self

    def score(self, components: TakeScoreComponents) -> float:
        value = (
            components.completeness * self.completeness_weight
            + components.pronunciation * self.pronunciation_weight
            + components.factual_correctness * self.factual_correctness_weight
            + components.delivery * self.delivery_weight
            + components.audio_quality * self.audio_quality_weight
            + components.gesture_continuity * self.gesture_continuity_weight
            + components.screen_state_continuity * self.screen_state_continuity_weight
        )
        return round(value, 6)


class RankedTake(StrictModel):
    take_id: str
    source_range: TakeRange
    components: TakeScoreComponents
    overall_score: float = Field(ge=0, le=1)
    rank: int = Field(gt=0)
    evidence_ids: list[str] = Field(min_length=1)
    notes: list[str] = Field(default_factory=list)
    recommendation: Literal["recommended", "alternative", "review_required"]


def rank_take_candidates(
    candidates: Sequence[TakeCandidate | Mapping[str, object]],
    *,
    policy: TakeRankingPolicy | None = None,
) -> dict[str, object]:
    """Rank explicit take evidence; never converts a ranking into approval."""

    selected_policy = policy or TakeRankingPolicy()
    normalized = [
        candidate
        if isinstance(candidate, TakeCandidate)
        else TakeCandidate.model_validate(candidate)
        for candidate in candidates
    ]
    if not normalized:
        raise ValueError("at least one take candidate is required")
    take_ids = [candidate.take_id for candidate in normalized]
    if len(set(take_ids)) != len(take_ids):
        raise ValueError("take IDs must be unique within a ranking batch")

    ranked_values = [
        (candidate, selected_policy.score(candidate.components)) for candidate in normalized
    ]
    ranked_values.sort(key=lambda item: (-item[1], item[0].take_id))
    top_score = ranked_values[0][1]
    second_score = ranked_values[1][1] if len(ranked_values) > 1 else 0.0
    margin = round(top_score - second_score, 6)
    clear_winner = len(ranked_values) == 1 or margin >= selected_policy.auto_selection_margin
    ranked: list[RankedTake] = []
    for index, (candidate, score) in enumerate(ranked_values, start=1):
        recommendation: Literal["recommended", "alternative", "review_required"]
        if not clear_winner:
            recommendation = "review_required"
        elif index == 1:
            recommendation = "recommended"
        else:
            recommendation = "alternative"
        ranked.append(
            RankedTake(
                take_id=candidate.take_id,
                source_range=candidate.source_range,
                components=candidate.components,
                overall_score=score,
                rank=index,
                evidence_ids=candidate.evidence_ids,
                notes=candidate.notes,
                recommendation=recommendation,
            )
        )
    return {
        "policy_id": selected_policy.policy_id,
        "recommended_take_id": ranked[0].take_id if clear_winner else None,
        "requires_review": not clear_winner,
        "score_margin": margin,
        "ranked_takes": [item.model_dump(mode="json") for item in ranked],
        "warnings": [] if clear_winner else ["take_scores_are_too_close_for_auto_selection"],
    }


__all__ = [
    "RankedTake",
    "TakeCandidate",
    "TakeRange",
    "TakeRankingPolicy",
    "TakeScoreComponents",
    "rank_take_candidates",
]
