# Product Requirements

## Product statement

Create a reliable local-first editing assistant for screen recordings. It turns
source media and a creative brief into an approved edit, branded visual
composition, reviewable previews, and a reproducible delivery.

## Functional requirements

1. Create a stable project with configuration, revision, state, and safe local
   directories.
2. Ingest without modifying the source; record its hash, metadata, streams, and
   tool versions.
3. Produce word-timed local transcripts and independent silence evidence.
4. Propose precise cuts, filler removal, dead-air removal, captions, sound,
   motion, B-roll, picture-in-picture, and screen-focus actions.
5. Require explicit Gate 1 approval before semantic edits or effects are applied.
6. Compile approved half-open microsecond ranges into a canonical edit decision
   list and source-to-output map.
7. Render picture and dialogue from the same edit map and preserve A/V sync.
8. Compile explicitly requested prompt-writing speed-ups before rebasing
   transcripts, captions, zooms, and effects. Keep audio audible and pitch
   unchanged.
9. Create a schema-valid integer-frame Remotion timeline with z-order,
   transforms, captions, audio, local assets, and purposeful zoom targets.
10. Keep zooms target-bound, centered, eased, stable, and absent when no clear
    target is visible.
11. Render bounded segment previews, re-transcribe them, and record repairable QA
    findings.
12. Apply review markers as new immutable revisions; never edit an approved
    artifact in place.
13. Require current Gate 2 and Gate 3 approvals bound to exact hashes.
14. Validate media streams, timing, audio, captions, safe areas, provenance,
    approvals, backups, and cleanup boundaries before delivery.
15. Produce an MP4 delivery manifest with checksums, captions, transcript, and
    publishing metadata.

## Nonfunctional requirements

- Python 3.11 core, Node.js 22 Remotion, FFmpeg/ffprobe, and local Whisper.
- Typed process execution with argument arrays, bounded output, timeouts,
  redaction, stable errors, and version records.
- Integer microseconds for media time and integer Remotion frames with tested
  rational frame-rate conversion.
- Idempotent, resumable stages that write to staging and promote atomically.
- Private source media and project evidence stay outside Git.
- Paid providers are disabled by default and require a current bounded budget.
