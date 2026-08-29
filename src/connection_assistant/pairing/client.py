"""HTTPS pairing client: create a pairing, wait for approval, upload the session.

Threat model shaping this client:

  * The **upload token** is the only secret the server hands back. It is returned to
    the desktop over HTTPS and used once as a bearer for the final upload. It is
    NEVER placed in the QR code — the QR carries only ``verification_uri`` (a public
    pairing id + a low-entropy verification handle) which is worthless without the
    approver's authenticated Telegram session.
  * The service URL is validated up-front: HTTPS only, except explicit localhost.
  * Token values (upload token, auth/refresh tokens) are never logged; errors are
    surfaced by status, not by echoing response bodies.

See ``protocol/pairing-api.yaml`` for the wire contract and the note on what the
private Icebreaker backend must implement.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from connection_assistant.models import PairingRequest, PairingState
from connection_assistant.security.files import validate_service_url


class PairingError(RuntimeError):
    """A pairing step failed. Message is safe to display (no secrets)."""


class PairingRejected(PairingError):
    """The approver rejected the pairing in the Telegram Mini App."""


class PairingExpired(PairingError):
    """The pairing window elapsed before it was approved/consumed."""


@dataclass
class PairingClientConfig:
    base_url: str
    timeout: float = 20.0
    # Desktop-declared client label (non-secret) to help the approver recognise it.
    client_label: str = "Icebreaker Connect"


class PairingClient:
    """Thin, synchronous HTTPS client for the desktop pairing flow.

    Synchronous by design: it runs inside the orchestrator's background thread and
    the surrounding code already handles cancellation, so an event loop would add
    nothing. A custom ``transport`` (httpx.MockTransport) is injectable for tests.
    """

    def __init__(
        self,
        config: PairingClientConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = validate_service_url(config.base_url).rstrip("/")
        self._config = config
        self._client = httpx.Client(
            base_url=self._base,
            timeout=config.timeout,
            transport=transport,
            headers={"User-Agent": "IcebreakerConnect/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PairingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- step 1: create --------------------------------------------------- #
    def create_pairing(self, *, consent_ack: bool) -> PairingRequest:
        """POST /desktop-pairings — start a short-lived, user-bound pairing."""
        if not consent_ack:
            raise PairingError("ownership/consent must be acknowledged before pairing")
        resp = self._post(
            "/desktop-pairings",
            json={"client_label": self._config.client_label, "consent_ack": True},
        )
        data = self._json(resp)
        try:
            return PairingRequest(
                pairing_id=str(data["pairing_id"]),
                verification_uri=str(data["verification_uri"]),
                upload_token=str(data["upload_token"]),
                expires_at=data.get("expires_at"),
                state=PairingState.PENDING,
            )
        except (KeyError, TypeError) as exc:
            raise PairingError("pairing service returned an unexpected response") from exc

    # -- step 2: poll ----------------------------------------------------- #
    def get_state(self, pairing_id: str) -> PairingState:
        """GET /desktop-pairings/{id} — current lifecycle state."""
        resp = self._get(f"/desktop-pairings/{pairing_id}")
        data = self._json(resp)
        raw = str(data.get("state", "")).lower()
        try:
            return PairingState(raw)
        except ValueError as exc:
            raise PairingError("pairing service returned an unknown state") from exc

    def wait_for_approval(
        self,
        pairing_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        should_cancel: Callable[[], bool] | None = None,
        on_poll: Callable[[PairingState], None] | None = None,
    ) -> PairingState:
        """Poll until the pairing is approved, or raise on expiry/rejection/cancel."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                raise PairingError("pairing cancelled")
            state = self.get_state(pairing_id)
            if on_poll:
                on_poll(state)
            if state in (PairingState.APPROVED, PairingState.CONSUMED):
                return state
            if state == PairingState.REJECTED:
                raise PairingRejected("the pairing was rejected in Telegram")
            if state == PairingState.EXPIRED:
                raise PairingExpired("the pairing expired before approval")
            time.sleep(poll_interval)
        raise PairingExpired("timed out waiting for pairing approval")

    # -- step 3: upload --------------------------------------------------- #
    def upload_session(
        self, pairing_id: str, upload_token: str, bundle_json: dict
    ) -> None:
        """POST /desktop-pairings/{id}/session — deliver the validated bundle.

        Authorizes with the one-time upload token. On success the server marks the
        pairing ``consumed``; a replayed upload therefore fails closed.
        """
        resp = self._client.post(
            f"/desktop-pairings/{pairing_id}/session",
            json={"session_bundle": bundle_json},
            headers={"Authorization": f"Bearer {upload_token}"},
        )
        if resp.status_code in (401, 403):
            raise PairingError("the pairing authorization was rejected (expired or already used)")
        if resp.status_code == 409:
            raise PairingError("this pairing was already consumed (replay refused)")
        if resp.status_code == 410:
            raise PairingExpired("the pairing expired before upload")
        if resp.status_code >= 400:
            raise PairingError(f"session upload failed (HTTP {resp.status_code})")

    # -- internals -------------------------------------------------------- #
    def _post(self, path: str, *, json: dict) -> httpx.Response:
        try:
            return self._client.post(path, json=json)
        except httpx.HTTPError as exc:
            raise PairingError(f"could not reach the pairing service: {type(exc).__name__}") from exc

    def _get(self, path: str) -> httpx.Response:
        try:
            return self._client.get(path)
        except httpx.HTTPError as exc:
            raise PairingError(f"could not reach the pairing service: {type(exc).__name__}") from exc

    def _json(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            raise PairingError(f"pairing service error (HTTP {resp.status_code})")
        try:
            data = resp.json()
        except ValueError as exc:
            raise PairingError("pairing service returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise PairingError("pairing service returned an unexpected payload")
        return data
