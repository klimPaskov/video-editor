# Contributing

Contributions should improve the deterministic, local-first workflow without
weakening its review or safety boundaries.

## First contribution

1. Start with [`README.md`](README.md) and [`docs/31_user_quickstart.md`](docs/31_user_quickstart.md).
2. Install the repository with `scripts/setup.ps1` or `scripts/setup.sh`.
3. Run `uv run videoedit doctor --json` and the repository checks before editing.
4. Keep source video, generated outputs, credentials, checkpoints, and private
   assets out of Git.
5. For behavior changes, update the relevant schema, tests, examples, and
   user-facing documentation together.
6. Use typed process adapters and argument arrays for FFmpeg, ffprobe, Node,
   and other external commands.
7. Preserve immutable ingest, hash-bound approvals, explicit human gates, and
   atomic staging/promotion.
8. Run the checks in `scripts/check-core.sh` (or the equivalent PowerShell
   commands) before opening a pull request.

The development extra includes NumPy only for validation-only worker contract
fixtures; it does not install or run SAM 3.1 or MatAnyone 2 inference.

## Documentation and prompts

User-facing changes belong in the README, `docs/31_user_quickstart.md`, or
`examples/input-prompts/`. Prompt examples should describe a real creator
scenario with a project id, source context, quoted editorial instructions,
visible timing, forbidden actions, expected evidence, and the approval boundary.
Avoid examples such as “make this better” or placeholders with no surrounding
story; use the existing Hermes-agent tutorial and green-screen examples as the
style reference.

Small, focused pull requests are easier to review. Include the fixture or
diagnostic evidence needed to reproduce any media behavior change.
