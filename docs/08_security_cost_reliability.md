# Security, Cost, and Reliability

## Threat model

Relevant risks include:

- untrusted source media
- malformed codecs and containers
- command injection
- path traversal
- malicious or broken HTML motion compositions
- secret leakage
- unauthorized paid generation
- accidental source deletion
- unbounded disk use
- unbounded process or network retries
- poisoned provider downloads
- personal or confidential speech in transcripts
- misleading generated visuals

## Trust boundaries

- Operator input is untrusted until validated.
- Source media is untrusted binary input.
- Model output is untrusted structured input.
- Provider responses and downloads are untrusted.
- Local configuration is trusted only after schema validation.
- Approved artifacts are trusted only when their hashes match.

## Filesystem policy

- Resolve all project paths before use.
- Reject paths that escape the project for derived output.
- Allow external source references only through the source manifest.
- Never follow unexpected symbolic links during cleanup.
- Write temporary files inside a project-controlled temporary directory.
- Atomically promote complete artifacts.
- Mark managed source copies read-only where the platform supports it.
- Cleanup acts only on known artifact records.

## Process security

- Never use `shell=True`.
- Pass arguments as a list.
- Use a minimal environment allowlist.
- Set explicit working directories.
- Set resource limits where supported.
- Set timeouts and terminate process groups on cancellation.
- Keep FFmpeg protocol allowlists narrow when possible.
- Disable network protocols for local media processing when practical.
- Treat HTML motion rendering as code execution and sandbox it.

## Codex implementation permissions

Recommended implementation posture:

- repository-scoped workspace write access
- approval on request for broader commands
- no provider credentials during normal implementation
- no production source media in the coding repository
- MCP servers added only for tasks that need them
- noninteractive Codex runs constrained by a structured result schema

## Secrets

- Read secrets from environment variables or an operating-system secret store.
- Store only a secret reference in project configuration.
- Redact environment values, authorization headers, and tokens from logs.
- Do not include secrets in process argument lists when a safer input channel exists.
- Prevent `.env` files from entering version control.
- Rotate a secret after suspected exposure.

## Data classification

Projects should support a data classification field:

- public
- internal
- confidential
- restricted

Provider use can be disabled by classification. A restricted project should default to local-only processing and short retention.

## Transcript privacy

- Store transcripts under the project retention policy.
- Keep remote semantic planning optional.
- Support redaction before remote planning where useful.
- Record which text was sent to each provider.
- Do not send full media when a bounded transcript window is sufficient.

## Generated visual safety

B-roll review should flag:

- false implication of documentary evidence
- identifiable people not present in the source
- logos and trademarks
- unsafe or disallowed content
- conflicting text inside generated frames
- mismatch with narration
- visual artifacts that harm credibility

Sensitive factual claims should use diagrams, licensed evidence, or no B-roll.

## Budget model

Configuration should include:

```yaml
budget:
  currency: USD
  project_limit: 20.00
  daily_limit: 40.00
  request_limit: 5.00
  reserve_percent: 20
  approval_expiry_hours: 24
```

Budget checks use decimal arithmetic. Do not use binary floating point for money.

## Cost lifecycle

1. Provider adapter returns an estimate and its assumptions.
2. System adds the configured reserve.
3. Reviewer approves a maximum amount bound to the request hash.
4. System re-estimates immediately before submission when possible.
5. Submission is blocked when the estimate exceeds remaining approval.
6. Actual cost is recorded when available.
7. Reconciliation flags missing or higher-than-approved cost.

An unavailable estimate should require an explicit maximum charge assumption. It should not silently become zero.

## Retry policy

### Local processes

- Retry only known transient failures.
- Do not retry invalid arguments or deterministic decode failures.
- Clean partial outputs before retry.

The local Whisper adapter is a bounded exception to the second rule: after the
audio proxy and model path have already passed existence checks, one
Windows-specific `OSError` retry is permitted for the known sharing/allocator
errno set, including `EINVAL`. A repeated error remains a stable
`transcription_output_invalid` failure. Atomic JSON and text promotion uses the
same bounded retry only for `PermissionError`; it never replaces a persistent
failure or an invalid staged file.

### Remote providers

- Use exponential backoff with jitter.
- Respect provider retry hints.
- Bound attempts and total elapsed time.
- Do not resubmit a paid request merely because polling failed.
- Resume polling from the persisted job identifier.
- Use idempotency keys where supported.

## Disk management

Before a stage:

- estimate temporary and final disk needs
- apply a safety factor
- compare against available space and quota
- stop before rendering when insufficient

Retention classes:

- source
- required reproducibility asset
- active revision artifact
- preview
- cache
- temporary

Cleanup priority starts with temporary files and inactive caches. Source is never eligible.

## Crash consistency

- Write stage status `running` before external work.
- Keep a heartbeat for long stages.
- Use temporary output names.
- Validate before atomic promotion.
- Commit artifact and state transition in one database transaction where possible.
- Detect stale locks by owner and heartbeat, not elapsed time alone.

## Concurrency

- One mutating stage per project revision by default.
- Bounded global workers.
- Separate limits for CPU, GPU, render, and provider jobs.
- Prevent two stages from writing the same artifact key.
- Serialize budget updates.

## Observability

Structured event fields:

- timestamp
- level
- event code
- project identifier
- revision identifier
- stage identifier
- run identifier
- artifact identifier
- elapsed milliseconds
- process exit code
- retry count
- cost estimate and actual cost
- redaction marker

Metrics should be local by default. Remote telemetry requires explicit policy.

## Backup and recovery

A portable project export should include:

- project and configuration snapshots
- source manifest
- approvals
- canonical JSON artifacts
- required local and provider assets
- final and preview manifests
- QA reports
- checksums

Source media may be excluded in reference-only mode. The export should clearly state that it is not self-contained.

## Reliability targets

Initial targets:

- no source mutation in automated tests
- one hundred percent budget enforcement before provider submission
- one hundred percent schema validation for complete artifacts
- full decode of every delivered output
- stage resume after injected process interruption
- no duplicate paid submission in retry tests
- deterministic timeline compilation for identical inputs

## Incident classes

- source integrity incident
- secret exposure
- unauthorized spend
- incorrect deletion
- provider duplication
- corrupt delivery
- license or provenance failure

Each incident should produce a retained report, affected project list, root cause, containment action, and regression test.
