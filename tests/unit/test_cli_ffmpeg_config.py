from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import videoedit.cli as cli
from videoedit.adapters.process import ProcessResult
from videoedit.services.qa import basic_media_qa


class _FakeMediaAdapter:
    def __init__(self) -> None:
        self.recolor_calls: list[tuple[Path, Path, Path, float]] = []
        self.probe_calls: list[Path] = []
        self.decode_calls: list[Path] = []

    def recolor_with_mask(
        self,
        source: Path,
        mask: Path,
        output: Path,
        *,
        hue_degrees: float,
    ) -> None:
        self.recolor_calls.append((source, mask, output, hue_degrees))

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


def test_configured_ffmpeg_adapter_uses_settings_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            ffmpeg_path="ffmpeg-amd",
            ffprobe_path="ffprobe-amd",
            video_codec="h264_amf",
            video_bitrate_bps=4_000_000,
        ),
    )

    adapter = cli._configured_ffmpeg_adapter()

    assert adapter.ffmpeg_path == "ffmpeg-amd"
    assert adapter.ffprobe_path == "ffprobe-amd"
    assert adapter.encoder_identity() == {
        "video_codec": "h264_amf",
        "video_bitrate_bps": 4_000_000,
    }


def test_media_cli_commands_pass_configured_adapter(monkeypatch, tmp_path: Path) -> None:
    adapter = _FakeMediaAdapter()
    monkeypatch.setattr(cli, "_configured_ffmpeg_adapter", lambda: adapter)

    recolor_output = tmp_path / "recolored.mp4"
    cli.recolor(
        tmp_path / "source.mp4",
        tmp_path / "mask.mp4",
        recolor_output,
        hue_degrees=37,
    )
    assert adapter.recolor_calls == [
        (
            (tmp_path / "source.mp4").resolve(),
            (tmp_path / "mask.mp4").resolve(),
            recolor_output.resolve(),
            37,
        )
    ]

    report_path = tmp_path / "media-qa.json"
    result = basic_media_qa(tmp_path / "candidate.mp4", report_path, adapter=adapter)
    assert result["status"] == "pass"
    assert adapter.probe_calls == [(tmp_path / "candidate.mp4").resolve()]
    assert adapter.decode_calls == [(tmp_path / "candidate.mp4").resolve()]


def test_project_media_commands_pass_configured_adapter(monkeypatch, tmp_path: Path) -> None:
    adapter = object()
    monkeypatch.setattr(cli, "_configured_ffmpeg_adapter", lambda: adapter)
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)

    foreground_manifest = tmp_path / "foreground.json"
    foreground_manifest.write_text("{}", encoding="utf-8")
    foreground_calls: dict[str, object] = {}

    def fake_chroma_key(*args, **kwargs):
        foreground_calls.update(kwargs)
        return foreground_manifest

    monkeypatch.setattr(cli, "render_chroma_key_foreground", fake_chroma_key)
    cli.chroma_key(
        "demo",
        source=tmp_path / "source.mp4",
        workspace=tmp_path,
    )
    assert foreground_calls["adapter"] is adapter

    qa_report = tmp_path / "qa-report.json"
    qa_report.write_text(json.dumps({"final_ready": True}), encoding="utf-8")
    qa_calls: dict[str, object] = {}

    def fake_qa_render(*args, **kwargs):
        qa_calls.update(kwargs)
        return qa_report

    monkeypatch.setattr(cli, "qa_render", fake_qa_render)
    cli.qa_project("demo", workspace=tmp_path)
    assert qa_calls["adapter"] is adapter
