# ADR 0011: Smart-Dense Editing and Purposeful Transitions

## Status

Accepted.

## Context

A cautious planner can avoid errors by making too few edits. That fails the product goal because the editor should remove frequent small problems. A motion-heavy planner can also hide ordinary cuts behind decorative transitions. That creates noise and weakens the structure.

## Decision

Use a `smart_dense` policy that emits all qualifying mechanical micro-edit candidates and allows high-confidence low-risk work under project-level policy approval. Require protected-content checks, an explicit join strategy, rendered preview, re-transcription, and join QA for every applied cut.

Treat cut density as a QA signal. Do not use a low cut count as a success metric or a high cut count as an automatic failure.

Represent structural transitions in a separate versioned plan. Motion transitions require an allowed structural purpose, exact incoming and outgoing segments, confidence, dialogue protection, licensed sound when required, and a clean-cut fallback. Routine cleanup cuts do not receive decorative motion.

## Consequences

- The system can make many small edits without asking about each one.
- Semantic deletion and risky joins remain reviewable.
- More render previews and QA work are required.
- Transition placement becomes auditable and consistent.
- Poor join quality cannot be excused by a successful command or a high confidence score.
