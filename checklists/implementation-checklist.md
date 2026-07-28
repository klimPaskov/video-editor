# Implementation Checklist

## Before coding

- [ ] Read `AGENTS.md` and the active phase prompt.
- [ ] Identify exact task identifiers.
- [ ] Inspect current contracts and accepted ADRs.
- [ ] Confirm no credential or paid call is needed.
- [ ] Identify the smallest vertical slice.

## During coding

- [ ] Keep source media immutable.
- [ ] Use integer microseconds for canonical time.
- [ ] Use decimal money.
- [ ] Run external tools through the process adapter.
- [ ] Validate artifacts before promotion.
- [ ] Add tests for success and failure.
- [ ] Keep provider details inside adapters.
- [ ] Record hashes and versions.

## Before completion

- [ ] Run format, lint, type, tests, and example validation.
- [ ] Review the diff for secrets and binaries.
- [ ] Review path and cleanup behavior.
- [ ] Review retry and cancellation behavior.
- [ ] Review approval and budget enforcement.
- [ ] Update docs and schemas.
- [ ] Write a schema-valid phase result.
