# MatAnyone 2 Worker Guide

This guide documents an optional deferred extension under ADR-0013. The final
workflow does not install or invoke MatAnyone 2; use chroma key or an approved
supplied/manual mask instead unless a new accepted re-enable decision exists.

MatAnyone 2 needs a video and first-frame segmentation mask. Use the SAM worker, an
interactive tool, or a manually prepared mask for initialization. The core
`videoedit.services.matting.build_matting_job` boundary accepts only a project-owned
mask and records its SHA-256, source, dimensions, frame-zero identity, and validation
diagnostics in the v1.1 job.

The mask must be one decoded lossless grayscale frame with white foreground polarity,
matching the input dimensions, no audio, and a non-empty mixed range. A mask that is
all black, all white, compressed/color, multi-frame, out of bounds, or undecodable is
rejected before a worker job is written. The `mask_approval` reference is supplied by
the operator; validation never creates or implies human approval. Matting runtime
access remains blocked until the separate licence, checkpoint, hardware, and worker
approval gates pass.

The legacy `1.0` job shape remains available for schema validation and `--dry-run`
migration checks. It is not a live inference contract: the worker rejects it before
importing MatAnyone 2, so an online `from_pretrained` download cannot occur. Live
inference requires the `1.1` contract, an approved runtime reference, and a local
checkpoint path and SHA-256 bound to that job.

After a worker result is produced, run `videoedit verify-matte` before composition.
The verifier preserves the raw worker result and writes a new result revision after
re-probing both output files, checking current hashes, role-specific pixel formats,
dimensions, frame rate, frame count, duration, full decode, and sampled alpha polarity.
It leaves contrasting-background review pending unless that evidence is already
recorded. `videoedit prepare-matte` re-runs the structural verification and refuses
stale or semantically inconsistent outputs.

For a partial frame-zero source range, the isolated adapter first creates an audio-free,
lossless bounded input through typed FFmpeg and atomically reuses it only when its
dimensions, frame count, and frame rate still match the job. After inference it rejects
foreground or alpha outputs whose frame count, dimensions, rate, duration, audio role,
or RGB/grayscale role does not match the approved range. These checks do not approve
matte semantics; the structural verifier and human contrasting-background review remain
required.

Create the separate review package with `videoedit review-matte-contrast
<matting-result.json>`. It renders black and white previews from the separate RGB
foreground and grayscale alpha streams, creates source/preview contact sheets, records
the exact command evidence and current hashes, and promotes the package atomically.
The resulting `matting-contrast-review.json` remains `pending`: distinct preview files
prove only that both render paths ran, while an operator must compare hair, fingers,
clothing, holes, transparent regions, motion blur, entry/exit edges, and temporal
stability on both backgrounds before updating the approval evidence.

Create the per-category quality report with
`videoedit review-matte-quality <matting-result.json> --contrast-review
<matting-contrast-review.json>`. It re-checks current hashes, fully decodes the alpha,
samples first/middle/last review frames, records numeric alpha evidence, and lists
pending checks for hair, fingers, clothing, holes, transparent regions, fast motion,
motion blur, entry/exit, and temporal edges. Numeric alpha evidence cannot approve
semantic identity or stability; the report remains pending until an operator records
those decisions, with the original/chroma-key path as the safe fallback.

## Test sequence

1. Run a short low-resolution range.
2. Identify output foreground and alpha files.
3. Composite over white, black, and saturated backgrounds.
4. Inspect boundaries and temporal stability.
5. Record accepted settings before full resolution.

## Failure handling

If the subject leaves and re-enters, if the first-frame mask is incomplete, or if edges become unstable, split the shot or use the controlled green-screen path. Preserve the original shot as fallback.
