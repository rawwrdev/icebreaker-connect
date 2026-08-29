"""Control a running Android emulator over adb: boot, proxy, launch, cleanup.

Every method is safe to call on any exit path. In particular :meth:`clear_proxy`
and :meth:`stop_emulator` are idempotent and swallow "no device" errors, because
the orchestrator calls them during cleanup after a cancel or a crash.

Multiple/absent devices are handled explicitly: an :class:`AdbController` is bound
to a single serial, chosen by :func:`list_devices` / :meth:`select_single_device`,
so a stray second emulator never gets proxied or launched by accident.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from connection_assistant.android.shell import CommandError, run

TINDER_PACKAGE = "com.tinder"
DEVICE_LOOPBACK = "127.0.0.1"


@dataclass
class AdbDevice:
    serial: str
    state: str  # "device", "offline", "unauthorized", ...

    @property
    def ready(self) -> bool:
        return self.state == "device"


class NoDeviceError(RuntimeError):
    """No usable adb device is attached."""


class MultipleDeviceError(RuntimeError):
    """More than one device is attached and no serial was chosen."""

    def __init__(self, serials: list[str]) -> None:
        super().__init__(
            "multiple devices attached; choose one: " + ", ".join(serials)
        )
        self.serials = serials


def list_devices(adb: str = "adb") -> list[AdbDevice]:
    """Parse ``adb devices`` into a typed list (excludes the header line)."""
    result = run([adb, "devices"], timeout=30)
    devices: list[AdbDevice] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        devices.append(AdbDevice(serial=serial.strip(), state=state.strip()))
    return devices


class AdbController:
    """adb operations scoped to one device serial."""

    def __init__(self, serial: str, *, adb: str = "adb") -> None:
        self._serial = serial
        self._adb = adb
        self._reverse_proxy_port: int | None = None

    @property
    def serial(self) -> str:
        return self._serial

    def _base(self) -> list[str]:
        return [self._adb, "-s", self._serial]

    def _shell(self, *args: str, timeout: float = 60.0) -> str:
        result = run([*self._base(), "shell", *args], timeout=timeout)
        return result.stdout

    @classmethod
    def select_single_device(cls, *, adb: str = "adb") -> AdbController:
        """Return a controller for the single ready device, or raise."""
        ready = [d for d in list_devices(adb) if d.ready]
        if not ready:
            raise NoDeviceError("no ready adb device attached")
        if len(ready) > 1:
            raise MultipleDeviceError([d.serial for d in ready])
        return cls(ready[0].serial, adb=adb)

    @classmethod
    def wait_for_single_device(
        cls,
        *,
        adb: str = "adb",
        timeout: float = 120.0,
        poll: float = 1.0,
        process_running: Callable[[], bool] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AdbController:
        """Wait for one emulator to become visible and ready in ADB.

        A cold emulator needs several seconds before it appears in ``adb devices``.
        Checking only once races normal startup and previously caused the caller to
        terminate a healthy emulator immediately after spawning it.
        """
        deadline = time.monotonic() + timeout
        progress_reported = False
        while time.monotonic() < deadline:
            try:
                ready = [device for device in list_devices(adb) if device.ready]
            except CommandError:
                ready = []  # adb itself can still be starting; retry below
            if len(ready) > 1:
                raise MultipleDeviceError([device.serial for device in ready])
            if ready:
                return cls(ready[0].serial, adb=adb)
            if process_running is not None and not process_running():
                raise NoDeviceError("emulator exited before it became available in adb")
            if on_progress is not None and not progress_reported:
                on_progress("waiting for emulator to appear in adb")
                progress_reported = True
            time.sleep(poll)
        raise NoDeviceError(f"emulator did not appear in adb within {timeout:g} seconds")

    def wait_for_boot(
        self,
        *,
        timeout: float = 300.0,
        poll: float = 2.0,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Block until ``sys.boot_completed == 1`` or raise on timeout."""
        run([*self._base(), "wait-for-device"], timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self._shell("getprop", "sys.boot_completed", timeout=15).strip()
            if out == "1":
                if on_progress:
                    on_progress("device booted")
                return
            if on_progress:
                on_progress("waiting for device to finish booting")
            time.sleep(poll)
        raise CommandError("emulator did not finish booting in time")

    def set_proxy(self, host: str, port: int) -> None:
        """Route the emulator's traffic through ``host:port``."""
        run(
            [*self._base(), "shell", "settings", "put", "global", "http_proxy", f"{host}:{port}"],
            check=True,
            timeout=30,
        )

    def set_loopback_proxy(self, port: int) -> None:
        """Route Android traffic to a host-loopback proxy through ADB.

        Binding the host proxy to all interfaces exposes it to the local network,
        while the emulator-only ``10.0.2.2`` alias does not work for every device
        or host setup. ``adb reverse`` gives both emulators and USB devices a
        private, reliable path to a proxy listening only on host loopback.
        """
        endpoint = f"tcp:{port}"
        run(
            [*self._base(), "reverse", endpoint, endpoint],
            check=True,
            timeout=30,
        )
        try:
            self.set_proxy(DEVICE_LOOPBACK, port)
        except Exception:
            try:
                run(
                    [*self._base(), "reverse", "--remove", endpoint],
                    timeout=30,
                )
            except CommandError:
                pass
            raise
        self._reverse_proxy_port = port

    def clear_proxy(self) -> None:
        """Remove the emulator proxy. Idempotent; never raises."""
        try:
            run(
                [*self._base(), "shell", "settings", "delete", "global", "http_proxy"],
                timeout=30,
            )
        except CommandError:
            pass
        reverse_port = self._reverse_proxy_port
        self._reverse_proxy_port = None
        if reverse_port is not None:
            try:
                run(
                    [
                        *self._base(),
                        "reverse",
                        "--remove",
                        f"tcp:{reverse_port}",
                    ],
                    timeout=30,
                )
            except CommandError:
                pass

    def restart_as_root(self) -> None:
        """`adb root` then wait for adbd to come back (needed for CA install)."""
        run([*self._base(), "root"], timeout=60)
        run([*self._base(), "wait-for-device"], timeout=120)

    def install_apk(self, apk_path: str, *, on_progress: Callable[[str], None] | None = None) -> None:
        """Install a user-supplied APK with ``adb install -r``."""
        if on_progress:
            on_progress("installing the selected APK")
        run([*self._base(), "install", "-r", apk_path], check=True, timeout=600)

    def is_package_installed(self, package: str = TINDER_PACKAGE) -> bool:
        out = self._shell("pm", "list", "packages", package, timeout=30)
        return f"package:{package}" in out

    def package_version(self, package: str = TINDER_PACKAGE) -> str | None:
        """Return an installed package's human-readable version name."""
        if not self.is_package_installed(package):
            return None
        out = self._shell("dumpsys", "package", package, timeout=30)
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("versionName="):
                version = stripped.partition("=")[2].strip()
                return version or None
        return None

    def launch_app(self, package: str = TINDER_PACKAGE) -> None:
        """Resolve and start the app's launcher activity, failing if it did not start."""
        resolved = self._shell(
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            package,
            timeout=30,
        )
        components = [line.strip() for line in resolved.splitlines() if "/" in line]
        if not components:
            raise CommandError(f"no launcher activity found for {package}")
        component = components[-1]
        run(
            [
                *self._base(),
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                component,
            ],
            timeout=60,
            check=True,
        )

    def force_stop_app(self, package: str = TINDER_PACKAGE) -> None:
        """Stop an existing app process so it reconnects through the fresh proxy."""
        run(
            [*self._base(), "shell", "am", "force-stop", package],
            timeout=30,
            check=True,
        )


class EmulatorProcess:
    """A rootable emulator started with ``-no-snapshot -writable-system``."""

    def __init__(self, emulator_bin: str, avd_name: str) -> None:
        self._bin = emulator_bin
        self._avd = avd_name
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        # -writable-system is required so the CA can be installed into the system
        # trust store; -no-snapshot forces a clean boot each session.
        self._proc = subprocess.Popen(
            [self._bin, "-avd", self._avd, "-no-snapshot", "-writable-system"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 20.0) -> None:
        """Terminate the emulator we started. Idempotent; never raises."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = None
