"""mitmproxy addon: distill a minimal session bundle and stream it to the parent.

Launched by the capture process as ``mitmdump -s mitm_addon.py``. Unlike the
reference capture tool, this addon writes **no files**: it never saves a traffic
dump, an ``.env``, refresh-request bytes, or a provider response body. Captured
fields flow back to the desktop app over an anonymous result channel (a POSIX pipe,
or a loopback socket on Windows) — see :mod:`connection_assistant.capture.process`.

Progress messages carry only field *names*; the final ``bundle`` message carries the
values, and it travels solely over the private channel to the parent process — never
to disk, a log, or stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow ``mitmdump -s <this file>`` to import the package even when the addon is
# run as a bare script path (src layout: this file is src/connection_assistant/
# capture/mitm_addon.py, so the src root is three parents up).
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from connection_assistant.capture.collector import SessionCollector  # noqa: E402
from connection_assistant.capture.process import open_child_writer  # noqa: E402


class _Addon:
    def __init__(self) -> None:
        self._writer = open_child_writer()
        self._collector = SessionCollector(on_field=self._on_field)
        self._prev_presence = self._collector.bundle.field_presence()

    def _on_field(self, label: str) -> None:
        # Value-free: only the human label of the field that was just captured.
        self._writer.send(
            {"type": "progress", "message": f"captured {label}"}
        )

    # mitmproxy hook: called once a full response is available.
    def response(self, flow) -> None:  # noqa: ANN001 - mitmproxy passes HTTPFlow
        try:
            self._collector.observe(
                host=flow.request.pretty_host,
                path=flow.request.path,
                headers=flow.request.headers,
                request_body=flow.request.raw_content,
                response_body=flow.response.raw_content if flow.response else None,
            )
        except Exception:
            # Never let a malformed flow crash the proxy or leak details.
            return
        self._emit_challenge_if_new()

    def _emit_challenge_if_new(self) -> None:
        bundle = self._collector.bundle
        if bundle.captcha_challenge:
            self._writer.send(
                {
                    "type": "progress",
                    "level": "warn",
                    "message": (
                        "Tinder returned a verification challenge — solve the "
                        "CAPTCHA in the app; no token was issued yet"
                    ),
                }
            )

    # mitmproxy hook: called on clean shutdown (Ctrl-C / proxy stop).
    def done(self) -> None:
        bundle = self._collector.bundle
        self._writer.send({"type": "bundle", "bundle": bundle.to_bundle_json(),
                           "captcha_challenge": bundle.captcha_challenge,
                           "usable": bundle.has_usable_session()})
        self._writer.close()


addons = [_Addon()]
