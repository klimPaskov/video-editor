# Prompt: repair a rejected candidate

The candidate at `<absolute local candidate path>` for `<project-id>` revision
`<old-revision-id>` was rejected because:

- `<bad cut, rushed pacing, missing zoom, wrong zoom target, or other finding>`
- `<second finding, if any>`

Create a new immutable revision `<new-revision-id>` from the registered source.
Do not edit the rejected revision in place and do not delete its output or QA
evidence.

Use safe fallback mode. Apply only these explicit operator decisions:

1. `<source start>-<source end>` - `<quoted words and reason>`
2. `<source start>-<source end>` - `<quoted words and reason>`

Reject automatic pause/dead-air cuts unless I explicitly list them. Rebuild
the EDL, join strategies and previews, output transcript, captions, retimed
timeline, focus/effect mappings, and QA. For every zoom, bind the target to
evidence and keep the intro unzoomed. For every speed-up, require visible
prompt-writing/dictation evidence, audible pitch-preserved audio, and exact
action boundaries.

Return a new Gate 1 packet bound to the new revision and stop before applying
or delivering it. Preserve the source, the rejected revision, and failed
evidence.
