# Prompt: review a real candidate before publishing

Review this candidate for `hermes-agent-demo-20260728`, revision `rev_004`:

```text
Candidate: C:\Users\me\Projects\video-editor\projects\hermes-agent-demo-20260728\revisions\rev_004\outputs\candidate.mp4
Review packet: C:\Users\me\Projects\video-editor\projects\hermes-agent-demo-20260728\review\qa-review-packet.json
Reviewer: Alex
Role: channel editor
```

Inspect the actual MP4 and the evidence, not just JSON or process exit codes.
Check every flagged join and a representative sample of passing joins for:

- missing or duplicated words, clipped syllables, bad grammar, and rushed
  cadence;
- clicks, dropouts, clipping, room-tone jumps, A/V drift, and audible changes
  in the speed-up section;
- black flashes, frozen frames, face jumps, impossible cursor or screen states;
- caption spelling, timing, line wrapping, contrast, and safe-area violations;
- target-centered zoom timing, easing, stability, edge coverage, and unrelated
  content entering the frame;
- exact prompt-writing boundaries, audible sound, and unchanged voice pitch.

Record each finding as `repair`, `false_positive`, `accepted_risk`, or
`reject_candidate`, with a timestamp, reason, and retained evidence path.
Repair failures in a new revision. Do not approve Gate 3, delivery, backup, or
cleanup until the current human approvals and full watch-through are complete.
