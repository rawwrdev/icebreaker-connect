"""Detect and locate the Android/Java/mitmproxy toolchain across platforms.

Pure discovery + policy: where the SDK lives, which binaries exist, whether Java is
present and new enough, and the *official Google* URLs to fetch missing pieces from.
Nothing here mutates the system — installation lives in :mod:`installer`.

Platform coverage: Linux, Windows, Intel macOS, Apple Silicon macOS. The emulator
system image is chosen to be **rootable** (``google_apis``, never a Play-Store image)
so the mitmproxy CA can be installed into the system trust store.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# A rootable Google APIs image (NOT ...;google_apis_playstore, which blocks
# `adb root`). x86_64 on Intel; arm64-v8a on Apple Silicon.
SYSTEM_IMAGE_API = "30"


def system_image_package(arch: str | None = None) -> str:
    """The sdkmanager package id for the rootable system image on this arch."""
    arch = (arch or platform.machine()).lower()
    abi = "arm64-v8a" if arch in {"arm64", "aarch64"} else "x86_64"
    return f"system-images;android-{SYSTEM_IMAGE_API};google_apis;{abi}"


# Official Google command-line tools downloads (developer.android.com/studio).
# Pinned build number; users may override via the setup UI if Google rotates it.
_CMDLINE_TOOLS_BUILD = "11076708"
CMDLINE_TOOLS_URLS = {
    "linux": f"https://dl.google.com/android/repository/commandlinetools-linux-{_CMDLINE_TOOLS_BUILD}_latest.zip",
    "win": f"https://dl.google.com/android/repository/commandlinetools-win-{_CMDLINE_TOOLS_BUILD}_latest.zip",
    "mac": f"https://dl.google.com/android/repository/commandlinetools-mac-{_CMDLINE_TOOLS_BUILD}_latest.zip",
}


def cmdline_tools_url() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return CMDLINE_TOOLS_URLS["win"]
    if system == "darwin":
        return CMDLINE_TOOLS_URLS["mac"]
    return CMDLINE_TOOLS_URLS["linux"]


def _exe(name: str) -> str:
    """Append .exe/.bat as Windows needs; sdkmanager/avdmanager are .bat there."""
    if os.name != "nt":
        return name
    if name in {"sdkmanager", "avdmanager"}:
        return name + ".bat"
    return name + ".exe"


@dataclass
class JavaStatus:
    present: bool
    version: int | None = None
    path: str | None = None

    @property
    def ok(self) -> bool:
        # cmdline-tools 11+ needs Java 17+.
        return self.present and (self.version is None or self.version >= 17)


def detect_java() -> JavaStatus:
    """Locate a Java runtime and read its major version."""
    java = shutil.which("java")
    if not java:
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidate = Path(java_home) / "bin" / _exe("java")
            if candidate.exists():
                java = str(candidate)
    if not java:
        return JavaStatus(present=False)
    version = _java_major_version(java)
    return JavaStatus(present=True, version=version, path=java)


def _java_major_version(java: str) -> int | None:
    try:
        proc = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    # Formats: '"1.8.0_301"' (=> 8) or '"17.0.9"' (=> 17).
    for token in text.replace('"', " ").split():
        parts = token.split(".")
        if parts and parts[0].isdigit():
            major = int(parts[0])
            if major == 1 and len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
            if 6 <= major <= 40:
                return major
    return None


def default_sdk_candidates() -> list[Path]:
    """Standard SDK locations per platform, most-specific first."""
    env = [os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")]
    candidates = [Path(p) for p in env if p]
    home = Path.home()
    system = platform.system().lower()
    if system == "darwin":
        candidates.append(home / "Library" / "Android" / "sdk")
    elif system.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Android" / "Sdk")
    else:
        candidates.append(home / "Android" / "Sdk")
    # Cross-platform fallbacks used by the reference guide / CI.
    candidates.append(home / "android-sdk")
    candidates.append(home / "Android" / "sdk")
    # De-duplicate, preserve order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def locate_sdk_root() -> Path | None:
    """Return the first SDK root that actually looks like an SDK, else None."""
    for candidate in default_sdk_candidates():
        if candidate.is_dir() and (
            (candidate / "cmdline-tools").is_dir()
            or (candidate / "platform-tools").is_dir()
            or (candidate / "emulator").is_dir()
        ):
            return candidate
    return None


def _find_in_sdk(sdk_root: Path | None, *rel_dirs: str, binary: str) -> str | None:
    if sdk_root is None:
        return None
    for rel in rel_dirs:
        candidate = sdk_root.joinpath(*rel.split("/"), _exe(binary))
        if candidate.exists():
            return str(candidate)
    return None


def _windows_openssl_path() -> str | None:
    """Find Shining Light OpenSSL, whose installer may not add itself to PATH."""
    if not platform.system().lower().startswith("win"):
        return None
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles(x86)"),
    ]
    install_dirs = ("OpenSSL-Win64", "OpenSSL-Win64-ARM", "OpenSSL-Win32")
    seen: set[Path] = set()
    for root in roots:
        if not root:
            continue
        for install_dir in install_dirs:
            candidate = Path(root) / install_dir / "bin" / "openssl.exe"
            if candidate not in seen:
                seen.add(candidate)
                if candidate.is_file():
                    return str(candidate)
    return None


@dataclass
class Toolchain:
    """A resolved view of every tool the onboarding flow needs."""

    sdk_root: Path | None
    java: JavaStatus
    adb: str | None
    emulator: str | None
    sdkmanager: str | None
    avdmanager: str | None
    mitmdump: str | None
    openssl: str | None
    missing: list[str] = field(default_factory=list)

    @property
    def sdk_ready(self) -> bool:
        return all([self.adb, self.emulator, self.sdkmanager, self.avdmanager])

    @property
    def capture_ready(self) -> bool:
        return bool(self.mitmdump)

    def summary(self) -> dict[str, bool]:
        """Value-free readiness map for the environment-check UI page."""
        return {
            "java": self.java.ok,
            "android_sdk": self.sdk_ready,
            "adb": bool(self.adb),
            "emulator": bool(self.emulator),
            "sdkmanager": bool(self.sdkmanager),
            "avdmanager": bool(self.avdmanager),
            "mitmproxy": self.capture_ready,
            "openssl": bool(self.openssl),
        }


def detect_toolchain() -> Toolchain:
    """Discover the full toolchain without modifying anything."""
    sdk = locate_sdk_root()
    adb = shutil.which("adb") or _find_in_sdk(sdk, "platform-tools", binary="adb")
    emulator = shutil.which("emulator") or _find_in_sdk(sdk, "emulator", binary="emulator")
    sdkmanager = shutil.which("sdkmanager") or _find_in_sdk(
        sdk,
        "cmdline-tools/latest/bin",
        "cmdline-tools/bin",
        "tools/bin",
        binary="sdkmanager",
    )
    avdmanager = shutil.which("avdmanager") or _find_in_sdk(
        sdk,
        "cmdline-tools/latest/bin",
        "cmdline-tools/bin",
        "tools/bin",
        binary="avdmanager",
    )
    mitmdump = shutil.which("mitmdump")
    openssl = shutil.which("openssl") or _windows_openssl_path()
    java = detect_java()

    missing: list[str] = []
    if not java.ok:
        missing.append("java17")
    if not sdkmanager:
        missing.append("android-cmdline-tools")
    if not emulator:
        missing.append("emulator")
    if not adb:
        missing.append("platform-tools")
    if not mitmdump:
        missing.append("mitmproxy")
    if not openssl:
        missing.append("openssl")

    return Toolchain(
        sdk_root=sdk,
        java=java,
        adb=adb,
        emulator=emulator,
        sdkmanager=sdkmanager,
        avdmanager=avdmanager,
        mitmdump=mitmdump,
        openssl=openssl,
        missing=missing,
    )
