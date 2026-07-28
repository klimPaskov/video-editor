# Remotion and Layering Guide

Purposeful zoom rules and the focus and pacing artifact are defined in `docs/29_purposeful_focus_and_prompt_speedups.md`. Generic transform keyframes must not be used to create decorative or untargeted screen zooms.

The `visual_timeline` contract is the bridge between Codex planning and React rendering. Keep scenes declarative. Do not generate a new composition component for each project when data can express the change.

## Standard passes

### Base presentation

A single Remotion pass can render a normal edited video, captions, B-roll, front graphics, and picture-in-picture.

### Subject separation

For text behind a person, use a plate pass, an FFmpeg foreground composite, and a final front pass. This avoids depending on browser alpha codec support for every stage.

### Object replacement

Use the tracked geometry to position an image or video layer. Use masks for visibility and occlusion. Render short previews around hand contact and object crossings.

## Determinism

Use current frame, rational frame rate, local assets, fixed fonts, seeded randomness, and exact props. Persist the props used for every render.

## Structural transition layer

Remotion consumes approved transition-plan entries in output time. Transition primitives must be frame-driven, cover the complete frame, expose deterministic easing, preserve readable incoming content, and render without network access. Routine micro cuts bypass this layer.

The Python compiler converts each selected transition range with the same rational frame
conversion used by the rest of the visual timeline. It requires an explicit approved
transition ID, binds the props to the transition-plan file hash, and drops hard-cut,
blocked, incomplete, or sound-unsynchronized proposals. The Remotion primitive uses an
oversized full-frame cover, frame-derived easing, and a bounded `Sequence`; the sequence
ends at `incoming_first_readable_frame`, so the first readable incoming frame is never
covered by the motion layer.
