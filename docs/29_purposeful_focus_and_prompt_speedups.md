# Purposeful Focus and Prompt Speed-Ups

## Purpose

This document defines two controlled editorial actions:

- purposeful zooms that make a visible screen target easier to understand
- requested speed-ups that condense visible prompt-writing or prompt-dictation actions

Neither action is decorative. Both require source evidence, confidence checks, safe fallbacks, and rendered-output QA.

The authoritative artifact is `schemas/focus_pacing_plan.schema.json`. Approved speed-ups compile into `schemas/retimed_timeline.schema.json` before the base render. Approved zooms compile into the Remotion visual timeline after source-to-output mapping.

## Control model

The planner classifies each candidate as:

- `auto_eligible` when evidence is strong and risk is low
- `review_required` when a material uncertainty remains
- `skipped` when the safe fallback is sufficient
- `blocked` when the request or evidence conflicts with policy

Uncertain items are batched into one concise review. Each review item includes a recommendation, evidence frames, confidence components, and a safe fallback. A low-confidence zoom falls back to no zoom. A low-confidence speed-up falls back to normal speed. Safe fallbacks should be used without interrupting the operator unless the choice materially affects meaning, continuity, or a stated requirement.

## Purposeful zoom policy

A zoom is permitted only when it helps the viewer inspect one of these visible targets:

- an opened window
- a prompt box
- a relevant cursor action
- important user interface content

Do not create a zoom because the timeline feels visually quiet. Do not choose random times, coordinates, or targets.

Every zoom must record:

- a clear purpose
- a description of the actual visible target
- the range in which the target is visibly present
- a normalized target bounding box or short target track
- source timing for zoom in, hold, and zoom out
- target, boundary, and stability confidence
- evidence frames at the start and end
- the fallback `no_zoom`

### Timing rules

- Start only after the target is visibly present.
- Finish before unrelated content or a different action begins.
- Keep the zoom active only while the target remains relevant.
- Do not bridge a cut, window change, tab change, modal close, or unrelated cursor action.
- Do not begin early to anticipate a target that is not yet visible.

### Framing rules

- Center the actual visible target.
- Add enough margin to preserve context and avoid clipping labels, menus, or cursor state.
- Lock the viewport to a smoothed target path.
- Do not pan across the whole screen without a target-driven reason.
- Do not expose blank image edges.
- Do not hide captions or other approved overlays.

### Motion rules

- Use frame-driven smooth ease-in and ease-out motion.
- Use stable transform keyframes with explicit easing.
- Reject shaking, jitter, snapping, overshoot, sudden direction changes, and drifting.
- Use the smallest scale that makes the target readable.
- Avoid back-to-back zooms when one stable focus window would work.

### Confidence behavior

Recommended initial policy:

- overall confidence at or above 0.96 and all required checks passing may be `auto_eligible`
- overall confidence from 0.85 through 0.959 requires review when the zoom materially affects comprehension
- confidence below 0.85 is skipped with `no_zoom`
- missing target evidence is blocked or skipped

These thresholds are starting values. Measure operator acceptance and rejection by purpose before changing them.

## Prompt speed-up policy

Speed-ups are disabled unless the operator asks for them in the project brief, Gate 1 review, or a `[SPEED]` fix marker.

A speed-up may cover only a visible action of:

- writing a prompt
- dictating a prompt while the prompt action is visibly occurring

Never include unrelated browsing, reading, waiting, scrolling without prompt entry, navigation, result inspection, or another action.

### Boundary rules

- Start on the first frame of the visible prompt-writing or prompt-dictation action.
- End on the last frame of that action.
- Split a candidate when another activity interrupts prompt entry.
- Do not add handles that include unrelated activity.
- Record start and end evidence frames.
- Require high boundary confidence for automatic execution.

### Audio rules

- Retain audible sound by default.
- Use pitch-preserving tempo adjustment for production audio by default.
- Muting or replacing audio requires an explicit operator request bound to the action.
- Keep picture and production audio on the same retimed range.
- Re-transcribe the rendered section to check that speech remains present and synchronized.

### Retiming rules

Approved speed-ups compile into a retimed canonical timeline. Each segment records source range, output range, playback rate, audio mode, action identifier, and boundary confidence.

The renderer should trim exact source ranges, apply video presentation-time scaling, apply a compatible audio tempo chain, concatenate in timeline order, and validate expected duration. Transcript words, captions, zooms, effects, and review timestamps must map through the retimed timeline.

The current local implementation exposes these stages through the CLI and services:

- `plan-focus-pacing` validates candidates, classifies confidence, and persists the hash-bound plan.
- `compile-retimed-timeline` emits the authoritative piecewise source/output map.
- `render-retimed` stages FFmpeg output, preserves audible pitch by default, validates decode, duration, and A/V drift, and writes a render manifest.
- `compose-visual --focus-pacing-plan` attaches approved target-centered keyframes to the Remotion timeline; `--retimed-timeline` rebases source times before keyframe generation.
- `qa-focus-pacing` persists the named zoom and speed-up checks. Visual overlay clearance remains an explicit review finding.

The implementation does not treat a low-confidence candidate as an applied effect: zooms use `no_zoom`, speed-ups use `normal_speed`, and mixed prompt activity is rejected from the speed-up list with a warning.

### Production master profile

For a source-specific lossless master, pass an explicit `libx264` profile with
`--qp 0`, `--audio-codec pcm_f32le`, the source rational frame rate, and strict
decode enabled. The renderer binds the source frame rate as CFR and binds the
canonical retimed duration at the mux boundary. A long retimed graph is written
to a staging-side FFmpeg filter script so Windows command-line limits cannot
silently truncate a production render. The AMD AMF profile remains available
for previews and derivatives, but it is not used when the delivery contract
requires an explicit H.264 QP-0 guarantee. No loudness-normalization filter is
added to this profile.

The lossless candidate is still subject to visual inspection, re-transcription,
per-join QA, Gate 3 approval, backup verification, and cleanup approval. A
successful render or strict decode does not create those approvals.

### Resumable rendered-join review

`qa-joins` persists an atomic, schema-validated `join_qa_progress` checkpoint in
the stage staging directory after every completed join. A retry with the same
hash-bound stage key reuses only completed items whose preview path, byte size,
and SHA-256 still match; an interrupted or failed join is recomputed. The stage
directory itself is reused only when the previous run has the same stage key and
is `running` or `failed`, and its path is still directly below the project
staging directory. A completed report remains the only cacheable QA result.

Checkpoint promotion tolerates a bounded Windows sharing `PermissionError` and
then preserves the failure if the file remains locked. Local Whisper retries one
validated transient Windows invocation error once; a persistent transcription
error still fails the join and leaves its staged evidence for recovery.

Single-range join previews use an input seek and an explicit preview duration so
long source recordings are not decoded from time zero for every small review
window. Multi-range renders retain the filter-graph path used by base-edit
compilation. Preview acceleration changes neither the lossless master profile
nor the requirement for full per-join decode, transcript, audio, visual, and
operator review evidence.

### Confidence behavior

Recommended initial policy:

- an explicit request is mandatory
- action visibility and boundary confidence at or above 0.97 may be `auto_eligible`
- confidence from 0.85 through 0.969 requires review when the requested action cannot be identified safely
- confidence below 0.85 uses normal speed
- any forbidden unrelated content blocks the candidate until it is split or rejected

## Gate 1 review

Gate 1 binds to the focus and pacing plan hash in addition to the edit and effect plans. The review should show:

- each zoom purpose, target, timing, target boxes, evidence frames, confidence, and recommendation
- each speed-up request source, exact action boundaries, rate, audio mode, evidence frames, confidence, and recommendation
- skipped candidates and their safe fallbacks
- no more than the configured maximum number of focused questions

## Gate 2 fix markers

Gate 2 supports:

```text
[ZOOM 00:20.000-00:24.500]
Center the visible prompt box. Start after it opens and end before the results panel appears.

[SPEED 00:31.200-00:36.900]
Speed up only the visible prompt writing at 2.5x. Keep pitch-preserved audio.
```

A fix marker creates a new revision. It does not modify an approved artifact in place.

## Required QA

### Zoom checks

- `ZOOM_PURPOSE_VALID`
- `ZOOM_TARGET_VISIBLE`
- `ZOOM_TARGET_CENTERED`
- `ZOOM_BOUNDARIES_EXACT`
- `ZOOM_EASING_SMOOTH`
- `ZOOM_STABILITY`
- `ZOOM_NO_EMPTY_EDGES`
- `ZOOM_OVERLAY_CLEARANCE`
- `ZOOM_NO_UNRELATED_CONTENT`

### Speed-up checks

- `SPEEDUP_EXPLICITLY_REQUESTED`
- `SPEEDUP_ACTION_VISIBLE`
- `SPEEDUP_ACTION_ALLOWED`
- `SPEEDUP_BOUNDARIES_EXACT`
- `SPEEDUP_NO_UNRELATED_CONTENT`
- `SPEEDUP_AUDIO_AUDIBLE`
- `SPEEDUP_AUDIO_SYNC`
- `SPEEDUP_EXPECTED_DURATION`
- `SPEEDUP_TRANSCRIPT_PRESENT`

A required failure blocks Gate 2. A warning may proceed only under the configured review policy and current approval.
