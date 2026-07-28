from __future__ import annotations

from videoedit.services.foreground import AlphaStatistics
from videoedit.services.masking import validate_mask_alignment


def _probe(*, codec: str = "ffv1", pixel_format: str = "gray") -> dict[str, object]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "pix_fmt": pixel_format,
                "duration": "1.000000",
            }
        ],
        "format": {"duration": "1.000000"},
    }


def test_mask_validation_requires_lossless_gray_and_matching_alignment() -> None:
    validation = validate_mask_alignment(
        _probe(codec="h264", pixel_format="yuv420p"),
        _probe(),
        source_frame_count=30,
        mask_frame_count=30,
        mask_samples=[AlphaStatistics(minimum=0, maximum=255, mean=5)],
    )

    assert validation.is_valid
    assert validation.validation["lossless"] == "pass"
    assert validation.validation["dimensions"] == "pass"
    assert validation.validation["frame_rate"] == "pass"
    assert validation.mask_statistics["polarity"] == "white_foreground"


def test_mask_validation_fails_uncertain_polarity_and_frame_count() -> None:
    validation = validate_mask_alignment(
        _probe(codec="h264", pixel_format="yuv420p"),
        _probe(),
        source_frame_count=30,
        mask_frame_count=29,
        mask_samples=[AlphaStatistics(minimum=60, maximum=190, mean=110)],
    )

    assert not validation.is_valid
    assert validation.validation["frame_count"] == "fail"
    assert validation.validation["polarity"] == "fail"
    assert validation.mask_statistics["polarity"] == "unknown"
