#!/usr/bin/env bash
#
# setup_linux.sh — prepare Icebreaker Connect on Linux.
#
# Installs the host prerequisites the app cannot fetch itself (adb, mitmproxy,
# Java, KVM), creates a virtualenv, and installs the app into it. The Android SDK,
# emulator and system image are NOT installed here — the app downloads those from
# official Google sources during its own "Environment" step. The Tinder APK is
# always supplied by you.
#
# Usage:
#   scripts/setup_linux.sh              # full setup (may use sudo for apt + kvm)
#   scripts/setup_linux.sh --no-system  # skip apt/kvm; only venv + pip install
#   scripts/setup_linux.sh --dev        # also install dev extras (pytest, ruff)
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
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# -- 1. host prerequisites ----------------------------------------------------
# adb, mitmproxy (mitmdump), openssl and a JDK are needed for a real capture;
# qemu-kvm gives the emulator hardware acceleration. Only apt is automated; on
# other distros install the same packages with your package manager.
if [[ "$DO_SYSTEM" -eq 1 ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    say "Installing host prerequisites via apt (adb, mitmproxy, JDK, KVM, openssl)..."
    sudo apt-get update -y
    sudo apt-get install -y \
      adb mitmproxy default-jdk qemu-kvm openssl python3-venv python3-pip
    if getent group kvm >/dev/null 2>&1; then
      if ! id -nG "$USER" | tr ' ' '\n' | grep -qx kvm; then
        say "Adding $USER to the 'kvm' group (log out/in for it to take effect)..."
        sudo adduser "$USER" kvm || warn "could not add to kvm group; add it manually"
      fi
    fi
  else
    warn "apt-get not found. Install these yourself, then re-run with --no-system:"
    warn "  adb  mitmproxy(mitmdump)  a JDK (17+)  qemu-kvm  openssl  python3-venv"
  fi
fi

# -- 2. python check ----------------------------------------------------------
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || die "python3 not found"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.1[2-9]|3.[2-9][0-9]) : ;;
  *) die "Python 3.12+ required, found $PYVER" ;;
esac
say "Using Python $PYVER"

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
  Or via the launcher:    scripts/run.sh
  Re-check the toolchain: $VENV/bin/icebreaker-connect --doctor

If you were just added to the 'kvm' group, log out and back in before starting
the emulator. The app will download the Android SDK/emulator from Google during
its Environment step; supply your own Tinder APK when prompted.
EOF
