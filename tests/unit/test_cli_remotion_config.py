from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import videoedit.cli as cli


class _RecordingRemotionService:
    calls: ClassVar[list[tuple[Path, dict[str, object]]]] = []

    def __init__(self, remotion_directory: Path, **kwargs: object) -> None:
        self.__class__.calls.append((remotion_directory, kwargs))


def test_configured_remotion_service_uses_settings_npm_path(monkeypatch, tmp_path: Path) -> None:
    configured_npm = tmp_path / "node-v22" / "npm.cmd"
    configured_ffmpeg = tmp_path / "ffmpeg.exe"
    configured_ffprobe = tmp_path / "ffprobe.exe"
    package_root = tmp_path / "package-root"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            npm_path=str(configured_npm),
            ffmpeg_path=str(configured_ffmpeg),
            ffprobe_path=str(configured_ffprobe),
        ),
    )
    monkeypatch.setattr(cli, "_package_root", lambda: package_root)
    monkeypatch.setattr(cli, "RemotionService", _RecordingRemotionService)
    _RecordingRemotionService.calls.clear()

    cli._configured_remotion_service(tmp_path / "remotion")

    assert _RecordingRemotionService.calls == [
        (
            (tmp_path / "remotion").resolve(),
            {
                "npm_path": str(configured_npm),
                "package_root": package_root,
                "ffmpeg_path": str(configured_ffmpeg),
                "ffprobe_path": str(configured_ffprobe),
            },
        )
    ]


def test_make_demo_forwards_configured_npm_path(monkeypatch, tmp_path: Path) -> None:
    configured_npm = str(tmp_path / "node-v22" / "npm.cmd")
    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "_configured_remotion_npm_path", lambda: configured_npm)
    monkeypatch.setattr(cli, "_configured_ffmpeg_adapter", lambda: object())
    monkeypatch.setattr(
        cli,
        "build_demo",
        lambda *args, **kwargs: calls.update(kwargs) or {"status": "ok"},
    )

    cli.make_demo("demo", workspace=tmp_path)

    assert calls["npm_path"] == configured_npm
