# Repository Layout

```text
.
|-- AGENTS.md
|-- GOAL_PROMPT.md
|-- INSTALL.md
|-- WORKFLOW.md
|-- TASKS.md
|-- README.md
|-- .agents/skills/
|   |-- video-editing-orchestrator/
|   |-- ffmpeg-video-engine/
|   |-- remotion-compositor/
|   `-- video-review-qa/
|-- config/
|-- docs/
|   `-- adr/
|-- examples/
|   `-- input-prompts/       filled-in creator prompts and templates
|-- prompts/
|-- remotion/
|   |-- src/
|   `-- public/generated/
|-- schemas/
|-- scripts/
|-- source/
|-- src/videoedit/
|   |-- adapters/
|   |-- domain/
|   |-- pipeline/
|   `-- services/
|-- tests/
`-- projects/                  ignored runtime data
    `-- <project-id>/
        |-- raw/
        |-- revisions/
        |-- work/
        |-- artifacts/
        |-- reviews/
        |-- output/
        `-- state/
```

## Runtime project layout

- `raw/` contains immutable ingested source copies or source reference manifests.
- `revisions/` contains immutable revision records and their active editorial identity.
- `work/` contains disposable proxies, experiments, and intermediate media.
- `artifacts/` contains versioned JSON, timelines, logs, and approved plans.
- `reviews/` contains operator-facing Markdown and imported decisions.
- `output/` contains preview and final candidates.
- `state/` contains resumable stage records, staging paths, locks, and cache records.

Large media and private assets are ignored. Schemas, examples, reusable components, tests, skills, and documentation are versioned.

For people using the workflow rather than developing it, read in this order:

1. `README.md` for the overview and copy/paste commands;
2. `docs/31_user_quickstart.md` for one complete local project;
3. `docs/README.md` for the documentation map;
4. `examples/input-prompts/` for realistic prompts;
5. `INSTALL.md` for platform troubleshooting;
6. `docs/11_runbook.md` for recovery, gates, and delivery operations.
