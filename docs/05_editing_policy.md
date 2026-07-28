# Editing Policy

## Purpose

This policy governs what the system may propose, what it may execute automatically, and what requires review.

## Policy modes

### Conservative

- Remove only leading, trailing, and long inter-sentence silence that passes all safety rules.
- Require approval for every semantic cut.
- Use generous handles.
- Disable filler and repetition deletion.

### Standard

- Auto-eligible mechanical cuts can be applied after a project-level approval.
- Semantic cuts still require item-level approval.
- Handles can be smaller when confidence is high.

### Aggressive

- More pause candidates become auto-eligible.
- Filler and immediate repetition proposals are enabled.
- Item-level approval remains required for meaning-bearing deletion.

### Smart dense

- Scan every transcript span and silence interval.
- Emit all qualifying mechanical micro edits.
- Permit many nearby small cuts when each join remains clean.
- Use project-level policy approval for high-confidence low-risk work.
- Require rendered join QA for every applied cut.
- Treat cut density as a warning signal, not a cut-count cap.

The default for new projects is `smart_dense`. Conservative and standard remain available for sensitive material.

## Evidence classes

### Mechanical evidence

- silence detector interval
- low audio energy
- transcript word gap
- leading or trailing media position
- scene boundary
- mouth movement or speech activity signal when implemented

### Semantic evidence

- false start
- abandoned phrase
- exact or near repetition
- tangent
- correction
- housekeeping
- weak hook material

Mechanical evidence can establish that a gap exists. It cannot establish that the gap has no editorial purpose.

## Protected content

The system must not automatically delete:

- numbers, prices, dates, names, quotations, or legal claims
- negation or qualification
- safety warnings
- disclosures
- the only statement of a key fact
- content near transcript uncertainty
- content with overlapping speakers
- intentional dramatic pauses marked by policy or reviewer
- content inside protected time ranges

## Cut candidate fields

Every candidate needs:

- source start and end
- proposed cut start and end after handles
- transcript before, inside, and after
- evidence
- reason code
- confidence
- meaning-risk level
- continuity-risk level
- approval requirement
- policy version

## Mechanical cut eligibility

A silence cut can become auto-eligible only when all conditions pass:

- duration meets the configured threshold
- it does not overlap speech words after timing tolerance
- it is outside a protected range
- it preserves minimum pre-roll and post-roll handles
- it does not reduce a sentence boundary below minimum pacing
- adjacent words have acceptable timing confidence
- the resulting cut is longer than the minimum useful edit
- the cut will not create a video flash shorter than the minimum kept duration

## Semantic proposal policy

A semantic planner can recommend a deletion. The planner cannot approve it.

The response must identify exact words and explain:

- why the material is removable
- what meaning remains
- what meaning could be lost
- whether audio or visual continuity is at risk
- a less aggressive alternative

The policy engine independently validates the returned range and risk.

## Handles

Example defaults:

```yaml
editing:
  pre_handle_ms: 120
  post_handle_ms: 160
  minimum_kept_segment_ms: 500
  minimum_cut_ms: 180
```

These values are starting points, not universal truths. Speech pace, room tone, frame rate, and edit style affect suitable values.

## Merging cuts

Two candidates can merge only when:

- the gap between them contains no protected content
- the combined deletion preserves sentence sense
- the kept interval between them is shorter than the configured minimum
- the merged result is no riskier than the separate cuts
- approval coverage remains valid

## Timeline invariants

- Keep ranges are the source of truth.
- Deleted ranges are derived for audit and review.
- A retained source instant maps to one output instant.
- Output mapping is monotonic.
- The same mapping applies to video, production audio, transcript words, and source-linked cues.
- Rounding cannot make keep ranges overlap.

## Review presentation

For each cut, show:

- proposal identifier
- source timecode
- duration saved
- transcript before and after
- deleted words
- evidence and reason
- confidence and risk
- a preview around the cut when available
- approve, reject, and modify options

## Approval scopes

- Item approval applies to one proposal hash.
- Batch approval can cover only mechanical proposals that already pass policy.
- Policy approval can authorize a named policy version for one project revision.
- Final approval covers one preview and QA report hash.

## Stale approvals

An approval becomes stale when any bound content changes, including:

- proposal range
- transcript excerpt
- policy version
- edit decision list
- preview render
- provider request
- estimated cost when it exceeds the approved amount

## Model prompt safety

The semantic planner prompt should:

- provide a bounded transcript window
- include protected categories
- forbid invented words and timecodes
- require references to supplied word identifiers
- request a conservative alternative
- use a strict output schema
- limit the number and total duration of proposals

## Operator interaction policy

High-confidence, low-risk mechanical work may proceed under the applicable approval scope. Medium-confidence or material-risk items are batched into one concise review with a recommendation, evidence, and safe fallback. Low-confidence items with a safe fallback use that fallback and appear in the summary. Low-confidence items without a safe fallback require one focused decision. Meaning, identity, spending, final delivery, and destructive actions always require explicit approval.

The default review round contains no more than five questions. Items are ranked by impact. Do not ask about a low-impact issue that can safely remain unchanged.

## Focus and pacing policy

Purposeful zoom and prompt speed-up policy is defined in `docs/29_purposeful_focus_and_prompt_speedups.md`. A zoom without a clear visible target is omitted. A speed-up without an explicit request is blocked.

## Metrics

Track:

- proposal acceptance rate by type
- modified proposal rate
- false deletion reports
- average saved duration
- continuity defects found in QA
- confidence calibration by proposal type
- operator review time

A higher cut rate is not a success metric by itself.

The `qa-edit-metrics` report also records retained-fragment distribution, speech cadence change, structural transition frequency, and repetition candidates. These are diagnostic signals only: a dense cut is acceptable when meaning, cadence, audio, and visual continuity pass, while a detected defect still routes to repair or review.

## Smart-dense execution rule

The planner must not avoid useful cuts merely because a segment already contains several edits. It should remove clear fillers, stutters, false starts, exact repetitions, dead air, and excess pause while preserving natural breath and cadence. Every automatic cut needs protected-content clearance, type-specific confidence, low risk, and a valid join strategy.

The canonical smart-dense pass scans every transcript word/span and every silence interval. It emits bounded evidence-based candidates for filler words and phrases, stutters, false starts, abandoned phrases, self-corrections, exact/near/semantic repetitions, duplicate or weak takes, dead air, accidental-noise events, and housekeeping. Uncertain or meaning-bearing candidates remain review items with a keep-original fallback; an explicit `noise_events` list in the silence artifact is required before accidental-noise candidates can be proposed.

Take ranking is a recommendation layer, not approval. It requires explicit component evidence for completeness, pronunciation, factual correctness, delivery, audio quality, gesture continuity, and screen-state continuity. Close scores are batched for review instead of being auto-selected.

Meaning-bearing deletion, near repetition, semantic repetition, close take scores, and uncertain continuity remain reviewable. Questions are batched. The operator does not approve each obvious micro edit.

## Complete proposal taxonomy

`leading_silence`, `trailing_silence`, `long_pause`, `dead_air`, `filler_word`, `filler_phrase`, `stutter`, `false_start`, `abandoned_phrase`, `self_correction`, `immediate_repetition`, `exact_repetition`, `near_repetition`, `semantic_repetition`, `duplicate_take`, `weak_take`, `tangent`, `housekeeping`, `accidental_noise`, and `hook_tightening`.

## Join policy

Every applied cut records a join strategy and produces a rendered preview. Re-transcription, audio continuity, visual continuity, semantic coherence, and pacing checks must pass. The workflow may repair a join with handles, a short audio crossfade, room tone, J-cut, L-cut, B-roll, alternate coverage, or a purposeful punch-in.

## Transition policy

Motion transitions are structural, not decorative join repair. Use them only for the purposes and limits in `config/transitions.example.yaml`. A weak or uncertain proposal falls back to a clean cut.
