from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import (
    DEFAULT_VIDEO_BITRATE_BPS,
    DEFAULT_VIDEO_CODEC,
    FFmpegAdapter,
    adapter_encoder_identity,
)
from videoedit.adapters.process import ProcessRequest, ProcessResult
from videoedit.settings import Settings


class RecordingRunner:
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessResult:
        self.requests.append(request)
        stdout = ""
        if request.executable == "ffprobe":
            stdout = json.dumps(
                {
                    "streams": [
                        {"codec_type": "video", "avg_frame_rate": "30/1"},
                        {"codec_type": "audio"},
                    ],
                    "format": {"duration": "1.000000"},
                }
            )
        return ProcessResult(
            arguments=(request.executable, *request.arguments),
            exit_code=0,
            stdout=stdout,
            stderr="",
            elapsed_ms=1,
        )


def _ffmpeg_arguments(runner: RecordingRunner) -> tuple[str, ...]:
    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    return request.arguments


def test_default_video_profile_is_software_and_hashable() -> None:
    adapter = FFmpegAdapter()

    assert adapter_encoder_identity(adapter) == {
        "video_codec": DEFAULT_VIDEO_CODEC,
        "video_bitrate_bps": DEFAULT_VIDEO_BITRATE_BPS,
    }


def test_video_profile_rejects_unsafe_codec_and_unbounded_bitrate() -> None:
    with pytest.raises(ValueError, match="safe"):
        FFmpegAdapter(video_codec="h264 amf")
    with pytest.raises(ValueError, match="between"):
        FFmpegAdapter(video_bitrate_bps=100)


def test_software_derivative_keeps_crf_preset_and_yuv420p(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)

    adapter.render_scaled_derivative(
        tmp_path / "source.mp4",
        tmp_path / "software.mp4",
        width=640,
        height=360,
    )
    arguments = _ffmpeg_arguments(runner)

    assert "libx264" in arguments
    assert "-crf" in arguments
    assert arguments[arguments.index("-crf") + 1] == "20"
    assert "-preset" in arguments
    assert "yuv420p" in arguments
    assert "nv12" not in arguments


def test_software_lossless_profile_uses_explicit_qp_zero() -> None:
    adapter = FFmpegAdapter()

    arguments = adapter._video_encode_arguments(
        "libx264",
        crf=20,
        preset="ultrafast",
        qp=0,
    )

    assert "-qp" in arguments
    assert arguments[arguments.index("-qp") + 1] == "0"
    assert "-crf" not in arguments
    assert "yuv420p" in arguments
    with pytest.raises(ValueError, match="explicit QP"):
        FFmpegAdapter(video_codec="h264_amf")._video_encode_arguments(
            "h264_amf",
            crf=20,
            preset="medium",
            qp=0,
        )


def test_amd_derivative_uses_bitrate_and_nv12_without_software_rate_controls(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.render_scaled_derivative(
        tmp_path / "source.mp4",
        tmp_path / "amd.mp4",
        width=640,
        height=360,
    )
    arguments = _ffmpeg_arguments(runner)

    assert "h264_amf" in arguments
    assert "-b:v" in arguments
    assert str(DEFAULT_VIDEO_BITRATE_BPS) in arguments
    assert "-crf" not in arguments
    assert "-preset" not in arguments
    assert "format=nv12" in " ".join(arguments)
    assert "nv12" in arguments


def test_amd_base_and_retime_graphs_use_nv12_and_bound_encoder(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.render_keep_ranges(
        tmp_path / "source.mp4",
        [(0, 1_000_000)],
        tmp_path / "base.mp4",
    )
    adapter.render_retimed_segments(
        tmp_path / "source.mp4",
        [
            {
                "source_range": {"start_us": 0, "end_us": 1_000_000},
                "playback_rate": 1,
            }
        ],
        tmp_path / "retimed.mp4",
    )

    ffmpeg_requests = [item for item in runner.requests if item.executable == "ffmpeg"]
    assert len(ffmpeg_requests) == 2
    for request in ffmpeg_requests:
        assert "h264_amf" in request.arguments
        assert "-b:v" in request.arguments
        assert "format=nv12" in " ".join(request.arguments)
        assert "-crf" not in request.arguments
        assert "-preset" not in request.arguments


def test_single_keep_range_preview_seeks_before_decoding_long_source(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)

    adapter.render_keep_ranges(
        tmp_path / "source.mp4",
        [(12_345_000, 16_789_000)],
        tmp_path / "preview.mp4",
    )

    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    assert request.arguments[request.arguments.index("-ss") + 1] == "12.345000"
    assert request.arguments[request.arguments.index("-t") + 1] == "4.444000"
    assert "-filter_complex" not in request.arguments
    assert request.arguments[request.arguments.index("-map") + 1] == "0:v:0"


def test_multi_keep_range_render_resolves_source_and_output_paths(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)
    source = Path("relative-source.mp4")
    output = tmp_path / "multi-range.mp4"

    adapter.render_keep_ranges(source, [(0, 1_000_000), (2_000_000, 3_000_000)], output)

    request = next(
        item
        for item in runner.requests
        if item.executable == "ffmpeg" and "-filter_complex" in item.arguments
    )
    assert request.arguments[request.arguments.index("-i") + 1] == str(source.resolve())
    assert request.arguments[-1] == str(output.resolve())
    assert request.working_directory == output.parent.resolve()


def test_media_diagnostics_accept_an_exact_post_input_analysis_window(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)
    source = tmp_path / "source.mp4"

    adapter.measure_clipping(source, start_us=125_000, end_us=625_000)
    adapter.detect_black_frames(source, start_us=125_000, end_us=625_000)
    adapter.detect_freeze_frames(source, start_us=125_000, end_us=625_000)

    for request in [item for item in runner.requests if item.executable == "ffmpeg"]:
        assert request.arguments[request.arguments.index("-ss") + 1] == "0.125000"
        assert request.arguments[request.arguments.index("-t") + 1] == "0.500000"
        assert request.arguments.index("-ss") > request.arguments.index("-i")

    with pytest.raises(ValueError, match="both start_us and end_us"):
        adapter.measure_clipping(source, start_us=1)
    with pytest.raises(ValueError, match="positive nonnegative"):
        adapter.detect_freeze_frames(source, start_us=2, end_us=1)


def test_long_retime_graph_uses_a_filter_script_on_windows_command_lines(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)
    segments = [
        {
            "source_range": {"start_us": index * 20_000, "end_us": (index + 1) * 20_000},
            "playback_rate": 1,
        }
        for index in range(150)
    ]

    adapter.render_retimed_segments(
        tmp_path / "source.mp4",
        segments,
        tmp_path / "retimed.mp4",
    )

    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    assert "-filter_complex_script" in request.arguments
    script = Path(request.arguments[request.arguments.index("-filter_complex_script") + 1])
    assert script.is_file()
    assert "concat=n=150" in script.read_text(encoding="utf-8")


def test_retime_render_binds_output_duration_when_requested(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner)

    adapter.render_retimed_segments(
        tmp_path / "source.mp4",
        [
            {
                "source_range": {"start_us": 0, "end_us": 1_000_000},
                "playback_rate": 1,
            }
        ],
        tmp_path / "retimed.mp4",
        duration_us=750_000,
        frame_rate=Fraction(60, 1),
    )

    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    assert request.arguments[request.arguments.index("-t") + 1] == "0.750000"
    assert request.arguments[request.arguments.index("-fps_mode") + 1] == "cfr"
    assert request.arguments[request.arguments.index("-r") + 1] == "60/1"


def test_amd_fixture_and_composite_outputs_use_bound_encoder(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.create_edit_proxy(tmp_path / "source.mp4", tmp_path / "proxy.mp4")
    adapter.generate_demo_source(tmp_path / "demo-source.mp4", duration_seconds=1)
    adapter.generate_demo_plate(tmp_path / "demo-plate.mp4", duration_seconds=1)
    adapter.generate_edit_demo_source(tmp_path / "edit-source.mp4")
    adapter.recolor_with_mask(
        tmp_path / "source.mp4",
        tmp_path / "mask.mp4",
        tmp_path / "recolored.mp4",
    )
    adapter.overlay_foreground(
        tmp_path / "plate.mp4",
        tmp_path / "foreground.mov",
        tmp_path / "composite.mp4",
    )

    requests = [item for item in runner.requests if item.executable == "ffmpeg"]
    assert len(requests) == 6
    for request in requests:
        assert "h264_amf" in request.arguments
        assert "-b:v" in request.arguments
        assert "nv12" in request.arguments
        assert "-crf" not in request.arguments
        assert "-preset" not in request.arguments


def test_audio_overlay_binds_padded_audio_to_the_visual_boundary(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.overlay_foreground(
        tmp_path / "plate.mp4",
        tmp_path / "foreground.mov",
        tmp_path / "composite.mp4",
        audio_source=tmp_path / "production-audio.mp4",
    )

    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    filter_graph = request.arguments[request.arguments.index("-filter_complex") + 1]
    assert "[2:a]apad,atrim=start=0:end=1.000000,asetpts=PTS-STARTPTS[outa]" in filter_graph
    assert request.arguments[request.arguments.index("-map") + 1] == "[outv]"
    assert (
        request.arguments[request.arguments.index("-map", request.arguments.index("-map") + 1) + 1]
        == "[outa]"
    )
    assert request.arguments[request.arguments.index("-t") + 1] == "1.000000"
    assert "-shortest" in request.arguments


def test_audio_boundary_finalization_pads_and_trims_to_the_requested_duration(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.bound_audio_to_visual_duration(
        tmp_path / "rendered.mp4",
        tmp_path / "bounded.mp4",
        duration_us=6_000_000,
    )

    request = next(item for item in runner.requests if item.executable == "ffmpeg")
    arguments = request.arguments
    filter_graph = arguments[arguments.index("-filter_complex") + 1]
    assert "[0:a]apad,atrim=start=0:end=6.000000,asetpts=PTS-STARTPTS[aout]" in filter_graph
    assert arguments[arguments.index("-map") + 1] == "0:v:0"
    assert "-c:v" in arguments and arguments[arguments.index("-c:v") + 1] == "copy"
    assert arguments[arguments.index("-t") + 1] == "6.000000"
    assert "-shortest" in arguments


def test_amd_profile_does_not_replace_lossless_mask_encoding(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = FFmpegAdapter(runner=runner, video_codec="h264_amf")

    adapter.generate_demo_mask(tmp_path / "mask.mp4", duration_seconds=1)

    arguments = _ffmpeg_arguments(runner)
    assert "libx264" in arguments
    assert "h264_amf" not in arguments
    assert "yuv420p" in arguments


def test_settings_accepts_explicit_amd_profile_without_changing_default() -> None:
    assert Settings(_env_file=None).video_codec == "libx264"
    settings = Settings(_env_file=None, video_codec="h264_amf", video_bitrate_bps=8_000_000)
    assert settings.video_codec == "h264_amf"
    assert settings.video_bitrate_bps == 8_000_000
