# Prompt: review and deliver a candidate

Review the current candidate for `<project-id>`:

- Candidate: `<absolute path to candidate MP4>`
- Review packet: `<absolute path to QA review packet>`
- Reviewer: `<name>`
- Review role: `<role>`

Show the candidate and its diagnostics. Check every flagged join and segment
for clipped words, clicks, room-tone jumps, duplicate or missing words, cadence,
black flashes, frozen frames, face or screen-state jumps, caption mistakes,
targetless zooms, incorrect prompt-speed boundaries, and audio/video sync.

Record a decision for each item as `repair`, `false_positive`,
`accepted_risk`, or `reject_candidate`, with a short reason. Do not treat a
successful FFmpeg or Remotion exit code as visual proof.

Only after all required findings are repaired or explicitly accepted by the
review policy, and after I explicitly approve Gate 3, should you:

1. promote the approved candidate to the configured `outputs` directory;
2. write delivery metadata, stream details, and checksums;
3. run strict FFmpeg decode and stream validation;
4. verify a backup copy by hash.

Keep source deletion and generated-output cleanup as separate approvals. Do not
delete or overwrite the source or a valid prior revision.
