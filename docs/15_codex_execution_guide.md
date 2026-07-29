# Codex Execution Guide

## Purpose

This guide turns the backlog into controlled Codex work sessions. It assumes Codex can read repository instructions, use repository skills, run commands in a workspace sandbox, and return a structured result.

## Repository preparation

1. Extract the planning package into an empty directory.
2. Keep the package structure intact. The package root is the implementation repository baseline.
3. Initialize version control and commit the untouched baseline.
4. Configure Codex for workspace write access with approval on request.
5. Complete the manual decisions that apply to the active phase.
6. Do not add GPU checkpoints or provider credentials before their phases.
7. Run each phase on a clean branch or worktree.

## Interactive session pattern

Inside the repository:

```text
Read AGENTS.md, TASKS.md, prompts/MASTER_PHASE_PROMPT.md, the active phase prompt, and the contracts named by that prompt. Inspect the repository. Implement only the selected phase. Run all available required checks. Write the structured phase result. Review the diff before stopping.
```

Keep one active phase per branch.

## Noninteractive pattern

A representative invocation is:

```bash
codex exec \
  --sandbox workspace-write \
  --output-schema schemas/codex_phase_result.schema.json \
  -o .codex/results/P0.json \
  "Implement Phase 0 using prompts/00_foundation.md. Follow AGENTS.md. Stop at any required human decision."
```

Check the exact Codex CLI options against the installed version. The result schema gives automation a stable completion contract.

## Phase selection

Select the first incomplete task on the required path P0-P6, U21/U22, P9, P10,
and P11. Retired optional integration prompts are not part of the phase loop.

Use one prompt from `prompts/`:

- `00_foundation.md`
- `01_ingest_probe.md`
- `02_transcription_silence.md`
- `03_edit_planning_review.md`
- `04_base_edit_audio.md`
- `05_remotion_captions_motion.md`
- `09_assets_broll_sound.md`
- `10_preview_self_verify.md`
- `11_delivery_operations.md`
- `99_review.md`

`MASTER_PHASE_PROMPT.md` defines the common phase loop.

## Skill use

Project skills live under `.agents/skills`:

- `video-editing-orchestrator` coordinates the whole workflow.
- `implement-videoedit` guides phase implementation.
- `operate-videoedit` guides later operation and diagnosis.
- `ffmpeg-video-engine` constrains deterministic media work.
- `remotion-compositor` constrains declarative visual work.
- `video-review-qa` constrains preview, review, and quality evidence.

The official Remotion skills may be installed separately. Project requirements remain authoritative when an external skill conflicts with this package.

## Worktree strategy

For parallel tasks:

- create one worktree and branch per independent pull request
- do not assign two agents to edit the same public schema
- merge foundation contracts before adapter implementations
- separate future external-integration changes from core environment changes
- rebase or merge current contract changes before final tests

Safe parallel work after Phase 0 includes media fixtures, QA helpers, timeline functions, caption segmentation, visual components, and documentation.

## Required evidence from each session

The phase result should include:

- task identifiers completed
- files changed
- schema or migration changes
- commands run and exit status
- test results
- acceptance criteria status
- unresolved risks
- decisions required
- next recommended task

A prose claim that checks passed is insufficient. Include command names and the observed result.

## Stop conditions

Codex must stop and record a decision request when:

- it needs a credential or gated checkpoint
- it would make a paid call
- a licence is uncertain
- the available GPU or CUDA stack is incompatible with an explicitly re-enabled worker extension
- an upstream model API differs materially from the planned worker contract for an explicitly re-enabled worker extension
- it would weaken a quality gate
- it would change a public schema incompatibly
- it cannot prove a cleanup action stays inside derived files
- the active prompt conflicts with an accepted ADR

## Diff review

Before completion, Codex should inspect:

- accidental binary or media files
- secrets and tokens
- new dependencies and licences
- shell invocation and path traversal risk
- timestamp units and frame rounding
- floating-point money
- missing tests
- changed schemas
- hidden provider or model coupling
- source mutation
- cleanup that can reach source media

Use `prompts/99_review.md` for an independent review session.

## Commit guidance

A commit should represent one coherent step. Good examples:

- `feat: add typed process runner and doctor checks`
- `feat: register immutable media source and parse ffprobe`
- `feat: compile approved keep ranges into output timeline`
- `feat: render schema-driven Remotion text plate`
- `test: add synthetic audio and frame sync fixture`

Avoid commits that mix formatting, architecture changes, model installation, provider integration, and feature behavior.

## MCP policy

MCP servers may be added for documentation or an optional provider only when the active task needs them. Record the server, purpose, permissions, data exposure, and removal path. The production local video pipeline must not require MCP to run.

## Completion interpretation

A phase is complete only when its schema-valid result shows that every acceptance criterion passed or names an explicit blocker. `partial` and `blocked` results are acceptable when accurate. Mocked worker success cannot satisfy a live worker acceptance criterion.
