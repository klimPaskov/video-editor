# ADR 0010: Purposeful Focus and Action-Bound Retiming

## Status

Accepted

## Context

Generic transform keyframes can create zooms, but they do not prove that a zoom has a useful target or correct boundaries. Generic playback-rate changes can shorten footage, but they can also speed up unrelated browsing, reading, or waiting and can damage audio.

The workflow needs explicit contracts that separate visual focus from source retiming and preserve confidence, review, mapping, and QA evidence.

## Decision

Create a `focus_pacing_plan` artifact during P3.

Purposeful zoom actions are consumed by Remotion after source-to-output mapping. They require an allowed purpose, visible target evidence, target-centered framing, smooth easing, stabilized transforms, exact relevance boundaries, confidence, and the safe fallback `no_zoom`.

Prompt speed-up actions are permitted only after an explicit operator request. They cover only visible prompt-writing or prompt-dictation actions. They preserve audible, pitch-adjusted production audio by default and use the safe fallback `normal_speed`.

Compile approved speed-ups into a `retimed_timeline` artifact before the P4 base render. This artifact becomes the authoritative source-to-output mapping for transcript rebasing and all later effects.

Gate 1 approval binds to the focus and pacing plan. Gate 2 checks rendered motion, target framing, exact boundaries, audio presence, synchronization, and transcript continuity.

## Consequences

- Existing edit and effect plans remain valid.
- Projects without speed-ups still receive a retimed timeline with rate 1 segments.
- Zooms cannot be added as untracked decoration.
- Speed-ups cannot be inferred from a desire for faster pacing.
- Every later timestamp must map through the retimed timeline.
- The implementation needs target evidence extraction, target-centered transform generation, smooth keyframe easing, FFmpeg retiming, audio tempo handling, and dedicated QA.
