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
|   |-- sam3-object-effects/
|   |-- matanyone-person-matting/
|   `-- video-review-qa/
|-- config/
|-- docs/
|   `-- adr/
|-- examples/
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
|-- workers/
|   |-- sam3/
|   `-- matanyone2/
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
- `artifacts/` contains versioned JSON, masks, mattes, timelines, logs, and approved plans.
- `reviews/` contains operator-facing Markdown and imported decisions.
- `output/` contains preview and final candidates.
- `state/` contains resumable stage records, staging paths, locks, and cache records.

Large media and private assets are ignored. Schemas, examples, reusable components, tests, skills, and documentation are versioned.
