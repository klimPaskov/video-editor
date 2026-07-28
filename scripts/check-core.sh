#!/usr/bin/env bash
set -euo pipefail

uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/validate_examples.py
(
  cd remotion
  npm run typecheck
)
