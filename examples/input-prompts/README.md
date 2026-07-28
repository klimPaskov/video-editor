# Input prompt examples

## Extended cookbook

Start with the first prompt, then use only the templates that match the next
decision. A prompt requests work or evidence; it does not grant approval.

### Start and plan

- [`04-dense-edit-policy.md`](04-dense-edit-policy.md) - smart-dense cleanup,
  exact cuts, and review questions.
- [`12-project-brief-template.md`](12-project-brief-template.md) - reusable
  project brief with editorial, visual, and delivery constraints.

### Focus and visual treatment

- [`05-screen-recording-zoom.md`](05-screen-recording-zoom.md) - screen
  recording focus targets and targetless-zoom rejection.
- [`06-green-screen-layering.md`](06-green-screen-layering.md) - chroma key,
  text behind the subject, captions in front, and object recolor.
- [`07-captions-and-brand.md`](07-captions-and-brand.md) - local fonts,
  captions, safe areas, and visual composition.
- [`11-amd-preview.md`](11-amd-preview.md) - AMD acceleration for previews
  while keeping the production profile deterministic.

### Repair, review, and delivery

- [`08-repair-rejected-candidate.md`](08-repair-rejected-candidate.md) - make
  a new revision from immutable source after bad cuts or zooms.
- [`09-final-qa-and-watchthrough.md`](09-final-qa-and-watchthrough.md) - inspect
  the candidate and record review decisions without self-approval.
- [`10-delivery-and-cleanup.md`](10-delivery-and-cleanup.md) - delivery and
  cleanup as separate, hash-bound decisions.

Useful prompt habits:

1. Name the project and revision.
2. Give exact source times, visible evidence, or quoted words for every
   requested cut, speed-up, and zoom.
3. State what must not change.
4. Ask for a plan, evidence, and approval packet before asking for a render.
5. Require a stop when identity, timing, licensing, dependencies, or QA are
   uncertain.

These are copyable operator prompts, not executable scripts. Replace every
`<placeholder>` before use. Keep private paths and source media on the local
machine; never paste credentials into a prompt or commit them to the project.

- [`01-basic-recording.md`](01-basic-recording.md) — ingest and edit a
  talking-head or screen recording.
- [`02-prompt-writing-focus.md`](02-prompt-writing-focus.md) — request exact
  prompt-writing speed-ups and purposeful UI zooms.
- [`03-review-and-deliver.md`](03-review-and-deliver.md) — review a candidate,
  approve Gate 3, and finish delivery safely.
