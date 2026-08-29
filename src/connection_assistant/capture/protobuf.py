"""Minimal protobuf/JWT parsing for the Tinder ``/v3/auth/login`` auth flow.

This is the *only* logic extracted from the Icebreaker capture tooling, reduced to
what a public onboarding utility needs: pull a refresh token (a JWT) out of an auth
response and tell a real token grant apart from a CAPTCHA / rules-engine challenge.

Nothing here writes files, logs values, or retains traffic. The wire format was
reverse-engineered from a captured exchange; both request and response are protobuf.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """Read a base-128 varint at offset ``i``. Returns (value, next_offset)."""
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Return a JWT's decoded payload dict, or None if it is not a readable JWT.

    Only the payload's *shape* is inspected (below); the value itself is never
    logged or returned to the UI.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def is_captcha_challenge(payload: dict[str, Any]) -> bool:
    """True when a JWT payload is a CAPTCHA/fraud challenge, not a credential.

    Tinder's rules engine answers a flagged login with a JWT that *looks* like a
    token but carries a ``captchaKeyId`` or ``displayReason == RULES_ENGINE`` and
    never a usable refresh token. Detecting it keeps a challenge from ever being
    mistaken for (and stored as) a credential.
    """
    return bool(payload) and (
        "captchaKeyId" in payload or payload.get("displayReason") == "RULES_ENGINE"
    )


def _as_ascii(chunk: bytes) -> str | None:
    """Decode a length-delimited chunk to printable ASCII, or None."""
    try:
        s = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if len(s) >= 2 and all(32 <= ord(c) < 127 for c in s) else None


class AuthExtract:
    """What a best-effort walk of an auth protobuf body found."""

    __slots__ = ("refresh_token", "is_challenge")

    def __init__(self) -> None:
        self.refresh_token: str | None = None
        self.is_challenge: bool = False


def extract_auth(raw: bytes) -> AuthExtract:
    """Best-effort scan of a ``/v3/auth`` protobuf body for a refresh JWT.

    Walks length-delimited fields, recursing into nested messages. A JWT is
    classified as either a CAPTCHA challenge (``is_challenge``) or a usable
    ``refresh_token`` — never both. UUID/user-id/TTL fields the full capture tool
    reads are intentionally dropped: an onboarding bundle does not need them.
    """
    out = AuthExtract()

    def walk(b: bytes) -> None:
        i = 0
        while i < len(b):
            try:
                tag, i = _read_varint(b, i)
            except ValueError:
                return
            wire = tag & 7
            if wire == 0:  # varint
                try:
                    _, i = _read_varint(b, i)
                except ValueError:
                    return
            elif wire == 2:  # length-delimited
                try:
                    length, i = _read_varint(b, i)
                except ValueError:
                    return
                chunk = b[i : i + length]
                if len(chunk) != length:
                    return
                i += length
                s = _as_ascii(chunk)
                if s and s.count(".") == 2 and s.startswith("eyJ"):
                    payload = decode_jwt_payload(s)
                    if payload and is_captcha_challenge(payload):
                        out.is_challenge = True
                    elif out.refresh_token is None:
                        out.refresh_token = s
                elif s is None:
                    walk(chunk)  # nested message
            elif wire == 5:  # fixed32
                i += 4
            elif wire == 1:  # fixed64
                i += 8
            else:
                return

    walk(raw or b"")
    return out
