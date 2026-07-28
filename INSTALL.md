# Installation and machine setup

This is the practical installation guide. The required workflow is local and
does not need CUDA, SAM 3.1, MatAnyone 2, a cloud account, or a paid provider.
Windows + AMD is a supported setup for local work and FFmpeg preview
acceleration.

## Required software

Install:

- Git;
- [uv](https://docs.astral.sh/uv/);
- Python 3.11;
- Node.js 22 and npm;
- FFmpeg and ffprobe with both commands on `PATH`.

Linux/WSL2 users can start with:

```bash
sudo apt update
sudo apt install -y git curl ffmpeg fontconfig build-essential
```

Install Node.js 22 through your preferred version manager or approved system
package. Confirm the versions before setup:

```bash
uv --version
python --version
node --version
npm --version
ffmpeg -version
ffprobe -version
```

## One-command setup

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

```bash
bash scripts/setup.sh
```

The helper runs `uv sync --extra dev --extra whisper`, creates `.env` only if
it is missing, and runs `npm ci` in `remotion/`. Use
`-SkipWhisper` on PowerShell or `SKIP_WHISPER=1` on Unix only when you are
working on non-transcription code.

If Node 22 is installed in a nonstandard location, set these local overrides
before running the doctor:

```powershell
$env:VIDEOEDIT_NODE_PATH = "C:\Tools\node-v22.23.1-win-x64\node.exe"
$env:VIDEOEDIT_NPM_PATH = "C:\Tools\node-v22.23.1-win-x64\npm.cmd"
uv run videoedit doctor --json
```

## Verify the setup

```bash
uv run videoedit doctor --json
uv run python scripts/validate_examples.py
uv run pytest -q
```

Typecheck Remotion separately:

```bash
(cd remotion && npm run typecheck)
```

```powershell
Push-Location remotion; npm run typecheck; Pop-Location
```

The doctor checks the core, FFmpeg/ffprobe, local Whisper, Node 22, Remotion,
fonts, and available encoder capabilities. A passing `h264_amf` check means
that the AMD media encoder can be used for bounded previews or derivatives. It
does not prove that AMF is lossless and does not satisfy CUDA requirements for
optional workers.

## Local Whisper model

The core never downloads a checkpoint implicitly. Provision one deliberately:

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

The helpers pin and verify the public model file and refuse to overwrite an
existing mismatched file. Review the package and model terms for your use case.

## Create a project

Keep source recordings outside the checkout. The project workspace is ignored
by Git and the ingest copy is immutable:

```powershell
$project = "my-tutorial-20260728"
uv run videoedit init $project
uv run videoedit ingest $project "C:\Users\me\Videos\my-tutorial.mp4"
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
uv run videoedit status $project
```

Then use the filled-in prompts in `examples/input-prompts/` to request the
editing plan, focus/pacing evidence, captions, effects, repair, and final QA.

## Assets and licences

Provide local fonts, logos, backgrounds, music, B-roll, sounds, and replacement
objects yourself. For each non-source asset, record its path, SHA-256, origin,
permission/licence, attribution, and intended use in the project asset
manifest. The MIT licence for this repository does not grant rights to
third-party media.

## Optional workers are not part of setup

Do not install SAM 3.1 or MatAnyone 2 for the required workflow. Their folders
contain isolated JSON contracts and reviewed documentation only. A future
re-enable decision would require separate licence, checkpoint, hardware,
runtime, and output-semantics approvals. An AMD GPU or AMF encoder is not a
substitute for those approvals.

## Troubleshooting

### `ffmpeg` or `ffprobe` is not found

Install FFmpeg from your operating system's approved source and add its `bin`
directory to `PATH`. Open a new shell, then rerun `videoedit doctor --json`.

### Node is too old

Install Node.js 22 or set `VIDEOEDIT_NODE_PATH` and `VIDEOEDIT_NPM_PATH` to an
explicit Node 22 installation. Do not silently use a different runtime for
Remotion.

### Whisper is unavailable

Run the explicit model helper, set `VIDEOEDIT_WHISPER_MODEL_PATH`, and rerun
the doctor. The workflow will not claim transcription success from a missing
model.

### A candidate is rejected

Keep the rejected revision and evidence. Create a new revision from the
immutable source with
`examples/input-prompts/08-repair-rejected-candidate.md`; do not overwrite the
old MP4.

### Status says `gate1_approval_missing_or_stale`

The active revision has no current hash-bound Gate 1 edit approval. An approval
for an earlier revision cannot be reused. Review the current packet, then use
the repository's Gate 1 command with the exact current artifact paths.

## Keep private data private

Never commit source media, generated outputs, `.env`, tokens, signed URLs,
private assets, downloaded checkpoints, or local project evidence. The supplied
`.gitignore` is intentionally strict; check `git status --ignored` before
publishing.
