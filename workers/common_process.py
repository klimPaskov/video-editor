"""Small typed process boundary shared by the isolated model workers.

The core process adapter cannot be imported from an isolated worker environment.  This
module keeps the same safety properties at the worker boundary: argument arrays only,
bounded captured diagnostics, explicit timeouts, redaction, and stable failures.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

DEFAULT_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
PROCESS_ADAPTER_VERSION = "videoedit-worker-process-v1"


class WorkerProcessError(RuntimeError):
    """Stable error raised when a worker-side process cannot be completed."""


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    arguments: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: int
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _BoundedCapture:
    def __init__(self, limit_bytes: int) -> None:
        if limit_bytes < 0:
            raise ValueError("process output limits must be nonnegative")
        self._limit_bytes = limit_bytes
        self._data = bytearray()
        self._total_bytes = 0
        self.truncated = False

    def read(self, stream: object) -> None:
        reader = getattr(stream, "read", None)
        if not callable(reader):
            return
        while True:
            chunk = reader(8192)
            if not chunk:
                return
            if isinstance(chunk, str):
                encoded = chunk.encode("utf-8", errors="replace")
            else:
                encoded = bytes(chunk)
            self._total_bytes += len(encoded)
            if len(self._data) < self._limit_bytes:
                remaining = self._limit_bytes - len(self._data)
                self._data.extend(encoded[:remaining])
            if self._total_bytes > self._limit_bytes:
                self.truncated = True

    def text(self) -> str:
        return bytes(self._data).decode("utf-8", errors="replace")


def _redact(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def run_process(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = 300.0,
    stdout_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    redactions: Sequence[str] = (),
) -> WorkerProcessResult:
    """Run one worker-side command without a shell and with bounded diagnostics."""

    command = tuple(str(argument) for argument in arguments)
    if not command or any(not argument for argument in command):
        raise ValueError("worker process arguments must be non-empty")
    if timeout_seconds <= 0:
        raise ValueError("worker process timeout must be positive")
    working_directory = (cwd or Path.cwd()).expanduser().resolve()
    if not working_directory.is_dir():
        raise WorkerProcessError(
            f"worker process working directory is unavailable: {working_directory}"
        )

    stdout_capture = _BoundedCapture(stdout_limit_bytes)
    stderr_capture = _BoundedCapture(stderr_limit_bytes)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError as exc:
        raise WorkerProcessError(f"worker executable not found: {command[0]}") from exc
    except OSError as exc:
        raise WorkerProcessError(f"worker process could not start: {command[0]}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = Thread(target=stdout_capture.read, args=(process.stdout,), daemon=True)
    stderr_thread = Thread(target=stderr_capture.read, args=(process.stderr,), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = process.wait()
    finally:
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        for stream in (process.stdout, process.stderr):
            stream.close()

    elapsed_ms = round((time.monotonic() - started) * 1000)
    safe_arguments = tuple(_redact(argument, redactions) for argument in command)
    stdout = _redact(stdout_capture.text(), redactions)
    stderr = _redact(stderr_capture.text(), redactions)
    if timed_out:
        raise WorkerProcessError(
            f"worker process timed out after {timeout_seconds:g}s: {safe_arguments[0]}"
        )
    return WorkerProcessResult(
        arguments=safe_arguments,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=elapsed_ms,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )
