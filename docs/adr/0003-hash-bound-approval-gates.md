# ADR 0003: Hash-Bound Approval Gates

- Status: Accepted
- Date: 2026-07-23

## Context

Semantic cuts can change meaning. Visual effects can misrepresent the subject or cover important content. Final rendering, paid generation, and cleanup carry different risks. A generic confirmation is too broad and becomes stale when reviewed inputs change.

## Decision

Require immutable approvals bound to the hashes of the reviewed artifacts.

Editorial gates:

- Gate 1 approves the edit plan and effect plan.
- Gate 2 approves segment previews, transcript comparisons, effect assets, composition code, and segment QA.
- Gate 3 approves the final candidate, final QA, asset manifest, delivery profile, and relevant code bundle.

Separate approvals cover:

- paid generation with exact request hash, maximum amount, currency, and expiry
- cleanup with exact cleanup plan hash and verified backup evidence
- explicit QA override when policy permits an override

Changing any bound input invalidates the related approval.

## Consequences

Positive:

- clear accountability
- stale decision detection
- stronger editorial control
- bounded spending
- safe cleanup
- reproducible final readiness

Costs:

- more review artifacts
- more state transitions
- required reapproval after material changes
