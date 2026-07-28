from __future__ import annotations

import pytest

from videoedit.adapters.ffmpeg import ChromaKeyConfig
from videoedit.errors import ForegroundValidationError
from videoedit.services.foreground import (
    AlphaStatistics,
    parse_alpha_statistics,
    validate_foreground_output,
)


def _probe(*, pixel_format: str = "yuva444p12le", frame_rate: str = "30/1") -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "prores",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": frame_rate,
                "r_frame_rate": frame_rate,
                "pix_fmt": pixel_format,
                "duration": "1.000000",
            }
        ],
        "format": {"duration": "1.000000"},
    }


def test_chroma_key_config_builds_explicit_edge_controls() -> None:
    config = ChromaKeyConfig(
        crop=(10, 20, 640, 360),
        edge_feather_px=2,
        edge_erode_iterations=2,
        despill=True,
        despill_color="blue",
    )

    filters = config.filters()

    assert filters == (
        "crop=640:360:10:20",
        "chromakey=0x00FF00:0.180000:0.080000",
        "despill=type=blue:mix=0.500000",
    )
    with pytest.raises(ValueError, match="similarity"):
        ChromaKeyConfig(similarity=0)


def test_alpha_statistics_parser_and_fail_closed_validation() -> None:
    statistics = parse_alpha_statistics(
        "lavfi.signalstats.YMIN=0\nlavfi.signalstats.YMAX=255\nlavfi.signalstats.YAVG=45.25"
    )
    assert statistics == AlphaStatistics(minimum=0, maximum=255, mean=45.25)
    with pytest.raises(ForegroundValidationError, match="YMAX"):
        parse_alpha_statistics("lavfi.signalstats.YMIN=0\nlavfi.signalstats.YAVG=1")

    validation = validate_foreground_output(
        _probe(),
        _probe(),
        source_frame_count=30,
        output_frame_count=30,
        alpha_samples=[
            AlphaStatistics(minimum=255, maximum=255, mean=255),
            AlphaStatistics(minimum=255, maximum=255, mean=255),
        ],
    )
    assert validation.validation["alpha_plane"] == "pass"
    assert validation.alpha["polarity"] == "opaque"
    assert not validation.is_valid


def test_foreground_validation_requires_matching_rational_frame_rate() -> None:
    validation = validate_foreground_output(
        _probe(frame_rate="30000/1001"),
        _probe(frame_rate="30/1"),
        source_frame_count=30,
        output_frame_count=30,
        alpha_samples=[AlphaStatistics(minimum=0, maximum=255, mean=44)],
    )

    assert validation.validation["frame_rate"] == "fail"
    assert not validation.is_valid
