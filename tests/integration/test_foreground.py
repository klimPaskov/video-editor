from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoedit.adapters.ffmpeg import ChromaKeyConfig, FFmpegAdapter
from videoedit.services.foreground import render_chroma_key_foreground
from videoedit.services.project import initialize_project, sha256_file
from videoedit.services.stage_state import load_stage_state


@pytest.mark.integration
def test_chroma_key_foreground_is_validated_and_cached(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[2]
    layout = initialize_project(tmp_path, "foreground_flow")
    adapter = FFmpegAdapter()
    source = tmp_path / "green-screen.mp4"
    adapter.generate_demo_source(source, duration_seconds=1)

    config = ChromaKeyConfig(edge_feather_px=1, edge_erode_iterations=1)
    manifest_path = render_chroma_key_foreground(
        package_root,
        layout,
        source,
        config=config,
        adapter=adapter,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = Path(payload["output"]["path"])

    assert payload["status"] == "complete"
    assert payload["alpha"]["polarity"] == "mixed"
    assert payload["validation"] == {
        "full_decode": "pass",
        "alpha_plane": "pass",
        "alpha_range": "pass",
        "dimensions": "pass",
        "frame_count": "pass",
        "frame_rate": "pass",
        "duration": "pass",
    }
    assert output.is_file()
    assert sha256_file(output) == payload["output"]["sha256"]

    cached_path = render_chroma_key_foreground(
        package_root,
        layout,
        source,
        config=config,
        adapter=adapter,
    )
    state = load_stage_state(package_root, layout, "chroma_key", "rev_001")
    assert cached_path == manifest_path
    assert state is not None
    assert state["status"] == "complete"
    assert state["attempt"] == 1
