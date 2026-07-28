# Prompt: final candidate QA and watch-through

Review `<absolute local candidate MP4>` for `<project-id>` revision
`<revision-id>`.

Use the current review packet, final QA, join previews, visual evidence, source
mapping, caption sidecars, and stream diagnostics. Inspect the actual media,
not just JSON or process exit codes. Check:

- source immutability, revision and approval hashes;
- duration, frame count, rational frame rate, dimensions, pixel format, color;
- audio sample rate, channel layout, codec, sync, clipping, clicks, dropouts,
  and room-tone continuity;
- every join for missing/duplicate words, clipped syllables, grammar, meaning,
  cadence, black flashes, freezes, face jumps, and impossible UI states;
- caption timing, glyphs, contrast, line wrapping, and safe areas;
- mask/alpha edges, subject identity, object recolor, z-order, and occlusion;
- every zoom target, target centering, easing, stability, and exact boundaries;
- every speed-up boundary, visible prompt action, audible sound, and original
  voice pitch.

Record each finding as `repair`, `false_positive`, `accepted_risk`, or
`reject_candidate`, with a reason and retained evidence path. Record a human
watch-through only after the candidate has been inspected. Do not approve Gate
3, delivery, backup, or cleanup on my behalf.
