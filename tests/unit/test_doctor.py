from __future__ import annotations

from pathlib import Path

from videoedit.adapters.process import ProcessRequest, ProcessResult
from videoedit.services.doctor import (
    DoctorCheck,
    DoctorReport,
    _amd_amf_check,
    _cuda_gpu_probe,
    _whisper_check,
    _worker_runtime_check,
)
from videoedit.settings import Settings


def test_doctor_distinguishes_whisper_package_from_model_readiness(tmp_path: Path) -> None:
    missing_model = _whisper_check(
        Settings(workspace=tmp_path, whisper_model_path=None),
        tmp_path,
        module_available=True,
    )
    assert missing_model.status == "warning"
    assert missing_model.code == "whisper_model_missing"

    model = tmp_path / "whisper-small.pt"
    model.write_bytes(b"fixture whisper model")
    ready = _whisper_check(
        Settings(workspace=tmp_path, whisper_model_path=model),
        tmp_path,
        module_available=True,
    )
    assert ready.status == "pass"
    assert ready.code == "whisper_ready"
    assert ready.evidence["model_sha256"]


def test_doctor_reports_missing_whisper_package_before_model_gate(tmp_path: Path) -> None:
    check = _whisper_check(
        Settings(workspace=tmp_path, whisper_model_path=tmp_path / "model.pt"),
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


class _AmfRunner:
    def __init__(self, *, exit_code: int) -> None:
        self.exit_code = exit_code
        self.request: ProcessRequest | None = None

    def run(self, request: ProcessRequest) -> ProcessResult:
        self.request = request
        return ProcessResult(
            arguments=(request.executable, *request.arguments),
            exit_code=self.exit_code,
            stdout="",
            stderr="AMF initialisation succeeded via D3D11."
            if self.exit_code == 0
            else "AMF failed",
            elapsed_ms=12,
        )


def test_amd_amf_check_records_a_bounded_success(tmp_path: Path, monkeypatch) -> None:
    runner = _AmfRunner(exit_code=0)
    monkeypatch.setattr("videoedit.services.doctor._binary_path", lambda _path: "ffmpeg")

    check = _amd_amf_check(
        configured_path="ffmpeg",
        runner=runner,
        working_directory=tmp_path,
    )

    assert check.status == "pass"
    assert check.code == "amd_amf_ready"
    assert check.required is False
    assert runner.request is not None
    assert "h264_amf" in runner.request.arguments
    assert "format=nv12" in runner.request.arguments
    assert "-" in runner.request.arguments


def test_amd_amf_check_is_optional_when_runtime_fails(tmp_path: Path, monkeypatch) -> None:
    runner = _AmfRunner(exit_code=1)
    monkeypatch.setattr("videoedit.services.doctor._binary_path", lambda _path: "ffmpeg")

    check = _amd_amf_check(
        configured_path="ffmpeg",
        runner=runner,
        working_directory=tmp_path,
    )

    assert check.status == "warning"
    assert check.code == "amd_amf_probe_failed"
    assert check.required is False


def test_worker_runtime_check_reports_missing_cuda_and_isolation_without_starting_worker(
    tmp_path: Path,
) -> None:
    check = _worker_runtime_check(
        worker="sam3",
        command="",
        package_root=tmp_path,
        runner=_AmfRunner(exit_code=0),
        working_directory=tmp_path,
        gpu_probe={"available": False, "reason": "nvidia_smi_missing"},
    )

    assert check.status == "warning"
    assert check.code == "worker_prerequisites_missing"
    assert check.required is False
    assert "isolated_python_missing" in check.evidence["blockers"]
    assert "nvidia_cuda_gpu_missing" in check.evidence["blockers"]
    assert check.evidence["upstream"]["immutable_ref_configured"] is False  # type: ignore[index]


def test_cuda_gpu_probe_uses_bounded_typed_command(tmp_path: Path, monkeypatch) -> None:
    runner_output = "NVIDIA RTX, 555.1, 16384 MiB"

    class NvidiaRunner(_AmfRunner):
        def run(self, request: ProcessRequest) -> ProcessResult:
            self.request = request
            return ProcessResult(
                arguments=(request.executable, *request.arguments),
                exit_code=0,
                stdout=runner_output,
                stderr="",
                elapsed_ms=2,
            )

    nvidia_runner = NvidiaRunner(exit_code=0)
    monkeypatch.setattr(
        "videoedit.services.doctor._binary_path",
        lambda path: "nvidia-smi" if path == "nvidia-smi" else None,
    )

    probe = _cuda_gpu_probe(runner=nvidia_runner, working_directory=tmp_path)

    assert probe["available"] is True
    assert nvidia_runner.request is not None
    assert nvidia_runner.request.arguments == (
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader",
    )
