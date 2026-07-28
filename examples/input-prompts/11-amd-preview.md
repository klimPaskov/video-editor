# Prompt: use my AMD Radeon RX 7700 XT for previews

I have an AMD Radeon RX 7700 XT on Windows and want a disposable review
preview for `hermes-agent-demo-20260728`.

Run `uv run videoedit doctor --json` first. If the configured FFmpeg exposes a
passing `h264_amf` capability, render a 1280x720 preview of the segment around
the prompt-writing action, approximately `11:30-12:25`. Record the GPU/driver,
encoder, dimensions, FPS, bitrate or rate-control settings, command, output
hash, full-decode result, and inspected contact-sheet frames. If AMF is not
ready, use the software preview path and say so.

Do not use AMF for the production master. Keep the master on software
`libx264` QP 0 with PCM `f32le` audio when that profile is selected. Do not call
SAM 3.1 or MatAnyone 2, do not claim AMF is lossless, do not overwrite the
source, and do not treat a successful encode as visual approval.
