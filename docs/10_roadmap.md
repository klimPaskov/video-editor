# Roadmap

## Milestone 0: verified local foundation

Python, Node, FFmpeg, schemas, tests, process adapters, and dependency checks pass.

## Milestone 1: immutable ingest

A real source is hashed, probed, proxied, and preserved.

## Milestone 2: evidence and plan

Local transcript, silence evidence, edit proposals, effect plan, and Gate 1 review work.

## Milestone 3: base edit

Approved keep ranges render with synchronized audio, time mapping, and QA.

## Milestone 4: Remotion composition

Typed visual timelines render backgrounds, text, captions, B-roll, and picture-in-picture.

## Milestone 5: green-screen effects

The local demo and a real short clip support recolor from a mask, chroma key, background replacement, behind-subject text, and final captions without a GPU.

## Milestone 6: SAM 3.1 (optional deferred extension)

The isolated worker produces reviewed object masks and geometry on the target
GPU only after a future accepted re-enable decision. It is not required for
the final workflow.

## Milestone 7: MatAnyone 2 (optional deferred extension)

The isolated worker produces verified person foreground and alpha outputs for a
real short clip only after a future accepted re-enable decision. It is not
required for the final workflow.

## Milestone 8: tracked replacement

A licensed asset replaces a tracked object with smoothing, scale, fallback, and occlusion review.

## Milestone 9: reusable assets

A local B-roll and sound index supports context search, licence tracking, and usage history.

## Milestone 10: review and self-correction

Segment previews, fix markers, re-transcription, rerender, and locking work across revisions.

## Milestone 11: delivery operations

Final QA, full approval, chapters, sidecars, checksums, backup verification, resume, and safe cleanup work.

## Ordering rule

Do not start an optional GPU milestone until Milestone 5 passes and the
extension is explicitly re-enabled. The required path proceeds from Milestone
5 to tracked effects, review, and delivery using supplied/manual masks and
chroma key. Do not automate cleanup until Milestone 11 verification passes.
Optional paid generation starts only after the complete local workflow is
stable.
