"""In-memory collector that distills a minimal session bundle from live traffic.

The collector is deliberately decoupled from mitmproxy: it consumes plain
``(host, headers, request_body, response_body, path)`` observations, so it can be
unit-tested with fabricated flows and reused by the mitmproxy addon.

What it keeps, and nothing else:
  * ``auth_token``   — first ``X-Auth-Token`` seen on an allowlisted host
  * ``device_id``    — ``persistent-device-id`` header
  * ``install_id``   — ``install-id`` header
  * ``refresh_token``— the durable JWT from a ``/v3/auth`` grant (not a challenge)
  * ``session_profile`` — the allowlisted app/OS identity headers
  * a ``captcha_challenge`` flag when the login was answered with a challenge

It never stores request/response bodies, never logs values, and only ever looks at
hosts on the exact allowlist.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from connection_assistant.capture.protobuf import extract_auth
from connection_assistant.models import (
    SESSION_PROFILE_HEADERS,
    TINDER_API_HOSTS,
    SessionBundle,
    SessionProfile,
)

# Field name -> human label used only for value-free progress events.
_FIELD_LABELS = {
    "auth_token": "auth token",
    "device_id": "device id",
    "install_id": "install id",
    "refresh_token": "refresh token",
    "session_profile": "session profile",
}


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): v for k, v in headers.items()}


class SessionCollector:
    """Accumulates the minimal session bundle from observed authed requests.

    ``on_field`` (optional) is called with a field *name* the first time each field
    is captured, so the UI can show live progress. It is never called with a value.
    """

    def __init__(
        self,
        *,
        hosts: tuple[str, ...] = TINDER_API_HOSTS,
        on_field: Callable[[str], None] | None = None,
    ) -> None:
        self._hosts = tuple(h.lower() for h in hosts)
        self._on_field = on_field
        self._bundle = SessionBundle()

    @property
    def bundle(self) -> SessionBundle:
        return self._bundle

    def _announce(self, field: str) -> None:
        if self._on_field is not None:
            self._on_field(_FIELD_LABELS.get(field, field))

    def _host_allowed(self, host: str) -> bool:
        host = (host or "").lower()
        return host in self._hosts

    def observe(
        self,
        *,
        host: str,
        path: str,
        headers: Mapping[str, str],
        request_body: bytes | None = None,
        response_body: bytes | None = None,
    ) -> None:
        """Feed one request/response observation into the collector.

        Only hosts on the allowlist are inspected. Bodies are examined solely for
        the ``/v3/auth`` refresh JWT and are never retained.
        """
        if not self._host_allowed(host):
            return
        lower = _lower_headers(headers)

        token = lower.get("x-auth-token")
        if token and not self._bundle.auth_token:
            self._bundle.auth_token = token
            self._announce("auth_token")

        # Capture app/OS identity headers only from an authed request, so the
        # profile matches the build the token was authorized under.
        if token and self._bundle.session_profile.is_empty():
            self._collect_profile(lower)

        device_id = lower.get("persistent-device-id")
        if device_id and not self._bundle.device_id:
            self._bundle.device_id = device_id
            self._announce("device_id")

        install_id = lower.get("install-id")
        if install_id and not self._bundle.install_id:
            self._bundle.install_id = install_id
            self._announce("install_id")

        # /v3/auth carries the durable refresh token (or a CAPTCHA challenge).
        base_path = (path or "").split("?", 1)[0]
        if "auth" in base_path:
            self._inspect_auth(request_body, response_body)

    def _collect_profile(self, lower_headers: Mapping[str, str]) -> None:
        profile = SessionProfile()
        found = False
        for header, key in SESSION_PROFILE_HEADERS.items():
            value = lower_headers.get(header)
            if value:
                setattr(profile, key, value[:200])
                found = True
        if found:
            self._bundle.session_profile = profile
            self._announce("session_profile")

    def _inspect_auth(
        self, request_body: bytes | None, response_body: bytes | None
    ) -> None:
        for body in (response_body, request_body):
            if not body:
                continue
            found = extract_auth(body)
            if found.is_challenge and not self._bundle.refresh_token:
                # A challenge is not a credential. Flag it so the bundle is not
                # emitted as usable and the UI can say "verify in Tinder".
                self._bundle.captcha_challenge = True
            if found.refresh_token:
                # A later grant means an earlier challenge was solved during this
                # same capture — clear the sticky flag and keep the real token.
                self._bundle.captcha_challenge = False
                if not self._bundle.refresh_token:
                    self._bundle.refresh_token = found.refresh_token
                    self._announce("refresh_token")
