# Manual Install Matrix

## Required operator actions

| Stage | Action | Evidence to retain | Codex stop condition |
|---|---|---|---|
| Foundation | Install Git, FFmpeg, ffprobe, fonts support, `uv`, Python 3.11, Node.js 22, and Codex | Version output and capability report | Required command or FFmpeg filter is missing |
| Remotion | Install project packages and official agent skills | Lockfile, type-check output, skill presence | Licence path is unknown or installation fails |
| Brand | Supply fonts, logos, colors, backgrounds, sounds, B-roll, and replacements | Asset manifest with hashes and licences | Asset provenance is missing |
| Storage | Select project workspace and backup destination | Path configuration and free-space report | Paths are unsafe or storage is insufficient |
| SAM 3.1 (optional deferred extension) | No action for the final workflow. If explicitly re-enabled, accept licence, request checkpoint access, verify GPU and CUDA, then create the hash-bound worker runtime approval | Licence decision, checkpoint ID and SHA-256, repo commit, driver report, `worker_runtime_approval` | If re-enabled: access, licence, runtime approval, or hardware is unresolved |
| MatAnyone 2 (optional deferred extension) | No action for the final workflow. If explicitly re-enabled, review licence, verify GPU environment and checkpoint, then create the hash-bound worker runtime approval | Licence decision, model ID and SHA-256, repo commit, runtime report, `worker_runtime_approval` | If re-enabled: output semantics, permitted use, runtime approval, or hardware is unresolved |
| Paid provider | Create account and bounded budget only when selected | Approval record and budget cap | Approval, credentials, or estimate is missing |
| Cleanup | Approve deletion after backup verification | Backup checksum report and cleanup approval | Backup or approval is missing |

## Codex-managed repository work

Codex should implement dependency checks, typed adapters, schemas, tests, render commands, diagnostics, and documentation. It should not perform the legal, financial, credential, or destructive actions listed above without the required gate.
