from __future__ import annotations

from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter


@pytest.mark.integration
def test_generate_probe_and_decode_screen_recording_fixture(tmp_path: Path) -> None:
    adapter = FFmpegAdapter()
    source = tmp_path / "screen-recording.mp4"
    output = tmp_path / "screen-recording-copy.mp4"

    adapter.generate_demo_source(source, duration_seconds=1)
    adapter.render_keep_ranges(source, [(0, 1_000_000)], output)

    probe = adapter.probe(output)
    assert output.stat().st_size > 0
    assert adapter.probe_frame_count(output) == 30
    assert {stream["codec_type"] for stream in probe["streams"]} == {"video", "audio"}
