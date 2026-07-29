# VideoEdit

VideoEdit turns screen recordings into finished videos without manual timeline
editing. Describe the edit, review the proposal, and render locally with
FFmpeg, local Whisper, and Remotion.

## Windows install

Download and run the latest [VideoEditInstaller.exe](https://github.com/klimPaskov/video-editor/releases/latest/download/VideoEditInstaller.exe).
It downloads the pinned tools, managed Python 3.11, Node.js, FFmpeg, project
dependencies, and the small Whisper model. No administrator access is needed.

When it finishes, run `VideoEdit.cmd` from `%LOCALAPPDATA%\VideoEdit`.

## Manual install

Install `uv`, Python 3.11, Node.js 22, npm, and FFmpeg/ffprobe, then run:

```powershell
git clone https://github.com/klimPaskov/video-editor.git
Set-Location video-editor
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

On macOS, Linux, or WSL2:

```bash
git clone https://github.com/klimPaskov/video-editor.git
cd video-editor
bash scripts/setup.sh
```

## Quick start

Keep the original recording outside the repository. The workflow copies it to
the ignored project workspace and never changes the original file.

```powershell
$project = "my-screen-recording"
uv run videoedit init $project
uv run videoedit ingest $project "C:\Users\me\Videos\recording.mp4"
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
```

Review the proposal before rendering. Later commands create captions, previews,
QA reports, delivery metadata, and the final MP4.

## Prompt example

```text
Please make this screen recording into a clean, focused tutorial. Cut obvious
dead air, filler, and repeated bits, but keep the important commands and
explanations. Only speed up visible prompt-writing; leave browsing and waiting
at normal speed, keep the audio audible, and keep the voice at its original
pitch. Add captions and only zoom when it helps show a useful UI element. Show
me the edit proposal before rendering.
```

Find more realistic prompts and reusable templates in
[examples/input-prompts](examples/input-prompts/).

## Output and privacy

Project outputs are written under the ignored workspace. Delivery is MP4 with
caption sidecars. Private recordings, generated videos, models, credentials,
and local project files do not belong in Git.

## Documentation

- [Installation guide](INSTALL.md)
- [User quickstart](docs/31_user_quickstart.md)
- [Prompt library](examples/input-prompts/README.md)
- [Operator runbook](docs/11_runbook.md)
- [Documentation index](docs/README.md)

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/validate_examples.py
cd remotion && npm run typecheck
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[LICENSE](LICENSE).
