import pytest
from pydantic import ValidationError

from videoedit.domain.models import BackgroundLayer, TextLayer, TimelineSpec


def test_timeline_rejects_layer_past_end() -> None:
    with pytest.raises(ValidationError):
        TimelineSpec(
            project_id="demo",
            width=1280,
            height=720,
            fps=30,
            duration_frames=60,
            background=BackgroundLayer(),
            layers=[
                TextLayer(
                    id="late",
                    start_frame=30,
                    duration_frames=40,
                    text="late",
                )
            ],
        )
