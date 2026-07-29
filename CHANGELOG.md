# Planning Package Changelog

## 2.2.1 - 2026-07-23

- Merged smart-dense editing, join repair, transition, sound, confidence, and review rules into canonical root instructions.
- Added the `U22-*` dependency graph directly to `TASKS.md`.
- Added the canonical cut and structural-transition contract directly to `WORKFLOW.md`.
- Made the seven root planning files deliberate overwrite targets in the drop-in overlay.
- Preserved active implementation code and added backup-first replacement behavior for canonical root files.

## Version 2.1.0, July 23, 2026

This release adds a bounded focus and pacing layer without replacing the existing edit pipeline.

### Purposeful zooms

- Adds a dedicated focus and pacing plan, schema, example, project skill, phase prompt, QA checklist, and accepted ADR.
- Permits zooms only for opened windows, prompt boxes, relevant cursor actions, or important visible UI.
- Requires a visible target, target-centered framing, exact relevance boundaries, smooth ease-in and ease-out motion, stabilized transforms, and edge checks.
- Uses `no_zoom` when there is no clear target or confidence is insufficient.
- Adds QA for target visibility, centering, jitter, snapping, drift, empty edges, stale tracking, and unrelated whole-screen motion.

### Requested prompt speed-ups

- Adds speed-ups only when the operator explicitly requests them in the project brief, Gate 1 review, or a revision marker.
- Restricts accelerated ranges to visible prompt-writing or prompt-dictation actions.
- Excludes browsing, reading, waiting, loading, navigation, result inspection, cursor wandering, and other actions.
- Requires exact action boundaries, synchronized picture and production audio, audible sound by default, and pitch preservation by default.
- Adds a canonical retimed timeline so transcripts, captions, effects, B-roll, sound, zooms, and review timecodes remain aligned.

### Confidence, review, and compatibility

- Adds type-specific confidence and QA evidence for focus and pacing decisions.
- Batches material uncertainty into short reviews with recommendations and safe fallbacks.
- Keeps old schema examples valid by making additions backward-compatible.
- Adds a ten-item implementation addendum and a continuation prompt for an agent already working on the previous package.
- Adds a planning-only drop-in overlay path that does not replace `src/`, `tests/`, `remotion/src/`, or worker implementation code.
- Keeps `GOAL_PROMPT.md` below the 4,000-character limit.

## Version 2.0.1, July 23, 2026

- Reduced `GOAL_PROMPT.md` to 3,877 characters so it remains below the 4,000-character limit.
- Preserved the product goal, local-first milestone, worker boundaries, approval rules, quality gates, and phase execution contract.
- Refreshed package integrity files and validation evidence.

## Version 2.0, July 23, 2026

This version replaces the earlier provider-heavy design with the expanded workflow derived from all supplied sources.

### Main changes

- Remotion is the primary visual compositor.
- FFmpeg remains the deterministic media engine.
- Transcript word identifiers trigger reviewed effects.
- The public workflow is scoped to screen recordings.
- SAM 3.1 is an optional isolated object segmentation and tracking worker.
- MatAnyone 2 is an optional isolated person matting worker.
- Object recoloring, object replacement, explicit occlusion, background replacement, and text behind the subject are first-class planned effects.
- The workflow has Gate 1 plan approval, Gate 2 segment approval, and Gate 3 final approval.
- Segment previews, fix markers, re-transcription, and self-verification are part of the core plan.
- Asset provenance, backup verification, and cleanup approval have explicit contracts.
- The repository contains 12 implementation phases, phase prompts, project skills, schemas, examples, checklists, and a starter code structure.

### Scope correction

The package is an implementation plan and repository baseline. It does not claim that production SAM 3.1 tracking, MatAnyone 2 matting, final visual quality, or real-project operator acceptance is complete.

## 2.2.0

- Added smart-dense micro editing, complete cut taxonomy, take ranking, join repair, and rendered join QA.
- Added purposeful structural transitions with synchronized licensed sound and clean-cut fallbacks.
- Added version 2.2 tasks, skills, prompts, schemas, examples, configuration, checklists, and active-agent continuation guidance.
