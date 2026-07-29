# User quickstart

VideoEdit turns a screen recording into a reviewed final video from a written
editing brief. It works locally and does not require manual timeline editing.

## 1. Install

On Windows, download and run
[VideoEditInstaller.exe](https://github.com/klimPaskov/video-editor/releases/latest/download/VideoEditInstaller.exe).
It installs the local tools, Python 3.11, Node.js, FFmpeg, project
dependencies, and the small Whisper model.

For a manual setup, follow [`INSTALL.md`](../INSTALL.md).

## 2. Create a project

Keep the original recording outside the repository:

```powershell
$project = "my-screen-recording"
$source = "C:\Users\me\Videos\recording.mp4"

uv run videoedit init $project
uv run videoedit ingest $project $source
uv run videoedit probe $project
uv run videoedit transcribe $project
uv run videoedit detect-silence $project
uv run videoedit plan-review $project --revision-id rev_001
uv run videoedit status $project
```

The original file is never edited. Each new attempt uses a new revision.

## 3. Write a specific prompt

Say what to remove, where it happens, what is visible, and what must remain
normal speed:

```text
Edit my 42-minute developer screen recording into a focused tutorial.

Remove "good job" around 07:18, "very nice" around 07:19, and the sentence
starting "the point of this series is also..." while the GitHub page is visible.
Cut "I am not even going to do this manually" directly to "So I'm gonna use a
Hermes agent for it." Remove obvious "uh", "um", and thinking pauses only when
the sentence and screen continuity remain natural.

Speed up only the visible typing in the Project settings Instructions box from
about 11:37 to 12:18. Keep sound audible and the voice at its original pitch.
Leave browsing, reading, waiting, loading, and result inspection at normal
speed. Do not zoom the intro; use one centered smooth zoom on the Instructions
box only if it is clearly visible.

Prepare the proposal first and preserve commands, names, numbers, warnings,
useful breaths, and uncertain meaning.
```

Use the filled-in prompts and templates in
[`examples/input-prompts/`](../examples/input-prompts/).

## 4. Review and deliver

Review proposed cuts and previews before rendering the final revision. Check
speech joins, captions, audio, timing, and purposeful UI zooms. When the
candidate is approved, VideoEdit writes the MP4 and caption sidecars under the
project output directory.

## Recovery

```bash
uv run videoedit status <project-id>
uv run videoedit doctor --json
```

Keep rejected revisions instead of replacing them. Never commit recordings,
generated videos, `.env` files, models, credentials, or private project files.
