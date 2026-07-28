# SAM 3.1 Worker Guide

This guide documents an optional deferred extension under ADR-0013. The final
workflow does not install or invoke SAM 3.1; use supplied/manual masks or
tracks instead unless a new accepted re-enable decision exists.

The worker follows the official predictor session lifecycle. A text prompt starts at a selected frame, then output propagates through the video. Results are normalized to PNG masks and JSON geometry.

## Prompt guidance

Use a concrete visible phrase such as `the blue foam ball in the speaker's right hand`. Avoid generic prompts when multiple matching objects exist. Select a prompt frame where the target is large, unoccluded, and visually distinct.

## Continuity checks

Measure missing frames, object count, centroid motion, bounding-box size, area, and abrupt changes. These signals indicate review targets. They do not decide identity alone.

## Retry rule

Refine the prompt or prompt frame before changing model thresholds. Preserve each attempt and never overwrite an approved mask set.

## Contract compatibility

The legacy `1.0` job shape remains available for schema validation and `--dry-run`
migration checks. It is not a live inference contract: the worker rejects it before
importing SAM or constructing a predictor. Live inference requires the `1.1` contract,
an approved runtime reference, and a local checkpoint path and SHA-256 bound to that job.

## Independent worker guards

Before a live predictor is constructed, the isolated adapter rechecks the v1.1 source
hash, canonical half-open frame range, and declared input frame count. It rejects
invalid input dimensions and refuses to write an empty mask, a mask with duplicate or
non-positive object IDs, or a mask whose dimensions differ from the input video. These
checks are independent of the core builder and keep a stale or malformed job from
being promoted even when the upstream process itself exits successfully.
