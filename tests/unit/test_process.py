import sys
from pathlib import Path
from threading import Event

import pytest

from videoedit.adapters.process import LocalProcessRunner, ProcessRequest
from videoedit.errors import ProcessCancelledError, ProcessTimeoutError


def test_process_runner_uses_argument_array() -> None:
    result = LocalProcessRunner().run(
        ProcessRequest(
            executable=sys.executable,
            arguments=("-c", "print('ok')"),
            working_directory=Path.cwd(),
        )
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"


def test_process_runner_bounds_and_redacts_output() -> None:
    result = LocalProcessRunner().run(
        ProcessRequest(
            executable=sys.executable,
            arguments=(
                "-c",
                "import sys; print('top-secret' * 100); print('top-secret', file=sys.stderr)",
            ),
            stdout_limit_bytes=32,
            stderr_limit_bytes=32,
            redactions=("top-secret",),
        )
    )
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False
    assert "top-secret" not in result.stdout
    assert "[REDACTED]" in result.stderr


def test_process_runner_reports_timeout() -> None:
    with pytest.raises(ProcessTimeoutError):
        LocalProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(1)"),
                timeout_seconds=0.05,
                kill_grace_seconds=0.05,
            )
        )


def test_process_runner_honours_pre_cancelled_event() -> None:
    cancelled = Event()
    cancelled.set()
    with pytest.raises(ProcessCancelledError):
        LocalProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("-c", "print('must not run')"),
                cancellation_event=cancelled,
            )
        )
