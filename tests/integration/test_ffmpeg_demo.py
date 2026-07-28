from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.adapters.process import LocalProcessRunner
from videoedit.services.doctor import _amd_amf_check


@pytest.mark.integration
def test_generate_probe_and_mask_recolor(tmp_path: Path) -> None:
    adapter = FFmpegAdapter()
    source = tmp_path / "source.mp4"
    mask = tmp_path / "mask.mp4"
    output = tmp_path / "recolored.mp4"
    adapter.generate_demo_source(source, duration_seconds=1)
    adapter.generate_demo_mask(mask, duration_seconds=1)
    adapter.recolor_with_mask(source, mask, output)
    probe = adapter.probe(output)
    assert output.stat().st_size > 0
    assert any(stream.get("codec_type") == "video" for stream in probe["streams"])


@pytest.mark.integration
def test_amd_overlay_preserves_the_final_visual_frame_with_audio(tmp_path: Path) -> None:
    amf_check = _amd_amf_check(
        configured_path="ffmpeg",
        runner=LocalProcessRunner(),
        working_directory=tmp_path,
    )
    if amf_check.status != "pass":
        pytest.skip(amf_check.message)

    adapter = FFmpegAdapter(video_codec="h264_amf")
    source = tmp_path / "source.mp4"
    plate = tmp_path / "plate.mp4"
    foreground = tmp_path / "foreground.mov"
    output = tmp_path / "composite.mp4"
    adapter.generate_demo_source(source, duration_seconds=1)
    adapter.generate_demo_plate(plate, duration_seconds=1)
    adapter.chroma_key_foreground(source, foreground)
    adapter.overlay_foreground(plate, foreground, output, audio_source=source)

    probe = adapter.probe(output)
    assert adapter.probe_frame_count(output) == 30
    assert probe["format"]["duration"] == "1.000000"
    assert {stream["codec_type"] for stream in probe["streams"]} == {"video", "audio"}


@pytest.mark.integration
def test_amd_audio_bound_stages_preserve_visual_boundaries(tmp_path: Path) -> None:
    amf_check = _amd_amf_check(
        configured_path="ffmpeg",
        runner=LocalProcessRunner(),
        working_directory=tmp_path,
    )
    if amf_check.status != "pass":
        pytest.skip(amf_check.message)

    adapter = FFmpegAdapter(video_codec="h264_amf")
    source = tmp_path / "source.mp4"
    adapter.generate_demo_source(source, duration_seconds=1)
    outputs = {
        "retimed": tmp_path / "retimed.mp4",
        "derivative": tmp_path / "derivative.mp4",
        "concat": tmp_path / "concat.mp4",
        "sound_mix": tmp_path / "sound-mix.mp4",
    }
    adapter.render_retimed_segments(
        source,
        [{"source_range": {"start_us": 0, "end_us": 1_000_000}, "playback_rate": 1}],
        outputs["retimed"],
    )
    adapter.render_scaled_derivative(source, outputs["derivative"], width=640, height=360)
    adapter.concat_media([source, source], outputs["concat"])
    adapter.mix_transition_sound(
        source,
        source,
        outputs["sound_mix"],
        start_us=0,
        gain_db=-12,
        fade_in_us=0,
        fade_out_us=100_000,
    )

    expected_frames = {"retimed": 30, "derivative": 30, "concat": 60, "sound_mix": 30}
    for name, path in outputs.items():
        probe = adapter.probe(path)
        assert adapter.probe_frame_count(path) == expected_frames[name]
        assert abs(float(probe["format"]["duration"]) - (2.0 if name == "concat" else 1.0)) <= 0.001
