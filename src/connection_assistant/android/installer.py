"""Prepare Android components from official Google sources, with explicit consent.

This module performs the mutating half of environment setup:

  * download + unpack the official Google command-line tools,
  * accept the Android SDK licenses only after the user explicitly opts in,
  * install the emulator, platform-tools and the **rootable** system image,
  * create the capture AVD,
  * ensure a mitmproxy CA exists and install it into the emulator's *system* trust
    store (the step that lets Tinder trust the proxy without patching the APK).

Downloads come from ``dl.google.com``; nothing is bundled or redistributed.
"""

from __future__ import annotations

import io
import os
import stat
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from connection_assistant.android.controller import AdbController
from connection_assistant.android.shell import CommandError, run
from connection_assistant.android.toolchain import (
    Toolchain,
    cmdline_tools_url,
    system_image_package,
)

ProgressFn = Callable[[str], None]

MITMPROXY_CA = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


class LicenseNotAcceptedError(RuntimeError):
    """SDK setup was requested without explicit license acceptance."""


def _noop(_msg: str) -> None:
    return


def download_cmdline_tools(
    sdk_root: Path, *, on_progress: ProgressFn = _noop
) -> Path:
    """Download + unpack Google's command-line tools into ``sdk_root``.

    Lays them out as ``cmdline-tools/latest`` — the layout sdkmanager expects.
    Returns the ``.../cmdline-tools/latest/bin`` directory.
    """
    url = cmdline_tools_url()
    on_progress("downloading official Android command-line tools")
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed Google host
        payload = resp.read()
    on_progress("unpacking command-line tools")
    tools_root = sdk_root / "cmdline-tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(tools_root)
    # The zip contains a top-level "cmdline-tools" dir; rename to "latest".
    extracted = tools_root / "cmdline-tools"
    latest = tools_root / "latest"
    if extracted.is_dir():
        if latest.exists():
            _rmtree(latest)
        extracted.rename(latest)
    _make_executable(latest / "bin")
    return latest / "bin"


def _make_executable(bin_dir: Path) -> None:
    if not bin_dir.is_dir():
        return
    for entry in bin_dir.iterdir():
        if entry.is_file():
            entry.chmod(entry.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def accept_licenses(
    sdkmanager: str, sdk_root: Path, *, on_progress: ProgressFn = _noop
) -> None:
    """Accept the Android SDK licenses. Caller must have gathered explicit consent.

    We feed a stream of 'y' answers to ``sdkmanager --licenses``; the UI gates this
    behind a checkbox so the user affirmatively accepts Google's SDK terms.
    """
    on_progress("accepting Android SDK licenses")
    result = run(
        [sdkmanager, f"--sdk_root={sdk_root}", "--licenses"],
        input_text="y\n" * 20,
        timeout=300,
    )
    if not result.ok:
        raise CommandError("failed to accept Android SDK licenses")


def install_packages(
    sdkmanager: str,
    sdk_root: Path,
    *,
    licenses_accepted: bool,
    on_progress: ProgressFn = _noop,
) -> None:
    """Install emulator, platform-tools and the rootable system image.

    ``licenses_accepted`` must be True (explicit user consent) or this refuses to run.
    """
    if not licenses_accepted:
        raise LicenseNotAcceptedError(
            "Android SDK licenses must be explicitly accepted before install"
        )
    packages = [
        "emulator",
        "platform-tools",
        system_image_package(),
    ]
    on_progress("installing emulator, platform-tools and system image")
    run(
        [sdkmanager, f"--sdk_root={sdk_root}", *packages],
        input_text="y\n" * 20,
        timeout=1800,
        check=True,
    )


def create_avd(
    avdmanager: str,
    *,
    name: str = "icebreaker_capture",
    sdk_root: Path | None = None,
    on_progress: ProgressFn = _noop,
) -> None:
    """Create (or replace) the capture AVD from the rootable system image."""
    on_progress(f"creating emulator '{name}'")
    args = [
        avdmanager,
        "create",
        "avd",
        "-n",
        name,
        "-k",
        system_image_package(),
        "--force",
    ]
    env_note = ""
    if sdk_root is not None:
        os.environ.setdefault("ANDROID_SDK_ROOT", str(sdk_root))
        env_note = str(sdk_root)
    # avdmanager prompts "create a custom hardware profile?"; answer no.
    result = run(args, input_text="no\n", timeout=300)
    if not result.ok:
        raise CommandError(
            f"failed to create AVD '{name}'"
            + (f" under {env_note}" if env_note else "")
        )


def list_avds(emulator_bin: str) -> list[str]:
    result = run([emulator_bin, "-list-avds"], timeout=60)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def ensure_mitmproxy_ca(mitmdump: str, *, on_progress: ProgressFn = _noop) -> Path:
    """Return the mitmproxy CA path, generating it once if it does not exist.

    mitmproxy writes ``~/.mitmproxy/mitmproxy-ca-cert.pem`` the first time it runs;
    we start and immediately stop it to force generation without capturing anything.
    """
    if MITMPROXY_CA.exists():
        return MITMPROXY_CA
    on_progress("generating mitmproxy certificate authority")
    import subprocess
    import time

    proc = subprocess.Popen(
        [mitmdump, "-q", "--listen-port", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if MITMPROXY_CA.exists():
                break
            time.sleep(0.5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not MITMPROXY_CA.exists():
        raise CommandError("could not generate the mitmproxy CA certificate")
    return MITMPROXY_CA


def install_system_ca(
    controller: AdbController,
    *,
    openssl: str,
    ca_cert: Path = MITMPROXY_CA,
    on_progress: ProgressFn = _noop,
) -> str:
    """Install the mitmproxy CA into the emulator's system trust store.

    Mirrors the documented rootable-emulator procedure: derive the Android cert
    filename from ``subject_hash_old``, get root, remount the cert dir as a writable
    tmpfs (preserving the stock certs), then push the CA. Survives until the
    emulator is killed; must be redone every boot. Returns the installed hash name.
    """
    if not ca_cert.exists():
        raise CommandError("mitmproxy CA certificate not found; generate it first")
    on_progress("computing certificate hash")
    hash_result = run(
        [openssl, "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(ca_cert)],
        timeout=30,
    )
    cert_hash = (hash_result.stdout.splitlines() or [""])[0].strip()
    if not cert_hash:
        raise CommandError("could not compute the certificate hash (openssl)")

    on_progress("installing the certificate as a system trust anchor")
    controller.restart_as_root()
    base = [*_adb_base(controller)]
    target = f"/system/etc/security/cacerts/{cert_hash}.0"
    # Preserve stock certs, mount a writable tmpfs over the dir, restore them,
    # then add the mitmproxy CA. Errors on the cp steps are tolerated (some images
    # have no user certs), but the final push must succeed.
    run([*base, "shell", "cp /system/etc/security/cacerts/* /data/local/tmp/"], timeout=60)
    run([*base, "shell", "mount -t tmpfs tmpfs /system/etc/security/cacerts"], timeout=60, check=True)
    run([*base, "shell", "cp /data/local/tmp/*.0 /system/etc/security/cacerts/"], timeout=60)
    run([*base, "push", str(ca_cert), target], timeout=120, check=True)
    run([*base, "shell", f"chmod 644 {target}"], timeout=30, check=True)
    return f"{cert_hash}.0"


def _adb_base(controller: AdbController) -> list[str]:
    # AdbController keeps its serial/adb private; rebuild the base command here so
    # installer can issue the raw push/mount steps the CA install needs.
    return controller._base()  # noqa: SLF001 - single trusted collaborator


def toolchain_paths(tc: Toolchain) -> dict[str, str]:
    """Small helper for callers/tests: present resolved tool paths (no secrets)."""
    return {
        "adb": tc.adb or "",
        "emulator": tc.emulator or "",
        "sdkmanager": tc.sdkmanager or "",
        "avdmanager": tc.avdmanager or "",
        "mitmdump": tc.mitmdump or "",
        "openssl": tc.openssl or "",
    }
