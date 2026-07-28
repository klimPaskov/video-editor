export type RationalFrameRate = {
  numerator: number;
  denominator: number;
};

export type FrameRate = number | RationalFrameRate;

export const frameRateToNumber = (fps: FrameRate): number =>
  typeof fps === 'number' ? fps : fps.numerator / fps.denominator;

export type LayerRole = 'background' | 'middle' | 'subject' | 'front';

export type TimelineAssetRef = {
  asset_id: string;
  src: string;
  sha256: string;
  role: LayerRole | 'audio' | 'font';
  font_family?: string | null;
  font_weight?: number | null;
};

export type Transform = {
  x: number;
  y: number;
  width: number | null;
  height: number | null;
  scale: number;
  rotation_degrees: number;
  opacity: number;
};

export type TransformKeyframe = {
  frame: number;
  x?: number | null;
  y?: number | null;
  width?: number | null;
  height?: number | null;
  scale?: number | null;
  rotation_degrees?: number | null;
  opacity?: number | null;
  easing?: 'linear' | 'ease_in' | 'ease_out' | 'ease_in_out' | 'hold';
};

export type BackgroundLayer = {
  kind: 'solid' | 'gradient' | 'image' | 'video';
  value: string;
  secondary_value: string | null;
};

export type TextLayer = {
  kind: 'text';
  id: string;
  start_frame: number;
  duration_frames: number;
  z_index: number;
  role: LayerRole;
  text: string;
  color: string;
  font_family: string;
  font_size: number;
  font_weight: number;
  align: 'left' | 'center' | 'right';
  template: 'plain' | 'title' | 'lower_third' | 'callout' | 'diagram' | 'transition';
  animation: 'none' | 'fade' | 'slide_up' | 'scale';
  transform: Transform;
  keyframes?: TransformKeyframe[];
  purposeful_zoom_id?: string | null;
};

export type VideoLayer = {
  kind: 'video';
  id: string;
  start_frame: number;
  duration_frames: number;
  z_index: number;
  role: LayerRole;
  src: string;
  source_from_frame: number;
  volume: number;
  muted: boolean;
  transparent?: boolean;
  fit: 'cover' | 'contain' | 'fill';
  transform: Transform;
  keyframes?: TransformKeyframe[];
};

export type ImageLayer = {
  kind: 'image';
  id: string;
  start_frame: number;
  duration_frames: number;
  z_index: number;
  role: LayerRole;
  src: string;
  fit: 'cover' | 'contain' | 'fill';
  transform: Transform;
  keyframes?: TransformKeyframe[];
};

export type AudioLayer = {
  kind: 'audio';
  id: string;
  start_frame: number;
  duration_frames: number;
  src: string;
  source_from_frame: number;
  volume: number;
};

export type TimelineTransition = {
  id: string;
  start_frame: number;
  duration_frames: number;
  transition_type:
    | 'dip_to_color'
    | 'swipe_left'
    | 'swipe_right'
    | 'push_left'
    | 'push_right'
    | 'blur_swipe'
    | 'chapter_transition';
  direction: 'none' | 'left' | 'right' | 'up' | 'down';
  easing: 'linear' | 'ease_in' | 'ease_out' | 'ease_in_out' | 'smooth_ease_in_out';
  full_frame_coverage: true;
  incoming_first_readable_frame: number;
  outgoing_segment_id?: string | null;
  incoming_segment_id?: string | null;
  fallback?: 'hard_cut' | 'j_cut' | 'l_cut' | 'short_crossfade' | 'no_transition';
};

export type CaptionCue = {
  id: string;
  start_frame: number;
  end_frame: number;
  text: string;
  emphasis: string[];
  style_id?: string;
  region?: 'top' | 'center' | 'bottom' | 'custom';
  lines?: string[];
  word_ids?: string[];
};

export type VisualLayer = TextLayer | VideoLayer | ImageLayer;

export type TimelineSpec = {
  schema_version: '1.0';
  project_id: string;
  width: number;
  height: number;
  fps: FrameRate;
  duration_frames: number;
  background: BackgroundLayer;
  layers: VisualLayer[];
  audio: AudioLayer[];
  captions: CaptionCue[];
  transitions?: TimelineTransition[];
  caption_safe_area?: {
    left: number;
    right: number;
    top: number;
    bottom: number;
  };
  assets?: TimelineAssetRef[];
  code_bundle_sha256?: string | null;
  focus_pacing_plan_sha256?: string | null;
  transition_plan_sha256?: string | null;
};
