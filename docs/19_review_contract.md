# Edit Review Contract

## Purpose

Review is a data transition between proposed edits and the canonical edit decision list. The reviewer should never edit the proposal artifact in place.

## Export package

`videoedit review export PROJECT` should create:

```text
review/
  edit-proposals.json
  edit-proposals.md
  edit-review-decisions.template.json
  previews/
  thumbnails/
```

The proposal JSON is immutable. The decision template contains the proposal set hash and one decision entry per proposal.

## Decision schema

Use `schemas/edit_review_decisions.schema.json`.

Each decision contains:

- proposal identifier
- proposal hash
- approve, reject, or modify
- modified cut range when the decision is modify
- reviewer reason

The artifact also records reviewer actor, role, time, project, revision, proposal set identifier, and proposal set hash.

## Import checks

The importer must reject:

- unknown proposal identifiers
- duplicate decisions for one proposal
- a proposal hash mismatch
- a proposal set hash mismatch
- a project or revision mismatch
- a modified range outside the original source
- a modified range that violates protected content or handle policy
- a missing decision when complete review is required
- extra unknown fields

A modified range is a new reviewer proposal. The policy engine validates it before an approval record is created.

## Approval creation

After import:

1. Verify proposal set and item hashes.
2. Validate every decision.
3. Create immutable approval records.
4. Compile approved cuts into keep ranges.
5. Validate timeline invariants.
6. Persist the canonical edit decision list.
7. Invalidate dependent artifacts from any prior timeline.

Rejected proposals remain in the audit trail and do not enter the timeline.

## Review Markdown

For each proposal, include:

- identifier and type
- source start and end timecode
- proposed cut timecode after handles
- duration saved
- transcript before, inside, and after
- evidence
- meaning and continuity risk
- confidence
- policy result
- preview link or command when available
- accepted decision values

The Markdown is for reading. The JSON decision file is the import contract.

## Batch review

Batch approval is limited to mechanical proposals that already pass the selected policy. A batch record lists every included proposal hash. Semantic proposals require item-level decisions in the first release. Focus and pacing uncertainties are batched into at most the configured maximum questions, with `no_zoom` and `normal_speed` used as safe fallbacks when possible.

The smart-dense batch artifact is `schemas/edit_review_batch.schema.json`. It separates policy-authorized mechanical candidates, ranked material questions, deferred questions beyond the five-question round cap, and low-impact keep-original fallbacks. A mechanical candidate is never marked policy-authorized unless an explicit human `smart_dense_policy` approval is supplied and remains hash-bound to the proposal set, policy, and project configuration. Semantic, meaning-bearing, protected, and high-continuity-risk proposals remain item-level questions or safe fallbacks; the batch planner cannot approve them.

## Revision behavior

A change to transcript, policy, proposal range, or proposal set makes prior review decisions stale. The system should export a new review package and preserve the old package under its revision.

# Segment and Final Review Extensions

## Gate 2 segment review

`videoedit preview segment PROJECT SEGMENT_ID` should create:

```text
review/segments/<segment-id>/
  preview.mp4
  contact-sheet.jpg
  transcript-comparison.json
  transcript-comparison.md
  effect-summary.json
  diagnostics/
  qa-report.json
  fixes.template.md
```

The imported Gate 2 record uses `schemas/segment_review.schema.json`. It binds the decision to:

- preview SHA-256
- transcript comparison SHA-256
- selected effect asset bundle SHA-256
- composition code bundle SHA-256
- QA report SHA-256

Supported fix markers are:

```text
[FIX] describe a general defect
[KEEP] lock this range as reviewed
[REMOVE] remove the named or timecoded range
[RETIME] move or extend an effect
[MASK] correct object or person selection
[TEXT] change copy, styling, or layout
[AUDIO] change timing, level, cue, or mix
[ZOOM] add, remove, retarget, or retime a purposeful zoom
[SPEED] add, remove, or retime a requested prompt speed-up
```

A marker creates a new revision request. It never edits an approved revision in place. After fixes, re-render, re-transcribe, re-run QA, and create a new Gate 2 decision against new hashes.

`slice-segment-comparisons` derives one immutable `segment_transcript_comparison`
for each current review segment from the revision-wide rendered Whisper comparison.
The slice retains absolute output-clock word timings and is keyed by the preview
plan, source comparison, and segment preview hashes. Gate 2 rejects a comparison
whose segment ID or range does not exactly match the review package.

`[ZOOM]` and `[SPEED]` markers are translated by `plan-marker-focus` into a revision-scoped focus/pacing plan. The importer requires an allowlisted visible target, normalized target evidence, explicit speed-up request evidence, exact action frames, audible-audio evidence, and the declared safe fallback. Missing evidence produces `no_zoom` or `normal_speed` and a warning; it never fabricates an effect.

`qa-focus-pacing` runs the required target, boundary, centering, easing, stability, edge, unrelated-content, action, audio, retiming, and transcript checks. Required failures keep Gate 2 blocked. Dead-air findings include the exact interior interval and a bounded fix-marker/re-render/re-transcription route; they do not self-approve an intentional pause. The existing U21 rendered proof frames and retimed fixture are retained as positive focus/speed evidence; the synthetic P10 segment fixture intentionally retains a failed dead-air finding and is not approved.

## Gate 2 import checks

Reject:

- unknown segment or revision
- a stale bound hash
- a locked segment whose inputs changed without a new revision
- a fix with an invalid or out-of-bounds range
- a mask correction without object identity or prompt context
- an approval while a required segment QA check fails
- a decision that names an asset absent from the project asset manifest

## QA warning overrides

An operator may document a reviewed warning with `schemas/qa_override.schema.json`:

```text
videoedit approve-qa-override PROJECT_ID \
  --qa-report PATH \
  --finding FINDING_ID=EVIDENCE_PATH \
  --actor TEXT --role TEXT --reason TEXT
videoedit check-qa-override PROJECT_ID --qa-report PATH --override PATH
```

The artifact binds the current QA report and every retained evidence file by SHA-256,
records the exact warning finding IDs, and records the operator classification. It is
not valid for a hard failure or skipped check. The assessment command preserves the
original report and reports unresolved required findings; a ready assessment still
requires the current human Gate 2 or Gate 3 decision. Gate 2 includes the override hash
when it is used, and segment locking revalidates the target and evidence before it can
promote the lock.

## QA operator review packets

`qa-review-packet` creates a revision- and hash-bound pending-only packet from the
current final QA, rendered join QA, and segment QA reports. It includes every join
with a warning signal, every segment warning finding, retained preview/report file
hashes, exact preview ranges where available, and grouped machine-warning counts.
The packet is an inspection aid: every decision is `pending`, and it cannot approve
an override, Gate 2, Gate 3, delivery, or cleanup.

```text
videoedit qa-review-packet PROJECT_ID \
  --candidate PATH \
  --final-qa PATH \
  --join-qa PATH \
  --segment-qa PATH \
  --revision-id rev_002 \
  --review-gate gate3
```

Inspect the generated JSON and Markdown packet. Repair decisions must create a new
revision and repeat render, re-transcription, and QA. A reviewed non-defect or
intentional-static decision may be recorded only through the current human QA
override workflow, with retained evidence and exact current hashes.

## QA decision records

`record-qa-review` records explicit human decisions against the exact packet hash:

```text
videoedit record-qa-review PROJECT_ID \
  --packet PATH \
  --decision ITEM_ID=DECISION \
  --actor TEXT --role TEXT --reason TEXT
```

Decisions may be recorded in batches. A partial record remains incomplete; a
`repair` or `reject_candidate` decision produces `changes_requested`, while a
fully classified non-defect batch produces `reviewed`. The record is not itself a
QA override or a Gate 3 approval. Repairs must flow through a new revision, and
non-defect classifications still require the separate current `qa_override` and
Gate 3 workflows.

## QA visual evidence

`qa-review-visuals` creates deterministic visual context for every join item in a
pending packet:

```text
videoedit qa-review-visuals PROJECT_ID \
  --packet PATH \
  --revision-id rev_002
```

The contact sheets are bound to the packet and candidate hashes and show the exact
join context at preview start, 250 ms before the boundary, the boundary, 250 ms after
the boundary, and preview end when those distinct in-range frames exist. A successful FFmpeg render is not a human decision;
the sheets must be inspected and any repair or classification must be recorded by a
human against the same packet. Stale previews, packets, candidates, or contact sheets
fail closed.

## Gate 3 final review

Gate 3 uses an approval record with approval type `final_delivery`. It binds to:

- final preview or candidate hash
- final QA report hash
- current edit decision list hash
- current effect plan hash
- current focus and pacing plan hash
- current retimed timeline hash
- project asset manifest hash
- composition code bundle hash
- selected delivery profile hash
- Gate 2 segment approval set hash

The reviewer records a complete watch-through or an explicitly approved equivalent review protocol. A changed render, plan, asset, code bundle, segment approval, QA report, or delivery profile makes Gate 3 stale.

## Cleanup approval

Cleanup uses a separate approval bound to:

- cleanup plan hash
- passing backup verification report hash
- current project and revision
- actor, role, reason, and time

Final delivery approval does not authorize cleanup.

## Version 2.2 review presentation

Gate 1 shows automatic micro-edit counts by type, saved duration, confidence bands, protected-content blocks, sample previews, all semantic proposals, and all structural transition proposals. The operator is not asked to approve every automatic micro edit.

Gate 2 shows failed joins, repaired joins, representative passing joins, transition previews, transition sound, and unresolved material findings. Each uncertain semantic item shows shared meaning, unique detail, the recommendation, and keep-first, keep-second, keep-both, remove, or modify choices where applicable.
