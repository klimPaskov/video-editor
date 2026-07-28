from __future__ import annotations

import json
from pathlib import Path

from videoedit.errors import StaleApprovalError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import ProjectLayout
from videoedit.services.review_batch import (
    SmartDenseReviewPolicy,
    build_smart_dense_review_batch,
    create_smart_dense_policy_approval,
    write_smart_dense_review_batch,
)

ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


def _proposal(
    proposal_id: str,
    proposal_type: str = "filler_word",
    *,
    confidence: float = 0.99,
    meaning_risk: str = "low",
    continuity_risk: str = "low",
    policy_result: str = "auto_eligible",
    start_us: int = 1_000_000,
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "source_range": {"start_us": start_us, "end_us": start_us + 300_000},
        "proposed_cut_range": {"start_us": start_us, "end_us": start_us + 200_000},
        "word_ids": [f"wrd_{proposal_id}"],
        "excerpt": "and um this",
        "transcript_before": "before",
        "transcript_inside": "inside",
        "transcript_after": "after",
        "reason": "The evidence supports a bounded cut.",
        "confidence": confidence,
        "meaning_risk": meaning_risk,
        "continuity_risk": continuity_risk,
        "evidence_ids": [f"ev_{proposal_id}"],
        "policy_result": policy_result,
        "approval_required": True,
        "alternative": "Keep the original range.",
        "recommended_action": "Remove and verify the rendered join.",
        "safe_fallback": "keep_original",
        "density_class": "micro",
        "join_strategy": "hard_cut_with_micro_audio_crossfade",
        "protected_content_check": {"passed": True, "categories": [], "notes": "clear"},
        "join_preview_required": True,
        "pacing_impact": "tightens",
    }


def test_high_confidence_mechanical_items_wait_for_then_use_policy_approval() -> None:
    proposal_set = {
        "policy_id": "pol_smart_dense",
        "policy_version": 3,
        "proposals": [_proposal("prp_auto")],
    }

    pending = build_smart_dense_review_batch(proposal_set)
    assert pending["summary"]["policy_pending_count"] == 1
    assert pending["policy_batch"][0]["execution"] == "awaiting_policy_approval"
    assert pending["questions"] == []

    approved = build_smart_dense_review_batch(proposal_set, policy_approved=True)
    assert approved["summary"]["policy_authorized_count"] == 1
    assert approved["policy_batch"][0]["execution"] == "policy_authorized"


def test_material_semantic_uncertainty_is_one_ranked_question() -> None:
    proposal = _proposal(
        "prp_semantic",
        "semantic_repetition",
        confidence=0.74,
        meaning_risk="medium",
        policy_result="review_required",
    )
    result = build_smart_dense_review_batch({"proposals": [proposal]})

    assert result["summary"]["question_count"] == 1
    assert result["questions"][0]["question_id"] == "q_prp_semantic"
    assert result["questions"][0]["decision_options"] == ["approve", "reject", "modify"]
    assert result["summary"]["policy_batch_count"] == 0


def test_low_impact_uncertainty_uses_fallback_without_a_question() -> None:
    proposal = _proposal(
        "prp_low_confidence",
        confidence=0.80,
        policy_result="review_required",
    )
    result = build_smart_dense_review_batch({"proposals": [proposal]})

    assert result["questions"] == []
    assert result["summary"]["fallback_count"] == 1
    assert result["fallbacks"][0]["fallback_applied"] == "keep_original"


def test_material_questions_are_capped_and_remaining_items_are_deferred() -> None:
    proposals = [
        _proposal(
            f"prp_semantic_{index:02d}",
            "semantic_repetition",
            confidence=0.60 + index / 100,
            meaning_risk="medium",
            policy_result="review_required",
            start_us=1_000_000 + index * 400_000,
        )
        for index in range(7)
    ]
    result = build_smart_dense_review_batch({"proposals": proposals})

    assert len(result["questions"]) == 5
    assert len(result["deferred_questions"]) == 2
    assert all(item["next_round_required"] is True for item in result["deferred_questions"])
    assert result["summary"]["fallback_count"] == 2
    assert "smart_dense_review_question_cap_reached" in result["warnings"]


def test_writer_and_explicit_policy_approval_are_hash_bound(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "batch_demo")
    proposals = json.loads(
        (ROOT / "examples" / "edit_proposals.example.json").read_text(encoding="utf-8")
    )
    proposals["project_id"] = "batch_demo"
    proposals_path = layout.artifacts / "edit-proposals.json"
    write_validated_artifact(ROOT, "edit_proposals", proposals_path, proposals)

    pending_path = write_smart_dense_review_batch(ROOT, layout, proposals_path)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "edit_review_batch", pending)
    assert pending["policy_approval"]["state"] == "pending"
    assert pending["summary"]["policy_pending_count"] >= 1

    approval_path = create_smart_dense_policy_approval(
        ROOT,
        layout,
        proposals_path,
        actor="operator@example.com",
    )
    approved_path = write_smart_dense_review_batch(
        ROOT,
        layout,
        proposals_path,
        policy_approval_path=approval_path,
    )
    approved = json.loads(approved_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "edit_review_batch", approved)
    assert approved["policy_approval"]["state"] == "approved"
    assert approved["summary"]["policy_authorized_count"] >= 1

    proposals["policy_version"] = 4
    write_validated_artifact(ROOT, "edit_proposals", proposals_path, proposals)
    try:
        write_smart_dense_review_batch(
            ROOT,
            layout,
            proposals_path,
            policy_approval_path=approval_path,
        )
    except StaleApprovalError:
        pass
    else:
        raise AssertionError("a changed policy must stale the prior approval")


def test_review_policy_reads_the_five_question_cap() -> None:
    policy = SmartDenseReviewPolicy.from_yaml(ROOT / "config" / "editing-policy.example.yaml")
    assert policy.maximum_questions_per_round == 5
    assert policy.batch_approval_for_mechanical_only is True
