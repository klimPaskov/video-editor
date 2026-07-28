# VideoEdit

VideoEdit is a local-first, review-gated workflow for turning talking-head,
green-screen, and screen-recording footage into a finished MP4 without manual
timeline editing.

The core is open source and runs on your machine. It combines Python 3.11,
FFmpeg/ffprobe, local Whisper transcription, and a Node.js 22 Remotion
compositor. Every important decision is represented by a hash-bound artifact:
transcripts, edit decisions, captions, timelines, previews, QA reports, and
delivery metadata.

## What it does

1. Hashes and probes an immutable source.
2. Creates word-timed transcript and silence evidence.
3. Proposes dense speech cleanup, focus, pacing, captions, and effects.
4. Waits for human approval before semantic edits or effects.
5. Compiles synchronized audio and picture from the same edit decision list.
6. Renders captions, text, backgrounds, chroma-key layers, and purposeful UI
   zooms with Remotion.
7. Re-transcribes edited material, checks joins and media integrity, and builds
   a review packet.
8. Promotes only an approved candidate, writes checksums and delivery metadata,
   verifies a backup, and keeps cleanup separate from delivery approval.

The required final path does not use CUDA, SAM 3.1, or MatAnyone 2. Controlled
green-screen footage uses chroma key or an explicitly approved supplied mask.
The optional worker directories contain contracts only and are not needed for
normal setup.

## Quick setup

Install these prerequisites first:

- [uv](https://docs.astral.sh/uv/)
- Python 3.11
- Node.js 22 and npm
- FFmpeg with `ffmpeg` and `ffprobe` on `PATH`
- Git, if you want to contribute or publish results

From the repository root, the setup helpers create the Python environment,
install the development and local-Whisper extras, copy `.env.example` to
`.env` without overwriting an existing file, and install the locked Remotion
packages.

Windows PowerShell:

```powershell
pwsh -File .\scripts\setup.ps1
```

macOS, Linux, or WSL2:

```bash
bash scripts/setup.sh
```

The scripts do not download a model or any media implicitly. Provision a local
Whisper checkpoint explicitly, then point `.env` at it:

```powershell
pwsh -File .\scripts\fetch-whisper-model.ps1 `
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

The fetch helpers use the public Whisper model URL, verify its pinned SHA-256,
and refuse to overwrite a mismatched file. Review the model's upstream terms
for your use case.

Check the installation:

```bash
uv run videoedit doctor --json
uv run python scripts/validate_examples.py
uv run pytest -q
(cd remotion && npm run typecheck)
```

On Windows, run the same commands from PowerShell; omit the surrounding
parentheses and use `Push-Location remotion; npm run typecheck; Pop-Location`
for the Remotion check.

## First project

Keep your source outside the repository or in the ignored project workspace.
The ingest command copies and hashes it; it never edits the original.

```powershell
$project = "my-video"
uv run videoedit init $project
uv run videoedit ingest $project "C:\path\to\recording.mp4"
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
```

On macOS/Linux/WSL2, replace the source path with a POSIX path. Continue with
the planning, review, render, and delivery commands shown by:

```bash
uv run videoedit --help
```

For a no-media installation smoke test, use the deterministic demo command:

```bash
uv run videoedit make-demo setup-smoke --render
```

If a human rejects a rendered candidate for bad pacing or joins, rebuild from
the immutable source with the conservative repair switch. It rejects automatic
pause/dead-air cuts and retains only explicit operator edits:

```powershell
uv run videoedit materialize-edit-decisions PROJECT_ID `
  --proposals projects/PROJECT_ID/artifacts/edit-proposals-production.json `
  --smart-dense-batch projects/PROJECT_ID/review/smart-dense-review-batch.json `
  --instructions projects/PROJECT_ID/review/operator-edit-instructions.json `
  --safe-fallback-only `
  --output projects/PROJECT_ID/review/edit-decisions-repair.json
```

## Human review is part of the workflow

Model and heuristic output is a proposal; it cannot approve itself.

- Gate 1 approves edit, effect, focus, and explicitly requested pacing plans.
- Gate 2 approves rendered segments and their join/QA evidence.
- Gate 3 approves the final candidate and final QA report.
- Delivery, backup verification, source deletion, and cleanup are separate
  decisions.

Approvals become stale when a bound input, configuration, implementation hash,
or model identity changes. A successful command is not visual proof: inspect
the preview and diagnostics before approving it.

## AMD GPUs and output quality

An AMD GPU can accelerate compatible FFmpeg preview or derivative encodes with
`h264_amf` after `videoedit doctor` reports that AMF is available. The
reproducible production default remains software `libx264`; do not switch the
lossless delivery profile to AMF.

For a source that already matches the requested delivery format, the strict
master profile is MP4 with H.264 lossless QP 0 video and PCM `f32le` audio at
48 kHz stereo. The actual source and approved project settings determine the
final stream dimensions and frame rate.

## Prompt examples

Copy, edit, and paste the templates in
[`examples/input-prompts/`](examples/input-prompts/) into Codex or another
operator interface. They cover a basic recording, precise prompt-writing
speed-ups and purposeful zooms, and final review/delivery. They use placeholders
so private paths, credentials, and source media never enter the public repo.

## Repository map

- `src/videoedit/` — typed orchestration, media services, schemas, approvals,
  and CLI commands
- `remotion/` — data-driven visual compositor
- `schemas/` — persisted JSON contracts
- `examples/` — schema examples and copyable operator prompts
- `tests/` — unit and integration coverage
- `docs/` — architecture, runbooks, QA, and decision records
- `workers/` — isolated optional-worker contracts; not required by the final
  workflow
- `scripts/` — setup, model provisioning, validation, and checks

## Media, assets, and provenance

No user video, generated output, private asset, credential, or model checkpoint
belongs in this repository. Add media through the local project workflow. For
assets you redistribute, record the source, hash, permission or licence, and
attribution requirements in the project asset manifest. The repository's
software licence does not grant rights to third-party media or models.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the boundary between
this repository and its dependencies.

## Licence

Original source code, documentation, and examples are released under the
[MIT Licence](LICENSE). Third-party packages, models, fonts, media, and
checkpoints remain under their own terms.
