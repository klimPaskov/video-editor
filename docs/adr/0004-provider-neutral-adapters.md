# ADR 0004: Provider-Neutral Adapters

- Status: Accepted
- Date: 2026-07-22

## Context

The source workflow names Whisper, HyperFrames, and Higgsfield. Their interfaces, models, prices, and requirements can change.

## Decision

Define provider-neutral protocols for transcription, semantic planning, motion rendering, sound catalogs, and generated video. Keep provider payloads and commands inside adapters. Require a local fallback for motion and a no-provider path for B-roll.

## Consequences

Positive:

- replaceable providers
- stable core contracts
- fake adapters for tests
- historical projects remain readable after provider removal

Costs:

- capability translation
- generic contracts can expose only shared concepts
- each adapter needs contract tests and current documentation checks
