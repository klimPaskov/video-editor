from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.qa_review_decision import write_qa_review_decision


def _packet(package_root: Path, layout: Any, packet_path: Path, evidence: Path) -> None:
    candidate = layout.output / "candidate.mp4"
    candidate.write_bytes(b"candidate")
    payload = {
        "schema_name": "qa_review_packet",
        "schema_version": "1.0.0",
        "artifact_id": "art_qa_review_packet",
        "project_id": layout.root.name,
        "revision_id": "rev_002",
        "created_at": "2026-01-01T00:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "qa-review-packet",
            "adapter": "deterministic-evidence",
            "adapter_version": "1",
        },
        "inputs": [{"artifact_id": "art_join_preview_000001", "sha256": sha256_file(evidence)}],
        "candidate": {
            "artifact_id": "art_source_candidate",
            "path": str(candidate),
            "sha256": sha256_file(candidate),
            "size_bytes": candidate.stat().st_size,
        },
        "review_gate": "gate3",
        "status": "review_required",
        "summary": {
            "total_items": 1,
            "join_item_count": 1,
            "segment_item_count": 0,
            "final_item_count": 0,
            "high_severity_count": 1,
            "pending_item_count": 1,
            "join_preview_count": 1,
            "join_warning_by_code": {"TRANSCRIPT_SEQUENCE": 1},
            "segment_warning_by_code": {},
            "source_warning_finding_ids": ["finding_source_join_review"],
        },
        "items": [
            {
                "item_id": "qa_join_join_000001",
                "scope": "join",
                "check_code": "JOIN_REVIEW",
                "severity": "high",
                "source_finding_id": "qa_join_warning_join_000001",
                "status": "warning",
                "title": "join_000001: TRANSCRIPT_SEQUENCE",
                "recommendation": "Inspect the join.",
                "decision_options": [
                    "repair",
                    "reviewed_non_defect",
                    "false_positive",
                    "accepted_risk",
                    "reject_candidate",
                ],
                "decision": "pending",
                "time_range": {"start_us": 1000000, "end_us": 2000000},
                "evidence": [
                    {
                        "artifact_id": "art_join_preview_000001",
                        "path": str(evidence),
                        "sha256": sha256_file(evidence),
                        "size_bytes": evidence.stat().st_size,
                    }
                ],
                "details": {},
            }
        ],
    }
    write_validated_artifact(package_root, "qa_review_packet", packet_path, payload)


def test_record_qa_review_binds_current_packet_and_keeps_approval_separate(
    tmp_path: Path,
) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "qa_decision_demo")
    evidence = layout.review / "join-preview.mp4"
    evidence.write_bytes(b"preview")
    packet_path = layout.review / "qa-review-packet.json"
    _packet(package_root, layout, packet_path, evidence)

    output = write_qa_review_decision(
        package_root,
        layout,
        packet_path,
        {"qa_join_join_000001": "reviewed_non_defect"},
        actor="operator",
        role="editor",
        reason="The static frame is intentional and was inspected in context.",
    )
    decision = json.loads(output.read_text(encoding="utf-8"))
    validate_artifact(package_root, "qa_review_decision", decision)

    assert decision["status"] == "reviewed"
    assert decision["summary"]["decided_item_count"] == 1
    assert decision["summary"]["pending_item_count"] == 0
    assert decision["decisions"][0]["decision"] == "reviewed_non_defect"
    assert decision["decision_record"] == "recorded"
    assert output.with_suffix(".md").is_file()
    assert any(item["artifact_id"] == "art_qa_review_packet" for item in decision["inputs"])


def test_record_qa_review_rejects_changed_retained_evidence(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "qa_decision_stale_demo")
    evidence = layout.review / "join-preview.mp4"
    evidence.write_bytes(b"preview")
    packet_path = layout.review / "qa-review-packet.json"
    _packet(package_root, layout, packet_path, evidence)
    evidence.write_bytes(b"tampered")

    with pytest.raises(PlanningValidationError, match="evidence hash is stale"):
        write_qa_review_decision(
            package_root,
            layout,
            packet_path,
            {"qa_join_join_000001": "repair"},
            actor="operator",
            role="editor",
            reason="Repair is required.",
        )
