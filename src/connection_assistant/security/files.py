"""Owner-only file writes, HTTPS URL validation, and error sanitization.

Central home for the security-sensitive primitives so their rules live in one
auditable place:

  * :func:`write_owner_only_json` — JSON written with 0600 permissions where the
    platform supports it, created without a readable window for other users.
  * :func:`validate_service_url` — rejects non-HTTPS service URLs except explicit
    localhost development URLs.
  * :func:`sanitize_text` — strips anything that looks like a credential, an
    absolute local path, or an IP address out of subprocess/exception text before
    it can reach a log or the UI.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Patterns for values we must never surface in logs/UI. These match the *shapes*
# of the secrets this app handles so a stray error string can't leak one.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Long hex blobs (auth tokens, device ids) — 20+ hex chars in a row.
_HEX_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{20,}\b")
# Absolute POSIX home paths and Windows user paths, collapsed to a placeholder.
_POSIX_HOME_RE = re.compile(r"/(?:home|Users)/[^\s/]+")
_WIN_USER_RE = re.compile(r"[A-Za-z]:\\Users\\[^\s\\]+", re.IGNORECASE)


class InsecureServiceURLError(ValueError):
    """Raised when a configured service URL is not HTTPS (and not localhost)."""


def validate_service_url(url: str) -> str:
    """Return ``url`` if it is an acceptable Icebreaker service URL, else raise.

    Only ``https://`` is accepted, with a single exception: an explicit localhost
    URL (any scheme host in {localhost, 127.0.0.1, ::1}) is allowed for local
    development. Everything else — including plain ``http://`` to a remote host —
    is rejected so session credentials never travel in cleartext.
    """
    if not url or not isinstance(url, str):
        raise InsecureServiceURLError("a service URL is required")
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise InsecureServiceURLError(f"not a valid URL: {url!r}")
    host = parsed.hostname
    is_localhost = host in _LOCALHOST_HOSTS
    if not is_localhost:
        try:
            is_localhost = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_localhost = False
    if parsed.scheme == "https":
        return url.strip()
    if parsed.scheme == "http" and is_localhost:
        return url.strip()
    raise InsecureServiceURLError(
        "service URL must use https:// (only explicit localhost may use http://)"
    )


def write_owner_only_json(path: str | os.PathLike[str], data: Any) -> Path:
    """Serialize ``data`` to ``path`` as JSON with owner-only (0600) permissions.

    On POSIX the file is created via ``os.open`` with mode 0600 so there is never a
    window where another user could read it. On Windows, where POSIX mode bits do
    not apply, we write normally and then best-effort restrict the ACL to the
    current user; the file is still created fresh (truncated).
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")

    if os.name == "posix":
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(target, flags, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        # Re-assert perms in case the file pre-existed with a looser mode.
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    else:
        target.write_bytes(payload)
        _restrict_windows_acl(target)
    return target


def _restrict_windows_acl(target: Path) -> None:
    """Best-effort: restrict a file's ACL to the current user on Windows.

    Uses ``icacls`` to remove inheritance and grant only the current user. Any
    failure is swallowed — the fallback is the default user-profile ACL, which is
    still not world-readable on a normal Windows install.
    """
    if sys.platform != "win32":  # pragma: no cover - platform guard
        return
    import subprocess  # local import; only needed on Windows

    user = os.environ.get("USERNAME")
    if not user:
        return
    try:  # pragma: no cover - exercised only on Windows
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def sanitize_text(text: str, *, max_len: int = 600) -> str:
    """Redact credential/path/IP shapes from arbitrary text before display/log.

    Applied to every subprocess stderr and exception message that can reach the
    user. It replaces JWTs, UUIDs, long hex blobs, IPv4 addresses and absolute
    home paths with fixed placeholders, then truncates. It is intentionally
    aggressive: over-redaction is safe, leaking is not.
    """
    if not text:
        return ""
    redacted = _JWT_RE.sub("<redacted-token>", text)
    redacted = _UUID_RE.sub("<redacted-id>", redacted)
    redacted = _HEX_BLOB_RE.sub("<redacted-id>", redacted)
    redacted = _IPV4_RE.sub("<redacted-ip>", redacted)
    redacted = _POSIX_HOME_RE.sub("<home>", redacted)
    redacted = _WIN_USER_RE.sub("<home>", redacted)
    redacted = " ".join(redacted.split())
    if len(redacted) > max_len:
        redacted = redacted[:max_len] + "…"
    return redacted
