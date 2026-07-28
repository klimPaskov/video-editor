# ADR 0006: Caption Plan, Remotion Rendering, and Sidecars

- Status: Accepted
- Date: 2026-07-23

## Context

The workflow needs branded word-timed captions, selective emphasis, deterministic timing, and accessible sidecars. Caption data should remain reusable even when the visual renderer changes.

## Decision

Use a renderer-neutral caption plan as the source of truth. Render branded burned-in captions through Remotion in the primary visual pipeline. Generate WebVTT and plain-text sidecars for delivery. Keep ASS and libass as a deterministic fallback for environments or projects that do not require the Remotion visual pass.

Any renderer must preserve caption identifiers, timing, grouping, emphasis, safe-area policy, font identity, and collision decisions from the caption plan.

## Consequences

Positive:

- one stable caption contract
- frame-driven branded rendering in Remotion
- accessible sidecars
- a local FFmpeg fallback
- snapshot and timing tests independent of the renderer

Costs:

- font deployment and licence tracking
- parity tests between the primary and fallback renderers
- platform capability checks for libass when the fallback is used
