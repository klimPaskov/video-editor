from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.backup import verify_backup_targets
from videoedit.services.cleanup import approve_cleanup, execute_cleanup, plan_cleanup
from videoedit.services.project import initialize_project, sha256_file

ROOT = Path(__file__).resolve().parents[2]


def _fixture(tmp_path: Path) -> tuple[object, Path, Path]:
    layout = initialize_project(tmp_path, "cleanup_fixture")
    source = layout.raw / "source.bin"
    backup = layout.work / "backups" / "source.bin"
    candidate = layout.work / "final-assembly" / "candidate.tmp"
    source.write_bytes(b"immutable source")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(source.read_bytes())
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"derived candidate")
    backup_report = verify_backup_targets(
        ROOT,
        layout,
        [
            {
                "role": "source",
                "source_path": str(source),
                "backup_path": str(backup),
            }
        ],
    )
    cleanup_plan = plan_cleanup(
        ROOT,
        layout,
        backup_report,
        preserve_valid_revisions=False,
        preserve_failed_evidence=False,
        preserve_active_assembly=False,
    )
    return layout, cleanup_plan, backup_report


def test_cleanup_approval_is_idempotent_but_rejects_reviewer_conflicts(
    tmp_path: Path,
) -> None:
    layout, cleanup_plan, _backup_report = _fixture(tmp_path)
    approval = approve_cleanup(
        ROOT,
        layout,
        cleanup_plan,
        actor="operator@example.test",
        role="editor",
        reason="Verified retained revisions and backups.",
    )
    payload = json.loads(approval.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "approval_record", payload)
    assert payload["approval_type"] == "cleanup"
    assert payload["approved_item_sha256"]

    same = approve_cleanup(
        ROOT,
        layout,
        cleanup_plan,
        actor="operator@example.test",
        role="editor",
        reason="Verified retained revisions and backups.",
    )
    assert same == approval

    with pytest.raises(StateConflictError, match="different reviewer"):
        approve_cleanup(
            ROOT,
            layout,
            cleanup_plan,
            actor="different@example.test",
            role="editor",
            reason="A different decision context.",
        )


def test_backup_verification_rejects_tampered_cached_report(tmp_path: Path) -> None:
    layout, _cleanup_plan, backup_report = _fixture(tmp_path)
    cached = json.loads(backup_report.read_text(encoding="utf-8"))
    cached["status"] = "fail"
    write_validated_artifact(ROOT, "backup_verification", backup_report, cached)

    source = layout.raw / "source.bin"
    backup = layout.work / "backups" / "source.bin"
    with pytest.raises(StateConflictError, match="backup verification exists with stale contents"):
        verify_backup_targets(
            ROOT,
            layout,
            [
                {
                    "role": "source",
                    "source_path": str(source),
                    "backup_path": str(backup),
                }
            ],
        )
    assert json.loads(backup_report.read_text(encoding="utf-8"))["status"] == "fail"


def test_backup_verification_stage_key_includes_project_configuration(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "backup_config_key")
    source = layout.raw / "source.bin"
    backup = layout.work / "backups" / "source.bin"
    source.write_bytes(b"immutable source")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(source.read_bytes())
    first = verify_backup_targets(
        ROOT,
        layout,
        [{"role": "source", "source_path": str(source), "backup_path": str(backup)}],
    )

    config_path = layout.config / "project.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n# cache-key regression\n",
        encoding="utf-8",
    )
    second = verify_backup_targets(
        ROOT,
        layout,
        [{"role": "source", "source_path": str(source), "backup_path": str(backup)}],
    )

    assert second != first
    assert second.is_file()


def test_cleanup_plan_rejects_tampered_cached_entries(tmp_path: Path) -> None:
    layout, cleanup_plan, _backup_report = _fixture(tmp_path)
    cached = json.loads(cleanup_plan.read_text(encoding="utf-8"))
    cached["entries"][0]["eligible"] = not cached["entries"][0]["eligible"]
    write_validated_artifact(ROOT, "cleanup_plan", cleanup_plan, cached)

    with pytest.raises(StateConflictError, match="cleanup plan exists with stale contents"):
        plan_cleanup(
            ROOT,
            layout,
            layout.artifacts / "backup-verification.json",
            preserve_valid_revisions=False,
            preserve_failed_evidence=False,
            preserve_active_assembly=False,
        )


def test_cleanup_rejects_foreign_backup_or_plan_bindings(tmp_path: Path) -> None:
    layout, cleanup_plan, backup_report = _fixture(tmp_path)
    foreign_backup = json.loads(backup_report.read_text(encoding="utf-8"))
    foreign_backup["project_id"] = "foreign_project"
    foreign_backup_path = layout.artifacts / "foreign-backup.json"
    write_validated_artifact(ROOT, "backup_verification", foreign_backup_path, foreign_backup)

    with pytest.raises(PlanningValidationError, match="backup verification belongs"):
        plan_cleanup(ROOT, layout, foreign_backup_path)

    foreign_plan = json.loads(cleanup_plan.read_text(encoding="utf-8"))
    foreign_plan["project_id"] = "foreign_project"
    foreign_plan_path = layout.artifacts / "foreign-cleanup-plan.json"
    write_validated_artifact(ROOT, "cleanup_plan", foreign_plan_path, foreign_plan)
    with pytest.raises(PlanningValidationError, match="cleanup plan belongs"):
        approve_cleanup(
            ROOT,
            layout,
            foreign_plan_path,
            actor="operator@example.test",
            role="editor",
            reason="Should be rejected.",
        )

    foreign_approval = json.loads(
        (ROOT / "examples" / "approval_record.example.json").read_text(encoding="utf-8")
    )
    foreign_approval.update(
        {
            "artifact_id": "art_approval_cleanup_foreign",
            "project_id": "foreign_project",
            "revision_id": "rev_001",
            "approval_type": "cleanup",
            "approved_item_type": "cleanup_plan",
            "approved_item_sha256": sha256_file(foreign_plan_path),
            "decision": "approved",
        }
    )
    foreign_approval_path = layout.review / "foreign-cleanup-approval.json"
    write_validated_artifact(ROOT, "approval_record", foreign_approval_path, foreign_approval)
    candidate = layout.work / "final-assembly" / "candidate.tmp"
    with pytest.raises(PlanningValidationError, match="belongs to another project"):
        execute_cleanup(
            ROOT,
            layout,
            foreign_plan_path,
            foreign_approval_path,
            backup_report,
        )
    assert candidate.is_file()


def test_cleanup_rejects_a_replacement_file_after_approval(tmp_path: Path) -> None:
    layout, cleanup_plan, backup_report = _fixture(tmp_path)
    approval = approve_cleanup(
        ROOT,
        layout,
        cleanup_plan,
        actor="operator@example.test",
        role="editor",
        reason="Verified retained revisions and backups.",
    )
    candidate = layout.work / "final-assembly" / "candidate.tmp"
    candidate.write_bytes(b"replacement derived candidate")

    with pytest.raises(PlanningValidationError, match="cleanup entry hash changed"):
        execute_cleanup(
            ROOT,
            layout,
            cleanup_plan,
            approval,
            backup_report,
        )
    assert candidate.is_file()
