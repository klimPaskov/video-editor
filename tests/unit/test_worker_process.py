from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _common_process() -> dict[str, Any]:
    return runpy.run_path(
        str(ROOT / "workers" / "common_process.py"),
        run_name="videoedit_worker_common_process_test",
    )


def test_worker_process_uses_argument_arrays_and_bounds_redacted_output() -> None:
    process = _common_process()
    result = process["run_process"](
        (
            sys.executable,
            "-c",
            "print('secret-worker-token'); print('x' * 512)",
        ),
        timeout_seconds=30,
        stdout_limit_bytes=64,
        redactions=("secret-worker-token",),
    )

    assert result.exit_code == 0
    assert result.arguments[0] == sys.executable
    assert "secret-worker-token" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert result.stdout_truncated is True


def test_worker_process_returns_captured_nonzero_result() -> None:
    process = _common_process()
    result = process["run_process"](
        (sys.executable, "-c", "import sys; print('diagnostic', file=sys.stderr); sys.exit(7)"),
        timeout_seconds=30,
    )

    assert result.exit_code == 7
    assert result.stderr.strip() == "diagnostic"


def test_worker_process_does_not_mark_exact_limit_as_truncated() -> None:
    process = _common_process()
    result = process["run_process"](
        (sys.executable, "-c", "print('1234567', end='')"),
        timeout_seconds=30,
        stdout_limit_bytes=7,
    )

    assert result.stdout == "1234567"
    assert result.stdout_truncated is False


def test_worker_process_timeout_is_a_stable_error() -> None:
    process = _common_process()
    with pytest.raises(process["WorkerProcessError"], match="timed out"):
        process["run_process"](
            (sys.executable, "-c", "import time; time.sleep(2)"),
            timeout_seconds=0.05,
        )


def test_matanyone_media_checks_use_the_worker_process_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(
        str(ROOT / "workers" / "matanyone2" / "run_job.py"),
        run_name="matanyone2_process_adapter_test",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    probe_payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 4,
                "height": 4,
                "nb_read_frames": "3",
                "avg_frame_rate": "30/1",
                "pix_fmt": "yuv420p",
            }
        ],
        "format": {"duration": "0.100000"},
    }

    class FakeResult:
        exit_code = 0
        stdout = json.dumps(probe_payload)
        stderr = ""

    def fake_run(arguments: tuple[str, ...], **kwargs: object) -> FakeResult:
        calls.append((arguments, kwargs))
        return FakeResult()

    probe_globals = namespace["_probe_media"].__globals__
    monkeypatch.setattr(probe_globals["shutil"], "which", lambda name: name)
    probe_globals["run_process"] = fake_run
    media_path = tmp_path / "output.mp4"

    metadata = namespace["_probe_media"](media_path)
    namespace["_decode_check"](media_path)

    assert metadata["frame_count"] == 3
    assert calls[0][0][0] == "ffprobe"
    assert calls[1][0][0] == "ffmpeg"
    assert calls[0][1]["timeout_seconds"] == 120
    assert calls[1][1]["timeout_seconds"] == 300


def test_isolated_worker_scripts_do_not_call_subprocess_directly() -> None:
    for relative in ("workers/sam3/run_job.py", "workers/matanyone2/run_job.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "subprocess.run" not in source
        assert "run_process" in source
