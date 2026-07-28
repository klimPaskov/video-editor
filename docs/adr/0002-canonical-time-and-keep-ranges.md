# ADR 0002: Integer Microseconds and Keep Ranges

- Status: Accepted
- Date: 2026-07-22

## Context

Speech timestamps, video frames, audio samples, and provider durations use different time representations. Repeated floating-point conversion can create gaps, overlaps, and A/V drift.

## Decision

Use integer microseconds as canonical time. Store edit decisions as ordered half-open source ranges to keep. Convert to output frames and audio samples only in render compilers.

## Consequences

Positive:

- exact domain arithmetic
- provider and frame-rate independence
- simpler invariants
- one mapping for media, words, and cues

Costs:

- render-boundary rounding logic
- explicit rational frame-rate handling
- migration required if a future contract changes units
