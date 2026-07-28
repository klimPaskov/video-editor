# ADR 0001: Local-First Deterministic Core

- Status: Accepted
- Date: 2026-07-22

## Context

The workflow combines local media processing, model planning, motion rendering, and paid generated B-roll. A direct agent script could couple every step to current tools and provider behavior.

## Decision

The production core will run locally and use deterministic tools for media execution. Remote models and providers are optional adapters. The pipeline will remain usable for ingest, transcript, silence analysis, approved cuts, audio, captions, preview, QA, and final delivery without a paid provider.

## Consequences

Positive:

- lower default cost
- stronger privacy
- easier tests
- fewer runtime dependencies
- clear fallbacks

Costs:

- local dependency installation
- hardware differences
- more adapter and capability testing
- local disk requirements
