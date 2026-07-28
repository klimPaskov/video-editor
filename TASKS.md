# Ordered Implementation Backlog

Complete required phases in order. A task may be checked only when its acceptance evidence exists in code, tests, artifacts, or a recorded operator decision. P7 and P8 are retained as optional deferred extensions under ADR-0013 and are not required by the final working workflow.

## P0: foundation and local controls

- [x] P0-01 Establish the Python 3.11 package, lockfile, CLI entry point, and supported exit codes.
- [x] P0-02 Establish the Node.js 22 Remotion project, lockfile, composition entry point, and type check.
- [x] P0-03 Implement a typed local process runner with argument arrays, timeouts, output limits, redaction, cancellation, and stable errors.
- [x] P0-04 Implement a worker process adapter that exchanges versioned JSON jobs and results.
- [x] P0-05 Implement structured logging with project, revision, stage, attempt, command, duration, and redacted output fields.
- [x] P0-06 Implement `videoedit doctor` for FFmpeg, ffprobe, codecs, filters, fonts, disk, Node, npm, Remotion, and optional workers.
- [x] P0-07 Validate every example against its JSON Schema in local checks and CI.
- [x] P0-08 Configure Ruff, mypy, pytest, TypeScript, shell syntax, and continuous integration checks.
- [x] P0-09 Add repository skills, the official Remotion skill installer, phase result writing, and skeptical review instructions.
- [x] P0-10 Prove a short generated FFmpeg fixture can be created, probed, and decoded.

Acceptance evidence:

- `uv run videoedit doctor --json`
- `uv run python scripts/validate_examples.py`
- `uv run pytest -q`
- `npm run typecheck` inside `remotion/`
- a decoded short fixture and `.codex/results/P0.json`

## P1: project, immutable ingest, and media probe

- [x] P1-01 Create the project directory, project manifest, revision layout, and project lock.
- [x] P1-02 Copy or register source media under a declared ingest policy without modifying source bytes.
- [x] P1-03 Compute source SHA-256, size, modification evidence, and read-only protection for managed copies.
- [x] P1-04 Parse ffprobe JSON into typed stream, time base, frame rate, orientation, colour, duration, and audio models.
- [x] P1-05 Detect corrupt media, unsupported codecs, missing audio, rotation, variable frame rate, and inconsistent timing.
- [x] P1-06 Create normalized edit and mono speech proxies with recorded commands and hashes.
- [x] P1-07 Implement stage keys, staging paths, atomic promotion, and interrupted ingest recovery.
- [x] P1-08 Add idempotent ingest, duplicate source, mutation, disk exhaustion, and invalid input tests.

Acceptance evidence:

- repeated ingest reuses the valid result
- source hashes remain unchanged
- invalid fixtures produce actionable status
- `.codex/results/P1.json`

## P2: transcript and silence evidence

- [x] P2-01 Implement a local Whisper adapter with a deterministic fake adapter for tests.
- [x] P2-02 Persist ordered transcript segments and stable word identifiers with integer microsecond bounds.
- [x] P2-03 Record model, language, device, adapter version, and available confidence evidence.
- [x] P2-04 Parse FFmpeg silence detection output and preserve the raw detector evidence.
- [x] P2-05 Detect overlapping, reversed, negative, out-of-bounds, and low-confidence transcript timing.
- [x] P2-06 Export readable timestamped transcript Markdown for planning and review.
- [x] P2-07 Add no-speech, multiple-speaker-warning, noisy-audio, and interrupted-transcription tests.

Acceptance evidence:

- no API key is required
- words are ordered and bounded
- silence intervals are independently reproducible
- `.codex/results/P2.json`

## P3: edit and effect planning, followed by Gate 1

- [x] P3-01 Generate deterministic long-pause and mechanical trim candidates from policy.
- [x] P3-02 Generate semantic proposals for false starts, duplicate phrases, weak takes, and removable tangents with evidence.
- [x] P3-03 Protect uncertain speech, negation, numbers, names, calls to action, and low-confidence regions from automatic semantic removal.
- [x] P3-04 Create an effect plan with transcript word IDs, source ranges, renderer choice, assets, fallback, risk, and approval requirement.
- [x] P3-05 Support captions, motion, sound, B-roll, picture-in-picture, screen focus, object recolor, object replacement, person matte, background replacement, and behind-subject text.
- [x] P3-06 Export immutable JSON proposals and readable Markdown review files.
- [x] P3-07 Import approve, reject, and modify decisions without editing proposal artifacts in place.
- [x] P3-08 Bind Gate 1 approval to source, transcript, policy, edit plan, effect plan, and asset hashes.
- [x] P3-09 Compile approved source keep ranges and a source-to-output mapping.
- [x] P3-10 Reject stale approvals, overlaps, missing decisions, unsupported effects, and out-of-bounds changes.
- [x] P3-11 Create a schema-valid focus and pacing plan for purposeful zooms and explicitly requested prompt speed-ups.
- [x] P3-12 Calculate type-specific target, boundary, stability, transcript, and audio confidence, then apply auto, review, skip, or block policy.
- [x] P3-13 Batch material uncertainties into one concise review with recommendations, evidence, and safe fallbacks.

Acceptance evidence:

- no semantic cut is self-approved
- every cut and effect has source evidence
- every zoom has a visible allowed target and every speed-up has an explicit request
- changed edit, effect, or focus and pacing plans invalidate Gate 1
- `.codex/results/P3.json`

## P4: deterministic base edit and audio

- [x] P4-01 Compile picture and production audio from the same approved keep ranges.
- [x] P4-02 Preserve frame order, sample continuity, time bases, and explicit stream maps.
- [x] P4-03 Render a clean base edit with no visual effect dependency.
- [x] P4-04 Implement two-pass loudness measurement and normalization with recorded input and output metrics.
- [x] P4-05 Rebase transcript segments and words through the source-to-output mapping.
- [x] P4-06 Persist render manifests with commands, versions, hashes, expected duration, actual duration, and warnings.
- [x] P4-07 Check decode, frame count, duration, audio duration, clipping, and A/V drift around every join.
- [x] P4-08 Add multiple-range, first-frame, final-frame, missing-audio, and rational-frame-rate tests.
- [x] P4-09 Compile approved prompt speed-ups into a canonical retimed timeline with exact source and output ranges.
- [x] P4-10 Render retimed picture and production audio together, keeping pitch-preserved audible sound by default.

Acceptance evidence:

- approved base edit decodes fully
- A/V drift is within configured limits
- loudness result meets the selected profile
- prompt speed-ups contain only the requested visible action and map every later timestamp correctly
- `.codex/results/P4.json`

## P5: Remotion, captions, and local motion

- [x] P5-01 Define a schema-valid visual timeline and matching TypeScript types.
- [x] P5-02 Stage only hash-verified local assets under the Remotion public directory.
- [x] P5-03 Implement frame-driven background, text, image, video, audio, and picture-in-picture layers.
- [x] P5-04 Implement branded word-timed captions with grouping, emphasis, safe areas, and collision policy.
- [x] P5-05 Implement reusable typography, lower thirds, callouts, diagrams, and transition primitives.
- [x] P5-06 Implement background and middle-layer plate rendering for behind-subject layouts.
- [x] P5-07 Implement front-layer rendering for captions and labels after subject compositing.
- [x] P5-08 Add composition listing, still preview, segment preview, full render, and props validation commands.
- [x] P5-09 Add frame boundary, duration, z-order, missing asset, font, and type-check tests.
- [x] P5-10 Implement purposeful target-centered zooms with explicit easing, stabilized transforms, and no untargeted whole-screen movement.
- [x] P5-11 Render zoom proof frames and short previews that verify target visibility, centering, relevance boundaries, edge coverage, and motion stability.

Acceptance evidence:

- a local scene renders from JSON props
- captions stay inside configured safe areas
- middle and front layers have deterministic z-order
- every zoom has an allowed purpose and a clear visible target or is omitted
- `.codex/results/P5.json`

## P6: green-screen and local mask effects

- [x] P6-01 Implement FFmpeg chroma key with configurable colour, similarity, blend, despill, crop, and edge controls.
- [x] P6-02 Encode and validate an alpha-preserving foreground intermediate.
- [x] P6-03 Composite a Remotion background and middle text plate under the subject.
- [x] P6-04 Render captions and front graphics above the subject.
- [x] P6-05 Implement mask-driven object recoloring from a supplied lossless local mask sequence or mask video.
- [x] P6-06 Validate mask dimensions, frame count, range alignment, polarity, and alpha semantics.
- [x] P6-07 Build a complete synthetic or licensed local milestone showing cut, audio, recolor, cutout, background replacement, behind-subject text, and captions.
- [x] P6-08 Generate contact sheets and inspect hair, hands, motion blur, spill, holes, edges, and effect boundaries.

Acceptance evidence:

- the complete local milestone works without GPU models or paid providers
- production audio is preserved
- every generated video decodes
- `.codex/results/P6.json`

## P7: SAM 3.1 object segmentation and tracking worker (optional deferred extension)

- [x] P7-01 Review the current official repository, SAM Licence, checkpoint terms, supported Python, PyTorch, CUDA, and exact predictor API.
- [ ] P7-02 Record an ADR with the accepted upstream commit and checkpoint identity. Deferred by ADR-0013; no live worker pin is required for the final workflow.
- [x] P7-03 Create an isolated Python 3.12 worker environment without changing the core environment.
- [x] P7-04 Implement versioned segmentation job and result contracts.
- [x] P7-05 Support text, point, box, or mask prompts where the selected upstream API supports them.
- [x] P7-06 Restrict each job to an approved source range and prompt frame.
- [x] P7-07 Export lossless masks, object IDs, bounding boxes, centroids, area, missing frames, and raw worker metadata.
- [x] P7-08 Detect identity switches, sudden area changes, jumps, leaks, and missing masks.
- [x] P7-09 Generate contact sheets and require review of first, middle, last, high-motion, entry, exit, and occlusion frames.
- [ ] P7-10 Add fake worker contract tests and one short live target-GPU smoke test. Deferred by ADR-0013; fake contract coverage remains useful, but live worker acceptance is not part of the final workflow.

Acceptance evidence for the optional extension:

- a reviewed real clip produces stable masks on the target GPU
- incompatible checkpoints or uncertain identity fail clearly
- `.codex/results/P7.json`

## P8: MatAnyone 2 person matting worker (optional deferred extension)

- [x] P8-01 Review the current official repository, NTU S-Lab License 1.0, checkpoint, supported Python, and exact inference API.
- [ ] P8-02 Record an ADR with the accepted upstream commit and checkpoint identity. Deferred by ADR-0013; no live worker pin is required for the final workflow.
- [x] P8-03 Create an isolated Python 3.10 worker environment without changing the core or SAM environment.
- [x] P8-04 Implement versioned matting job and result contracts.
- [x] P8-05 Accept an approved first-frame person mask from SAM, an interactive tool, or a manual file.
- [x] P8-06 Identify and verify the foreground and alpha output roles before composition.
- [x] P8-07 Export hashes, dimensions, frame count, model identity, warnings, and stability metrics.
- [x] P8-08 Check hair, fingers, loose clothing, holes, transparent regions, fast motion, motion blur, and temporal edge stability.
- [x] P8-09 Render contrasting-background previews and compare them with the source.
- [ ] P8-10 Add fake worker contract tests and one short live target-GPU smoke test. Deferred by ADR-0013; fake contract coverage remains useful, but live worker acceptance is not part of the final workflow.

Acceptance evidence for the optional extension:

- a reviewed real clip produces a verified foreground and alpha result
- output semantics are proved before consumption
- chroma key remains the preferred controlled-shoot path
- `.codex/results/P8.json`

### Final workflow scope

P7 and P8 remain isolated contract boundaries for a future re-enable decision;
they are not required dependencies of the final workflow. The dependency-safe
working path is P0-P6, U21/U22, P9, P10, and P11. P9 consumes reviewed supplied
or manual masks and tracks when an effect needs them, and P6 uses chroma key or
an approved local mask for subject separation. The unchecked live worker tasks
remain explicitly deferred under ADR-0013 and must not be marked complete by
fake tests or by an FFmpeg AMD encoder result.

## P9: object effects, asset library, B-roll, and sound

- [x] P9-01 Convert approved object tracks into smoothed position, scale, rotation, and visibility keyframes.
- [x] P9-02 Implement object replacement with an original-shot fallback.
- [x] P9-03 Support explicit foreground occluder tracks for hands, fingers, and other crossing objects.
- [x] P9-04 Keep inpainting behind a separate optional adapter and approval boundary.
- [x] P9-05 Index local B-roll, sound, images, backgrounds, fonts, and replacement objects with hashes, descriptions, tags, licences, and usage history.
- [x] P9-06 Search the local asset index using transcript context and effect intent.
- [x] P9-07 Plan and approve B-roll, motion, and sound cue placement with density and collision rules.
- [x] P9-08 Implement speech-priority mixing, gain, fades, ducking, and clipping checks.
- [x] P9-09 Record every selected asset and licence in the project asset manifest.
- [x] P9-10 Keep network providers disabled until a current bounded spend approval exists.

Acceptance evidence:

- object recolor and replacement work from reviewed masks and tracks
- local assets can be found, approved, rendered, mixed, and audited
- no paid service is required
- `.codex/results/P9.json`

## P10: segment review, re-transcription, QA, and Gate 2

- [x] P10-01 Divide the project into logical review segments and render low-cost previews.
- [x] P10-02 Generate contact sheets, transcript excerpts, effect summaries, mask or matte diagnostics, and warnings for each segment.
- [x] P10-03 Import `[FIX]`, `[KEEP]`, `[REMOVE]`, `[RETIME]`, `[MASK]`, `[TEXT]`, and `[AUDIO]` markers from review Markdown.
- [x] P10-04 Apply fixes as a new immutable revision and invalidate only dependent stages.
- [x] P10-05 Re-transcribe rendered segments and compare intended speech with rendered speech.
- [x] P10-06 Detect duplicate phrases, missing words, dead air, abrupt joins, caption drift, freeze frames, black frames, clipping, and A/V drift.
- [x] P10-07 Check mask continuity, matte flicker, effect boundaries, z-order, safe areas, hidden screen content, and picture-in-picture framing.
- [x] P10-08 Bind Gate 2 approval to segment preview, transcript comparison, effect assets, composition code bundle, and QA hashes.
- [x] P10-09 Lock approved segment revisions while preserving the ability to create a later revision.
- [x] P10-10 Import `[ZOOM]` and `[SPEED]` markers as revision-safe focus and pacing changes.
- [x] P10-11 Run required zoom QA for purpose, target visibility, centering, boundaries, easing, stability, edge coverage, and unrelated content.
- [x] P10-12 Run required speed-up QA for request scope, allowed action, exact boundaries, audible audio, synchronization, duration, and re-transcribed speech.

Acceptance evidence:

- a duplicate phrase fixture is corrected from a fix marker and verified by re-transcription
- changed segment inputs make approval stale
- unresolved required zoom or speed-up QA blocks Gate 2
- `.codex/results/P10.json`

## P11: final QA, Gate 3, delivery, and safe operations

- [x] P11-01 Assemble approved segment revisions and run the final loudness pass.
- [x] P11-02 Run the required decode, duration, stream, frame rate, audio, loudness, A/V sync, caption, visual, provenance, and approval checks.
- [x] P11-03 Require a recorded full watch-through or approved equivalent review protocol.
- [x] P11-04 Bind Gate 3 approval to the final preview, QA report, plan, asset manifest, composition code bundle, and delivery profile hashes.
- [x] P11-05 Render the master and selected platform derivatives.
- [x] P11-06 Generate caption sidecars, final transcript, chapter suggestions, description draft, checksums, and delivery manifest.
- [x] P11-07 Verify the source and final output backup targets by hash before cleanup becomes eligible.
- [x] P11-08 Implement a cleanup dry run with strict path boundaries and source exclusions.
- [x] P11-09 Add status, retry, cancellation, crash recovery, retention, quota, and operator acceptance tests.
- [x] P11-10 Document performance measurements without treating the supplied 45-minute figure as guaranteed.

Acceptance evidence:

- final rendering is blocked until Gate 3 and required QA pass
- cleanup is blocked until backup verification and explicit approval
- a retained project can reproduce the delivery
- `.codex/results/P11.json`

## U22: smart-dense editing, join repair, and structural transitions

These tasks are part of the canonical dependency graph. Do not reset completed `P#-##` or `U21-##` tasks.

- [x] U22-01 Add compatible cut-taxonomy, transition-plan, join-QA, sound-sync, approval, configuration, and example contracts.
- [x] U22-02 Implement the `smart_dense` candidate generator. Scan all transcript and silence evidence and emit all qualifying mechanical micro edits.
- [x] U22-03 Implement filler, filler-phrase, stutter, false-start, abandoned-phrase, self-correction, exact-repetition, near-repetition, semantic-repetition, duplicate-take, weak-take, dead-air, accidental-noise, and housekeeping analysis.
- [x] U22-04 Add take ranking using completeness, pronunciation, factual correctness, delivery, audio quality, gesture continuity, and screen-state continuity.
- [x] U22-05 Add type-specific confidence, protected-content checks, safe fallbacks, and policy compilation for automatic, reviewed, and blocked work.
- [x] U22-06 Implement a deterministic join strategy and repair stage for each applied cut.
- [x] U22-07 Implement rendered join QA with re-transcription, transcript comparison, audio checks, visual checks, pacing checks, and repair routing.
- [x] U22-08 Implement structural-boundary detection and a purpose-bound transition planner with clean-cut fallbacks.
- [x] U22-09 Implement Remotion transition primitives with smooth easing, stable full-frame coverage, and exact incoming-content timing.
- [x] U22-10 Implement licensed transition-sound selection, transient alignment, gain, fades, speech protection, reuse spacing, and mix QA.
- [x] U22-11 Add review batching that lets high-confidence micro edits proceed under the approved policy while asking only about material semantic or continuity uncertainty.
- [x] U22-12 Add cut-density, retained-fragment, cadence, transition-frequency, and repetition metrics as QA signals, not arbitrary cut-count blockers.
- [x] U22-13 Add fixtures for fillers, stutters, false starts, exact and semantic repetition, duplicate takes, clean and broken joins, screen-state jumps, good structural transitions, bad random transitions, and masked dialogue.
- [x] U22-14 Render and inspect the required fixtures, update traceability, and write `.codex/results/U22.json` with media evidence.

Acceptance evidence:

- the planner makes many useful small cuts on a dense talking-head fixture without deleting protected meaning
- every applied cut has a valid join strategy and rendered join result
- re-transcription catches a deliberately broken join and routes it to repair or review
- routine cleanup cuts receive no decorative motion transition
- a real new-point boundary receives a smooth transition and synchronized licensed sound
- a random or weak transition proposal falls back to a clean cut
- high cut density alone does not fail QA, while rushed pacing, damaged speech, flashing, or poor continuity does
- `.codex/results/U22.json`
