# User quickstart

This page is for someone who wants to use VideoEdit on a real recording, not
contribute to the implementation. The workflow is local-first and review-gated:
you describe the edit in a prompt, VideoEdit creates evidence and proposals,
and you approve only the current artifacts you have inspected.

## 1. Install the local tools

Install `uv`, Python 3.11, Node.js 22, npm, FFmpeg, ffprobe, and Git. Then:

```powershell
git clone https://github.com/klimPaskov/video-editor.git
cd video-editor
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

On macOS, Linux, or WSL2:

```bash
git clone https://github.com/klimPaskov/video-editor.git
cd video-editor
bash scripts/setup.sh
```

If you have an AMD GPU, it is fine to use Windows and the local FFmpeg AMF
preview path. It is not necessary to install CUDA, SAM 3.1, or MatAnyone 2.

## 2. Verify the machine

```bash
uv run videoedit doctor --json
```

Look for passing core checks for Python, FFmpeg, ffprobe, Node.js, Remotion,
and local Whisper. An AMD machine may also show `h264_amf` as a passing media
encoder check. That check authorizes preview acceleration only; it does not
change the lossless production profile or prove CUDA compatibility.

## 3. Provision local Whisper

The workflow does not download a model in the background. Provision one
explicitly and set `VIDEOEDIT_WHISPER_MODEL_PATH`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch-whisper-model.ps1 `
  -Model small `
  -Destination "$env:LOCALAPPDATA\videoedit\models\small.pt"
$env:VIDEOEDIT_WHISPER_MODEL_PATH = `
  "$env:LOCALAPPDATA\videoedit\models\small.pt"
```

Use `scripts/fetch-whisper-model.sh` on Unix-like systems. The helper checks a
pinned hash and stages the checkpoint atomically.

## 4. Create a project from a recording

Keep your original recording outside the checkout. Here is a realistic example
for a screen-recorded developer tutorial:

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

The ingest copy is immutable and hash-bound. Never replace it manually. A new
editorial attempt is a new revision and leaves rejected media/evidence intact.

## 5. Give the workflow a specific prompt

Avoid “make this video better.” Describe the viewer, the exact words, the
visible screen action, and what must stay normal speed. For example:

```text
For project hermes-agent-demo-20260728, prepare revision rev_004 from the
immutable source.

Audience: developers who want to see the Hermes agent workflow without watching
me wait for pages to load.

Remove exactly:
- “good job” around 07:18;
- “very nice” around 07:19;
- the sentence beginning “the point of this series is also…” while the GitHub
  page is visible around 04:11;
- “I am not even going to do this manually” around 01:31, cutting directly to
  “So I’m gonna use a Hermes agent for it.”

Also propose clearly mechanical “uh”, “um”, stutter, accidental-repeat, and
excess-dead-air edits. Keep the original range when a join would remove a
syllable, a useful breath, a qualification, a warning, or screen continuity.

Speed up only the visible typing in the Project settings Instructions box from
approximately 11:37 to 12:18. Keep the keyboard/voice audio audible at the
original pitch. Do not speed up browsing, reading, waiting, loading,
navigation, cursor wandering, or result inspection.

Use one centered smooth zoom only while that Instructions box is visible and
relevant. Do not zoom the intro. If the target or boundary is not verified,
use no zoom.

Create proposals, evidence frames, join plans, the retimed timeline, and the
Gate 1 review packet. Do not apply or approve anything yet. Return exact paths,
hashes, warnings, and the decisions that require a human.
```

Copy more realistic scenarios from [`examples/input-prompts/`](../examples/input-prompts/).

## 6. Review Gate 1

Inspect the proposal packet and evidence frames. Approve only the current
decision artifacts. Gate 1 is where you decide:

- which cuts are acceptable;
- whether a semantic sentence should be removed;
- whether a visible UI target deserves a zoom;
- whether a prompt-writing range is exactly bounded;
- which local assets and fonts may be used.

Do not approve an old revision. The status command now reports
`gate1_approval_missing_or_stale` when the active revision has no matching
approval.

## 7. Render and review

After Gate 1, compile the approved EDL and retimed timeline. Render a short
preview before a full candidate. For every applied cut, inspect a join preview
with context on both sides and re-transcribe it. Check clipped words, missing or
duplicated words, clicks, room-tone changes, black flashes, frozen frames,
face/screen jumps, captions, zoom boundaries, and speed-up audio.

Gate 2 is separate from Gate 1. It locks rendered segments only after the
actual evidence passes.

## 8. Finish and deliver

Before Gate 3, inspect the complete candidate and record a full watch-through.
The final QA must include source identity, stream details, A/V sync, audio
quality, captions, visual evidence, provenance, and current approval hashes.

Only after Gate 3 may the workflow promote an MP4 into the configured output
directory, write checksums and sidecars, and verify a backup. Cleanup has its
own approval and never removes the immutable source or valid failed evidence.

## What stays out of Git

Do not commit source recordings, generated MP4s, project state, `.env` files,
Whisper checkpoints, private fonts, stock media, tokens, or signed URLs. The
repository is MIT-licensed, but third-party media and models keep their own
terms. Record local asset hashes and licence/permission details in the project
manifest before using them in a deliverable.

## Useful recovery commands

```bash
uv run videoedit status <project-id>
uv run videoedit doctor --json
uv run python scripts/validate_examples.py
```

When a candidate is rejected, create a new revision from the immutable source;
never patch the rejected MP4 in place. Use
[`08-repair-rejected-candidate.md`](../examples/input-prompts/08-repair-rejected-candidate.md)
as a starting prompt.
