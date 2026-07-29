# External adapters and providers

The supported workflow is local. FFmpeg, ffprobe, local Whisper, and Remotion
are the required media components.

## Local Whisper

Whisper runs locally and is supplied through the optional `whisper` dependency
group. Persist the model identifier, local model hash, device, adapter version,
confidence evidence, and raw transcript output. The adapter does not download a
model implicitly; provide `VIDEOEDIT_WHISPER_MODEL_PATH` or use the explicit
hash-pinned fetch helper. Word timestamps are evidence, not automatic edit
approval.

## Remotion

Remotion is the local compositor. The Python adapter stages hash-verified
assets, writes schema-valid props, and invokes the Node.js CLI. Keep the
composition code bundle and package lock hash-bound to visual approvals.

## Optional providers

Generated media and paid providers remain outside the required path. Any future
provider adapter must keep the network disabled by default, require a bounded
spend approval, use an idempotency key, persist lifecycle state, validate and
hash downloads, and record retention and privacy decisions.

The current provider boundary has no provider SDK and no default submitter.
Provider submission is unavailable until a provider-specific adapter and fresh
operator approvals exist.
