from __future__ import annotations

import json
from pathlib import Path

from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.operations import (
    cancel_stage,
    read_project_status,
    recover_crashed_stage,
    request_stage_retry,
    write_project_status,
)
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.stage_state import begin_stage

ROOT = Path(__file__).resolve().parents[2]


def test_status_cancellation_and_retry_are_schema_bound(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "operations_fixture")
    status = read_project_status(ROOT, layout)
    assert status["source_integrity"] == "unknown"
    assert status["qa_ready"] is False
    assert "gate1_approval_missing_or_stale" in status["warnings"]
    status_path = write_project_status(ROOT, layout)
    validate_artifact(ROOT, "operation_status", json.loads(status_path.read_text(encoding="utf-8")))

    staging_path = layout.staging / "fixture" / "partial.bin"
    staging_path.parent.mkdir(parents=True)
    staging_path.write_bytes(b"partial")
    begin_stage(
        ROOT,
        layout,
        project_id=layout.root.name,
        revision_id="rev_001",
        stage="fixture_stage",
        stage_key="a" * 64,
        staging_paths=[staging_path],
    )
    state_path = cancel_stage(
        ROOT,
        layout,
        "fixture_stage",
        reason="operator stopped the fixture run",
    )
    assert state_path.is_file()
    assert not staging_path.exists()
    retry_path = request_stage_retry(
        ROOT,
        layout,
        "fixture_stage",
        reason="retry after the operator repair",
    )
    retry = json.loads(retry_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "retry_request", retry)
    assert retry["previous_status"] == "cancelled"

    current = read_project_status(ROOT, layout)
    stage = next(item for item in current["stages"] if item["stage"] == "fixture_stage")
    assert stage["status"] == "cancelled"


def test_crash_recovery_requires_an_orphaned_running_state(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "recovery_fixture")
    begin_stage(
        ROOT,
        layout,
        project_id=layout.root.name,
        revision_id="rev_001",
        stage="orphan_stage",
        stage_key="b" * 64,
        staging_paths=[],
    )
    state_path = recover_crashed_stage(
        ROOT,
        layout,
        "orphan_stage",
        reason="operator confirmed the process was lost",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["error"]["code"] == "CRASH_RECOVERY_REQUIRED"
    retry_path = request_stage_retry(
        ROOT, layout, "orphan_stage", reason="retry after crash recovery"
    )
    retry = json.loads(retry_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "retry_request", retry)
    assert retry["previous_status"] == "failed"


def test_status_reuses_immutable_ingest_source_for_later_revision_and_finds_review_qa(
    tmp_path: Path,
) -> None:
    layout = initialize_project(tmp_path, "revision_status_fixture")
    source_path = layout.raw / "source.mp4"
    source_path.write_bytes(b"immutable source")
    source_manifest = json.loads(
        (ROOT / "examples" / "source_manifest.example.json").read_text(encoding="utf-8")
    )
    source_manifest.update(
        {
            "project_id": layout.root.name,
            "managed_path": str(source_path.resolve()),
            "source_path": str(source_path.resolve()),
            "sha256": sha256_file(source_path),
            "size_bytes": source_path.stat().st_size,
        }
    )
    write_validated_artifact(
        ROOT,
        "source_manifest",
        layout.artifacts / "source-manifest.json",
        source_manifest,
    )

    final_qa = json.loads(
        (ROOT / "examples" / "final_qa_report.example.json").read_text(encoding="utf-8")
    )
    final_qa.update(
        {
            "project_id": layout.root.name,
            "revision_id": "rev_002",
            "final_ready": False,
            "overall_status": "warning",
        }
    )
    write_validated_artifact(
        ROOT,
        "final_qa_report",
        layout.review / "final-qa-source-current.json",
        final_qa,
    )

    status = read_project_status(ROOT, layout, revision_id="rev_002")

    assert status["source_integrity"] == "pass"
    assert status["qa_ready"] is False
    assert "final_qa_not_ready" in status["warnings"]
    assert "final_qa_missing" not in status["warnings"]
    assert "source_manifest_project_or_revision_mismatch" not in status["warnings"]


def test_status_accepts_a_current_gate1_approval(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "current_gate1_status_fixture")
    approval = json.loads(
        (ROOT / "examples" / "approval_record.example.json").read_text(encoding="utf-8")
    )
    approval.update(
        {
            "project_id": layout.root.name,
            "revision_id": "rev_001",
            "approval_type": "edit",
            "decision": "approved",
        }
    )
    write_validated_artifact(
        ROOT,
        "approval_record",
        layout.review / "gate1-approval-current.json",
        approval,
    )

    status = read_project_status(ROOT, layout)

    assert "gate1_approval_missing_or_stale" not in status["warnings"]
