from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, cast

from videoedit.domain.models import (
    BackgroundLayer,
    CaptionCue,
    RationalFrameRate,
    TimelineAssetRef,
    TimelineSpec,
    TimelineTransition,
    TimelineTransitionDirection,
    TimelineTransitionEasing,
    TimelineTransitionFallback,
    TimelineTransitionType,
    VideoLayer,
)
from videoedit.domain.timeline import microseconds_to_frame
from videoedit.services.artifacts import (
    canonical_sha256,
    validate_artifact,
    write_text_atomically,
)
from videoedit.services.focus_pacing import (
    FocusPacingPlan,
    PurposefulZoom,
    build_zoom_keyframes,
)
from videoedit.services.media import parse_rate
from videoedit.services.project import ProjectLayout, sha256_file
from videoedit.services.transitions import MOTION_TRANSITION_TYPES

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
NETWORK_REF_PATTERN = re.compile(r"^(?:https?|ftp):", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TransitionCompilation:
    transitions: tuple[TimelineTransition, ...]
    transition_plan_sha256: str
    skipped_transition_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def frame_rate_parts(value: int | RationalFrameRate | Mapping[str, Any]) -> tuple[int, int]:
    if isinstance(value, RationalFrameRate):
        return value.numerator, value.denominator
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("fps must be positive")
        return value, 1
    try:
        numerator = int(value["numerator"])
        denominator = int(value["denominator"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("fps must be an integer or rational object") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("rational fps values must be positive")
    return numerator, denominator


def _safe_local_ref(value: str) -> None:
    if NETWORK_REF_PATTERN.match(value) or value.startswith(("data:", "blob:")):
        raise ValueError("visual timeline assets must be local static files")
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"visual timeline asset escapes its static root: {value}")


def _validate_asset_refs(
    payload: Mapping[str, Any],
    *,
    asset_root: Path | None,
) -> None:
    assets = payload.get("assets", [])
    if assets is None:
        return
    if not isinstance(assets, list):
        raise ValueError("visual timeline assets must be an array")
    seen: set[str] = set()
    for item in assets:
        if not isinstance(item, dict):
            raise ValueError("visual timeline asset reference must be an object")
        asset_id = str(item.get("asset_id", ""))
        src = str(item.get("src", ""))
        digest = str(item.get("sha256", ""))
        if not asset_id or not src or not SHA256_PATTERN.fullmatch(digest):
            raise ValueError("visual timeline asset reference is not hash-bound")
        if asset_id in seen:
            raise ValueError(f"duplicate visual timeline asset id: {asset_id}")
        seen.add(asset_id)
        _safe_local_ref(src)
        if asset_root is not None:
            staged = (asset_root / src).resolve()
            try:
                staged.relative_to(asset_root.resolve())
            except ValueError as exc:
                raise ValueError(f"visual timeline asset escapes its static root: {src}") from exc
            if not staged.is_file():
                raise ValueError(f"staged visual timeline asset is missing: {src}")
            if sha256_file(staged) != digest:
                raise ValueError(f"staged visual timeline asset hash does not match: {src}")


def validate_visual_timeline(
    package_root: Path,
    payload: Mapping[str, Any],
    *,
    asset_root: Path | None = None,
) -> TimelineSpec:
    value = dict(payload)
    validate_artifact(package_root, "visual_timeline", value)
    _validate_asset_refs(value, asset_root=asset_root)
    source_refs: list[str] = []
    for item in value.get("layers", []):
        if isinstance(item, dict) and isinstance(item.get("src"), str):
            source = str(item["src"])
            _safe_local_ref(source)
            source_refs.append(source)
    for item in value.get("audio", []):
        if isinstance(item, dict) and isinstance(item.get("src"), str):
            source = str(item["src"])
            _safe_local_ref(source)
            source_refs.append(source)
    background = value.get("background")
    if isinstance(background, dict) and background.get("kind") in {"image", "video"}:
        if isinstance(background.get("value"), str):
            source = str(background["value"])
            _safe_local_ref(source)
            source_refs.append(source)
    if asset_root is not None:
        assets = value.get("assets", [])
        declared_sources = {
            str(item.get("src"))
            for item in assets
            if isinstance(item, dict) and isinstance(item.get("src"), str)
        }
        missing_sources = sorted(set(source_refs) - declared_sources)
        if missing_sources:
            raise ValueError(
                "visual timeline source is missing a hash-bound asset reference: "
                + ", ".join(missing_sources)
            )
    model = TimelineSpec.model_validate(value)
    if model.code_bundle_sha256 is not None and not SHA256_PATTERN.fullmatch(
        model.code_bundle_sha256
    ):
        raise ValueError("visual timeline code bundle hash is invalid")
    return model


def write_visual_timeline(
    package_root: Path,
    path: Path,
    timeline: TimelineSpec | Mapping[str, Any],
    *,
    asset_root: Path | None = None,
) -> Path:
    model = (
        timeline
        if isinstance(timeline, TimelineSpec)
        else validate_visual_timeline(package_root, timeline, asset_root=asset_root)
    )
    payload = model.model_dump(mode="json")
    validate_visual_timeline(package_root, payload, asset_root=asset_root)
    return write_text_atomically(path, json.dumps(payload, indent=2) + "\n")


def _caption_cues_from_plan(
    events: Sequence[Mapping[str, Any]],
    frame_rate: tuple[int, int],
    duration_frames: int,
) -> list[CaptionCue]:
    numerator, denominator = frame_rate
    cues: list[CaptionCue] = []
    for event in events:
        start_us = int(event["start_us"])
        end_us = int(event["end_us"])
        start_frame = microseconds_to_frame(start_us, numerator, denominator)
        end_frame = microseconds_to_frame(end_us, numerator, denominator)
        start_frame = max(0, min(duration_frames - 1, start_frame))
        end_frame = max(start_frame + 1, min(duration_frames, end_frame))
        emphasis_words: list[str] = []
        event_text = str(event.get("text", ""))
        for item in event.get("emphasis", []):
            if not isinstance(item, Mapping):
                continue
            start_char = item.get("start_char")
            end_char = item.get("end_char")
            if not isinstance(start_char, int) or not isinstance(end_char, int):
                continue
            emphasis_words.extend(
                token.casefold()
                for token in re.findall(r"[\w]+", event_text[start_char:end_char], re.UNICODE)
            )
        if not emphasis_words:
            emphasis_words = [
                str(item)
                for item in event.get("emphasis", [])
                if isinstance(item, str) and item.casefold() != "emphasis"
            ]
        cues.append(
            CaptionCue(
                id=str(event["caption_id"]),
                start_frame=start_frame,
                end_frame=end_frame,
                text=str(event["text"]),
                emphasis=list(dict.fromkeys(emphasis_words)),
                style_id=str(event.get("style_id", "sty_caption_default")),
                region=cast(
                    Literal["top", "center", "bottom", "custom"],
                    str(event.get("region", "bottom")),
                ),
                lines=[str(line) for line in event.get("lines", [])],
                word_ids=[str(word_id) for word_id in event.get("word_ids", [])],
            )
        )
    return cues


def compile_transition_plan(
    package_root: Path,
    transition_plan_path: Path,
    *,
    fps: tuple[int, int],
    duration_frames: int,
    approved_transition_ids: set[str] | None = None,
) -> TransitionCompilation:
    """Compile approved motion entries from a transition plan into frame props.

    The transition plan is a proposal artifact. An explicit set of approved IDs is
    therefore required to render any motion entry. Unapproved, blocked, incomplete,
    or clean-cut entries are kept as deterministic clean-cut fallbacks and recorded
    in the compilation diagnostics.
    """

    plan_path = transition_plan_path.expanduser().resolve()
    payload = _read_object(plan_path, "transition plan")
    validate_artifact(package_root, "transition_plan", payload)
    if payload.get("timeline_basis") != "output":
        raise ValueError("transition plan must use output time")
    if duration_frames <= 0:
        raise ValueError("transition compilation requires a positive timeline duration")
    if len(fps) != 2 or fps[0] <= 0 or fps[1] <= 0:
        raise ValueError("transition compilation requires a positive rational frame rate")
    raw_transitions = payload.get("transitions", [])
    if not isinstance(raw_transitions, list):
        raise ValueError("transition plan transitions must be an array")
    transition_by_id = {
        str(item.get("transition_id")): item
        for item in raw_transitions
        if isinstance(item, Mapping) and isinstance(item.get("transition_id"), str)
    }
    approved = set(approved_transition_ids or set())
    unknown_approved = sorted(approved - set(transition_by_id))
    if unknown_approved:
        raise ValueError(
            "approved transition IDs are not present in the plan: " + ", ".join(unknown_approved)
        )

    compiled: list[TimelineTransition] = []
    skipped: list[str] = []
    warnings: list[str] = []
    for item in raw_transitions:
        if not isinstance(item, Mapping):
            continue
        transition_id = str(item.get("transition_id", ""))
        if not transition_id:
            continue
        transition_type = str(item.get("transition_type", ""))
        if transition_type not in MOTION_TRANSITION_TYPES:
            skipped.append(transition_id)
            continue
        if transition_id not in approved:
            skipped.append(transition_id)
            warnings.append(f"transition_not_approved:{transition_id}")
            continue
        if str(item.get("policy_result")) in {"blocked", "fallback_only"}:
            skipped.append(transition_id)
            warnings.append(f"transition_policy_fallback:{transition_id}")
            continue
        if item.get("full_frame_coverage") is not True:
            skipped.append(transition_id)
            warnings.append(f"transition_full_frame_coverage_missing:{transition_id}")
            continue
        if item.get("sound_sync_status") != "pass":
            skipped.append(transition_id)
            warnings.append(f"transition_sound_sync_not_pass:{transition_id}")
            continue
        range_value = item.get("range")
        if not isinstance(range_value, Mapping):
            skipped.append(transition_id)
            warnings.append(f"transition_range_missing:{transition_id}")
            continue
        first_readable_us = item.get("incoming_first_readable_frame_us")
        if not isinstance(first_readable_us, int):
            skipped.append(transition_id)
            warnings.append(f"transition_incoming_readability_missing:{transition_id}")
            continue
        try:
            start_us = int(range_value["start_us"])
            end_us = int(range_value["end_us"])
        except (KeyError, TypeError, ValueError):
            skipped.append(transition_id)
            warnings.append(f"transition_range_invalid:{transition_id}")
            continue
        start_frame = microseconds_to_frame(start_us, fps[0], fps[1])
        end_frame = microseconds_to_frame(end_us, fps[0], fps[1])
        incoming_frame = microseconds_to_frame(first_readable_us, fps[0], fps[1])
        end_frame = min(end_frame, incoming_frame, duration_frames)
        if start_frame < 0 or end_frame <= start_frame:
            skipped.append(transition_id)
            warnings.append(f"transition_frame_range_invalid:{transition_id}")
            continue
        if incoming_frame > duration_frames:
            skipped.append(transition_id)
            warnings.append(f"transition_incoming_readability_out_of_bounds:{transition_id}")
            continue
        try:
            compiled.append(
                TimelineTransition(
                    id=transition_id,
                    start_frame=start_frame,
                    duration_frames=end_frame - start_frame,
                    transition_type=cast(TimelineTransitionType, transition_type),
                    direction=cast(TimelineTransitionDirection, str(item.get("direction", "none"))),
                    easing=cast(
                        TimelineTransitionEasing,
                        str(item.get("easing", "smooth_ease_in_out")),
                    ),
                    full_frame_coverage=True,
                    incoming_first_readable_frame=incoming_frame,
                    outgoing_segment_id=(
                        str(item["outgoing_segment_id"])
                        if item.get("outgoing_segment_id") is not None
                        else None
                    ),
                    incoming_segment_id=(
                        str(item["incoming_segment_id"])
                        if item.get("incoming_segment_id") is not None
                        else None
                    ),
                    fallback=cast(
                        TimelineTransitionFallback, str(item.get("fallback", "hard_cut"))
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            skipped.append(transition_id)
            warnings.append(f"transition_contract_invalid:{transition_id}:{exc}")

    compiled.sort(key=lambda item: (item.start_frame, item.id))
    for previous, current in pairwise(compiled):
        if current.start_frame < previous.start_frame + previous.duration_frames:
            raise ValueError("compiled structural transitions overlap")
    return TransitionCompilation(
        transitions=tuple(compiled),
        transition_plan_sha256=sha256_file(plan_path),
        skipped_transition_ids=tuple(skipped),
        warnings=tuple(warnings),
    )


def build_base_visual_timeline(
    package_root: Path,
    layout: ProjectLayout,
    render_manifest_path: Path,
    *,
    asset_src: str,
    caption_plan_path: Path | None = None,
    revision_id: str = "rev_001",
    code_bundle_sha256: str | None = None,
) -> TimelineSpec:
    render_manifest_path = render_manifest_path.expanduser().resolve()
    manifest = _read_object(render_manifest_path, "render manifest")
    validate_artifact(package_root, "render_manifest", manifest)
    if manifest.get("project_id") != layout.root.name:
        raise ValueError("render manifest project does not match the project")
    if manifest.get("revision_id") != revision_id:
        raise ValueError("render manifest revision does not match the project")
    video = manifest.get("video")
    if not isinstance(video, dict):
        raise ValueError("render manifest video metadata is missing")
    frame_rate = parse_rate(
        f"{video.get('frame_rate', {}).get('numerator')}/"
        f"{video.get('frame_rate', {}).get('denominator')}"
    )
    if frame_rate is None:
        raise ValueError("render manifest has no rational frame rate")
    duration_frames = int(manifest.get("frame_count") or manifest.get("expected_frame_count") or 0)
    if duration_frames <= 0:
        raise ValueError("render manifest has no positive frame count")
    output = manifest.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("sha256"), str):
        raise ValueError("render manifest output hash is missing")
    output_path_value = output.get("path")
    if not isinstance(output_path_value, str):
        raise ValueError("render manifest output path is missing")
    output_path = Path(output_path_value).expanduser().resolve()
    try:
        output_path.relative_to(layout.root.resolve())
    except ValueError as exc:
        raise ValueError("render manifest output escapes the project") from exc
    if not output_path.is_file():
        raise ValueError(f"render manifest output is missing: {output_path}")
    if sha256_file(output_path) != output["sha256"]:
        raise ValueError("render manifest output hash does not match the file")
    numerator = int(frame_rate["numerator"])
    denominator = int(frame_rate["denominator"])
    captions: list[CaptionCue] = []
    if caption_plan_path is not None:
        caption_plan = _read_object(caption_plan_path, "caption plan")
        validate_artifact(package_root, "caption_plan", caption_plan)
        events = caption_plan.get("events", [])
        if not isinstance(events, list):
            raise ValueError("caption plan events must be an array")
        captions = _caption_cues_from_plan(
            [item for item in events if isinstance(item, dict)],
            (numerator, denominator),
            duration_frames,
        )
    timeline = TimelineSpec(
        project_id=layout.root.name,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=RationalFrameRate(numerator=numerator, denominator=denominator),
        duration_frames=duration_frames,
        background=BackgroundLayer(kind="solid", value="#000000"),
        layers=[
            VideoLayer(
                id="base-edit",
                start_frame=0,
                duration_frames=duration_frames,
                z_index=20,
                role="subject",
                src=asset_src,
                muted=False,
                volume=1,
            )
        ],
        audio=[],
        captions=captions,
        assets=[
            TimelineAssetRef(
                asset_id="asset_base_edit",
                src=asset_src,
                sha256=str(output["sha256"]),
                role="subject",
            )
        ],
        code_bundle_sha256=code_bundle_sha256,
    )
    validate_visual_timeline(package_root, timeline.model_dump(mode="json"))
    return timeline


def timeline_artifact_key(timeline: TimelineSpec) -> str:
    return canonical_sha256(timeline.model_dump(mode="json"))


def apply_purposeful_zooms(
    timeline: TimelineSpec,
    plan: FocusPacingPlan | Mapping[str, Any],
    *,
    layer_id: str = "base-edit",
    approved_zoom_ids: set[str] | None = None,
) -> TimelineSpec:
    """Add only approved, target-driven zoom keyframes to one video layer."""

    selected_plan = (
        plan if isinstance(plan, FocusPacingPlan) else FocusPacingPlan.model_validate(dict(plan))
    )
    plan_zoom_ids = {zoom.zoom_id for zoom in selected_plan.zooms}
    if approved_zoom_ids is not None:
        unknown_zoom_ids = sorted(set(approved_zoom_ids) - plan_zoom_ids)
        if unknown_zoom_ids:
            raise ValueError(
                "approved purposeful zoom IDs are not present in the focus plan: "
                + ", ".join(unknown_zoom_ids)
            )
    selected: list[PurposefulZoom] = []
    for zoom in selected_plan.zooms:
        if zoom.policy_result in {"blocked", "skipped"}:
            continue
        if approved_zoom_ids is None:
            if zoom.policy_result != "auto_eligible":
                continue
        elif zoom.zoom_id not in approved_zoom_ids:
            continue
        selected.append(zoom)
    selected.sort(key=lambda item: item.source_range.start_us)
    for previous, current in pairwise(selected):
        if current.source_range.start_us < previous.source_range.end_us:
            raise ValueError("purposeful zoom ranges overlap")
    layer_index = next(
        (
            index
            for index, layer in enumerate(timeline.layers)
            if isinstance(layer, VideoLayer) and layer.id == layer_id
        ),
        None,
    )
    if layer_index is None:
        raise ValueError(f"video layer is missing for purposeful zooms: {layer_id}")
    layer = timeline.layers[layer_index]
    if not isinstance(layer, VideoLayer):
        raise ValueError(f"timeline layer is not a video layer: {layer_id}")
    fps_numerator, fps_denominator = frame_rate_parts(timeline.fps)
    keyframes: list[Any] = list(layer.keyframes)
    for zoom in selected:
        keyframes.extend(
            build_zoom_keyframes(
                zoom,
                fps_numerator=fps_numerator,
                fps_denominator=fps_denominator,
                width=timeline.width,
                height=timeline.height,
                layer_start_frame=layer.start_frame,
            )
        )
    keyframes.sort(key=lambda item: item.frame)
    for previous, current in pairwise(keyframes):
        if current.frame <= previous.frame:
            raise ValueError("purposeful zoom keyframes must have unique increasing frames")
    updated_layer = layer.model_copy(
        update={
            "keyframes": keyframes,
            "purposeful_zoom_id": selected[0].zoom_id if len(selected) == 1 else None,
        }
    )
    layers = list(timeline.layers)
    layers[layer_index] = updated_layer
    return timeline.model_copy(update={"layers": layers})
