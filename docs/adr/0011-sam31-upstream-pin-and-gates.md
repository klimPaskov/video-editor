# ADR 0011: Keep the optional vision contract isolated

## Status

Superseded for the public workflow by ADR 0013 and ADR 0014.

## Decision

The public product does not install, invoke, or depend on the optional vision
worker contract. Its JSON schemas and isolated worker scaffolding remain only as
historical extension material and are not included in setup, doctor, or the
production CLI.

## Consequences

The supported workflow remains credential-free and uses the local screen,
transcript, FFmpeg, local Whisper, and Remotion path. A future experiment must
be separately approved, versioned, and kept outside the production workflow.
