import React from 'react';
import type {TextLayer} from './types';

export type TextPrimitiveProps = {
  layer: TextLayer;
  animationTransform: string;
  animationOpacity: number;
  progress: number;
};

const textStyle = (layer: TextLayer): React.CSSProperties => ({
  color: layer.color,
  fontFamily: layer.font_family,
  fontSize: layer.font_size,
  fontWeight: layer.font_weight,
  lineHeight: 1.05,
  textAlign: layer.align,
});

export const Typography: React.FC<TextPrimitiveProps> = ({layer, animationTransform, animationOpacity}) => (
  <div
    style={{
      ...textStyle(layer),
      transform: animationTransform,
      opacity: animationOpacity,
      whiteSpace: 'pre-wrap',
    }}
  >
    {layer.text}
  </div>
);

export const LowerThird: React.FC<TextPrimitiveProps> = ({layer, animationTransform, animationOpacity}) => (
  <div
    style={{
      display: 'inline-block',
      maxWidth: '82%',
      padding: '12px 22px',
      borderLeft: '8px solid ' + layer.color,
      backgroundColor: 'rgba(16, 16, 16, 0.88)',
      borderRadius: 8,
      transform: animationTransform,
      opacity: animationOpacity,
      whiteSpace: 'pre-wrap',
      ...textStyle(layer),
    }}
  >
    {layer.text}
  </div>
);

export const Callout: React.FC<TextPrimitiveProps> = ({layer, animationTransform, animationOpacity}) => (
  <div
    style={{
      display: 'inline-block',
      maxWidth: '78%',
      padding: '18px 26px',
      backgroundColor: 'rgba(16, 16, 16, 0.92)',
      border: '2px solid ' + layer.color,
      borderRadius: 18,
      boxShadow: '0 8px 24px rgba(0, 0, 0, 0.28)',
      transform: animationTransform,
      opacity: animationOpacity,
      whiteSpace: 'pre-wrap',
      ...textStyle(layer),
    }}
  >
    {layer.text}
  </div>
);

export const Diagram: React.FC<TextPrimitiveProps> = ({layer, animationOpacity, progress}) => (
  <div
    style={{
      width: '72%',
      padding: 18,
      opacity: animationOpacity,
      color: layer.color,
      fontFamily: layer.font_family,
      fontSize: Math.max(16, layer.font_size * 0.5),
      fontWeight: layer.font_weight,
    }}
  >
    <div style={{display: 'flex', alignItems: 'center', gap: 10}}>
      <div style={{width: 18, height: 18, borderRadius: '50%', backgroundColor: layer.color}} />
      <div style={{flex: 1, height: 6, backgroundColor: 'rgba(255,255,255,0.25)'}}>
        <div
          style={{
            width: String(progress * 100) + '%',
            height: '100%',
            backgroundColor: layer.color,
          }}
        />
      </div>
      <div style={{width: 18, height: 18, borderRadius: '50%', backgroundColor: layer.color}} />
    </div>
    <div style={{marginTop: 10, textAlign: 'center'}}>{layer.text}</div>
  </div>
);

export const Transition: React.FC<TextPrimitiveProps> = ({layer, animationOpacity, progress}) => (
  <div
    style={{
      position: 'absolute',
      inset: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      backgroundColor: layer.color,
      clipPath: 'inset(0 ' + String((1 - progress) * 100) + '% 0 0)',
      opacity: animationOpacity,
      ...textStyle(layer),
    }}
  >
    {layer.text}
  </div>
);

export const TextPrimitive: React.FC<TextPrimitiveProps> = (props) => {
  switch (props.layer.template ?? 'plain') {
    case 'lower_third':
      return <LowerThird {...props} />;
    case 'callout':
      return <Callout {...props} />;
    case 'diagram':
      return <Diagram {...props} />;
    case 'transition':
      return <Transition {...props} />;
    case 'title':
    case 'plain':
    default:
      return <Typography {...props} />;
  }
};
