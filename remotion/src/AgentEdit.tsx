import React from 'react';
import {
  AbsoluteFill,
  Html5Audio,
  Img,
  interpolate,
  OffthreadVideo,
  Sequence,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import type {
  AudioLayer,
  BackgroundLayer,
  CaptionCue,
  ImageLayer,
  TextLayer,
  TimelineSpec,
  Transform,
  TransformKeyframe,
  VideoLayer,
  VisualLayer,
} from './types';
import {TextPrimitive} from './primitives';
import {StructuralTransition} from './StructuralTransition';

const FontFaces: React.FC<{assets?: TimelineSpec['assets']}> = ({assets}) => {
  const fontRules = (assets ?? [])
    .filter((asset) => asset.role === 'font' && asset.font_family)
    .map((asset) => {
      const format = asset.src.toLocaleLowerCase().endsWith('.woff2')
        ? 'woff2'
        : asset.src.toLocaleLowerCase().endsWith('.woff')
          ? 'woff'
          : 'truetype';
      const weight = asset.font_weight ?? 400;
      return (
        '@font-face{font-family:"' +
        asset.font_family +
        '";src:url("' +
        assetSource(asset.src) +
        '") format("' +
        format +
        '");font-weight:' +
        String(weight) +
        ';font-style:normal;font-display:block;}'
      );
    });
  return fontRules.length > 0 ? <style>{fontRules.join('\n')}</style> : null;
};

const assetSource = (src: string): string => {
  if (/^(https?:|data:|blob:)/.test(src)) {
    return src;
  }
  return staticFile(src.replace(/^\//, ''));
};

const transformStyle = (transform: Transform): React.CSSProperties => ({
  left: '50%',
  top: '50%',
  width: transform.width ?? '100%',
  height: transform.height ?? '100%',
  opacity: transform.opacity,
  transform: `translate(-50%, -50%) translate(${transform.x}px, ${transform.y}px) scale(${transform.scale}) rotate(${transform.rotation_degrees}deg)`,
  transformOrigin: 'center center',
});

type NumericTransformKey = Exclude<keyof Transform, 'width' | 'height'>;

const interpolateKeyframes = (
  frame: number,
  base: number,
  keyframes: TransformKeyframe[],
  property: NumericTransformKey,
): number => {
  const points = keyframes
    .filter((item) => typeof item[property] === 'number')
    .map((item) => ({
      frame: item.frame,
      value: item[property] as number,
      easing: item.easing ?? 'linear',
    }));
  if (points.length === 0) {
    return base;
  }
  if (points[0].frame > 0) {
    points.unshift({frame: 0, value: base, easing: 'linear'});
  }
  if (frame <= points[0].frame) {
    return points[0].value;
  }
  for (let index = 0; index < points.length - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    if (frame > end.frame) {
      continue;
    }
    const progress = (frame - start.frame) / Math.max(1, end.frame - start.frame);
    const eased =
      end.easing === 'hold'
        ? 0
        : end.easing === 'ease_in'
          ? progress * progress
          : end.easing === 'ease_out'
            ? 1 - (1 - progress) * (1 - progress)
            : end.easing === 'ease_in_out'
              ? progress < 0.5
                ? 2 * progress * progress
                : 1 - Math.pow(-2 * progress + 2, 2) / 2
              : progress;
    return start.value + (end.value - start.value) * eased;
  }
  return points[points.length - 1].value;
};

const interpolateDimension = (
  frame: number,
  base: number | null,
  keyframes: TransformKeyframe[],
  property: 'width' | 'height',
): number | null => {
  const points = keyframes
    .filter((item) => typeof item[property] === 'number')
    .map((item) => ({frame: item.frame, value: item[property] as number}));
  if (points.length === 0) {
    return base;
  }
  if (points[0].frame > 0) {
    points.unshift({frame: 0, value: base ?? points[0].value});
  }
  return interpolate(
    frame,
    points.map((point) => point.frame),
    points.map((point) => point.value),
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
};

const useAnimatedTransform = (
  base: Transform,
  keyframes: TransformKeyframe[],
): Transform => {
  const frame = useCurrentFrame();
  return {
    x: interpolateKeyframes(frame, base.x, keyframes, 'x'),
    y: interpolateKeyframes(frame, base.y, keyframes, 'y'),
    width: interpolateDimension(frame, base.width, keyframes, 'width'),
    height: interpolateDimension(frame, base.height, keyframes, 'height'),
    scale: interpolateKeyframes(frame, base.scale, keyframes, 'scale'),
    rotation_degrees: interpolateKeyframes(
      frame,
      base.rotation_degrees,
      keyframes,
      'rotation_degrees',
    ),
    opacity: interpolateKeyframes(frame, base.opacity, keyframes, 'opacity'),
  };
};

const Background: React.FC<{background: BackgroundLayer}> = ({background}) => {
  if (background.kind === 'gradient') {
    return (
      <AbsoluteFill
        style={{
          background: `linear-gradient(135deg, ${background.value}, ${background.secondary_value ?? background.value})`,
        }}
      />
    );
  }
  if (background.kind === 'image') {
    return <Img src={assetSource(background.value)} style={{width: '100%', height: '100%', objectFit: 'cover'}} />;
  }
  if (background.kind === 'video') {
    return (
      <OffthreadVideo
        src={assetSource(background.value)}
        muted
        style={{width: '100%', height: '100%', objectFit: 'cover'}}
      />
    );
  }
  return <AbsoluteFill style={{backgroundColor: background.value}} />;
};

const TextVisual: React.FC<{layer: TextLayer}> = ({layer}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const transform = useAnimatedTransform(layer.transform, layer.keyframes ?? []);
  const fade = interpolate(frame, [0, Math.max(1, Math.round(fps * 0.2))], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const progress = spring({frame, fps, config: {damping: 18, mass: 0.7, stiffness: 130}});
  const animationTransform =
    layer.animation === 'slide_up'
      ? `translateY(${interpolate(progress, [0, 1], [55, 0])}px)`
      : layer.animation === 'scale'
        ? `scale(${interpolate(progress, [0, 1], [0.75, 1])})`
        : 'none';
  const animationOpacity = layer.animation === 'none' ? 1 : fade;

  return (
    <div
      style={{
        position: 'absolute',
        display: 'flex',
        alignItems: 'center',
        justifyContent: layer.align === 'left' ? 'flex-start' : layer.align === 'right' ? 'flex-end' : 'center',
        boxSizing: 'border-box',
        padding: 40,
        color: layer.color,
        fontFamily: layer.font_family,
        fontSize: layer.font_size,
        fontWeight: layer.font_weight,
        textAlign: layer.align,
        lineHeight: 1.05,
        whiteSpace: 'pre-wrap',
        ...transformStyle(transform),
        opacity: transform.opacity * animationOpacity,
      }}
    >
      <TextPrimitive
        layer={layer}
        animationTransform={animationTransform}
        animationOpacity={animationOpacity}
        progress={progress}
      />
    </div>
  );
};

const VideoVisual: React.FC<{layer: VideoLayer}> = ({layer}) => {
  const transform = useAnimatedTransform(layer.transform, layer.keyframes ?? []);
  return (
    <OffthreadVideo
      src={assetSource(layer.src)}
      trimBefore={layer.source_from_frame}
      muted={layer.muted}
      volume={layer.volume}
      transparent={layer.transparent ?? false}
      style={{
        position: 'absolute',
        objectFit: layer.fit,
        ...transformStyle(transform),
      }}
    />
  );
};

const ImageVisual: React.FC<{layer: ImageLayer}> = ({layer}) => {
  const transform = useAnimatedTransform(layer.transform, layer.keyframes ?? []);
  return (
    <Img
      src={assetSource(layer.src)}
      style={{
        position: 'absolute',
        objectFit: layer.fit,
        ...transformStyle(transform),
      }}
    />
  );
};

const Visual: React.FC<{layer: VisualLayer}> = ({layer}) => {
  if (layer.kind === 'text') {
    return <TextVisual layer={layer} />;
  }
  if (layer.kind === 'video') {
    return <VideoVisual layer={layer} />;
  }
  return <ImageVisual layer={layer} />;
};

const AudioVisual: React.FC<{layer: AudioLayer}> = ({layer}) => (
  <Html5Audio
    src={assetSource(layer.src)}
    trimBefore={layer.source_from_frame}
    volume={layer.volume}
  />
);

const Caption: React.FC<{
  cue: CaptionCue;
  safeArea: NonNullable<TimelineSpec['caption_safe_area']>;
}> = ({cue, safeArea}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const opacity = interpolate(frame, [0, Math.max(1, Math.round(fps * 0.12))], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const emphasized = new Set(cue.emphasis.map((word) => word.toLocaleLowerCase()));
  const tokens = (cue.lines?.length ? cue.lines.join('\n') : cue.text).split(/(\s+)/);
  const positionStyle: React.CSSProperties =
    cue.region === 'top'
      ? {justifyContent: 'flex-start', paddingTop: String(safeArea.top * 100) + '%'}
      : cue.region === 'center'
        ? {justifyContent: 'center'}
        : {justifyContent: 'flex-end', paddingBottom: String(safeArea.bottom * 100) + '%'};

  return (
    <AbsoluteFill
      style={{
        alignItems: 'center',
        paddingLeft: String(safeArea.left * 100) + '%',
        paddingRight: String(safeArea.right * 100) + '%',
        opacity,
        ...positionStyle,
      }}
    >
      <div
        style={{
          maxWidth: String((1 - safeArea.left - safeArea.right) * 100) + '%',
          padding: '14px 24px',
          borderRadius: 14,
          backgroundColor: 'rgba(0, 0, 0, 0.72)',
          color: 'white',
          fontFamily: 'Arial',
          fontSize: 42,
          fontWeight: 700,
          lineHeight: 1.12,
          textAlign: 'center',
          boxShadow: '0 6px 24px rgba(0,0,0,0.26)',
        }}
      >
        {tokens.map((token, index) => {
          const normalized = token.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
          return (
            <span key={`${cue.id}-${index}`} style={{color: emphasized.has(normalized) ? '#FFD166' : 'white'}}>
              {token}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

export const AgentEdit: React.FC<TimelineSpec> = (props) => {
  const roleOrder: Record<VisualLayer['role'], number> = {
    background: 0,
    middle: 1,
    subject: 2,
    front: 3,
  };
  const layers = [...props.layers].sort(
    (a, b) =>
      roleOrder[a.role] - roleOrder[b.role] ||
      a.z_index - b.z_index ||
      a.id.localeCompare(b.id),
  );
  const safeArea = props.caption_safe_area ?? {
    left: 0.08,
    right: 0.08,
    top: 0.06,
    bottom: 0.1,
  };
  return (
    <AbsoluteFill style={{backgroundColor: '#000', overflow: 'hidden'}}>
      <FontFaces assets={props.assets} />
      <Background background={props.background} />
      {layers.map((layer) => (
        <Sequence
          key={layer.id}
          from={layer.start_frame}
          durationInFrames={layer.duration_frames}
          layout="none"
          name={layer.id}
        >
          <Visual layer={layer} />
        </Sequence>
      ))}
      {props.audio.map((layer) => (
        <Sequence
          key={layer.id}
          from={layer.start_frame}
          durationInFrames={layer.duration_frames}
          layout="none"
          name={layer.id}
        >
          <AudioVisual layer={layer} />
        </Sequence>
      ))}
      {props.captions.map((cue) => (
        <Sequence
          key={cue.id}
          from={cue.start_frame}
          durationInFrames={cue.end_frame - cue.start_frame}
          name={cue.id}
        >
          <Caption cue={cue} safeArea={safeArea} />
        </Sequence>
      ))}
      {(props.transitions ?? []).map((transition) => {
        const readableDuration =
          transition.incoming_first_readable_frame - transition.start_frame;
        const durationInFrames = Math.min(transition.duration_frames, readableDuration);
        if (durationInFrames <= 0) {
          return null;
        }
        const boundedTransition =
          durationInFrames === transition.duration_frames
            ? transition
            : {...transition, duration_frames: durationInFrames};
        return (
          <Sequence
            key={transition.id}
            from={transition.start_frame}
            durationInFrames={durationInFrames}
            layout="none"
            name={transition.id}
          >
            <StructuralTransition transition={boundedTransition} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
