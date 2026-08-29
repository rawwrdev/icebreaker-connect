"""Owner-only save permissions, URL validation, and error sanitization."""

from __future__ import annotations

import os
import stat

import pytest

from connection_assistant.security.files import (
    InsecureServiceURLError,
    sanitize_text,
    validate_service_url,
    write_owner_only_json,
)


def test_https_url_is_accepted():
    assert validate_service_url("https://api.icebreaker.example") == "https://api.icebreaker.example"


def test_plain_http_remote_is_rejected():
    with pytest.raises(InsecureServiceURLError):
        validate_service_url("http://api.icebreaker.example")


@pytest.mark.parametrize("url", [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
])
def test_localhost_http_is_allowed_for_development(url):
    assert validate_service_url(url) == url


def test_empty_and_schemeless_urls_are_rejected():
    with pytest.raises(InsecureServiceURLError):
        validate_service_url("")
    with pytest.raises(InsecureServiceURLError):
        validate_service_url("api.icebreaker.example")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_saved_json_is_owner_only(tmp_path):
    target = tmp_path / "session-bundle.json"
    written = write_owner_only_json(target, {"auth_token": "fabricated-token"})
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o600  # rw for owner only; no group/other access


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_saved_json_overwrites_looser_existing_file(tmp_path):
    target = tmp_path / "session-bundle.json"
    target.write_text("{}")
    target.chmod(0o644)
    write_owner_only_json(target, {"auth_token": "fabricated-token"})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_sanitize_redacts_jwt_uuid_hex_ip_and_home():
    dirty = (
        "token=eyJhbGciOiJIUzI1NiJ9.eyJhIjoiYiJ9.sig device="
        "0123456789abcdef0123456789abcdef host 192.168.1.42 uuid "
        "12345678-1234-1234-1234-1234567890ab path /home/alice/secret"
    )
    clean = sanitize_text(dirty)
    assert "eyJ" not in clean
    assert "0123456789abcdef" not in clean
    assert "192.168.1.42" not in clean
    assert "12345678-1234-1234-1234-1234567890ab" not in clean
    assert "/home/alice" not in clean
    assert "<redacted-token>" in clean
    assert "<home>" in clean
