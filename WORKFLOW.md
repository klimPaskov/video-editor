# Final Video Editing Workflow

## Goal

Turn recorded footage and a creative brief into a reviewed final video without manual timeline editing. Codex plans and coordinates the work. Typed tools perform media operations. The operator approves the current plan, current segment previews, and current final candidate.

## Preferred recording protocol

The most reliable first path is a one-take recording in front of a clean green screen.

- Use even lighting on the screen and subject.
- Separate the subject from the screen when possible.
- Keep motion blur low enough for usable edges.
- Record at a fixed frame rate.
- Record clean production audio on a separate channel or device when available.
- Keep the object to be tracked visible before the effect starts.
- Avoid complete object occlusion unless an occluder track is planned.
- Say effect cues naturally when transcript-triggered timing is desired.
- Record screen and camera feeds separately when picture-in-picture flexibility matters.

The final workflow uses controlled green-screen chroma key or an approved
project-owned supplied/manual mask. MatAnyone 2 remains a deferred optional
extension for a future accepted decision; it is not installed, invoked, or
required by the final path.

## Project layout

```text
projects/<project-id>/
  raw/                 immutable managed source copies
  config/              project, brand, policy, and delivery snapshots
  artifacts/           manifests, transcripts, plans, mappings, jobs, and reports
  work/                disposable derived files and experiments
  review/              human-readable plans, previews, contact sheets, and fixes
  output/              approved previews, master, derivatives, and sidecars
  logs/                redacted process and stage logs
  state/               resumable stage state, locks, and cache records
```

## P0: foundation

Codex establishes the Python and Node projects, process adapter, schemas, structured logs, doctor checks, CI, skills, and a decoded FFmpeg fixture.

No real source media, checkpoint, credential, or paid provider is needed.

## P1: initialize, ingest, and probe

Example commands:

```bash
videoedit init my-video
videoedit ingest my-video /path/to/source.mp4
videoedit probe my-video
```

Actions:

1. Create the project and initial revision.
2. Copy or register the source under the configured ingest policy.
3. Compute and persist SHA-256.
4. Protect managed source copies from writes.
5. Probe all streams, duration, time base, frame rate, orientation, colour, and codecs.
6. Create normalized edit and speech proxies where required.
7. Stop on unreadable media, unsupported timing, insufficient space, or unexpected mutation.

## P2: transcript and silence evidence

Example commands:

```bash
videoedit transcribe my-video
videoedit detect-silence my-video
```

Actions:

1. Extract a mono speech proxy.
2. Run local Whisper with word timing.
3. Assign stable IDs to transcript words.
4. Detect silence independently with FFmpeg.
5. Normalize all source times to integer microseconds.
6. Preserve raw detector and transcription metadata.
7. Export readable timestamped transcript Markdown.

No semantic edit has been approved at this point.

## P3: edit and effect planning

Codex reads the transcript, silence evidence, recording structure, creative brief, brand rules, and available asset descriptions.

It creates:

- mechanical cut candidates
- semantic cut proposals
- selected-take proposals
- effect triggers tied to stable word IDs or explicit source ranges
- local asset requests
- optional worker requests only when an explicitly re-enabled extension is in scope; the final path has no worker dependency
- fallbacks and review risks
- a focus and pacing plan for purposeful zooms and explicitly requested prompt speed-ups
- readable plan Markdown
- schema-valid plan JSON

Supported effect kinds include:

- `caption`
- `motion_graphic`
- `sound_effect`
- `broll`
- `track_recolor`
- `track_replace`
- `person_matte`
- `background_replace`
- `text_between_subject_and_background`
- `picture_in_picture`
- `screen_focus`
- `purposeful_zoom`

### Gate 1

The operator reviews:

- proposed cuts and retained meaning
- selected takes
- effect timing and trigger phrases
- object or person prompts
- background, text, motion, B-roll, and sound choices
- zoom purpose, visible target, timing, centering evidence, and confidence
- requested prompt speed-up boundaries, rate, audio mode, and confidence
- local asset provenance
- requested GPU work
- any predicted paid work

Gate 1 approval binds to the exact source, transcript, policy, edit plan, effect plan, focus and pacing plan, and asset hashes. Uncertain items are batched with recommendations. Low-confidence zooms use no zoom. Low-confidence speed-ups use normal speed.


## Smart-dense cut planning and structural transitions

This contract applies across P3, P4, P5, P9, and P10.

### Candidate generation

1. Scan every word, transcript gap, silence interval, take boundary, and nearby meaning span.
2. Generate all qualifying mechanical candidates. Do not impose a per-minute cap on safe micro edits.
3. Classify candidates as leading silence, trailing silence, long pause, dead air, filler word, filler phrase, stutter, false start, abandoned phrase, self-correction, immediate repetition, exact repetition, near repetition, semantic repetition, duplicate take, weak take, tangent, housekeeping, accidental noise, or hook tightening.
4. Attach transcript, timing, silence, repetition, semantic, audio-join, visual-join, and overall confidence where relevant.
5. Protect facts, names, numbers, negation, qualifications, warnings, useful breaths, intentional pauses, emotion, overlapping speakers, and uncertain words.
6. Let high-confidence low-risk mechanical edits proceed under the approved policy. Batch material semantic or continuity uncertainty for review.

### Cut compilation and repair

1. Compile approved cuts into canonical keep ranges.
2. Give every cut a join strategy and handles.
3. Prefer precise micro edits over coarse removals when the resulting speech remains natural.
4. Repair joins with a hard cut, short audio crossfade, adjusted handles, room tone, J-cut, L-cut, B-roll cover, alternate coverage, or purposeful punch-in.
5. Render a short join preview with context on both sides.
6. Re-transcribe the preview and compare it with the approved transcript.
7. Check missing or duplicate words, grammar, meaning, clipped syllables, clicks, room tone, cadence, flashes, frozen frames, face movement, cursor continuity, and screen state.
8. Route failed joins to repair or focused review. High cut density by itself is not a failure.

### Structural transition planning

1. Detect real structural boundaries such as a new point, chapter, mode change, comparison, before-and-after state, major demonstration, location change, or return from a major explanation.
2. Keep routine cleanup cuts free of decorative motion.
3. Select a hard cut, J-cut, L-cut, short crossfade, dip, swipe, push, blur swipe, or chapter transition according to purpose.
4. Require exact outgoing and incoming segments, duration, direction, easing, full-frame coverage, dialogue clearance, confidence, and a clean fallback.
5. Pair motion transitions with a licensed sound cue when required. Align the sound transient to the strongest visual movement and protect the first important incoming word.
6. Apply reuse spacing and reject random timing, random targets, repeated gimmicks, edge gaps, unreadable incoming frames, or masked dialogue.
7. Render first, midpoint, and final proof frames plus a short audio preview for QA.

## P4: deterministic base edit and audio

Actions:

1. Compile approved source ranges to keep.
2. Compile approved prompt speed-ups into a retimed timeline.
3. Build the authoritative source-to-output time map.
4. Render picture and production audio from the same timeline.
5. Preserve audible, pitch-adjusted sound for speed-ups unless an explicit exception exists.
6. Run two-pass dialogue loudness normalization.
7. Rebase transcript words and segments to output time.
8. Validate exact speed-up boundaries, decode, duration, frame count, audio duration, joins, clipping, and A/V drift.
9. Persist the retimed timeline, render manifest, and QA findings.

The clean base edit must work before expensive visual analysis.

## P5: Remotion, captions, and local motion

Remotion owns the declarative visual timeline:

- backgrounds
- text and typography
- branded captions
- local motion graphics and diagrams
- B-roll and replacement images
- picture-in-picture layouts
- front and middle visual layers
- frame-based keyframes
- purposeful target-centered zooms from the approved focus and pacing plan

FFmpeg remains responsible for exact media cutting, audio, masks, alpha, codecs, and final validation.

P5 proves:

1. schema-valid timeline props
2. deterministic frame and duration mapping
3. middle plate rendering for background plus behind-subject text
4. front pass rendering for captions and labels
5. local asset staging and font loading
6. low-cost segment previews
7. smooth zoom easing, stable target centering, and evidence that the target is visible only during the relevant action

## P6: green-screen and local mask effects

This is the first full visual milestone. It requires no gated checkpoint.

### Person extraction

1. Apply FFmpeg chroma key and despill.
2. Produce an alpha-capable foreground intermediate.
3. Inspect hair, hands, clothes, motion blur, holes, and spill.

### Text behind the subject

1. Remotion renders the background and middle text plate.
2. FFmpeg composites the transparent subject above that plate.
3. Remotion renders front captions and labels.

### Object recoloring from a local mask

1. Validate mask dimensions, frame count, range, and polarity.
2. Create a colour-transformed source version.
3. Merge transformed and original pixels through the mask.
4. Inspect first, middle, last, high-motion, and edge frames.

P6 must produce one decoded end-to-end fixture with approved cuts, production audio, background replacement, behind-subject text, object recolor, and branded captions.

## P7: SAM 3.1 object worker (optional deferred extension)

P7 is not part of the final working workflow. It remains an isolated,
versioned extension boundary for a future decision. No final-workflow stage may
require this worker or block because its environment is unavailable. If the
extension is re-enabled, it begins only after operator licence review,
checkpoint access, hardware confirmation, and P6 success.

Before a v1.1 job can use live runtime access, `videoedit approve-worker-runtime`
must persist a separate hash-bound human acceptance for the exact upstream
commit, checkpoint hash, licence identity, PyTorch/CUDA stack, and target
device. Gate 1 effect approval alone never authorizes installation or GPU use.

A job declares:

- source identity and frame range
- prompt type and prompt value
- prompt frame
- expected object count
- output mask format
- worker and schema version

The worker exports:

- lossless masks
- stable object IDs when available
- bounding boxes, centroids, and area per frame
- missing-frame and identity warnings
- upstream commit, checkpoint identity, environment, and device metadata
- hashes and contact sheets

Uncertain object identity fails the effect and preserves the original shot fallback.

## P8: MatAnyone 2 person worker (optional deferred extension)

P8 is not part of the final working workflow. It remains an isolated,
versioned extension boundary for a future decision. The final workflow uses
chroma key or an approved supplied/manual mask and does not call this worker.
If the extension is re-enabled, it begins only after operator licence review,
hardware confirmation, an accepted upstream pin, and an approved first-frame
person mask.

MatAnyone 2 jobs use the same separate worker-runtime acceptance. The accepted
runtime reference is carried into the job and must match the code, checkpoint,
licence, and device identity before the isolated worker can run.

The worker accepts a source video and first-frame person mask. It exports verified foreground and alpha outputs, metadata, hashes, and stability findings.

Review includes hair, fingers, loose clothing, transparent areas, holes, fast motion, blur, entry, exit, and temporal edge stability. Chroma key stays preferred for controlled green-screen footage.

## Final workflow scope decision

Under ADR-0013, the dependency-safe final path is P0-P6, U21/U22, P9, P10,
and P11. SAM 3.1 and MatAnyone 2 contracts, fake-worker tests, and isolated
runtime gates remain available for future extensions only. `videoedit doctor`
may report their missing capability as a warning, but the final workflow does
not install or invoke either worker. P9 uses reviewed supplied/manual masks or
tracks and preserves the original-shot fallback when a tracked effect cannot
be verified.

## P9: object effects, assets, B-roll, and sound

### Object replacement

1. Convert reviewed object geometry into smoothed position, scale, rotation, and visibility keyframes.
2. Place a licensed replacement asset in Remotion.
3. Use explicit hand or finger occluder masks when needed.
4. Keep the original shot as the declared fallback.
5. Route inpainting through a separate optional adapter only when the original remains visible.

### Local asset library

Index every B-roll clip, sound, image, background, font, and replacement object with:

- SHA-256
- path or managed URI
- media properties
- description and tags
- source and licence
- permitted uses and attribution
- usage history

Codex searches this index with transcript context. Asset selection remains a proposal until approved.

### Sound

Sound plans record source, licence, placement, purpose, gain, fades, ducking, and collision policy. Speech retains priority. Clipping and overuse fail QA.

## P10: segment previews, fixes, re-transcription, and Gate 2

Render one preview per logical segment or effect group. Each review package includes:

- preview video
- contact sheet
- relevant transcript excerpt
- effect and asset summary
- mask or matte diagnostics
- machine QA findings
- known warnings

The operator writes timestamped markers:

```text
[FIX 00:35.200-00:37.000]
Delete the first duplicate phrase and keep the second.

[MASK 00:41.000-00:42.500]
The hand leaks into the object mask. Refine the prompt or mask.

[RETIME 00:54.100-00:56.000]
Hold the replacement asset for another 12 frames.

[ZOOM 01:10.000-01:14.500]
Center the visible prompt box. Start after it opens and end before the results panel appears.

[SPEED 01:22.200-01:27.900]
Speed up only the visible prompt writing at 2.5x and keep pitch-preserved audio.
```

Codex applies fixes as a new revision. It re-transcribes the rendered segment and compares intended speech with rendered speech. It also checks joins, captions, freeze frames, black frames, A/V drift, z-order, safe areas, mask continuity, matte flicker, hidden screen content, zoom purpose and stability, and speed-up request scope, boundaries, audio, and transcript continuity.

### Gate 2

The operator approves only the current segment revision. Approval binds to preview, transcript comparison, effect assets, composition code bundle, and QA hashes.

## P11: final QA, Gate 3, delivery, and cleanup

Actions:

1. Assemble approved segment revisions.
2. Run the final loudness pass.
3. Render the final preview and machine QA report.
4. Record the required full watch-through.
5. Confirm every external asset has permitted provenance.
6. Confirm all approvals and any spend records are current.

### Gate 3

Final approval binds to:

- final preview hash
- final QA report hash
- edit and effect plan hashes
- asset manifest hash
- composition code bundle hash
- delivery profile hash

After Gate 3:

1. Render the master and selected derivatives.
2. Create caption sidecars and final transcript.
3. Create chapter suggestions and a short description draft.
4. Write checksums and a delivery manifest.
5. Verify source and final master backups by hash.
6. Produce a cleanup dry run.
7. Delete only eligible derived work after explicit cleanup approval.

## Agent loop

For every phase Codex follows this cycle:

1. Read current state, active prompt, and relevant skill.
2. Identify exact task IDs and dependencies.
3. Form the smallest testable plan.
4. Add or update tests.
5. Call deterministic tools or isolated workers.
6. Validate schemas and media outputs.
7. Inspect diagnostics and visual evidence.
8. Revise when evidence shows a defect.
9. Stop at approval, licence, credential, spend, or cleanup boundaries.
10. Record commands, versions, hashes, results, and remaining risks.

Experiments belong under `work/`. Failed experiment evidence may be retained for diagnosis. Codex may not mutate `raw/`, bypass approval, invent successful model output, or hide failed checks.
