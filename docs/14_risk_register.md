# Risk Register

Scores use likelihood and impact from 1 to 5. Priority is their product.

| ID | Risk | L | I | Priority | Mitigation | Trigger | Owner |
|---|---|---:|---:|---:|---|---|---|
| R-001 | Source media is modified or deleted | 2 | 5 | 10 | Immutable source policy, hashes, cleanup allowlist, tests | Hash mismatch or missing source | Core maintainer |
| R-002 | A semantic cut changes meaning | 3 | 5 | Protected content rules, item approval, benchmark, conservative default | Reviewer rejects or reports altered meaning | Editorial owner |
| R-003 | Silence trimming removes intentional pacing | 3 | 4 | Speech-aware handles, protected pauses, review, policy modes | High modified-cut rate | Editorial owner |
| R-004 | Word timestamps are inaccurate | 4 | 4 | Confidence evidence, bounds checks, tolerances, fixtures, optional alignment upgrade | Timing QA warnings rise | Speech maintainer |
| R-005 | A/V drift accumulates across cuts | 3 | 5 | One timeline, rational rates, late rounding, sync fixture, hard QA | Drift exceeds threshold | Media maintainer |
| R-006 | Variable frame rate input breaks assumptions | 3 | 4 | Probe metadata, transcode strategy, VFR fixtures, explicit output frame rate | Duration or frame mismatch | Media maintainer |
| R-007 | Captions are unreadable or clipped | 3 | 4 | Safe areas, layout checks, proof render, language fixtures | Caption QA fail | Brand maintainer |
| R-008 | Missing font changes branding | 3 | 3 | Doctor check, approved fallback, font hash and license | Font capability fail | Brand maintainer |
| R-009 | Sound effects reduce speech clarity | 3 | 3 | Gain caps, ducking, cue review, loudness checks | Speech intelligibility complaint | Audio maintainer |
| R-010 | Motion renderer output is nondeterministic | 2 | 4 | Pin versions, frame tests, adapter spike, local fallback | Golden frame drift | Motion maintainer |
| R-011 | Motion HTML executes unsafe code | 3 | 5 | Restricted directory, network controls, trusted templates, sandbox | Unexpected network or path access | Security owner |
| R-012 | Generated B-roll is misleading | 3 | 5 | Factual sensitivity flags, review, safer fallback, provenance | Reviewer flags false implication | Editorial owner |
| R-013 | Provider retry duplicates paid generation | 3 | 5 | Persist-before-submit, idempotency, resume polling, fake failure tests | Duplicate remote jobs | Provider maintainer |
| R-014 | Provider price changes | 4 | 4 | Live estimate, reserve, hard maximum, no hard-coded prices | Estimate differs from config | Product owner |
| R-015 | Credential leaks in logs | 2 | 5 | Secret references, redaction, process input policy, tests | Secret scanner or incident | Security owner |
| R-016 | Untrusted media exploits a decoder | 2 | 5 | Updated dependencies, restricted process, protocol limits, quarantine | Security advisory or crash | Security owner |
| R-017 | Disk exhaustion corrupts a render | 4 | 4 | Estimate, quota, temporary cleanup, atomic promotion | Free space below threshold | Operations owner |
| R-018 | Long jobs cannot resume | 3 | 4 | Stage keys, persisted jobs, heartbeat, restart tests | Full rerun after crash | Core maintainer |
| R-019 | Model output violates schema | 4 | 3 | Structured output, strict validation, bounded retry, no-model fallback | Parse failure rate | Planner maintainer |
| R-020 | Provider interface changes | 4 | 3 | Adapter isolation, capability check, pinned smoke test | CLI or API failure | Provider maintainer |
| R-021 | Asset license is unknown | 3 | 5 | Catalog metadata, provenance gate, delivery QA | Missing license field | Legal or content owner |
| R-022 | Review approval becomes stale | 3 | 4 | Hash-bound approvals, invalidation rules, state checks | Reviewed item hash changes | Core maintainer |
| R-023 | QA produces too many false alarms | 3 | 3 | Severity tiers, benchmark, tune per profile, retain evidence | High override rate | QA maintainer |
| R-024 | Operator bypasses controls by editing state | 2 | 4 | Clear CLI, database validation, event audit, documented recovery | Inconsistent state | Operations owner |
| R-025 | Watch folder ingests incomplete copies | 4 | 3 | Atomic rename or stability window, lock file protocol, checksum | File changes during ingest | Operations owner |
| R-026 | Cross-platform FFmpeg behavior differs | 3 | 4 | Pin supported builds, platform CI, property checks | Platform-only failure | Media maintainer |
| R-027 | Transcript or media privacy is breached | 2 | 5 | Local default, classification, provider gate, retention policy | Unauthorized remote transfer | Security owner |
| R-028 | Final result cannot be reproduced | 3 | 5 | Tool versions, hashes, retained assets, manifests, reproduction test | Manifest mismatch | Release owner |

| R-029 | A speed-up includes browsing, reading, waiting, or result inspection | 3 | 4 | 12 | Require visible action evidence, exact text-change boundaries, normal-speed fallback, and re-transcription | Retimed range contains unrelated activity | Editorial owner |
| R-030 | A purposeful zoom targets the wrong UI or starts too early | 3 | 4 | 12 | Require target visibility, centered framing, relevance boundaries, smooth easing, and no-zoom fallback | Preview shows unrelated content or drift | Motion maintainer |
| R-031 | Captions or graphics hide important screen content | 3 | 4 | 12 | Safe-area checks, layout collision rules, still review, and segment preview | Prompt box or result becomes unreadable | Brand maintainer |
| R-032 | Picture-in-picture or B-roll obscures the active action | 3 | 4 | 12 | Explicit bounds, timing, z-order, collision review, and source fallback | Important UI is covered | Visual effects owner |
| R-036 | Transcript-triggered effect fires on the wrong phrase | 3 | 4 | 12 | Stable word IDs, quoted trigger context, source range review, Gate 1 | Effect appears at the wrong moment | Editorial owner |
| R-037 | Remotion use exceeds the selected licence terms | 2 | 5 | 10 | Manual licence review, decision record, upgrade check, release gate | Team or product scope changes | Product owner |
| R-038 | The reported 45-minute workflow target drives unsafe shortcuts | 3 | 4 | 12 | Measure target hardware, separate machine time from review time, keep quality gates fixed | Pressure to skip review or weaken QA | Product owner |

## Review cadence

- Review critical and high risks at every milestone.
- Review provider risks before any live integration change.
- Review media integrity and timing risks before every release.
- Convert realized risks into incidents and regression tests.
- Close a risk only with evidence. Do not close it because no recent failure was observed.
