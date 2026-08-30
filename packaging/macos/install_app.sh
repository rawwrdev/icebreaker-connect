#!/usr/bin/env bash
#
# install_app.sh — create a double-clickable macOS .app for the GUI (no terminal).
#
# Generates "~/Applications/Icebreaker Connect.app" whose launcher
# execs the venv's GUI directly, so Finder opens the window with no Terminal
# window. Run scripts/setup_macos.sh first so the venv exists.
#
#   packaging/macos/install_app.sh              # install
#   packaging/macos/install_app.sh --uninstall  # remove
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR="$HOME/Applications/Icebreaker Connect.app"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$APP_DIR"
  echo "Removed $APP_DIR"
  exit 0
fi

mkdir -p "$APP_DIR/Contents/MacOS"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Icebreaker Connect</string>
  <key>CFBundleDisplayName</key><string>Icebreaker Connect</string>
  <key>CFBundleIdentifier</key><string>com.icebreaker.connect</string>
  <key>CFBundleVersion</key><string>0.1.11</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# The launcher runs the GUI directly from the project venv (no Terminal window).
cat > "$APP_DIR/Contents/MacOS/launcher" <<LAUNCH
#!/usr/bin/env bash
ROOT="$ROOT"
if [[ -x "\$ROOT/.venv/bin/icebreaker-connect" ]]; then
  exec "\$ROOT/.venv/bin/icebreaker-connect"
fi
exec "\$ROOT/.venv/bin/python" -m connection_assistant
LAUNCH
chmod +x "$APP_DIR/Contents/MacOS/launcher"

echo "Installed $APP_DIR"
echo "Open it from Finder / Launchpad (no terminal appears)."
