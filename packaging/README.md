# Packaging

The desktop app is pure-Python (PySide6 + mitmproxy + httpx). Two build outputs are
supported, both driven by [`scripts/build.py`](../scripts/build.py):

| Command | Output | Notes |
|---|---|---|
| `python scripts/build.py wheel` | `dist/*.whl`, `dist/*.tar.gz` | Install with `pip install <wheel>`; run `icebreaker-connect`. |
| `python scripts/build.py app` | `dist/IcebreakerConnect/` | Self-contained onedir bundle built with PyInstaller. |

## Per-platform setup, run, and build

Each OS has a one-command environment setup, a launcher, and the same `app` build.
PyInstaller does **not** cross-compile — build the `app` target once on each OS.

| OS | Setup | Launch | Build output |
|---|---|---|---|
| **Linux** (x86_64) | `scripts/setup_linux.sh` | `scripts/run.sh` | `dist/IcebreakerConnect/` (onedir, ELF) |
| **macOS** (Apple Silicon / Intel) | `scripts/setup_macos.sh` | `scripts/run.command` | `dist/IcebreakerConnect.app` + onedir |
| **Windows** (x86_64) | `scripts\setup_windows.ps1` | `scripts\run_windows.bat` | `dist\IcebreakerConnect\...exe` |

Build on every OS with the same command:

```bash
python scripts/build.py app
```

No virtualenv activation or prior PyInstaller install is required. If the command
starts under a system interpreter, `build.py` creates/reuses the project `.venv`,
installs its dependencies without prompting, and continues inside that isolated
environment. This also avoids Debian/Ubuntu's PEP 668 `externally-managed-environment`
error. The first build downloads any missing dependencies; later builds reuse them.

`build.py` selects the right PyInstaller data separator per OS and passes
`--windowed`, which yields a no-console `.exe` on Windows and a `.app` bundle on
macOS. On Windows it also passes `--uac-admin`, so setup receives elevation from a
single prompt when the app starts. The emulator system image chosen at runtime follows the host arch
(`arm64-v8a` on Apple Silicon, `x86_64` elsewhere).

### Acceleration prerequisites

- **Linux:** KVM (`qemu-kvm`, and membership of the `kvm` group).
- **macOS:** Apple Hypervisor.framework — built in, nothing to install.
- **Windows:** the "Windows Hypervisor Platform" optional feature (or Intel HAXM),
  then one reboot.

### macOS notarization / Windows signing

Distributing outside a trusted channel needs code signing: `codesign` + notarization
on macOS, `signtool` on Windows. Left out of `build.py` to keep it dependency-free;
add it as a post-build step for public distribution.

## Running the Linux bundle

```bash
python scripts/build.py app
./dist/IcebreakerConnect/IcebreakerConnect           # GUI
./dist/IcebreakerConnect/IcebreakerConnect --check   # readiness
```

The bundle is self-contained for the app/UI. For a **real capture** it still calls
the host's `mitmdump` and `adb`/`emulator` as subprocesses, so those must be present
on the machine (on Linux, `scripts/setup_linux.sh` installs `mitmproxy` and `adb`;
the SDK/emulator are fetched by the app). The Environment step reports whether they
are found before any capture is attempted.

## What is and isn't bundled

- **Bundled:** the Python app, PySide6, mitmproxy, the pairing/capture code, and the
  `protocol/` contract files.
- **NOT bundled (by design):** the Android SDK, emulator, system image, and Tinder.
  The SDK/emulator are downloaded from official Google sources during setup; Tinder
  can be downloaded from the configured APKPure endpoint or supplied by the user as
  an APK/XAPK. Nothing proprietary is redistributed.

## Icons / signing

Add `--icon` to the PyInstaller invocation and platform code-signing
(`codesign`/`signtool`) as a follow-up when distributing outside a trusted channel.
Left out here to keep the build dependency-free.
