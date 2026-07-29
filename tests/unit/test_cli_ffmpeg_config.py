from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import videoedit.cli as cli
from videoedit.adapters.process import ProcessResult
from videoedit.services.qa import basic_media_qa


class _FakeMediaAdapter:
    def __init__(self) -> None:
        self.probe_calls: list[Path] = []
        self.decode_calls: list[Path] = []

    def probe(self, path: Path) -> dict[str, object]:
        self.probe_calls.append(path)
        return {
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": "1.0"},
        }

    def full_decode_check(self, path: Path) -> ProcessResult:
        self.decode_calls.append(path)
        return ProcessResult(
            arguments=("ffmpeg", "-i", str(path)),
            exit_code=0,
            stdout="",
            stderr="",
            elapsed_ms=1,
        )


def test_configured_ffmpeg_adapter_uses_software_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            ffmpeg_path="ffmpeg",
            ffprobe_path="ffprobe",
            video_codec="libx264",
            video_bitrate_bps=4_000_000,
        ),
    )

    adapter = cli._configured_ffmpeg_adapter()

    assert adapter.ffmpeg_path == "ffmpeg"
    assert adapter.ffprobe_path == "ffprobe"
    assert adapter.encoder_identity() == {
        "video_codec": "libx264",
        "video_bitrate_bps": 4_000_000,
    }


def test_media_qa_uses_the_configured_adapter(monkeypatch, tmp_path: Path) -> None:
    adapter = _FakeMediaAdapter()
    monkeypatch.setattr(cli, "_configured_ffmpeg_adapter", lambda: adapter)

    report_path = tmp_path / "media-qa.json"
    result = basic_media_qa(tmp_path / "candidate.mp4", report_path, adapter=adapter)

    assert result["status"] == "pass"
    assert adapter.probe_calls == [(tmp_path / "candidate.mp4").resolve()]
    assert adapter.decode_calls == [(tmp_path / "candidate.mp4").resolve()]
