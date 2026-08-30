"""The onboarding orchestrator — GUI-agnostic coordination of every step.

It owns the lifecycle and, critically, the *cleanup contract*: no matter how a run
ends (success, cancel, exception), :meth:`cleanup` clears the Android proxy, stops
the capture process, and stops any emulator this app started, then wipes the
in-memory bundle. Callers should treat the orchestrator as a context manager or
always call :meth:`cleanup` in a ``finally``.

All progress is reported through :class:`~connection_assistant.models.ProgressEvent`
callbacks that carry only operational text — never token values.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from connection_assistant.android import installer
from connection_assistant.android.controller import (
    AdbController,
    EmulatorProcess,
)
from connection_assistant.android.installer import ensure_mitmproxy_ca, install_system_ca
from connection_assistant.android.toolchain import Toolchain, detect_toolchain
from connection_assistant.capture.process import CaptureProcess
from connection_assistant.models import (
    DeliveryResult,
    ProgressEvent,
    SessionBundle,
    Stage,
)
from connection_assistant.pairing.client import PairingClient, PairingClientConfig
from connection_assistant.security.files import (
    sanitize_text,
    validate_service_url,
    write_owner_only_json,
)

ProgressSink = Callable[[ProgressEvent], None]
DEFAULT_SERVICE_URL = "https://bot.rawwr.dev"


@dataclass
class AssistantConfig:
    """User-tunable settings. ``service_url`` empty => save-locally-only mode."""

    service_url: str = DEFAULT_SERVICE_URL
    avd_name: str = "tinder_cap"
    mitm_port: int = 8080
    sdk_root: Path | None = None
    licenses_accepted: bool = False

    def pairing_enabled(self) -> bool:
        return bool(self.service_url.strip())


@dataclass
class _RunState:
    toolchain: Toolchain | None = None
    controller: AdbController | None = None
    emulator: EmulatorProcess | None = None
    capture: CaptureProcess | None = None
    proxy_set: bool = False
    started_emulator: bool = False
    downloaded_package: Path | None = None
    bundle: SessionBundle = field(default_factory=SessionBundle)


class Orchestrator:
    def __init__(self, config: AssistantConfig, *, on_progress: ProgressSink | None = None) -> None:
        self._config = config
        self._on_progress = on_progress or (lambda _e: None)
        self._state = _RunState()
        self._cancelled = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------- #
    def __enter__(self) -> Orchestrator:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    @property
    def config(self) -> AssistantConfig:
        return self._config

    @property
    def bundle(self) -> SessionBundle:
        return self._state.bundle

    @property
    def capture_active(self) -> bool:
        return self._state.capture is not None

    @property
    def emulator_started(self) -> bool:
        """Whether this run owns an emulator process it is safe to stop."""
        return self._state.started_emulator and self._state.emulator is not None

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _emit(self, stage: Stage, message: str, *, fraction: float | None = None, level: str = "info") -> None:
        self._on_progress(ProgressEvent(stage, message, fraction=fraction, level=level))

    # -- environment ------------------------------------------------------ #
    def detect_environment(self) -> Toolchain:
        self._emit(Stage.ENVIRONMENT, "checking for Java, Android SDK and mitmproxy")
        tc = detect_toolchain()
        self._state.toolchain = tc
        return tc

    def plan_host_tools(self):
        """Return an install plan for missing host tools (mitmproxy/adb/Java/openssl)."""
        from connection_assistant.android.host_tools import plan_install

        tc = self._state.toolchain or self.detect_environment()
        return plan_install(tc.missing)

    def install_host_tools(self) -> Toolchain:
        """Install missing host tools via the platform package manager (GUI prompt)."""
        from connection_assistant.android.host_tools import run_install

        plan = self.plan_host_tools()
        if plan.is_empty:
            self._emit(Stage.ENVIRONMENT, plan.note or "no host tools to install")
            return self._require_toolchain()
        self._emit(Stage.ENVIRONMENT, f"installing host tools via {plan.manager}")
        run_install(plan, on_progress=lambda m: self._emit(Stage.ENVIRONMENT, m))
        tc = detect_toolchain()
        self._state.toolchain = tc
        unresolved = [tool for tool in plan.tools if tool in tc.missing]
        if unresolved:
            names = ", ".join(unresolved)
            raise RuntimeError(
                f"Installation finished, but the app still cannot find: {names}. "
                "Close Icebreaker Connect, open it again, and retry."
            )
        return tc

    def setup_environment(self) -> Toolchain:
        """Install any missing Android components from official Google sources.

        Requires explicit license acceptance in the config; refuses otherwise.
        """
        tc = self._state.toolchain or self.detect_environment()
        if not tc.java.ok:
            raise RuntimeError(
                "Java 17 is required before Android setup. Use 'Install missing tools' "
                "first and wait for it to finish."
            )
        sdk_root = self._config.sdk_root or (tc.sdk_root or Path.home() / "android-sdk")
        sdk_root.mkdir(parents=True, exist_ok=True)

        def progress(msg: str) -> None:
            self._emit(Stage.ENVIRONMENT, msg)

        if not tc.sdkmanager:
            installer.download_cmdline_tools(sdk_root, on_progress=progress)
        # Re-detect now that cmdline-tools may exist.
        self._config.sdk_root = sdk_root
        tc = detect_toolchain()
        self._state.toolchain = tc
        if not tc.sdkmanager:
            raise RuntimeError("Android command-line tools are still unavailable after setup")
        if not self._config.licenses_accepted:
            raise installer.LicenseNotAcceptedError(
                "Android SDK licenses must be accepted to install components"
            )
        installer.accept_licenses(tc.sdkmanager, sdk_root, on_progress=progress)
        installer.install_packages(
            tc.sdkmanager,
            sdk_root,
            licenses_accepted=self._config.licenses_accepted,
            on_progress=progress,
        )
        tc = detect_toolchain()
        self._state.toolchain = tc
        if tc.avdmanager and self._config.avd_name not in installer.list_avds(tc.emulator or "emulator"):
            installer.create_avd(
                tc.avdmanager, name=self._config.avd_name, sdk_root=sdk_root, on_progress=progress
            )
        return tc

    def download_tinder_package(self) -> str:
        """Download APKPure's latest Tinder XAPK for this run only."""
        from connection_assistant.android.packages import download_latest_tinder

        self._delete_downloaded_package()
        path = download_latest_tinder(
            on_progress=lambda message: self._emit(Stage.APK, message)
        )
        self._state.downloaded_package = path
        return str(path)

    def list_emulators(self) -> list[str]:
        """Return the locally configured Android Virtual Device names."""
        tc = self._state.toolchain or self.detect_environment()
        if not tc.emulator:
            return []
        return installer.list_avds(tc.emulator)

    def create_emulator(self, name: str) -> str:
        """Create a new rootable capture AVD after explicit SDK-license consent."""
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError(
                "emulator name must be 1-64 letters, numbers, dots, underscores or hyphens"
            )
        if name in self.list_emulators():
            raise ValueError(f"an emulator named '{name}' already exists")
        if not self._config.licenses_accepted:
            raise installer.LicenseNotAcceptedError(
                "accept the Android SDK license terms before creating an emulator"
            )
        self._config.avd_name = name
        self.setup_environment()
        if name not in self.list_emulators():
            raise RuntimeError(f"emulator '{name}' was not created")
        return name

    # -- emulator + capture prep ----------------------------------------- #
    def start_emulator(self) -> AdbController:
        tc = self._require_toolchain()
        adb = tc.adb or "adb"
        # Reuse an already-attached device; otherwise boot our AVD.
        from connection_assistant.android.controller import list_devices

        try:
            ready = [d for d in list_devices(adb) if d.ready]
        except Exception:
            ready = []
        emu: EmulatorProcess | None = None
        if not ready:
            if not tc.emulator:
                raise RuntimeError("emulator binary not found; run environment setup first")
            self._emit(Stage.EMULATOR, f"starting emulator '{self._config.avd_name}'")
            emu = EmulatorProcess(tc.emulator, self._config.avd_name)
            emu.start()
            self._state.emulator = emu
            self._state.started_emulator = True
        controller = AdbController.wait_for_single_device(
            adb=adb,
            process_running=(lambda: emu.running) if emu is not None else None,
            on_progress=lambda message: self._emit(Stage.EMULATOR, message),
        )
        self._state.controller = controller
        controller.wait_for_boot(on_progress=lambda m: self._emit(Stage.EMULATOR, m))
        # A previously interrupted run may have persisted a dead global proxy in
        # this AVD. Clear it before checks/preparation so Android has normal
        # connectivity until our verified capture listener is ready.
        self._emit(Stage.EMULATOR, "clearing any stale emulator proxy")
        controller.clear_proxy()
        self._state.proxy_set = False
        return controller

    def prepare_capture(self, apk_path: str | None = None, *, launch_app: bool = True) -> None:
        """Install the CA/APK and optionally configure the proxy and launch Tinder."""
        tc = self._require_toolchain()
        controller = self._require_controller()
        if not tc.mitmdump:
            raise RuntimeError("mitmproxy (mitmdump) is required for capture")
        if not tc.openssl:
            raise RuntimeError("openssl is required to install the capture certificate")

        ensure_mitmproxy_ca(tc.mitmdump, on_progress=lambda m: self._emit(Stage.EMULATOR, m))
        install_system_ca(
            controller,
            openssl=tc.openssl,
            on_progress=lambda m: self._emit(Stage.EMULATOR, m),
        )
        if apk_path:
            try:
                controller.install_package(
                    apk_path, on_progress=lambda message: self._emit(Stage.APK, message)
                )
            finally:
                if self._state.downloaded_package == Path(apk_path):
                    self._delete_downloaded_package()

        if not controller.is_package_installed():
            raise RuntimeError("Tinder is not installed; choose a Tinder APK first")

        if launch_app:
            self._configure_proxy_and_launch()

    def tinder_installed(self) -> bool:
        """Return whether Tinder is already present on the selected emulator."""
        return self._require_controller().is_package_installed()

    def tinder_status(self) -> tuple[bool, str | None]:
        """Return installed state and version for the selected running emulator."""
        controller = self._require_controller()
        installed = controller.is_package_installed()
        return installed, controller.package_version() if installed else None

    def _configure_proxy_and_launch(self) -> None:
        controller = self._require_controller()

        # Keep the capture listener private on host loopback and expose it only to
        # this ADB device. This works for both emulators and attached USB devices.
        self._emit(Stage.EMULATOR, "connecting the emulator to the local capture port")
        controller.set_loopback_proxy(self._config.mitm_port)
        self._state.proxy_set = True

        # A reused emulator can already have Tinder sockets/processes alive from
        # before the CA and proxy were installed. Restart only Tinder (not its data)
        # so every connection is created against the current trust/proxy settings.
        controller.force_stop_app()
        self._emit(Stage.EMULATOR, "launching Tinder — log in on the emulator")
        controller.launch_app()

    def start_emulator_and_capture(self, apk_path: str | None = None) -> None:
        """Prepare the emulator, start capture, then launch Tinder.

        Starting mitmdump before proxying and launching the app avoids losing the
        first login requests. Any partial startup is cleaned up before the error
        is returned to the GUI.
        """
        try:
            self.start_emulator()
            self.prepare_capture(apk_path, launch_app=False)
            self.start_capture()
            self._configure_proxy_and_launch()
        except Exception:
            self.stop_emulator()
            raise

    # -- capture ---------------------------------------------------------- #
    def start_capture(self) -> None:
        """Launch mitmdump and pump its result channel into the in-memory bundle."""
        if self._state.capture is not None:
            raise RuntimeError("capture is already running")
        tc = self._require_toolchain()
        capture = CaptureProcess(
            mitmdump_path=tc.mitmdump or "mitmdump",
            listen_port=self._config.mitm_port,
        )
        capture.start()
        self._state.capture = capture
        self._emit(Stage.CAPTURE, "capture is live — log in and open a conversation")

        def pump() -> None:
            try:
                for msg in capture.messages():
                    self._handle_capture_message(msg)
            finally:
                with self._lock:
                    unexpected = self._state.capture is capture
                    if unexpected:
                        self._state.capture = None
                if unexpected:
                    # Never leave Android pointed at a dead proxy. Direct internet
                    # is preferable to silently stranding Tinder and Chrome.
                    self._clear_proxy()
                    detail = capture.stderr_tail()
                    message = "capture proxy stopped unexpectedly; emulator proxy was removed"
                    if detail:
                        message += f": {detail}"
                    self._emit(Stage.CAPTURE, message, level="error")

        thread = threading.Thread(target=pump, name="capture-pump", daemon=True)
        thread.start()
        self._capture_thread = thread

    def _handle_capture_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "progress":
            self._emit(
                Stage.CAPTURE,
                sanitize_text(str(msg.get("message", ""))),
                level=str(msg.get("level", "info")),
            )
        elif kind == "bundle":
            bundle_json = msg.get("bundle") or {}
            with self._lock:
                merged = SessionBundle.from_bundle_json(bundle_json)
                merged.captcha_challenge = bool(msg.get("captcha_challenge", False))
                self._state.bundle = merged
            presence = ", ".join(k for k, v in merged.field_presence().items() if v) or "none"
            self._emit(Stage.CAPTURE, f"capture finished — fields present: {presence}")

    def stop_capture(self) -> SessionBundle:
        """Stop mitmdump so the addon flushes the bundle, then return it."""
        capture = self._state.capture
        if capture is not None:
            self._state.capture = None
            capture.stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=10)
            self._capture_thread = None
        # Proxy is no longer needed once capture stops.
        self._clear_proxy()
        return self._state.bundle

    def stop_emulator(self) -> SessionBundle:
        """Stop capture and the emulator owned by this run, preserving the bundle."""
        error: Exception | None = None
        try:
            self.stop_capture()
        except Exception as exc:
            error = exc
            try:
                self._clear_proxy()
            except Exception:
                pass

        emulator = self._state.emulator
        if emulator is not None and self._state.started_emulator:
            self._emit(Stage.EMULATOR, "stopping emulator")
            try:
                emulator.stop()
            except Exception as exc:
                error = error or exc
        self._state.emulator = None
        self._state.started_emulator = False
        self._state.controller = None
        if error is not None:
            raise error
        return self._state.bundle

    # -- delivery --------------------------------------------------------- #
    def send_securely(
        self,
        *,
        consent_ack: bool,
        on_pairing: Callable[[object], None] | None = None,
    ) -> DeliveryResult:
        """Pair with Icebreaker and upload the bundle over HTTPS."""
        bundle = self._state.bundle
        if not bundle.has_usable_session():
            return DeliveryResult(False, "paired", "no usable session was captured")
        if not self._config.pairing_enabled():
            return DeliveryResult(False, "paired", "no Icebreaker service URL is configured")

        config = PairingClientConfig(base_url=validate_service_url(self._config.service_url))
        with PairingClient(config) as client:
            self._emit(Stage.DELIVER, "requesting a one-time pairing from Icebreaker")
            request = client.create_pairing(consent_ack=consent_ack)
            if on_pairing is not None:
                on_pairing(request)  # UI shows verification_uri / QR (never the token)
            self._emit(Stage.DELIVER, "waiting for approval in the Telegram Mini App")
            last_state = None

            def report_state(state) -> None:
                nonlocal last_state
                if state != last_state:
                    self._emit(Stage.DELIVER, f"pairing state: {state.value}")
                    last_state = state

            client.wait_for_approval(
                request.pairing_id,
                should_cancel=self.is_cancelled,
                on_poll=report_state,
            )
            self._emit(Stage.DELIVER, "uploading the session bundle over HTTPS")
            client.upload_session(request.pairing_id, request.upload_token, bundle.to_bundle_json())
        return DeliveryResult(True, "paired", "session delivered to Icebreaker")

    def save_locally(self, path: str | Path) -> DeliveryResult:
        """Explicit fallback: write the bundle as owner-only JSON."""
        bundle = self._state.bundle
        if not bundle.has_usable_session():
            return DeliveryResult(False, "saved", "no usable session was captured")
        written = write_owner_only_json(path, bundle.to_bundle_json())
        self._emit(Stage.DELIVER, "saved session bundle to disk (owner-only)")
        return DeliveryResult(True, "saved", "session saved locally", saved_path=str(written))

    # -- cleanup ---------------------------------------------------------- #
    def _clear_proxy(self) -> None:
        controller = self._state.controller
        if controller is not None and self._state.proxy_set:
            controller.clear_proxy()
            self._state.proxy_set = False

    def cleanup(self) -> None:
        """Guaranteed teardown. Idempotent; safe on every exit path."""
        # 1) Stop capture first so mitmdump releases the port.
        capture = self._state.capture
        if capture is not None:
            self._state.capture = None
            try:
                capture.stop()
            except Exception:
                pass
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5)
            self._capture_thread = None
        # 2) Always clear the emulator proxy.
        try:
            self._clear_proxy()
        except Exception:
            pass
        # 3) Stop the emulator only if we started it.
        emu = self._state.emulator
        if emu is not None and self._state.started_emulator:
            try:
                emu.stop()
            except Exception:
                pass
        self._state.emulator = None
        self._state.started_emulator = False
        self._state.controller = None
        # 4) Remove any temporary third-party app download.
        self._delete_downloaded_package()
        # 5) Wipe the in-memory bundle (drop secrets).
        self.wipe_bundle()

    def _delete_downloaded_package(self) -> None:
        path = self._state.downloaded_package
        self._state.downloaded_package = None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def wipe_bundle(self) -> None:
        """Drop captured secrets from memory (Cancel-and-erase / post-delivery)."""
        with self._lock:
            self._state.bundle = SessionBundle()

    # -- helpers ---------------------------------------------------------- #
    def _require_toolchain(self) -> Toolchain:
        if self._state.toolchain is None:
            return self.detect_environment()
        return self._state.toolchain

    def _require_controller(self) -> AdbController:
        if self._state.controller is None:
            raise RuntimeError("emulator is not ready; start it first")
        return self._state.controller
