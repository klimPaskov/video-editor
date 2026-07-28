# Source Brief

## Goal

Build a Codex-operated video editor that turns raw talking-head, green-screen, and screen-recording footage into a reviewed final video without manual timeline editing.

## Supplied source patterns

### Transcript-led automatic editing

The first supplied transcript describes automatic cuts, dead-space removal, branded captions, sound effects, motion graphics, optional generated B-roll, and cost concerns.

### Skill-based editor with review gates

The second supplied transcript describes a wrapper editing skill that calls specialized skills. It plans before rendering, accepts review notes in transcript Markdown, renders and reviews logical segments, re-transcribes edited outputs, then assembles and packages the final video.

### Codex with Remotion and vision tools

The user-supplied implementation context describes Codex as a tool harness, Remotion as the programmatic React video base, SAM 3.1 for object masks and tracking, MatAnyone for person matting, FFmpeg for media composition, and transcript timing for effect triggers.

The demonstrated target effects include:

- tracking a ball held in a hand
- changing the ball colour
- replacing the ball with an apple
- separating the person from the background
- inserting new backgrounds
- placing text between the person and background

## Product interpretation

Codex is the orchestrator. It reads the creative brief and project state, forms a typed proposal, calls deterministic tools or isolated workers, inspects diagnostics and previews, revises, and presents a controlled review gate.

Codex is not the source of truth for media timing, source identity, approvals, licences, spend, or cleanup eligibility.

## Required capabilities

- immutable source ingest and media probing
- word-timed transcript and independent silence evidence
- reviewable cut, B-roll, motion, sound, and visual effect proposals
- stable transcript word IDs for effect triggers
- explicit plan approval
- synchronized base edit and audio treatment
- branded captions
- Remotion visual composition
- green-screen cutout and background replacement
- local mask-driven object recoloring
- optional SAM 3.1 object segmentation and tracking
- optional MatAnyone 2 person matting
- object replacement and occlusion handling
- text between the background and subject
- segment review with timestamped fix markers
- re-transcription and self-verification
- final QA, approval, delivery metadata, backup, and safe cleanup

## Deliberate changes from the source examples

- Remotion is the primary visual engine. HyperFrames remains an optional future adapter.
- Local Whisper is the default transcript engine. ElevenLabs remains optional.
- A local asset index replaces private applications such as Tubery.
- Green-screen chroma key is preferred over neural matting when the shoot can be controlled.
- SAM 3.1 and MatAnyone 2 run in isolated environments because their runtime requirements differ from the core.
- Generated B-roll remains optional and disabled by default.
- Three approvals bind to exact current hashes.
- The reported 45-minute edit time is a later benchmark target, not a promise.

## Human decisions

The operator must decide or approve:

- edit and effect plan
- object and person identity when uncertain
- selected assets and licences
- checkpoint and model licence acceptance
- paid provider requests and budgets
- segment previews
- final candidate and QA report
- backup target and destructive cleanup
