#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command '$1' was not found on PATH. Install it and run setup again." >&2
    exit 2
  }
}

for command_name in uv node npm ffmpeg ffprobe; do
  require_command "$command_name"
done

node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
if [[ ! "$node_major" =~ ^[0-9]+$ || "$node_major" -lt 22 ]]; then
  echo "Node.js 22 or newer is required; found $(node --version)." >&2
  exit 2
fi

if [[ "${SKIP_WHISPER:-0}" == "1" ]]; then
  uv sync --extra dev
else
  uv sync --extra dev --extra whisper
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
else
  echo "Kept existing .env."
fi

(cd remotion && npm ci)

echo "Setup complete. Next: run 'uv run videoedit doctor --json'."
if [[ "${SKIP_WHISPER:-0}" == "1" ]]; then
  echo "Whisper extra was skipped; rerun without SKIP_WHISPER=1 before transcribing."
else
  echo "Whisper model download is explicit; see README.md for the hash-verified helper."
fi
