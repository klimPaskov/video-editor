# VideoEdit

VideoEdit is a local-first, review-gated video workflow for turning talking-head,
green-screen, and screen-recording footage into a finished MP4 without manually
editing a timeline.

The core runs locally with Python 3.11, FFmpeg/ffprobe, local Whisper, and a
Node.js 22 Remotion compositor. Codex plans and orchestrates the work; typed
adapters perform deterministic media operations; every important result is
stored as a hash-bound artifact.

[![CI](https://github.com/klimPaskov/video-editor/actions/workflows/ci.yml/badge.svg)](https://github.com/klimPaskov/video-editor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What you get

- immutable source ingest with SHA-256 and ffprobe evidence;
- local word-timed transcripts and silence intervals;
- dense, evidence-backed proposals for fillers, stutters, false starts,
  repetitions, dead air, and exact editorial cuts;
- synchronized picture and production audio from one edit decision list;
- captions, text layers, backgrounds, picture-in-picture, green-screen chroma
  keying, object recolor, and purposeful screen-recording focus;
- explicitly requested prompt-writing/dictation speed-ups with audible,
  pitch-preserved sound and a persisted source-to-output map;
- Remotion previews, join previews, re-transcription, media QA, review packets,
  delivery metadata, backup verification, and safe cleanup planning;
- optional SAM 3.1 and MatAnyone 2 contracts kept outside the required workflow.

The required final path is worker-free: controlled green-screen footage uses
deterministic chroma keying or an approved supplied/manual mask. No CUDA,
checkpoint download, cloud provider, or paid service is required for the core.

## Pick the path that matches your machine

### Windows + AMD GPU

This is a supported local setup. Use the AMD GPU for disposable previews or
derivatives only when `videoedit doctor --json` reports `h264_amf` ready. Keep
the production master on software `libx264` QP 0 with PCM `f32le` audio when
the selected delivery profile supports it.

### macOS, Linux, or WSL2

Use the same local core with software FFmpeg. WSL2 is optional; you do not need
an NVIDIA GPU for the required workflow.

### Optional vision workers

The repository retains isolated, versioned contracts for SAM 3.1 and MatAnyone
2 so a future accepted extension can be integrated safely. They are not
installed, invoked, or needed for the final workflow. Do not download their
checkpoints just to use VideoEdit.

## Install in about ten minutes

Install:

- [uv](https://docs.astral.sh/uv/);
- Python 3.11;
- Node.js 22 and npm;
- FFmpeg and ffprobe on `PATH`;
- Git.

Clone the public repository and run the setup helper.

Windows PowerShell:

```powershell
git clone https://github.com/klimPaskov/video-editor.git
Set-Location video-editor
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

macOS, Linux, or WSL2:

```bash
git clone https://github.com/klimPaskov/video-editor.git
cd video-editor
bash scripts/setup.sh
```

The helper installs the locked Python and Remotion dependencies and creates
`.env` only when it does not already exist. It does not download a Whisper
checkpoint or any media implicitly.

Check the installation:

```bash
uv run videoedit doctor --json
uv run python scripts/validate_examples.py
uv run pytest -q
(cd remotion && npm run typecheck)
```

On PowerShell, replace the last line with:

```powershell
Push-Location remotion; npm run typecheck; Pop-Location
```

### Add local Whisper explicitly

Transcription needs a local Whisper package and an operator-provisioned model
file. The helper verifies a pinned SHA-256 and never overwrites a mismatched
file.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch-whisper-model.ps1 `
  -Model small `
  -Destination "$env:LOCALAPPDATA\videoedit\models\small.pt"
$env:VIDEOEDIT_WHISPER_MODEL_PATH = `
  "$env:LOCALAPPDATA\videoedit\models\small.pt"
```

```bash
bash scripts/fetch-whisper-model.sh \
  --model small \
  --destination "$HOME/.local/share/videoedit/models/small.pt"
export VIDEOEDIT_WHISPER_MODEL_PATH="$HOME/.local/share/videoedit/models/small.pt"
```

Review the upstream model terms for your use case. No API key is needed for
the local Whisper path.

## Run your first project

Keep the original recording outside the Git checkout. VideoEdit copies it into
the ignored project workspace and hashes the copy; it never edits the original.

```powershell
$project = "hermes-agent-demo-20260728"
$source = "C:\Users\me\Videos\hermes-agent-demo.mp4"

uv run videoedit init $project
uv run videoedit ingest $project $source
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
uv run videoedit status $project
```

`videoedit status` prints a schema-valid JSON snapshot. It reports source
integrity, stage state, current-revision approval warnings, QA readiness, and
delivery paths. It does not approve anything.

For a no-media smoke test:

```bash
uv run videoedit make-demo setup-smoke --render
```

## The workflow in plain English

```text
ingest -> transcript/silence -> proposals -> Gate 1
       -> retime/EDL -> render previews -> join/transcript/visual QA -> Gate 2
       -> final assembly -> full QA/watch-through -> Gate 3
       -> delivery metadata -> backup verification -> cleanup dry run
```

The gates are deliberately separate:

1. **Gate 1** approves the current edit, effect, focus, pacing, asset, and
   transition plans.
2. **Gate 2** approves rendered segments and their join, transcript, audio,
   caption, mask, and visual evidence.
3. **Gate 3** approves one complete candidate and its final QA report.

Delivery, backup verification, source deletion, and cleanup are separate
decisions. An old approval is never reused after a bound source, plan,
configuration, implementation, or model hash changes.

## A realistic prompt you can paste today

This is a filled-in example for a screen-recorded tutorial. Replace only the
local path and project id if needed; keep the editorial language specific.

```text
Create a review-gated VideoEdit project named hermes-agent-demo-20260728 from
C:\Users\me\Videos\hermes-agent-demo.mp4.

This is a 42-minute screen recording for software developers. I explain how I
use a Hermes agent to modify a local GitHub project. Keep the opening title
card, the explanation of the problem, the successful run, and the final
result. The target is a focused 12-18 minute tutorial, but do not remove facts
or demonstrations just to hit a duration.

First do the credential-free local path: copy and hash the source, probe it,
transcribe it with local Whisper, detect silence, and create a review packet.
Scan every transcript word, pause, take boundary, repetition, and screen-state
change. Propose small safe edits with evidence and a join strategy.

Requested editorial changes:
- remove the spoken phrase “good job” around 07:18;
- remove “very nice” around 07:19;
- remove the sentence beginning “the point of this series is also…” while the
  GitHub page is visible around 04:11;
- remove “I am not even going to do this manually” and cut directly to “So I’m
  gonna use a Hermes agent for it” around 01:31;
- remove obvious “uh” and “um” fillers and excess thinking pauses only when the
  join remains a complete, natural sentence;
- preserve names, commands, warnings, numbers, negation, useful breaths, and
  any sentence whose meaning is uncertain.

Speed and focus:
- speed up only the visible prompt-writing action in the Project settings
  Instructions box, approximately 11:37-12:18, after verifying the first and
  last text-change frames;
- keep its sound audible and at the original voice pitch;
- leave browsing, reading, waiting, loading, navigation, and result inspection
  at normal speed;
- do not zoom the intro;
- if the Instructions box is clearly visible, use one centered smooth zoom
  while it is relevant; otherwise use no zoom.

Do not use SAM 3.1, MatAnyone 2, cloud providers, paid calls, or remote asset
generation. Do not apply edits or approve Gate 1 on my behalf. Return the
source hash, probe, transcript, silence evidence, proposal packet, exact
artifact paths, warnings, and the small set of decisions that require me.
```

More filled-in scenarios are in [`examples/input-prompts/`](examples/input-prompts/).

## Prompt library

| Situation | Copy this example |
| --- | --- |
| Start a real tutorial recording | [`01-basic-recording.md`](examples/input-prompts/01-basic-recording.md) |
| Remove fillers while protecting meaning | [`04-dense-edit-policy.md`](examples/input-prompts/04-dense-edit-policy.md) |
| Speed up a prompt-writing section | [`02-prompt-writing-focus.md`](examples/input-prompts/02-prompt-writing-focus.md) |
| Focus a visible screen target | [`05-screen-recording-zoom.md`](examples/input-prompts/05-screen-recording-zoom.md) |
| Key a presenter and layer text | [`06-green-screen-layering.md`](examples/input-prompts/06-green-screen-layering.md) |
| Add readable captions and local branding | [`07-captions-and-brand.md`](examples/input-prompts/07-captions-and-brand.md) |
| Repair a rejected cut/zoom pass | [`08-repair-rejected-candidate.md`](examples/input-prompts/08-repair-rejected-candidate.md) |
| Review a real candidate | [`09-final-qa-and-watchthrough.md`](examples/input-prompts/09-final-qa-and-watchthrough.md) |
| Publish and back up safely | [`10-delivery-and-cleanup.md`](examples/input-prompts/10-delivery-and-cleanup.md) |
| Use an AMD GPU without changing the master | [`11-amd-preview.md`](examples/input-prompts/11-amd-preview.md) |
| Start from a complete brief | [`12-project-brief-template.md`](examples/input-prompts/12-project-brief-template.md) |

Prompt habits that produce useful results:

- name the project, revision, source, audience, and delivery profile;
- quote the exact words to remove and give approximate source times;
- describe what is visible at a speed-up or zoom boundary;
- state what must remain normal speed and what must remain untouched;
- ask for evidence, artifact hashes, and a review packet;
- tell the workflow to stop on uncertain meaning, identity, timing, licences,
  credentials, cost, or QA.

A prompt requests work or evidence. It is not an approval record.

## Output quality and AMD previews

When the source and project profile permit it, the lossless master profile is:

- MP4 container;
- H.264 `libx264` video with QP 0;
- original project dimensions and rational frame rate;
- `yuv420p` and recorded BT.709 color metadata;
- PCM `f32le` audio, 48 kHz, stereo, with no silent normalization fallback.

An AMD AMF encode is useful for a quick disposable preview or derivative. It
is not automatically lossless and must not silently replace the production
profile. Run `videoedit doctor --json` first and retain the encoder diagnostics.

## Assets, privacy, and provenance

The repository is MIT-licensed, but that licence covers only the original code,
documentation, and examples. Keep source footage, outputs, fonts, music, B-roll,
backgrounds, logos, replacement objects, checkpoints, credentials, and private
asset files outside Git or under ignored project paths.

For every non-source asset used in a project, record its local path, SHA-256,
origin, permission/licence, attribution requirement, and intended use in the
project asset manifest. You may use local assets in your own project, but do
not redistribute assets unless their terms allow it.

## Repository map

- `src/videoedit/` - Python orchestration, typed adapters, schemas, approvals,
  and CLI services;
- `remotion/` - data-driven visual compositor;
- `schemas/` - persisted JSON contracts;
- `examples/input-prompts/` - filled-in operator prompts and templates;
- `tests/` - unit and integration tests;
- `docs/` - user guide, architecture, runbook, QA, and decisions;
- `workers/` - optional worker contracts only; not required by the final path;
- `scripts/` - setup, model provisioning, validation, and checks.

Start with [`docs/31_user_quickstart.md`](docs/31_user_quickstart.md), use the
[`documentation guide`](docs/README.md) to find deeper references, then use
[`INSTALL.md`](INSTALL.md) for platform details and [`docs/11_runbook.md`](docs/11_runbook.md)
for recovery and review operations.

## Development checks

Before opening a pull request, run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/validate_examples.py
cd remotion && npm run typecheck
```

Do not weaken a quality threshold to make a check pass. Media behavior changes
also require a short decoded fixture or preview with retained visual/audio
evidence.

## Contributing, security, and licence

See [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md),
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md), and [`LICENSE`](LICENSE).
Never commit private media or credentials. Report security issues privately to
the maintainer rather than including secrets or source footage in a public issue.
