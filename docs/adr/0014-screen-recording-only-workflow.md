# ADR 0014: Screen-recording-only public workflow

## Status

Accepted.

## Decision

VideoEdit ships one public input workflow: screen recordings. The production
CLI accepts ingest, transcript, edit, retiming, captions, screen focus,
composition, review, QA, delivery, backup, and cleanup operations for that
workflow. Retired capture-specific effects and optional model workers are not
part of the installer, doctor checks, examples, or public guidance.

## Consequences

The setup is smaller and deterministic. The source screen layer remains
immutable, visual effects are explicit and reviewable, and optional experiments
cannot silently enter a production render.
