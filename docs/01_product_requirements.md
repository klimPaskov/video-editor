# Product Requirements

## Product statement

Create a reliable local-first editing assistant for talking-head, green-screen, and screen-recording video. It should turn source media and a creative brief into an approved edit, branded visual composition, optional tracked effects, segment previews, and a reproducible final delivery without manual timeline editing.

## Users

### Primary operator

A creator or content operator who can review timecoded choices but does not want to edit a conventional timeline.

### Technical maintainer

A developer who installs dependencies, maintains adapters, adjusts policy, and diagnoses failures.

### Reviewer

A brand, legal, editorial, client, or final-delivery reviewer who approves the current artifact hashes.

## Jobs to be done

- Turn raw footage into a concise edit without losing intended meaning.
- Trigger effects from spoken content without ambiguous phrase matching.
- Apply consistent captions, graphics, backgrounds, and audio treatment.
- Track and transform visible objects when the effect is approved.
- Separate the subject from the background and place layers around the subject.
- Understand every proposed semantic deletion before it is executed.
- Review short segments and request exact fixes without opening a timeline editor.
- Detect editing mistakes through re-transcription and media QA.
- Limit paid generation and preserve provenance.
- Recover from failure without restarting the whole project.
- Reproduce the final result later.

## Functional requirements

### FR-001 Project creation

The system shall create a project with a stable identifier, configuration snapshot, directory layout, state record, and creation time.

### FR-002 Immutable ingest

The system shall register source media without modifying it. It shall compute a cryptographic hash, record file metadata, inspect streams, and reject unsupported inputs with an actionable reason.

### FR-003 Media proxies

The system shall create derived speech and optional edit proxies. It shall record source hash, exact command, output hash, and tool versions.

### FR-004 Transcription

The system shall transcribe speech into ordered segments and stable words with timestamps. It shall record model, language, device, adapter version, and available confidence evidence.

### FR-005 Silence analysis

The system shall detect silence or low-energy intervals with configurable thresholds. It shall retain raw detector output and normalized intervals.

### FR-006 Edit proposals

The system shall propose cuts using transcript, silence, policy, recording structure, and optional model reasoning. Each proposal shall contain a source range, exact excerpt, evidence, reason, confidence, meaning risk, continuity risk, handles, and approval requirement.

### FR-007 Protected content

The system shall protect low-confidence speech, names, numbers, dates, negation, disclosures, safety warnings, calls to action, and configured protected ranges from automatic semantic removal.

### FR-008 Effect proposals

The system shall propose transcript-triggered or explicitly timed effects with stable word IDs, source range, intended result, target prompt, renderer, worker, assets, fallback, risk, and approval requirement.

### FR-009 Gate 1 approval

The system shall require explicit approval for the current edit and effect plan. It shall support approve, reject, and modify decisions without editing proposal artifacts in place.

### FR-010 Canonical edit timeline

The system shall compile approved decisions into ordered source ranges to keep and a source-to-output time map. It shall reject overlaps, invalid order, negative durations, and out-of-bounds ranges.

### FR-011 Base render

The system shall render picture and production audio from the same approved edit timeline and preserve synchronization.

### FR-012 Audio processing

The system shall measure and normalize programme loudness using a configured profile. It shall record input and output measurements, clipping, peak, and relevant channel data.

### FR-013 Transcript rebasing

The system shall map retained transcript words and segments from source time to output time using the canonical mapping, including approved variable-rate prompt speed-up segments.

### FR-014 Captions

The system shall create branded captions from retained word timing. It shall support grouping, emphasis, safe areas, collision policy, and sidecar formats.

### FR-015 Visual timeline

The system shall create a schema-valid frame-driven visual timeline for Remotion. The timeline shall support backgrounds, text, images, video, audio, captions, picture-in-picture, motion graphics, tracked layers, and approved purposeful zoom keyframes with explicit easing.

### FR-016 Green-screen subject separation

The system shall support configurable chroma key, despill, alpha intermediate encoding, and subject composition for controlled green-screen footage.

### FR-017 Local mask effects

The system shall support lossless local masks for object recoloring and other pixel transforms. It shall validate dimensions, range, frame count, polarity, and alignment.

### FR-018 Object segmentation and tracking

The system shall support an isolated SAM 3.1 worker through a versioned job and result contract. It shall record prompts, frame range, masks, object geometry, missing frames, identity warnings, model identity, checkpoint identity, code revision, device, and hashes.

### FR-019 Person matting

The system shall support an isolated MatAnyone 2 worker through a versioned job and result contract. It shall accept an approved first-frame person mask and emit verified foreground and alpha outputs with model and quality metadata.

### FR-020 Object recoloring

The system shall recolor an approved tracked object while preserving texture, lighting, and the original shot fallback.

### FR-021 Object replacement

The system shall place a licensed replacement asset from reviewed track geometry. It shall support explicit occluder tracks and preserve the original shot fallback.

### FR-022 Layered subject composition

The system shall support background replacement and text placed behind the subject with front captions and labels above the subject.

### FR-023 Asset library

The system shall index local B-roll, sound effects, fonts, images, backgrounds, and replacement assets with hashes, descriptions, tags, media properties, licences, permitted uses, attribution, and usage history.

### FR-024 Asset retrieval

The system shall search approved local assets using transcript context and effect intent. It shall return ranked proposals with rationale and provenance.

### FR-025 Sound cues

The system shall plan and mix sound cues with source, licence, purpose, timing, gain, fades, ducking, collision rules, and fallback. Dialogue shall retain priority.

### FR-026 Optional provider jobs

The system shall keep network providers disabled by default. It shall submit, poll, download, validate, and cache paid or remote outputs only through provider-neutral adapters and current bounded approval.

### FR-027 Segment previews

The system shall render one preview per logical segment or effect group and include contact sheets, transcript excerpt, effect summary, mask or matte diagnostics, and QA findings.

### FR-028 Fix markers

The system shall import timestamped review markers for content, timing, mask, text, and audio changes. It shall apply changes as a new immutable revision.

### FR-029 Re-transcription

The system shall re-transcribe rendered segments and the final candidate, then compare intended and rendered speech for duplicate phrases, missing speech, unexpected speech, dead air, and timing drift.

### FR-030 Gate 2 approval

The system shall require approval for current segment revisions. Approval shall bind to preview, transcript comparison, assets, relevant composition code bundle, and QA hashes.

### FR-031 Quality assurance

The system shall run schema, decode, duration, stream, frame rate, audio, loudness, A/V sync, caption, mask, matte, safe-area, provenance, budget, approval, and cleanup-boundary checks.

### FR-032 Gate 3 approval

The system shall require final approval for the current final candidate and QA report. Approval shall bind to the current plan, asset manifest, composition code bundle, and delivery profile.

### FR-033 Final delivery

The system shall render configured masters and derivatives, compute checksums, and write a delivery manifest.

### FR-034 Publishing metadata

The system shall create caption sidecars, final transcript, chapter suggestions, and a short description draft from the final approved media.

### FR-035 Backup and cleanup

The system shall verify source and final delivery backups by hash. It shall delete only eligible derived files under explicit policy and separate cleanup approval.

### FR-036 Status and recovery

The system shall expose stage state, last error, retry eligibility, artifact locations, required operator actions, and cache status.

### FR-037 Phase evidence

The system repository shall require a schema-valid Codex phase result that records implementation scope, tests, acceptance evidence, risks, and skipped checks.

### FR-038 Purposeful screen focus

The system shall create dynamic zooms only for an opened window, prompt box, relevant cursor action, or important visible user interface. Every zoom shall record the visible target, target evidence, exact relevance boundaries, target-centered framing, smooth easing, stability confidence, policy result, and the fallback `no_zoom`. A missing clear target shall prevent the zoom.

### FR-039 Requested prompt speed-ups

The system shall create a speed-up only after an explicit operator request. The range shall contain only visible prompt writing or prompt dictation, begin and end on the exact action frames, exclude browsing, reading, waiting, loading, navigation, result inspection, cursor wandering, and unrelated actions, and retain audible pitch-adjusted production sound by default.

### FR-040 Focus and pacing confidence

The system shall calculate type-specific target, boundary, stability, action, transcript, and audio confidence. It shall batch material uncertainties into a concise review with recommendations and use `no_zoom` or `normal_speed` when a safe fallback resolves a low-confidence case.

## Nonfunctional requirements

### NFR-001 Reproducibility

A retained project shall be reproducible from source, configuration, approvals, local or retained provider assets, implementation versions, model pins, and recorded commands.

### NFR-002 Idempotency

A stage invoked with identical validated inputs shall reuse or reproduce the same valid artifact.

### NFR-003 Auditability

Every cut, effect, asset, worker job, provider request, approval, render, QA override, delivery, and cleanup action shall identify actor, time, reason, and related hashes.

### NFR-004 Reliability

A process crash shall not mark a partial artifact complete. Retry shall resume at the earliest invalid stage.

### NFR-005 Security

Secrets shall stay outside project artifacts and logs. Network and provider access shall be disabled by default.

### NFR-006 Performance

The implementation shall stream large files, avoid loading complete media into memory, use bounded concurrency, and cache expensive stages by exact inputs.

### NFR-007 Portability

Core contracts and domain logic shall not depend on one operating system, one provider, or one model implementation.

### NFR-008 Observability

Logs shall include project, revision, stage, attempt, command, elapsed time, output size, and relevant device information.

### NFR-009 Accessibility

Caption sidecars shall be delivered. Review reports shall use readable text and exact timecodes.

### NFR-010 Maintainability

The repository shall keep domain rules, services, adapters, CLI, Remotion, and GPU workers separate.

### NFR-011 Visual determinism

Remotion renders shall use frame-driven animation, local assets, fixed fonts, explicit props, and recorded code bundle identity.

### NFR-012 Privacy

Private source media, transcripts, masks, mattes, previews, and identifiers shall remain local unless an explicit policy and approval allows upload.

### NFR-013 Licence traceability

Every non-source asset, model, checkpoint, font, and provider output shall have a recorded source and licence decision before final delivery.

## Default acceptance scenario

Given a supported short green-screen talking-head clip with two long pauses, one false start, one held object, a brand configuration, a background image, a replacement object image, and a local object mask:

1. Ingest succeeds and source hash is retained.
2. Transcript and silence artifacts validate.
3. Pause, false-start, and effect proposals reach Gate 1.
4. No semantic cut is self-approved.
5. Approved keep ranges render without decode or sync errors.
6. Dialogue meets the configured loudness profile.
7. Captions use the configured font, colours, and safe margins.
8. The background and middle text plate render in Remotion.
9. FFmpeg isolates the green-screen subject.
10. The object is recolored through the local mask.
11. The subject composites above the middle text.
12. Front captions render above the subject.
13. A deliberate duplicate phrase is detected after a segment re-transcription fixture.
14. Gate 2 binds to the current segment evidence.
15. Final QA and Gate 3 block delivery when stale or failed.
16. Delivery includes the master, captions, transcript, chapters, QA report, manifest, and checksums.
17. Cleanup remains blocked until backup verification and approval.

## Success measures

- operator review time per five minutes of source
- percentage of cut proposals accepted without modification
- percentage of projects passing automated QA on first preview
- duplicate or missing speech findings per project
- A/V drift failures per one hundred renders
- visual effect revision count by effect type
- worker mask or matte acceptance rate
- provider spend blocked before unauthorized submission
- percentage of stages resumed without full reprocessing
- caption correction rate
- final delivery reproduction success rate
- elapsed time by phase on the target hardware

## Version 2.2 smart editing requirements

- The system shall actively seek frequent small edits and shall not suppress safe mechanical candidates to minimize cut count.
- The system shall distinguish filler, stutter, false start, self-correction, repetition, take selection, silence, dead air, noise, and semantic deletion.
- Every applied cut shall have a join strategy and rendered join-QA result.
- The system shall re-transcribe around rendered joins and detect missing, duplicate, or damaged speech.
- A high cut count shall be allowed when meaning, cadence, audio, and visual continuity pass.
- Motion transitions shall be limited to verified structural boundaries and shall have clean-cut fallbacks.
- Transition sounds shall be licensed, synchronized, speech safe, and QA checked.
