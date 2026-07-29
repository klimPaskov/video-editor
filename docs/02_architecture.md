# Architecture

VideoEdit is a local-first screen-recording editor. Python coordinates the
workflow, FFmpeg performs deterministic media work, local Whisper supplies
word-timed speech evidence, and Remotion renders the visual timeline.

## Runtime boundaries

| Component | Runtime | Responsibility |
| --- | --- | --- |
| Core | Python 3.11 | ingest, hashes, plans, approvals, timing, QA, delivery |
| Media engine | FFmpeg and ffprobe | probe, cut, retime, mux, decode checks |
| Transcript adapter | local Whisper | transcript words, timestamps, model identity |
| Compositor | Node.js 22 and Remotion | captions, text, graphics, B-roll, PiP, purposeful UI focus |

The core invokes external programs through the typed process adapter. Arguments
are passed as arrays, timeouts and output limits are explicit, output is
captured and redacted, and tool versions are recorded.

## Data flow

```text
source file
  -> immutable ingest and hash
  -> ffprobe and local Whisper evidence
  -> edit, caption, focus, and effect proposals
  -> Gate 1 approval
  -> approved edit and retimed timeline
  -> FFmpeg base render and audio pass
  -> Remotion visual timeline
  -> segment previews and re-transcription
  -> Gate 2 review
  -> final QA and Gate 3 approval
  -> MP4 delivery, backup verification, cleanup plan
```

## Timeline contract

- Source and output media time is integer microseconds with half-open ranges.
- Remotion uses integer frames.
- Frame-rate values stay rational; conversions use explicit, tested rounding.
- Approved speed-ups are compiled before captions, zooms, effects, and review
  timestamps are rebased.
- Every timeline layer carries timing, z-order, transforms, content, and asset
  identity.

## Visual layers

The base screen recording is the stable visual layer. Remotion can add a
background plate, text and motion graphics, captions, B-roll, picture-in-
picture windows, and purposeful target-centered UI zooms. Zooms are created
only from an approved visible target and verified action boundaries; otherwise
the clean view is retained.

## Safety and approvals

Sources are copied into an immutable project area and never modified. Stages
write to staging, validate, and promote atomically. Cache keys include input
hashes, configuration, implementation version, and model identity where
relevant.

Codex can propose edits and effects, but Gate 1 approves the plan, Gate 2
approves previews, and Gate 3 approves delivery. Approvals bind to exact hashes
and become stale when a bound input changes. Paid providers and nonlocal
integrations remain disabled without a separate bounded approval.
