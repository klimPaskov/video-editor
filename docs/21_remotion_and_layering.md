# Remotion and layering guide

Purposeful zoom rules and the focus-and-pacing artifact are defined in
`docs/29_purposeful_focus_and_prompt_speedups.md`. Generic transforms must not
create decorative or untargeted UI movement.

The `visual_timeline` contract bridges Codex planning and React rendering. Keep
scenes declarative and express project changes through validated data props.

## Standard pass

A Remotion pass can render the edited recording, captions, text, B-roll, front
graphics, and picture-in-picture. Keep the base recording stable underneath
these additions.

## Determinism

Use the current frame, rational frame rates, local hash-verified assets, fixed
fonts, seeded randomness where needed, and exact serializable props. Persist the
props used for every render.

## Structural transitions

Remotion consumes approved transition-plan entries in output time. Primitives
must be frame-driven, cover the complete frame, expose deterministic easing,
preserve readable incoming content, and render without network access. Routine
micro cuts use a clean-cut fallback.
