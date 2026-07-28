from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.gate2 import approve_segment_gate2
from videoedit.services.gate3 import _validate_gate2_lock
from videoedit.services.project import ProjectLayout, initialize_project, sha256_file
from videoedit.services.qa_override import create_qa_override, evaluate_qa_override
from videoedit.services.segment_lock import lock_segment_revision

ROOT = Path(__file__).resolve().parents[2]


def _qa_fixture(tmp_path: Path) -> tuple[ProjectLayout, Path, Path]:
    layout = initialize_project(tmp_path, "qa_override_fixture")
    report = json.loads(
        (ROOT / "examples" / "segment_qa_report.example.json").read_text(encoding="utf-8")
    )
    report.update(
        {
            "project_id": layout.root.name,
            "revision_id": "rev_002",
            "overall_status": "warning",
            "final_ready": False,
        }
    )
    report["findings"][0]["status"] = "warning"
    report["findings"][0]["severity"] = "medium"
    report["summary"].update(
        {"passed": report["summary"]["passed"] - 1, "warnings": 1, "required_failures": 1}
    )
    report_path = layout.revision_root("rev_002") / "segment-qa.json"
    write_validated_artifact(ROOT, "segment_qa_report", report_path, report)
    evidence_path = layout.review / "evidence.png"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(b"review evidence")
    return layout, report_path, evidence_path


def test_qa_override_is_warning_only_and_hash_bound(tmp_path: Path) -> None:
    layout, report_path, evidence_path = _qa_fixture(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    finding_id = report["findings"][0]["finding_id"]

    override_path = create_qa_override(
        ROOT,
        layout,
        report_path,
        {finding_id: [evidence_path]},
        actor="operator@example.test",
        role="editor",
        reason="Inspected the retained frame and classified the static interval.",
        classification="intentional_static",
    )
    override = json.loads(override_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "qa_override", override)
    result = evaluate_qa_override(ROOT, layout, report_path, override_path)

    assert result["status"] == "ready"
    assert result["overridden_finding_ids"] == [finding_id]
    assert result["operator_approval_still_required"] is True

    assert (
        create_qa_override(
            ROOT,
            layout,
            report_path,
            {finding_id: [evidence_path]},
            actor="operator@example.test",
            role="editor",
            reason="Inspected the retained frame and classified the static interval.",
            classification="intentional_static",
        )
        == override_path
    )


def test_qa_override_rejects_failed_findings_and_stale_reports(tmp_path: Path) -> None:
    layout, report_path, evidence_path = _qa_fixture(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    finding_id = report["findings"][0]["finding_id"]
    report["findings"][0]["status"] = "fail"
    write_validated_artifact(ROOT, "segment_qa_report", report_path, report)

    with pytest.raises(PlanningValidationError, match="warnings only"):
        create_qa_override(
            ROOT,
            layout,
            report_path,
            {finding_id: [evidence_path]},
            actor="operator@example.test",
            role="editor",
            reason="Attempted to override a hard failure.",
        )


def test_gate2_and_segment_lock_bind_a_current_qa_override(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "qa_override_gate2_fixture")
    revision_root = layout.revision_root("rev_002")
    package_root = layout.review / "segment"
    package_root.mkdir(parents=True)

    def file_for(name: str, content: bytes) -> Path:
        path = package_root / name
        path.write_bytes(content)
        return path

    preview = file_for("preview.mp4", b"preview")
    contact_sheet = file_for("contact.jpg", b"contact")
    excerpt = file_for("excerpt.json", b"excerpt")
    markdown = file_for("transcript.md", b"transcript")
    effect_summary = file_for("effect-summary.json", b"effect-summary")
    diagnostics = file_for("diagnostics.json", b"diagnostics")
    fixes = file_for("fixes.template.md", b"fixes")
    package = {
        "schema_name": "segment_review_package",
        "schema_version": "1.0.0",
        "artifact_id": "art_segment_review_package",
        "project_id": layout.root.name,
        "revision_id": "rev_002",
        "segment_id": "segment_000001",
        "created_at": "2026-07-24T12:00:00Z",
        "producer": {
            "application_version": "0.2.0",
            "stage": "segment-review",
            "adapter": "fixture",
            "adapter_version": "1",
        },
        "planning_key": "1" * 64,
        "package_key": "2" * 64,
        "source_range": {"start_us": 0, "end_us": 6_000_000},
        "preview": {
            "artifact_id": "preview_artifact",
            "path": str(preview),
            "sha256": sha256_file(preview),
        },
        "contact_sheet": {
            "artifact_id": "contact_artifact",
            "path": str(contact_sheet),
            "sha256": sha256_file(contact_sheet),
        },
        "transcript_excerpt": {
            "artifact_id": "excerpt_artifact",
            "path": str(excerpt),
            "sha256": sha256_file(excerpt),
        },
        "transcript_markdown": {
            "artifact_id": "markdown_artifact",
            "path": str(markdown),
            "sha256": sha256_file(markdown),
        },
        "effect_summary": {
            "artifact_id": "effect_summary_artifact",
            "path": str(effect_summary),
            "sha256": sha256_file(effect_summary),
        },
        "diagnostics": {
            "artifact_id": "diagnostics_artifact",
            "path": str(diagnostics),
            "sha256": sha256_file(diagnostics),
        },
        "fixes_template": {
            "artifact_id": "fixes_artifact",
            "path": str(fixes),
            "sha256": sha256_file(fixes),
        },
        "warnings": [],
        "status": "complete",
    }
    package_path = package_root / "review-package.json"
    write_validated_artifact(ROOT, "segment_review_package", package_path, package)

    def example(name: str, schema: str) -> Path:
        payload = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
        payload.update({"project_id": layout.root.name, "revision_id": "rev_002"})
        path = revision_root / name
        write_validated_artifact(ROOT, schema, path, payload)
        return path

    comparison = example(
        "segment_transcript_comparison.example.json", "segment_transcript_comparison"
    )
    segment_qa = example("segment_qa_report.example.json", "segment_qa_report")
    visual_qa = example("segment_visual_qa_report.example.json", "segment_visual_qa_report")
    composition = layout.work / "composition-bundle.js"
    composition.write_bytes(b"composition-bundle")

    qa_payload = json.loads(segment_qa.read_text(encoding="utf-8"))
    qa_payload["findings"][0]["status"] = "warning"
    qa_payload["findings"][0]["severity"] = "medium"
    qa_payload["overall_status"] = "warning"
    qa_payload["final_ready"] = False
    qa_payload["summary"].update(
        {"passed": qa_payload["summary"]["passed"] - 1, "warnings": 1, "required_failures": 1}
    )
    write_validated_artifact(ROOT, "segment_qa_report", segment_qa, qa_payload)
    evidence_path = layout.review / "operator-evidence.png"
    evidence_path.write_bytes(b"operator evidence")
    finding_id = qa_payload["findings"][0]["finding_id"]
    override = create_qa_override(
        ROOT,
        layout,
        segment_qa,
        {finding_id: [evidence_path]},
        actor="operator@example.test",
        role="editor",
        reason="Reviewed the warning against the retained segment evidence.",
    )

    review = approve_segment_gate2(
        ROOT,
        layout,
        package_path,
        comparison,
        segment_qa,
        visual_qa,
        composition,
        actor="operator@example.test",
        role="editor",
        qa_override_path=override,
    )
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    assert review_payload["bound_hashes"]["qa_override_sha256"] == sha256_file(override)
    assert review_payload["qa_override"]["sha256"] == sha256_file(override)

    lock = lock_segment_revision(
        ROOT,
        layout,
        review,
        package_path,
        comparison,
        segment_qa,
        visual_qa,
        composition,
    )
    lock_payload = json.loads(lock.read_text(encoding="utf-8"))
    assert lock_payload["bound_hashes"]["qa_override_sha256"] == sha256_file(override)
    assert _validate_gate2_lock(ROOT, layout, lock, revision_id="rev_002") == sha256_file(lock)
