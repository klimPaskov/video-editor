from videoedit.domain.time import RationalRate, TimeRange, validate_ordered_nonoverlapping


def test_time_range_duration_and_overlap() -> None:
    first = TimeRange(0, 1_000_000)
    second = TimeRange(1_000_000, 2_000_000)
    assert first.duration_us == 1_000_000
    assert not first.overlaps(second)
    validate_ordered_nonoverlapping([first, second])


def test_rate_maps_one_second_to_thirty_frames() -> None:
    rate = RationalRate(30, 1)
    assert rate.nearest_frame(1_000_000) == 30
