from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.services.masking import recolor_local_mask, validate_local_mask
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.stage_state import load_stage_state


@pytest.mark.integration
def test_lossless_mask_validation_recolor_and_cache(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "mask_flow")
    adapter = FFmpegAdapter()
    source = tmp_path / "green-screen.mp4"
    lossy_mask = tmp_path / "mask-lossy.mp4"
    lossless_mask = tmp_path / "mask-lossless.mkv"
    adapter.generate_demo_source(source, duration_seconds=1)
    adapter.generate_demo_mask(lossy_mask, duration_seconds=1)
    adapter.transcode_mask_lossless(lossy_mask, lossless_mask)

    validation_path = validate_local_mask(
        package_root,
        layout,
        source,
        lossless_mask,
        adapter=adapter,
    )
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    assert validation["status"] == "complete"
    assert validation["validation"] == {
        "full_decode": "pass",
        "lossless": "pass",
        "pixel_format": "pass",
        "dimensions": "pass",
        "frame_count": "pass",
        "frame_rate": "pass",
        "range": "pass",
        "polarity": "pass",
        "duration": "pass",
    }

    recolor_path = recolor_local_mask(
        package_root,
        layout,
        source,
        lossless_mask,
        hue_degrees=100,
        adapter=adapter,
    )
    recolor = json.loads(recolor_path.read_text(encoding="utf-8"))
    output = Path(recolor["output"]["path"])
    assert recolor["validation"]["production_audio"] == "pass"
    assert output.is_file()
    assert sha256_file(output) == recolor["output"]["sha256"]

    cached_recolor = recolor_local_mask(
        package_root,
        layout,
        source,
        lossless_mask,
        hue_degrees=100,
        adapter=adapter,
    )
    validation_state = load_stage_state(package_root, layout, "mask_validation", "rev_001")
    recolor_state = load_stage_state(package_root, layout, "mask_recolor", "rev_001")
    assert cached_recolor == recolor_path
    assert validation_state is not None and validation_state["attempt"] == 1
    assert recolor_state is not None and recolor_state["attempt"] == 1
