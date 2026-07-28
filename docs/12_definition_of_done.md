# Definition of Done

## Task done

A task is done when:

- implemented behavior matches its acceptance criteria
- public contracts are documented
- tests cover success and relevant failures
- format, lint, type, test, schema, and applicable media checks pass
- no source media or credentials enter the repository
- errors are actionable and secrets are redacted
- task-specific docs are updated
- the phase result records files, commands, evidence, risks, blockers, and decisions

## Stage done

A pipeline stage is done when:

- input and output contracts exist
- stage key and idempotency behavior are defined
- temporary output and atomic promotion are implemented
- interruption, cancellation, and retry are tested
- output schemas validate
- external processes run through typed adapters
- logs identify project, revision, stage, run, duration, and result
- cleanup behavior is defined
- operator status explains failure and next action

## Core editorial milestone done

The edit and audio core is done when:

- a project can ingest a supported source without mutation
- source integrity is checked throughout the run
- local transcription and silence analysis complete
- cut and effect proposals are reviewable
- semantic deletion cannot bypass Gate 1
- approved keep ranges render synchronized picture and production audio
- loudness meets the delivery profile
- transcript words are mapped to output time
- render manifests record commands, versions, hashes, duration, and warnings
- the base edit fully decodes and passes synchronization checks

## Focus and pacing slice done

The slice is done only when:

- the focus and pacing plan and retimed timeline validate
- old edit, effect, and visual timeline examples remain readable
- every zoom has an allowed purpose, visible-target evidence, exact relevance boundaries, target-centered framing, smooth easing, stability checks, and `no_zoom` fallback
- every speed-up has an explicit request, exact visible-action boundaries, excluded-activity proof, audible synchronized audio, pitch policy, and `normal_speed` fallback
- every downstream transcript word, caption, effect, B-roll cue, sound cue, zoom, and review timecode maps through the retimed timeline
- medium-confidence material decisions are batched with recommendations
- low-impact uncertainty uses a safe fallback without unnecessary questions
- positive and negative rendered fixtures pass visual and audio QA
- Gate 1 and Gate 2 approvals become stale when bound inputs change

A schema-valid plan without decoded media and inspected proof frames is not done.

## Local visual milestone done

The first complete visual milestone is done when:

- a schema-valid Remotion timeline renders from JSON props
- brand fonts, colors, captions, safe areas, and motion primitives are deterministic
- middle and front render passes have explicit z-order
- a green-screen or approved-mask subject can be placed over a new background
- text can be rendered behind the subject
- one object can be recolored through a supplied local mask
- production audio is preserved
- contact sheets and timecoded visual findings are produced
- the milestone works without credentials, paid providers, SAM 3.1, or MatAnyone 2

## SAM 3.1 worker done (optional deferred extension)

The SAM worker is done only if the optional extension is later re-enabled. It
is not a required final-workflow criterion. When enabled, it is done when:

- current licence, checkpoint, code revision, Python, PyTorch, CUDA, and hardware decisions are recorded
- the worker runs in its own environment
- job and result contracts validate
- jobs are bounded to approved ranges and prompt frames
- masks are lossless and frame aligned
- object IDs and geometry are retained
- missing masks, area jumps, leaks, jumps, and identity switches are reported
- fake contract tests pass
- one short licensed live target-GPU test passes human review

## MatAnyone 2 worker done (optional deferred extension)

The MatAnyone worker is done only if the optional extension is later re-enabled.
It is not a required final-workflow criterion. When enabled, it is done when:

- current licence, checkpoint, code revision, Python, and hardware decisions are recorded
- the worker runs in its own environment
- job and result contracts validate
- an approved first-frame person mask is required
- foreground and alpha roles are proved before use
- dimensions, frame count, hashes, and model identity are retained
- hair, fingers, clothes, holes, motion blur, and temporal stability are reviewed
- fake contract tests pass
- one short licensed live target-GPU test passes human review

## Object effects and local assets done

This stage is done when:

- approved tracks convert to bounded transform keyframes
- object replacement keeps an original-shot fallback
- occluder masks have explicit z-order
- uncertain tracking or occlusion fails visibly
- local assets have hashes, descriptions, tags, licences, attribution, and usage history
- B-roll and sound cues require approval and obey density and collision rules
- speech-priority mixing, fades, gain, ducking, and clipping checks pass
- no paid service is required for acceptance

## Optional provider adapter done

An optional paid provider adapter is done when:

- capabilities and unsupported options are explicit
- network is disabled by default
- estimate and hard budget checks run before submission
- spend approval is bound to the exact request hash and expiry
- submission retry cannot duplicate a paid job
- polling can resume after restart
- downloads are quarantined, decoded, and hashed
- actual spend is reconciled when available
- privacy, retention, licence, and factual-safety review paths exist
- provider removal does not break historical projects
- fake lifecycle tests and a bounded operator-approved live smoke test pass

## Review and delivery done

A final delivery is done when:

- Gate 2 approvals match current segment previews, transcript comparisons, assets, composition bundle, and QA hashes
- approved segment revisions assemble into the final candidate
- final loudness and required QA checks pass
- a complete watch-through or approved equivalent is recorded
- Gate 3 approval matches the final preview, QA report, plan, asset manifest, composition bundle, and delivery profile hashes
- final output fully decodes
- duration, frame rate, audio, A/V sync, captions, and visual checks meet profile thresholds
- caption sidecars, transcript, chapter suggestions, description draft, checksums, and delivery manifest exist
- source and final backups are verified by hash before cleanup becomes eligible
- cleanup uses a dry run, strict path boundaries, and explicit approval
- a retained project can reproduce the delivery

## Release done

A release is done when:

- supported platforms and dependencies are documented
- installation and upgrade paths are tested
- all required CI and manual suites pass
- open critical and high risks have owners and accepted treatment
- schema compatibility is declared
- security review covers paths, processes, secrets, network, spend, checkpoints, and cleanup
- licence review covers Remotion, models, fonts, sounds, B-roll, music, backgrounds, and replacement assets
- operator and maintainer runbooks are current
- known limitations and measured performance are published
- rollback instructions exist

## Version 2.2 completion criteria

- Smart-dense candidate generation covers the complete taxonomy.
- High-confidence micro edits can proceed under a current project policy approval.
- No automatic deletion changes protected meaning.
- Every applied cut has rendered join evidence and a schema-valid join-QA result.
- A dense fixture demonstrates many passing small edits without rushed speech or continuity defects.
- Motion transitions appear only at structural boundaries and have a valid fallback.
- Required transition sounds are licensed, synchronized, and speech safe.
- Negative cut and transition fixtures fail or route to repair as expected.
