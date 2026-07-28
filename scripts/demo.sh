#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:-demo-project}"
uv run videoedit make-demo "$PROJECT_ID" --render
uv run videoedit qa "projects/$PROJECT_ID/output/demo-final.mp4"
