"""Core data models for the connection assistant.

These are deliberately small, JSON-serialisable dataclasses. The one rule that
shapes everything here: token *values* live only inside :class:`SessionBundle`
and are never included in any progress event, log line, or repr that the UI or
logging layer might display. See :meth:`SessionBundle.field_presence` for the
"which fields were captured, without their values" view the UI renders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

# The exact hosts we intercept. Anything else the emulator talks to is ignored by
# the collector so we never parse or retain unrelated traffic.
TINDER_API_HOSTS: tuple[str, ...] = ("api.gotinder.com",)

# request header -> session_profile key. Only these stable app/OS identity headers
# are ever captured; nothing dynamic or per-request is kept. Mirrors the allowlist
# the Icebreaker provider replays, so a captured bundle stays consistent with the
# build the token was authorized under.
SESSION_PROFILE_HEADERS: dict[str, str] = {
    "user-agent": "user_agent",
    "app-version": "app_version",
    "tinder-version": "tinder_version",
    "platform": "platform",
    "platform-variant": "platform_variant",
    "store-variant": "store_variant",
    "os-version": "os_version",
}

# The complete set of secret-bearing field names in a session bundle. Used by the
# sanitizer and the presence view so nothing outside this set is ever treated as
# a credential and these are never rendered by value.
SECRET_FIELDS: frozenset[str] = frozenset({"auth_token", "refresh_token"})


class Stage(StrEnum):
    """High-level onboarding stages, mirrored by the guided UI flow."""

    CONSENT = "consent"
    PAIRING = "pairing"
    ENVIRONMENT = "environment"
    APK = "apk"
    EMULATOR = "emulator"
    CAPTURE = "capture"
    REVIEW = "review"
    DELIVER = "deliver"
    RESULT = "result"


class ProgressEvent:
    """A sanitized, sensitive-data-free progress signal emitted by background work.

    Constructed only from operational text (stage, human message, optional 0..1
    fraction). Callers must never pass token values or raw provider bodies here;
    the capture layer emits *field names*, never values.
    """

    __slots__ = ("stage", "message", "fraction", "level")

    def __init__(
        self,
        stage: Stage | str,
        message: str,
        *,
        fraction: float | None = None,
        level: str = "info",
    ) -> None:
        self.stage = stage.value if isinstance(stage, Stage) else str(stage)
        self.message = message
        self.fraction = fraction
        self.level = level

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "fraction": self.fraction,
            "level": self.level,
        }


@dataclass
class SessionProfile:
    """Allowlisted app/OS identity headers of the captured session."""

    user_agent: str | None = None
    app_version: str | None = None
    tinder_version: str | None = None
    platform: str | None = None
    platform_variant: str | None = None
    store_variant: str | None = None
    os_version: str | None = None

    def is_empty(self) -> bool:
        return not any(v for v in asdict(self).values())

    def to_json(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class SessionBundle:
    """The minimal captured session. Kept in memory by default.

    ``auth_token`` and ``refresh_token`` are the only secrets. Everything else is a
    stable, non-secret identity value. Never log or render this dataclass directly;
    use :meth:`field_presence` to show *which* fields were captured.
    """

    auth_token: str | None = None
    refresh_token: str | None = None
    device_id: str | None = None
    install_id: str | None = None
    session_profile: SessionProfile = field(default_factory=SessionProfile)
    # Set when Tinder answered the login with a CAPTCHA/rules-engine challenge
    # instead of a token grant. A challenge is NOT a credential.
    captcha_challenge: bool = False

    def has_usable_session(self) -> bool:
        """A bundle is usable only when a real auth token was captured and no
        unresolved CAPTCHA challenge is outstanding."""
        return bool(self.auth_token) and not self.captcha_challenge

    def field_presence(self) -> dict[str, bool]:
        """Which fields were captured, WITHOUT their values — the exact view the
        UI renders as a summary."""
        return {
            "auth_token": bool(self.auth_token),
            "refresh_token": bool(self.refresh_token),
            "device_id": bool(self.device_id),
            "install_id": bool(self.install_id),
            "session_profile": not self.session_profile.is_empty(),
        }

    def to_bundle_json(self) -> dict[str, Any]:
        """The session-bundle wire/export form (schema: protocol/session-bundle.schema.json).

        Omits null optionals and an empty profile so the artifact stays minimal.
        """
        out: dict[str, Any] = {"auth_token": self.auth_token}
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        if self.device_id:
            out["device_id"] = self.device_id
        if self.install_id:
            out["install_id"] = self.install_id
        if not self.session_profile.is_empty():
            out["session_profile"] = self.session_profile.to_json()
        return out

    @classmethod
    def from_bundle_json(cls, data: dict[str, Any]) -> SessionBundle:
        prof = data.get("session_profile") or {}
        allowed = set(SESSION_PROFILE_HEADERS.values())
        profile = SessionProfile(
            **{k: v for k, v in prof.items() if k in allowed and isinstance(v, str)}
        )
        return cls(
            auth_token=data.get("auth_token"),
            refresh_token=data.get("refresh_token"),
            device_id=data.get("device_id"),
            install_id=data.get("install_id"),
            session_profile=profile,
            captcha_challenge=bool(data.get("captcha_challenge", False)),
        )


class PairingState(StrEnum):
    """Lifecycle of a desktop pairing request, per protocol/pairing-api.yaml."""

    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass
class PairingRequest:
    """A short-lived pairing request returned by ``POST /desktop-pairings``.

    ``verification_uri`` is what the desktop shows/QR-encodes for the Telegram Mini
    App to confirm. ``upload_token`` is the one-time bearer used only for the final
    session upload; it is a secret and must never be placed in the QR code.
    """

    pairing_id: str
    verification_uri: str
    upload_token: str
    expires_at: str | None = None
    state: PairingState = PairingState.PENDING


@dataclass
class DeliveryResult:
    """Outcome of a send-securely or save-locally action, safe to display."""

    ok: bool
    mode: str  # "paired" | "saved" | "cancelled"
    detail: str = ""
    saved_path: str | None = None
