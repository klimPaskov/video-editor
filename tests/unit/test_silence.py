from __future__ import annotations

from videoedit.services.silence import parse_silencedetect_detailed


def test_silencedetect_parser_normalizes_intervals_and_closes_final_silence() -> None:
    parsed = parse_silencedetect_detailed(
        """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 0.800000 | silence_duration: 0.800000
[silencedetect @ 0x1] silence_start: 1.200000
[silencedetect @ 0x1] silence_end: 2.500000 | silence_duration: 1.300000
[silencedetect @ 0x1] silence_start: 2.700000
""",
        3_000_000,
    )

    assert parsed.intervals == [(0, 800_000), (1_200_000, 2_500_000), (2_700_000, 3_000_000)]
    assert "unterminated_detector_interval_closed_at_duration" in parsed.warnings


def test_silencedetect_parser_reports_malformed_and_overlapping_events() -> None:
    parsed = parse_silencedetect_detailed(
        """
silence_start: 1.0
silence_start: 0.5
silence_end: 0.8
silence_end: nope
silence_end: 0.7
""",
        2_000_000,
    )

    assert parsed.intervals == [(500_000, 800_000)]
    assert "overlapping_detector_start" in parsed.warnings
    assert any(warning.startswith("invalid_detector_time:end_line_") for warning in parsed.warnings)
    assert "detector_end_without_start" in parsed.warnings
