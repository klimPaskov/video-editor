from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import videoedit.cli as cli


class _RecordingWhisperAdapter:
    instances: ClassVar[list[_RecordingWhisperAdapter]] = []

    def __init__(self, *, model_path: Path | None = None) -> None:
        self.model_path = model_path
        self.__class__.instances.append(self)


def test_configured_whisper_adapter_prefers_explicit_model_path(
    monkeypatch, tmp_path: Path
) -> None:
    configured = tmp_path / "configured-small.pt"
    explicit = tmp_path / "explicit-small.pt"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(whisper_model_path=configured, whisper_model="medium"),
    )
    monkeypatch.setattr(cli, "WhisperAdapter", _RecordingWhisperAdapter)

    adapter = cli._configured_whisper_adapter(explicit)

    assert adapter.model_path == explicit
    assert cli._configured_whisper_model() == "medium"
    assert cli._configured_whisper_model("large") == "large"


def test_post_render_commands_use_configured_whisper_model_by_default(
    monkeypatch, tmp_path: Path
) -> None:
    configured = tmp_path / "configured-small.pt"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(whisper_model_path=configured, whisper_model="medium"),
    )
    monkeypatch.setattr(cli, "WhisperAdapter", _RecordingWhisperAdapter)
    monkeypatch.setattr(cli, "_configured_ffmpeg_adapter", lambda: object())
    _RecordingWhisperAdapter.instances.clear()

    join_calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "qa_rendered_joins",
        lambda *args, **kwargs: join_calls.update(kwargs) or tmp_path / "join-qa.json",
    )
    cli.qa_joins("project", workspace=tmp_path)

    revision_calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "retranscribe_revision",
        lambda *args, **kwargs: (
            revision_calls.update(kwargs) or tmp_path / "transcript-comparison.json"
        ),
    )
    cli.retranscribe_revision_command(
        "project",
        revision_media=tmp_path / "revision-media.json",
        transcript=tmp_path / "transcript.json",
        workspace=tmp_path,
    )

    assert join_calls["transcriber"].model_path == configured  # type: ignore[union-attr]
    assert join_calls["model_name"] == "medium"
    assert join_calls["revision_id"] == "rev_001"
    assert join_calls["transcript_clock"] == "output"
    join_calls.clear()
    cli.qa_joins("project", revision_id="rev_002", workspace=tmp_path)
    assert join_calls["revision_id"] == "rev_002"
    assert join_calls["transcript_clock"] == "output"
    assert revision_calls["transcriber"].model_path == configured  # type: ignore[union-attr]
    assert revision_calls["model_name"] == "medium"
    assert len(_RecordingWhisperAdapter.instances) == 3
