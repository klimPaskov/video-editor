from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.pipeline.stage_key import make_stage_key
from videoedit.services.artifacts import (
    artifact_input,
    canonical_sha256,
    now_iso,
    producer,
    validate_artifact,
    write_text_atomically,
    write_validated_artifact,
)
from videoedit.services.project import ProjectLayout, ProjectLock, sha256_file

IMPLEMENTATION_VERSION = "p11-qa-review-decision-1"
_DECISIONS = frozenset(
    {
        "repair",
        "reviewed_non_defect",
        "intentional_static",
        "false_positive",
        "accepted_risk",
        "reject_candidate",
    }
)


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(f"{description} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(f"{description} must be an object: {path}")
    return value


def _owned_path(layout: ProjectLayout, path: Path, description: str) -> Path:
    selected = path.expanduser().resolve()
    try:
        selected.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise PlanningValidationError(f"{description} must be inside the project") from exc
    return selected


def _file_ref(
    layout: ProjectLayout, path: Path, artifact_id: str, description: str
) -> dict[str, Any]:
    selected = _owned_path(layout, path, description)
    if not selected.is_file() or selected.stat().st_size <= 0:
        raise PlanningValidationError(f"{description} is missing or empty: {selected}")
    return {
        "artifact_id": artifact_id,
        "path": str(selected),
        "sha256": sha256_file(selected),
        "size_bytes": selected.stat().st_size,
    }


def _validate_packet_evidence(
    layout: ProjectLayout, reference: Mapping[str, Any]
) -> dict[str, Any]:
    artifact_id = str(reference.get("artifact_id", ""))
    raw_path = reference.get("path")
    if not artifact_id or not isinstance(raw_path, str) or not raw_path:
        raise PlanningValidationError("QA review packet evidence reference is incomplete")
    selected = _owned_path(layout, Path(raw_path), "QA review packet evidence")
    if not selected.is_file():
        raise PlanningValidationError(f"QA review packet evidence is missing: {selected}")
    if reference.get("sha256") != sha256_file(selected):
        raise PlanningValidationError(f"QA review packet evidence hash is stale: {selected}")
    if reference.get("size_bytes") != selected.stat().st_size:
        raise PlanningValidationError(f"QA review packet evidence size is stale: {selected}")
    return {
        "artifact_id": artifact_id,
        "path": str(selected),
        "sha256": sha256_file(selected),
        "size_bytes": selected.stat().st_size,
    }


def _binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("created_at", None)
    reviewer = value.get("reviewer")
    if isinstance(reviewer, Mapping):
        reviewer_value = dict(reviewer)
        reviewer_value.pop("reviewed_at", None)
        value["reviewer"] = reviewer_value
    return value


def qa_review_decision_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# QA review decision record",
        "",
        f"- Project: `{payload['project_id']}`",
        f"- Revision: `{payload['revision_id']}`",
        f"- Packet: `{payload['packet']['sha256']}`",
        f"- Reviewer: `{payload['reviewer']['actor']}` ({payload['reviewer']['role']})",
        f"- Status: `{payload['status']}`",
        f"- Decisions: {summary['decided_item_count']}/{summary['packet_item_count']}",
        "",
        "This record documents human review only. It is not a QA override, Gate 2 or "
        "Gate 3 approval, delivery approval, or cleanup approval.",
        "",
    ]
    for item in payload["decisions"]:
        lines.extend(
            [
                f"## {item['item_id']} | {item['decision']}",
                "",
                f"- Check: `{item['check_code']}`",
                f"- Source finding: `{item['source_finding_id']}`",
                f"- Reason: {item['reason']}",
                f"- Evidence: {', '.join(str(ref['path']) for ref in item['evidence'])}",
                "",
            ]
        )
    return "\n".join(lines)


def write_qa_review_decision(
    package_root: Path,
    layout: ProjectLayout,
    packet_path: Path,
    decisions: Mapping[str, str],
    *,
    actor: str,
    role: str,
    reason: str,
    evidence_by_item: Mapping[str, Sequence[Path]] | None = None,
    notes: str = "",
    revision_id: str | None = None,
    output_path: Path | None = None,
) -> Path:
    """Persist a human decision subset against one immutable QA packet."""

    if not actor.strip() or not role.strip():
        raise PlanningValidationError("QA review decision actor and role are required")
    if not reason.strip():
        raise PlanningValidationError("QA review decision reason is required")
    if not decisions:
        raise PlanningValidationError("QA review decision requires at least one item")
    if evidence_by_item is not None and not set(evidence_by_item).issubset(decisions):
        raise PlanningValidationError("QA decision evidence names an undecided packet item")

    selected_packet = _owned_path(layout, packet_path, "QA review packet")
    if not selected_packet.is_file():
        raise PlanningValidationError(f"QA review packet does not exist: {selected_packet}")
    packet = _read_object(selected_packet, "QA review packet")
    validate_artifact(package_root, "qa_review_packet", packet)
    if packet.get("project_id") != layout.root.name:
        raise PlanningValidationError("QA review packet belongs to another project")
    packet_revision = str(packet["revision_id"])
    if revision_id is not None and packet_revision != revision_id:
        raise PlanningValidationError("QA review packet belongs to another revision")

    raw_items = packet.get("items")
    if not isinstance(raw_items, list):
        raise PlanningValidationError("QA review packet has no items array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise PlanningValidationError("QA review packet item must be an object")
        item_id = str(item.get("item_id", ""))
        if not item_id or item_id in indexed:
            raise PlanningValidationError("QA review packet item IDs must be unique")
        indexed[item_id] = item

    evidence_by_item = evidence_by_item or {}
    selected_ids = list(decisions)
    if len(selected_ids) != len(set(selected_ids)):
        raise PlanningValidationError("QA review decision item IDs must be unique")
    packet_ref = _file_ref(layout, selected_packet, str(packet["artifact_id"]), "QA review packet")
    input_refs: dict[str, dict[str, Any]] = {packet_ref["artifact_id"]: packet_ref}
    records: list[dict[str, Any]] = []
    classification_counts: dict[str, int] = {}
    for item_id in selected_ids:
        item = indexed.get(item_id)
        if item is None:
            raise PlanningValidationError(f"QA review packet item does not exist: {item_id}")
        decision = str(decisions[item_id])
        if decision not in _DECISIONS:
            raise PlanningValidationError(f"QA review decision is invalid: {decision}")
        options = item.get("decision_options")
        if not isinstance(options, list) or decision not in options:
            raise PlanningValidationError(
                f"QA review decision is not allowed for packet item: {item_id}"
            )
        item_evidence = item.get("evidence")
        if not isinstance(item_evidence, list) or not item_evidence:
            raise PlanningValidationError(f"QA packet item has no retained evidence: {item_id}")
        evidence: list[dict[str, Any]] = [
            _validate_packet_evidence(layout, reference)
            for reference in item_evidence
            if isinstance(reference, Mapping)
        ]
        if len(evidence) != len(item_evidence):
            raise PlanningValidationError(f"QA packet item evidence is invalid: {item_id}")
        seen_paths = {Path(str(reference["path"])) for reference in evidence}
        for index, extra_path in enumerate(evidence_by_item.get(item_id, ()), start=1):
            extra_ref = _file_ref(
                layout,
                extra_path,
                f"art_qa_decision_{item_id}_{index:03d}",
                f"QA decision evidence for {item_id}",
            )
            if Path(str(extra_ref["path"])) not in seen_paths:
                evidence.append(extra_ref)
                seen_paths.add(Path(str(extra_ref["path"])))
        for reference in evidence:
            input_refs.setdefault(str(reference["artifact_id"]), reference)
        classification_counts[decision] = classification_counts.get(decision, 0) + 1
        records.append(
            {
                "item_id": item_id,
                "check_code": str(item["check_code"]),
                "source_finding_id": str(item["source_finding_id"]),
                "packet_item_sha256": canonical_sha256(item),
                "decision": decision,
                "reason": reason.strip(),
                "time_range": item.get("time_range"),
                "evidence": evidence,
            }
        )

    pending_count = len(raw_items) - len(records)
    repair_count = classification_counts.get("repair", 0)
    reject_count = classification_counts.get("reject_candidate", 0)
    status = (
        "partial"
        if pending_count
        else "changes_requested"
        if repair_count or reject_count
        else "reviewed"
    )
    input_paths = [
        (artifact_id, Path(str(reference["path"]))) for artifact_id, reference in input_refs.items()
    ]
    stage_key = make_stage_key(
        "qa-review-decision",
        IMPLEMENTATION_VERSION,
        [sha256_file(path) for _artifact_id, path in input_paths],
        {
            "project_id": layout.root.name,
            "revision_id": packet_revision,
            "actor": actor.strip(),
            "role": role.strip(),
            "reason": reason.strip(),
            "notes": notes,
            "decisions": decisions,
        },
    )
    selected_output = (
        _owned_path(layout, output_path, "QA review decision output")
        if output_path is not None
        else (layout.review / "qa-decisions" / f"{stage_key[:16]}-qa-review-decision.json")
    )
    evidence_paths = {Path(str(reference["path"])) for reference in input_refs.values()}
    if selected_output in {selected_packet, *evidence_paths}:
        raise PlanningValidationError(
            "QA review decision output cannot overwrite its packet or evidence"
        )
    payload: dict[str, Any] = {
        "schema_name": "qa_review_decision",
        "schema_version": "1.0.0",
        "artifact_id": f"art_qa_review_decision_{stage_key[:16]}",
        "project_id": layout.root.name,
        "revision_id": packet_revision,
        "created_at": now_iso(),
        "producer": producer("qa-review-decision", "human-review", IMPLEMENTATION_VERSION),
        "inputs": [artifact_input(artifact_id, path) for artifact_id, path in input_paths],
        "packet": packet_ref,
        "decision_record": "recorded",
        "reviewer": {"actor": actor.strip(), "role": role.strip(), "reviewed_at": now_iso()},
        "decisions": records,
        "summary": {
            "packet_item_count": len(raw_items),
            "decided_item_count": len(records),
            "pending_item_count": pending_count,
            "repair_count": repair_count,
            "reject_count": reject_count,
            "classification_counts": classification_counts,
        },
        "status": status,
        "notes": notes,
    }
    with ProjectLock(layout, stage="qa_review_decision", revision_id=packet_revision):
        if selected_output.is_file():
            current = _read_object(selected_output, "QA review decision")
            validate_artifact(package_root, "qa_review_decision", current)
            if _binding(current) == _binding(payload):
                return selected_output
            raise StateConflictError("QA review decision output exists with different bindings")
        write_validated_artifact(package_root, "qa_review_decision", selected_output, payload)
        write_text_atomically(
            selected_output.with_suffix(".md"), qa_review_decision_markdown(payload)
        )
    return selected_output


__all__ = [
    "IMPLEMENTATION_VERSION",
    "qa_review_decision_markdown",
    "write_qa_review_decision",
]
