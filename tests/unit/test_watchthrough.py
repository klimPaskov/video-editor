from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.artifacts import validate_artifact
from videoedit.services.project import initialize_project
from videoedit.services.watchthrough import record_watchthrough

ROOT = Path(__file__).resolve().parents[2]


def test_watchthrough_record_is_hash_bound_and_idempotent(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "watchthrough_fixture")
    candidate = layout.output / "candidate.mp4"
    evidence = layout.review / "candidate-contact-sheet.png"
    candidate.write_bytes(b"candidate media")
    evidence.write_bytes(b"decoded contact sheet")

    record_path = record_watchthrough(
        ROOT,
        layout,
        candidate,
        actor="operator@example.test",
        role="editor",
        evidence_paths=[evidence],
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    validate_artifact(ROOT, "watchthrough_record", record)
    assert record["status"] == "complete"
    assert record["decision"] == "pass"
    assert record["candidate"]["sha256"]
    assert record["evidence"][0]["sha256"]

    second_path = record_watchthrough(
        ROOT,
        layout,
        candidate,
        actor="operator@example.test",
        role="editor",
        evidence_paths=[evidence],
    )
    assert second_path == record_path
    assert json.loads(second_path.read_text(encoding="utf-8")) == record


def test_approved_equivalent_watchthrough_requires_explanation(tmp_path: Path) -> None:
    layout = initialize_project(tmp_path, "watchthrough_fixture")
    candidate = layout.output / "candidate.mp4"
    candidate.write_bytes(b"candidate media")

    with pytest.raises(PlanningValidationError, match="approved equivalent review"):
        record_watchthrough(
            ROOT,
            layout,
            candidate,
            actor="operator@example.test",
            role="editor",
            protocol="approved_equivalent",
            notes="",
        )
