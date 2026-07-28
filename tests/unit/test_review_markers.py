from __future__ import annotations

import pytest

from videoedit.errors import PlanningValidationError
from videoedit.services.review_markers import parse_review_markdown


def test_parse_review_markdown_supports_timecodes_units_and_all_marker_kinds() -> None:
    markdown = "\n".join(
        [
            "[FIX 00:00:01.000-00:00:02.000] Repair the duplicate phrase.",
            "[KEEP 2000000us-2500ms] Preserve the clean take.",
            "[REMOVE 3s-3500ms] Remove dead air.",
            "[RETIME 4000ms-4500000us] Retiming note.",
            "[MASK 5000000-5500000] Correct the object selection.",
            "[TEXT 6s-6500ms] Change the label.",
            "[AUDIO 7000000-7500000] Lower the cue.",
            "[ZOOM 8s-8500ms] Target the cursor.",
            "[SPEED 9000000-9500000] Speed up the visible prompt action at 1.5x.",
        ]
    )
    markers = parse_review_markdown(markdown, segment_range=(0, 10_000_000))
    assert len(markers) == 9
    assert markers[0]["range_us"] == {"start_us": 1_000_000, "end_us": 2_000_000}
    assert markers[1]["range_us"] == {"start_us": 2_000_000, "end_us": 2_500_000}
    assert markers[-1]["kind"] == "SPEED"


@pytest.mark.parametrize(
    "markdown,match",
    [
        ("[UNKNOWN 0-1] Unsupported", "unsupported"),
        ("[FIX 2-1] Backwards", "positive"),
        ("[FIX 0-2000000] Outside", "outside"),
        ("[FIX] Missing range", "must include a range"),
        ("[FIX 0-1]", "no instruction"),
    ],
)
def test_parse_review_markdown_rejects_ambiguous_or_unsafe_markers(
    markdown: str, match: str
) -> None:
    with pytest.raises(PlanningValidationError, match=match):
        parse_review_markdown(markdown, segment_range=(0, 1_000_000))
