from __future__ import annotations

import pytest

from videoedit.services.media import parse_seconds_to_us, seconds_to_us
from videoedit.services.transcription import (
    _raw_seconds_to_us,
)
from videoedit.services.transcription import (
    seconds_to_us as transcription_seconds_to_us,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1234565", 123456),
        ("0.1234575", 123458),
        ("1.23456789", 1234568),
        ("-0.0000015", -2),
    ],
)
def test_decimal_seconds_use_explicit_half_even_microsecond_rounding(
    value: str,
    expected: int,
) -> None:
    assert parse_seconds_to_us(value) == expected


@pytest.mark.parametrize("value", [None, True, "", "N/A", "nan", "inf", "not-a-time"])
def test_invalid_or_nonfinite_seconds_are_rejected(value: object) -> None:
    assert parse_seconds_to_us(value) is None
    assert seconds_to_us(value) is None
    assert transcription_seconds_to_us(value) == 0


def test_public_normalizers_clamp_negative_time_but_raw_transcription_keeps_sign() -> None:
    assert seconds_to_us("-0.0000015") == 0
    assert transcription_seconds_to_us("-0.0000015") == 0
    assert _raw_seconds_to_us("-0.0000015") == -2
