#!/usr/bin/env python3
"""Cross-platform build entrypoint for the connection assistant.

Wraps the two supported outputs:

    python scripts/build.py wheel        # build an sdist + wheel (pure-Python)
    python scripts/build.py app          # build a onedir desktop app via PyInstaller
    python scripts/build.py clean        # remove build/ dist/ artifacts

The PyInstaller step produces a per-OS bundle: run it once on each target platform
(Linux, Windows, Intel macOS, Apple Silicon macOS) — PyInstaller does not cross-
compile. The emulator/SDK are NOT bundled; the app downloads them from Google on
first setup, and the user supplies their own APK.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
ENTRY = ROOT / "src" / "connection_assistant" / "__main__.py"
APP_NAME = "IcebreakerConnect"


def _run(args: list[str]) -> None:
    printable = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    print("+", printable, flush=True)
    env = os.environ.copy()
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    subprocess.run(args, check=True, cwd=ROOT, env=env)


def _in_virtualenv() -> bool:
    """Return whether this interpreter is isolated from the system Python."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _bootstrap(target: str) -> int | None:
    """Re-run a build target in an isolated interpreter when necessary.

    Debian and Ubuntu mark their system Python as externally managed (PEP 668),
    so installing build dependencies into the interpreter that launched this
    script is neither reliable nor safe.  A caller already inside any virtual
    environment keeps using it; otherwise we create/reuse the project's .venv.
    """
    if target == "clean" or _in_virtualenv():
        return None

    python = _venv_python()
    if not python.is_file():
        print(f"Creating isolated build environment at {VENV}", flush=True)
        _run([sys.executable, "-m", "venv", str(VENV)])
    if not python.is_file():
        raise RuntimeError(
            f"virtual environment creation did not produce a Python executable at {python}"
        )

    # Python 3.12's venv no longer guarantees setuptools is present. Install the
    # declared build backend before asking pip to install this project with
    # --no-build-isolation inside the fresh environment.
    _run([
        str(python),
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-input",
        "setuptools>=68",
        "wheel",
    ])

    print(f"Re-running with isolated Python: {python}", flush=True)
    env = os.environ.copy()
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return subprocess.run(
        [str(python), str(Path(__file__).resolve()), target],
        cwd=ROOT,
        env=env,
        check=False,
    ).returncode


def _install_build_dependencies() -> None:
    # Installing the project as editable makes all runtime dependencies visible
    # to PyInstaller.  The [build] extra supplies both supported build frontends.
    _run([
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-input",
        "-e",
        ".[build]",
    ])


def build_wheel() -> None:
    # The bootstrap step already installed the declared build backend. Avoid a
    # second temporary environment (and a redundant network download) here.
    _run([sys.executable, "-m", "build", "--no-isolation"])


def build_app() -> None:
    data_sep = ";" if sys.platform == "win32" else ":"
    protocol = ROOT / "protocol"
    capture_addon = ROOT / "src" / "connection_assistant" / "capture" / "mitm_addon.py"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",  # no console window on Win/macOS; primary experience is GUI
        "--collect-all", "mitmproxy",
        "--collect-submodules", "connection_assistant",
        "--add-data", f"{protocol}{data_sep}protocol",
        "--add-data", f"{capture_addon}{data_sep}connection_assistant/capture",
        str(ENTRY),
    ]
    if sys.platform == "win32":
        # Ask once when the packaged app opens. Child setup/install processes
        # inherit elevation, avoiding a separate UAC prompt for every tool.
        command.insert(command.index("--windowed") + 1, "--uac-admin")
    _run(command)
    print(f"\nBuilt: {ROOT / 'dist' / APP_NAME}")


def clean() -> None:
    for name in ("build", "dist"):
        target = ROOT / name
        if target.exists():
            shutil.rmtree(target)
            print(f"removed {target}")
    for spec in ROOT.glob("*.spec"):
        spec.unlink()
        print(f"removed {spec}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the connection assistant.")
    parser.add_argument("target", choices=["wheel", "app", "clean"])
    args = parser.parse_args(argv)

    try:
        bootstrap_result = _bootstrap(args.target)
        if bootstrap_result is not None:
            return bootstrap_result
        if args.target != "clean":
            _install_build_dependencies()
        {"wheel": build_wheel, "app": build_app, "clean": clean}[args.target]()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command exited with status {exc.returncode}", file=sys.stderr)
        return exc.returncode
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
