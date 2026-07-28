from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from videoedit import __version__
from videoedit.errors import PlanningValidationError, StaleApprovalError
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    config_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, sha256_file

SEMANTIC_PROPOSAL_TYPES = {
    "near_repetition",
    "semantic_repetition",
    "duplicate_take",
    "weak_take",
    "tangent",
    "housekeeping",
    "hook_tightening",
}


@dataclass(frozen=True, slots=True)
class SmartDenseReviewPolicy:
    maximum_questions_per_round: int = 5
    batch_approval_for_mechanical_only: bool = True
    allow_policy_authorized_micro_edits: bool = True
    batch_uncertain_items: bool = True
    sort_by_material_risk: bool = True
    include_recommendation: bool = True
    use_safe_fallback_when_available: bool = True
    avoid_low_impact_questions: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> SmartDenseReviewPolicy:
        try:
            value = yaml.safe_load(path.expanduser().resolve().read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PlanningValidationError(f"editing policy is unreadable: {path}") from exc
        review = value.get("review", {}) if isinstance(value, dict) else {}
        if not isinstance(review, dict):
            raise PlanningValidationError("editing policy review section must be an object")
        maximum = int(review.get("maximum_questions_per_round", 5))
        if not 1 <= maximum <= 5:
            raise PlanningValidationError("maximum_questions_per_round must be between 1 and 5")
        return cls(
            maximum_questions_per_round=maximum,
            batch_approval_for_mechanical_only=bool(
                review.get("batch_approval_for_mechanical_only", True)
            ),
            allow_policy_authorized_micro_edits=bool(
                review.get("allow_policy_authorized_micro_edits", True)
            ),
            batch_uncertain_items=bool(review.get("batch_uncertain_items", True)),
            sort_by_material_risk=bool(review.get("sort_by_material_risk", True)),
            include_recommendation=bool(review.get("include_recommendation", True)),
            use_safe_fallback_when_available=bool(
                review.get("use_safe_fallback_when_available", True)
            ),
            avoid_low_impact_questions=bool(review.get("avoid_low_impact_questions", True)),
        )


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be a JSON object: {path}")
    return value


def _owned_project_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return resolved


def _proposal_items(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        raw = value.get("proposals", [])
    else:
        raw = value
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PlanningValidationError("edit proposals must contain a proposals array")
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PlanningValidationError("each edit proposal must be an object")
        items.append(dict(item))
    return items


def _time_range(value: object, description: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PlanningValidationError(f"{description} must be an object")
    try:
        start_us = int(value["start_us"])
        end_us = int(value["end_us"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanningValidationError(f"{description} must have integer bounds") from exc
    if start_us < 0 or end_us <= start_us:
        raise PlanningValidationError(f"{description} must be a positive half-open range")
    return {"start_us": start_us, "end_us": end_us}


def _saved_us(proposal: Mapping[str, Any]) -> int:
    cut = _time_range(proposal.get("proposed_cut_range"), "proposal cut range")
    return cut["end_us"] - cut["start_us"]


def _proposal_ref(proposal: Mapping[str, Any]) -> dict[str, Any]:
    proposal_id = str(proposal.get("proposal_id", ""))
    proposal_type = str(proposal.get("proposal_type", ""))
    if not proposal_id or not proposal_type:
        raise PlanningValidationError("review batch proposals require an id and type")
    evidence = proposal.get("evidence_ids", [])
    if not isinstance(evidence, list):
        raise PlanningValidationError(f"proposal evidence_ids must be an array: {proposal_id}")
    return {
        "proposal_id": proposal_id,
        "proposal_sha256": canonical_sha256(proposal),
        "proposal_type": proposal_type,
        "source_range": _time_range(proposal.get("source_range"), "proposal source range"),
        "proposed_cut_range": _time_range(proposal.get("proposed_cut_range"), "proposal cut range"),
        "confidence": float(proposal.get("confidence", 0.0)),
        "meaning_risk": str(proposal.get("meaning_risk", "high")),
        "continuity_risk": str(proposal.get("continuity_risk", "high")),
        "evidence_ids": [str(item) for item in evidence],
        "safe_fallback": str(proposal.get("safe_fallback") or "keep_original"),
    }


def _material_score(proposal: Mapping[str, Any]) -> float:
    meaning = {"low": 0.0, "medium": 4.0, "high": 8.0}.get(
        str(proposal.get("meaning_risk", "high")), 8.0
    )
    continuity = {"low": 0.0, "medium": 2.0, "high": 5.0}.get(
        str(proposal.get("continuity_risk", "high")), 5.0
    )
    semantic = 4.0 if str(proposal.get("proposal_type")) in SEMANTIC_PROPOSAL_TYPES else 0.0
    confidence = max(0.0, min(1.0, float(proposal.get("confidence", 0.0))))
    uncertainty = (1.0 - confidence) * 2.0
    saved = min(10.0, _saved_us(proposal) / 1_000_000) * 0.01
    return meaning + continuity + semantic + uncertainty + saved


def _is_policy_batch_candidate(proposal: Mapping[str, Any]) -> bool:
    return (
        str(proposal.get("policy_result")) == "auto_eligible"
        and str(proposal.get("proposal_type")) not in SEMANTIC_PROPOSAL_TYPES
        and str(proposal.get("density_class", "micro")) == "micro"
        and str(proposal.get("meaning_risk", "high")) == "low"
        and str(proposal.get("continuity_risk", "high")) in {"low", "medium"}
        and isinstance(proposal.get("protected_content_check"), Mapping)
        and bool(proposal["protected_content_check"].get("passed"))
        and str(proposal.get("join_strategy", "")) not in {"", "keep_original"}
        and bool(proposal.get("join_preview_required", True))
    )


def _is_material_question(proposal: Mapping[str, Any]) -> bool:
    return (
        str(proposal.get("proposal_type")) in SEMANTIC_PROPOSAL_TYPES
        or str(proposal.get("meaning_risk", "high")) in {"medium", "high"}
        or str(proposal.get("continuity_risk", "high")) == "high"
        or not str(proposal.get("safe_fallback") or "keep_original").strip()
    )


def _question_item(proposal: Mapping[str, Any]) -> dict[str, Any]:
    ref = _proposal_ref(proposal)
    score = _material_score(proposal)
    return {
        **ref,
        "question_id": f"q_{ref['proposal_id']}",
        "priority": "high" if score >= 7.0 else "medium",
        "reason": str(proposal.get("reason") or "Material meaning or continuity uncertainty."),
        "recommendation": str(
            proposal.get("recommended_action")
            or proposal.get("alternative")
            or "Keep the original range unless the reviewer confirms the proposed cut."
        ),
        "transcript_before": str(proposal.get("transcript_before") or ""),
        "transcript_inside": str(proposal.get("transcript_inside") or ""),
        "transcript_after": str(proposal.get("transcript_after") or ""),
        "decision_options": ["approve", "reject", "modify"],
        "_material_score": score,
    }


def _fallback_item(proposal: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        **_proposal_ref(proposal),
        "reason": reason,
        "fallback_applied": "keep_original",
    }


def build_smart_dense_review_batch(
    proposals: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    policy_approved: bool = False,
    policy: SmartDenseReviewPolicy | None = None,
) -> dict[str, Any]:
    selected_policy = policy or SmartDenseReviewPolicy()
    items = _proposal_items(proposals)
    policy_batch: list[dict[str, Any]] = []
    pending_batch: list[dict[str, Any]] = []
    material: list[tuple[float, dict[str, Any]]] = []
    fallbacks: list[dict[str, Any]] = []
    for proposal in items:
        if _is_policy_batch_candidate(proposal):
            ref = _proposal_ref(proposal)
            item = {
                **ref,
                "execution": (
                    "policy_authorized" if policy_approved else "awaiting_policy_approval"
                ),
                "join_strategy": str(proposal["join_strategy"]),
                "join_preview_required": bool(proposal.get("join_preview_required", True)),
                "recommendation": (
                    "Apply under the approved smart_dense policy; render and QA the join."
                    if policy_approved
                    else "Hold until explicit smart_dense policy approval; render and QA the join."
                ),
            }
            (policy_batch if policy_approved else pending_batch).append(item)
            continue
        if _is_material_question(proposal):
            material.append((_material_score(proposal), _question_item(proposal)))
            continue
        fallbacks.append(
            _fallback_item(
                proposal,
                "Low-impact uncertainty uses the keep-original fallback; "
                "no operator question is needed.",
            )
        )
    if selected_policy.sort_by_material_risk:
        material.sort(
            key=lambda item: (
                -item[0],
                int(item[1]["proposed_cut_range"]["start_us"]),
                str(item[1]["proposal_id"]),
            )
        )
    questions = [item for _score, item in material[: selected_policy.maximum_questions_per_round]]
    deferred: list[dict[str, Any]] = []
    for _score, item in material[selected_policy.maximum_questions_per_round :]:
        deferred.append(
            {
                **item,
                "deferred_reason": "question_cap",
                "next_round_required": True,
                "_material_score": None,
            }
        )
    for item in [*questions, *deferred]:
        item.pop("_material_score", None)
    if selected_policy.use_safe_fallback_when_available:
        for item in deferred:
            fallbacks.append(
                {
                    "proposal_id": item["proposal_id"],
                    "proposal_sha256": item["proposal_sha256"],
                    "proposal_type": item["proposal_type"],
                    "source_range": item["source_range"],
                    "proposed_cut_range": item["proposed_cut_range"],
                    "confidence": item["confidence"],
                    "meaning_risk": item["meaning_risk"],
                    "continuity_risk": item["continuity_risk"],
                    "evidence_ids": item["evidence_ids"],
                    "safe_fallback": item["safe_fallback"],
                    "reason": (
                        "Question cap reached; keep the original range until the next review round."
                    ),
                    "fallback_applied": "keep_original",
                }
            )
    source_payload = proposals if isinstance(proposals, Mapping) else {}
    policy_batch_all = [*policy_batch, *pending_batch]
    batch_cut_us = sum(_saved_us(item) for item in items if _is_policy_batch_candidate(item))
    question_cut_us = sum(_saved_us(item) for item in items if _is_material_question(item))
    fallback_ids = {str(item["proposal_id"]) for item in fallbacks}
    fallback_cut_us = sum(
        _saved_us(item) for item in items if str(item.get("proposal_id")) in fallback_ids
    )
    total_cut_us = sum(_saved_us(item) for item in items)
    warnings: list[str] = []
    if pending_batch:
        warnings.append("smart_dense_policy_approval_required")
    if deferred:
        warnings.append("smart_dense_review_question_cap_reached")
    if fallbacks:
        warnings.append("smart_dense_safe_fallbacks_present")
    return {
        "policy_approval": {
            "state": "approved" if policy_approved else "pending",
            "approval_id": None,
            "approval_sha256": None,
        },
        "maximum_questions_per_round": selected_policy.maximum_questions_per_round,
        "policy_batch": policy_batch_all,
        "questions": questions,
        "deferred_questions": deferred,
        "fallbacks": fallbacks,
        "summary": {
            "total_proposals": len(items),
            "policy_batch_count": len(policy_batch_all),
            "policy_authorized_count": len(policy_batch),
            "policy_pending_count": len(pending_batch),
            "question_count": len(questions),
            "deferred_question_count": len(deferred),
            "fallback_count": len(fallbacks),
            "total_proposed_cut_us": total_cut_us,
            "policy_batch_cut_us": batch_cut_us,
            "question_cut_us": question_cut_us,
            "fallback_cut_us": fallback_cut_us,
        },
        "warnings": warnings,
        "_source_policy_id": str(source_payload.get("policy_id", "pol_smart_dense")),
        "_source_policy_version": int(source_payload.get("policy_version", 3)),
        "_source_policy_sha256": str(
            source_payload.get(
                "policy_sha256",
                canonical_sha256(
                    {
                        "policy_id": source_payload.get("policy_id", "pol_smart_dense"),
                        "policy_version": source_payload.get("policy_version", 3),
                    }
                ),
            )
        ),
    }


def _policy_values(proposals: Mapping[str, Any]) -> tuple[str, int, str]:
    policy_id = str(proposals.get("policy_id", "pol_smart_dense"))
    policy_version = int(proposals.get("policy_version", 3))
    policy_sha = str(
        proposals.get(
            "policy_sha256",
            canonical_sha256({"policy_id": policy_id, "policy_version": policy_version}),
        )
    )
    return policy_id, policy_version, policy_sha


def validate_smart_dense_policy_approval(
    package_root: Path,
    layout: ProjectLayout,
    approval_path: Path,
    proposals_path: Path,
    *,
    revision_id: str = "rev_001",
) -> dict[str, Any]:
    approval_file = _owned_project_path(layout, approval_path, "smart_dense policy approval")
    proposals_file = _owned_project_path(layout, proposals_path, "edit proposals")
    approval = _read_object(approval_file, "smart_dense policy approval")
    proposals = _read_object(proposals_file, "edit proposals")
    validate_artifact(package_root, "approval_record", approval)
    validate_artifact(package_root, "edit_proposals", proposals)
    policy_id, _policy_version, policy_sha = _policy_values(proposals)
    if approval.get("project_id") != layout.root.name or approval.get("revision_id") != revision_id:
        raise StaleApprovalError("smart_dense policy approval project or revision does not match")
    if approval.get("approval_type") != "smart_dense_policy":
        raise StaleApprovalError("approval is not a smart_dense policy approval")
    if approval.get("decision") != "approved":
        raise StaleApprovalError("smart_dense policy approval is not approved")
    if approval.get("approved_item_type") != "smart_dense_policy":
        raise StaleApprovalError("smart_dense policy approval item type is invalid")
    if approval.get("approved_item_sha256") != policy_sha:
        raise StaleApprovalError(f"smart_dense policy approval is stale for {policy_id}")
    if approval.get("config_sha256") != config_sha256(layout):
        raise StaleApprovalError("smart_dense policy approval is stale for project configuration")
    expected_input = artifact_input(str(proposals["artifact_id"]), proposals_file)
    inputs = approval.get("inputs", [])
    if expected_input not in inputs:
        raise StaleApprovalError("smart_dense policy approval is not bound to the proposal set")
    return approval


def create_smart_dense_policy_approval(
    package_root: Path,
    layout: ProjectLayout,
    proposals_path: Path,
    *,
    actor: str,
    role: str = "editor",
    reason: str = "Approved high-confidence mechanical edits under smart_dense policy",
    revision_id: str = "rev_001",
) -> Path:
    if not actor.strip():
        raise PlanningValidationError("smart_dense policy approval requires an actor")
    proposals_file = _owned_project_path(layout, proposals_path, "edit proposals")
    proposals = _read_object(proposals_file, "edit proposals")
    validate_artifact(package_root, "edit_proposals", proposals)
    if (
        proposals.get("project_id") != layout.root.name
        or proposals.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("edit proposals project or revision does not match")
    policy_id, _policy_version, policy_sha = _policy_values(proposals)
    if policy_id != "pol_smart_dense":
        raise PlanningValidationError(
            "smart_dense policy approval requires the smart_dense profile"
        )
    payload = {
        "schema_name": "approval_record",
        "schema_version": "1.0.0",
        "artifact_id": "art_smart_dense_policy_approval",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("smart-dense-policy-approval", "human-review", __version__),
        "inputs": [artifact_input(str(proposals["artifact_id"]), proposals_file)],
        "config_sha256": config_sha256(layout),
        "approval_id": f"apr_smart_dense_{policy_sha[:16]}",
        "approval_type": "smart_dense_policy",
        "actor": actor,
        "role": role,
        "decision": "approved",
        "reason": reason,
        "approved_item_type": "smart_dense_policy",
        "approved_item_sha256": policy_sha,
        "expires_at": None,
        "budget": None,
    }
    validate_artifact(package_root, "approval_record", payload)
    output = layout.review / f"smart-dense-policy-approval-{policy_sha[:16]}.json"
    if output.is_file():
        existing = _read_object(output, "existing smart_dense policy approval")
        validate_artifact(package_root, "approval_record", existing)
        if existing.get("approved_item_sha256") != policy_sha:
            raise StaleApprovalError("existing smart_dense policy approval is stale")
        return output
    return write_validated_artifact(package_root, "approval_record", output, payload)


def smart_dense_review_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Smart-dense review batch",
        "",
        f"- Batch: `{payload['batch_id']}`",
        f"- Proposal set SHA-256: `{payload['proposal_set_sha256']}`",
        f"- Policy approval: `{payload['policy_approval']['state']}`",
        f"- Policy-authorized mechanical edits: {summary['policy_authorized_count']}",
        f"- Mechanical edits awaiting policy approval: {summary['policy_pending_count']}",
        "- Questions in this round: "
        f"{summary['question_count']}/{payload['maximum_questions_per_round']}",
        f"- Safe fallbacks: {summary['fallback_count']}",
        "",
        "Policy-authorized mechanical edits still require rendered join previews and QA.",
        "Semantic proposals are never included in the policy batch.",
        "",
    ]
    questions = payload.get("questions", [])
    if questions:
        lines.extend(["## Questions", ""])
        for item in questions:
            lines.extend(
                [
                    f"### {item['question_id']} | {item['proposal_type']} | {item['priority']}",
                    "",
                    "- Range: "
                    f"{int(item['proposed_cut_range']['start_us']) / 1_000_000:.3f}s to "
                    f"{int(item['proposed_cut_range']['end_us']) / 1_000_000:.3f}s",
                    f"- Reason: {item['reason']}",
                    f"- Recommendation: {item['recommendation']}",
                    f"- Safe fallback: {item['safe_fallback']}",
                    f"- Evidence: {', '.join(item['evidence_ids']) or 'none recorded'}",
                    "- Decisions: approve, reject, modify",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "## Questions",
                "",
                "No material semantic or continuity questions are required in this round.",
                "",
            ]
        )
    if payload.get("deferred_questions"):
        lines.extend(
            [
                "## Deferred questions",
                "",
                "The question cap was reached. Deferred items retain the original "
                "range until the next round.",
                "",
            ]
        )
    return "\n".join(lines)


def write_smart_dense_review_batch(
    package_root: Path,
    layout: ProjectLayout,
    proposals_path: Path,
    *,
    policy_approval_path: Path | None = None,
    policy_path: Path | None = None,
    revision_id: str = "rev_001",
) -> Path:
    proposals_file = _owned_project_path(layout, proposals_path, "edit proposals")
    proposals = _read_object(proposals_file, "edit proposals")
    validate_artifact(package_root, "edit_proposals", proposals)
    if (
        proposals.get("project_id") != layout.root.name
        or proposals.get("revision_id") != revision_id
    ):
        raise PlanningValidationError("edit proposals project or revision does not match")
    selected_policy_path = (
        (policy_path or package_root / "config" / "editing-policy.example.yaml")
        .expanduser()
        .resolve()
    )
    selected_policy = SmartDenseReviewPolicy.from_yaml(selected_policy_path)
    approval: dict[str, Any] | None = None
    approval_file: Path | None = None
    approval_hash: str | None = None
    if policy_approval_path is not None:
        approval_file = _owned_project_path(
            layout, policy_approval_path, "smart_dense policy approval"
        )
        approval = validate_smart_dense_policy_approval(
            package_root,
            layout,
            approval_file,
            proposals_file,
            revision_id=revision_id,
        )
        approval_hash = sha256_file(approval_file)
    batch = build_smart_dense_review_batch(
        proposals,
        policy_approved=approval is not None,
        policy=selected_policy,
    )
    policy_id, policy_version, policy_sha = _policy_values(proposals)
    batch_hash = canonical_sha256(
        {
            "proposal_set_sha256": sha256_file(proposals_file),
            "policy_sha256": policy_sha,
            "approval_sha256": approval_hash,
            "maximum_questions_per_round": selected_policy.maximum_questions_per_round,
        }
    )
    policy_approval = {
        "state": "approved" if approval is not None else "pending",
        "approval_id": str(approval["approval_id"]) if approval else None,
        "approval_sha256": approval_hash,
    }
    payload: dict[str, Any] = {
        "schema_name": "edit_review_batch",
        "schema_version": "1.0.0",
        "artifact_id": f"art_smart_dense_batch_{batch_hash[:16]}",
        "project_id": layout.root.name,
        "revision_id": revision_id,
        "created_at": now_iso(),
        "producer": producer("smart-dense-review-batch", "deterministic-policy", __version__),
        "inputs": [
            artifact_input(str(proposals["artifact_id"]), proposals_file),
            *(
                [artifact_input(str(approval["artifact_id"]), approval_file)]
                if approval is not None and approval_file is not None
                else []
            ),
        ],
        "config_sha256": config_sha256(layout),
        "batch_id": f"bat_smart_dense_{batch_hash[:16]}",
        "proposal_set_artifact_id": str(proposals["artifact_id"]),
        "proposal_set_sha256": sha256_file(proposals_file),
        "policy_id": policy_id,
        "policy_version": policy_version,
        "policy_sha256": policy_sha,
        "policy_approval": policy_approval,
        "maximum_questions_per_round": selected_policy.maximum_questions_per_round,
        "policy_batch": batch["policy_batch"],
        "questions": batch["questions"],
        "deferred_questions": batch["deferred_questions"],
        "fallbacks": batch["fallbacks"],
        "summary": batch["summary"],
        "warnings": batch["warnings"],
    }
    validate_artifact(package_root, "edit_review_batch", payload)
    output = layout.review / f"smart-dense-review-batch-{batch_hash[:16]}.json"
    write_validated_artifact(package_root, "edit_review_batch", output, payload)
    write_text_atomically(output.with_suffix(".md"), smart_dense_review_markdown(payload))
    return output


__all__ = [
    "SEMANTIC_PROPOSAL_TYPES",
    "SmartDenseReviewPolicy",
    "build_smart_dense_review_batch",
    "create_smart_dense_policy_approval",
    "smart_dense_review_markdown",
    "validate_smart_dense_policy_approval",
    "write_smart_dense_review_batch",
]
