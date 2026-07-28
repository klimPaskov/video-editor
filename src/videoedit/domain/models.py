from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RationalFrameRate(StrictModel):
    numerator: int = Field(gt=0)
    denominator: int = Field(gt=0)

    @property
    def as_float(self) -> float:
        return self.numerator / self.denominator


FrameRate = int | RationalFrameRate


class FrameRange(StrictModel):
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.duration_frames


class Transform(StrictModel):
    x: float = 0
    y: float = 0
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    scale: float = Field(default=1, gt=0)
    rotation_degrees: float = 0
    opacity: float = Field(default=1, ge=0, le=1)


class TransformKeyframe(StrictModel):
    frame: int = Field(ge=0)
    x: float | None = None
    y: float | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    scale: float | None = Field(default=None, gt=0)
    rotation_degrees: float | None = None
    opacity: float | None = Field(default=None, ge=0, le=1)
    easing: Literal["linear", "ease_in", "ease_out", "ease_in_out", "hold"] = "linear"


class BackgroundLayer(StrictModel):
    kind: Literal["solid", "gradient", "image", "video"] = "solid"
    value: str = "#111111"
    secondary_value: str | None = None


class TextLayer(StrictModel):
    kind: Literal["text"] = "text"
    id: str
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    z_index: int = 0
    role: Literal["background", "middle", "subject", "front"] = "subject"
    text: str
    color: str = "#FFFFFF"
    font_family: str = "Arial"
    font_size: int = Field(default=72, gt=0)
    font_weight: int = Field(default=700, ge=100, le=900)
    align: Literal["left", "center", "right"] = "center"
    template: Literal["plain", "title", "lower_third", "callout", "diagram", "transition"] = "plain"
    animation: Literal["none", "fade", "slide_up", "scale"] = "fade"
    transform: Transform = Field(default_factory=Transform)
    keyframes: list[TransformKeyframe] = Field(default_factory=list)


class VideoLayer(StrictModel):
    kind: Literal["video"] = "video"
    id: str
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    z_index: int = 0
    role: Literal["background", "middle", "subject", "front"] = "subject"
    src: str
    source_from_frame: int = Field(default=0, ge=0)
    volume: float = Field(default=1, ge=0, le=4)
    muted: bool = False
    transparent: bool = False
    fit: Literal["cover", "contain", "fill"] = "cover"
    transform: Transform = Field(default_factory=Transform)
    keyframes: list[TransformKeyframe] = Field(default_factory=list)
    purposeful_zoom_id: str | None = None


class ImageLayer(StrictModel):
    kind: Literal["image"] = "image"
    id: str
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    z_index: int = 0
    role: Literal["background", "middle", "subject", "front"] = "subject"
    src: str
    fit: Literal["cover", "contain", "fill"] = "contain"
    transform: Transform = Field(default_factory=Transform)
    keyframes: list[TransformKeyframe] = Field(default_factory=list)


class AudioLayer(StrictModel):
    kind: Literal["audio"] = "audio"
    id: str
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    src: str
    source_from_frame: int = Field(default=0, ge=0)
    volume: float = Field(default=1, ge=0, le=4)


TimelineTransitionType = Literal[
    "dip_to_color",
    "swipe_left",
    "swipe_right",
    "push_left",
    "push_right",
    "blur_swipe",
    "chapter_transition",
]
TimelineTransitionDirection = Literal["none", "left", "right", "up", "down"]
TimelineTransitionEasing = Literal[
    "linear",
    "ease_in",
    "ease_out",
    "ease_in_out",
    "smooth_ease_in_out",
]
TimelineTransitionFallback = Literal[
    "hard_cut",
    "j_cut",
    "l_cut",
    "short_crossfade",
    "no_transition",
]


class TimelineTransition(StrictModel):
    """One approved structural transition expressed in Remotion frames."""

    id: str
    start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    transition_type: TimelineTransitionType
    direction: TimelineTransitionDirection = "none"
    easing: TimelineTransitionEasing = "smooth_ease_in_out"
    full_frame_coverage: bool = True
    incoming_first_readable_frame: int = Field(ge=0)
    outgoing_segment_id: str | None = None
    incoming_segment_id: str | None = None
    fallback: TimelineTransitionFallback = "hard_cut"

    @model_validator(mode="after")
    def validate_coverage_and_readability(self) -> TimelineTransition:
        if not self.full_frame_coverage:
            raise ValueError("structural transitions must declare full-frame coverage")
        if self.incoming_first_readable_frame < self.start_frame + self.duration_frames:
            raise ValueError("incoming_first_readable_frame must be at or after the transition end")
        return self


class CaptionCue(StrictModel):
    id: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    text: str
    emphasis: list[str] = Field(default_factory=list)
    style_id: str = "sty_caption_default"
    region: Literal["top", "center", "bottom", "custom"] = "bottom"
    lines: list[str] = Field(default_factory=list)
    word_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range(self) -> CaptionCue:
        if self.end_frame <= self.start_frame:
            raise ValueError("caption end_frame must be greater than start_frame")
        return self


class CaptionSafeArea(StrictModel):
    left: float = Field(default=0.08, ge=0, lt=0.5)
    right: float = Field(default=0.08, ge=0, lt=0.5)
    top: float = Field(default=0.06, ge=0, lt=0.5)
    bottom: float = Field(default=0.10, ge=0, lt=0.5)

    @model_validator(mode="after")
    def validate_sum(self) -> CaptionSafeArea:
        if self.left + self.right >= 1 or self.top + self.bottom >= 1:
            raise ValueError("caption safe area margins leave no usable composition area")
        return self


class TimelineAssetRef(StrictModel):
    asset_id: str
    src: str
    sha256: str
    role: Literal["background", "middle", "subject", "front", "audio", "font"] = "subject"
    font_family: str | None = None
    font_weight: int | None = Field(default=None, ge=100, le=900)


VisualLayer = Annotated[TextLayer | VideoLayer | ImageLayer, Field(discriminator="kind")]


class TimelineSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    fps: FrameRate = 30
    duration_frames: int = Field(gt=0)
    background: BackgroundLayer = Field(default_factory=BackgroundLayer)
    layers: list[VisualLayer] = Field(default_factory=list)
    audio: list[AudioLayer] = Field(default_factory=list)
    captions: list[CaptionCue] = Field(default_factory=list)
    transitions: list[TimelineTransition] = Field(default_factory=list)
    caption_safe_area: CaptionSafeArea = Field(default_factory=CaptionSafeArea)
    assets: list[TimelineAssetRef] = Field(default_factory=list)
    code_bundle_sha256: str | None = None
    focus_pacing_plan_sha256: str | None = None
    transition_plan_sha256: str | None = None

    @field_validator("fps")
    @classmethod
    def validate_fps(cls, value: FrameRate) -> FrameRate:
        if isinstance(value, int):
            if value <= 0 or value > 240:
                raise ValueError("fps must be between 1 and 240")
            return value
        if value.as_float > 240:
            raise ValueError("rational fps must not exceed 240")
        return value

    @model_validator(mode="after")
    def validate_layer_bounds(self) -> TimelineSpec:
        ids: set[str] = set()
        for layer in [*self.layers, *self.audio]:
            if layer.id in ids:
                raise ValueError(f"duplicate timeline item id: {layer.id}")
            ids.add(layer.id)
            if layer.start_frame + layer.duration_frames > self.duration_frames:
                raise ValueError(f"layer {layer.id} exceeds timeline duration")
            keyframes = getattr(layer, "keyframes", [])
            if any(item.frame >= layer.duration_frames for item in keyframes):
                raise ValueError(f"layer {layer.id} has a keyframe outside its duration")
            if any(current.frame >= following.frame for current, following in pairwise(keyframes)):
                raise ValueError(f"layer {layer.id} keyframes must be strictly increasing")
        for cue in self.captions:
            if cue.id in ids:
                raise ValueError(f"duplicate timeline item id: {cue.id}")
            ids.add(cue.id)
            if cue.end_frame > self.duration_frames:
                raise ValueError(f"caption {cue.id} exceeds timeline duration")
        for transition in self.transitions:
            if transition.id in ids:
                raise ValueError(f"duplicate timeline item id: {transition.id}")
            ids.add(transition.id)
            if transition.start_frame + transition.duration_frames > self.duration_frames:
                raise ValueError(f"transition {transition.id} exceeds timeline duration")
            if transition.incoming_first_readable_frame > self.duration_frames:
                raise ValueError(
                    f"transition {transition.id} incoming readable frame exceeds timeline duration"
                )
        ordered_transitions = sorted(self.transitions, key=lambda item: item.start_frame)
        for previous, current in pairwise(ordered_transitions):
            if current.start_frame < previous.start_frame + previous.duration_frames:
                raise ValueError("timeline transitions must not overlap")
        asset_ids: set[str] = set()
        for asset in self.assets:
            if asset.asset_id in asset_ids:
                raise ValueError(f"duplicate timeline asset id: {asset.asset_id}")
            asset_ids.add(asset.asset_id)
        return self


class EffectRequest(BaseModel):
    id: str
    kind: Literal[
        "cut",
        "caption",
        "motion_graphic",
        "sound_effect",
        "broll",
        "track_recolor",
        "track_replace",
        "inpainting",
        "person_matte",
        "background_replace",
        "text_between_subject_and_background",
        "picture_in_picture",
        "screen_focus",
    ]
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    trigger_quote: str | None = None
    target_prompt: str | None = None
    renderer: Literal["ffmpeg", "remotion", "sam3", "matanyone2", "provider"]
    fallback: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    requires_approval: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> EffectRequest:
        if self.end_us <= self.start_us:
            raise ValueError("effect end_us must be greater than start_us")
        return self


class EditPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str
    source_sha256: str
    effects: list[EffectRequest]
    notes: list[str] = Field(default_factory=list)
