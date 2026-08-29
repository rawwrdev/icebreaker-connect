"""Regression tests for the self-bootstrapping build entry point."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from scripts import build


def test_clean_never_bootstraps(monkeypatch):
    monkeypatch.setattr(
        build,
        "_in_virtualenv",
        lambda: (_ for _ in ()).throw(AssertionError("clean checked the interpreter")),
    )

    assert build._bootstrap("clean") is None


def test_active_virtualenv_is_used_directly(monkeypatch):
    monkeypatch.setattr(build, "_in_virtualenv", lambda: True)

    assert build._bootstrap("app") is None


def test_system_python_relaunches_with_project_venv(monkeypatch, tmp_path: Path):
    venv = tmp_path / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    calls: list[tuple[list[str], Path, dict[str, str], bool]] = []

    def fake_run(args, *, cwd, env, check):
        calls.append((args, cwd, env, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build, "VENV", venv)
    monkeypatch.setattr(build, "_in_virtualenv", lambda: False)
    monkeypatch.setattr(build.subprocess, "run", fake_run)

    assert build._bootstrap("app") == 0
    assert calls[0][0][0] == str(python)
    assert "setuptools>=68" in calls[0][0]
    assert "wheel" in calls[0][0]
    assert calls[0][3] is True
    assert calls[1][0][0] == str(python)
    assert calls[1][0][-1] == "app"
    assert calls[1][1] == build.ROOT
    assert calls[1][2]["PIP_NO_INPUT"] == "1"
    assert calls[1][3] is False


def test_dependency_install_is_noninteractive_and_uses_existing_environment(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "_run", calls.append)

    build._install_build_dependencies()

    assert "--no-input" in calls[0]
    assert "--no-build-isolation" not in calls[0]
    assert ".[build]" in calls[0]


def test_app_build_includes_capture_addon_as_a_real_file(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(build, "_run", calls.append)

    build.build_app()

    command = calls[0]
    addon = build.ROOT / "src" / "connection_assistant" / "capture" / "mitm_addon.py"
    data_sep = ";" if os.name == "nt" else ":"
    assert f"{addon}{data_sep}connection_assistant/capture" in command
