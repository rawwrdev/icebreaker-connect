"""Import/launch smoke checks that need no real APK, emulator, or network."""

from __future__ import annotations

import importlib
import time

import pytest

MODULES = [
    "connection_assistant",
    "connection_assistant.__main__",
    "connection_assistant.models",
    "connection_assistant.orchestrator",
    "connection_assistant.security.files",
    "connection_assistant.capture.protobuf",
    "connection_assistant.capture.collector",
    "connection_assistant.capture.process",
    "connection_assistant.capture.mitm_addon",
    "connection_assistant.android.toolchain",
    "connection_assistant.android.shell",
    "connection_assistant.android.controller",
    "connection_assistant.android.installer",
    "connection_assistant.android.host_tools",
    "connection_assistant.pairing.client",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    assert importlib.import_module(name) is not None


def test_cli_version(capsys):
    from connection_assistant.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_cli_check_runs_without_error():
    from connection_assistant.__main__ import main

    rc = main(["--check"])
    assert rc in (0, 1)  # 1 only means some component is missing, not a crash


def test_toolchain_detect_is_side_effect_free():
    from connection_assistant.android.toolchain import detect_toolchain

    tc = detect_toolchain()
    summary = tc.summary()
    assert set(summary) == {
        "java", "android_sdk", "adb", "emulator",
        "sdkmanager", "avdmanager", "mitmproxy", "openssl",
    }


def test_system_image_is_rootable_google_apis_not_playstore():
    from connection_assistant.android.toolchain import system_image_package

    pkg = system_image_package("x86_64")
    assert "google_apis" in pkg
    assert "playstore" not in pkg.lower()


def test_bundle_json_round_trip_and_presence():
    from connection_assistant.models import SessionBundle, SessionProfile

    bundle = SessionBundle(
        auth_token="fab-auth",
        refresh_token="fab-refresh",
        device_id="fab-device",
        install_id="fab-install",
        session_profile=SessionProfile(app_version="9999", tinder_version="99.9.9"),
    )
    data = bundle.to_bundle_json()
    restored = SessionBundle.from_bundle_json(data)
    assert restored.to_bundle_json() == data
    assert restored.field_presence()["refresh_token"] is True


def test_gui_module_imports_offscreen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    mod = importlib.import_module("connection_assistant.gui")
    assert hasattr(mod, "MainWindow")
    assert hasattr(mod, "run_gui")


def test_async_completion_callback_runs_on_gui_thread(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    gui_thread = QThread.currentThread()
    callback_threads = []
    window._run_async(  # noqa: SLF001
        lambda: None,
        lambda _result: callback_threads.append(QThread.currentThread()),
    )
    deadline = time.monotonic() + 5
    while not callback_threads and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert callback_threads == [gui_thread]
    window.close()
    app.processEvents()


def test_gui_has_single_capture_workspace_without_url_or_next_back(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    capture_page = window._stack.widget(1)  # noqa: SLF001
    button_texts = {button.text() for button in capture_page.findChildren(QPushButton)}

    assert window._stack.count() == 5  # noqa: SLF001
    assert "Choose APK && start capture…" in button_texts
    assert "Stop emulator" in button_texts
    assert "Back" not in button_texts
    assert "Next" not in button_texts
    assert window.findChildren(QLineEdit) == []

    window.close()
    app.processEvents()


def test_setup_buttons_are_hidden_when_environment_is_ready(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from pathlib import Path
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from connection_assistant.android.toolchain import JavaStatus, Toolchain
    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    ready = Toolchain(
        sdk_root=Path("/fabricated/sdk"),
        java=JavaStatus(True, 17, "/fabricated/java"),
        adb="adb",
        emulator="emulator",
        sdkmanager="sdkmanager",
        avdmanager="avdmanager",
        mitmdump="mitmdump",
        openssl="openssl",
    )
    monkeypatch.setattr(
        window._orch, "plan_host_tools", lambda: SimpleNamespace(tools=[])  # noqa: SLF001
    )

    window._update_environment_controls(ready)  # noqa: SLF001

    assert window._env_label.text() == "✓ Ready"  # noqa: SLF001
    assert window._repair_box.isHidden() is True  # noqa: SLF001
    assert window._host_btn.isHidden() is True  # noqa: SLF001
    assert window._setup_btn.isHidden() is True  # noqa: SLF001
    window.close()
    app.processEvents()


def test_only_relevant_setup_button_is_shown(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from pathlib import Path
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from connection_assistant.android.toolchain import JavaStatus, Toolchain
    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    missing_mitmproxy = Toolchain(
        sdk_root=Path("/fabricated/sdk"),
        java=JavaStatus(True, 17, "/fabricated/java"),
        adb="adb",
        emulator="emulator",
        sdkmanager="sdkmanager",
        avdmanager="avdmanager",
        mitmdump=None,
        openssl="openssl",
        missing=["mitmproxy"],
    )
    monkeypatch.setattr(
        window._orch,
        "plan_host_tools",
        lambda: SimpleNamespace(tools=["mitmproxy"]),  # noqa: SLF001
    )

    window._update_environment_controls(missing_mitmproxy)  # noqa: SLF001

    assert window._repair_box.isHidden() is False  # noqa: SLF001
    assert window._host_btn.isHidden() is False  # noqa: SLF001
    assert window._setup_btn.isHidden() is True  # noqa: SLF001
    assert window._license_check.isHidden() is True  # noqa: SLF001
    window.close()
    app.processEvents()


def test_choosing_apk_starts_capture_immediately(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._populate_emulators(["tinder_cap"])  # noqa: SLF001
    started = []
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("/tmp/fabricated.apk", "APK files (*.apk)"),
    )
    monkeypatch.setattr(window, "_start_capture_flow", lambda: started.append(True))

    window._pick_apk_and_start()  # noqa: SLF001

    assert started == [True]
    assert window._apk_path == "/tmp/fabricated.apk"  # noqa: SLF001
    window.close()
    app.processEvents()


def test_apk_can_be_selected_while_emulator_is_booting(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._populate_emulators(["tinder_cap"])  # noqa: SLF001
    started = []
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("/tmp/fabricated.apk", "APK files (*.apk)"),
    )
    monkeypatch.setattr(window, "_operation_running", lambda: True)
    monkeypatch.setattr(window, "_start_capture_flow", lambda: started.append(True))

    window._pick_apk_and_start()  # noqa: SLF001

    assert started == []
    assert window._choose_start_btn.isEnabled() is True  # noqa: SLF001
    assert "as soon as the emulator is ready" in window._capture_status.text()  # noqa: SLF001
    window.close()
    app.processEvents()


def test_emulator_dropdown_shows_tinder_installation_and_version(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._emulator_status = {  # noqa: SLF001
        "with_tinder": (True, "16.8.0"),
        "empty": (False, None),
    }
    window._populate_emulators(["with_tinder", "empty"], selected="with_tinder")  # noqa: SLF001
    items = [
        window._emulator_combo.itemText(index)  # noqa: SLF001
        for index in range(window._emulator_combo.count())  # noqa: SLF001
    ]

    assert "with_tinder — Tinder 16.8.0" in items
    assert "empty — Tinder not installed" in items
    assert "＋ Create a new emulator…" in items
    window.close()
    app.processEvents()


def test_selecting_another_emulator_starts_it_automatically(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._populate_emulators(["first", "second"], selected="first")  # noqa: SLF001
    started = []
    monkeypatch.setattr(window, "_start_emulator_automatically", lambda: started.append(True))

    window._emulator_combo.setCurrentIndex(  # noqa: SLF001
        window._emulator_combo.findData("second")  # noqa: SLF001
    )

    assert window._config.avd_name == "second"  # noqa: SLF001
    assert started == [True]
    window.close()
    app.processEvents()


def test_pairing_request_renders_scannable_qr_and_phone_instructions(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from connection_assistant.gui import MainWindow
    from connection_assistant.models import PairingRequest

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    request = PairingRequest(
        pairing_id="fabricated-pairing",
        verification_uri="https://t.me/example_bot?startapp=fabricated",
        upload_token="fabricated-upload-token",
    )

    window._show_pairing(request)  # noqa: SLF001

    assert window._qr_label.pixmap().isNull() is False  # noqa: SLF001
    assert "Scan this QR" in window._pairing_label.text()  # noqa: SLF001
    assert "Scanning alone does not approve" in window._pairing_label.text()  # noqa: SLF001
    assert "Tap Start" in window._pairing_label.text()  # noqa: SLF001
    assert "waiting for credentials" in window._pairing_label.text()  # noqa: SLF001
    assert request.upload_token not in window._pairing_link_label.text()  # noqa: SLF001
    assert request.verification_uri in window._pairing_link_label.text()  # noqa: SLF001
    window.close()
    app.processEvents()


def test_installed_tinder_button_stays_clickable_during_emulator_boot(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from connection_assistant.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    started = []
    monkeypatch.setattr(window, "_operation_running", lambda: True)
    monkeypatch.setattr(window, "_start_capture_flow", lambda: started.append(True))

    assert window._start_installed_btn.isEnabled() is True  # noqa: SLF001
    window._start_installed_btn.click()  # noqa: SLF001

    assert started == []
    assert "automatically as soon as it is ready" in window._capture_status.text()  # noqa: SLF001
    window.close()
    app.processEvents()
