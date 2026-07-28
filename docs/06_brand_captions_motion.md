# Brand, Captions, and Motion

## Brand configuration

Brand configuration should declare fonts, colour tokens, logo assets, caption layout, motion intensity, safe areas, and examples. Assets stay private unless redistribution rights are clear.

## Captions

Use word timing as evidence and phrase timing as presentation. Caption groups should remain readable, fit the safe area, and avoid covering faces or screen content. Emphasis must be deterministic and limited to declared brand colours.

The baseline Remotion caption component supports:

- start and end frames
- phrase text
- emphasis tokens
- font family, size, weight, and safe-area placement through brand configuration
- deterministic fade or entrance animation

ASS and WebVTT sidecars should also be emitted for portability and accessibility.

The local caption planner groups canonical transcript words deterministically and
persists phrase timing, word IDs, line breaks, emphasis spans, brand identity,
safe-area fractions, and hashed ASS/WebVTT/text outputs. If no licensed caption font
file is configured, the plan records an explicit warning and the compositor uses its
declared local fallback family; it never fetches a font at render time. Cue end times
are clamped before the next cue begins, so one active cue is the collision policy.

## Motion graphics

Remotion is the primary motion engine. Reusable components should accept typed data rather than embedded project logic. Suitable components include:

- title cards
- quote cards
- step lists
- comparisons
- diagrams
- progress and timeline views
- metric callouts
- screen focus frames
- lower thirds

The schema-backed text `template` field selects reusable Remotion primitives for
plain typography, titles, lower thirds, callouts, diagrams, and transitions. Every
primitive is driven by `useCurrentFrame`, `interpolate`, or `spring`; CSS transition
timing is not used in rendered output.

All motion uses frame-driven values. Avoid CSS transition timing, random values without a seed, and network-fetched runtime assets.

## Text behind a subject

Use three render layers:

1. background
2. middle text or graphic plate
3. subject foreground

Add captions and front labels only after the subject composite. This keeps the middle text visually behind the person while captions remain readable.

## Object effects

SAM masks are media evidence, not style instructions. A separate effect plan defines hue changes, replacement assets, smoothing, scale, anchor, and fallback. Geometry should be smoothed with explicit limits and never extrapolated far beyond visible masks.

## Sound

Sound effects come from a licensed local catalogue. Each cue declares source, start, gain, fade, ducking behavior, and reason. Speech has priority. Avoid adding a sound at every visual change.

## Review

Every new motion component must have a short fixture, a preview image or clip, and tests for duration, bounds, missing assets, and invalid props. Visual approval still requires human inspection.
