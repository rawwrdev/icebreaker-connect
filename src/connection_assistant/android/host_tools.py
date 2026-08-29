"""GUI-driven installation of host prerequisites (mitmproxy, adb, Java, openssl).

The Android SDK/emulator are fetched by :mod:`installer`; the *host* tools that the
app shells out to are installed here so the whole setup can happen inside the GUI
without opening a terminal. Each platform uses its native package manager and its
native graphical privilege prompt:

  * Linux   — apt/dnf/pacman via ``pkexec`` (polkit shows a GUI auth dialog).
  * macOS   — Homebrew (no sudo; casks manage their own privilege).
  * Windows — winget (UAC shows a GUI elevation prompt).

Output is streamed line-by-line and sanitized before it reaches the UI, so a
package manager can never leak a path or address into the log.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from connection_assistant.security.files import sanitize_text

ProgressFn = Callable[[str], None]

# Host tools this module can install (SDK/emulator are handled elsewhere).
# Keyed by the neutral name used across the app; values are per-manager packages.
_PACKAGES: dict[str, dict[str, list[str]]] = {
    "apt": {
        "mitmproxy": ["mitmproxy"],
        "java17": ["default-jdk"],
        "platform-tools": ["adb"],
        "openssl": ["openssl"],
        "kvm": ["qemu-kvm"],
    },
    "dnf": {
        "mitmproxy": ["mitmproxy"],
        "java17": ["java-17-openjdk"],
        "platform-tools": ["android-tools"],
        "openssl": ["openssl"],
        "kvm": ["qemu-kvm"],
    },
    "pacman": {
        "mitmproxy": ["mitmproxy"],
        "java17": ["jdk17-openjdk"],
        "platform-tools": ["android-tools"],
        "openssl": ["openssl"],
        "kvm": ["qemu"],
    },
    "brew": {
        "mitmproxy": ["mitmproxy"],
        "platform-tools": ["android-platform-tools"],
        "openssl": ["openssl"],
        # java17 is a cask (temurin), handled specially below.
    },
    "winget": {
        "mitmproxy": ["mitmproxy.mitmproxy"],
        "java17": ["EclipseAdoptium.Temurin.17.JDK"],
        "openssl": ["ShiningLight.OpenSSL.Light"],
    },
}

# The subset of toolchain "missing" names this module can resolve on each OS.
_INSTALLABLE = {
    "linux": {"mitmproxy", "java17", "platform-tools", "openssl", "kvm"},
    "darwin": {"mitmproxy", "java17", "platform-tools", "openssl"},
    "windows": {"mitmproxy", "java17", "openssl"},
}


@dataclass
class InstallPlan:
    """A concrete, reviewable plan for installing missing host tools."""

    manager: str
    tools: list[str]
    commands: list[list[str]] = field(default_factory=list)
    privileged: bool = False
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.commands


class HostToolError(RuntimeError):
    """Host-tool installation could not proceed. Message is safe to display."""


def _os_key() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


def detect_manager() -> str | None:
    """Return the available package manager for this OS, or None."""
    os_key = _os_key()
    if os_key == "windows":
        return "winget" if shutil.which("winget") else None
    if os_key == "darwin":
        return "brew" if shutil.which("brew") else None
    for mgr in ("apt", "dnf", "pacman"):
        binary = "apt-get" if mgr == "apt" else mgr
        if shutil.which(binary):
            return mgr
    return None


def _graphical_privilege_prefix() -> list[str] | None:
    """A GUI privilege-escalation prefix for Linux, or None if unavailable.

    ``pkexec`` shows a polkit dialog, so the whole install stays graphical. If it is
    absent we return None and the caller reports that admin rights are required.
    """
    if shutil.which("pkexec"):
        return ["pkexec"]
    return None


def plan_install(missing: list[str]) -> InstallPlan:
    """Build an :class:`InstallPlan` for the installable subset of ``missing``.

    Only tools this module knows how to install on the current OS are included;
    SDK components (cmdline-tools, emulator) are intentionally excluded — the app's
    Android setup installs those from Google.
    """
    os_key = _os_key()
    manager = detect_manager()
    installable = _INSTALLABLE[os_key]
    tools = [t for t in missing if t in installable]
    if not tools:
        return InstallPlan(manager or "", tools=[], note="no installable host tools missing")
    if manager is None:
        hint = {
            "windows": "winget",
            "darwin": "Homebrew (https://brew.sh)",
            "linux": "apt/dnf/pacman",
        }[os_key]
        return InstallPlan("", tools=tools, note=f"no supported package manager found (need {hint})")

    if manager == "brew":
        return _plan_brew(tools)
    if manager == "winget":
        return _plan_winget(tools)
    return _plan_linux(manager, tools)


def _packages_for(manager: str, tools: list[str]) -> list[str]:
    table = _PACKAGES[manager]
    packages: list[str] = []
    for tool in tools:
        packages.extend(table.get(tool, []))
    return packages


def _plan_linux(manager: str, tools: list[str]) -> InstallPlan:
    prefix = _graphical_privilege_prefix()
    if prefix is None:
        return InstallPlan(
            manager,
            tools=tools,
            note="install pkexec (polkit) for a graphical prompt, or run scripts/setup_linux.sh",
        )
    packages = _packages_for(manager, tools)
    if manager == "apt":
        inner = ["sh", "-c", "apt-get update && apt-get install -y " + " ".join(packages)]
    elif manager == "dnf":
        inner = ["dnf", "install", "-y", *packages]
    else:  # pacman
        inner = ["pacman", "-S", "--noconfirm", *packages]
    return InstallPlan(manager, tools=tools, commands=[[*prefix, *inner]], privileged=True)


def _plan_brew(tools: list[str]) -> InstallPlan:
    commands: list[list[str]] = []
    formulae = _packages_for("brew", [t for t in tools if t != "java17"])
    if formulae:
        commands.append(["brew", "install", *formulae])
    if "java17" in tools:
        # Temurin is a cask; Homebrew manages any privilege prompt itself.
        commands.append(["brew", "install", "--cask", "temurin"])
    return InstallPlan("brew", tools=tools, commands=commands, privileged=False,
                       note="Homebrew installs without sudo")


def _plan_winget(tools: list[str]) -> InstallPlan:
    commands: list[list[str]] = []
    for pkg in _packages_for("winget", tools):
        commands.append([
            "winget", "install", "--exact", "--id", pkg,
            "--accept-source-agreements", "--accept-package-agreements",
        ])
    return InstallPlan("winget", tools=tools, commands=commands, privileged=True,
                       note="winget shows a UAC prompt for elevation")


def run_install(plan: InstallPlan, *, on_progress: ProgressFn | None = None) -> None:
    """Execute an install plan, streaming sanitized output through ``on_progress``.

    Raises :class:`HostToolError` (sanitized) if the plan cannot run or a command
    fails. Callers run this on a background thread.
    """
    if plan.is_empty:
        raise HostToolError(plan.note or "nothing to install")

    def emit(message: str) -> None:
        if on_progress is not None:
            on_progress(sanitize_text(message))

    for command in plan.commands:
        emit(f"running {command[0]} to install host tools")
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise HostToolError(f"could not start {command[0]}") from exc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                emit(line)
        returncode = proc.wait()
        if returncode != 0:
            raise HostToolError(
                f"{command[0]} failed while installing host tools (exit {returncode})"
            )
    emit("host tools installation finished")
