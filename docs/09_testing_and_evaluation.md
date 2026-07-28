# Testing and Evaluation

## Purpose

The system needs ordinary software tests, media tests, model-worker tests, and human visual review. A valid JSON file can describe a bad edit. A successful process can produce a broken mask. A decodable video can still contain drift, unreadable captions, unstable edges, or misleading B-roll.

## Test tiers

### Tier 1: deterministic core

Runs in continuous integration without media credentials, GPU checkpoints, or network access.

Covers:

- pure timeline and frame mapping
- schemas and examples
- process and worker adapters with fake commands
- project and stage state
- approval validity
- budget rules
- source immutability
- short synthetic FFmpeg fixtures
- Remotion type checking and low-cost fixture renders when dependencies are installed

### Tier 2: local media suite

Runs on a supported workstation with FFmpeg, Node, Remotion, fonts, and local Whisper.

Covers:

- real local transcription
- edit and audio rendering
- caption proofs
- chroma key
- supplied-mask recoloring
- subject layering
- segment previews
- re-transcription and final QA

### Tier 3: target-GPU worker suite (optional deferred extensions)

Runs manually on approved hardware only after a new accepted re-enable decision,
licence and checkpoint gates. It is not required by final workflow acceptance.

Covers:

- SAM 3.1 live segmentation and tracking
- MatAnyone 2 live person matting
- mask and matte stability
- memory and runtime limits
- pinned code and checkpoint compatibility

### Tier 4: optional provider suite

Runs manually with a hard test budget and approved nonconfidential fixtures.

Covers:

- estimate and approval enforcement
- idempotent submit and resume
- download validation
- cost reconciliation
- provider retention and provenance records

## Unit tests

Cover pure behavior:

- integer microsecond ranges
- rational frame conversion
- first-frame and final-frame rounding
- output duration calculation
- source-to-output mapping
- handle application
- cut merging and rejection
- caption segmentation
- z-order sorting
- transform interpolation
- track smoothing
- mask polarity and dimension validation
- budget arithmetic with decimal values
- approval staleness
- stage transitions
- dependency invalidation
- provider error mapping

Use property-based tests for range operations, time conversion, and timeline invariants.

## Contract tests

Cover:

- JSON Schema and Pydantic agreement
- JSON Schema and TypeScript timeline agreement
- every example in `examples/index.json`
- unknown-field rejection where contracts are closed
- backward compatibility for supported schema versions
- worker request construction and result parsing
- stable CLI JSON envelopes
- phase result validation

## Process adapter tests

Use fake executables or helper scripts to test:

- success
- nonzero exit
- timeout
- cancellation
- process-group termination
- large output truncation
- secret redaction
- missing binary
- invalid working directory
- malformed result JSON
- result written after process exit
- atomic output promotion

## Media fixture matrix

Use short licensed or synthetic fixtures. Include:

- constant frame rate
- variable frame rate metadata
- 23.976, 25, 29.97, 30, and 60 fps
- mono and stereo audio
- unusual sample rate
- no audio
- leading and trailing silence
- internal long pause
- false start and duplicate phrase
- fast speech
- quiet speech
- background music
- clipped audio
- corrupt tail
- rotation metadata
- non-square pixels when supported
- separate camera and microphone tracks
- picture-in-picture screen recording
- green screen with uneven lighting
- green spill and motion blur
- a supplied object mask
- a supplied person alpha matte

## End-to-end fixtures

### Core editorial fixture

```text
init -> ingest -> transcribe fake or local -> silence -> plan
-> Gate 1 fixture decisions -> base render -> audio normalize
-> transcript rebase -> QA
```

### Local visual fixture

```text
approved base edit -> Remotion background and middle plate
-> chroma-key or supplied-mask subject -> mask-driven recolor
-> front captions and labels -> segment preview -> QA
```

### Review fixture

```text
segment preview -> [FIX] duplicate phrase -> new revision
-> rerender -> re-transcribe -> speech comparison -> Gate 2
```

### Delivery fixture

```text
approved segments -> final assembly -> final loudness -> final QA
-> Gate 3 fixture approval -> master -> sidecars -> checksums
-> backup verification -> cleanup dry run
```

GPU workers and paid providers use fakes in ordinary end-to-end tests.

## Golden artifacts

Golden tests are useful for:

- edit proposals
- effect plans
- edit decision lists
- source-to-output maps
- visual timelines
- caption plans
- ASS files
- FFmpeg argument arrays
- worker jobs and normalized results
- QA reports
- readable review Markdown

Do not compare compressed video files byte for byte across platforms. Compare decoded properties, selected frame hashes under controlled settings, alpha and mask statistics, perceptual metrics where stable, and timing.

## Media assertions

### Decode

Decode the complete output to a null sink. A successful probe is insufficient.

### Duration and frame count

Compare expected timeline duration and frame count with decoded or probed values. Default tolerance is one output frame plus a small justified container allowance for audio.

### A/V synchronization

Use a synthetic flash and audio-click fixture. Enforce the delivery-profile threshold. For ordinary outputs, also inspect stream starts, join boundaries, and end drift.

### Loudness

Check integrated loudness, loudness range, and true peak against the delivery profile. Record the measurement command and tolerances.

### Captions

Check:

- nonnegative timing
- events within output duration
- monotonic events
- no unapproved overlap
- minimum display duration
- reading-speed warnings
- maximum lines
- safe-area bounds
- font availability
- missing glyphs
- foreground collisions
- drift against re-transcribed speech

### Visual composition

Check:

- declared z-order
- layer bounds
- source range and frame range alignment
- missing staged assets
- placeholder text
- font fallback
- safe-area collisions
- target dimensions and pixel format
- unexpected black frames
- suspicious freeze duration
- picture-in-picture hiding screen content
- subject framing and head position where relevant

### Chroma key

Check representative frames for:

- missing subject pixels
- retained green background
- green spill
- edge chatter
- holes in clothes or props
- hair and finger damage
- motion-blur damage
- alpha channel presence and polarity

### Object masks

Check per frame:

- dimensions and frame index
- nonempty area when the object should be present
- empty area when absence is expected
- centroid jump
- area ratio jump
- border leakage
- mask fragmentation
- identity continuity
- entry, exit, and occlusion behavior

### Person mattes

Check:

- foreground and alpha roles
- alpha range and dimensions
- temporal edge movement
- hair, fingers, clothing, holes, and transparent regions
- foreground color contamination
- motion blur
- contrasting-background previews

### Object replacement

Check:

- position, scale, rotation, and visibility
- smoothing lag
- source object leakage
- occluder z-order
- hand and finger overlap
- entry and exit fades
- fallback when tracking confidence drops

## Focus and pacing evaluation

### Purposeful zoom fixtures

Include at least these cases:

- an opened window with stable bounds
- a visible prompt box that appears after the shot begins
- a relevant cursor action over important UI
- no clear target, which must produce `no_zoom`
- a target that disappears before the proposed end
- a target near an edge that would expose blank pixels at the requested scale
- a moving target that would reveal jitter without smoothing

Assert the allowed purpose, evidence frames, target-visible range, target centering, easing, scale bounds, edge coverage, overlay clearance, stability, and end before unrelated content. Inspect first, transition, hold, return, and final frames.

### Prompt speed-up fixtures

Include at least these cases:

- explicitly requested visible prompt typing
- explicitly requested visible prompt dictation
- prompt typing interrupted by browsing
- prompt typing followed by reading or result inspection
- waiting or page loading inside a broad candidate range
- no explicit request, which must produce `normal_speed`

Assert that the accelerated range begins on the first action frame and ends on the last action frame. Verify that browsing, reading, waiting, loading, navigation, result inspection, cursor wandering, and unrelated actions remain at normal speed. Decode the output, measure expected duration, verify audible production audio, check pitch policy and A/V synchronization, re-transcribe the rendered range, and verify every later cue after time rebasing.

### Confidence and review fixtures

Test high-confidence automatic eligibility, medium-confidence batched review, low-confidence safe fallback, stale Gate 1 approval, and the configured question cap. A confidence score alone cannot pass a fixture when required evidence or QA is missing.

## Transcript and edit evaluation set

Build an internal reviewed set with:

- clean single-speaker speech
- noisy room
- multiple accents
- intentional pauses
- false starts
- corrections with negation
- repeated ideas that should remain
- repeated words that can be removed
- numbers and names
- disclosures
- quick pacing
- code and technical terms
- calls to action
- screen narration where the visible action constrains cuts

Each item should record acceptable cut ranges, protected content, and preferred effect opportunities.

## Proposal metrics

Track:

- acceptable-proposal precision
- recall of known removable regions
- modification rate
- meaning-risk failures
- average boundary error
- continuity defect rate
- review time
- total deleted percentage

Precision matters more than recall in conservative mode.

## Visual-effect evaluation

Human reviewers score:

- intent match
- object identity
- edge quality
- temporal stability
- occlusion correctness
- texture and lighting preservation
- visual readability
- style consistency
- factual safety
- need for manual correction

A successful render is not a passing visual evaluation.

## B-roll and sound evaluation

Human reviewers score:

- relevance to narration
- provenance
- factual safety
- style consistency
- placement value
- repetition
- branding conflicts
- speech interference
- whether the cue should be removed

For paid media, track cost per accepted second and cost per accepted asset.

## Failure injection

Inject:

- FFmpeg termination during render
- Remotion process termination
- disk-full behavior
- stale project lock
- transcript timeout
- malformed planner JSON
- stale Gate 1, Gate 2, or Gate 3 approval
- missing font
- missing staged asset
- mask frame-count mismatch
- mask polarity error
- SAM result with missing frames
- SAM identity switch
- MatAnyone result with unknown output semantics
- corrupt alpha video
- provider rate limit
- provider submit succeeds but local response is lost
- provider download corruption
- QA failure after preview
- backup mismatch
- cleanup path escape

Verify state, cleanup, retry behavior, and operator messages.

## Performance tests

Benchmark representative durations and resolutions:

- 2 minutes at 1080p
- 15 minutes at 1080p
- 60 minutes at 4K when in scope
- one 5-second GPU object track
- one 10-second GPU person matte

Track:

- ingest throughput
- transcription real-time factor
- base-render real-time factor
- Remotion render real-time factor
- GPU worker real-time factor
- peak CPU memory
- peak GPU memory
- temporary disk
- cache hit rate
- stage resume savings
- operator review time

Do not set the reported 45-minute workflow as a release criterion before measuring the target hardware, source length, effect count, and review standard.

## Continuous integration gates

Required on every change:

- Python format check
- Python lint
- strict Python type check
- unit tests
- contract tests
- schema example validation
- shell syntax checks
- TypeScript type check when dependencies are available
- short synthetic media integration tests
- coverage threshold for domain and application code

Nightly or manual:

- complete local Whisper fixture
- full media matrix
- long-file tests
- Remotion fixture render on supported operating systems
- target-GPU SAM 3.1 smoke test, only if the optional extension is re-enabled
- target-GPU MatAnyone 2 smoke test, only if the optional extension is re-enabled
- bounded live provider test when a provider is enabled

## Release blockers

A release cannot ship when:

- a required schema example fails
- source immutability tests fail
- time or timeline property tests fail
- A/V drift exceeds threshold on the sync fixture
- a semantic cut or visual effect bypasses approval
- a paid provider can be reached without request-bound approval
- retry can duplicate a paid submission
- a worker can produce a complete status without validated outputs
- final output is not fully decoded
- required provenance is missing
- cleanup can reach source media
- critical QA findings can be ignored without a recorded override

## Human acceptance session

A nontechnical operator should be able to:

1. Run `videoedit doctor` and understand missing dependencies.
2. Create a project and ingest a fixture.
3. Read and decide edit and effect proposals.
4. Approve Gate 1.
5. Render and review the local zero-cost visual path.
6. Add a timecoded fix marker and receive a new verified revision.
7. Approve Gate 2.
8. Review final QA and approve Gate 3.
9. Export the final master, captions, transcript, chapters, manifest, and checksums.
10. Verify backup and inspect a cleanup dry run.

## Version 2.2 evaluation set

Add fixtures with isolated fillers, adjacent fillers, useful breaths, stutters, false starts, self-corrections, exact repetition, semantic repetition with unique detail, duplicate takes, weak takes, long pauses, accidental noise, clipped phonemes, room-tone jumps, black flashes, face jumps, and screen-state jumps.

Measure candidate recall by type, false deletion rate, protected-content violations, join repair rate, missing and duplicate word rate after re-transcription, speech-rate change, retained-fragment length, operator questions per minute, and acceptance rate by confidence band.

Add positive and negative transition fixtures. A true new-point boundary with a licensed swoosh should pass. Routine filler cleanup with a swipe, a random direction, a transition that masks incoming speech, and repeated identical swooshes should fail or fall back.
