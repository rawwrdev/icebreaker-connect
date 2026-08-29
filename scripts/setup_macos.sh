#!/usr/bin/env bash
#
# setup_macos.sh — prepare Icebreaker Connect on macOS.
#
# Installs the host prerequisites the app cannot fetch itself (adb, mitmproxy,
# a JDK, openssl) via Homebrew, creates a virtualenv, and installs the app into
# it. The Android SDK, emulator and system image are NOT installed here — the app
# downloads those from official Google sources during its "Environment" step,
# choosing the arm64-v8a image on Apple Silicon and x86_64 on Intel. The Tinder
# APK is always supplied by you.
#
# macOS uses Apple's Hypervisor.framework for emulator acceleration — no KVM/HAXM
# is required on either Apple Silicon or recent Intel Macs.
#
# Usage:
#   scripts/setup_macos.sh              # full setup (uses Homebrew)
#   scripts/setup_macos.sh --no-system  # skip brew; only venv + pip install
#   scripts/setup_macos.sh --dev        # also install dev extras (pytest, ruff)
#
# Re-runnable: every step is idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
DO_SYSTEM=1
EXTRAS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-system) DO_SYSTEM=0; shift ;;
    --dev) EXTRAS="[dev]"; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# -- 1. host prerequisites ----------------------------------------------------
if [[ "$DO_SYSTEM" -eq 1 ]]; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing host prerequisites via Homebrew (adb, mitmproxy, JDK, openssl)..."
    # android-platform-tools provides adb; temurin is a JDK 17+ cask.
    brew install mitmproxy openssl android-platform-tools || warn "some brew formulae failed"
    brew install --cask temurin || warn "could not install the Temurin JDK cask; install a JDK 17+ yourself"
  else
    warn "Homebrew not found. Install it from https://brew.sh, or install these"
    warn "yourself and re-run with --no-system:"
    warn "  adb (android-platform-tools)  mitmproxy  a JDK 17+  openssl"
  fi
fi

# -- 2. python check ----------------------------------------------------------
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 not found (install from python.org or 'brew install python@3.12')"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.1[2-9]|3.[2-9][0-9]) : ;;
  *) die "Python 3.12+ required, found $PYVER" ;;
esac
say "Using Python $PYVER on $(uname -m)"

# -- 3. virtualenv + install --------------------------------------------------
if [[ ! -d "$VENV" ]]; then
  say "Creating virtualenv at $VENV"
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
say "Upgrading pip and installing the app (editable$EXTRAS)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e "$ROOT$EXTRAS"

# -- 4. readiness report ------------------------------------------------------
say "Environment readiness:"
icebreaker-connect --check || true

cat <<EOF

$(say "Setup complete.")
  Run the GUI:            $VENV/bin/icebreaker-connect
  Or double-click:        scripts/run.command
  Re-check the toolchain: $VENV/bin/icebreaker-connect --doctor

The app will download the Android SDK/emulator from Google during its Environment
step (arm64-v8a image on Apple Silicon, x86_64 on Intel). Supply your own Tinder
APK when prompted.
EOF
