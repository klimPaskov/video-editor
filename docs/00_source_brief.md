# Source Brief

## Goal

Build a local-first, Codex-operated editor that turns screen recordings into a
reviewed final video without manual timeline editing.

## Product interpretation

Codex plans and coordinates. FFmpeg/ffprobe perform deterministic media work,
local Whisper supplies word timing, and Remotion renders the visual timeline.
Every proposal, render, QA report, and approval is hash-bound to the current
project revision.

## Required capabilities

- immutable source ingest, probing, transcription, and silence evidence;
- dense but safe speech cleanup with explicit edit approval;
- synchronized base renders with preserved dialogue and approved prompt-action
  speed-ups;
- captions, layered text, picture-in-picture, backgrounds, and purposeful screen
  focus;
- local assets with hashes and usage records;
- segment previews, re-transcription, QA, revisions, delivery metadata, backup
  verification, and safe cleanup.

## Operator decisions

The operator approves semantic edits, effects, assets, spend, previews, final
delivery, backup targets, and cleanup. Silence or a model proposal never counts
as approval.
