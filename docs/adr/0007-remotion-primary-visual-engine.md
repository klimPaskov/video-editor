# ADR 0007: Remotion Is the Primary Visual Engine

Status: accepted

## Decision

Use Remotion as the repository's primary declarative visual timeline, motion, typography, caption, B-roll, and picture-in-picture engine.

## Reason

React components are readable and editable by Codex, props can be schema-bound, frame timing is deterministic, and the engine fits layered programmatic composition.

## Consequence

HyperFrames and other renderers may exist only behind optional adapters. They do not define core contracts.
