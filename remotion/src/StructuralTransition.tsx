import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';
import type {TimelineTransition} from './types';

const transitionColor = (transition: TimelineTransition): string => {
  if (transition.transition_type === 'chapter_transition') {
    return '#030712';
  }
  if (transition.transition_type === 'dip_to_color') {
    return '#111827';
  }
  return '#172554';
};

const clamp01 = (value: number): number => Math.max(0, Math.min(1, value));

const easeProgress = (
  progress: number,
  easing: TimelineTransition['easing'],
): number => {
  const clamped = clamp01(progress);
  if (easing === 'ease_in') {
    return clamped * clamped;
  }
  if (easing === 'ease_out') {
    return 1 - (1 - clamped) * (1 - clamped);
  }
  if (easing === 'ease_in_out') {
    return clamped < 0.5
      ? 2 * clamped * clamped
      : 1 - Math.pow(-2 * clamped + 2, 2) / 2;
  }
  if (easing === 'smooth_ease_in_out') {
    return interpolate(clamped, [0, 1], [0, 1]) ** 2 * (3 - 2 * clamped);
  }
  return clamped;
};

const coverTransform = (
  transition: TimelineTransition,
  progress: number,
): string => {
  const travel = interpolate(progress, [0, 1], [0, 124]);
  if (transition.direction === 'right') {
    return `translateX(${-travel}%)`;
  }
  if (transition.direction === 'up') {
    return `translateY(${travel}%)`;
  }
  if (transition.direction === 'down') {
    return `translateY(${-travel}%)`;
  }
  return `translateX(${travel}%)`;
};

const DipTransition: React.FC<{
  transition: TimelineTransition;
  progress: number;
}> = ({transition, progress}) => {
  const opacity = interpolate(
    progress,
    [0, 0.5, 1],
    [0, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  return (
    <AbsoluteFill
      aria-hidden="true"
      style={{
        backgroundColor: transitionColor(transition),
        opacity,
        pointerEvents: 'none',
      }}
    />
  );
};
export const StructuralTransition: React.FC<{transition: TimelineTransition}> = ({
  transition,
}) => {
  const frame = useCurrentFrame();
  const incomingFrame = transition.incoming_first_readable_frame - transition.start_frame;
  if (incomingFrame <= 0 || frame >= incomingFrame) {
    return null;
  }

  const lastFrame = Math.max(1, Math.min(transition.duration_frames - 1, incomingFrame - 1));
  const rawProgress = interpolate(
    frame,
    [0, lastFrame],
    [0, 1],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const progress = easeProgress(rawProgress, transition.easing);

  if (
    transition.transition_type === 'dip_to_color' ||
    transition.transition_type === 'chapter_transition'
  ) {
    return <DipTransition transition={transition} progress={progress} />;
  }

  const blur = transition.transition_type === 'blur_swipe' ? ' blur(12px)' : '';
  return (
    <AbsoluteFill aria-hidden="true" style={{overflow: 'hidden', pointerEvents: 'none'}}>
      <div
        style={{
          position: 'absolute',
          left: '-12%',
          top: '-12%',
          width: '124%',
          height: '124%',
          backgroundColor: transitionColor(transition),
          transform: coverTransform(transition, progress),
          filter: blur.trim() ? blur.trim() : undefined,
          boxShadow: '0 0 0 2px rgba(255,255,255,0.08)',
        }}
      />
    </AbsoluteFill>
  );
};
