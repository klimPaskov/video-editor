# ADR 0013: Defer optional vision workers

## Status

Accepted and superseded by ADR 0014 for the public release.

## Decision

The public workflow is worker-free. The installer, doctor, CLI, tests, and
release documentation must not install, invoke, or require optional vision
workers. Their contracts may remain in the repository as isolated extension
material, but they are not a production dependency.

## Consequences

- The supported path is local, deterministic, and credential-free.
- No checkpoint, device, or isolated runtime approval is needed to edit a screen
  recording.
- Any future re-enable requires a new accepted decision and fresh live evidence.
