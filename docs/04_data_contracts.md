# Data Contracts

## General conventions

- Persist JSON as UTF-8.
- Use ISO 8601 UTC timestamps with a `Z` suffix.
- Use stable sortable identifiers.
- Use snake_case field names.
- Use integer microseconds for canonical media time.
- Use integer frames for Remotion and frame-aligned worker data.
- Use integer bytes for sizes.
- Use rational frame rates as numerator and denominator.
- Use lowercase hexadecimal SHA-256 hashes.
- Use explicit schema versions.
- Reject unknown fields in core persisted contracts.
- Use half-open time and frame ranges.

## Common identifiers

| Field | Meaning |
|---|---|
| `project_id` | Stable project identity |
| `revision_id` | Immutable editorial revision |
| `artifact_id` | Persisted artifact identity |
| `stage_run_id` | One stage attempt |
| `proposal_id` | One edit or effect proposal |
| `approval_id` | One approval record |
| `asset_id` | One indexed asset |
| `segment_id` | One logical review segment |
| `worker_job_id` | One local GPU worker request |
| `provider_job_id` | One optional remote provider request |

## Time range

```json
{
  "start_us": 1200000,
  "end_us": 2450000
}
```

The range is half-open. Duration is `end_us - start_us`.

## Frame range

```json
{
  "start_frame": 120,
  "end_frame": 181
}
```

The frame range is half-open. The declared frame rate and media hash are required in the containing artifact.

## Artifact envelope

Canonical artifacts should include:

```json
{
  "schema_name": "transcript",
  "schema_version": "1.0.0",
  "artifact_id": "art_transcript_001",
  "project_id": "prj_demo",
  "revision_id": "rev_001",
  "created_at": "2026-07-23T10:00:00Z",
  "producer": {
    "application_version": "0.2.0",
    "stage": "transcribe",
    "adapter": "whisper_local",
    "adapter_version": "1"
  },
  "inputs": [
    {
      "artifact_id": "art_audio_proxy",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  ],
  "config_sha256": "1111111111111111111111111111111111111111111111111111111111111111"
}
```

Focused schemas in this package do not all repeat the full envelope. The implementation may factor common fields through Pydantic models while preserving emitted compatibility.

## Transcript

A transcript contains:

- source identity and duration
- language
- ordered segments
- ordered words
- stable word IDs
- text
- source start and end time
- available confidence evidence
- flags for adjusted, uncertain, or invalid timing

Effects should reference stable word IDs and a source range. Raw quote matching alone is not sufficient.

## Edit proposals

Each proposal contains:

- controlled type
- source range
- affected word IDs
- exact excerpt
- evidence links
- reason
- confidence
- meaning risk
- continuity risk
- suggested handles
- policy result
- approval requirement

## Effect plan

Each effect contains:

- effect ID and controlled kind
- trigger word IDs or explicit range
- source time range
- output time or frame range after mapping
- intended result
- target prompt
- renderer and optional worker
- asset requirements
- parameters
- fallback
- risk
- approval requirement

## Focus and pacing plan

A focus and pacing plan contains approved or reviewable purposeful zooms and requested prompt speed-ups. Zooms record an allowed purpose, visible target track, target-visible range, easing, centering, stability, confidence, evidence frames, policy result, and `no_zoom` fallback. Speed-ups record the explicit request, allowed visible action, exact source range, playback rate, audio mode, forbidden-content checks, confidence, evidence frames, policy result, and `normal_speed` fallback.

## Edit decision list

The canonical edit decision list stores approved source ranges to keep. It may also record deletions for audit.

Required invariants:

- keep ranges are ordered
- keep ranges do not overlap
- keep ranges are inside source bounds
- expected output duration equals the sum of keep durations
- every deletion is traceable to a proposal and approval
- all ranges use the same source clock

## Retimed timeline and source-to-output mapping

The retimed timeline records every retained source range and its corresponding output range. Each segment declares playback rate, audio mode, command strategy, speed-up action identifier, and boundary confidence. Projects without speed-ups use rate 1 segments. This artifact is the only accepted basis for rebasing transcript words, captions, zooms, effects, and review timestamps.

## Visual timeline

A visual timeline records:

- dimensions
- rational frame rate
- duration frames
- background
- ordered layers
- audio layers
- caption events
- approved structural transitions in integer Remotion frames
- local asset references
- code bundle identity used for the render

Cross-field validation ensures layer and transition ranges fit the composition, structural
transitions do not overlap, the first readable incoming frame is not covered, and referenced
assets exist. `transition_plan_sha256` binds the rendered props to the output-time plan;
unapproved or clean-cut entries are omitted from the Remotion transition layer.

## Asset library

Each asset record contains:

- asset ID and type
- file reference and hash
- media properties
- description and tags
- source and licence
- permitted use and attribution
- sensitive content flags
- usage history

`videoedit index-assets` accepts this metadata as the licence declaration, then
resolves every file under the declared local root, recomputes its SHA-256 and
size, and probes image, video, and audio properties through FFprobe. It never
infers ownership or permitted use. A path escape, missing file, changed hash,
duplicate asset, or missing provenance fails before the indexed catalog is
promoted. Re-indexing an unchanged catalog is idempotent.

`videoedit search-assets` ranks the current catalog locally from transcript
context and effect-intent terms. It supports an exact asset-type filter and
required tags, returns deterministic scores and matched terms, binds the
result to the current catalog hash, and records a warning instead of inventing
a match when nothing is relevant.

## Worker jobs and results

Worker jobs declare source identity, frame rate, bounded frame range, prompts, output format, and contract version.

Worker results declare input identity, model and checkpoint identity, upstream commit, environment, device, generated file references, hashes, geometry, warnings, and quality evidence.

## Segment review

A segment review records:

- segment identity and revision
- preview hash
- transcript comparison hash
- composition code bundle hash
- asset and worker result hashes
- QA report hash
- reviewer decision and notes
- parsed fix markers
- lock state

## Project asset manifest

The project asset manifest records every selected external asset, its catalog hash,
file hash, role, source and output range, effect identifiers, approval identifiers,
licence reference, attribution text, licence status, source, description, tags,
permitted uses, sensitive-content declaration, and usage-history snapshot. The
manifest also binds the current cue bundle, human approval, and plan hashes through
its `inputs` and deterministic `selection_key`. `videoedit manifest-assets` rejects
stale catalog files, changed selected media, missing licence references, disabled
provider selections, and cue batches without current human approval. Delivery QA
rejects a selected asset that is absent from this manifest.

## Segment review

A segment review binds Gate 2 to the exact preview, transcript comparison, effect assets, composition code bundle, and QA report. It records the reviewer, decision, notes, parsed fix markers, and lock state. A change to any bound hash makes the review stale.

QA warning overrides are separate `qa_override` artifacts. They bind one current QA
report, exact warning finding IDs, retained evidence hashes, reviewer identity, reason,
and classification. They never mutate the source QA report. Hard failures and skipped
checks cannot be overridden by this contract; the operator must repair them or use the
separate gate appropriate to the decision.

`qa_review_packet` is a pending-only operator inspection artifact. It binds the current
candidate, final/join/segment QA inputs, every included join-preview hash, grouped
warning signals, and exact evidence references. `qa_review_decision` records a human
actor's decisions against the packet item hashes. Partial records are resumable;
`repair` and `reject_candidate` remain change requests, while non-defect classifications
do not bypass the separate `qa_override` or Gate 3 approval contracts.

The `qa_review_visual_evidence` artifact is a review-only, hash-bound collection of
FFmpeg-generated PNG contact sheets for the join items in a QA packet. It records the
exact preview and frame index for each sample, so the visual context can be inspected
without treating a process exit code as visual proof. It does not contain decisions,
approvals, overrides, delivery authorization, or cleanup authorization.

## Render manifest

A render manifest records:

- composition or edit plan hash
- relevant code bundle hash
- redacted process commands
- input and output hashes
- codec and stream properties
- expected and actual duration
- application and binary versions
- elapsed time
- hardware acceleration choice
- warnings

## QA report

QA check status values are:

- `pass`
- `fail`
- `warning`
- `skipped`

A skipped required check prevents final readiness. Each finding includes a stable code, severity, message, evidence, relevant time range, repair hint, and whether it is required.

## Backup verification and cleanup plan

Backup verification records source and delivery paths, sizes, hashes, verification time, and pass or fail status. The cleanup plan references the backup report hash, lists only derived artifacts, proves source paths are excluded, records eligibility reasons, and requires a current cleanup approval before execution.

## Approval record

An immutable approval record includes:

- approval type
- actor and role
- decision and reason
- approved item type and hash
- bound input hashes
- creation time
- optional expiry
- optional budget scope

Changing a bound item invalidates the approval.

## Schema compatibility policy

- Patch versions may clarify descriptions without changing validation.
- Minor versions may add optional fields.
- Major versions may remove fields, change meaning, or tighten validation in a breaking way.
- Readers reject unsupported major versions.
- Migrations create a new artifact and retain the old one.

## Validation layers

1. JSON parsing
2. JSON Schema validation
3. Pydantic model validation
4. cross-field domain invariants
5. cross-artifact reference validation
6. file hash and media validation
7. approval and stage dependency validation

## Included schemas

The authoritative list is `examples/index.json`. It includes project, source, media, transcript, edit, effect, focus and pacing, retimed timeline, approval, asset, worker, visual, review, render, QA, delivery, and phase result contracts.

## Version 2.2 contracts

- `edit_proposals` now permits the complete cut taxonomy and optional smart-edit evidence, take scores, protected-content checks, density class, and join strategy.
- `structural_boundaries` records output-time boundary evidence, outgoing and incoming segment identities, purpose, transcript/visual evidence, confidence components, and review status. `transition_plan` consumes that evidence and records structural boundaries, motion style, timing, easing, full-frame coverage, sound synchronization, confidence, fallback, and approval state. The new fields are optional in schema 1.0.0 so existing plans remain valid.
- `join_qa_report` records transcript, audio, visual, semantic, and pacing evidence for each rendered join. Each generated join item also binds a decoded preview file by path, SHA-256, size, duration, and decode status. Semantic protection fields are additive and make meaning-preservation review explicit. Caller-supplied media evidence is canonically hashed into the resumable join-QA stage key so changed click, room-tone, screen-state, or semantic findings cannot reuse a stale report.
- `join_plan` records one deterministic non-decorative join strategy, bounded preview context, repair order, handles, and hard-cut fallback for every applied cut.
- `sound_plan` may link a cue to a transition and record transient synchronization, tolerance, speech protection, reuse spacing, and mix QA data.
- Transition-sound planning selects only local catalogued assets with licence provenance and audio metadata. Selection, alignment, gain, fades, speech clearance, reuse spacing, clipping, loudness, and true-peak diagnostics remain recorded; sound cues stay proposed until an operator approval is present.
- `cue_plan_bundle` binds the proposed B-roll, motion, and sound plans to their current dependency hashes, local density policy, collision diagnostics, and a canonical bundle hash. A `cue_batch` approval record binds to that bundle hash; writing a proposal never approves it.
- Cue planning fails closed on stale catalog/search results, missing licence references, reused assets beyond policy, overlapping picture or sound cues, motion-spacing violations, and excess B-roll coverage. Rejected placements remain visible as warnings with the declared base-video or clean-cut fallback.
- `transition_sound_qa` binds a single decoded mix preview to the sound plan, catalog, source hash, clipping result, loudness/true-peak measurement, speech-priority mode, and QA status. `sound_mix_qa` records the aggregate result of applying every cue in a current approved `cue_batch`, including each cue's gain, fades, ducking mode, output hash, and preserved failure evidence.
- `edit_review_batch` groups smart-dense proposals into policy-authorized mechanical work, material questions, deferred questions, and keep-original fallbacks. Its question cap, policy hash, proposal-set hash, and optional human `smart_dense_policy` approval are persisted for reproducible review.
- `edit_metrics_qa` records cut density, retained-fragment distribution, cadence, transition frequency, and repetition counts. These metrics have `signal_only: true` and `blocking: false`; warnings route attention but never fail an edit solely because it contains many cuts.
- `approval_record` includes `smart_dense_policy`, `transition`, `transition_batch`, and `cue_batch` scopes; approvals still bind to exact input and approved-item hashes.
- `provider_job` is the optional remote-provider boundary. It records the request hash, deterministic idempotency key, disabled-by-default network state, effect and spend approval references, estimate, remote job identity, retry state, and output provenance. Provider submission requires current request-bound effect approval, current spend approval with a maximum at least as large as the decimal estimate, and explicit network opt-in; an unresolved `submitting` state is never blindly resubmitted.
- `config/editing-policy.example.yaml` exposes the complete smart-dense cut taxonomy and protected-content review categories, while `config/transitions.example.yaml` declares structural purposes, motion-sound requirements, and clean-cut fallback policy.
- Existing version 2.1 artifacts remain valid because new fields are optional and new plans are separate artifacts.
