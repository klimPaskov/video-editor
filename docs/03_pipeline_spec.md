# Pipeline specification

The implementation pipeline follows the phases in `TASKS.md` and supports
screen recordings only.

## P0: foundation

Check Python, Node.js, FFmpeg, ffprobe, fonts, schemas, and the process
adapter. Record versions and create a decoded fixture.

## P1: project and ingest

Create a revision, copy the source into the immutable project area, calculate
SHA-256, probe streams, and write resumable stage state.

Outputs include `project-manifest.json`, `source-manifest.json`,
`media-probe.json`, and the ingest report.

## P2: transcript and evidence

Run local Whisper with word timing and independent FFmpeg silence detection.
Normalize source timing to integer microseconds and preserve raw adapter output.

Outputs include `transcript.json`, `transcript.md`,
`silence-intervals.json`, and adapter metadata.

## P3: planning and Gate 1

Create proposals for mechanical speech cleanup, captions, B-roll, motion,
sound, picture-in-picture, UI focus, and explicitly requested prompt-action
speed-ups. Each proposal records ranges, evidence, reason, risk, fallback, and
renderer. Gate 1 binds the current source, transcript, edit plan, focus plan,
effect plan, and asset hashes.

## P4: base edit and audio

Compile approved keep ranges and prompt-action speed-ups into one retimed
timeline. FFmpeg renders picture and production audio from the same ranges,
keeps speed-up audio audible, preserves voice pitch, and validates joins,
duration, frame count, clipping, and A/V sync.

## P5: Remotion and captions

Build the schema-valid visual timeline. Stage local assets by hash and compile
approved target-centered UI zooms into integer-frame keyframes with smooth
easing. Render stills and short previews before the full candidate.

## P6: visual composition and focused pacing

Render the original recording with background plates, text, captions, local
motion graphics, B-roll, picture-in-picture, and verified purposeful focus.
Apply speed-ups only to the explicitly requested visible prompt-writing action.
Inspect the decoded fixture for joins, UI continuity, captions, audio, safe
areas, and motion quality.

## P9: assets, B-roll, picture-in-picture, and sound

Index local assets with hashes and usage metadata. Plan approved B-roll,
graphics, picture-in-picture, and sound cues. Mix with speech priority and keep
network providers disabled without bounded approval.

## P10: review, re-transcription, and Gate 2

Render bounded segment previews with transcript excerpts, effect summaries, and
QA findings. Import timestamped fixes as a new revision, re-transcribe edited
segments, and compare intended and rendered speech. Gate 2 binds to the current
preview, transcript comparison, assets, compositor bundle, and QA hashes.

## P11: final QA, Gate 3, delivery, and cleanup

Assemble approved revisions, render the final candidate, run media and visual
QA, and record the required watch-through. Gate 3 binds to the final preview,
QA report, plans, asset manifest, compositor bundle, and delivery profile.
Write the final MP4, caption sidecars, checksums, delivery metadata, backup
verification report, and cleanup dry run. Cleanup requires separate approval.

## Smart-dense editing and transitions

Inspect transcript words, silences, joins, and structural boundaries. Use
precise mechanical edits and clean cuts for routine cleanup. Use a motion
transition only at a verified chapter, mode, comparison, or major demonstration
boundary, with smooth coverage, dialogue clearance, and a matching sound cue
when appropriate.
