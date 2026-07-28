from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GPU_PROBE = "nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader"


@pytest.mark.parametrize(
    ("worker", "ref_variable"),
    (("sam3", "SAM3_REF"), ("matanyone2", "MATANYONE2_REF")),
)
def test_unix_worker_installers_require_pinned_commit_and_gpu_probe(
    worker: str, ref_variable: str
) -> None:
    script = (ROOT / "workers" / worker / "install.sh").read_text(encoding="utf-8")

    assert "=~ ^[0-9a-f]{40}$" in script
    assert f'"${ref_variable}"' in script
    assert GPU_PROBE in script
    assert 'checkout --detach "$' in script
    assert 'RESOLVED_COMMIT" != "$' in script
    assert "Tags and main are not accepted" in script


@pytest.mark.parametrize("worker", ("sam3", "matanyone2"))
def test_windows_worker_installers_require_pinned_commit_and_gpu_probe(worker: str) -> None:
    script_path = ROOT / "workers" / worker / "install.ps1"
    script = script_path.read_text(encoding="utf-8")

    assert "-notmatch '^[0-9a-fA-F]{40}$'" in script
    assert GPU_PROBE in script
    assert "checkout --detach" in script
    assert "ToLowerInvariant()" in script
    assert "Tags and main are not accepted" in script
