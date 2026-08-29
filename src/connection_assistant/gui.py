"""PySide6 guided-flow GUI — the primary onboarding experience.

Five focused pages mirror the user-visible onboarding flow. All slow work
(SDK setup, emulator boot, capture, pairing) runs on a background :class:`_Worker`
thread so the UI never freezes; the worker communicates only through Qt signals
carrying sanitized, value-free text.

The window guarantees the orchestrator's cleanup contract: closing the window, or
pressing Cancel-and-erase, always tears down the proxy/capture/emulator and wipes
the in-memory bundle.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import qrcode
from PySide6.QtCore import QObject, QSignalBlocker, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from connection_assistant.android.toolchain import Toolchain
from connection_assistant.models import PairingRequest, ProgressEvent, Stage
from connection_assistant.orchestrator import AssistantConfig, Orchestrator
from connection_assistant.security.files import sanitize_text

CONSENT_TEXT = (
    "This assistant captures a session from YOUR OWN Tinder account, running in a "
    "local Android emulator that you control.\n\n"
    "By continuing you confirm that:\n"
    "  • You own this Tinder account and are authorized to connect it to Icebreaker.\n"
    "  • You understand automating a provider account may violate that provider's "
    "terms of service, and you accept responsibility for that decision.\n"
    "  • You will complete login, OTP and any CAPTCHA yourself on the emulator.\n\n"
    "No credentials are captured until you log in. Token values are never shown, "
    "logged, or sent anywhere except directly to Icebreaker over HTTPS (or saved "
    "locally if you explicitly choose to)."
)


class _Worker(QObject):
    """Runs one orchestrator operation off the UI thread."""

    progress = Signal(object)   # ProgressEvent
    finished = Signal(object)   # arbitrary result
    failed = Signal(str)        # sanitized message
    pairing = Signal(object)    # PairingRequest

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # sanitize every failure before it reaches the UI
            self.failed.emit(sanitize_text(f"{type(exc).__name__}: {exc}"))
            return
        self.finished.emit(result)


class MainWindow(QWidget):
    _CREATE_EMULATOR = "__create_emulator__"

    def __init__(self, config: AssistantConfig | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Icebreaker Connect")
        self.resize(780, 700)
        self._config = config or AssistantConfig()
        self._orch = Orchestrator(self._config, on_progress=self._on_progress_threadsafe)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._async_on_done: Callable[[object], None] | None = None
        self._async_on_failed: Callable[[str], None] | None = None
        self._apk_path: str | None = None
        self._emulator_status: dict[str, tuple[bool | None, str | None]] = {}
        self._pairing_uri = ""

        self._stack = QStackedWidget()
        self._log = QPlainTextEdit(readOnly=True)
        self._log.setMaximumHeight(150)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate; hidden when idle
        self._progress.hide()

        self._build_pages()

        root = QVBoxLayout(self)
        root.addWidget(self._stack, 1)
        root.addWidget(self._progress)
        root.addWidget(QLabel("Activity (no sensitive values are shown):"))
        root.addWidget(self._log)

        self._goto(0)

    # -- progress plumbing ------------------------------------------------ #
    _progress_signal = Signal(object)
    _pairing_signal = Signal(object)

    def _on_progress_threadsafe(self, event: ProgressEvent) -> None:
        # Called from worker threads; marshal to the UI thread via a queued signal.
        self._progress_signal.emit(event)

    def _append_log(self, event: ProgressEvent) -> None:
        prefix = {"warn": "⚠ ", "error": "✗ "}.get(event.level, "• ")
        self._log.appendPlainText(prefix + sanitize_text(event.message))

    # -- page construction ------------------------------------------------ #
    def _build_pages(self) -> None:
        self._progress_signal.connect(self._append_log)
        self._pairing_signal.connect(self._show_pairing)
        self._stack.addWidget(self._page_consent())
        self._stack.addWidget(self._page_capture_setup())
        self._stack.addWidget(self._page_review())
        self._stack.addWidget(self._page_deliver())
        self._stack.addWidget(self._page_result())

    TOTAL_STEPS = 5

    def _page(
        self,
        title: str,
        body: QWidget,
        *,
        does: str,
        then: str = "",
        privacy: str = "",
    ) -> QWidget:
        """Wrap a page body with a step counter, heading and a plain-language card.

        Every step therefore shows, consistently: what the step does, what happens
        when you act, and the privacy implication — so nothing is a mystery click.
        """
        step = self._stack.count() + 1
        page = QWidget()
        layout = QVBoxLayout(page)
        counter = QLabel(f"<span style='color:#888'>Step {step} of {self.TOTAL_STEPS}</span>")
        layout.addWidget(counter)
        heading = QLabel(f"<h2 style='margin:0'>{title}</h2>")
        layout.addWidget(heading)
        layout.addWidget(self._explain_card(does, then, privacy))
        layout.addWidget(body, 1)
        return page

    def _explain_card(self, does: str, then: str, privacy: str) -> QWidget:
        """A framed, three-part explanation shown at the top of every step."""
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setStyleSheet(
            "QFrame { background: rgba(127,127,127,0.10); border: 1px solid "
            "rgba(127,127,127,0.35); border-radius: 8px; } QLabel { border: none; }"
        )
        v = QVBoxLayout(card)
        v.setSpacing(4)
        rows = [("What this step does", does)]
        if then:
            rows.append(("When you continue", then))
        if privacy:
            rows.append(("Your privacy", privacy))
        for label, value in rows:
            row = QLabel(f"<b>{label}:</b> {value}")
            row.setWordWrap(True)
            row.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(row)
        return card

    def _nav(self, back: int | None, forward_label: str, forward_fn: Callable[[], None]) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        if back is not None:
            b = QPushButton("Back")
            b.clicked.connect(lambda: self._goto(back))
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel && erase")
        cancel.clicked.connect(self._cancel_and_erase)
        row.addWidget(cancel)
        nxt = QPushButton(forward_label)
        nxt.clicked.connect(forward_fn)
        row.addWidget(nxt)
        return bar

    # 1. Consent -------------------------------------------------------- #
    def _page_consent(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        text = QLabel(CONSENT_TEXT)
        text.setWordWrap(True)
        v.addWidget(text)
        self._consent_check = QCheckBox("I own this account and accept the above.")
        v.addWidget(self._consent_check)
        v.addStretch(1)
        v.addWidget(self._nav(None, "Continue", self._consent_next))
        return self._page(
            "Ownership & consent",
            body,
            does="Confirms you own the Tinder account and understand the terms-of-service "
            "implications before anything is captured.",
            then="The capture workspace opens and checks the environment automatically.",
            privacy="No account data is touched until you log in later. This is just a checkbox.",
        )

    def _consent_next(self) -> None:
        if not self._consent_check.isChecked():
            self._warn("Please confirm ownership and consent to continue.")
            return
        self._goto(1)
        self._run_env_check(auto_start=True)

    # 2. Setup, emulator and capture ----------------------------------- #
    def _page_capture_setup(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        self._env_label = QLabel("Environment will be checked automatically.")
        self._env_label.setWordWrap(True)
        v.addWidget(self._env_label)

        self._repair_box = QFrame()
        repair_layout = QVBoxLayout(self._repair_box)
        repair_layout.setContentsMargins(0, 0, 0, 0)
        self._license_check = QCheckBox(
            "I accept the Android SDK license terms (required to download components)."
        )
        repair_layout.addWidget(self._license_check)
        environment_row = QHBoxLayout()
        self._host_btn = QPushButton("Install missing tools")
        self._host_btn.clicked.connect(self._run_host_tools)
        self._setup_btn = QPushButton("Set up Android emulator")
        self._setup_btn.clicked.connect(self._run_env_setup)
        environment_row.addWidget(self._host_btn)
        environment_row.addWidget(self._setup_btn)
        environment_row.addStretch(1)
        repair_layout.addLayout(environment_row)
        self._repair_box.hide()
        v.addWidget(self._repair_box)

        emulator_row = QHBoxLayout()
        emulator_row.addWidget(QLabel("Emulator:"))
        self._emulator_combo = QComboBox()
        self._emulator_combo.addItem("Checking configured emulators…", None)
        self._emulator_combo.currentIndexChanged.connect(self._emulator_changed)
        emulator_row.addWidget(self._emulator_combo, 1)
        v.addLayout(emulator_row)
        self._emulator_label = QLabel(
            "Choose an emulator. Tinder installation and version appear in the list after "
            "that emulator is checked."
        )
        self._emulator_label.setWordWrap(True)
        v.addWidget(self._emulator_label)

        self._apk_label = QLabel("Choose an APK, or use Tinder already installed in the emulator.")
        self._apk_label.setWordWrap(True)
        v.addWidget(self._apk_label)
        start_row = QHBoxLayout()
        self._choose_start_btn = QPushButton("Choose APK && start capture…")
        self._choose_start_btn.clicked.connect(self._pick_apk_and_start)
        self._start_installed_btn = QPushButton("Open selected emulator")
        self._start_installed_btn.clicked.connect(self._start_installed_capture)
        self._choose_start_btn.hide()
        self._start_installed_btn.hide()
        start_row.addWidget(self._choose_start_btn)
        start_row.addWidget(self._start_installed_btn)
        start_row.addStretch(1)
        v.addLayout(start_row)

        self._capture_status = QLabel(
            "Capture starts automatically before Tinder opens. Log in, complete OTP/CAPTCHA, "
            "then open one conversation."
        )
        self._capture_status.setWordWrap(True)
        v.addWidget(self._capture_status)
        stop_row = QHBoxLayout()
        self._stop_capture_btn = QPushButton("Stop capture && review")
        self._stop_capture_btn.clicked.connect(self._stop_capture_and_review)
        self._stop_capture_btn.setEnabled(False)
        self._stop_capture_btn.hide()
        self._stop_emulator_btn = QPushButton("Stop emulator")
        self._stop_emulator_btn.clicked.connect(self._stop_emulator_and_review)
        self._stop_emulator_btn.setEnabled(False)
        self._stop_emulator_btn.hide()
        stop_row.addWidget(self._stop_capture_btn)
        stop_row.addWidget(self._stop_emulator_btn)
        v.addLayout(stop_row)
        v.addStretch(1)

        cancel = QPushButton("Cancel && erase")
        cancel.clicked.connect(self._cancel_and_erase)
        v.addWidget(cancel)
        return self._page(
            "Set up, open Tinder && capture",
            body,
            does="Checks the tools, accepts your APK, boots the emulator and starts capture "
            "before opening Tinder — all on this page.",
            then="Choosing an APK starts everything automatically. Use Stop capture when login "
            "is complete, or Stop emulator to stop both while preserving captured fields.",
            privacy="Only api.gotinder.com traffic is inspected, nothing is dumped to disk, and "
            "the emulator proxy is always removed when capture stops.",
        )

    def _run_env_check(self, *, auto_start: bool = False) -> None:
        def task() -> tuple[Toolchain, list[str]]:
            toolchain = self._orch.detect_environment()
            emulators = self._orch.list_emulators() if toolchain.emulator else []
            return toolchain, emulators

        def done(result: tuple[Toolchain, list[str]]) -> None:
            toolchain, emulators = result
            summary = toolchain.summary()
            self._update_environment_controls(toolchain)
            self._populate_emulators(emulators)
            if auto_start and all(summary.values()):
                if len(emulators) == 1:
                    self._capture_status.setText(
                        "Environment is ready. Starting the only configured emulator "
                        "automatically…"
                    )
                    self._start_emulator_automatically()
                elif emulators:
                    self._capture_status.setText(
                        "Ready. Choose an emulator above; selecting it opens Tinder and starts "
                        "capture automatically."
                    )
                    self._show_capture_action("open")
                else:
                    self._capture_status.setText(
                        "No emulator exists yet. Choose “Create a new emulator…” above."
                    )
            elif auto_start:
                self._capture_status.setText(
                    "Use the setup option shown above. It disappears as soon as setup is ready."
                )

        self._run_async(task, done)

    def _update_environment_controls(self, toolchain: Toolchain) -> None:
        """Keep prerequisite controls out of sight unless they can fix a real gap."""
        summary = toolchain.summary()
        missing = [name.replace("_", " ") for name, ok in summary.items() if not ok]
        if not missing:
            self._env_label.setText("✓ Ready")
        else:
            self._env_label.setText("Setup needed: " + ", ".join(missing))

        plan = self._orch.plan_host_tools()
        show_host = bool(plan.tools)
        show_sdk = not toolchain.sdk_ready
        self._host_btn.setVisible(show_host)
        self._setup_btn.setVisible(show_sdk)
        self._license_check.setVisible(show_sdk)
        self._repair_box.setVisible(show_host or show_sdk)

    def _show_capture_action(self, action: str) -> None:
        """Show only the single action that makes sense for the current state."""
        choose_apk = action == "apk"
        open_emulator = action == "open"
        self._choose_start_btn.setVisible(choose_apk)
        self._choose_start_btn.setEnabled(choose_apk)
        self._start_installed_btn.setVisible(open_emulator)
        self._start_installed_btn.setEnabled(open_emulator)

    def _emulator_display(self, name: str) -> str:
        installed, version = self._emulator_status.get(name, (None, None))
        if installed is None:
            return f"{name} — Tinder status not checked"
        if not installed:
            return f"{name} — Tinder not installed"
        return f"{name} — Tinder {version or 'installed (version unavailable)'}"

    def _populate_emulators(self, names: list[str], *, selected: str | None = None) -> None:
        selected = selected or self._config.avd_name
        blocker = QSignalBlocker(self._emulator_combo)
        self._emulator_combo.clear()
        for name in names:
            self._emulator_combo.addItem(self._emulator_display(name), name)
        if names:
            self._emulator_combo.insertSeparator(self._emulator_combo.count())
        self._emulator_combo.addItem("＋ Create a new emulator…", self._CREATE_EMULATOR)
        index = self._emulator_combo.findData(selected)
        if index < 0 and names:
            index = 0
        self._emulator_combo.setCurrentIndex(index if index >= 0 else 0)
        self._emulator_combo.setEnabled(True)
        del blocker
        data = self._emulator_combo.currentData()
        if isinstance(data, str) and data != self._CREATE_EMULATOR:
            self._config.avd_name = data
            self._emulator_label.setText(self._emulator_display(data))

    def _refresh_emulators(self) -> None:
        self._run_async(self._orch.list_emulators, self._populate_emulators)

    def _emulator_changed(self, _index: int) -> None:
        selected = self._emulator_combo.currentData()
        if selected == self._CREATE_EMULATOR:
            self._create_emulator()
            return
        if not isinstance(selected, str) or not selected:
            return
        self._config.avd_name = selected
        self._emulator_label.setText(self._emulator_display(selected))
        if not self._operation_running():
            self._capture_status.setText(
                f"Starting '{selected}' to check Tinder and begin capture automatically…"
            )
            self._start_emulator_automatically()

    def _create_emulator(self) -> None:
        previous = self._config.avd_name
        name, accepted = QInputDialog.getText(
            self,
            "Create emulator",
            "Name for the new rootable emulator:",
            text="icebreaker_capture",
        )
        if not accepted or not name.strip():
            self._select_emulator(previous)
            return
        if not self._ensure_android_license_consent():
            self._select_emulator(previous)
            return
        self._emulator_combo.setEnabled(False)
        self._capture_status.setText(f"Creating rootable emulator '{name.strip()}'…")

        def done(created: str) -> None:
            names = self._orch.list_emulators()
            self._populate_emulators(names, selected=created)
            self._capture_status.setText(
                f"Emulator '{created}' was created. Starting it automatically…"
            )
            self._start_emulator_automatically()

        self._run_async(
            lambda: self._orch.create_emulator(name),
            done,
            on_failed=self._emulator_create_failed,
        )

    def _select_emulator(self, name: str) -> None:
        blocker = QSignalBlocker(self._emulator_combo)
        index = self._emulator_combo.findData(name)
        if index >= 0:
            self._emulator_combo.setCurrentIndex(index)
        del blocker

    def _emulator_create_failed(self, message: str) -> None:
        self._emulator_combo.setEnabled(True)
        self._capture_status.setText("Could not create the emulator. Fix the issue and retry.")
        self._warn(message)

    def _selected_emulator(self) -> str | None:
        selected = self._emulator_combo.currentData()
        if isinstance(selected, str) and selected != self._CREATE_EMULATOR:
            return selected
        return None

    def _run_host_tools(self) -> None:
        plan = self._orch.plan_host_tools()
        if plan.is_empty:
            self._info(plan.note or "No host tools are missing.")
            return
        pkgs = ", ".join(plan.tools)
        if not self._confirm(
            f"Install host tools ({pkgs}) using {plan.manager}?\n\n"
            "A graphical admin prompt may appear."
        ):
            return

        def task() -> Toolchain:
            self._orch.install_host_tools()
            return self._orch.detect_environment()

        def done(_toolchain: Toolchain) -> None:
            self._run_env_check()

        self._run_async(task, done)

    def _run_env_setup(self) -> None:
        if not self._ensure_android_license_consent():
            return

        def task() -> Toolchain:
            return self._orch.setup_environment()

        def done(_toolchain: Toolchain) -> None:
            self._run_env_check()

        self._run_async(task, done)

    def _ensure_android_license_consent(self) -> bool:
        if self._config.licenses_accepted:
            return True
        if self._license_check.isChecked() or self._confirm(
            "Android emulator setup downloads Google SDK components and requires accepting "
            "the Android SDK license terms. Accept and continue?"
        ):
            self._config.licenses_accepted = True
            self._license_check.setChecked(True)
            return True
        return False

    def _pick_apk_and_start(self) -> None:
        if self._selected_emulator() is None:
            self._warn("Choose or create an emulator first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select Tinder APK", "", "APK files (*.apk)")
        if path:
            self._apk_path = path
            self._apk_label.setText(f"Selected: {Path(path).name}")
            if self._operation_running():
                self._capture_status.setText(
                    "APK selected. It will be installed and capture will start as soon as "
                    "the emulator is ready."
                )
                return
            self._start_capture_flow()

    def _start_emulator_automatically(self) -> None:
        selected = self._selected_emulator()
        if selected is None:
            self._warn("Choose or create an emulator first.")
            return
        self._config.avd_name = selected
        self._emulator_combo.setEnabled(False)
        self._show_capture_action("none")

        def task() -> tuple[bool, str | None]:
            self._orch.start_emulator()
            return self._orch.tinder_status()

        def done(status: tuple[bool, str | None]) -> None:
            installed, version = status
            self._emulator_status[selected] = status
            index = self._emulator_combo.findData(selected)
            if index >= 0:
                self._emulator_combo.setItemText(index, self._emulator_display(selected))
            self._emulator_label.setText(self._emulator_display(selected))
            self._stop_emulator_btn.setEnabled(self._orch.emulator_started)
            self._stop_emulator_btn.setVisible(self._orch.emulator_started)
            if self._apk_path or installed:
                self._capture_status.setText(
                    "Emulator is ready. Starting capture before Tinder opens…"
                )
                self._start_capture_flow()
                return
            self._show_capture_action("apk")
            self._capture_status.setText(
                "Emulator is ready, but Tinder is not installed. Choose your APK; capture "
                "will start immediately after installation."
            )

        self._run_async(task, done, on_failed=self._capture_start_failed)

    def _start_installed_capture(self) -> None:
        if self._operation_running():
            self._capture_status.setText(
                "The emulator is still starting. Installed Tinder will open and capture will "
                "begin automatically as soon as it is ready."
            )
            return
        if self._selected_emulator() is None:
            self._warn("Choose or create an emulator first.")
            return
        self._start_capture_flow()

    def _start_capture_flow(self) -> None:
        self._capture_status.setText("Starting emulator and capture…")
        self._emulator_combo.setEnabled(False)
        self._show_capture_action("none")

        def task() -> None:
            tc = self._orch.detect_environment()
            missing = list(tc.missing)
            if not tc.openssl:
                missing.append("openssl")
            if missing:
                raise RuntimeError(
                    "missing required tools: " + ", ".join(dict.fromkeys(missing))
                )
            self._orch.start_emulator_and_capture(self._apk_path)

        self._run_async(task, self._capture_started, on_failed=self._capture_start_failed)

    def _operation_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _capture_started(self, _result: object) -> None:
        self._capture_status.setText(
            "Capture is live. Finish Tinder login and open one conversation, then stop capture."
        )
        self._stop_capture_btn.setEnabled(True)
        self._stop_capture_btn.show()
        self._stop_emulator_btn.setEnabled(True)
        self._stop_emulator_btn.show()

    def _capture_start_failed(self, message: str) -> None:
        self._emulator_combo.setEnabled(True)
        selected = self._selected_emulator()
        installed = self._emulator_status.get(selected or "", (None, None))[0]
        self._show_capture_action("apk" if installed is False else "open")
        self._stop_capture_btn.setEnabled(False)
        self._stop_capture_btn.hide()
        self._stop_emulator_btn.setEnabled(self._orch.emulator_started)
        self._stop_emulator_btn.setVisible(self._orch.emulator_started)
        self._capture_status.setText("Could not start capture. Fix the reported issue and retry.")
        self._warn(message)

    def _stop_capture_and_review(self) -> None:
        self._stop_capture_btn.setEnabled(False)
        self._run_async(self._orch.stop_capture, self._capture_stopped)

    def _stop_emulator_and_review(self) -> None:
        self._stop_capture_btn.setEnabled(False)
        self._stop_emulator_btn.setEnabled(False)
        owned = self._orch.emulator_started

        def done(_bundle: object) -> None:
            self._emulator_combo.setEnabled(True)
            if owned:
                self._capture_status.setText("Capture and emulator stopped.")
            else:
                self._capture_status.setText(
                    "Capture stopped. The pre-existing emulator was left running."
                )
            self._capture_stopped(_bundle)

        self._run_async(self._orch.stop_emulator, done)

    def _capture_stopped(self, _bundle: object) -> None:
        self._show_capture_action("open")
        self._stop_capture_btn.setEnabled(False)
        self._stop_capture_btn.hide()
        self._stop_emulator_btn.setEnabled(self._orch.emulator_started)
        self._stop_emulator_btn.setVisible(self._orch.emulator_started)
        self._refresh_review()
        self._goto(2)

    # 3. Review --------------------------------------------------------- #
    def _page_review(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        self._review_label = QLabel("Captured fields will appear automatically.")
        self._review_label.setWordWrap(True)
        v.addWidget(self._review_label)
        v.addStretch(1)
        row = QHBoxLayout()
        capture_again = QPushButton("Capture again")
        capture_again.clicked.connect(lambda: self._goto(1))
        self._review_stop_emulator_btn = QPushButton("Stop emulator")
        self._review_stop_emulator_btn.clicked.connect(self._stop_emulator_from_review)
        cancel = QPushButton("Cancel && erase")
        cancel.clicked.connect(self._cancel_and_erase)
        proceed = QPushButton("Continue to QR approval")
        proceed.clicked.connect(self._review_next)
        row.addWidget(capture_again)
        row.addWidget(self._review_stop_emulator_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(proceed)
        v.addLayout(row)
        return self._page(
            "Captured fields",
            body,
            does="Shows which fields were captured with a ✓/✗ — so you can confirm a usable "
            "session before deciding to deliver it.",
            then="You can only continue once a usable auth token was captured (and no "
            "unsolved CAPTCHA remains).",
            privacy="Only presence is shown. Token and id values are never displayed here or "
            "anywhere in the UI.",
        )

    def _refresh_review(self) -> None:
        bundle = self._orch.bundle
        presence = bundle.field_presence()
        lines = [f"{'✓' if v else '✗'} {k}" for k, v in presence.items()]
        if bundle.captcha_challenge:
            lines.append("\n⚠ A CAPTCHA/verification challenge was detected. Solve it in "
                         "the Tinder app and re-capture; no usable token was issued.")
        elif not bundle.has_usable_session():
            lines.append("\n✗ No usable auth token captured yet.")
        else:
            lines.append("\n✓ A usable session is ready to deliver.")
        self._review_label.setText("Captured (values hidden):\n" + "\n".join(lines))
        self._review_stop_emulator_btn.setEnabled(self._orch.emulator_started)

    def _stop_emulator_from_review(self) -> None:
        self._review_stop_emulator_btn.setEnabled(False)

        def done(_bundle: object) -> None:
            self._emulator_combo.setEnabled(True)
            self._append_log(ProgressEvent(Stage.EMULATOR, "emulator stopped"))

        self._run_async(self._orch.stop_emulator, done)

    def _review_next(self) -> None:
        if not self._orch.bundle.has_usable_session():
            self._warn("No usable session captured yet. Capture a session first.")
            return
        self._goto(3)
        self._send_securely()

    # 4. Deliver -------------------------------------------------------- #
    def _page_deliver(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        self._send_btn = QPushButton("Create QR && send securely")
        self._send_btn.clicked.connect(self._send_securely)
        save = QPushButton("Save locally (JSON)…")
        save.clicked.connect(self._save_locally)
        v.addWidget(self._send_btn)
        v.addWidget(save)
        self._pairing_label = QLabel(
            "Create a pairing, then scan the QR with your phone and approve it in Telegram."
        )
        self._pairing_label.setWordWrap(True)
        self._pairing_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self._pairing_label)
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignCenter)
        self._qr_label.setMinimumHeight(0)
        v.addWidget(self._qr_label)
        self._pairing_link_label = QLabel("")
        self._pairing_link_label.setWordWrap(True)
        self._pairing_link_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self._pairing_link_label)
        pairing_actions = QHBoxLayout()
        self._open_pairing_btn = QPushButton("Open in Telegram on this device")
        self._open_pairing_btn.clicked.connect(self._open_pairing_link)
        self._open_pairing_btn.setEnabled(False)
        self._copy_pairing_btn = QPushButton("Copy approval link")
        self._copy_pairing_btn.clicked.connect(self._copy_pairing_link)
        self._copy_pairing_btn.setEnabled(False)
        pairing_actions.addWidget(self._open_pairing_btn)
        pairing_actions.addWidget(self._copy_pairing_btn)
        v.addLayout(pairing_actions)
        v.addStretch(1)
        cancel = QPushButton("Cancel && erase")
        cancel.clicked.connect(self._cancel_and_erase)
        v.addWidget(cancel)
        return self._page(
            "Send securely / Save locally",
            body,
            does="Delivers the captured session. “Send securely” pairs with Icebreaker over "
            "HTTPS and uploads it; “Save locally” writes an owner-only JSON file instead.",
            then="Sending shows an approval link/QR you confirm in the Telegram Mini App, then "
            "uploads once. Saving asks where to write the file.",
            privacy="Upload uses a one-time token that is never shown in the QR. Saved files are "
            "created readable only by you. On success the in-memory copy is wiped.",
        )

    def _send_securely(self) -> None:
        if not self._config.pairing_enabled():
            self._warn("Secure delivery is unavailable. Use Save locally instead.")
            return
        self._send_btn.setEnabled(False)
        self._pairing_label.setText("Creating a short-lived approval QR…")
        self._qr_label.clear()
        self._pairing_link_label.clear()

        def on_pairing(request: PairingRequest) -> None:
            self._pairing_signal.emit(request)
            self._progress_signal.emit(ProgressEvent(
                "deliver",
                f"Approve this pairing in Telegram: {request.verification_uri}",
            ))

        def task() -> object:
            return self._orch.send_securely(consent_ack=True, on_pairing=on_pairing)

        self._run_async(task, self._delivery_done, on_failed=self._delivery_failed)

    def _show_pairing(self, request: PairingRequest) -> None:
        self._pairing_uri = request.verification_uri
        self._qr_label.setPixmap(self._qr_pixmap(request.verification_uri))
        self._pairing_label.setText(
            "Waiting for approval from the Icebreaker bot…\n\n"
            "1. Scan this QR with your phone camera.\n"
            "2. Tap the link/banner that appears on your phone. Scanning alone does not "
            "approve the connection.\n"
            "3. Telegram will open the Icebreaker bot. Tap Start to approve.\n"
            "4. The bot will say it is waiting for credentials from this desktop app.\n\n"
            "Keep this window open. The desktop will continue automatically after approval."
        )
        self._pairing_link_label.setText(
            "If scanning is unavailable, open or copy this approval link:\n"
            + request.verification_uri
        )
        self._open_pairing_btn.setEnabled(True)
        self._copy_pairing_btn.setEnabled(True)

    @staticmethod
    def _qr_pixmap(value: str, *, box_size: int = 7) -> QPixmap:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=4,
        )
        qr.add_data(value)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        side = len(matrix) * box_size
        image = QImage(side, side, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.black)
        for row, cells in enumerate(matrix):
            for column, dark in enumerate(cells):
                if dark:
                    painter.drawRect(
                        column * box_size,
                        row * box_size,
                        box_size,
                        box_size,
                    )
        painter.end()
        return QPixmap.fromImage(image)

    def _open_pairing_link(self) -> None:
        if self._pairing_uri:
            QDesktopServices.openUrl(QUrl(self._pairing_uri))

    def _copy_pairing_link(self) -> None:
        if self._pairing_uri:
            QApplication.clipboard().setText(self._pairing_uri)
            self._pairing_label.setText(
                "Approval link copied. Open it on the phone that owns the Telegram account."
            )

    def _delivery_failed(self, message: str) -> None:
        self._send_btn.setEnabled(True)
        self._pairing_label.setText(
            "Delivery did not complete. Create a new QR and approve it before it expires."
        )
        self._warn(message)

    def _save_locally(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save session bundle", "session-bundle.json", "JSON (*.json)"
        )
        if not path:
            return
        self._run_async(lambda: self._orch.save_locally(path), self._delivery_done)

    def _delivery_done(self, result: object) -> None:
        from connection_assistant.models import DeliveryResult

        if isinstance(result, DeliveryResult):
            self._result_ok = result.ok
            detail = result.detail
            if result.saved_path:
                detail += f"\nSaved to: {result.saved_path}"
            self._result_label.setText(("✓ " if result.ok else "✗ ") + detail)
            if result.ok:
                self._orch.wipe_bundle()
            self._goto(4)

    # 5. Result --------------------------------------------------------- #
    def _page_result(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        self._result_ok = False
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        v.addWidget(self._result_label)
        v.addStretch(1)
        close = QPushButton("Finish & clean up")
        close.clicked.connect(self._finish)
        v.addWidget(close)
        return self._page(
            "Result",
            body,
            does="Reports the outcome of delivery in plain language — success, or an "
            "actionable, sanitized error with nothing sensitive in it.",
            then="“Finish & clean up” removes the emulator proxy, stops capture, stops any "
            "emulator this app started, and clears captured data from memory.",
            privacy="Cleanup also runs automatically if you close the window at any time.",
        )

    def _finish(self) -> None:
        self._orch.cleanup()
        self.close()

    # -- async plumbing --------------------------------------------------- #
    def _run_async(
        self,
        task: Callable[[], object],
        on_done: Callable[[object], None],
        *,
        on_failed: Callable[[str], None] | None = None,
    ) -> None:
        if self._operation_running():
            self._warn("Another operation is still running. Please wait.")
            return
        self._progress.show()
        thread = QThread()
        worker = _Worker(task)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # These are bound QObject slots, so Qt queues them onto MainWindow's GUI
        # thread. Connecting to local Python closures executes them on the worker
        # thread in PySide, which caused native libQt6Gui segmentation faults.
        worker.finished.connect(self._async_finished)
        worker.failed.connect(self._async_failed)
        self._async_on_done = on_done
        self._async_on_failed = on_failed
        self._thread, self._worker = thread, worker
        thread.start()

    def _finish_async_thread(self) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        self._progress.hide()
        if thread is not None:
            thread.quit()
            thread.wait()

    @Slot(object)
    def _async_finished(self, result: object) -> None:
        callback = self._async_on_done
        self._async_on_done = None
        self._async_on_failed = None
        self._finish_async_thread()
        if callback is not None:
            callback(result)

    @Slot(str)
    def _async_failed(self, message: str) -> None:
        callback = self._async_on_failed
        self._async_on_done = None
        self._async_on_failed = None
        self._finish_async_thread()
        if callback is None:
            self._warn(message)
        else:
            callback(message)

    # -- misc UI helpers -------------------------------------------------- #
    def _goto(self, index: int) -> None:
        self._stack.setCurrentIndex(index)

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "Icebreaker Connect", sanitize_text(message))

    def _info(self, message: str) -> None:
        QMessageBox.information(self, "Icebreaker Connect", sanitize_text(message))

    def _confirm(self, message: str) -> bool:
        reply = QMessageBox.question(
            self, "Icebreaker Connect", sanitize_text(message),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _cancel_and_erase(self) -> None:
        self._orch.cancel()
        self._orch.cleanup()
        self._result_label.setText("Cancelled. The proxy was cleared and captured data erased.")
        self._goto(4)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        # The cleanup contract: tearing down the window always cleans up.
        try:
            self._orch.cleanup()
        finally:
            super().closeEvent(event)


def run_gui(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
