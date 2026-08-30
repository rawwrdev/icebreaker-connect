# Icebreaker Connect

Use this small desktop app to connect your own Tinder account to Icebreaker.

## Get started

You need your **computer** and your **phone**.

1. On your computer, open **[bot.rawwr.dev/download](https://bot.rawwr.dev/download)**.
2. Download the app for your computer and open it.
3. Follow the instructions in the app and sign in to your Tinder account when asked.
4. When the app shows a QR code, scan it with your phone, open the link, and approve
   the connection in Telegram.

Keep the desktop app open until it says the connection is complete. Then return to
Icebreaker on your phone. That is all you need to do.

Only connect a Tinder account that belongs to you. Never send anyone a Tinder token,
session file, or screenshot containing account details.

## For developers

The rest of this page covers how the assistant works, security details, development,
and packaging. End users do not need to follow these instructions.

---

## What it does

A guided five-step flow:

1. **Ownership & consent** — you confirm you own the account and understand the
   provider-terms implications.
2. **Set up, open Tinder & capture** — one screen checks the environment and lists
   every configured emulator in a dropdown. Checked entries show whether Tinder is
   installed and its version. Selecting an emulator starts it automatically; the
   same dropdown can create a new rootable emulator. If Tinder is missing, downloading
   it or choosing an APK/XAPK installs it and starts capture. No Next/Back clicks are needed. You can
   stop capture or stop the emulator from the GUI. If Tinder is missing, the app can
   download the latest XAPK from the user-approved APKPure endpoint, or install an
   APK/XAPK selected by the user. Tinder is never bundled or redistributed here.
3. **Captured fields** — an automatic, value-free summary of what was captured.
4. **QR approval / Save locally / Cancel & erase** — the desktop displays a QR that
   the account owner scans with their phone, then opens an authenticated approval
   screen in the Telegram Mini App. After approval, the desktop uploads the session
   directly over HTTPS to the built-in production service (`https://bot.rawwr.dev`);
   the user never needs to find, enter, or change its URL.
5. **Result** — clear success or an actionable, sanitized failure.

### What gets captured (and nothing else)

`auth_token`, optional `refresh_token`, `device_id`, `install_id`, and an
allowlisted `session_profile` (stable app/OS identity headers). See
[`protocol/session-bundle.schema.json`](protocol/session-bundle.schema.json). A
Tinder CAPTCHA/rules-engine challenge is detected and **never** mistaken for a
credential.

---

## Security model

- Captured secrets stay **in memory** by default; JSON is written **only** on an
  explicit Save, with **owner-only (0600)** permissions where the OS supports it.
- The optional APKPure XAPK download is stored in a temporary file, validated as a
  Tinder package, and deleted immediately after installation or on cleanup.
- Token values are never printed, logged, or placed in progress events.
- The capture subprocess returns results over an **anonymous channel** (a POSIX
  pipe; a nonce-guarded loopback socket on Windows) — never a credential file. No
  `.env`, no traffic dump, no refresh bytes, no provider response bodies are written.
- Interception uses an **exact host allowlist** (`api.gotinder.com`); captured
  headers are limited to the known session-identity fields.
- The built-in production service uses **HTTPS**. Programmatic development
  overrides must also use HTTPS, except explicit `localhost`.
- Pairing uses a random, single-use, user-bound, short-lived, replay-resistant
  upload token that is **never** placed in the QR code.
- The QR contains only a short-lived Telegram Mini App approval reference. Tinder
  tokens never pass through the QR, phone camera, Telegram message, or clipboard.
- Every exit path (success, cancel, crash, window close) clears the Android proxy,
  stops capture, stops any emulator this app started, and wipes the in-memory bundle.

---

## Install & run

The whole experience is a **GUI on every platform** — no terminal is required after
the app is on the machine:

- **Host tools** (mitmproxy, adb, Java, openssl) install from the combined capture
  screen via your OS package manager, with a graphical admin prompt.
- **Android SDK/emulator** download from Google on the same screen.
- **Double-click launchers** open the window with no console:
  Linux `.desktop` entry, macOS `.app`, Windows Start-Menu shortcut / `run_windows.vbs`.
- The packaged Windows app asks for administrator access once when it opens. Setup
  tools launched during that session inherit access instead of showing repeated prompts.

The one-command `setup_*` scripts below are an optional convenience for developers;
end users can do everything from the GUI. Requirements on the host: Python 3.12+ (or
use a packaged bundle from `scripts/build.py app`, which needs no Python), plus KVM on
Linux / WHPX on Windows for emulator acceleration (macOS needs nothing extra).

### Fully-GUI, no terminal

1. Get the app on the machine — either a built bundle (`python scripts/build.py app`)
   or `pip install` once, then add a double-click launcher:
   - Linux: `packaging/linux/install_desktop.sh`
   - macOS: `packaging/macos/install_app.sh` (creates a real `.app`)
   - Windows: `packaging\windows\install_shortcut.ps1` (Start-Menu shortcut)
2. Launch it. If prerequisites are missing, use **Install host tools** and
   **Download & set up Android SDK** on the capture screen. Once ready, the app
   starts the emulator and capture automatically.

### Linux (one command)

```bash
scripts/setup_linux.sh --dev     # apt prereqs + KVM group + venv + install (+ dev extras)
scripts/run.sh                   # launch the GUI
```

`setup_linux.sh` installs `adb mitmproxy default-jdk qemu-kvm openssl` via apt, adds
you to the `kvm` group (log out/in once for it to take effect), creates `.venv`, and
installs the app. Use `--no-system` to skip apt/kvm if you manage those yourself.
Add a menu entry with `packaging/linux/install_desktop.sh`.

### macOS (one command)

```bash
scripts/setup_macos.sh --dev     # Homebrew prereqs + venv + install (+ dev extras)
scripts/run.command              # launch the GUI (or double-click it in Finder)
```

Installs `mitmproxy`, `android-platform-tools` (adb), `openssl`, and a Temurin JDK
via Homebrew. macOS uses Apple's Hypervisor.framework, so no HAXM/KVM is needed on
Apple Silicon or Intel. The app fetches the arm64-v8a system image on Apple Silicon
and x86_64 on Intel.

### Windows (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Dev
scripts\run_windows.bat          # launch the GUI (or double-click it)
```

Installs `mitmproxy`, a Temurin JDK, and `openssl` via winget. Enable the **Windows
Hypervisor Platform** optional feature (or install Intel HAXM) and reboot once so the
emulator can use hardware acceleration. If the emulator cannot start, the app keeps
only a short, sanitized in-memory tail of its startup output and shows the relevant
repair instruction; it is never included with captured account data. When acceleration
is unavailable (for example, inside VirtualBox), the app retries automatically in a
very slow software-only test mode with software graphics, reduced RAM and virtual
audio and Vulkan disabled for compatibility.

### Any platform (manual)

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

icebreaker-connect          # launch the guided GUI
icebreaker-connect --check  # environment readiness report (no capture)
icebreaker-connect --doctor # readiness + resolved tool paths
```

The production pairing service is preconfigured, so users do not need to find,
enter, or change a URL. **Save locally** remains available on the delivery screen.

## Test

```bash
. .venv/bin/activate
ruff check src tests scripts
QT_QPA_PLATFORM=offscreen pytest -q
```

All tests use fabricated credentials and a fake HTTP transport — no real account,
emulator, or network is required.

## Build

See [`packaging/README.md`](packaging/README.md).

```bash
python scripts/build.py wheel   # sdist + wheel
python scripts/build.py app     # per-OS PyInstaller bundle
```

These commands are self-bootstrapping and non-interactive: when launched with a
system Python (including Debian/Ubuntu's PEP 668 externally managed Python), the
script creates or reuses `.venv`, installs the app and build dependencies there,
and re-runs the requested build. You do not need to activate the environment or
install PyInstaller yourself. The first build needs internet access to download
dependencies that are not already present; later builds reuse the environment.

---

## Pairing contract (for the private Icebreaker backend)

The desktop expects three HTTPS endpoints, specified in
[`protocol/pairing-api.yaml`](protocol/pairing-api.yaml):

- `POST /desktop-pairings` — create a short-lived, user-bound pairing; returns
  `pairing_id`, a QR-safe `verification_uri`, and the secret one-time `upload_token`.
- `GET  /desktop-pairings/{id}` — returns `pending | approved | consumed | expired
  | rejected`.
- `POST /desktop-pairings/{id}/session` — upload the validated bundle using the
  `upload_token` as a bearer; the server marks the pairing `consumed` (replay-safe).

**What the private backend must implement** (not part of this repo):

- Generate the pairing, bind approval to the authenticated Telegram Mini App user,
  and expire pairings quickly.
- Keep `upload_token` out of `verification_uri`/the QR; treat it as single-use.
- On successful upload, confirm to the bound Telegram user via the bot. The session
  bundle must travel **only** over this HTTPS API — never through Telegram.
- Store the received bundle in the owner's per-user credential storage.

No Telegram bot token or server secret is embedded in this app.

---

## Scope

This project contains only the onboarding experience: the desktop app/UI, Android
preparation, minimal capture, the pairing/upload client, local JSON export, build
entrypoints, and focused tests. It intentionally excludes any other Icebreaker
functionality. It does not modify the private Icebreaker repository.
