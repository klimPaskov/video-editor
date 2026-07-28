# Documentation guide

Choose the page that matches what you are trying to do:

| Goal | Read |
| --- | --- |
| Install and run a first real project | [`31_user_quickstart.md`](31_user_quickstart.md) |
| Understand the complete operator sequence | [`11_runbook.md`](11_runbook.md) |
| Copy a realistic request into Codex | [`../examples/input-prompts/README.md`](../examples/input-prompts/README.md) |
| Troubleshoot installation | [`../INSTALL.md`](../INSTALL.md) |
| Understand the repository layout | [`17_repository_layout.md`](17_repository_layout.md) |
| Check the executable command surface | [`16_cli_contract.md`](16_cli_contract.md) |
| Understand architecture and boundaries | [`02_architecture.md`](02_architecture.md) |
| Review accepted decisions | [`adr/`](adr/) |
| Review QA and evidence requirements | [`13_qa_strategy.md`](13_qa_strategy.md), [`19_review_contract.md`](19_review_contract.md) |

## Public workflow defaults

The normal workflow is local and worker-free: Python 3.11, FFmpeg/ffprobe,
local Whisper, and Node.js 22 with Remotion. Windows with an AMD GPU is
supported for bounded previews when `videoedit doctor` confirms the AMF
encoder. The production master remains on the selected lossless profile.

SAM 3.1 and MatAnyone 2 are retained as optional, isolated contract boundaries
only. They are not installed, invoked, or required by the public workflow.

## Evidence and privacy

The workflow keeps source media immutable and binds approvals to current hashes.
Do not commit recordings, transcripts, generated media, credentials, model
checkpoints, or private assets. Project runtime data is ignored by Git; record
local asset hashes and licence or permission details in the project manifest.

The repository is MIT-licensed. Third-party media, fonts, models, and
checkpoints keep their own terms.
