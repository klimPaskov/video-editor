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
    layout = initialize_project(workspace, project_id)
    ffmpeg = adapter or FFmpegAdapter()
    remotion = RemotionService(
        workspace / "remotion",
        npm_path=npm_path,
        ffmpeg_adapter=ffmpeg,
    )

    source = layout.work / "synthetic-green-screen.mp4"
    mask = layout.work / "synthetic-object-mask.mp4"
    recolored = layout.work / "synthetic-recolored.mp4"
    foreground = layout.work / "synthetic-foreground.mov"
    plate = layout.work / "plate.mp4"
    composite = layout.output / "demo-local.mp4"
    final = layout.output / "demo-final.mp4"

    ffmpeg.generate_demo_source(source)
    ffmpeg.generate_demo_mask(mask)
    manifest = ingest_source(layout, source, package_root=workspace)
    ffmpeg.recolor_with_mask(source, mask, recolored)
    ffmpeg.chroma_key_foreground(recolored, foreground)
    ffmpeg.generate_demo_plate(plate)
    ffmpeg.overlay_foreground(plate, foreground, composite, audio_source=recolored)

    duration_frames = 180
    plate_timeline = TimelineSpec(
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
            TextLayer(
                id="behind-subject",
                start_frame=24,
                duration_frames=132,
                z_index=1,
                text="TEXT BEHIND SUBJECT",
                color="#FFFFFF",
                font_size=74,
                animation="scale",
                transform=Transform(x=0, y=-20),
            )
        ],
    )
    plate_props = remotion.write_props(plate_timeline, layout.artifacts / "plate-timeline.json")

    outputs = {
        "project": str(layout.root),
        "source_manifest": str(layout.artifacts / "source-manifest.json"),
        "source_sha256": str(manifest["sha256"]),
        "recolored": str(recolored),
        "foreground": str(foreground),
        "plate_timeline": str(plate_props),
        "local_composite": str(composite),
        "final": str(final),
    }

    if render:
        composite_asset = remotion.stage_asset(project_id, composite)
        final_timeline = TimelineSpec(
            project_id=project_id,
            width=1280,
            height=720,
            fps=30,
            duration_frames=duration_frames,
            background=BackgroundLayer(kind="solid", value="#000000"),
            layers=[
                VideoLayer(
                    id="composite",
                    start_frame=0,
                    duration_frames=duration_frames,
                    z_index=0,
                    src=composite_asset,
                    muted=False,
                ),
                TextLayer(
                    id="front-label",
                    start_frame=12,
                    duration_frames=70,
                    z_index=10,
                    text="CODEX VIDEO AGENT",
                    color="#FFD166",
                    font_size=44,
                    animation="slide_up",
                    transform=Transform(y=-260),
                ),
            ],
            captions=[
                CaptionCue(
                    id="caption-1",
                    start_frame=48,
                    end_frame=132,
                    text="Mask driven recolor, green screen cutout, and layered text",
                    emphasis=["recolor", "layered"],
                )
            ],
            assets=[
                TimelineAssetRef(
                    asset_id="asset_composite",
                    src=composite_asset,
                    sha256=sha256_file(composite),
                    role="subject",
                )
            ],
        )
        final_props = remotion.write_props(final_timeline, layout.artifacts / "final-timeline.json")
        remotion.render(final_props, final)
        outputs["plate"] = str(plate)
        outputs["composite"] = str(composite)
        outputs["final_timeline"] = str(final_props)

    (layout.artifacts / "demo-result.json").write_text(
        json.dumps(outputs, indent=2), encoding="utf-8"
    )
    return outputs
