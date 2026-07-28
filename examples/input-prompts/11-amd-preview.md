# Prompt: AMD preview and production profile

I have an AMD GPU at `<GPU model>` and want local preview acceleration for
`<project-id>`.

Run `videoedit doctor --json` first. If FFmpeg reports a passing `h264_amf`
capability, use it only for a disposable proxy, still, or review preview and
record the encoder, driver, resolution, frame rate, and settings in the stage
diagnostics. If AMF is unavailable, use the software preview path.

Keep the production delivery profile deterministic and lossless: software
`libx264` with QP 0 when the project profile permits it, plus PCM `f32le`
audio at the configured sample rate and channel layout. Do not claim AMF is
lossless, silently substitute a lossy codec, or use AMF as evidence for CUDA.
Do not start SAM 3.1 or MatAnyone 2. Keep the source immutable and show the
preview evidence before any approval request.
