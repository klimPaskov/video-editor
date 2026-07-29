# Brand, captions, and motion

## Brand configuration

Brand configuration declares fonts, colour tokens, logo assets, caption layout,
motion intensity, safe areas, and examples. Assets stay private unless their
redistribution rights are clear.

## Captions

Use word timing as evidence and phrase timing as presentation. Caption groups
must remain readable, fit the safe area, and avoid covering important screen
content. Emphasis is deterministic and limited to declared brand colours.

The Remotion caption component supports start and end frames, phrase text,
emphasis tokens, font settings, safe-area placement, and deterministic fades.
Emit ASS and WebVTT sidecars for portability and accessibility.

The caption planner persists phrase timing, word IDs, line breaks, emphasis,
brand identity, safe-area fractions, and hashed sidecar outputs. It never fetches
a font at render time.

## Motion graphics

Remotion is the primary motion engine. Reusable components accept typed data and
may include title cards, quote cards, step lists, comparisons, diagrams, metric
callouts, screen-focus frames, and lower thirds. Components are driven by the
current frame and deterministic interpolation; browser-time transitions and
network-fetched runtime assets are not used.

## Layer order

Use a stable base recording layer, then add background or middle text plates,
supporting graphics, B-roll or picture-in-picture, and front captions. Every
layer has explicit timing, z-order, bounds, and asset identity.

## Sound and review

Sound cues come from the local catalogue. Each cue declares source, start, gain,
fade, ducking behavior, and reason. Speech has priority. Every new motion
component gets a short fixture, proof frame or clip, and tests for duration,
bounds, missing assets, and invalid props.
