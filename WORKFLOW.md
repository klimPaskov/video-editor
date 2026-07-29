# Final Video Editing Workflow

## Goal

Turn recorded footage and a creative brief into a reviewed final video without manual timeline editing. Codex plans and coordinates the work. Typed tools perform media operations. The operator approves the current plan, current segment previews, and current final candidate.

## Preferred recording protocol

The supported input is a screen recording with a stable capture configuration.

- Record at a fixed frame rate.
- Record clean production audio on a separate channel or device when available.
- Keep important prompt-writing actions and relevant UI visible long enough to
  identify their exact start and end boundaries.
- Avoid unnecessary window changes during an action that should receive a
  purposeful focus or speed-up.

The final workflow uses the original screen recording, local captions, text and
motion graphics, B-roll, picture-in-picture, sound, and approved prompt-action
retiming. Optional vision workers are not installed or invoked by the public
workflow.

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
- fallbacks and review risks
- a focus and pacing plan for purposeful zooms and explicitly requested prompt speed-ups
- readable plan Markdown
- schema-valid plan JSON

Supported effect kinds include:

- `caption`
- `motion_graphic`
- `sound_effect`
- `broll`
- `picture_in_picture`
- `screen_focus`
- `purposeful_zoom`

### Gate 1

The operator reviews:

- proposed cuts and retained meaning
- selected takes
- effect timing and trigger phrases
- visible UI targets and action ranges
- background, text, motion, B-roll, and sound choices
- zoom purpose, visible target, timing, centering evidence, and confidence
- requested prompt speed-up boundaries, rate, audio mode, and confidence
- local asset provenance
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
7. Check missing or duplicate words, grammar, meaning, clipped syllables, clicks, room tone, cadence, flashes, frozen frames, cursor continuity, and screen state.
8. Route failed joins to repair or focused review. High cut density by itself is not a failure.

### Structural transition planning

1. Detect real structural boundaries such as a new point, chapter, mode change, comparison, before-and-after state, major demonstration, location change, or return from a major explanation.
2. Keep routine cleanup cuts free of decorative motion.
3. Select a hard cut, J-cut, L-cut, short crossfade, dip, swipe, push, blur swipe, or chapter transition according to purpose.
4. Require exact outgoing and incoming segments, duration, direction, easing, full-frame coverage, dialogue clearance, confidence, and a clean fallback.
5. Pair motion transitions with a licensed sound cue when required. Align the sound transient to the strongest visual movement and protect the first important incoming word.
6. Apply reuse spacing and reject random timing, random targets, repeated gimmicks, edge gaps, unreadable incoming frames, or dialogue overlap.
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

FFmpeg remains responsible for exact media cutting, audio, codecs, and final validation.

P5 proves:

1. schema-valid timeline props
2. deterministic frame and duration mapping
3. middle plate rendering for background plus behind-content text
4. front pass rendering for captions and labels
5. local asset staging and font loading
6. low-cost segment previews
7. smooth zoom easing, stable target centering, and evidence that the target is visible only during the relevant action

## P6: screen composition and focused pacing

This is the first full visual milestone and requires no gated model or worker.

1. Compile the approved edit and retimed prompt-action ranges from one source
   timeline.
2. Render the Remotion background, behind-content text, captions, and labels.
3. Apply only purposeful target-centered UI zooms with verified boundaries.
4. Preserve sound during approved prompt-action speed-ups and keep the original
   voice pitch.
5. Decode and inspect the end-to-end fixture for timing, joins, audio, captions,
   safe areas, and motion quality.

P6 must produce one decoded end-to-end fixture with approved cuts, production
audio, screen text, captions, and purposeful focus.

## Deferred integrations

Optional vision-worker contracts remain isolated extension material. They are
not installed, invoked, or required by the public workflow, and their missing
runtime or model files cannot block the supported screen-recording path.

## Final workflow scope decision

The dependency-safe final path is P0-P6, U21/U22, P9, P10, and P11. The public
doctor command checks only the local runtime needed by that path. P9 keeps a
source-shot fallback whenever a tracked effect cannot be verified.

## P9: assets, B-roll, picture-in-picture, and sound

### Local asset library

Index every B-roll clip, sound, image, background, font, and replacement object with:

- SHA-256
- path or managed URI
- media properties
- description and tags
- source and licence
- permitted uses and attribution when applicable
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
- machine QA findings
- known warnings

The operator writes timestamped markers:

```text
[FIX 00:35.200-00:37.000]
Delete the first duplicate phrase and keep the second.

[TEXT 00:54.100-00:56.000]
The chapter label overlaps the active prompt box. Move it to the safe area.

[ZOOM 01:10.000-01:14.500]
Center the visible prompt box. Start after it opens and end before the results panel appears.

[SPEED 01:22.200-01:27.900]
Speed up only the visible prompt writing at 2.5x and keep pitch-preserved audio.
```

Codex applies fixes as a new revision. It re-transcribes the rendered segment and compares intended speech with rendered speech. It also checks joins, captions, freeze frames, black frames, A/V drift, z-order, safe areas, hidden screen content, zoom purpose and stability, and speed-up request scope, boundaries, audio, and transcript continuity.

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
