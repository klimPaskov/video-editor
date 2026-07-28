from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise


@dataclass(frozen=True, slots=True, order=True)
class TimeRange:
    """Half-open range in integer microseconds."""

    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        if self.start_us < 0:
            raise ValueError("start_us must be nonnegative")
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def overlaps(self, other: TimeRange) -> bool:
        return self.start_us < other.end_us and other.start_us < self.end_us

    def contains(self, time_us: int) -> bool:
        return self.start_us <= time_us < self.end_us


@dataclass(frozen=True, slots=True)
class RationalRate:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("rate values must be positive")

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def nearest_frame(self, time_us: int) -> int:
        if time_us < 0:
            raise ValueError("time_us must be nonnegative")
        frames = Fraction(time_us, 1_000_000) * self.fraction
        return int(frames + Fraction(1, 2))


def validate_ordered_nonoverlapping(ranges: list[TimeRange]) -> None:
    for previous, current in pairwise(ranges):
        if previous.end_us > current.start_us:
            raise ValueError("ranges must be ordered and nonoverlapping")
