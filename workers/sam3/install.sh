#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${SAM3_ENV_DIR:-$HERE/.venv}"
UPSTREAM_DIR="${SAM3_UPSTREAM_DIR:-$HERE/upstream}"
SAM3_REF="${SAM3_REF:-}"

command -v uv >/dev/null 2>&1 || { echo "uv is required" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 2; }

if [[ ! "$SAM3_REF" =~ ^[0-9a-f]{40}$ ]]; then
  cat >&2 <<'MSG'
Set SAM3_REF to an operator-approved 40-character commit.
Tags and main are not accepted because they can move.
Review the current official SAM repository, licence, checkpoint terms, Python,
PyTorch, CUDA, and driver requirements before running this script.
MSG
  exit 2
fi

if ! command -v nvidia-smi >/dev/null 2>&1 || \
  ! nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader >/dev/null 2>&1; then
  echo "nvidia-smi cannot verify a target CUDA GPU; installation is blocked." >&2
  exit 3
fi

uv python install 3.12
uv venv --python 3.12 --seed "$ENV_DIR"
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"
python -m pip install --upgrade pip

if [[ ! -d "$UPSTREAM_DIR/.git" ]]; then
  git clone https://github.com/facebookresearch/sam3.git "$UPSTREAM_DIR"
fi

git -C "$UPSTREAM_DIR" fetch --all --tags --prune
git -C "$UPSTREAM_DIR" checkout --detach "$SAM3_REF"
RESOLVED_COMMIT="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
if [[ "$RESOLVED_COMMIT" != "$SAM3_REF" ]]; then
  echo "The upstream checkout did not resolve to the requested commit." >&2
  exit 4
fi

python -m pip install -e "$UPSTREAM_DIR"

cat <<MSG
SAM 3 worker environment prepared at:
  $ENV_DIR

Pinned upstream commit:
  $RESOLVED_COMMIT

Manual steps still required:
  1. Confirm the installed PyTorch and CUDA builds match the pinned upstream requirements.
  2. Request and accept official checkpoint access.
  3. Authenticate with the official checkpoint host.
  4. Record the checkpoint identity and hash when available.
  5. Run only a short licensed dry run and live smoke test.
  6. Review masks before enabling the worker for project footage.
MSG
