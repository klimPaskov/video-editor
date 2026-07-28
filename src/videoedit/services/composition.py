from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videoedit.domain.models import (
    AudioLayer,
    BackgroundLayer,
    CaptionSafeArea,
    ImageLayer,
    RationalFrameRate,
    TextLayer,
    TimelineAssetRef,
    TimelineSpec,
    Transform,
    VideoLayer,
)
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.services.artifacts import validate_artifact
from videoedit.services.focus_pacing import FocusPacingPlan, read_focus_pacing_plan
from videoedit.services.media import seconds_to_us
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.remotion import RemotionService
from videoedit.services.retiming import read_retimed_timeline, rebase_focus_pacing_plan
from videoedit.services.visual_timeline import (
    _caption_cues_from_plan,
    apply_purposeful_zooms,
    compile_transition_plan,
    timeline_artifact_key,
)


@dataclass(frozen=True, slots=True)
class CompositionResult:
    timeline_path: Path
    timeline: TimelineSpec
    code_bundle_sha256: str
    composition_bundle_path: Path
    staged_assets: tuple[str, ...]
    transition_warnings: tuple[str, ...] = ()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _duration_frames_from_manifest(
    manifest: dict[str, Any],
    base_path: Path,
    *,
    numerator: int,
    denominator: int,
    remotion: RemotionService,
) -> int:
    explicit = manifest.get("frame_count") or manifest.get("expected_frame_count")
    if explicit is not None and int(explicit) > 0:
        return int(explicit)

    visual_duration_us: int | None = None
    probe = remotion.ffmpeg.probe(base_path)
    streams = probe.get("streams", [])
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("codec_type") != "video":
                continue
            visual_duration_us = seconds_to_us(stream.get("duration"))
            if visual_duration_us is not None:
                break
    if visual_duration_us is None:
        raw_duration = manifest.get("actual_duration_us") or manifest.get("expected_duration_us")
        if isinstance(raw_duration, int) and raw_duration > 0:
            visual_duration_us = raw_duration
    if visual_duration_us is None:
        raise ValueError("render manifest has no decoded visual duration or frame count")
    duration_frames = microseconds_to_frame(visual_duration_us, numerator, denominator)
    if duration_frames <= 0:
        raise ValueError("render manifest visual duration produces no Remotion frames")
    return duration_frames


def build_visual_composition(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    *,
    remotion_directory: Path,
    npm_path: str = "npm",
    caption_plan_path: Path | None = None,
    subject_path: Path | None = None,
    middle_text: str = "TEXT BEHIND SUBJECT",
    front_label: str = "CODEX VIDEO AGENT",
    revision_id: str = "rev_001",
    focus_pacing_plan_path: Path | None = None,
    retimed_timeline_path: Path | None = None,
    approved_zoom_ids: set[str] | None = None,
    transition_plan_path: Path | None = None,
    approved_transition_ids: set[str] | None = None,
) -> CompositionResult:
    manifest_path = render_manifest_path.expanduser().resolve()
    manifest = _read_object(manifest_path, "render manifest")
    validate_artifact(package_root, "render_manifest", manifest)
    if manifest.get("project_id") != layout.root.name or manifest.get("revision_id") != revision_id:
        raise ValueError("render manifest does not belong to the requested project revision")
    output = manifest.get("output")
    video = manifest.get("video")
    if not isinstance(output, dict) or not isinstance(output.get("path"), str):
        raise ValueError("render manifest output path is missing")
    if not isinstance(output.get("sha256"), str):
        raise ValueError("render manifest output hash is missing")
    if not isinstance(video, dict):
        raise ValueError("render manifest video metadata is missing")
    base_path = Path(str(output["path"])).expanduser().resolve()
    if not base_path.is_file() or sha256_file(base_path) != output["sha256"]:
        raise ValueError("render manifest output is missing or hash-mismatched")
    numerator = int(video.get("frame_rate", {}).get("numerator", 0))
    denominator = int(video.get("frame_rate", {}).get("denominator", 0))
    width = int(video.get("width", 0))
    height = int(video.get("height", 0))
    if numerator <= 0 or denominator <= 0 or width <= 0 or height <= 0:
        raise ValueError("render manifest has incomplete visual timeline metadata")
    remotion = RemotionService(
        remotion_directory.expanduser().resolve(),
        npm_path=npm_path,
        package_root=package_root,
    )
    duration_frames = _duration_frames_from_manifest(
        manifest,
        base_path,
        numerator=numerator,
        denominator=denominator,
        remotion=remotion,
    )
    base_src = remotion.stage_asset(
        layout.root.name,
        base_path,
        expected_sha256=str(output["sha256"]),
    )
    assets = [
        TimelineAssetRef(
            asset_id="asset_base_edit",
            src=base_src,
            sha256=str(output["sha256"]),
            role="audio" if subject_path is not None else "subject",
        )
    ]
    staged_assets = [base_src]
    captions = []
    safe_area = CaptionSafeArea()
    if caption_plan_path is not None:
        caption_plan = _read_object(caption_plan_path.expanduser().resolve(), "caption plan")
        validate_artifact(package_root, "caption_plan", caption_plan)
        if caption_plan.get("project_id") != layout.root.name:
            raise ValueError("caption plan does not belong to the project")
        events = caption_plan.get("events", [])
        if not isinstance(events, list):
            raise ValueError("caption plan events must be an array")
        safe_area_value = caption_plan.get("safe_area")
        if safe_area_value is not None:
            if not isinstance(safe_area_value, dict):
                raise ValueError("caption plan safe_area must be an object")
            safe_area = CaptionSafeArea.model_validate(safe_area_value)
        captions = _caption_cues_from_plan(
            [item for item in events if isinstance(item, dict)],
            (numerator, denominator),
            duration_frames,
        )
    layers: list[TextLayer | VideoLayer | ImageLayer] = []
    audio: list[AudioLayer] = []
    if subject_path is None:
        layers.append(
            VideoLayer(
                id="base-edit",
                start_frame=0,
                duration_frames=duration_frames,
                z_index=20,
                role="subject",
                src=base_src,
                muted=False,
                volume=1,
            )
        )
    else:
        subject_path = subject_path.expanduser().resolve()
        subject_src = remotion.stage_asset(layout.root.name, subject_path)
        subject_hash = sha256_file(subject_path)
        assets.append(
            TimelineAssetRef(
                asset_id="asset_subject_foreground",
                src=subject_src,
                sha256=subject_hash,
                role="subject",
            )
        )
        staged_assets.append(subject_src)
        layers.append(
            VideoLayer(
                id="subject-foreground",
                start_frame=0,
                duration_frames=duration_frames,
                z_index=20,
                role="subject",
                src=subject_src,
                muted=True,
                transparent=True,
                fit="cover",
            )
        )
        audio.append(
            AudioLayer(
                id="production-audio",
                start_frame=0,
                duration_frames=duration_frames,
                src=base_src,
                source_from_frame=0,
                volume=1,
            )
        )
    fps = RationalFrameRate(numerator=numerator, denominator=denominator)
    transition_compilation = None
    if transition_plan_path is not None:
        selected_transition_plan = transition_plan_path.expanduser().resolve()
        try:
            selected_transition_plan.relative_to(layout.root.resolve())
        except ValueError as exc:
            raise ValueError("transition plan must be inside the project") from exc
        transition_payload = _read_object(selected_transition_plan, "transition plan")
        validate_artifact(package_root, "transition_plan", transition_payload)
        if (
            transition_payload.get("project_id") != layout.root.name
            or transition_payload.get("revision_id") != revision_id
        ):
            raise ValueError("transition plan project or revision does not match")
        transition_compilation = compile_transition_plan(
            package_root,
            selected_transition_plan,
            fps=(numerator, denominator),
            duration_frames=duration_frames,
            approved_transition_ids=approved_transition_ids,
        )
    if middle_text:
        layers.append(
            TextLayer(
                id="middle-plate-title",
                start_frame=0,
                duration_frames=duration_frames,
                z_index=10,
                role="middle",
                text=middle_text,
                color="#FFFFFF",
                font_family="Arial",
                font_size=max(24, round(height * 0.095)),
                font_weight=800,
                align="center",
                template="title",
                animation="slide_up",
                transform=Transform(y=-round(height * 0.18)),
            )
        )
    if front_label:
        label_duration = min(duration_frames, max(1, round(numerator / denominator * 2.2)))
        layers.append(
            TextLayer(
                id="front-label",
                start_frame=0,
                duration_frames=label_duration,
                z_index=40,
                role="front",
                text=front_label,
                color="#FFD166",
                font_family="Arial",
                font_size=max(18, round(height * 0.055)),
                font_weight=800,
                align="center",
                template="lower_third",
                animation="slide_up",
                transform=Transform(y=-round(height * 0.39)),
            )
        )
    code_hash = remotion.code_bundle_sha256()
    timeline = TimelineSpec(
        project_id=layout.root.name,
        width=width,
        height=height,
        fps=fps,
        duration_frames=duration_frames,
        background=BackgroundLayer(
            kind="gradient",
            value="#182848",
            secondary_value="#4B6CB7",
        ),
        layers=layers,
        audio=audio,
        captions=captions,
        transitions=list(transition_compilation.transitions) if transition_compilation else [],
        caption_safe_area=safe_area,
        assets=assets,
        code_bundle_sha256=code_hash,
        transition_plan_sha256=(
            transition_compilation.transition_plan_sha256 if transition_compilation else None
        ),
    )
    if focus_pacing_plan_path is not None:
        focus_plan_path = focus_pacing_plan_path.expanduser().resolve()
        plan = read_focus_pacing_plan(package_root, focus_plan_path)
        if plan.project_id != layout.root.name or plan.revision_id != revision_id:
            raise ValueError("focus pacing plan does not belong to the project revision")
        approved_zoom_set = set(approved_zoom_ids or set())
        selected_zoom_ids = (
            approved_zoom_set
            if approved_zoom_ids is not None
            else {item.zoom_id for item in plan.zooms if item.policy_result == "auto_eligible"}
        )
        if selected_zoom_ids and retimed_timeline_path is None:
            raise ValueError(
                "approved purposeful zooms require the authoritative retimed timeline for "
                "source-to-output rebasing"
            )
        if retimed_timeline_path is not None:
            retimed = read_retimed_timeline(package_root, retimed_timeline_path.resolve())
            if retimed.project_id != layout.root.name or retimed.revision_id != revision_id:
                raise ValueError("retimed timeline does not belong to the project revision")
            rebased = rebase_focus_pacing_plan(retimed, plan.model_dump(mode="json"))
            plan = FocusPacingPlan.model_validate(rebased)
        zoom_layer_id = "base-edit" if subject_path is None else "subject-foreground"
        timeline = apply_purposeful_zooms(
            timeline,
            plan,
            layer_id=zoom_layer_id,
            approved_zoom_ids=approved_zoom_ids,
        ).model_copy(update={"focus_pacing_plan_sha256": sha256_file(focus_plan_path)})
    timeline_key = timeline_artifact_key(timeline)
    timeline_path = layout.artifacts / f"visual-timeline-{timeline_key}.json"
    remotion.write_props(timeline, timeline_path)
    remotion.write_props(timeline, layout.artifacts / "visual-timeline.json")
    composition_bundle_path = remotion.write_code_bundle(
        layout.work / f"composition-bundle-{code_hash}.json"
    )
    return CompositionResult(
        timeline_path,
        timeline,
        code_hash,
        composition_bundle_path,
        tuple(staged_assets),
        transition_warnings=(transition_compilation.warnings if transition_compilation else ()),
    )
