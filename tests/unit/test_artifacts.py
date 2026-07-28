from __future__ import annotations

import json
from pathlib import Path

import pytest

import videoedit.services.artifacts as artifacts

ROOT = Path(__file__).resolve().parents[2]


def test_write_validated_artifact_retries_transient_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(
        (ROOT / "examples" / "approval_record.example.json").read_text(encoding="utf-8")
    )
    destination = tmp_path / "approval.json"
    real_replace = artifacts.os.replace
    calls = 0
    sleeps: list[float] = []

    def flaky_replace(source: str, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(13, "temporary sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(artifacts.os, "replace", flaky_replace)
    monkeypatch.setattr(artifacts.time, "sleep", sleeps.append)

    artifacts.write_validated_artifact(ROOT, "approval_record", destination, payload)

    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert calls == 3
    assert sleeps == [
        artifacts.ATOMIC_REPLACE_DELAY_SECONDS,
        artifacts.ATOMIC_REPLACE_DELAY_SECONDS,
    ]
    assert not list(tmp_path.glob(".approval.json.*.tmp"))


def test_write_text_atomically_preserves_failure_after_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "notes.txt"
    calls = 0

    def always_denied(_source: str, _target: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(13, "persistent sharing violation")

    monkeypatch.setattr(artifacts.os, "replace", always_denied)
    monkeypatch.setattr(artifacts.time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError, match="persistent sharing violation"):
        artifacts.write_text_atomically(destination, "evidence")

    assert calls == artifacts.ATOMIC_REPLACE_ATTEMPTS
    assert not destination.exists()
    assert not list(tmp_path.glob(".notes.txt.*.tmp"))
