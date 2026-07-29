# Prompt: perform the final review on a real candidate

Review:

```text
Project: hermes-agent-demo-20260728
Revision: rev_004
Candidate: C:\Users\me\Projects\video-editor\projects\hermes-agent-demo-20260728\revisions\rev_004\outputs\candidate.mp4
Final QA: C:\Users\me\Projects\video-editor\projects\hermes-agent-demo-20260728\review\final-qa.json
```

Use the current review packet, every join preview, the output transcript, the
source-to-output map, caption sidecars, visual evidence, and stream diagnostics.
Watch the actual candidate from beginning to end. Sample the first, middle,
last, high-motion, caption-heavy, zoom, and speed-up sections, and inspect all
machine failures.

Check source/revision/approval hashes; frame count and rational FPS; dimensions,
pixel format, BT.709 metadata; audio codec, sample rate, stereo layout, sync,
clipping, clicks, dropouts, and room-tone continuity; every join for missing or
duplicated words; captions and safe areas; layer edges and z-order; black flashes
and freezes; cursor/screen-state continuity; target-centered zoom boundaries;
and audible pitch-preserved prompt speed-ups.

Record every finding as `repair`, `false_positive`, `accepted_risk`, or
`reject_candidate` with timecode, reason, and retained evidence. Record the
human watch-through only after inspection. Do not invent Gate 3, delivery,
backup, or cleanup approval from a passing command.
