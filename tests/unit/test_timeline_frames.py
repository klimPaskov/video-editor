from videoedit.domain.timeline import frame_to_microseconds, microseconds_to_frame


def test_round_trip_whole_second() -> None:
    frame = microseconds_to_frame(1_000_000, 30)
    assert frame == 30
    assert frame_to_microseconds(frame, 30) == 1_000_000


def test_ntsc_frame_mapping_is_rational() -> None:
    assert microseconds_to_frame(1_000_000, 30_000, 1001) == 30
