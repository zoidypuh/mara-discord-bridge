#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ ! -d .venv ]]; then
  uv venv --python python3.12
fi

source .venv/bin/activate
uv pip install -q -r requirements.txt

exec python discord_mara_bridge.py "$@"
