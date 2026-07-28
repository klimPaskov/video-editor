# Operator and Maintainer Runbook

## First installation

1. Follow `MANUAL_INSTALL_CHECKLIST.md`.
2. Install Python 3.11 and `uv`.
3. Install FFmpeg and ffprobe with the required filters, codecs, and alpha formats.
4. Install Node.js 22 and the Remotion dependencies.
5. Install or review the official Remotion skills.
6. Install the core project dependencies and run schema checks.
7. Run `videoedit doctor --json`.
8. Add licensed fonts and local assets through documented configuration.
9. Install the local Whisper extra before Phase 2.
10. Leave GPU workers and provider networking disabled until their gates.

## Normal local workflow

The intended public flow is:

```bash
videoedit project init projects/demo
videoedit ingest projects/demo raw/demo.mp4
videoedit plan projects/demo
videoedit review export projects/demo
videoedit approve edits projects/demo --from projects/demo/review/gate1-decisions.json
videoedit render rough projects/demo
videoedit effects plan projects/demo
videoedit remotion validate projects/demo
videoedit remotion render projects/demo --pass background
videoedit remotion render projects/demo --pass middle
# Optional deferred extensions only after a separate re-enable decision; the final workflow does not call these:
# videoedit segment projects/demo --effect EFFECT_ID
# videoedit matte projects/demo --effect EFFECT_ID --initial-mask MASK.png
videoedit preview segment projects/demo segment_intro
videoedit review import-fixes projects/demo projects/demo/review/fixes.md
videoedit verify speech projects/demo
videoedit qa projects/demo --render preview
videoedit approve segments projects/demo --from projects/demo/review/gate2-decisions.json
videoedit render final-candidate projects/demo
videoedit qa projects/demo --render final
videoedit approve final projects/demo --preview active --qa-report active
videoedit render final projects/demo
videoedit backup verify projects/demo
videoedit clean projects/demo --derived-only --dry-run
videoedit status projects/demo
```

The implemented P11 command sequence is:

```bash
videoedit assemble-final demo --segment-spec review/gate3/approved-segments.json
videoedit qa-final demo --assembly artifacts/final-assembly.json \
  --plan edit_decision_list=artifacts/edit-decision-list.json \
  --gate2 review/gate2/segment-000001.lock.json \
  --visual-evidence review/gate3/contact-sheet.png
videoedit record-watchthrough demo --candidate output/candidates/rev_001/final-candidate.mp4 \
  --actor reviewer@example.test --role editor
videoedit approve-gate3 demo --final-qa artifacts/final-qa.json \
  --watchthrough review/gate3/watchthrough.json \
  --asset-manifest artifacts/asset-manifest.json \
  --composition-bundle work/composition-bundle.js \
  --delivery-profile config/delivery-profile.json \
  --plan edit_decision_list=artifacts/edit-decision-list.json \
  --gate2 review/gate2/segment-000001.lock.json \
  --actor reviewer@example.test --role editor
videoedit write-publishing-metadata demo --candidate output/candidates/rev_001/final-candidate.mp4 \
  --caption-plan artifacts/caption-plan.json --transcript artifacts/transcript.json
videoedit publish-delivery demo --gate3 review/gate3/gate3-approval.json \
  --final-qa artifacts/final-qa.json --metadata artifacts/publishing-metadata.json \
  --delivery-profile config/delivery-profile.json --derivative mobile=1280x720
videoedit backup-verify demo --targets config/backup-targets.json
videoedit cleanup-plan demo
videoedit status demo
```

The example reviewer identities are placeholders. Gate 3, delivery, and cleanup must use the
current human decisions and exact paths produced by the preceding commands. A measured fixture
run is evidence for that fixture only; the earlier 45-minute estimate is not a performance
guarantee.

Some command names remain planned contracts until their phases are implemented. Once the first public release is published, preserve command names, exit codes, and JSON envelopes.

## Purposeful zoom and speed-up review

Before Gate 1, inspect `focus-pacing-plan.json` and its evidence frames. Confirm that every zoom has an allowed visible target, begins after the target appears, centers the target, and ends before unrelated content. Reject decorative or targetless zooms.

Prompt speed-ups are absent unless explicitly requested. Confirm that every accelerated range contains only visible prompt writing or dictation. Keep surrounding browsing, reading, waiting, loading, navigation, result inspection, cursor wandering, and other actions at normal speed. Production audio remains audible and pitch-preserved unless a separate reviewed exception says otherwise.

Typical commands after implementation:

```bash
videoedit focus plan projects/demo
videoedit review export projects/demo
videoedit approve focus projects/demo --decisions review/focus-decisions.json
videoedit timeline retime projects/demo
videoedit render preview projects/demo --focus
videoedit qa projects/demo --scope focus-pacing
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

## Review workflow

The review export should include:

- readable Markdown summary
- machine JSON decision template
- timecodes in source and output clocks where relevant
- transcript context
- thumbnails or short previews when available
- clear allowed decision values

A reviewer edits only the decision fields or uses a future review interface. The import command validates that the reviewed proposal hash is current.

## Paid generation workflow

```bash
videoedit plan assets PROJECT
videoedit costs PROJECT
videoedit approve spend PROJECT --max-usd 10 --expires-in 24h
videoedit generate assets PROJECT
```

Before generation, confirm:

- provider is allowed for the project data classification
- network access is enabled
- credential is available
- every request has a fallback
- estimate plus reserve is within the approval
- request plan hash matches the approval

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
videoedit clean PROJECT --dry-run --derived-only
videoedit clean PROJECT --derived-only
```

The dry run lists artifact identifiers, paths, sizes, and retention classes. Source and required reproducibility assets are excluded.

## Backup

Use a project export command when implemented. Verify checksums after copying. Confirm whether source media is included or referenced.

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
