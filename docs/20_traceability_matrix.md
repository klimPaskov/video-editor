# Traceability Matrix

This matrix links each requirement to the current backlog, persisted contract, and acceptance evidence. Update it whenever `TASKS.md`, a public schema, or a release gate changes.

## Final workflow scope

The required path is P0-P6, U21/U22, P9, P10, and P11. The production path
keeps screen media local and uses reviewed timeline layers. Deferred
integrations are outside this matrix and cannot block release acceptance.

## Functional requirements

| Requirement | Main backlog items | Main contracts or configuration | Required evidence |
|---|---|---|---|
| FR-001 Project creation | P1-01, P1-07 | project manifest, stage state | project initialization, locking, revision layout, and interrupted setup tests |
| FR-002 Ingest | P1-02 through P1-05, P1-08 | source manifest, media probe | source hash, mutation rejection, duplicate ingest, corrupt input, and unsupported media tests |
| FR-003 Media proxies | P1-06, P1-07 | source manifest, render metadata | proxy probe, command record, source-hash binding, and atomic promotion tests |
| FR-004 Transcription | P2-01 through P2-03, P2-05, P2-07 | transcript | timestamp bounds, model and device record, fake adapter, local adapter, and interruption tests |
| FR-005 Silence analysis | P2-04, P2-05, P2-07 | silence intervals, editing policy | parser fixtures, interval normalization, raw evidence retention, and threshold boundary tests |
| FR-006 Edit proposal | P3-01 through P3-03, P3-06 | edit proposals, editing policy | conservative proposal benchmark, protected-content tests, and readable review export |
| FR-007 Edit approval | P3-06 through P3-10 | edit review decisions, approval record | approve, reject, modify, missing decision, unsupported change, and stale-hash tests |
| FR-008 Canonical timeline | P3-09, P3-10, P4-01, P4-05 | edit decision list | monotonic keep-range properties, source-to-output mapping, and range boundary tests |
| FR-009 Rough render | P4-01 through P4-03, P4-06 through P4-08 | edit decision list, render manifest | full decode, expected duration, stream maps, and synthetic synchronization fixture |
| FR-010 Audio processing | P4-01, P4-02, P4-04, P4-07 | delivery profile, render manifest, QA report | two-pass loudness measurement, true peak, clipping, and drift thresholds |
| FR-011 Captions | P4-05, P5-04, P5-07, P5-09, P10-06, P11-06 | transcript, caption plan, brand profile | grouping snapshots, glyph checks, safe-area checks, timing comparison, and sidecar delivery |
| FR-012 Sound cues | P9-05 through P9-09 | sound plan, asset index | licence and hash checks, gain limits, fade, ducking, clipping, and speech-priority tests |
| FR-013 Motion cues | P3-04, P3-05, P5-01 through P5-09, P9-07 | effect plan, visual timeline, motion plan, brand profile | TypeScript validation, still and segment renders, z-order, safe-area, and collision checks |
| FR-014 B-roll plan | P3-04, P3-05, P9-05 through P9-09 | B-roll plan, asset index | provenance, relevance, density, spacing, usage history, and fallback checks |
| FR-015 Spend approval | P9-10 | approval record, budget configuration | exact request hash, expiry, insufficient budget, and disabled-network tests |
| FR-016 Provider jobs | P9-10 and any accepted provider adapter task added under P9 | provider job, approval record | fake lifecycle, idempotent retry, duplicate prevention, validated download, and bounded live smoke test |
| FR-017 Preview | P5-08, P6-08, P10-01, P10-02 | render manifest, QA report | short segment decode, contact sheet, transcript excerpt, effect diagnostics, and readable warnings |
| FR-018 Revision | P10-03, P10-04, P10-08, P10-09 | review markers, project state, approvals | dependency invalidation, locked segment, stale approval, and revision history tests |
| FR-019 Quality assurance | P4-07, P5-09, P6-06, P6-08, P10-05 through P10-07, P11-02 | QA report, delivery profile | media, speech, caption, z-order, provenance, approval, and budget checks |
| FR-020 Final delivery | P11-01 through P11-06 | final approval, delivery manifest | hash-bound approval, complete render, checksum, sidecar, metadata, and reproduction test |
| FR-021 Status and recovery | P0-03 through P0-06, P0-09, P1-07, P11-09 | phase result, stage state, structured CLI output | interruption, cancellation, retry, doctor, status, and operator-action tests |
| FR-022 Cleanup | P11-07 through P11-09 | retention policy, cleanup approval, backup report | dry run, path boundary, source exclusion, explicit approval, and backup checksum proof |
| FR-023 Effect plan | P3-04 through P3-08, P3-10 | effect plan, approval record | trigger-word validation, bounded ranges, fallback, risk, asset, and stale approval tests |
| FR-024 Declarative visual timeline | P5-01 through P5-09 | visual timeline | JSON Schema and TypeScript agreement, frame mapping, asset verification, deterministic still and segment renders |
| FR-025 Segment review and self-verification | P10-01 through P10-09 | review markers, transcript, QA report | re-transcription comparison, duplicate and missing speech checks, fix import, and visual diagnostics |
| FR-026 Gate 2 approval | P10-08, P10-09 | segment review, QA report, approval record | current preview, transcript comparison, effect assets, composition bundle, QA hashes, and stale-approval tests |
| FR-027 Gate 3 approval | P11-02 through P11-04 | final approval, QA report | full review evidence, exact candidate hash, plan and asset hashes, and stale-approval tests |
| FR-028 Final delivery | P11-05, P11-06 | delivery manifest | master and derivative decode, checksums, sidecars, and reproduction evidence |
| FR-029 Publishing metadata | P11-06 | final transcript, delivery manifest | chapters, description draft, caption sidecar, and timestamp validation |
| FR-030 Backup and cleanup | P11-07 through P11-09 | backup verification, cleanup plan, approval record | hash-verified backup, dry run, path boundaries, source exclusion, and explicit cleanup approval |
| FR-031 Status and recovery | P0-03 through P0-06, P0-09, P11-09 | phase result, stage state, structured CLI output | interruption, cancellation, retry, stale lock, and operator-action tests |
| FR-032 Phase evidence | P0-09 and every phase | Codex phase result | task IDs, changed files, tests, media evidence, warnings, blockers, and skipped checks |
| FR-033 Purposeful screen focus | P3-11 through P3-13, P5-10, P5-11, P10-10, P10-11 | focus and pacing plan, visual timeline, QA report | allowed target, evidence frames, exact relevance range, target centering, smooth easing, stability, edge coverage, and no-target fallback |
| FR-034 Requested prompt speed-ups | P3-11 through P3-13, P4-09, P4-10, P10-10, P10-12 | focus and pacing plan, retimed timeline, render manifest, QA report | explicit request, exact visible-action range, forbidden-activity exclusion, audible pitch-preserved audio, A/V sync, duration, re-transcription, and cue rebasing |
| FR-035 Focus and pacing confidence | P3-12, P3-13, P10-11, P10-12 | editing policy, focus and pacing plan, review contract | type-specific confidence, question cap, batched recommendations, safe fallback, rejection calibration, and missing-evidence blockers |

## Nonfunctional requirements

| Requirement | Main backlog items | Main mechanism | Required evidence |
|---|---|---|---|
| NFR-001 Reproducibility | P0-01, P0-02, P1-07, P4-06, P11-06 | lockfiles and versioned artifacts | rebuild from retained source, config, approvals, assets, and versions |
| NFR-002 Idempotency | P0-03, P0-04, P1-07, P1-08, P10-04, P10-09 | content-addressed stage keys and atomic completion | repeated-stage and crash-recovery tests |
| NFR-003 Auditability | P0-05, P0-09, P3-08, P4-06, P9-09, P11-04, P11-06 | phase results, approvals, job records, manifests | actor, reason, time, hash, version, command, and asset assertions |
| NFR-004 Reliability | P0-03, P0-04, P1-07, P10-04, P11-09 | typed errors, locks, atomic writes, resumable stages | failure injection for process exit, disk, corruption, cancellation, and stale locks |
| NFR-005 Security | P0-03, P0-05, P0-06, P9-10, P11-07, P11-08 | sandboxing, redaction, disabled network, bounded paths | secret redaction, argument-array, path traversal, budget, backup, and cleanup tests |
| NFR-006 Performance | P1-06, P4-08, P5-08, P10-01, P11-10 | proxies, caching, and resumable work | representative throughput, peak memory, disk, and resume benchmarks |
| NFR-007 Portability | P0-06, P1-04 | process boundaries and capability detection | supported operating-system matrix and clear unsupported capability reports |
| NFR-008 Observability | P0-05, P0-06, P1-07, P4-06, P10-02, P11-02 | structured logs, render and QA manifests | project, revision, stage, elapsed time, output size, warning, and version assertions |
| NFR-009 Accessibility | P2-06, P5-04, P10-02, P11-06 | caption plan, sidecars, readable review Markdown | caption sidecar delivery and nonvisual review evidence |
| NFR-010 Maintainability | all phases | ADRs, public schemas, adapter boundaries, project skills | architecture review, import-boundary tests, phase-sized diffs, and schema compatibility checks |

## Completion rule

A required-workflow requirement is complete only when the implementation exists,
its persisted contracts validate, and the named evidence is retained. Deferred
integration contracts do not change the supported workflow scope.

## Version 2.2 traceability additions

| Requirement | Contract or configuration | Required evidence |
|---|---|---|
| Frequent safe micro edits | `edit_proposals`, smart-dense policy | dense speech fixture and proposal metrics |
| Protected meaning | protected-content checks | negative fact and qualification fixtures |
| Clean rendered joins | `join_qa_report` | decoded previews and re-transcription |
| Structural transitions only | `transition_plan` | valid and invalid boundary fixtures |
| Synchronized licensed swoosh | transition and sound plans | waveform, transient, mix, and licence evidence |
| High cut count allowed | pacing QA policy | passing dense fixture without quality defects |

The U22 fixture catalog and retained media evidence are version-bound at
`tests/fixtures/u22_required_cases.json` and
`.codex/evidence/U22/required-fixtures/`. The catalog is consumed by
`tests/contract/test_u22_fixtures.py`; the retained evidence includes decoded
FFmpeg renders and contact sheets for dense edits, clean and broken joins, a
screen-state jump, and a good structural transition. Planner evidence in
`transition-fixture-evidence.json` records the clean-cut decisions for the
random and overlapping-dialogue negatives. The existing join-QA and transition
sound reports remain the authoritative schema-valid re-transcription and
licensed-sound evidence.
