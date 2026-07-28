# ADR 0009: Prefer Green-Screen Chroma Key

Status: accepted

## Decision

When recording can be controlled, use a green screen and FFmpeg chroma key before neural person matting.

## Reason

Chroma key is faster, cheaper, deterministic, easier to test, and requires no checkpoint or GPU.

## Consequence

The final workflow uses chroma key or an approved supplied/manual mask. MatAnyone
2 remains available only as a deferred optional extension under ADR-0013 for a
future accepted re-enable decision. The local milestone and final path work
without it.
