"""Host-tool install planning (no real installs are performed)."""

from __future__ import annotations

import io

import pytest

from connection_assistant.android import host_tools


@pytest.fixture
def force_os(monkeypatch):
    def _set(os_key: str, manager: str | None, *, has_pkexec: bool = True):
        monkeypatch.setattr(host_tools, "_os_key", lambda: os_key)
        monkeypatch.setattr(host_tools, "detect_manager", lambda: manager)
        monkeypatch.setattr(
            host_tools.shutil, "which",
            lambda name: "/usr/bin/pkexec" if (name == "pkexec" and has_pkexec) else None,
        )
    return _set


def test_apt_plan_is_privileged_and_uses_pkexec(force_os):
    force_os("linux", "apt")
    plan = host_tools.plan_install(["mitmproxy", "java17", "platform-tools", "openssl"])
    assert plan.manager == "apt"
    assert plan.privileged is True
    assert plan.commands[0][0] == "pkexec"
    joined = " ".join(plan.commands[0])
    assert "apt-get" in joined and "mitmproxy" in joined and "adb" in joined


def test_linux_without_pkexec_gives_guidance_not_a_command(force_os):
    force_os("linux", "apt", has_pkexec=False)
    plan = host_tools.plan_install(["mitmproxy"])
    assert plan.is_empty
    assert "pkexec" in plan.note


def test_brew_plan_uses_cask_for_java(force_os):
    force_os("darwin", "brew")
    plan = host_tools.plan_install(["mitmproxy", "java17", "platform-tools"])
    assert plan.privileged is False
    flat = [" ".join(c) for c in plan.commands]
    assert any("android-platform-tools" in c for c in flat)
    assert any("--cask temurin" in c for c in flat)


def test_winget_plan_builds_one_command_per_package(force_os):
    force_os("windows", "winget")
    plan = host_tools.plan_install(["mitmproxy", "java17", "openssl"])
    assert plan.manager == "winget"
    assert len(plan.commands) == 3
    assert all(c[0] == "winget" for c in plan.commands)
    assert all("--source" in c and c[c.index("--source") + 1] == "winget" for c in plan.commands)
    assert all("--silent" in c and "--disable-interactivity" in c for c in plan.commands)


def test_sdk_only_missing_is_not_installable_here(force_os):
    force_os("linux", "apt")
    # cmdline-tools / emulator are installed by the app's Android setup, not here.
    plan = host_tools.plan_install(["android-cmdline-tools", "emulator"])
    assert plan.is_empty


def test_no_package_manager_reports_a_hint(force_os):
    force_os("linux", None)
    plan = host_tools.plan_install(["mitmproxy"])
    assert plan.is_empty
    assert "package manager" in plan.note


def test_run_install_rejects_empty_plan():
    with pytest.raises(host_tools.HostToolError):
        host_tools.run_install(host_tools.InstallPlan("apt", tools=[], commands=[]))


def test_run_install_decodes_utf8_and_hides_winget_spinner(monkeypatch):
    class FakeProcess:
        stdout = io.StringIO("████████\nInstalling package\n")

        @staticmethod
        def wait():
            return 0

    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(host_tools.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(host_tools, "_refresh_windows_environment", lambda: None)
    messages = []
    plan = host_tools.InstallPlan("winget", tools=["java17"], commands=[["winget", "install"]])

    host_tools.run_install(plan, on_progress=messages.append)

    assert "Installing package" in messages
    assert not any("█" in message for message in messages)
    assert popen_calls[0][1]["encoding"] == "utf-8"
    assert popen_calls[0][1]["errors"] == "replace"
