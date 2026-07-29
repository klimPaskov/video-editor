# Installation

VideoEdit is a local workflow for screen recordings. The easiest Windows
setup is the release installer; manual setup is available for contributors and
non-Windows systems.

## Windows installer

Download and run:

[VideoEditInstaller.exe](https://github.com/klimPaskov/video-editor/releases/latest/download/VideoEditInstaller.exe)

The installer downloads the pinned local tools, managed Python 3.11, Node.js,
FFmpeg/ffprobe, locked project dependencies, and the small local Whisper model.
It installs under `%LOCALAPPDATA%\VideoEdit` without administrator access and
creates `VideoEdit.cmd` when complete.

## Manual setup

Install:

- [uv](https://docs.astral.sh/uv/);
- Python 3.11;
- Node.js 22 and npm;
- FFmpeg and ffprobe on `PATH`.

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

Verify the setup:

```bash
uv run videoedit doctor --json
uv run pytest -q
uv run python scripts/validate_examples.py
(cd remotion && npm run typecheck)
```

## Whisper model

Manual installs must provision a local model explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch-whisper-model.ps1 `
  -Model small `
  -Destination "$env:LOCALAPPDATA\videoedit\models\small.pt"
$env:VIDEOEDIT_WHISPER_MODEL_PATH = `
  "$env:LOCALAPPDATA\videoedit\models\small.pt"
```

Use `scripts/fetch-whisper-model.sh` on Unix-like systems. The helper verifies
the pinned model hash and reuses a matching local file.

## Create a project

Keep source recordings outside the repository:

```powershell
$project = "my-screen-recording"
uv run videoedit init $project
uv run videoedit ingest $project "C:\Users\me\Videos\recording.mp4"
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
```

Use the prompts in [`examples/input-prompts/`](examples/input-prompts/) to
describe the exact cuts, prompt-writing speed-ups, captions, and purposeful UI
zooms you want.

## Troubleshooting

- Run `uv run videoedit doctor --json` to check the local tools.
- If Whisper is unavailable, set `VIDEOEDIT_WHISPER_MODEL_PATH` to the model
  downloaded by the helper.
- Keep rejected revisions; create a new revision instead of replacing a video
  in place.

Never commit recordings, generated videos, `.env` files, models, credentials,
or private project files. See [SECURITY.md](SECURITY.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
