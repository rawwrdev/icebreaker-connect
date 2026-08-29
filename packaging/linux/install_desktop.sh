#!/usr/bin/env bash
#
# install_desktop.sh — add a per-user application menu entry on Linux.
#
# Installs a .desktop file into ~/.local/share/applications pointing at
# scripts/run.sh, so the assistant appears in your desktop's app launcher.
# Removes it again with:  install_desktop.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/packaging/linux/icebreaker-connect.desktop"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$DEST_DIR/icebreaker-connect.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$DEST"
  echo "Removed $DEST"
  command -v update-desktop-database >/dev/null && update-desktop-database "$DEST_DIR" || true
  exit 0
fi

mkdir -p "$DEST_DIR"
sed "s|__EXEC__|$ROOT/scripts/run.sh|g" "$SRC" > "$DEST"
chmod 644 "$DEST"
command -v update-desktop-database >/dev/null && update-desktop-database "$DEST_DIR" || true
echo "Installed $DEST"
echo "Launch it from your application menu, or run: $ROOT/scripts/run.sh"
