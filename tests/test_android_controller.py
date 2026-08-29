"""ADB device selection waits for normal cold-emulator startup."""

from __future__ import annotations

import pytest

from connection_assistant.android import controller
from connection_assistant.android.controller import AdbController, AdbDevice, NoDeviceError
from connection_assistant.android.shell import CommandError, CommandResult


def test_wait_for_single_device_retries_until_emulator_is_ready(monkeypatch):
    states = [[], [AdbDevice("emulator-5554", "offline")], [AdbDevice("emulator-5554", "device")]]
    progress = []
    monkeypatch.setattr(controller, "list_devices", lambda _adb: states.pop(0))
    monkeypatch.setattr(controller.time, "sleep", lambda _seconds: None)

    selected = AdbController.wait_for_single_device(
        process_running=lambda: True,
        on_progress=progress.append,
    )

    assert selected.serial == "emulator-5554"
    assert progress == ["waiting for emulator to appear in adb"]


def test_wait_for_single_device_reports_early_emulator_exit(monkeypatch):
    monkeypatch.setattr(controller, "list_devices", lambda _adb: [])

    with pytest.raises(NoDeviceError, match="exited before"):
        AdbController.wait_for_single_device(process_running=lambda: False)


def test_package_version_reports_installed_tinder_version(monkeypatch):
    selected = AdbController("emulator-5554")
    monkeypatch.setattr(selected, "is_package_installed", lambda _package="com.tinder": True)
    monkeypatch.setattr(
        selected,
        "_shell",
        lambda *args, **kwargs: "Package [com.tinder]\n  versionCode=123\n  versionName=16.8.0\n",
    )

    assert selected.package_version() == "16.8.0"


def test_launch_app_resolves_and_checks_launcher_activity(monkeypatch):
    selected = AdbController("emulator-5554")
    monkeypatch.setattr(
        selected,
        "_shell",
        lambda *args, **kwargs: "priority=0\ncom.tinder/.activities.LoginActivity\n",
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return CommandResult(0, "Status: ok", "")

    monkeypatch.setattr(controller, "run", fake_run)

    selected.launch_app()

    args, kwargs = calls[0]
    assert args[-4:] == ["start", "-W", "-n", "com.tinder/.activities.LoginActivity"]
    assert kwargs["check"] is True


def test_launch_app_rejects_package_without_launcher(monkeypatch):
    selected = AdbController("emulator-5554")
    monkeypatch.setattr(selected, "_shell", lambda *args, **kwargs: "No activity found\n")

    with pytest.raises(CommandError, match="no launcher activity"):
        selected.launch_app()


def test_force_stop_app_uses_selected_device(monkeypatch):
    selected = AdbController("emulator-5554")
    calls = []
    monkeypatch.setattr(
        controller,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or CommandResult(0, "", ""),
    )

    selected.force_stop_app()

    assert calls[0][0][-4:] == ["shell", "am", "force-stop", "com.tinder"]
    assert calls[0][1]["check"] is True


def test_loopback_proxy_creates_private_adb_tunnel(monkeypatch):
    selected = AdbController("emulator-5554")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return CommandResult(0, "", "")

    monkeypatch.setattr(controller, "run", fake_run)

    selected.set_loopback_proxy(8765)
    selected.clear_proxy()

    assert calls[0][0][-3:] == ["reverse", "tcp:8765", "tcp:8765"]
    assert calls[0][1]["check"] is True
    assert calls[1][0][-5:] == [
        "settings",
        "put",
        "global",
        "http_proxy",
        "127.0.0.1:8765",
    ]
    assert calls[-1][0][-3:] == ["reverse", "--remove", "tcp:8765"]
