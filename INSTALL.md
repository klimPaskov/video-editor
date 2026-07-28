# Installation Plan

This document separates the local core from optional GPU workers. Complete only the section needed for the active phase.

## Supported baseline

The preferred implementation environment is Linux or WSL2 with an NVIDIA GPU available only for later worker phases. The deterministic core also works without an NVIDIA GPU. On Windows, an AMD GPU may be used for an explicitly tested FFmpeg AMF encode, but AMF does not satisfy the current SAM 3.1 or MatAnyone 2 CUDA worker preconditions and must not silently replace software or lossless encoders.

## Core tools required before P0

Install:

- Git
- curl
- FFmpeg and ffprobe with the filters and encoders required by the selected delivery profile
- fontconfig on Linux
- build tools needed by Python dependencies
- `uv`
- Python 3.11 through `uv`
- Node.js 22 and npm
- Codex CLI or the Codex app

Example Ubuntu or WSL package command:

```bash
sudo apt update
sudo apt install -y git curl ffmpeg fontconfig build-essential
```

Install `uv` using its current official installer, then:

```bash
uv python install 3.11
uv sync --extra dev
```

Install Node.js 22 through a version manager or an approved system package. Confirm:

```bash
node --version
npm --version
```

If the system Node.js version must remain unchanged, the core accepts explicit
per-process overrides instead:

```powershell
$env:VIDEOEDIT_NODE_PATH = "C:\path\to\node-v22.23.1-win-x64\node.exe"
$env:VIDEOEDIT_NPM_PATH = "C:\path\to\node-v22.23.1-win-x64\npm.cmd"
uv run videoedit doctor --json
```

The same `VIDEOEDIT_NPM_PATH` is used by every Remotion command, including
`make-demo --render`, composition listing, still/segment renders, and
hash-bound asset-layer commands. This keeps the compositor on the configured
Node.js 22 runtime instead of silently falling back to an unrelated `npm` on
`PATH`.

The verified Windows portable-runtime evidence is retained in
`.codex/evidence/P0/node22-portable-20260725.json`; its archive was checked
against the official Node.js SHA-256 manifest before extraction.

## Repository checks

From the repository root:

```bash
uv run videoedit doctor --json
uv run python scripts/validate_examples.py
uv run pytest -q
```

When the local FFmpeg build exposes AMD AMF, `videoedit doctor --json` runs a
bounded one-frame `h264_amf` probe through the typed process adapter. A passing
`FFmpeg AMD AMF` check records media-encoder readiness only; it does not replace
the software or lossless defaults and does not satisfy the NVIDIA/CUDA
preconditions for the SAM 3.1 or MatAnyone 2 workers.

The production render commands read the optional encoder profile from `.env`:

```text
VIDEOEDIT_VIDEO_CODEC=libx264
VIDEOEDIT_VIDEO_BITRATE_BPS=4000000
```

Set `VIDEOEDIT_VIDEO_CODEC=h264_amf` only after the AMD AMF doctor check passes.
The setting is bound into base, retimed, final-assembly, delivery, proxy, demo,
effect, and media-QA operations; `libx264` remains the default, and lossless
intermediates and alpha/mask outputs remain on their explicit codecs. AMF changes
media encoding only; it does not authorize or enable the isolated SAM 3.1 or
MatAnyone 2 workers.

From `remotion/`:

```bash
npm ci
npm run typecheck
```

The committed `remotion/package-lock.json` is the reproducibility lockfile. Do not
commit `node_modules`; review `npm audit` findings before changing the declared
Remotion dependency versions.

Do not commit `node_modules` or virtual environments.

## Remotion skill

The package includes repository-specific Remotion rules. It also includes `scripts/install-remotion-skill.sh` to invoke the current agent skill installer against the official Remotion skills repository.

Review every file added or changed by that installer before committing it. Upstream layout can change. The repository-specific skill remains authoritative for local contracts and gates.

## Fonts and assets

The operator must provide licensed files for:

- brand fonts
- logos
- backgrounds
- B-roll
- sound effects
- replacement objects
- intro and outro elements

Store private assets outside Git or in an ignored asset directory. Add each asset to the local asset index with a hash, source, licence, permitted use, and attribution requirements.

## Whisper

The baseline uses local OpenAI Whisper through the Python optional dependency. FFmpeg is required. Choose the model and device in local configuration. Keep a deterministic fake transcription adapter for tests.

No OpenAI API key is required for the local Whisper path.

The adapter never downloads a model implicitly. To explicitly provision one,
use the operator-invoked, hash-pinned helper and then set
`VIDEOEDIT_WHISPER_MODEL_PATH`:

```powershell
pwsh -File .\scripts\fetch-whisper-model.ps1 -Model small -Destination C:\path\to\whisper\small.pt
$env:VIDEOEDIT_WHISPER_MODEL_PATH = "C:\path\to\whisper\small.pt"
```

On Unix-like systems use `scripts/fetch-whisper-model.sh`. The helpers use the
official OpenAI public model URL, verify SHA-256, stage atomically, and refuse
to overwrite an existing file. The `openai-whisper` package and model path
remain subject to the operator's intended-use and licence review.

## SAM 3.1, optional deferred extension

Do not run the SAM installer for the final workflow. The required path does not
need it. If a future accepted decision re-enables this extension, do not run
the installer unattended and complete the following operator gates first:

Before Phase 7, the operator must:

1. review the current SAM Licence
2. confirm the intended use is permitted
3. request access to the official checkpoints
4. authenticate with the checkpoint host
5. confirm a compatible NVIDIA driver and CUDA-capable GPU
6. review the current official Python, PyTorch, and CUDA requirements
7. pin an upstream repository commit and checkpoint identity in an ADR
8. run only a short licensed smoke test first

The included `workers/sam3/install.sh` is an operator-reviewed starting script. It has network, licence, storage, GPU, and checkpoint implications. Codex must not run it unattended.

An AMD adapter, OpenCL platform, or FFmpeg AMF encoder is not evidence of SAM
3.1 CUDA compatibility. The current worker installer intentionally fails closed
when `nvidia-smi` cannot verify the target runtime.

## MatAnyone 2, optional deferred extension

Do not run the MatAnyone installer for the final workflow. The required path
does not need it. If a future accepted decision re-enables this extension, do
not run the installer unattended and complete the following operator gates
first:

Before Phase 8, the operator must:

1. review NTU S-Lab License 1.0
2. confirm the intended use is permitted
3. confirm a compatible GPU environment
4. review the current official Python and package requirements
5. pin an upstream repository commit and checkpoint identity in an ADR
6. provide or approve a first-frame person mask
7. run only a short licensed smoke test first

The included `workers/matanyone2/install.sh` is an operator-reviewed starting script. Codex must not run it unattended.

The same boundary applies to MatAnyone 2: AMD FFmpeg acceleration can help with
media encoding, but it does not authorize or replace the isolated CUDA worker.

## Optional providers

Cloud transcription, generated B-roll, cloud rendering, storage, and publishing integrations are outside the core installation. Add them only behind provider-neutral adapters after the local workflow passes. Keep network use off by default and require a current bounded spend approval for paid calls.

## Verification record

Record exact versions and relevant capabilities in the P0 phase result and `videoedit doctor` output. For GPU workers, also record:

- GPU model
- driver version
- CUDA runtime
- Python version
- PyTorch version
- upstream repository commit
- checkpoint identity and hash when available
- licence decision reference
- smoke-test artifact hashes
