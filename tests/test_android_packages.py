"""XAPK validation and extraction use only fabricated archives."""

from __future__ import annotations

import json
import os
import urllib.error
import zipfile

import pytest

from connection_assistant.android import packages
from connection_assistant.android.packages import (
    AndroidPackageError,
    extract_xapk,
    validate_xapk,
)


def _write_xapk(path, *, package_name="com.tinder"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"package_name": package_name}))
        archive.writestr("base.apk", b"fabricated-base")
        archive.writestr("config.x86_64.apk", b"fabricated-split")
        archive.writestr(
            "Android/obb/com.tinder/main.123.com.tinder.obb",
            b"fabricated-obb",
        )
        archive.writestr("../../ignored.txt", b"must-not-be-extracted")


def test_tinder_xapk_extracts_only_installable_payloads(tmp_path):
    package = tmp_path / "tinder.xapk"
    destination = tmp_path / "extracted"
    _write_xapk(package)

    validate_xapk(package)
    extracted = extract_xapk(package, destination)

    assert [path.read_bytes() for path in extracted.apk_paths] == [
        b"fabricated-base",
        b"fabricated-split",
    ]
    assert [path.name for path in extracted.obb_paths] == ["main.123.com.tinder.obb"]
    assert (tmp_path / "ignored.txt").exists() is False


def test_xapk_for_another_app_is_rejected(tmp_path):
    package = tmp_path / "other.xapk"
    _write_xapk(package, package_name="com.example.other")

    with pytest.raises(AndroidPackageError, match="not Tinder"):
        validate_xapk(package)


def test_blocked_automatic_download_is_cleaned_up(monkeypatch, tmp_path):
    target = tmp_path / "download.xapk"

    def fake_mkstemp(**_kwargs):
        return os.open(target, os.O_CREAT | os.O_RDWR), str(target)

    def blocked(*_args, **_kwargs):
        raise urllib.error.HTTPError(packages.TINDER_XAPK_URL, 403, "blocked", {}, None)

    monkeypatch.setattr(packages.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(packages.urllib.request, "urlopen", blocked)

    with pytest.raises(AndroidPackageError, match="blocked the automatic download"):
        packages.download_latest_tinder()

    assert target.exists() is False
