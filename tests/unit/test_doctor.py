from __future__ import annotations

from pathlib import Path

from videoedit.services.doctor import DoctorCheck, DoctorReport, _whisper_check
from videoedit.settings import Settings


def test_doctor_distinguishes_whisper_package_from_model_readiness(tmp_path: Path) -> None:
    missing_model = _whisper_check(
        Settings(_env_file=None, workspace=tmp_path, whisper_model_path=None),
        tmp_path,
        module_available=True,
    )
    assert missing_model.status == "warning"
    assert missing_model.code == "whisper_model_missing"

    model = tmp_path / "whisper-small.pt"
    model.write_bytes(b"fixture whisper model")
    ready = _whisper_check(
        Settings(_env_file=None, workspace=tmp_path, whisper_model_path=model),
        tmp_path,
        module_available=True,
    )
    assert ready.status == "pass"
    assert ready.code == "whisper_ready"
    assert ready.evidence["model_sha256"]


def test_doctor_reports_missing_whisper_package_before_model_gate(tmp_path: Path) -> None:
    check = _whisper_check(
        Settings(_env_file=None, workspace=tmp_path, whisper_model_path=tmp_path / "model.pt"),
        tmp_path,
        module_available=False,
    )
    assert check.status == "warning"
    assert check.code == "whisper_optional_missing"


def test_doctor_required_warning_blocks_readiness() -> None:
    report = DoctorReport(
        (
            DoctorCheck(
                name="Node.js 22",
                status="warning",
                required=True,
                code="node_version_mismatch",
                message="Node.js 24 is installed; Node.js 22 is the target",
                evidence={},
            ),
        )
    )

    assert report.failed
    assert report.as_payload()["status"] == "error"
