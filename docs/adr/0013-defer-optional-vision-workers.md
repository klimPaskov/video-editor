# ADR-0013: Defer Optional SAM and MatAnyone Workers from the Final Workflow

Status: accepted

Date: 2026-07-26

Decision owner: project operator (explicit thread decision)

## Context

The repository contains isolated, versioned contracts for SAM 3.1 object
tracking and MatAnyone 2 person matting. Their live acceptance still requires
model-specific licence decisions, checkpoint access, and a supported runtime.
The current machine exposes an AMD GPU and FFmpeg AMF encoding, but no accepted
SAM or MatAnyone inference path has been established. The required local
milestone already proves the useful editing path with chroma key and supplied
local masks.

## Decision

SAM 3.1 and MatAnyone 2 are deferred optional extensions and are not part of
the final working workflow. The required workflow is:

`P0-P6 -> U21/U22 -> P9-P11`

The final path uses controlled green-screen chroma key and approved
project-owned supplied or manual masks where a mask is needed. It must not
invoke, install, or require either isolated worker. `videoedit doctor` may
report worker readiness warnings, but those warnings do not fail the final
workflow. P7/P8 contract tests and worker code remain available as a future
extension boundary; their live checkpoint, licence, runtime, and target-GPU
acceptance tasks remain explicitly unchecked.

## Consequences

- The final workflow remains credential-free and usable on the available AMD
  machine for the local editing, compositing, effect, review, and delivery
  stages.
- P9 object effects must accept reviewed supplied masks or tracks and retain a
  source-shot fallback; it cannot assume a SAM result.
- P6 remains the approved subject-separation path for controlled green-screen
  footage and for the local milestone.
- Missing worker environments are an optional capability warning, not a
  blocker for the required workflow.
- P7/P8 results may be `partial` with live criteria `not_run`; this is a
  deliberate scope boundary, not live-worker acceptance.

## Re-enable conditions

Re-enable either worker only through a new accepted decision that names the
upstream revision, checkpoint identity and SHA-256, licence treatment, worker
runtime approval, compatible device/runtime, bounded frame-range policy, and
human-reviewed output evidence. Re-enabling a worker must not silently change
the required fallback path or approve its own outputs.
