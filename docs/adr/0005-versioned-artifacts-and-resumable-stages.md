# ADR 0005: Versioned Artifacts and Resumable Stages

- Status: Accepted
- Date: 2026-07-22

## Context

Video jobs are long and can fail after expensive work. Unstructured files make it hard to know whether an output is current or safe to reuse.

## Decision

Every stage reads declared versioned artifacts and writes validated versioned artifacts. A stage key includes relevant input hashes, configuration hash, schema version, and implementation version. Complete artifacts are promoted atomically and indexed in SQLite.

## Consequences

Positive:

- idempotent retries
- selective invalidation
- crash recovery
- reproducibility
- clear provenance

Costs:

- artifact bookkeeping
- schema migration policy
- additional storage
- careful transaction design
