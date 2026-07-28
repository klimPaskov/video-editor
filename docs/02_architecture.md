# Architecture

ADR-0013 makes the SAM 3.1 and MatAnyone 2 branches optional deferred
extensions. The required final workflow uses the core, FFmpeg, local Whisper,
Remotion, chroma key, and approved supplied/manual masks or tracks without
starting either worker.

## System shape

```text
Codex session
  |
  | reads plans, schemas, skills, review notes, and stage state
  v
Python 3.11 core orchestrator
  |-- FFmpeg and ffprobe adapter
  |-- local Whisper adapter
  |-- Remotion adapter -> Node.js 22 compositor
  |-- asset index and retrieval service
  |-- optional SAM job adapter -> isolated Python 3.12 CUDA worker
  |-- optional MatAnyone job adapter -> isolated Python 3.10 GPU worker
  |-- review, approval, QA, state, delivery, backup, and cleanup services
  v
Versioned artifacts and rendered media
```

## Core boundaries

### Domain

The domain contains media identity, media time, source and output ranges, transcripts, stable word IDs, plans, approvals, assets, visual timelines, worker jobs, QA findings, and delivery records.

The domain has no FFmpeg, React, CUDA, provider SDK, or model imports.

### Services

Services implement ingest, transcript normalization, edit and focus planning, review import, variable-rate timeline mapping, base rendering, asset selection, Remotion staging, optional worker orchestration, segment review, re-transcription, QA, delivery, backup, and cleanup planning.

### Adapters

Adapters invoke external processes, local models, workers, and optional providers. They normalize output and preserve raw logs. Adapter-specific types do not leak into domain contracts.

## Environment separation

| Component | Runtime | Reason |
|---|---|---|
| Core | Python 3.11 | Stable orchestration and media contracts |
| Remotion | Node.js 22 | React visual rendering and TypeScript checks |
| SAM 3.1 optional extension | Python 3.12 with a compatible CUDA stack | Current upstream requirement when re-enabled |
| MatAnyone 2 optional extension | Python 3.10 with a compatible GPU stack | Current upstream environment when re-enabled |

Each worker accepts a JSON job path and writes a JSON result. The core never imports worker packages.

## Media responsibility matrix

| Responsibility | Core | FFmpeg | Remotion | SAM 3.1 | MatAnyone 2 |
|---|---:|---:|---:|---:|---:|
| Source identity and hashes | Yes | No | No | No | No |
| Probe streams and timing | Coordinate | Yes | No | No | No |
| Transcript and word IDs | Yes | Audio extract | No | No | No |
| Approved base cuts | Coordinate | Yes | No | No | No |
| Requested prompt speed-ups | Plan and map | Exact A/V retiming | Preview only | No | No |
| Audio normalization | Coordinate | Yes | No | No | No |
| Captions and visual layers | Props and policy | Optional burn-in | Yes | No | No |
| Purposeful UI zooms | Plan and validate | No | Target-centered render | No | No |
| Green-screen key | Coordinate | Yes | No | No | No |
| Object masks and tracks | Validate | Consume | Consume geometry | Produce | No |
| Person foreground and alpha | Validate | Consume | Compose around subject | Initial mask optional | Produce |
| Final mux and media QA | Coordinate | Yes | Render visual pass | No | No |

## Time model

### Canonical time

Source and output media time uses integer microseconds and half-open ranges.

Decimal seconds reported by ffprobe or a transcription provider are parsed with exact decimal
arithmetic and explicit ``ROUND_HALF_EVEN`` conversion to microseconds before nonnegative
normalization. Binary floating-point conversion is not used for canonical media time.

### Frame rate

Frame rate uses a rational numerator and denominator. Do not flatten 30000/1001 to a floating value for canonical calculations.

### Remotion frame conversion

```text
frame = round(time_us * fps_numerator / (1_000_000 * fps_denominator))
time_us = round(frame * 1_000_000 * fps_denominator / fps_numerator)
```

The selected rounding mode is explicit and tested. Cut plans remain in source microseconds. Visual timelines remain in output frames. Mapping artifacts connect them.

### Worker alignment

Worker jobs state the exact source hash or base edit hash, frame rate, start frame, and end frame. Results that refer to a different timeline are rejected.

## Data flow

```text
source media
  -> source manifest and media probe
  -> speech and edit proxies
  -> transcript and silence evidence
  -> edit, effect, and focus and pacing proposals
  -> Gate 1 decisions
  -> approved keep ranges and source-to-output map
  -> approved prompt speed-ups and retimed timeline
  -> base edit and normalized audio
  -> captions, purposeful zooms, and visual timeline
  -> chroma key or approved supplied/manual masks and tracks
  -> optional SAM or MatAnyone outputs only when a future extension is re-enabled
  -> Remotion and FFmpeg segment renders
  -> Gate 2 fixes and re-transcription
  -> approved segment revisions
  -> final assembly and QA
  -> Gate 3 approval
  -> delivery, backup verification, and cleanup plan
```

## Focus and pacing boundary

Purposeful zooms and requested prompt speed-ups use separate execution paths. A zoom changes only visual framing and is compiled into Remotion keyframes after source time maps to output time. A speed-up changes output duration and must be compiled into the canonical retimed timeline before transcript, caption, effect, B-roll, sound, zoom, and review timestamps are rebased.

The focus and pacing plan is hash-bound at Gate 1. Low-confidence zooms use `no_zoom`. Low-confidence speed-ups use `normal_speed`. A safe fallback should not interrupt the operator unless it would violate an explicit request or materially affect meaning, continuity, audio, or final quality.

## Layering model

### Text behind a subject

1. Remotion renders background and middle text plate.
2. FFmpeg overlays a chroma-key or matted subject foreground.
3. Remotion renders front labels and captions.

### Object replacement

1. A reviewed supplied/manual track produces masks and geometry; an optional SAM extension may provide them only after re-enable.
2. Remotion positions the replacement asset from reviewed geometry.
3. FFmpeg or Remotion applies explicit occlusion layers.
4. The original shot remains a declared fallback.

## State and resumability

Every stage key includes input hashes, configuration hash, implementation version, contract version, and model identity where relevant. Stages write to temporary directories, validate outputs, then promote atomically. A failed stage does not replace a prior valid artifact.

## Code bundle identity

Visual approvals bind to a deterministic hash of the relevant Remotion source, package lockfile, timeline props, local asset hashes, and render configuration. A code change can alter frames without changing media assets, so it must invalidate the relevant approval.

## Trust boundary

Codex may create proposals, code, effect parameters, review summaries, and test fixtures. It cannot:

- approve its own proposal
- accept a licence
- authorize paid work
- approve uncertain identity
- waive required QA
- approve final delivery
- delete source media
- approve destructive cleanup
