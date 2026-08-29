#!/usr/bin/env bash
#
# run.sh — launch Icebreaker Connect from the project virtualenv on Linux.
#
# Prefers the venv created by setup_linux.sh; falls back to `python -m` if the
# package is importable in the active interpreter. Passes through any arguments
# (e.g. --check, --doctor).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [[ -x "$VENV/bin/icebreaker-connect" ]]; then
  exec "$VENV/bin/icebreaker-connect" "$@"
fi
if [[ -x "$VENV/bin/python" ]]; then
  exec "$VENV/bin/python" -m connection_assistant "$@"
fi
echo "No .venv found. Run scripts/setup_linux.sh first (or activate your env)." >&2
exec python3 -m connection_assistant "$@"
