from __future__ import annotations

import json
from pathlib import Path

from videoedit.adapters.ffmpeg import FFmpegAdapter
from videoedit.domain.models import (
    BackgroundLayer,
    CaptionCue,
    TextLayer,
    TimelineAssetRef,
    TimelineSpec,
    Transform,
    VideoLayer,
)
from videoedit.services.project import ingest_source, initialize_project, sha256_file
from videoedit.services.remotion import RemotionService


def build_demo(
    workspace: Path,
    project_id: str,
    render: bool,
    *,
    adapter: FFmpegAdapter | None = None,
    npm_path: str = "npm",
) -> dict[str, str]:
    """Create a small screen-recording fixture for smoke tests and onboarding."""

    layout = initialize_project(workspace, project_id)
    ffmpeg = adapter or FFmpegAdapter()
    remotion = RemotionService(
        workspace / "remotion",
        npm_path=npm_path,
        ffmpeg_adapter=ffmpeg,
    )

    source = layout.work / "synthetic-screen-recording.mp4"
    final = layout.output / "demo-final.mp4"

    ffmpeg.generate_demo_source(source)
    manifest = ingest_source(layout, source, package_root=workspace)

    duration_frames = 180
    timeline = TimelineSpec(
        project_id=project_id,
        width=1280,
        height=720,
        fps=30,
        duration_frames=duration_frames,
        background=BackgroundLayer(
            kind="gradient",
            value="#182848",
            secondary_value="#4B6CB7",
        ),
        layers=[
            VideoLayer(
                id="screen-recording",
                start_frame=0,
                duration_frames=duration_frames,
                z_index=20,
                role="subject",
                src="",
                muted=False,
            ),
            TextLayer(
                id="demo-label",
                start_frame=24,
                duration_frames=132,
                z_index=30,
                text="SCREEN RECORDING WORKFLOW",
                color="#FFFFFF",
                font_size=64,
                animation="scale",
                transform=Transform(y=-20),
            ),
        ],
        captions=[
            CaptionCue(
                id="caption-1",
                start_frame=48,
                end_frame=132,
                text="A small screen-recording fixture",
                emphasis=["screen-recording"],
            )
        ],
    )

    outputs = {
        "project": str(layout.root),
        "source_manifest": str(layout.artifacts / "source-manifest.json"),
        "source_sha256": str(manifest["sha256"]),
        "screen_recording": str(source),
        "final": str(final),
    }

    if render:
        staged_source = remotion.stage_asset(project_id, source)
        render_timeline = timeline.model_copy(
            update={
                "layers": [
                    timeline.layers[0].model_copy(update={"src": staged_source}),
                    timeline.layers[1],
                ],
                "assets": [
                    TimelineAssetRef(
                        asset_id="asset_screen_recording",
                        src=staged_source,
                        sha256=sha256_file(source),
                        role="subject",
                    )
                ],
            }
        )
        props = remotion.write_props(render_timeline, layout.artifacts / "final-timeline.json")
        remotion.render(props, final)
        outputs["final_timeline"] = str(props)

    (layout.artifacts / "demo-result.json").write_text(
        json.dumps(outputs, indent=2), encoding="utf-8"
    )
    return outputs
