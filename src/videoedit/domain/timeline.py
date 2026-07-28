from __future__ import annotations

from fractions import Fraction


def microseconds_to_frame(time_us: int, fps_numerator: int, fps_denominator: int = 1) -> int:
    if time_us < 0:
        raise ValueError("time_us must be nonnegative")
    if fps_numerator <= 0 or fps_denominator <= 0:
        raise ValueError("frame rate values must be positive")
    frames = Fraction(time_us, 1_000_000) * Fraction(fps_numerator, fps_denominator)
    return int(frames + Fraction(1, 2))


def frame_to_microseconds(frame: int, fps_numerator: int, fps_denominator: int = 1) -> int:
    if frame < 0:
        raise ValueError("frame must be nonnegative")
    if fps_numerator <= 0 or fps_denominator <= 0:
        raise ValueError("frame rate values must be positive")
    seconds = Fraction(frame * fps_denominator, fps_numerator)
    return int(seconds * 1_000_000)
