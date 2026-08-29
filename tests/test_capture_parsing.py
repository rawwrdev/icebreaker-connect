"""Capture parsing and CAPTCHA detection with fabricated flows."""

from __future__ import annotations

from connection_assistant.capture.collector import SessionCollector
from connection_assistant.capture.protobuf import extract_auth, is_captcha_challenge


def test_extracts_refresh_token_from_auth_response(fabricated_auth_response):
    result = extract_auth(fabricated_auth_response)
    assert result.refresh_token is not None
    assert result.refresh_token.startswith("eyJ")
    assert result.is_challenge is False


def test_captcha_response_is_flagged_not_treated_as_token(fabricated_captcha_response):
    result = extract_auth(fabricated_captcha_response)
    assert result.is_challenge is True
    assert result.refresh_token is None


def test_is_captcha_challenge_detects_rules_engine():
    assert is_captcha_challenge({"displayReason": "RULES_ENGINE"}) is True
    assert is_captcha_challenge({"captchaKeyId": "abc"}) is True
    assert is_captcha_challenge({"refreshToken": "x"}) is False


def test_empty_or_garbage_body_is_safe():
    assert extract_auth(b"").refresh_token is None
    assert extract_auth(b"\x00\x01not-protobuf").is_challenge is False


def test_collector_captures_minimal_bundle_from_allowlisted_host(fabricated_auth_response):
    seen: list[str] = []
    collector = SessionCollector(on_field=seen.append)
    collector.observe(
        host="api.gotinder.com",
        path="/v2/profile",
        headers={
            "X-Auth-Token": "fabricated-auth-token",
            "persistent-device-id": "fabricated-device",
            "install-id": "fabricated-install",
            "user-agent": "Tinder Android Version 99.9.9",
            "app-version": "9999",
            "tinder-version": "99.9.9",
            "platform": "android",
            "os-version": "30",
        },
    )
    collector.observe(
        host="api.gotinder.com",
        path="/v3/auth/login",
        headers={"X-Auth-Token": "fabricated-auth-token"},
        response_body=fabricated_auth_response,
    )
    bundle = collector.bundle
    assert bundle.has_usable_session() is True
    presence = bundle.field_presence()
    assert presence == {
        "auth_token": True,
        "refresh_token": True,
        "device_id": True,
        "install_id": True,
        "session_profile": True,
    }
    # Progress callbacks carry field labels, never values.
    assert "auth token" in seen
    assert not any("fabricated" in label for label in seen)


def test_collector_ignores_non_allowlisted_hosts():
    collector = SessionCollector()
    collector.observe(
        host="evil.example.com",
        path="/v2/profile",
        headers={"X-Auth-Token": "should-be-ignored"},
    )
    assert collector.bundle.auth_token is None
    assert collector.bundle.has_usable_session() is False


def test_captcha_only_capture_is_not_usable(fabricated_captcha_response):
    collector = SessionCollector()
    collector.observe(
        host="api.gotinder.com",
        path="/v2/profile",
        headers={"X-Auth-Token": "fabricated-auth-token"},
    )
    collector.observe(
        host="api.gotinder.com",
        path="/v3/auth/login",
        headers={"X-Auth-Token": "fabricated-auth-token"},
        response_body=fabricated_captcha_response,
    )
    bundle = collector.bundle
    assert bundle.captcha_challenge is True
    assert bundle.has_usable_session() is False


def test_solving_captcha_after_challenge_clears_flag(
    fabricated_captcha_response, fabricated_auth_response
):
    collector = SessionCollector()
    collector.observe(
        host="api.gotinder.com",
        path="/v2/profile",
        headers={"X-Auth-Token": "fabricated-auth-token"},
    )
    collector.observe(
        host="api.gotinder.com",
        path="/v3/auth/login",
        headers={"X-Auth-Token": "fabricated-auth-token"},
        response_body=fabricated_captcha_response,
    )
    assert collector.bundle.captcha_challenge is True
    # A later successful grant clears the sticky challenge flag.
    collector.observe(
        host="api.gotinder.com",
        path="/v3/auth/login",
        headers={"X-Auth-Token": "fabricated-auth-token"},
        response_body=fabricated_auth_response,
    )
    assert collector.bundle.captcha_challenge is False
    assert collector.bundle.has_usable_session() is True
