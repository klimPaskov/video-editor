# Visual Effect Recipes

## Purposeful screen zoom

1. Confirm the target is an opened window, prompt box, relevant cursor action, or important visible UI.
2. Record the target-visible range and target bounding boxes.
3. Start after the target appears and finish before unrelated content begins.
4. Compute the smallest target-centered scale that preserves useful context.
5. Generate stabilized keyframes with smooth ease in and ease out.
6. Render start, peak, hold, and exit proof frames.
7. Reject jitter, snap, drift, empty edges, hidden overlays, or an unclear target.
8. Use no zoom when confidence fails.

## Requested prompt speed-up

1. Confirm an explicit request exists.
2. Identify the first and last frames of visible prompt writing or prompt dictation.
3. Split out browsing, reading, waiting, navigation, result inspection, and other actions.
4. Compile the range into the retimed timeline.
5. Apply matching video timing and audible pitch-adjusted audio timing.
6. Rebase transcript, captions, zooms, and later effects through the new mapping.
7. Re-transcribe and check audio presence, synchronization, and exact boundaries.
8. Use normal speed when confidence fails.


## Recolor a tracked object

1. Generate and approve an object mask sequence.
2. Build a color-transformed copy of the source.
3. Merge transformed and original pixels through the mask.
4. Inspect edge spill and motion blur.

## Replace an object

1. Use mask geometry for anchor, scale, and position.
2. Smooth geometry within explicit limits.
3. Place a licensed replacement asset in Remotion.
4. Use front and rear masks when occlusion matters.
5. Fade to the original when confidence fails.

The Phase 9-01 implementation makes the review boundary explicit. A structural
`segmentation_validation` report is not an approval. An operator must write an
`object_track_review` bound to the current result and validation hashes, inspect
the selected frames, and mark identity, continuity, geometry, and occlusion as
passing before keyframes are compiled. The compiler uses a centered moving
average inside visible runs, emits explicit opacity, unit scale, and zero
rotation keyframes, and records `original_shot/keep_original` as the fallback.
The zero rotation is deliberate because the segmentation contract does not
provide object orientation; it must not be inferred from motion direction.

```text
videoedit review-track RESULT --segmentation-validation VALIDATION \
  --actor REVIEWER --decision approved \
  --identity pass --continuity pass --geometry pass --occlusion pass
videoedit compile-track-keyframes RESULT \
  --segmentation-validation VALIDATION --track-review REVIEW \
  --width 1920 --height 1080
videoedit track-overlay TIMELINE SEGMENTATION_RESULT ASSET OUTPUT \
  --segmentation-validation VALIDATION --track-review REVIEW
```

Do not create an approved review from a model result or consume a stale review;
changed result, validation, range, object identity, or unresolved findings keep
the original-shot fallback active.

P9-02 binds a replacement asset separately. `object_replacement_manifest` checks
the current catalog hash, selected asset hash and type, licence reference,
permitted uses, and a non-empty project approval selection before the asset is
staged. The `replace-object` command then adds the asset to the timeline's
hash-bound asset list; the original shot stays below it and remains visible when
the approved track emits zero opacity.

```text
videoedit replace-object TIMELINE SEGMENTATION_RESULT OUTPUT \
  --segmentation-validation VALIDATION --track-review REVIEW \
  --asset-catalog ASSET_CATALOG --asset-manifest ASSET_MANIFEST \
  --asset-id asset_apple --keyframes KEYFRAMES
```

The replacement path is local-only. Missing licence provenance, a changed asset
file, a stale catalog selection, or absent approval IDs blocks staging.

P9-03 keeps foreground occlusion as a separate reviewed layer. The source range
is extracted losslessly, the approved white-foreground mask sequence is staged
and decoded frame-for-frame, and the alpha plane must have mixed transparency
before the result can be consumed by Remotion. A stale segmentation result,
validation report, track review, mask file, source range, or alpha diagnostic
fails closed and leaves the original shot as the declared fallback.

```text
videoedit render-occluder PROJECT_ID SEGMENTATION_RESULT \
  --segmentation-validation VALIDATION --track-review REVIEW \
  --object-id 1 --layer-id tracked-occluder --revision-id rev_001
videoedit add-occluder-layer TIMELINE OCCLUDER_MANIFEST OUTPUT \
  --remotion-directory remotion
```

The resulting `occluder_manifest` binds the current source, track review,
segmentation validation, every staged mask hash, output hash, dimensions,
frame rate, frame count, duration, alpha range, and full-decode diagnostics.
The appended Remotion video layer is `role=front`, transparent, muted, and
hash-bound. An explicit occluder review is required; a model result cannot
approve identity, continuity, geometry, or crossing-object semantics.

The 2026-07-24 synthetic validation proved the contract, lossless source-range
extraction, mask alignment, mixed alpha, and full decode. An earlier sandboxed
render attempt produced a green-only sampled frame and was retained as failed
evidence. After the compositor process became runnable, the current CLI
regenerated the manifest and timeline, and three independent multi-frame
Remotion renders produced the same output hash. First, middle, and last frames
all show the replacement plate with the blue foreground occluder. The
original-shot fallback remains required for stale or uncertain tracks; the
synthetic fixture approval is not production operator approval.

P9-04 keeps inpainting outside the deterministic compositor. `plan-inpainting`
only writes a hash-bound `inpainting_request` after a passing local mask
validation, and records a disabled network state, request idempotency key, and
the required original-shot fallback. Submission is a separate adapter call:

```text
videoedit plan-inpainting PROJECT_ID SOURCE MASK_VALIDATION \
  --start-frame 0 --end-frame END --provider PROVIDER --model MODEL
videoedit submit-inpainting PROJECT_ID REQUEST EFFECT_APPROVAL SPEND_APPROVAL \
  --command PROVIDER_COMMAND
```

The submit boundary requires current request-bound edit and spend approvals,
then checks the provider network policy. The default disabled adapter fails
closed; provider SDKs and credentials are not imported by the core. A provider
must receive one absolute request path and return one JSON object through the
typed process adapter. No provider output is consumed until its own validated
result and review contract are implemented.

## Plan local B-roll, motion, and sound together

`plan-cues` builds three proposal artifacts plus a hash-bound `cue_plan_bundle`.
The B-roll proposal records the selected local asset ID, file hash, licence
reference, transcript context, output range, and base-video fallback. The motion
proposal is derived only from verified structural transitions and uses a clean-cut
fallback. Transition sound is selected from the current local catalogue and stays
speech-priority and proposed.

The planner enforces the configured maximum B-roll coverage, minimum B-roll
spacing, same-asset usage limit, motion frequency and spacing, sound cue density,
and non-overlapping picture/sound collision groups. A rejected placement is
omitted from the active cue list and retained as a deterministic warning; it is
never replaced by an unlicensed or remote asset. The bundle records the policy,
dependency, plan, and collision hashes so a later approval cannot survive a stale
input.

```text
videoedit plan-cues PROJECT --transition-plan TRANSITIONS \
  --catalog ASSET_CATALOG --search-result SEARCH_RESULT \
  --timeline-duration-us 60000000 \
  --broll-start-us 0 --broll-end-us 1000000
videoedit approve-cues PROJECT --bundle artifacts/cue-plan-bundle.json \
  --actor REVIEWER --reason "Approved after cue review"
```

The plan files intentionally remain `proposed` after approval. The separate
`cue_batch` approval record is the authorization that downstream renderers must
verify against the bundle hash.

To consume the approved sound portion, run `mix-sound-plan` with the current
source and catalogue. The command applies every approved cue with the recorded
gain and fades, ducks speech when the policy requires it, and checks full decode,
clipping, loudness, and true peak before atomic promotion. It writes the aggregate
`sound_mix_qa` report; a failed mix remains as uniquely named evidence rather than
replacing the approved output.

After cue consumption, `manifest-assets` records the selected B-roll and sound
files, their current hashes and licence references, the cue approval ID, effect
ranges, permitted uses, attribution, and usage history. The manifest is a delivery
provenance input, not an approval by itself.

## Replace a background

Use chroma key or person alpha, render the background plate, overlay the subject, then add front graphics.

## Put text behind a person

Render the text on the background plate before subject overlay. Keep captions in the front pass.

## Trigger an effect from speech

Match the effect request to stable word IDs and source time, then map it through the approved output time map. Do not search the final transcript by raw string alone.

## Purposeful swipe with sound

Use a swipe only when the edit moves to a verified new point or major visual mode. Render outgoing and incoming frames with full-frame coverage. Align the strongest movement frame with the catalogued sound transient. Finish before the viewer must read or act on the incoming content. If the purpose, timing, or dialogue clearance is weak, use the declared clean-cut fallback.
