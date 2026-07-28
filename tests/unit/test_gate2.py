from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError, StateConflictError
from videoedit.services.artifacts import validate_artifact, write_validated_artifact
from videoedit.services.gate2 import approve_segment_gate2
from videoedit.services.gate3 import _validate_gate2_lock
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.segment_lock import lock_segment_revision

ROOT = Path(__file__).resolve().parents[2]


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    layout = initialize_project(tmp_path, "gate2_fixture")
    revision_root = layout.revision_root("rev_002")
    revision_root.mkdir(parents=True)
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
    package_path = package_root / "review-package.json"
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
    write_validated_artifact(ROOT, "segment_review_package", package_path, package)

    def example(name: str, schema: str, **updates: object) -> Path:
        payload = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
        payload.update(updates)
        path = revision_root / name
        write_validated_artifact(ROOT, schema, path, payload)
        return path

    comparison = example(
        "segment_transcript_comparison.example.json",
        "segment_transcript_comparison",
        project_id=layout.root.name,
        revision_id="rev_002",
    )
    segment_qa = example(
        "segment_qa_report.example.json",
        "segment_qa_report",
        project_id=layout.root.name,
        revision_id="rev_002",
    )
    visual_qa = example(
        "segment_visual_qa_report.example.json",
        "segment_visual_qa_report",
        project_id=layout.root.name,
        revision_id="rev_002",
    )
    composition = layout.work / "composition-bundle.js"
    composition.write_bytes(b"composition-bundle")
    return package_path, comparison, segment_qa, visual_qa, composition, layout.root, effect_summary


def test_gate2_approval_binds_all_current_hashes_and_reuses(tmp_path: Path) -> None:
    package, comparison, segment_qa, visual_qa, composition, root, _effect = _fixture(tmp_path)
    from videoedit.services.project import ProjectLayout

    layout = ProjectLayout(root)
    approval_path = approve_segment_gate2(
        ROOT,
        layout,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
        actor="operator@example.test",
        role="editor",
        notes="Reviewed the segment evidence.",
    )
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_review", approval)
    assert approval["decision"] == "approved"
    assert approval["locked"] is False
    assert approval["bound_hashes"]["review_package_sha256"] == sha256_file(package)
    assert approval["bound_hashes"]["visual_qa_sha256"] == sha256_file(visual_qa)
    assert approval["bound_hashes"]["composition_bundle_sha256"] == sha256_file(composition)

    lock_path = lock_segment_revision(
        ROOT,
        layout,
        approval_path,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
    )
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "segment_lock", lock)
    assert lock["locked"] is True
    assert lock["review"]["sha256"] == sha256_file(approval_path)

    second = approve_segment_gate2(
        ROOT,
        layout,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
        actor="operator@example.test",
        role="editor",
        notes="Reviewed the segment evidence.",
    )
    assert second == approval_path
    assert (
        lock_segment_revision(
            ROOT,
            layout,
            approval_path,
            package,
            comparison,
            segment_qa,
            visual_qa,
            composition,
        )
        == lock_path
    )


def test_gate2_rejects_transcript_comparison_for_another_segment(tmp_path: Path) -> None:
    package, comparison, segment_qa, visual_qa, composition, root, _effect = _fixture(tmp_path)
    payload = json.loads(comparison.read_text(encoding="utf-8"))
    payload["scope"]["segment_id"] = "segment_000002"
    write_validated_artifact(ROOT, "segment_transcript_comparison", comparison, payload)

    from videoedit.services.project import ProjectLayout

    with pytest.raises(PlanningValidationError, match="another segment"):
        approve_segment_gate2(
            ROOT,
            ProjectLayout(root),
            package,
            comparison,
            segment_qa,
            visual_qa,
            composition,
            actor="operator@example.test",
            role="editor",
        )


def test_gate3_rejects_lock_with_tampered_gate2_bound_hashes(tmp_path: Path) -> None:
    package, comparison, segment_qa, visual_qa, composition, root, _effect = _fixture(tmp_path)
    from videoedit.services.project import ProjectLayout

    layout = ProjectLayout(root)
    approval_path = approve_segment_gate2(
        ROOT,
        layout,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
        actor="operator@example.test",
        role="editor",
    )
    lock_path = lock_segment_revision(
        ROOT,
        layout,
        approval_path,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
    )
    assert _validate_gate2_lock(ROOT, layout, lock_path, revision_id="rev_002")

    tampered = json.loads(lock_path.read_text(encoding="utf-8"))
    tampered["bound_hashes"]["preview_sha256"] = "f" * 64
    lock_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(PlanningValidationError, match="bound hashes do not match"):
        _validate_gate2_lock(ROOT, layout, lock_path, revision_id="rev_002")


def test_gate2_rejects_tampered_reviewer_binding_on_cache_reuse(tmp_path: Path) -> None:
    package, comparison, segment_qa, visual_qa, composition, root, _effect = _fixture(tmp_path)
    from videoedit.services.project import ProjectLayout

    layout = ProjectLayout(root)
    approval_path = approve_segment_gate2(
        ROOT,
        layout,
        package,
        comparison,
        segment_qa,
        visual_qa,
        composition,
        actor="operator@example.test",
        role="editor",
        notes="Reviewed the segment evidence.",
    )
    tampered = json.loads(approval_path.read_text(encoding="utf-8"))
    tampered["reviewer"]["actor"] = "different@example.test"
    write_validated_artifact(ROOT, "segment_review", approval_path, tampered)

    with pytest.raises(StateConflictError, match="Gate 2 decision path exists with stale contents"):
        approve_segment_gate2(
            ROOT,
            layout,
            package,
            comparison,
            segment_qa,
            visual_qa,
            composition,
            actor="operator@example.test",
            role="editor",
            notes="Reviewed the segment evidence.",
        )
