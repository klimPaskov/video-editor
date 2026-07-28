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
  --revision-id REVISION_ID `
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

## Prompt cookbook

The workflow is designed to be driven by plain-language operator prompts. The
prompt is an instruction to create or revise evidence and proposals; it is not
an approval. Replace angle-bracket placeholders with local values and keep
private paths, credentials, source media, and provider tokens on your machine.

The complete copy/paste library is in
[`examples/input-prompts/`](examples/input-prompts/). The most useful starting
points are listed below, followed by copyable prompt bodies.

| Use case | Prompt template |
| --- | --- |
| New talking-head or screen recording | [`01-basic-recording.md`](examples/input-prompts/01-basic-recording.md) |
| Exact cuts, fillers, and semantic boundaries | [`04-dense-edit-policy.md`](examples/input-prompts/04-dense-edit-policy.md) |
| Prompt-writing speed-ups and UI focus | [`02-prompt-writing-focus.md`](examples/input-prompts/02-prompt-writing-focus.md) |
| Screen-recording zooms | [`05-screen-recording-zoom.md`](examples/input-prompts/05-screen-recording-zoom.md) |
| Green-screen keying and layers | [`06-green-screen-layering.md`](examples/input-prompts/06-green-screen-layering.md) |
| Captions, text, and brand settings | [`07-captions-and-brand.md`](examples/input-prompts/07-captions-and-brand.md) |
| Repairing a rejected candidate | [`08-repair-rejected-candidate.md`](examples/input-prompts/08-repair-rejected-candidate.md) |
| QA watch-through | [`09-final-qa-and-watchthrough.md`](examples/input-prompts/09-final-qa-and-watchthrough.md) |
| Delivery, backup, and cleanup | [`10-delivery-and-cleanup.md`](examples/input-prompts/10-delivery-and-cleanup.md) |
| AMD preview workflow | [`11-amd-preview.md`](examples/input-prompts/11-amd-preview.md) |
| Reusable project brief | [`12-project-brief-template.md`](examples/input-prompts/12-project-brief-template.md) |

### 1. Start a project and collect evidence

```text
Create a VideoEdit project.

Project id: <project-id>
Source file: <absolute-local-video-path>
Audience: <audience>
Delivery target: <platform or internal review>

Run the credential-free local path only. Preserve the source as immutable.
Hash and probe it, create the local word-timed transcript, detect silence,
and write a review packet. Propose edits, captions, focus/pacing, effects, and
assets with confidence and reasons. Do not cut, render a final candidate,
approve anything, delete files, use a provider, or use a GPU worker yet.

Return the exact commands run, artifact paths, hashes, warnings, and the
questions that require my decision.
```

The corresponding local command sequence is:

```powershell
uv run videoedit init <project-id>
uv run videoedit ingest <project-id> "<absolute-local-video-path>"
uv run videoedit probe <project-id>
uv run videoedit transcribe <project-id> --model <local-whisper-model>
uv run videoedit detect-silence <project-id>
uv run videoedit plan-review <project-id> --revision-id rev_001
uv run videoedit status <project-id>
```

### 2. Request safe mechanical cleanup

```text
For <project-id>, create a smart-dense edit proposal from the current
transcript and silence evidence.

Candidate policy:
- propose every clearly mechanical dead-air, filler, stutter, false-start,
  accidental-repeat, and excess-silence edit that passes the configured
  thresholds;
- preserve breaths, emphasis, names, numbers, negation, qualifications,
  uncertainty, and overlapping speech;
- keep the original range whenever a join is ambiguous;
- assign a join strategy and a bounded preview to every applied cut.

Do not silently apply semantic deletion. Rank uncertain meaning or continuity
items for review. Generate the edit-review batch, join plan, and metrics QA.
Ask for one Gate 1 decision instead of asking me about every obvious
mechanical edit.
```

To choose the conservative repair mode after a bad candidate:

```text
The previous candidate for <project-id> was rejected for rushed pacing or bad
joins. Create revision <revision-id> from the immutable source.

Use safe fallback mode: retain only the exact operator cuts listed below and
reject automatic pause/dead-air cuts. Recompute the EDL, retimed timeline,
captions, zoom/effect mappings, join previews, transcript comparison, and QA.
Do not edit the rejected revision in place.

Exact operator cuts:
1. <source start>-<source end>: <reason>
2. <source start>-<source end>: <reason>

Return the new revision manifest and the Gate 1 packet. Stop before applying
the revision until Gate 1 is explicitly recorded for this revision.
```

### 3. Request exact semantic edits

```text
For <project-id>, prepare revision <revision-id> using only these explicit
operator instructions:

- Remove the exact phrase "<phrase>" at approximately <time>.
- Remove the sentence beginning "<opening words>" while <visible context>.
- Cut directly from "<last phrase to keep>" to "<next phrase to keep>".
- Remove thinking pauses and "uh/um" only when the resulting join is a
  complete, natural sentence and no syllable, qualifier, or meaning is lost.

Do not infer additional semantic cuts. Preserve all other source ranges.
Create decision records bound to the current proposal, instruction hash,
source hash, and target revision. Render join previews and flag any uncertain
boundary for my review.
```

### 4. Request prompt-writing speed-ups

```text
For <project-id>, propose speed-ups only for visible prompt-writing or
dictation actions.

Allowed action ranges:
- <start>-<end>: typing the prompt into <visible prompt box>
- <start>-<end>: dictating the prompt while <visible UI state>

Requirements:
- begin and end exactly on the visible action;
- retain audible production sound;
- preserve original voice pitch;
- never speed up browsing, reading, loading, waiting, navigation, cursor
  wandering, or unrelated work;
- rebase captions, cuts, joins, and zooms through the retimed source map;
- if the action boundary is uncertain, leave it at normal speed and report it.

Create the evidence frames, retimed timeline, focus/pacing QA, and a Gate 1
packet. Do not apply the speed-up without explicit approval.
```

### 5. Request purposeful screen-recording zooms

```text
Review <project-id> for purposeful UI focus. Do not zoom the intro or title
card. Add a zoom only for a clearly visible and relevant target:

- Target: <window, prompt box, control, or cursor action>
- Appears at: <time>
- Relevant action ends at: <time>
- Why it matters: <reason>

Center the actual target, start after it appears, end before unrelated content,
use smooth frame-driven ease-in/ease-out motion, and keep the full frame stable.
Reject targetless, decorative, jittery, snapping, drifting, edge-clipping, or
whole-screen zooms. Produce target evidence frames and focus QA. If there is
no verifiable target, use no zoom and explain why.
```

### 6. Request captions and visual layers

```text
For <project-id>, create a caption and visual composition proposal.

Captions:
- language: <language>
- style: <caption style>
- font file: <local approved font path>
- maximum lines: <1 or 2>
- safe-area policy: <policy>

Layers:
- background: <solid, local image, or local video asset>
- title/text: <copy and time range>
- subject: <original frame or approved chroma-key layer>
- captions: in front of the subject
- optional text-behind-subject layer: <copy and time range>

Use local hashed assets only. Validate dimensions, timing, z-order, contrast,
safe areas, glyph coverage, and provenance. Render a still and a short proof
before asking for Gate 2. Do not claim visual success from a render exit code.
```

### 7. Request green-screen processing

```text
For <project-id>, use the green-screen-first path.

Source range: <start>-<end>
Key color/sample guidance: <description>
Replacement background: <local asset path>
Object recolor, if any: <object and color>

Prefer deterministic chroma key. Do not invoke SAM 3.1 or MatAnyone 2. Write
the mask/alpha diagnostics, verify polarity, dimensions, frame alignment,
edge quality, spill, holes, and subject identity, then layer the subject over
the approved background. Keep uncertain masks blocked for review. Produce a
short proof and QA findings before Gate 2.
```

### 8. Review a candidate without confusing automation for approval

```text
Review this candidate for <project-id>:

Candidate: <absolute-local-candidate-mp4>
Review packet: <absolute-local-review-packet>
Revision: <revision-id>

Inspect the actual preview and evidence. Check A/V sync, every join, clipped
words, clicks, room-tone jumps, duplicate or missing words, cadence, black
flashes, frozen frames, captions, safe areas, mask/alpha edges, object
identity, text occlusion, zoom target and boundaries, and prompt-speed action
boundaries. Record each finding as repair, false_positive, accepted_risk, or
reject_candidate with evidence and a reason.

Do not approve Gate 1, Gate 2, Gate 3, delivery, backup, or cleanup on my
behalf. Keep the candidate and failed evidence if anything fails.
```

### 9. Finish only after the human gates

```text
The candidate for <project-id> is ready for my review. Show:

- candidate path and revision;
- review packet, final QA, join QA, visual evidence, and stream diagnostics;
- all warnings and any accepted overrides;
- source, asset, composition, and approval hashes;
- exact Gate 1 and Gate 2 bindings.

Wait for my explicit Gate 3 approval. After I approve, write publishing
metadata, promote the MP4 to the configured outputs directory, run strict
FFmpeg decode and stream validation, verify the backup by SHA-256, and report
the final path. Keep source deletion and cleanup as separate decisions.
```

### 10. Recover or resume after interruption

```text
Resume <project-id> safely.

Read the persisted status, stage state, source manifest, active revision,
approval records, and last error. Reuse a stage only when its input hashes,
configuration, implementation version, model identity, and output validation
still match. Recover incomplete stages into staging, validate them, and
promote atomically. Do not rerun a paid or remote request whose state is
unknown. Do not overwrite valid prior artifacts. Report what was reused,
recomputed, quarantined, or blocked.
```

### 11. Use an AMD GPU for previews only

```text
I have an AMD GPU. Run `videoedit doctor --json` and report whether the local
FFmpeg build exposes `h264_amf`. If it does, use it only for a disposable
preview or proxy after recording the encoder capability and settings. Keep the
production delivery profile on software libx264 QP 0 with PCM f32le unless I
explicitly choose another compatible delivery profile. Do not claim that AMF
is lossless, and do not start SAM 3.1 or MatAnyone 2.
```

### 12. A reusable project brief template

```text
Project: <project-id>
Source: <absolute-local-path>
Revision: <revision-id or new>
Audience: <audience>
Purpose: <one sentence>
Delivery: <MP4 profile, dimensions, frame rate, audio>

Editorial must keep:
- <fact, phrase, visual, or section>

Editorial must remove:
- <exact phrase or source range>

Allowed mechanical cleanup:
- <fillers, pauses, repetitions, or none>

Allowed retiming:
- <exact visible prompt-writing/dictation ranges, or none>

Allowed focus:
- <target and evidence range, or none>

Visual direction:
- background: <local asset or none>
- captions: <style>
- text: <copy and ranges>
- green screen: <yes/no and key details>

Constraints:
- preserve original source;
- no cloud/provider calls;
- no SAM 3.1 or MatAnyone 2;
- stop for uncertain identity, missing licence, missing dependency, or failed QA.

Return a plan and the exact Gate 1 questions before making semantic changes.
```

The workflow remains approval-gated: a prompt can request a plan or a repair,
but only an explicit human approval bound to the current hashes can authorize
cuts, effects, segment locks, final delivery, or cleanup.

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
