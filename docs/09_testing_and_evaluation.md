# Testing and evaluation

VideoEdit uses software tests, decoded-media checks, and human visual review.
A valid JSON file or successful process is not proof that an edit is good.

## Test tiers

### Tier 1: core behavior

Run Ruff, mypy, pytest, example validation, shell syntax checks, and Remotion
type checking. Cover time arithmetic, schemas, hashes, stage resumption,
approval staleness, process failures, transcript mapping, caption grouping,
planning, and delivery metadata.

### Tier 2: media behavior

Use short local fixtures to verify:

- immutable ingest and source hashes;
- ffprobe stream identity, dimensions, frame rate, duration, and audio;
- cut joins and prompt-action retiming;
- audible speed-up audio with original pitch;
- captions, text, graphics, B-roll, picture-in-picture, and purposeful zooms;
- full decode, stream mapping, A/V sync, clipping, and output metadata.

### Tier 3: human review

Inspect stills, contact sheets, segment previews, and a complete candidate.
Review speech meaning, missing or duplicated words, clipped syllables, clicks,
room-tone changes, cadence, black flashes, frozen frames, cursor continuity,
screen-state continuity, caption readability, safe areas, zoom target,
boundaries, easing, and unrelated-content movement.

## Fixture requirements

Every media change gets the smallest useful decoded fixture. A fixture should
contain visible UI changes, at least one prompt-writing action, ordinary
browsing or waiting, speech, a caption opportunity, and a clean end. Record the
fixture hash, probe result, decode result, and review notes.

## Cut and pacing review

Inspect every transcript span, silence interval, and rendered join. Mechanical
removals may include obvious fillers, stutters, false starts, exact repeats,
dead air, and excess silence when the join is natural. Preserve meaning,
commands, names, numbers, negation, qualifications, warnings, useful breaths,
emotion, and uncertain words.

Only an explicitly requested visible prompt-writing or dictation action may be
sped up. The action must be visible for the full range, retain audible sound,
keep the original pitch, and start and end on the actual text-change action.
Browsing, reading, waiting, loading, and result inspection stay at normal
speed.

## Zoom review

Do not zoom the intro. A zoom needs an approved purpose and a visible target:
an opened window, prompt box, relevant cursor action, or important UI. Start
after the target appears, end before unrelated content begins, center the target,
use smooth ease-in/ease-out, and reject snapping, jitter, edge gaps, drift, or
whole-screen movement without a target. If evidence is insufficient, omit the
zoom.

## Final validation

The final candidate must pass strict FFmpeg decode and stream validation,
duration and frame-rate checks, audio sample-rate/channel checks, caption and
safe-area checks, approval-hash checks, and delivery-manifest checks. Preserve
the source and valid revisions. Failed evidence remains available for diagnosis.
