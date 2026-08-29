"""Shared fabricated fixtures.

Every credential-shaped value used in tests is synthetic. No captured session
material, real token, private URL, or IP address from any account appears here.
"""

from __future__ import annotations

import base64
import json

import pytest

# --- fabricated protobuf builders (mirror the on-wire /v3/auth layout) ------- #


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def len_delimited(field: int, payload: bytes) -> bytes:
    tag = _encode_varint((field << 3) | 2)
    return tag + _encode_varint(len(payload)) + payload


def fake_jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return "eyJhbGciOiJIUzI1NiJ9." + body + ".sig"


@pytest.fixture
def fabricated_refresh_jwt() -> str:
    # A normal (non-challenge) refresh JWT.
    return fake_jwt({"refreshToken": "x", "userId": "000000000000000000000001"})


@pytest.fixture
def fabricated_captcha_jwt() -> str:
    return fake_jwt(
        {"displayReason": "RULES_ENGINE", "captchaKeyId": "TEST-KEY", "refreshToken": ""}
    )


@pytest.fixture
def fabricated_auth_response(fabricated_refresh_jwt: str) -> bytes:
    # AuthGatewayResponse{ field 8 = { 1: <refresh jwt> } }.
    inner = len_delimited(1, fabricated_refresh_jwt.encode())
    return len_delimited(8, inner)


@pytest.fixture
def fabricated_captcha_response(fabricated_captcha_jwt: str) -> bytes:
    inner = len_delimited(1, fabricated_captcha_jwt.encode())
    return len_delimited(8, inner)
