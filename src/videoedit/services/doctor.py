from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from videoedit.adapters.process import (
    LocalProcessRunner,
    ProcessRequest,
    ProcessResult,
    ProcessRunner,
    parse_command,
)
from videoedit.errors import VideoeditError
from videoedit.services.project import sha256_file
from videoedit.settings import Settings

CheckStatus = Literal["pass", "warning", "fail"]

_WORKER_RUNTIME_SPECS: dict[str, dict[str, str]] = {
    "sam3": {
        "display_name": "SAM 3.1",
        "python": "3.12",
        "ref_variable": "SAM3_REF",
    },
    "matanyone2": {
        "display_name": "MatAnyone 2",
        "python": "3.10",
        "ref_variable": "MATANYONE2_REF",
    },
}


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    required: bool
    code: str
    message: str
    evidence: dict[str, object]
    repair_hint: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
            "repair_hint": self.repair_hint,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def failed(self) -> bool:
        return any(check.required and check.status != "pass" for check in self.checks)

    def as_payload(self) -> dict[str, object]:
        warnings = [check.message for check in self.checks if check.status == "warning"]
        errors = [check.message for check in self.checks if check.status == "fail"]
        return {
            "command": "videoedit doctor",
            "status": "error" if self.failed else "ok",
            "data": {"checks": [check.as_dict() for check in self.checks]},
            "warnings": warnings,
            "errors": errors,
        }


def _binary_path(configured_path: str) -> str | None:
    resolved = shutil.which(configured_path)
    if resolved:
        return resolved
    candidate = Path(configured_path).expanduser()
    return str(candidate.resolve()) if candidate.is_file() else None


def _command_evidence(result: ProcessResult) -> dict[str, object]:
    return {
        "arguments": list(result.arguments),
        "exit_code": result.exit_code,
        "elapsed_ms": result.elapsed_ms,
        "stdout": result.stdout[:500],
        "stderr": result.stderr[-500:],
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


def _run_check(
    *,
    name: str,
    configured_path: str,
    arguments: tuple[str, ...],
    required: bool,
    code: str,
    runner: ProcessRunner,
    working_directory: Path,
    repair_hint: str,
) -> tuple[DoctorCheck, ProcessResult | None]:
    resolved = _binary_path(configured_path)
    if resolved is None:
        status: CheckStatus = "fail" if required else "warning"
        return (
            DoctorCheck(
                name=name,
                status=status,
                required=required,
                code=f"{code}_missing",
                message=f"{name} is not available: {configured_path}",
                evidence={"configured_path": configured_path},
                repair_hint=repair_hint,
            ),
            None,
        )
    try:
        result = runner.run(
            ProcessRequest(
                executable=resolved,
                arguments=arguments,
                working_directory=working_directory,
                timeout_seconds=30,
                stdout_limit_bytes=200_000,
                stderr_limit_bytes=200_000,
            )
        )
    except VideoeditError as exc:
        status = "fail" if required else "warning"
        return (
            DoctorCheck(
                name=name,
                status=status,
                required=required,
                code=f"{code}_unrunnable",
                message=f"{name} could not be run: {exc.message}",
                evidence={"path": resolved},
                repair_hint=repair_hint,
            ),
            None,
        )
    if result.exit_code != 0:
        status = "fail" if required else "warning"
        return (
            DoctorCheck(
                name=name,
                status=status,
                required=required,
                code=f"{code}_failed",
                message=f"{name} exited with code {result.exit_code}",
                evidence={"path": resolved, **_command_evidence(result)},
                repair_hint=repair_hint,
            ),
            result,
        )
    return (
        DoctorCheck(
            name=name,
            status="pass",
            required=required,
            code=f"{code}_ok",
            message=f"{name} is available",
            evidence={"path": resolved, **_command_evidence(result)},
            repair_hint=None,
        ),
        result,
    )


def _capability_check(
    *,
    name: str,
    configured_path: str,
    arguments: tuple[str, ...],
    required_items: tuple[str, ...],
    runner: ProcessRunner,
    working_directory: Path,
) -> DoctorCheck:
    resolved = _binary_path(configured_path)
    if resolved is None:
        return DoctorCheck(
            name=name,
            status="fail",
            required=True,
            code="ffmpeg_missing",
            message=f"Cannot inspect {name}; FFmpeg is unavailable",
            evidence={"configured_path": configured_path},
            repair_hint="Install FFmpeg and configure VIDEOEDIT_FFMPEG_PATH",
        )
    try:
        result = runner.run(
            ProcessRequest(
                executable=resolved,
                arguments=arguments,
                working_directory=working_directory,
                timeout_seconds=30,
                stdout_limit_bytes=500_000,
                stderr_limit_bytes=500_000,
            )
        )
    except VideoeditError as exc:
        return DoctorCheck(
            name=name,
            status="fail",
            required=True,
            code="capability_unavailable",
            message=f"Could not inspect {name}: {exc.message}",
            evidence={"path": resolved},
            repair_hint="Install an FFmpeg build with the required capabilities",
        )
    combined = f"{result.stdout}\n{result.stderr}"
    missing = tuple(item for item in required_items if item not in combined)
    if result.exit_code != 0 or missing:
        return DoctorCheck(
            name=name,
            status="fail",
            required=True,
            code="capability_missing",
            message=f"{name} is missing: {', '.join(missing) or 'command failed'}",
            evidence={
                "path": resolved,
                "missing": list(missing),
                **_command_evidence(result),
            },
            repair_hint="Install an FFmpeg build with the required capabilities",
        )
    return DoctorCheck(
        name=name,
        status="pass",
        required=True,
        code="capability_ok",
        message=f"{name} capabilities are available",
        evidence={"path": resolved, "required": list(required_items)},
    )


def _workspace_checks(workspace: Path) -> tuple[DoctorCheck, DoctorCheck]:
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".videoedit-doctor-", suffix=".tmp", dir=workspace, delete=True
        ) as handle:
            handle.write(b"videoedit doctor")
            handle.flush()
        writable = DoctorCheck(
            name="Workspace writable",
            status="pass",
            required=True,
            code="workspace_writable",
            message="Configured workspace is writable",
            evidence={"path": str(workspace)},
        )
    except OSError as exc:
        writable = DoctorCheck(
            name="Workspace writable",
            status="fail",
            required=True,
            code="workspace_not_writable",
            message=f"Configured workspace is not writable: {exc}",
            evidence={"path": str(workspace)},
            repair_hint="Choose a writable workspace path",
        )
    try:
        usage = shutil.disk_usage(workspace)
        free_gib = usage.free / (1024**3)
        disk_status: CheckStatus = "pass" if usage.free >= 512 * 1024**2 else "warning"
        disk = DoctorCheck(
            name="Workspace disk",
            status=disk_status,
            required=False,
            code="disk_space_ok" if disk_status == "pass" else "disk_space_low",
            message=f"Workspace has {free_gib:.2f} GiB free",
            evidence={"free_bytes": usage.free, "total_bytes": usage.total},
            repair_hint="Free disk space before rendering media"
            if disk_status == "warning"
            else None,
        )
    except OSError as exc:
        disk = DoctorCheck(
            name="Workspace disk",
            status="warning",
            required=False,
            code="disk_space_unknown",
            message=f"Could not inspect workspace disk space: {exc}",
            evidence={"path": str(workspace)},
            repair_hint="Check available disk space manually",
        )
    return writable, disk


def _font_check(workspace: Path, runner: ProcessRunner) -> DoctorCheck:
    windows_root = Path(os.environ.get("WINDIR", "C:\\Windows"))
    candidates = (
        windows_root / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    existing = next((path for path in candidates if path.is_file()), None)
    if existing is not None:
        return DoctorCheck(
            name="Font availability",
            status="pass",
            required=False,
            code="font_available",
            message="A baseline local font is available",
            evidence={"path": str(existing)},
        )
    fc_match = _binary_path("fc-match")
    if fc_match is not None:
        try:
            result = runner.run(
                ProcessRequest(
                    executable=fc_match,
                    arguments=("Arial",),
                    working_directory=workspace,
                    timeout_seconds=10,
                    stdout_limit_bytes=10_000,
                    stderr_limit_bytes=10_000,
                )
            )
            if result.exit_code == 0 and result.stdout.strip():
                return DoctorCheck(
                    name="Font availability",
                    status="pass",
                    required=False,
                    code="font_available",
                    message="Fontconfig can resolve a baseline font",
                    evidence={"match": result.stdout.strip()[:500]},
                )
        except VideoeditError:
            pass
    return DoctorCheck(
        name="Font availability",
        status="warning",
        required=False,
        code="font_missing",
        message="No baseline local font was detected",
        evidence={"checked": [str(path) for path in candidates]},
        repair_hint="Install or configure a licensed local font before visual rendering",
    )


def _worker_check(name: str, command: str) -> DoctorCheck:
    if not command.strip():
        return DoctorCheck(
            name=name,
            status="warning",
            required=False,
            code="worker_not_configured",
            message=f"{name} is not configured",
            evidence={},
            repair_hint=(
                "Configure the worker only after its licence, checkpoint, and GPU "
                "prerequisites pass"
            ),
        )
    try:
        parts = parse_command(command)
    except ValueError as exc:
        return DoctorCheck(
            name=name,
            status="warning",
            required=False,
            code="worker_command_invalid",
            message=f"{name} command is invalid: {exc}",
            evidence={},
            repair_hint="Use an executable plus argument array syntax in worker configuration",
        )
    resolved = _binary_path(parts[0])
    if resolved is None:
        return DoctorCheck(
            name=name,
            status="warning",
            required=False,
            code="worker_executable_missing",
            message=f"{name} executable is unavailable: {parts[0]}",
            evidence={"command": list(parts)},
            repair_hint="Install the worker in its isolated environment before enabling it",
        )
    return DoctorCheck(
        name=name,
        status="warning",
        required=False,
        code="worker_configured_not_run",
        message=f"{name} is configured but not run by doctor",
        evidence={"command": [resolved, *parts[1:]]},
        repair_hint=(
            "Run only an approved, licensed, bounded worker smoke test in its own environment"
        ),
    )


def _cuda_gpu_probe(*, runner: ProcessRunner, working_directory: Path) -> dict[str, object]:
    """Probe the optional CUDA target without importing or starting a model worker."""

    configured_path = "nvidia-smi"
    resolved = _binary_path(configured_path)
    if resolved is None:
        return {
            "available": False,
            "path": None,
            "reason": "nvidia_smi_missing",
        }
    try:
        result = runner.run(
            ProcessRequest(
                executable=resolved,
                arguments=(
                    "--query-gpu=name,driver_version,memory.total",
                    "--format=csv,noheader",
                ),
                working_directory=working_directory,
                timeout_seconds=30,
                stdout_limit_bytes=20_000,
                stderr_limit_bytes=20_000,
            )
        )
    except VideoeditError as exc:
        return {
            "available": False,
            "path": resolved,
            "reason": "nvidia_smi_unrunnable",
            "error": exc.message,
        }
    if result.exit_code != 0 or not result.stdout.strip():
        return {
            "available": False,
            "path": resolved,
            "reason": "nvidia_smi_failed",
            **_command_evidence(result),
        }
    return {
        "available": True,
        "path": resolved,
        **_command_evidence(result),
    }


def _worker_runtime_check(
    *,
    worker: str,
    command: str,
    package_root: Path,
    runner: ProcessRunner,
    working_directory: Path,
    gpu_probe: dict[str, object],
) -> DoctorCheck:
    """Report optional worker readiness without treating media-GPU evidence as CUDA readiness."""

    spec = _WORKER_RUNTIME_SPECS[worker]
    display_name = spec["display_name"]
    worker_root = package_root / "workers" / worker
    environment_path = worker_root / ".venv"
    python_candidates = (
        environment_path / "Scripts" / "python.exe",
        environment_path / "bin" / "python",
    )
    python_path = next((path for path in python_candidates if path.is_file()), None)
    upstream_path = worker_root / "upstream"
    checkpoint_path = worker_root / "models"
    checkpoint_candidates = (
        sum(1 for path in checkpoint_path.iterdir() if path.suffix.lower() in {".pt", ".pth"})
        if checkpoint_path.is_dir()
        else 0
    )
    configured_ref = os.environ.get(spec["ref_variable"], "")
    ref_is_immutable = re.fullmatch(r"[0-9a-fA-F]{40}", configured_ref) is not None
    blockers: list[str] = []
    python_version: str | None = None
    python_evidence: dict[str, object] = {
        "path": str(python_path) if python_path else None,
        "expected": spec["python"],
        "environment_path": str(environment_path),
        "environment_exists": environment_path.is_dir(),
    }
    if python_path is None:
        blockers.append("isolated_python_missing")
    else:
        try:
            result = runner.run(
                ProcessRequest(
                    executable=str(python_path),
                    arguments=("--version",),
                    working_directory=working_directory,
                    timeout_seconds=30,
                    stdout_limit_bytes=10_000,
                    stderr_limit_bytes=10_000,
                )
            )
            python_version = (result.stdout or result.stderr).strip()
            python_evidence["version"] = python_version
            python_evidence.update(_command_evidence(result))
            version_match = re.search(r"Python\s+(\d+\.\d+)", python_version)
            if result.exit_code != 0 or version_match is None:
                blockers.append("isolated_python_unrunnable")
            elif version_match.group(1) != spec["python"]:
                blockers.append("isolated_python_version_mismatch")
        except VideoeditError as exc:
            python_evidence["error"] = exc.message
            blockers.append("isolated_python_unrunnable")

    upstream_evidence = {
        "path": str(upstream_path),
        "git_checkout": (upstream_path / ".git").is_dir(),
        "immutable_ref_configured": ref_is_immutable,
        "ref_variable": spec["ref_variable"],
    }
    if not upstream_evidence["git_checkout"]:
        blockers.append("upstream_checkout_missing")
    if not ref_is_immutable:
        blockers.append("operator_approved_immutable_ref_missing")
    if not bool(gpu_probe.get("available")):
        blockers.append("nvidia_cuda_gpu_missing")
    if checkpoint_candidates == 0:
        blockers.append("local_checkpoint_missing")

    command_check = _worker_check(display_name + " worker", command)
    if command_check.code in {
        "worker_not_configured",
        "worker_command_invalid",
        "worker_executable_missing",
    }:
        blockers.append(command_check.code)

    evidence: dict[str, object] = {
        "worker": worker,
        "python": python_evidence,
        "upstream": upstream_evidence,
        "checkpoint": {
            "directory": str(checkpoint_path),
            "candidate_file_count": checkpoint_candidates,
            "sha256_binding": "job/runtime-approval scoped; not inferred by doctor",
        },
        "cuda_gpu": gpu_probe,
        "command": command_check.as_dict(),
        "blockers": blockers,
    }
    if blockers:
        return DoctorCheck(
            name=f"{display_name} worker",
            status="warning",
            required=False,
            code="worker_prerequisites_missing",
            message=f"{display_name} worker is not ready: {', '.join(blockers)}",
            evidence=evidence,
            repair_hint=(
                "Resolve the listed operator, licence, immutable-ref, checkpoint, "
                "isolated-runtime, "
                "and compatible NVIDIA/CUDA prerequisites before enabling this worker; "
                "AMD AMF is "
                "media encoding evidence only"
            ),
        )
    return DoctorCheck(
        name=f"{display_name} worker",
        status="warning",
        required=False,
        code="worker_ready_not_run",
        message=(
            f"{display_name} worker prerequisites are present but the worker was not run by doctor"
        ),
        evidence=evidence,
        repair_hint=(
            "Run only an approved, licensed, bounded worker smoke test and review its outputs"
        ),
    )


def _whisper_check(
    settings: Settings,
    workspace: Path,
    *,
    module_available: bool | None = None,
) -> DoctorCheck:
    available = (
        importlib.util.find_spec("whisper") is not None
        if module_available is None
        else module_available
    )
    model_path = settings.whisper_model_path
    if model_path is not None and not model_path.is_absolute():
        model_path = workspace / model_path
    if not available:
        return DoctorCheck(
            name="Whisper adapter",
            status="warning",
            required=False,
            code="whisper_optional_missing",
            message="Local Whisper is not installed; transcription remains unavailable",
            evidence={"module": "whisper", "model_path": str(model_path) if model_path else None},
            repair_hint="Install the optional local Whisper extra before Phase 2",
        )
    if model_path is None:
        return DoctorCheck(
            name="Whisper adapter",
            status="warning",
            required=False,
            code="whisper_model_missing",
            message=(
                "Local Whisper is installed, but no local model path is configured; "
                "transcription remains unavailable"
            ),
            evidence={"module": "whisper", "model_path": None},
            repair_hint="Set VIDEOEDIT_WHISPER_MODEL_PATH to an operator-supplied local model",
        )
    resolved_model = model_path.expanduser().resolve()
    if not resolved_model.is_file():
        return DoctorCheck(
            name="Whisper adapter",
            status="warning",
            required=False,
            code="whisper_model_path_missing",
            message=f"Configured local Whisper model does not exist: {resolved_model}",
            evidence={"module": "whisper", "model_path": str(resolved_model)},
            repair_hint="Provide a readable operator-supplied local Whisper model file",
        )
    return DoctorCheck(
        name="Whisper adapter",
        status="pass",
        required=False,
        code="whisper_ready",
        message="Local Whisper and its supplied model are available",
        evidence={
            "module": "whisper",
            "model_path": str(resolved_model),
            "model_sha256": sha256_file(resolved_model),
            "model_size_bytes": resolved_model.stat().st_size,
        },
        repair_hint=None,
    )


def _amd_amf_check(
    *,
    configured_path: str,
    runner: ProcessRunner,
    working_directory: Path,
) -> DoctorCheck:
    """Run a bounded, output-free AMD AMF encode probe.

    AMF is an optional media-engine acceleration path.  It is deliberately not
    treated as evidence that the isolated CUDA model workers can run.
    """

    resolved = _binary_path(configured_path)
    if resolved is None:
        return DoctorCheck(
            name="FFmpeg AMD AMF",
            status="warning",
            required=False,
            code="ffmpeg_missing_for_amf",
            message="FFmpeg AMD AMF was not checked because FFmpeg is unavailable",
            evidence={"configured_path": configured_path},
            repair_hint=(
                "Install an FFmpeg build with AMD AMF support if GPU media encoding is required"
            ),
        )
    try:
        result = runner.run(
            ProcessRequest(
                executable=resolved,
                arguments=(
                    "-hide_banner",
                    "-loglevel",
                    "verbose",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=30",
                    "-vf",
                    "format=nv12",
                    "-frames:v",
                    "1",
                    "-an",
                    "-c:v",
                    "h264_amf",
                    "-b:v",
                    "2M",
                    "-f",
                    "null",
                    "-",
                ),
                working_directory=working_directory,
                timeout_seconds=60,
                stdout_limit_bytes=100_000,
                stderr_limit_bytes=200_000,
            )
        )
    except VideoeditError as exc:
        return DoctorCheck(
            name="FFmpeg AMD AMF",
            status="warning",
            required=False,
            code="amd_amf_unrunnable",
            message=f"FFmpeg AMD AMF probe could not be run: {exc.message}",
            evidence={"path": resolved},
            repair_hint="Keep the software encoder or install a compatible AMD FFmpeg runtime",
        )
    if result.exit_code != 0:
        return DoctorCheck(
            name="FFmpeg AMD AMF",
            status="warning",
            required=False,
            code="amd_amf_probe_failed",
            message=f"FFmpeg AMD AMF probe exited with code {result.exit_code}",
            evidence={"path": resolved, **_command_evidence(result)},
            repair_hint=(
                "Keep the software encoder; inspect the bounded AMF diagnostics before "
                "enabling AMD encoding"
            ),
        )
    return DoctorCheck(
        name="FFmpeg AMD AMF",
        status="pass",
        required=False,
        code="amd_amf_ready",
        message="FFmpeg AMD AMF produced a bounded test frame",
        evidence={"path": resolved, **_command_evidence(result)},
        repair_hint=(
            "AMD AMF remains an explicit media-engine option and does not authorize CUDA "
            "model workers"
        ),
    )


def run_doctor(
    settings: Settings,
    *,
    package_root: Path,
    runner: ProcessRunner | None = None,
) -> DoctorReport:
    process_runner = runner or LocalProcessRunner()
    workspace = settings.workspace.resolve()
    remotion_directory = settings.remotion_directory
    if not remotion_directory.is_absolute():
        remotion_directory = (Path.cwd() / remotion_directory).resolve()

    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            name="Python 3.11",
            status="pass" if sys.version_info[:2] == (3, 11) else "fail",
            required=True,
            code="python_version",
            message=(
                f"Running Python {sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
            evidence={"version": sys.version.split()[0]},
            repair_hint="Run the core with Python 3.11"
            if sys.version_info[:2] != (3, 11)
            else None,
        )
    )
    writable, disk = _workspace_checks(workspace)
    checks.extend((writable, disk))

    ffmpeg_check, ffmpeg_result = _run_check(
        name="FFmpeg",
        configured_path=settings.ffmpeg_path,
        arguments=("-version",),
        required=True,
        code="ffmpeg",
        runner=process_runner,
        working_directory=workspace,
        repair_hint="Install FFmpeg and configure VIDEOEDIT_FFMPEG_PATH",
    )
    checks.append(ffmpeg_check)
    ffprobe_check, _ = _run_check(
        name="ffprobe",
        configured_path=settings.ffprobe_path,
        arguments=("-version",),
        required=True,
        code="ffprobe",
        runner=process_runner,
        working_directory=workspace,
        repair_hint="Install ffprobe and configure VIDEOEDIT_FFPROBE_PATH",
    )
    checks.append(ffprobe_check)
    if ffmpeg_result is not None and ffmpeg_result.exit_code == 0:
        checks.append(
            _capability_check(
                name="FFmpeg filters",
                configured_path=settings.ffmpeg_path,
                arguments=("-hide_banner", "-filters"),
                required_items=(
                    "silencedetect",
                    "chromakey",
                    "alphamerge",
                    "maskedmerge",
                    "overlay",
                ),
                runner=process_runner,
                working_directory=workspace,
            )
        )
        checks.append(
            _capability_check(
                name="FFmpeg codecs",
                configured_path=settings.ffmpeg_path,
                arguments=("-hide_banner", "-encoders"),
                required_items=("libx264", "aac", "ffv1"),
                runner=process_runner,
                working_directory=workspace,
            )
        )
        checks.append(
            _amd_amf_check(
                configured_path=settings.ffmpeg_path,
                runner=process_runner,
                working_directory=workspace,
            )
        )
    else:
        checks.extend(
            (
                DoctorCheck(
                    name="FFmpeg filters",
                    status="fail",
                    required=True,
                    code="ffmpeg_unavailable",
                    message="FFmpeg filters were not inspected",
                    evidence={},
                    repair_hint="Repair FFmpeg before running doctor again",
                ),
                DoctorCheck(
                    name="FFmpeg codecs",
                    status="fail",
                    required=True,
                    code="ffmpeg_unavailable",
                    message="FFmpeg codecs were not inspected",
                    evidence={},
                    repair_hint="Repair FFmpeg before running doctor again",
                ),
            )
        )

    node_check, node_result = _run_check(
        name="Node.js 22",
        configured_path=settings.node_path,
        arguments=("--version",),
        required=True,
        code="node",
        runner=process_runner,
        working_directory=workspace,
        repair_hint="Install Node.js 22 and configure VIDEOEDIT_NODE_PATH",
    )
    if node_result is not None and node_result.exit_code == 0:
        version_match = re.search(r"(?:^|\s)v?(\d+)(?:\.\d+){0,2}", node_result.stdout)
        if version_match is None or version_match.group(1) != "22":
            node_check = replace(
                node_check,
                status="fail",
                code="node_version_mismatch",
                message=(
                    f"Node.js {node_result.stdout.strip()} is installed; Node.js 22 is the target"
                ),
                repair_hint="Use Node.js 22 for the supported Remotion runtime",
            )
    npm_check, _ = _run_check(
        name="npm",
        configured_path=settings.npm_path,
        arguments=("--version",),
        required=True,
        code="npm",
        runner=process_runner,
        working_directory=workspace,
        repair_hint="Install npm and configure VIDEOEDIT_NPM_PATH",
    )
    checks.extend((node_check, npm_check))
    package_json = remotion_directory / "package.json"
    lockfile = remotion_directory / "package-lock.json"
    node_modules = remotion_directory / "node_modules"
    remotion_missing = [
        label
        for label, path in (
            ("package.json", package_json),
            ("package-lock.json", lockfile),
        )
        if not path.is_file()
    ]
    if remotion_missing:
        checks.append(
            DoctorCheck(
                name="Remotion project",
                status="fail",
                required=True,
                code="remotion_project_incomplete",
                message=f"Remotion project is missing: {', '.join(remotion_missing)}",
                evidence={"directory": str(remotion_directory)},
                repair_hint="Create the lockfile and install the declared Remotion dependencies",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="Remotion project",
                status="pass" if node_modules.is_dir() else "warning",
                required=False,
                code="remotion_ready" if node_modules.is_dir() else "remotion_dependencies_missing",
                message="Remotion project and lockfile are present"
                if node_modules.is_dir()
                else "Remotion project is present but node_modules is not installed",
                evidence={
                    "directory": str(remotion_directory),
                    "package_json": str(package_json),
                    "lockfile": str(lockfile),
                    "node_modules": node_modules.is_dir(),
                },
                repair_hint=None if node_modules.is_dir() else "Run npm ci inside remotion/",
            )
        )
    checks.append(_whisper_check(settings, workspace))
    gpu_probe = _cuda_gpu_probe(runner=process_runner, working_directory=workspace)
    checks.extend(
        (
            _font_check(workspace, process_runner),
            _worker_runtime_check(
                worker="sam3",
                command=settings.sam3_worker_command,
                package_root=package_root,
                runner=process_runner,
                working_directory=workspace,
                gpu_probe=gpu_probe,
            ),
            _worker_runtime_check(
                worker="matanyone2",
                command=settings.matanyone_worker_command,
                package_root=package_root,
                runner=process_runner,
                working_directory=workspace,
                gpu_probe=gpu_probe,
            ),
        )
    )
    if not package_root.is_dir():
        checks.append(
            DoctorCheck(
                name="Repository contracts",
                status="fail",
                required=True,
                code="package_root_missing",
                message=f"Repository root is unavailable: {package_root}",
                evidence={},
                repair_hint="Run videoedit from the repository root",
            )
        )
    return DoctorReport(tuple(checks))
