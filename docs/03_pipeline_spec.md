# Pipeline Specification

The implementation pipeline follows the twelve phases in `TASKS.md`.

## P0 foundation

Run dependency, storage, font, codec, process, schema, and optional worker
checks. Record tool versions. Missing GPU workers are warnings and do not block
the final workflow; they are actionable only when an optional worker extension
has been explicitly re-enabled.

Outputs:

- local capability report
- validated schema examples
- process runner tests
- generated FFmpeg fixture
- initial Codex phase result

## P1 project and ingest

Create a project revision, copy or register source media according to policy, calculate SHA-256, probe streams, and create proxies. Preserve camera, screen, and microphone tracks separately when available.

Outputs:

- `project-manifest.json`
- `source-manifest.json`
- `media-probe.json`
- edit proxy
- speech proxy
- resumable stage record

## P2 transcript and evidence

Transcribe locally with word timing. Run independent silence detection. Normalize source timing to integer microseconds. Preserve raw model and detector output.

Outputs:

- `transcript.json`
- `transcript.md`
- `silence-intervals.json`
- detector and adapter metadata

## P3 planning and Gate 1

Codex analyzes the transcript, takes, segment numbering, screen recordings, creative brief, brand rules, and available assets. It proposes:

- false-start, bad-take, duplicate, silence, and trailing cuts
- caption and phrase grouping direction
- B-roll, motion, and sound moments
- picture-in-picture and screen focus
- object recolor or replacement
- background replacement and behind-subject text
- optional worker work only when a future accepted extension decision requires it; the final path uses chroma key or approved supplied/manual masks
- purposeful zooms for visible opened windows, prompt boxes, relevant cursor actions, or important UI
- explicitly requested prompt-writing or prompt-dictation speed-ups with exact action boundaries
- type-specific confidence, safe fallbacks, and local risk

Every proposal contains source time, stable word IDs where relevant, evidence, reason, risk, fallback, and renderer.

Gate 1 binds to source, transcript, policy, edit plan, effect plan, focus and pacing plan, and selected asset hashes.

Outputs:

- `edit-proposals.json`
- `effect-plan.json`
- `focus-pacing-plan.json`
- review Markdown
- decision artifact
- approval record
- edit decision list
- source-to-output mapping
- approved focus and pacing plan

## P4 base edit and audio

Compile approved keep ranges. Compile explicitly approved prompt speed-ups into a contiguous retimed timeline. FFmpeg trims and retimes picture and production audio from the same exact ranges, concatenates them, normalizes loudness, and creates the clean base edit. Keep sound audible and preserve pitch by default. Rebase transcript timing through the final piecewise map.

Outputs:

- retimed timeline
- base edit
- normalized audio metrics
- output transcript
- render manifest
- base QA report

## P5 Remotion and captions

Build the schema-valid visual timeline. Stage local assets by hash. Compile approved purposeful zooms into target-centered frame keyframes with explicit smooth easing. Render stills, segment previews, background and middle plates, captions, and front layers.

Outputs:

- visual timeline
- caption plan
- composition plan
- Remotion props snapshot
- composition code bundle hash
- stills and local previews

## P6 green-screen and local effects

Select the cheapest verifiable local method:

1. normal Remotion or FFmpeg layer
2. green-screen chroma key
3. approved local mask

Build the complete local milestone with subject separation, background replacement, behind-subject text, local mask recolor, front captions, and production audio.

Outputs:

- foreground and alpha intermediate
- local mask validation report
- effect previews and contact sheets
- decoded local milestone render
- QA report

## P7 SAM 3.1 worker (optional deferred extension)

P7 is not required by the final workflow and is not invoked by the required
path. If a future accepted decision re-enables it, after the manual
preconditions pass:

1. write a segmentation job
2. invoke the isolated worker
3. normalize and validate results
4. generate continuity metrics and contact sheets
5. require human identity review

Do not use masks before the result and review pass.

## P8 MatAnyone 2 worker (optional deferred extension)

P8 is not required by the final workflow and is not invoked by the required
path. If a future accepted decision re-enables it, after the manual
preconditions pass:

1. provide an approved first-frame person mask
2. write a matting job
3. invoke the isolated worker
4. verify output roles and alpha semantics
5. render contrasting-background previews
6. require human quality review

## Final workflow scope

ADR-0013 defines the required dependency-safe path as P0-P6, U21/U22, P9,
P10, and P11. P9 consumes reviewed supplied/manual masks or tracks when an
effect needs them and preserves a source-shot fallback. P7 and P8 remain
versioned isolated contracts for future extensions only; fake or dry-run
worker evidence cannot be presented as live model acceptance.

## P9 object effects, assets, B-roll, and sound

Turn reviewed masks and geometry into recolor, replacement, and occlusion effects. Search the local asset library using transcript context. Plan and mix sound with speech priority. Optional providers remain disabled without bounded approval.

Outputs:

- tracked keyframes
- project asset manifest
- B-roll, motion, and sound plans
- object effect previews
- mixed segment candidates

## P10 segment review, re-transcription, and Gate 2

Render each logical segment independently. For each segment produce a preview, contact sheet, transcript excerpt, effect summary, diagnostics, and QA findings.

Import timestamped fix markers. Apply them as a new revision. Re-transcribe and compare intended and rendered speech. Repeat until the segment is approved.

Gate 2 binds to preview, transcript comparison, assets, relevant composition code bundle, and QA hashes.

## P11 final QA, Gate 3, delivery, and cleanup

Assemble approved segment revisions. Apply the final audio pass. Render the complete final candidate. Run automated checks and record the required watch-through.

Gate 3 binds to final preview, final QA, plans, asset manifest, code bundle, and delivery profile.

Create:

- final master and derivatives
- caption sidecars
- final transcript
- chapter suggestions
- description draft
- checksums
- delivery manifest
- backup verification report
- cleanup dry run

Cleanup remains blocked until backup verification and separate approval.

## Version 2.2 pipeline insertion

After transcription and silence analysis, run a smart-dense candidate pass and take-ranking pass. After approval compilation, assign a join strategy before base rendering. After base rendering, render and re-transcribe every join preview. Repair failed joins before downstream visual work.

Detect structural boundaries only after the approved content order is stable. Produce a separate transition plan. Render structural transitions and synchronized sound after ordinary joins pass. Run transition QA before Gate 2.
