# Prompt: deliver a reviewed master and prepare safe cleanup

For `hermes-agent-demo-20260728`, revision `rev_004`, show me before delivery:

- the complete MP4 candidate and full-watch-through record;
- final QA, join QA, segment locks, Gate 1/Gate 2/Gate 3 bindings;
- source, asset, composition bundle, delivery profile, and candidate hashes;
- caption sidecars, transcript, chapters, description draft, and warnings.

After I explicitly approve the current Gate 3 candidate, promote the approved
MP4 to the configured `outputs` directory, preserving the requested profile:
MP4, H.264 `libx264` QP 0, original dimensions/FPS, `yuv420p` BT.709, and
PCM `f32le` 48 kHz stereo. Run strict FFmpeg decode and stream validation,
write checksums and delivery metadata, and verify the configured backup by
SHA-256.

Then create a cleanup **dry run only**. Never include the immutable source,
valid revisions, active approvals, phase results, failed QA evidence, or the
new delivery. List each derived cache/temp/superseded item with its hash,
retention class, and recoverability. Wait for a separate cleanup approval
before deleting anything. Stop if any hash, backup, QA, or approval is stale.
