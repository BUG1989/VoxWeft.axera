#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env.ax.local ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.ax.local
  set +a
fi

PYTHON=${PYTHON:-.venv-ax/bin/python}
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON; create it with:" >&2
  echo "  python3.10 -m venv .venv-ax" >&2
  echo "  .venv-ax/bin/pip install -r requirements-ax.txt" >&2
  exit 1
fi

export VOXWEFT_BACKEND=${VOXWEFT_BACKEND:-ax8860}
export AX_ASR_MODEL=${AX_ASR_MODEL:-default}
export AX_MT_MODEL=${AX_MT_MODEL:-default}
export AX_TTS_MODEL=${AX_TTS_MODEL:-voxcpm2}
export PYTHONPATH=.
exec "$PYTHON" -m voxweft.runtime.server \
  --host "${VOXWEFT_HOST:-0.0.0.0}" \
  --port "${VOXWEFT_PORT:-3100}"
