# Operator and Maintainer Runbook

## First installation

For a creator setup, start with [`README.md`](../README.md),
[`INSTALL.md`](../INSTALL.md), and [`docs/31_user_quickstart.md`](31_user_quickstart.md).
The short version is:

1. Install Python 3.11, `uv`, Node.js 22, npm, FFmpeg, ffprobe, and Git.
2. Run `scripts/setup.ps1` on Windows or `scripts/setup.sh` on Unix-like systems.
3. Provision a local Whisper checkpoint explicitly.
4. Run `uv run videoedit doctor --json`.
5. Keep provider networking and optional workers disabled for the normal path.
6. Add only local assets whose permission/licence and attribution are recorded.

## Normal local workflow

These are the implemented core commands. Run them from the repository root;
use `videoedit --help` for the complete command list and options.

```powershell
$project = "my-tutorial-20260728"
uv run videoedit init $project
uv run videoedit ingest $project "C:\Users\me\Videos\my-tutorial.mp4"
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
uv run videoedit plan-focus-pacing $project --candidates review/focus-candidates.json
uv run videoedit status $project
```

After inspecting the Gate 1 packet, use the exact current decision paths:

```powershell
uv run videoedit approve-gate1 $project `
  --decisions projects/$project/review/edit-decisions.json `
  --effect-plan projects/$project/artifacts/effect-plan.json `
  --focus-pacing-plan projects/$project/artifacts/focus-pacing-plan.json `
  --revision-id rev_001 `
  --actor "human-editor"
uv run videoedit compile-edl $project --revision-id rev_001
uv run videoedit plan-joins $project
uv run videoedit qa-joins $project
uv run videoedit compile-retimed-timeline $project `
  --retime-plan projects/$project/review/focus-pacing-plan.json `
  --edl projects/$project/artifacts/edit-decision-list.json
uv run videoedit render-retimed $project --source projects/$project/raw/source.mp4
uv run videoedit compose-visual $project
uv run videoedit render-segment projects/$project/artifacts/visual-timeline.json `
  projects/$project/review/segment-000001.mp4 --start-frame 0 --end-frame 299
uv run videoedit qa-focus-pacing $project --focus-pacing-plan projects/$project/review/focus-pacing-plan.json
```

Do not copy these paths blindly: each project stores its current artifact names
and hashes. A prompt should ask Codex to resolve the exact paths and stop when
an approval is missing or stale.

The implemented P11 command sequence is:

```bash
uv run videoedit assemble-final demo --segment-spec review/gate3/approved-segments.json
uv run videoedit qa-final demo --assembly artifacts/final-assembly.json \
  --plan edit_decision_list=artifacts/edit-decision-list.json \
  --gate2 review/gate2/segment-000001.lock.json \
  --visual-evidence review/gate3/contact-sheet.png
uv run videoedit record-watchthrough demo --candidate output/candidates/rev_001/final-candidate.mp4 \
  --actor reviewer@example.test --role editor
uv run videoedit approve-gate3 demo --final-qa artifacts/final-qa.json \
  --watchthrough review/gate3/watchthrough.json \
  --asset-manifest artifacts/asset-manifest.json \
  --composition-bundle work/composition-bundle.js \
  --delivery-profile config/delivery-profile.json \
  --plan edit_decision_list=artifacts/edit-decision-list.json \
  --gate2 review/gate2/segment-000001.lock.json \
  --actor reviewer@example.test --role editor
uv run videoedit write-publishing-metadata demo --candidate output/candidates/rev_001/final-candidate.mp4 \
  --caption-plan artifacts/caption-plan.json --transcript artifacts/transcript.json
uv run videoedit publish-delivery demo --gate3 review/gate3/gate3-approval.json \
  --final-qa artifacts/final-qa.json --metadata artifacts/publishing-metadata.json \
  --delivery-profile config/delivery-profile.json --derivative mobile=1280x720
uv run videoedit backup-verify demo --targets config/backup-targets.json
uv run videoedit cleanup-plan demo
uv run videoedit status demo
```

The example reviewer identities are placeholders. Gate 3, delivery, and cleanup must use the
current human decisions and exact paths produced by the preceding commands. A measured fixture
run is evidence for that fixture only; the earlier 45-minute estimate is not a performance
guarantee.

The CLI help and the repository-specific command contract are the source of
truth. Historical phase notes may mention older command spellings; do not use
those as executable instructions.

## Purposeful zoom and speed-up review

Before Gate 1, inspect `focus-pacing-plan.json` and its evidence frames. Confirm that every zoom has an allowed visible target, begins after the target appears, centers the target, and ends before unrelated content. Reject decorative or targetless zooms.

Prompt speed-ups are absent unless explicitly requested. Confirm that every accelerated range contains only visible prompt writing or dictation. Keep surrounding browsing, reading, waiting, loading, navigation, result inspection, cursor wandering, and other actions at normal speed. Production audio remains audible and pitch-preserved unless a separate reviewed exception says otherwise.

Typical commands after implementation:

```bash
uv run videoedit plan-focus-pacing demo --candidates review/focus-candidates.json
uv run videoedit validate-focus-plan review/focus-pacing-plan.json
uv run videoedit compile-retimed-timeline demo --retime-plan review/focus-pacing-plan.json
uv run videoedit render-retimed demo --source projects/demo/raw/source.mp4
uv run videoedit qa-focus-pacing demo --focus-pacing-plan review/focus-pacing-plan.json
```

Use `[ZOOM start-end]` and `[SPEED start-end]` markers only as revision requests. Never edit an approved focus and pacing artifact in place.

## Project status

`videoedit status PROJECT` emits JSON and should report:

- project and revision
- source integrity
- current state
- completed stages
- invalidated stages
- running process
- last error
- required approvals
- spend status
- QA readiness
- final delivery paths

The immutable ingest source manifest retains its ingest revision when later edit revisions
are created; status therefore verifies its project and source hash without requiring the
ingest revision to equal the active edit revision. A current review-scoped final QA report is
included in readiness evaluation, but a non-ready report does not count as Gate 3 approval.
The status warnings include `gate1_approval_missing_or_stale` whenever no schema-valid,
approved Gate 1 edit record is bound to the active revision; an older revision's approval is
never treated as current.

## Review workflow

The review export should include:

- readable Markdown summary
- machine JSON decision template
- timecodes in source and output clocks where relevant
- transcript context
- thumbnails or short previews when available
- clear allowed decision values

A reviewer edits only the decision fields or uses a future review interface. The import command validates that the reviewed proposal hash is current.

## Paid or remote providers

Paid generation is outside the normal local path and remains disabled by
default. If a future project explicitly selects a provider, use the typed
`plan-provider-job` and `submit-provider-job` commands only after recording a
current bounded spend approval, network decision, credential state, retention
terms, fallback, and idempotency key. Do not use a provider to compensate for
an unresolved local QA failure.

## Failure: dependency missing

Symptoms:

- doctor reports missing binary or capability

Action:

1. Read the exact binary path and check code.
2. Install or configure the supported dependency.
3. Re-run doctor.
4. Do not bypass a required capability check for production work.

## Failure: source changed

Symptoms:

- source hash or size no longer matches

Action:

1. Stop the project.
2. Determine whether the source was intentionally replaced.
3. Create a new project or explicit source revision.
4. Do not overwrite the recorded hash.

## Failure: transcription timing invalid

Symptoms:

- words outside media duration
- nonmonotonic timestamps
- large timing uncertainty

Action:

1. Inspect the audio proxy.
2. Confirm model, language, and tool version.
3. Retry with a supported model profile.
4. Keep uncertain ranges protected.
5. Do not hand-adjust canonical times without a recorded correction artifact.

## Failure: rough render duration mismatch

Action:

1. Compare expected timeline duration with probe and decode values.
2. Inspect frame-rate rational values.
3. Inspect rounding at each cut boundary.
4. Confirm picture and sound use the same keep ranges.
5. Reproduce with the smallest failing fixture.
6. Add a regression test before changing tolerance.

## Failure: A/V drift

Action:

1. Check source stream start times.
2. Check variable frame rate handling.
3. Inspect audio resampling and filter graph timestamps.
4. Compare drift after each concatenated segment.
5. Avoid hiding drift by expanding the allowed threshold.

## Failure: missing font or glyph

Action:

1. Run the font capability check.
2. Confirm the font file and license.
3. Confirm the glyph exists for the language.
4. Use the configured approved fallback font.
5. Re-render caption proof and QA.

## Failure: provider job unknown after submit

Action:

1. Do not submit again.
2. Inspect the persisted local job and provider response.
3. Use provider status or history lookup with the recorded identifier or idempotency key.
4. Escalate when status cannot be established.
5. Mark the job for manual reconciliation.

## Failure: provider download corrupt

Action:

1. Keep the remote job record.
2. Remove only the quarantined partial download.
3. Retry download within policy.
4. Validate the complete file.
5. Do not spend on a new generation unless the existing job is confirmed unusable.

## Failure: budget blocked

This is expected behavior when no valid approval covers the current estimate. Review the request plan, reduce scope, or create a new approval. Do not edit the state database directly.

## Failure: QA fails

1. Keep the preview.
2. Read the machine and readable findings.
3. Apply the suggested repair when safe.
4. Create a revision when editorial content changes.
5. Re-run only invalidated stages.
6. Use an override only with actor, reason, affected check, and preview hash.

## Cancellation

A cancellation should:

- signal the child process group
- wait for a bounded grace period
- force termination when needed
- mark the stage cancelled
- retain logs
- remove partial outputs
- keep prior complete artifacts

## Disk cleanup

```bash
uv run videoedit cleanup-plan PROJECT
uv run videoedit approve-cleanup PROJECT --cleanup-plan PATH --actor NAME --reason TEXT
uv run videoedit execute-cleanup PROJECT --cleanup-plan PATH \
  --approval PATH --backup-verification PATH
```

The cleanup plan is a dry run. It lists artifact identifiers, paths, sizes,
hashes, retention classes, and recoverability. Source media, valid revisions,
active approvals, failed evidence, and required reproducibility assets are
excluded. Execution requires a separate current cleanup approval and a passing
backup verification.

## Backup

Run `uv run videoedit backup-verify PROJECT` with the configured target file.
Verify checksums after copying and confirm whether the backup includes a source
copy or an immutable source reference. Cleanup remains blocked until this
verification passes.

## Upgrade

Before an application or dependency upgrade:

1. Export or back up active projects.
2. Run schema compatibility tests.
3. Run the end-to-end fixture on the old version.
4. Upgrade in a test environment.
5. Re-run fixtures and compare manifests.
6. Record changed binary and model versions.
7. Roll out only after acceptance.

## Support bundle

A support bundle should contain:

- redacted configuration
- doctor output
- project and stage identifiers
- state summary
- relevant manifests
- redacted logs
- QA findings
- tool versions

It should exclude source media, transcripts, credentials, and provider prompts unless the operator explicitly includes them.

## Smart-dense edit run

1. Confirm the active policy and its approval hash.
2. Generate all mechanical micro-edit candidates and bounded semantic candidates.
3. Create the smart-dense review batch. Approve the policy explicitly before any high-confidence mechanical batch is authorized; review only the ranked material questions, and retain keep-original fallbacks for low-impact uncertainty.
4. Review the summary, semantic items, and any blocked protected content.
5. Compile keep ranges and assign join strategies.
6. Render join previews before a full segment render.
7. Re-transcribe and run join QA.
8. Repair failed joins and rerun only affected previews.
9. Detect structural boundaries and create the transition plan.
10. Render transition previews with sound and run transition QA.
11. Continue to the normal segment and final gates.
