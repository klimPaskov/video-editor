# Contributing

Contributions should improve the deterministic, local-first workflow without
weakening its review or safety boundaries.

1. Install the repository with `scripts/setup.ps1` or `scripts/setup.sh`.
2. Keep source video, generated outputs, credentials, checkpoints, and private
   assets out of Git.
3. For behavior changes, update the relevant schema, tests, examples, and
   user-facing documentation together.
4. Use typed process adapters and argument arrays for FFmpeg, ffprobe, Node,
   and other external commands.
5. Preserve immutable ingest, hash-bound approvals, explicit human gates, and
   atomic staging/promotion.
6. Run the checks in `scripts/check-core.sh` (or the equivalent PowerShell
   commands) before opening a pull request.

Small, focused pull requests are easier to review. Include the fixture or
diagnostic evidence needed to reproduce any media behavior change.
