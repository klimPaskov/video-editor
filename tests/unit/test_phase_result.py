from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from videoedit.services.phase_result import write_phase_result


def test_phase_result_is_schema_validated_and_promoted_atomically(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    payload = {
        "schema_name": "codex_phase_result",
        "schema_version": "1.0.0",
        "phase_id": "P0",
        "status": "partial",
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "tasks_completed": ["P0-03"],
        "scope_summary": "Process adapter contract test",
        "files_changed": ["src/videoedit/adapters/process.py"],
        "tests": [{"command": "pytest", "exit_code": 0, "summary": "passed"}],
        "acceptance_criteria": [
            {"criterion": "process contract", "status": "pass", "evidence": "unit test"}
        ],
        "unresolved_risks": [],
        "decisions_needed": [],
        "next_recommended_task": "P0-04",
        "notes": [],
    }
    destination = write_phase_result(package_root, tmp_path / "P0.json", payload)
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
