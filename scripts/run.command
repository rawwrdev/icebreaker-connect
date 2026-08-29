#!/usr/bin/env bash
#
# run.command — double-clickable macOS launcher for Icebreaker Connect.
#
# Finder runs .command files in Terminal. Prefers the venv created by
# setup_macos.sh; falls back to `python3 -m`. Passes through any arguments.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [[ -x "$VENV/bin/icebreaker-connect" ]]; then
  exec "$VENV/bin/icebreaker-connect" "$@"
fi
if [[ -x "$VENV/bin/python" ]]; then
  exec "$VENV/bin/python" -m connection_assistant "$@"
fi
echo "No .venv found. Run scripts/setup_macos.sh first (or activate your env)." >&2
exec python3 -m connection_assistant "$@"
