# Smart Micro Edits and Purposeful Transitions

## Goal

The editor should remove friction at a fine scale. It should make many small edits when evidence is strong, then prove that the result still sounds natural and reads clearly. Caution must reduce bad deletions, not suppress useful work.

## Smart-dense policy

The planner scans every word, silence interval, nearby phrase pair, take boundary, and rendered join. It emits all qualifying mechanical candidates. It may apply high-confidence low-risk candidates under a hash-bound project policy approval. It groups meaning-bearing uncertainty into a short review.

The following are first-class types:

- leading and trailing silence
- long pause and dead air
- filler word and filler phrase
- stutter and repeated word start
- false start, abandoned phrase, and self-correction
- exact, immediate, near, and semantic repetition
- duplicate take and weak take
- tangent, housekeeping, accidental noise, and hook tightening

## Filler and breath handling

A filler is removable when word timing is reliable, the word is isolated, adjacent phonemes are protected, the visual join is viable, and the resulting cadence is natural. A filler can remain when it carries emotion, thoughtfulness, or rhythm, overlaps speech, or creates a visible or audible defect when removed.

Breaths remain protected by default. The system may shorten excess dead air around a breath while retaining enough inhale and room tone for natural delivery.

## Pause handling

Silence detection and transcript gaps provide separate evidence. The policy should usually trim a long pause to a configured natural pause instead of deleting it completely. A pause at a chapter boundary, emotional beat, warning, or deliberate reveal remains protected.

## Repetition and take selection

Exact repetition can become automatic when the duplicate is clear and the join passes. Near and semantic repetition require a meaning comparison. The review must identify shared meaning, unique details, and the recommended version.

Take selection scores completeness, pronunciation, factual correctness, confidence of delivery, filler burden, audio quality, gesture compatibility, screen-state compatibility, and compatibility with adjacent speech. The last take is not assumed to be best.

## Protected content

Automatic deletion is forbidden when a candidate includes or could alter:

- facts, names, dates, prices, numbers, quotations, or citations
- negation, qualification, uncertainty, comparison, or causality
- disclosures, warnings, promises, or legal and safety language
- the only statement of a key point
- low-confidence words or overlapping speakers
- intentional emotion, timing, or humor

## Join strategy

Every applied cut declares one strategy:

- hard cut
- hard cut with short production-audio crossfade
- adjusted handles
- room-tone insertion
- J-cut or L-cut
- B-roll cover
- alternate coverage
- purposeful punch-in

The planner selects the least intrusive strategy that solves the known continuity risk. Decorative transitions are not join repair.

## Join QA

Render a preview with at least two seconds of context on each side. Re-transcribe the preview and compare it with the approved transcript. Inspect:

- missing, duplicate, or unexpected words
- clipped syllables and consonants
- grammar and semantic coherence
- clicks, pops, room-tone changes, and noise-floor jumps
- speech rhythm and retained breaths
- black flashes, frozen or duplicate frames, and tiny retained fragments
- face, body, gesture, cursor, and screen-state continuity

A failed join returns to repair. Material uncertainty goes to review. A passing join can remain even when local cut density is high.

## Pacing

Cut density is a diagnostic, not a quota. The workflow records cuts per minute, average retained fragment length, local speech-rate change, repeated punch-ins, and visual-change frequency. It warns when the sequence looks rushed or unstable. It does not reject an edit only because it contains many cuts.

## Structural transitions

Motion transitions exist for clear structural changes. Allowed purposes include a new point, new chapter, major demonstration, comparison, before-after state, mode change, location change, or return from a major visual explanation.

Routine speech cleanup normally uses a hard cut, J-cut, L-cut, short crossfade, B-roll cover, or punch-in. A swipe should not hide a weak dialogue join.

Each motion transition records:

- outgoing and incoming segment identifiers
- structural purpose and transcript evidence
- exact output timing and duration
- direction and easing
- first readable incoming frame
- dialogue clearance
- sound cue, transient alignment, gain, and fades
- type-specific confidence and fallback

The fallback is usually a hard cut, J-cut, L-cut, or short crossfade.

## Transition sound

A motion transition may use a licensed soft whoosh, fast whoosh, deep whoosh, subtle swipe, chapter riser, soft impact, or reverse swell. The asset catalog records loudness, true peak, transient position, intensity, suitable transition types, speech safety, brand context, and minimum reuse interval.

The sound transient aligns with the strongest visual movement. The sound must not cover the first important word or create clipping. Reuse spacing prevents repetitive editing.

## Review contract

The operator should not answer one question per micro edit. The review contains:

- a summary of automatic edits by type
- sample previews of passing automatic edits
- all semantic deletions
- all failed or uncertain joins
- all motion transitions
- recommendations and safe fallbacks

## Required fixtures

The implementation must render positive and negative fixtures for fillers, stutters, false starts, exact repetition, semantic repetition, duplicate takes, pauses, clipped words, room-tone jumps, face jumps, screen-state jumps, structural transitions, random transitions, sound alignment, and dialogue masking.
