import React from 'react';
import {Composition} from 'remotion';
import {AgentEdit} from './AgentEdit';
import {frameRateToNumber} from './types';
import type {TimelineSpec} from './types';

const defaultTransform = {
  x: 0,
  y: 0,
  width: null,
  height: null,
  scale: 1,
  rotation_degrees: 0,
  opacity: 1,
};

const defaultProps: TimelineSpec = {
  schema_version: '1.0',
  project_id: 'preview',
  width: 1920,
  height: 1080,
  fps: 30,
  duration_frames: 300,
  background: {kind: 'gradient', value: '#182848', secondary_value: '#4B6CB7'},
  layers: [
    {
      kind: 'text',
      id: 'preview-title',
      start_frame: 0,
      duration_frames: 300,
      z_index: 1,
      role: 'front',
      text: 'CODEX VIDEO AGENT',
      color: '#FFFFFF',
      font_family: 'Arial',
      font_size: 92,
      font_weight: 700,
      align: 'center',
      template: 'title',
      animation: 'scale',
      transform: defaultTransform,
      keyframes: [],
    },
  ],
  audio: [],
  captions: [],
  transitions: [],
  caption_safe_area: {left: 0.08, right: 0.08, top: 0.06, bottom: 0.1},
  assets: [],
  code_bundle_sha256: null,
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="AgentEdit"
    component={AgentEdit}
    durationInFrames={defaultProps.duration_frames}
    fps={frameRateToNumber(defaultProps.fps)}
    width={defaultProps.width}
    height={defaultProps.height}
    defaultProps={defaultProps}
    calculateMetadata={({props}) => ({
      durationInFrames: props.duration_frames,
      fps: frameRateToNumber(props.fps),
      width: props.width,
      height: props.height,
    })}
  />
);
