"""Pairing lifecycle with a fabricated HTTP transport (no network).

Exercises pending -> approved -> upload, plus expired, rejected, and replay. All
tokens and URLs are fabricated; the transport never leaves the process.
"""

from __future__ import annotations

import json

import httpx
import pytest

from connection_assistant.models import PairingState
from connection_assistant.orchestrator import DEFAULT_SERVICE_URL, AssistantConfig
from connection_assistant.pairing.client import (
    PairingClient,
    PairingClientConfig,
    PairingError,
    PairingExpired,
    PairingRejected,
)
from connection_assistant.security.files import InsecureServiceURLError

BASE = "https://api.icebreaker.example"
UPLOAD_TOKEN = "fabricated-one-time-upload-token"  # noqa: S105 - fabricated
PAIRING_ID = "pair_fabricated_123"
VERIFICATION_URI = f"{BASE}/pair/{PAIRING_ID}?v=ABC123"


class FakeBackend:
    """A tiny in-memory pairing server exercised through httpx.MockTransport."""

    def __init__(self, *, states: list[str], consume: bool = True) -> None:
        self._states = list(states)
        self._consume = consume
        self.consumed = False
        self.uploads: list[dict] = []
        self.captured_auth: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/desktop-pairings":
            return httpx.Response(201, json={
                "pairing_id": PAIRING_ID,
                "verification_uri": VERIFICATION_URI,
                "upload_token": UPLOAD_TOKEN,
                "expires_at": "2099-01-01T00:00:00Z",
                "state": "pending",
            })
        if request.method == "GET" and path == f"/desktop-pairings/{PAIRING_ID}":
            state = self._states.pop(0) if self._states else "approved"
            return httpx.Response(200, json={"pairing_id": PAIRING_ID, "state": state})
        if request.method == "POST" and path == f"/desktop-pairings/{PAIRING_ID}/session":
            self.captured_auth.append(request.headers.get("Authorization"))
            if self.consumed:
                return httpx.Response(409, json={"error": "already consumed"})
            body = json.loads(request.content)
            self.uploads.append(body["session_bundle"])
            if self._consume:
                self.consumed = True
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


def _client(backend: FakeBackend) -> PairingClient:
    transport = httpx.MockTransport(backend.handler)
    return PairingClient(PairingClientConfig(base_url=BASE), transport=transport)


def test_full_pending_then_approved_then_upload():
    backend = FakeBackend(states=["pending", "pending", "approved"])
    with _client(backend) as client:
        request = client.create_pairing(consent_ack=True)
        assert request.pairing_id == PAIRING_ID
        # The secret upload token must NOT be embedded in the QR/verification URI.
        assert UPLOAD_TOKEN not in request.verification_uri
        state = client.wait_for_approval(request.pairing_id, poll_interval=0, timeout=5)
        assert state == PairingState.APPROVED
        client.upload_session(request.pairing_id, request.upload_token, {"auth_token": "fab"})
    assert backend.uploads == [{"auth_token": "fab"}]
    assert backend.captured_auth == [f"Bearer {UPLOAD_TOKEN}"]


def test_expired_pairing_raises():
    backend = FakeBackend(states=["pending", "expired"])
    with _client(backend) as client:
        request = client.create_pairing(consent_ack=True)
        with pytest.raises(PairingExpired):
            client.wait_for_approval(request.pairing_id, poll_interval=0, timeout=5)


def test_rejected_pairing_raises():
    backend = FakeBackend(states=["pending", "rejected"])
    with _client(backend) as client:
        request = client.create_pairing(consent_ack=True)
        with pytest.raises(PairingRejected):
            client.wait_for_approval(request.pairing_id, poll_interval=0, timeout=5)


def test_replay_upload_is_refused():
    backend = FakeBackend(states=["approved"])
    with _client(backend) as client:
        request = client.create_pairing(consent_ack=True)
        client.wait_for_approval(request.pairing_id, poll_interval=0, timeout=5)
        client.upload_session(request.pairing_id, request.upload_token, {"auth_token": "fab"})
        # A second upload with the same one-time token must fail (409 -> error).
        with pytest.raises(PairingError):
            client.upload_session(request.pairing_id, request.upload_token, {"auth_token": "fab"})


def test_create_requires_consent():
    backend = FakeBackend(states=[])
    with _client(backend) as client:
        with pytest.raises(PairingError):
            client.create_pairing(consent_ack=False)


def test_wait_honours_cancellation():
    backend = FakeBackend(states=["pending"] * 10)
    with _client(backend) as client:
        request = client.create_pairing(consent_ack=True)
        with pytest.raises(PairingError):
            client.wait_for_approval(
                request.pairing_id, poll_interval=0, timeout=5, should_cancel=lambda: True
            )


def test_non_https_base_url_is_rejected():
    with pytest.raises(InsecureServiceURLError):
        PairingClient(PairingClientConfig(base_url="http://remote.example"))


def test_assistant_defaults_to_production_pairing_service():
    assert DEFAULT_SERVICE_URL == "https://bot.rawwr.dev"
    assert AssistantConfig().service_url == DEFAULT_SERVICE_URL
    assert AssistantConfig().avd_name == "tinder_cap"
