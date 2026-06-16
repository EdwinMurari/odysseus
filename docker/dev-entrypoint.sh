#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
GOSU_BIN="$(command -v gosu)"
PYTHON_BIN="$(command -v python)"

export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
export PATH="/app/.local/bin:$PATH"

"$GOSU_BIN" "$PUID:$PGID" "$PYTHON_BIN" /app/setup.py || true
exec "$GOSU_BIN" "$PUID:$PGID" "$@"
