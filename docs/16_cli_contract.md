# Command Line Contract

## General behavior

Executable name:

```text
videoedit
```

All commands:

- provide `--help`
- use stable exit codes
- print human-readable output by default
- support `--json` for machine output where meaningful
- write logs separately from JSON standard output
- reject unknown options
- avoid interactive prompts in automation mode

## Global options

```text
--project PATH
--config PATH
--log-level LEVEL
--json
--no-color
--trace
--version
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | success |
| 2 | usage or configuration error |
| 3 | dependency unavailable |
| 4 | input or contract validation error |
| 5 | approval required or stale |
| 6 | budget blocked |
| 7 | external process failure |
| 8 | provider transient failure |
| 9 | provider permanent failure |
| 10 | QA failed |
| 11 | project locked or state conflict |
| 12 | cancelled |
| 13 | internal error |

Exact values can change before the first release. Once published, preserve them.

## Commands

### `videoedit doctor`

Checks:

- Python version
- writable project path
- FFmpeg and ffprobe
- required filters and encoders
- libass support
- Whisper availability
- optional GPU support, including a bounded FFmpeg AMD AMF media-encoder probe
- optional, deferred SAM 3.1 and MatAnyone 2 worker prerequisites: isolated Python path and
  version, immutable upstream checkout/ref, local checkpoint presence, compatible
  `nvidia-smi` probe, and configured command state
- optional provider CLI and authentication state
- font availability
- disk space

JSON result includes a list of checks with status, code, message, evidence, and repair hint.
Worker checks are optional warnings and never start model inference. `h264_amf` remains
media-encoder evidence only; it does not satisfy the NVIDIA/CUDA requirement for either
isolated worker. Missing operator acceptance, checkpoint hashes, licence decisions, or
project-scoped `worker_runtime_approval` remain explicit blockers only for a
worker extension job. Under ADR-0013 they are warnings for the final workflow,
which does not invoke `run-worker`.

### Encoder profile

The local render stages use these process settings:

```text
VIDEOEDIT_VIDEO_CODEC=libx264|h264_amf
VIDEOEDIT_VIDEO_BITRATE_BPS=4000000
```

`libx264` is the default. `h264_amf` is an explicit AMD media-encoder opt-in and
requires a passing doctor capability check. The selected codec and bitrate are
part of stage/cache identity for base, retimed, final assembly, delivery derivative,
proxy, demo, recolor, overlay, contrast-review, and sound/matte QA media operations.
Audio-bearing overlays, approved-segment assembly, retimed renders, platform
derivatives, and transition-sound mixes pad or trim mapped production audio before
the bounded `-shortest` operation so AAC/container end padding cannot remove the
final visual frame; the visual boundary remains authoritative and the result still
receives normal A/V and loudness checks.
CLI commands that probe or render media pass the same configured typed adapter through
the service boundary; an omitted explicit adapter only falls back to the software
default for library callers. This profile does not change lossless intermediate or
alpha encoding and does not establish compatibility for the isolated GPU workers.

### Remotion runtime

Remotion commands use `VIDEOEDIT_NPM_PATH` from the process settings. The configured
Node.js 22/npm pair is passed through the CLI, demo, composition, preview, render, and
hash-bound asset-layer paths; they do not silently resolve a different `npm` from
`PATH`. The Node/npm runtime identity remains an environment prerequisite recorded by
`videoedit doctor` and must be rechecked when the runtime changes. Frame-range and full
Remotion renders use `--concurrency=1` so OffthreadVideo decode is frame-deterministic
for local transparent and ordinary video layers. Full renders that contain audio pass
through the configured typed FFmpeg adapter after Remotion completes: the video stream
is explicitly copied, while mixed audio is padded and trimmed to the frame-derived
timeline duration before atomic promotion. Frame-range and still renders do not run
this full-render audio finalization. A successful process exit still does not replace
decoded-frame visual review.

### `videoedit project init PATH`

Options:

```text
--name TEXT
--template PATH
--project-id TEXT
```

Creates no media output.

### `videoedit ingest PROJECT SOURCE`

Options:

```text
--mode reference|copy
--audio-proxy-profile NAME
--video-proxy-profile NAME
--dry-run
```

### `videoedit plan PROJECT`

Runs missing analysis and creates edit proposals.

Options:

```text
--through STAGE
--policy NAME
--planner none|configured
--force-stage NAME
```

### `videoedit review export PROJECT`

Options:

```text
--format markdown|json|all
--output PATH
```

### `videoedit approve edits PROJECT`

Options:

```text
--from PATH
--actor TEXT
--role TEXT
```

Imports explicit decisions. It does not infer approval from a modified proposal file.

### `videoedit approve-smart-dense-policy PROJECT`

Options include `--actor`, `--role`, `--proposals`, and `--reason`. The command creates an explicit human policy approval for high-confidence mechanical smart-dense candidates. It binds the approval to the current proposal set, policy hash, revision, and project configuration; it does not approve semantic deletions or effects.

### `videoedit plan-smart-dense-review PROJECT`

Writes a hash-bound `edit_review_batch` artifact and Markdown summary. High-confidence mechanical candidates are held until the explicit policy approval is supplied, material semantic or continuity uncertainty is ranked into at most five questions, and low-impact uncertainty uses the keep-original fallback.

### `videoedit qa-edit-metrics PROJECT`

Writes a non-blocking `edit_metrics_qa` artifact from the canonical EDL, proposals, transcript, and optional transition and join-QA artifacts. It reports cut density, retained fragments, cadence, transition frequency, and repetition signals; high cut density is a warning signal and is never an automatic failure.

### `videoedit render rough PROJECT`

Options:

```text
--profile NAME
--revision ID
--dry-run
```

### `videoedit plan assets PROJECT`

Options:

```text
--sound on|off
--motion on|off
--broll on|off
--provider NAME
```

### `videoedit costs PROJECT`

Shows planned requests, estimates, reserve, approved amount, committed amount, and actual amount.

### `videoedit approve spend PROJECT`

Options:

```text
--max-usd DECIMAL
--expires-in DURATION
--actor TEXT
--role TEXT
```

Currency should come from project configuration when more than USD is supported.

### `videoedit generate assets PROJECT`

Options:

```text
--request ID
--all-approved
--dry-run
--network
```

The `--network` flag is an additional explicit intent signal. It does not replace policy and configuration.

### `videoedit render preview PROJECT`

Options:

```text
--profile NAME
--revision ID
--watermark-review
```

### `videoedit qa PROJECT`

Options:

```text
--render preview|final|PATH
--profile NAME
--report PATH
```

Returns exit code 10 when required checks fail.

### `videoedit plan-joins PROJECT`

Creates one deterministic join strategy, bounded preview range, repair order, and hard-cut fallback for every approved applied cut.

### `videoedit qa-joins PROJECT`

Renders every join preview with context on both sides, extracts a speech proxy, re-transcribes it through the selected local transcription adapter, compares it with the approved output transcript, runs audio/visual/pacing checks, and persists `artifacts/join-qa-report.json`. Resuming an identical stage reuses the report only when the stage-bound report hash, every preview hash and size, project boundary, exact join ID coverage, and join count still match; tampered evidence is re-rendered.

Retimed join plans also persist `source_preview_ranges`: the disjoint source-clock ranges
that correspond to each output-clock preview after cuts and approved speed-ups. The
post-render transcript used by `qa-joins` is output-clock by default, so the default
approved excerpt comes from `preview_range`; this prevents stale source-clock ranges from
being applied to a post-render transcript after a revision. Use `--transcript-clock source`
only when the supplied transcript is source-clock and the plan's `source_preview_ranges`
are the intended evidence. A non-empty local Whisper difference is retained as a required
operator-review warning unless the evidence proves empty speech, adjacent duplicated
speech, an explicit semantic defect, or a deterministic media/pacing failure. This is
uncertainty classification, not an automatic approval or a lowered output-quality
threshold.

Options:

```text
--render-manifest PATH
--join-plan PATH
--transcript PATH
--model-path PATH
--model-name NAME
--transcript-clock output|source
--revision-id rev_002
```

`--revision-id` binds the persisted join-QA report and resumable stage to the
current review revision. The applied join plan may remain bound to the parent
retimed timeline; the report revision identifies the review/candidate revision.

When `--model-path` and `--model-name` are omitted, `qa-joins` and
`retranscribe-revision` use the operator-supplied path and model name from
`VIDEOEDIT_WHISPER_MODEL_PATH` and `VIDEOEDIT_WHISPER_MODEL`. Explicit options
take precedence. If the path is not configured, local re-transcription fails
closed with the adapter's missing-model error.

Missing manual continuity evidence remains a warning and requires review. A transcript or media failure includes a deterministic repair route; it does not silently approve the join.

Freeze, black-frame, and clipping probes first scan the full context preview, then
re-probe a bounded window around the exact output join when context evidence exists.
The report retains both context evidence and boundary-local results; context-only media
evidence is routed to segment review and does not misclassify an otherwise clean join.
The boundary window is part of the policy-bound stage key.

The persisted join-QA report records the transcription adapter, model identifier,
device, and local model SHA-256 when available. The join-QA stage key includes
that model identity, so replacing a model at the same path invalidates cached
join evidence.

### `videoedit index-assets PROJECT_ID ASSET_ROOT METADATA`

Recomputes local asset hashes and media properties from a licensed metadata
catalog and writes a schema-valid indexed catalog. The root, metadata, and
output must remain inside the project; licence fields are supplied by the
operator and are never inferred.

### `videoedit search-assets PROJECT_ID CATALOG QUERY`

Ranks current catalogue entries using transcript context and effect intent.
The result is hash-bound to the catalog and remains local; empty results are a
recorded warning and never trigger a remote search.

### `videoedit detect-boundaries PROJECT`

Scans ordered output transcript segments and optional operator evidence for explicit new points, chapters, mode changes, comparisons, before/after changes, location changes, and returns from visual explanations. It writes `structural-boundaries.json`; unsupported or weak evidence is not promoted to motion.

Options:

```text
--transcript PATH
--explicit-boundaries PATH
--policy PATH
```

### `videoedit plan-transitions PROJECT`

Consumes structural boundary evidence and an optional sound plan. It emits purpose-bound transitions only when full-frame coverage, readable incoming content, dialogue clearance, spacing, confidence, and required licensed sound are present. Otherwise it emits a `hard_cut` fallback with a warning.

Options:

```text
--transcript PATH
--boundaries PATH
--sound-plan PATH
--policy PATH
```

### `videoedit plan-transition-sounds PROJECT`

Selects only hash-bound, locally catalogued, licensed, speech-safe transition sounds.
It aligns each transient to the declared visual peak, applies bounded gain and fades,
checks the first important incoming word and reuse spacing, and writes proposed cues.
The command never self-approves a cue.

Options:

```text
--transition-plan PATH
--catalog PATH
--policy PATH
--brand-context TAG
```

### `videoedit plan-cues PROJECT`

Builds proposed local B-roll, motion, and transition-sound plans and writes a
`cue_plan_bundle`. The bundle records exact catalog, search-result, transition,
policy, and plan hashes. B-roll coverage, asset reuse, motion spacing/frequency,
full-picture collisions, and sound cue density are checked before a placement is
written. Local assets have zero provider cost; no network or paid provider is
invoked.

Options:

```text
--transition-plan PATH
--catalog PATH
--search-result PATH
--assets-policy PATH
--transitions-policy PATH
--timeline-duration-us INTEGER
--broll-start-us INTEGER
--broll-end-us INTEGER
--broll-context TEXT
--broll-rationale TEXT
```

Every generated cue remains `proposed` and the bundle remains approval-required.
An omitted, stale, colliding, or over-dense B-roll request is not silently
substituted with remote media; the bundle records the warning and keeps the base
video fallback.

### `videoedit approve-cues PROJECT`

Writes a human `approval_record` with `approval_type: cue_batch`, bound to the
current canonical `cue_plan_bundle` hash and all three plan hashes. Approval is a
separate action and becomes stale when a catalog, search result, policy, transition
plan, or generated cue plan changes.

Options:

```text
--bundle PATH
--actor TEXT
--role TEXT
--reason TEXT
```

### `videoedit qa-transition-sound PROJECT CUE_ID`

Renders a bounded local mix preview through the typed FFmpeg adapter and checks full
decode, clipping, loudness, true peak, and speech-priority mixing. It writes a
schema-valid `transition_sound_qa` report. Proposed cues can be previewed with
`--allow-proposed`; final use still requires explicit sound approval.

### `videoedit mix-sound-plan PROJECT_ID`

Applies every cue from a current human-approved `cue_batch` to the immutable source
through the typed FFmpeg adapter. It verifies the approved bundle, current local
catalogue hashes and licence references, speech-safe audio metadata, gain and fade
limits, and speech-priority ducking before staging each mix. The final output is
promoted only after full decode, clipping, loudness, and true-peak checks. A failed
mix is preserved under a unique `.failed-<id>` path and is never promoted as the
approved output. The command writes a schema-valid `sound_mix_qa` report and reuses
an existing report only when all bound hashes and the output hash still match.

Options:

```text
--source PATH
--catalog PATH
--bundle PATH
--approval PATH
--sound-plan PATH
--output PATH
--report PATH
--policy PATH
--revision-id TEXT
```

### `videoedit manifest-assets PROJECT_ID`

Writes the current project asset manifest from a licensed local catalog and, when
provided, an authorized cue-plan bundle plus optional object-replacement manifests.
It verifies each selected file hash and licence reference, snapshots provenance and
usage history, records the effect and approval IDs, and binds the input hashes to a
deterministic `selection_key`. Existing output with the same binding is reused;
existing output with a different binding is preserved and rejected rather than
overwritten. A schema-valid catalog with an empty `assets` array is supported for a
source-only cut; the resulting manifest records `no_external_assets_selected` rather
than inventing a licence or attribution for media that was not used.

Options:

```text
--catalog PATH
--bundle PATH
--approval PATH
--replacement-manifest PATH  (repeatable)
--output PATH
--revision-id TEXT
```

### `videoedit plan-provider-job PROJECT_ID`

Creates a schema-valid `provider_job` proposal from a provider-backed B-roll request.
The request hash, estimate, provider/model identity, deterministic idempotency key,
and disabled network state are persisted; no provider call is made.

Options:

```text
--request-plan PATH
--request-id TEXT
--provider TEXT
--model TEXT
--output PATH
--revision-id TEXT
```

### `videoedit submit-provider-job PROJECT_ID`

The submission gate verifies current request-bound effect and spend approvals and the
decimal spend ceiling before requiring explicit network opt-in. The repository ships
no provider SDK or default submitter, so the credential-free path remains blocked.
An active or unresolved job is never submitted a second time automatically.

Options:

```text
--job PATH
--effect-approval PATH
--spend-approval PATH
--network-enabled
```

### `videoedit approve final PROJECT`

Options:

```text
--preview ID|active
--qa-report ID|active
--actor TEXT
--role TEXT
--reason TEXT
```

### `videoedit render final PROJECT`

Options:

```text
--profile NAME
--revision ID
--output-dir PATH
```

### `videoedit status PROJECT`

Options:

```text
--watch
--events INTEGER
```

The command emits the schema-valid JSON snapshot directly; there is no separate `--json`
flag. Source integrity is checked against the immutable ingest manifest and managed source
hash, independent of the later edit revision. Final QA is selected from the current project
or review artifacts for the requested revision, while a non-ready report remains an explicit
`final_qa_not_ready` warning and never implies Gate 3 approval.

### `videoedit retry PROJECT`

Options:

```text
--stage NAME
--run ID
```

Retry validates eligibility and does not resubmit a remote paid request without resolving its job state.

### `videoedit clean PROJECT`

Options:

```text
--derived-only
--cache
--inactive-revisions
--dry-run
```

At least one cleanup scope is required.

### `videoedit project export PROJECT`

Options:

```text
--output PATH
--include-source
--include-cache
```

## JSON envelope

Machine output should use:

```json
{
  "command": "videoedit status",
  "status": "ok",
  "project_id": "prj_demo",
  "data": {},
  "warnings": [],
  "errors": []
}
```

Errors include stable code, message, retryable flag, and optional safe details.

## Progress output

Human mode can show progress. JSON mode should keep standard output valid JSON. Send progress events to standard error or use a separate JSON-lines mode.

## Automation safety

- Mutating commands should support `--dry-run` when practical.
- Provider calls require explicit network permission, project policy, credential, spend approval, and budget.
- Destructive cleanup requires a declared scope.
- Final render requires current approval.
- Stale artifacts produce a clear exit code and required next command.

## Planned focus and pacing commands

### `videoedit focus plan PROJECT`

Creates or revises a schema-valid focus and pacing plan. It records visible zoom targets, exact relevance ranges, explicit speed-up request evidence, exact prompt-action boundaries, confidence components, safe fallbacks, and review requirements.

### `videoedit approve focus PROJECT`

Imports operator decisions and writes a hash-bound focus and pacing approval. It cannot approve its own proposals and cannot modify the proposal artifact in place.

### `videoedit timeline retime PROJECT`

Compiles approved prompt speed-ups into a contiguous retimed timeline before the base render. It fails when ranges overlap, contain forbidden activity, lack request evidence, omit required audio policy, or leave gaps in the canonical map.

### `videoedit focus preview PROJECT`

Renders short proof clips and evidence frames for zoom and speed-up candidates. It reports target centering, easing, stability, edge coverage, exact action boundaries, audible audio, A/V synchronization, expected duration, and downstream cue mapping.

### Segment self-verification commands

The implemented segment review path is:

```text
videoedit preview-segments PROJECT_ID
videoedit review-segments PROJECT_ID
videoedit import-review-markers PROJECT_ID --markdown PATH
videoedit apply-review-markers PROJECT_ID --markers PATH
videoedit recut-revision PROJECT_ID --revision-request PATH --source PATH \
  [--video-codec CODEC] [--audio-codec CODEC] [--qp QP] [--preset PRESET] [--strict-decode]
videoedit retranscribe-revision PROJECT_ID --revision-media PATH --transcript PATH
videoedit rebase-join-plan-revision PROJECT_ID --join-plan PATH --revision-media PATH --revision-id REVISION
videoedit slice-segment-comparisons PROJECT_ID --preview-plan PATH --comparison PATH
videoedit qa-segment PROJECT_ID --revision-media PATH
videoedit approve-qa-override PROJECT_ID --qa-report PATH --finding FINDING_ID=EVIDENCE_PATH --actor TEXT --role TEXT --reason TEXT
videoedit check-qa-override PROJECT_ID --qa-report PATH --override PATH
videoedit qa-visual-segment PROJECT_ID --review-package PATH
videoedit plan-marker-focus PROJECT_ID --markers PATH
videoedit qa-focus-pacing PROJECT_ID --focus-pacing-plan PATH
videoedit approve-segment PROJECT_ID --review-package PATH --transcript-comparison PATH --segment-qa PATH --visual-qa PATH --composition-bundle PATH --actor TEXT --role TEXT
videoedit lock-segment PROJECT_ID --review PATH --review-package PATH --transcript-comparison PATH --segment-qa PATH --visual-qa PATH --composition-bundle PATH
```

Every command writes a new hash-bound artifact or revision. Segment QA reuses an existing report only when the revision manifest, rendered media, transcript comparison, caption plan, optional join-QA report, and current FFmpeg adapter version have the exact current hashes and provenance; changed evidence or tool provenance fails closed as stale rather than reusing an old finding. Segment visual QA also binds its fixed-path cache reuse to the current visual-QA implementation producer version, so an older schema-valid report fails closed after the checker changes. Re-transcription cache reuse additionally requires the requested model name, current adapter identity/version when declared, device, checkpoint identity, and project configuration hash to match the persisted rendered transcript; changed provenance fails closed before invoking media or transcription work. Final QA reuses a report only when its complete current payload matches the recomputed candidate, inputs, profile, findings, and readiness result; tampered or stale contents fail closed. The recut and re-transcription stages preserve the immutable source, and Gate 2 approval refuses non-final-ready transcript, media, visual, zoom, or speed-up QA unless a current hash-bound warning-only QA override covers every required warning. A review marker is a revision request; it never edits an approved artifact in place.

`approve-qa-override` creates `schemas/qa_override.schema.json` only from a human
actor, a non-empty reason, exact current warning finding IDs, and retained project-local
evidence. It refuses `fail` and `skipped` findings. `check-qa-override` validates the
target report, evidence hashes, and complete required-warning coverage; it does not
rewrite the QA report or imply Gate 2, Gate 3, delivery, or cleanup approval. When a
segment Gate 2 decision uses an override, the override hash is included in the decision
and subsequent segment lock.

### `videoedit qa-review-packet`

```text
videoedit qa-review-packet PROJECT_ID \
  --candidate PATH \
  --final-qa PATH \
  --join-qa PATH \
  --segment-qa PATH \
  --revision-id rev_002 \
  --review-gate gate3
```

Creates a schema-valid JSON and Markdown operator packet from the current candidate
QA. It independently verifies every join preview path, hash, size, and recorded
full-decode status before including it. Every packet item remains `pending`; the
command never approves a warning, creates a QA override, or grants Gate 3.

### `videoedit qa-review-visuals`

```text
videoedit qa-review-visuals PROJECT_ID \
  --packet PATH \
  --revision-id rev_002
```

Renders one project-local PNG contact sheet for each current join item. Each sheet
samples preview start, 250 ms before the join, the join boundary, 250 ms after the
join, and preview end when those distinct in-range frames exist. The output JSON and Markdown are hash-bound to the packet,
candidate, previews, frame counts, and FFmpeg contact-sheet adapter. It is visual
evidence only and never classifies a finding or grants any approval.

### `videoedit record-qa-review`

```text
videoedit record-qa-review PROJECT_ID \
  --packet PATH \
  --decision ITEM_ID=DECISION \
  --actor TEXT --role TEXT --reason TEXT
```

Records one or more explicit human decisions against the exact current packet.
Repeat `--decision` for a batch and optionally provide retained evidence with
`--evidence ITEM_ID=PATH`. The command rejects stale packet evidence, unknown item
IDs, decisions not allowed by the packet, missing reviewer identity, and empty
reasons. It never mutates the packet or grants QA override, Gate 2, Gate 3,
delivery, or cleanup approval.

### `videoedit qa PROJECT --scope focus-pacing`

Runs the required focus and pacing QA codes. Required failures block Gate 2.

## Planned visual-effect commands

### `videoedit effects plan PROJECT`

Creates or revises the schema-valid effect plan from the approved edit, creative brief, transcript word identifiers, and available assets.

### `videoedit remotion validate PROJECT`

Validates the visual timeline, staged assets, fonts, frame ranges, z-order, and TypeScript props without rendering a final video.

### `videoedit compose-visual PROJECT`

The visual composition command accepts an optional `--transition-plan` and repeated
`--approved-transition-id` options. The plan is converted from output microseconds to
integer Remotion frames. Without an explicitly approved ID, motion entries are omitted
and the clean-cut fallback remains active. The command accepts `--revision-id` for
revision-bound render manifests, derives the frame count from the decoded video stream
when a render manifest does not carry one, and emits a project-local composition bundle
whose bytes are the canonical path/hash manifest of the current Remotion source and
package lock. The emitted timeline records the transition plan hash and reports any
fail-closed transition warnings. A changed Remotion source produces a new bundle hash;
an existing bundle with stale bytes is rejected.

### `videoedit remotion render PROJECT`

Options:

```text
--pass background|middle|front|full
--range START-END
--profile NAME
--output PATH
```

### `videoedit segment PROJECT` (optional deferred extension)

Creates or runs an approved SAM 3.1 job only for an explicitly re-enabled
optional extension. The final workflow does not call this command.

Options:

```text
--effect ID
--job PATH
--dry-run
--worker-command PATH
```

The command must stop when checkpoint access, licence, device compatibility, or object identity is unresolved.

### `videoedit approve-worker-runtime PROJECT WORKER`

Creates a project-local `worker_runtime_approval` artifact for `sam3` or
`matanyone2`. It requires the operator-accepted immutable upstream commit,
checkpoint identifier and SHA-256, licence identity, installed PyTorch/CUDA,
target device, actor, role, and reason. The approval is hash-bound to that
exact identity and is required before a v1.1 worker job may declare
`runtime.access` as `approved`. It does not install packages, download a
checkpoint, or run inference. The reference also binds the current project
configuration and is rejected when that configuration changes or the approval
expires.

### `videoedit matte PROJECT` (optional deferred extension)

Creates or runs an approved MatAnyone 2 job only for an explicitly re-enabled
optional extension. The final workflow does not call this command.

Options:

```text
--effect ID
--initial-mask PATH
--job PATH
--dry-run
--worker-command PATH
```

### `videoedit preview segment PROJECT SEGMENT_ID`

Renders a bounded review clip, contact sheet, transcript excerpt, effect metadata, and mask or matte diagnostics.

The P10-01 planning entry point is `videoedit preview-segments PROJECT --media PATH [--transcript PATH]`.
It creates a schema-valid, hash-bound logical segment plan and low-cost FFmpeg previews under
`review/segments/<segment-id>/<planning-key>/`. The media and transcript must be project-local;
each preview is rendered from a half-open integer-microsecond range, decoded, probed, and promoted
atomically. The full `preview segment` package remains the Gate 2 command described below.

### `videoedit review import-fixes PROJECT PATH`

Imports timestamped fix markers into a new immutable revision. It does not modify an approved revision in place.

### `videoedit verify speech PROJECT`

Re-transcribes an edited segment or final candidate and compares it with the intended approved speech sequence.

### `videoedit backup verify PROJECT`

Checks that configured source and delivery backups exist and match recorded checksums. A hash-keyed cached report is reused only when its project, revision, target paths, sizes, hashes, statuses, and messages still match the freshly recomputed evidence; schema-valid stale contents fail closed. Cleanup remains blocked until this command passes and a cleanup approval exists.

### `videoedit approve segments PROJECT`

Imports Gate 2 decisions from one or more `segment_review` artifacts.

Options:

```text
--from PATH
--actor TEXT
--role TEXT
```

The command rejects stale preview, transcript comparison, effect asset, composition bundle, or QA hashes.

### `videoedit render final-candidate PROJECT`

Assembles only Gate 2 approved segment revisions, applies the final loudness pass, and writes a review candidate. Reuse of a derived manifest requires the current lock/media hashes, segment ranges, project configuration, encoder version, and exact promoted output/pre-normalized file hashes; a stale schema-valid manifest fails closed. It does not create a delivery master and does not imply Gate 3 approval.

Options:

```text
--profile NAME
--revision ID
--output PATH
```

### Implemented P11 commands

The local production path exposes these schema- and hash-bound commands:

```text
videoedit assemble-final PROJECT --segment-spec PATH [--segment-spec PATH ...]
videoedit qa-final PROJECT --assembly PATH --plan SCHEMA=PATH --gate2 PATH --visual-evidence PATH
videoedit qa-source-candidate PROJECT --candidate PATH --source-manifest PATH --retimed-render-manifest PATH --focus-pacing-qa PATH --transcript-comparison PATH --join-qa PATH --segment-qa PATH --join-plan PATH --gate1 PATH --backup-verification PATH --visual-evidence PATH
videoedit bridge-render-manifest PROJECT --base-render-manifest PATH --revision-media PATH --revision-id REVISION
videoedit bridge-retimed-render-manifest PROJECT --base-retimed-manifest PATH --revision-media PATH --revision-id REVISION
videoedit build-captions PROJECT --transcript PATH --render-manifest PATH --revision-id REVISION
videoedit record-watchthrough PROJECT --candidate PATH --actor TEXT --role TEXT
videoedit approve-gate3 PROJECT --final-qa PATH --watchthrough PATH --asset-manifest PATH --composition-bundle PATH --delivery-profile PATH --plan SCHEMA=PATH --gate2 PATH --actor TEXT --role TEXT
videoedit write-publishing-metadata PROJECT --candidate PATH --caption-plan PATH --transcript PATH
videoedit publish-delivery PROJECT --gate3 PATH --final-qa PATH --metadata PATH --delivery-profile PATH
videoedit backup-verify PROJECT --targets PATH
videoedit cleanup-plan PROJECT --backup-verification PATH
videoedit approve-cleanup PROJECT --cleanup-plan PATH --actor TEXT --reason TEXT
videoedit execute-cleanup PROJECT --cleanup-plan PATH --approval PATH --backup-verification PATH
videoedit status PROJECT
videoedit retry PROJECT --stage NAME --reason TEXT [--run ID]
videoedit cancel PROJECT --stage NAME --reason TEXT
videoedit recover-stage PROJECT --stage NAME --reason TEXT
```

`qa-final` is fail-closed for decode, duration, streams, dimensions, rational frame rate and
frame count, A/V drift, loudness/clipping, black/freeze diagnostics, captions, transcript,
visual proof, provenance, and current locked Gate 2 inputs. Gate 2 decision reuse also validates
the current reviewer, notes, fixes, decision, identity, and bound hashes; stale schema-valid
decisions fail closed. `approve-gate3` also verifies each
lock's review reference, approved decision, project/revision/segment identity, and exact Gate 2
bound-hash set before binding the lock into the final approval. `publish-delivery` requires an
approved Gate 3 record and final-ready QA, validates the publishing metadata project/revision
and every caption/transcript child hash, validates every rendered master or derivative
before writing the delivery manifest, and reuses a cached delivery only after checking its
hash-bound inputs, output paths and hashes, provenance, checksum manifest, and decodability.
A schema-valid stale delivery manifest fails closed. `record-watchthrough` binds the candidate and any
evidence hashes; repeating it with identical bindings reuses the existing record, while a
changed candidate, reviewer, protocol, decision, notes, or evidence produces a new record.
Cleanup remains a separate, explicit destructive action.

`qa-source-candidate` is the worker-free source-specific path for a retimed candidate that has
no additional Remotion composition, external asset, or locked Gate 2 segment. It validates the
candidate through the typed FFmpeg adapter, binds the source, retimed render, focus/pacing,
transcript, join, segment, Gate 1, join-plan, backup, and retained visual-evidence hashes, and
records warnings instead of treating automated ASR or media diagnostics as operator approval.
The `JOIN_REVIEW` finding also carries a deterministic `warning_breakdown` with counts for
transcript mismatches, freeze evidence, clipped-syllable evidence, pacing warnings, preview
decode failures, and hard failures. These counts improve review batching only; they do not
reclassify a warning as a pass or replace inspection of the linked join previews.
It records missing caption sidecars as a required skipped finding rather than implying that
captions were delivered. Supplying `--caption-plan` validates the revision-bound ASS,
WebVTT, and text sidecar hashes and their event timing against the candidate duration. The
source-specific path records sidecars as metadata only; it does not imply that captions were
burned into the MP4.
It always records Gate 3 as skipped until a human watch-through and approval exist.

The older `videoedit deliver` command is intentionally disabled because its legacy final-approval
path does not satisfy the current Gate 3 contract. Use `publish-delivery` with the current Gate 3,
final-QA, publishing-metadata, and delivery-profile artifacts.

Final visual proof must include at least one retained, non-empty image or video artifact
(for example `.png`, `.jpg`, `.webp`, `.mp4`, `.mov`, or `.webm`). A path that merely exists,
an empty file, or a JSON/text diagnostic is a required QA failure. This file check proves
that review material was retained; it does not replace human visual inspection or Gate 3.

### `videoedit cleanup plan PROJECT`

Creates the schema-valid cleanup plan from retained artifact records and a passing backup verification report. A keyed cached plan is reused only when its current derived entries, exact hashes, eligibility, backup binding, project, revision, and configuration identity match; schema-valid stale contents fail closed.

Options:

```text
--derived-only
--cache
--inactive-revisions
--dry-run
```

The command never includes registered source paths. Each generated entry records the exact
SHA-256 of the derived file, and execution refuses to remove a file if it has been replaced
since the plan was created. Execution remains blocked until the exact cleanup plan hash has a
current approval. A cleanup approval is also bound to its project, revision, reviewer, role,
reason, and plan hash; conflicting approval details are rejected rather than silently reused.
