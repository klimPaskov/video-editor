from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO, Protocol

from videoedit.errors import (
    ProcessCancelledError,
    ProcessExecutionError,
    ProcessTimeoutError,
)
from videoedit.logging import log_event

DEFAULT_OUTPUT_LIMIT_BYTES = 1_000_000
DEFAULT_ALLOWED_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "HOME",
)
LOGGER = logging.getLogger(__name__)


def redact_text(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def parse_command(command: str | Sequence[str]) -> tuple[str, ...]:
    """Parse a configured command while retaining Windows backslashes.

    New code should pass a sequence of arguments. The string form remains for
    configuration and CLI compatibility; Windows uses ``posix=False`` so an
    absolute Python or worker path is not rewritten into ``C:Users...``.
    """

    if isinstance(command, str):
        parts = shlex.split(command, posix=os.name != "nt")
        if os.name == "nt":
            parts = [
                part[1:-1]
                if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
                else part
                for part in parts
            ]
    else:
        parts = [str(part) for part in command]
    if not parts or not parts[0]:
        raise ValueError("process command must include an executable")
    if any(not part for part in parts):
        raise ValueError("process command arguments must not be empty")
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    executable: str
    arguments: tuple[str, ...] = ()
    working_directory: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 300.0
    stdout_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    stderr_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    cancellation_event: Event | None = None
    redactions: tuple[str, ...] = ()
    kill_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.executable:
            raise ValueError("executable must not be empty")
        if any(not argument for argument in self.arguments):
            raise ValueError("process arguments must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.stdout_limit_bytes < 0 or self.stderr_limit_bytes < 0:
            raise ValueError("process output limits must be nonnegative")
        if self.kill_grace_seconds < 0:
            raise ValueError("kill_grace_seconds must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    arguments: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ProcessRunner(Protocol):
    def run(self, request: ProcessRequest) -> ProcessResult: ...


class _StreamCapture:
    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.data = bytearray()
        self.truncated = False
        self.total_bytes = 0

    def read(self, stream: BinaryIO | None) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            self.total_bytes += len(chunk)
            if len(self.data) < self.limit_bytes:
                remaining = self.limit_bytes - len(self.data)
                self.data.extend(chunk[:remaining])
            if self.total_bytes > self.limit_bytes:
                self.truncated = True

    def text(self, secrets: Sequence[str]) -> str:
        return redact_text(bytes(self.data).decode("utf-8", errors="replace"), secrets)


class LocalProcessRunner(ProcessRunner):
    """Run one local process without a shell and with bounded diagnostics."""

    def __init__(
        self,
        allowed_environment: tuple[str, ...] = DEFAULT_ALLOWED_ENVIRONMENT,
    ) -> None:
        self._allowed_environment = frozenset(allowed_environment)

    def run(self, request: ProcessRequest) -> ProcessResult:
        if request.cancellation_event is not None and request.cancellation_event.is_set():
            raise ProcessCancelledError(request.executable)

        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self._allowed_environment
        }
        environment.update(request.environment)
        command = (request.executable, *request.arguments)
        display_command = tuple(redact_text(value, request.redactions) for value in command)
        working_directory = (request.working_directory or Path.cwd()).resolve()
        if not working_directory.is_dir():
            raise ProcessExecutionError(f"working directory does not exist: {working_directory}")

        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise ProcessExecutionError(f"executable not found: {request.executable}") from exc
        except PermissionError as exc:
            raise ProcessExecutionError(
                f"executable is not runnable: {request.executable}"
            ) from exc
        except OSError as exc:
            raise ProcessExecutionError(f"could not start {request.executable}: {exc}") from exc

        stdout_capture = _StreamCapture(request.stdout_limit_bytes)
        stderr_capture = _StreamCapture(request.stderr_limit_bytes)
        stdout_thread = Thread(
            target=stdout_capture.read,
            args=(process.stdout,),
            name="videoedit-process-stdout",
            daemon=True,
        )
        stderr_thread = Thread(
            target=stderr_capture.read,
            args=(process.stderr,),
            name="videoedit-process-stderr",
            daemon=True,
        )
        started = time.monotonic()
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        cancelled = False
        deadline = started + request.timeout_seconds
        while process.poll() is None:
            if request.cancellation_event is not None and request.cancellation_event.is_set():
                cancelled = True
                self._terminate(process, request.kill_grace_seconds)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process, request.kill_grace_seconds)
                break
            time.sleep(0.02)

        exit_code = process.wait()
        stdout_thread.join(timeout=request.kill_grace_seconds + 1)
        stderr_thread.join(timeout=request.kill_grace_seconds + 1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        stdout = stdout_capture.text(request.redactions)
        stderr = stderr_capture.text(request.redactions)
        if cancelled:
            log_event(
                LOGGER,
                "process_cancelled",
                f"{request.executable} cancelled",
                command=list(display_command),
                elapsed_ms=elapsed_ms,
                redacted=bool(request.redactions),
            )
            raise ProcessCancelledError(request.executable, stderr=stderr)
        if timed_out:
            log_event(
                LOGGER,
                "process_timeout",
                f"{request.executable} timed out",
                command=list(display_command),
                elapsed_ms=elapsed_ms,
                redacted=bool(request.redactions),
            )
            raise ProcessTimeoutError(
                request.executable,
                timeout_seconds=request.timeout_seconds,
                stderr=stderr,
            )
        process_result = ProcessResult(
            arguments=display_command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_ms=elapsed_ms,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
        )
        log_event(
            LOGGER,
            "process_completed",
            f"{request.executable} exited with code {exit_code}",
            command=list(display_command),
            elapsed_ms=elapsed_ms,
            exit_code=exit_code,
            redacted=bool(request.redactions),
        )
        return process_result

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        if os.name != "nt":
            kill_group = getattr(os, "killpg", None)
            try:
                if callable(kill_group):
                    kill_group(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                return
        else:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
