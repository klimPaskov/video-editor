# External Workers and Optional Providers

## Local worker contract

A worker command receives one absolute JSON job path. It writes durable result artifacts and prints the result JSON to standard output. Exit code 0 means the contract completed, not that visual quality is approved.

The core records:

- command and arguments
- start and finish time
- exit code
- standard output and error
- input and output hashes
- software and model identity
- retry state

## SAM 3.1

Use `workers/sam3/` and the segmentation job and result schemas. Installation, checkpoint access, authentication, licence review, and GPU compatibility are manual gates. The adapter normalizes masks and geometry, while review validates identity and continuity.

## MatAnyone 2

Use `workers/matanyone2/` and the matting job and result schemas. The worker consumes a source and first-frame person mask. Output file names are discovered and recorded because upstream naming may change. Production use requires verifying foreground and alpha semantics.

## Local Whisper

Whisper remains in a core optional dependency group because it can run locally. Persist the model identifier, supplied model SHA-256, device, adapter version, confidence evidence, and raw transcript output. Rendered-segment re-transcription carries the adapter's supplied model SHA-256 into the rendered transcript artifact and includes a local model-file hash in its stage identity when available. The adapter does not download models implicitly; provide an operator-supplied local checkpoint path through `VIDEOEDIT_WHISPER_MODEL_PATH`, or use the explicit hash-pinned `scripts/fetch-whisper-model.ps1`/`.sh` helper. `videoedit doctor --json` reports the package and model-file readiness separately. Do not treat word timestamps as exact edit boundaries without handles and review.

## Remotion

Remotion is a local compositor, not a model provider. The Python adapter stages assets, writes schema-valid props, and invokes the CLI. Review its current commercial licence terms for the intended team and product use.

## Optional generated B-roll

Generated video providers remain outside the required workflow. Any provider adapter must support:

- network disabled by default
- explicit bounded spend approval
- idempotency key
- submitted, running, complete, failed, and cancelled states
- polling without duplicate submission
- prompt and model record
- download and checksum
- retention and privacy record

Higgsfield may be implemented through this interface after an implementation-time compatibility and pricing check. Do not hard-code current credit prices.

The core `provider_job` boundary is implemented without a provider SDK. `plan-provider-job`
records a request-bound, disabled job and deterministic idempotency key. Submission
requires separate current effect and spend approvals, a bounded decimal maximum, and
explicit network opt-in. The default path has no submitter and never makes a network
call. A persisted `submitting` state is treated as unresolved and cannot be retried
automatically, preventing duplicate paid requests after an interruption.

## Optional inpainting

Inpainting uses the separate `inpainting_request` contract and command adapter.
The request is produced only from a current passing local mask validation and
always records the original shot as the fallback. Submission requires both a
request-bound effect approval and a request-bound spend approval. The network
and adapter are disabled by default; the core does not import a provider SDK or
consume an output until a provider-specific result and validation contract are
added.

## Optional HyperFrames

HyperFrames can remain a future renderer adapter for projects that already use it. It does not replace the primary Remotion timeline and must not change core schemas.
