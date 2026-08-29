"""Shared subprocess helper for the Android layer.

Every command runs through :func:`run` so error text is uniformly sanitized before
it can reach a log or the UI. We never raise with raw stderr attached.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from connection_assistant.security.files import sanitize_text


class CommandError(RuntimeError):
    """A subprocess failed. ``message`` is already sanitized and safe to display."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: list[str],
    *,
    timeout: float = 120.0,
    input_text: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run ``args`` and return a :class:`CommandResult` with sanitized output.

    Raises :class:`CommandError` (sanitized) on non-zero exit only when ``check``.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"command not found: {sanitize_text(str(exc))}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"command timed out after {timeout:.0f}s: {args[0]}"
        ) from exc
    result = CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=sanitize_text(proc.stderr or ""),
    )
    if check and not result.ok:
        detail = result.stderr or sanitize_text(result.stdout)
        raise CommandError(
            f"{args[0]} failed (exit {proc.returncode}): {detail}",
            returncode=proc.returncode,
        )
    return result
