"""Capture process manager + the anonymous result channel it uses.

The desktop app never asks the capture subprocess to write credentials to a file.
Instead it opens an *anonymous* channel and passes only a handle to the child:

  * POSIX: an ``os.pipe()`` whose write end is inherited by the child (``pass_fds``).
    The fd number is handed over in ``CA_RESULT_FD``. Nothing touches the disk.
  * Windows: a loopback ``127.0.0.1`` socket bound to an ephemeral port, guarded by
    a one-time random nonce (``CA_RESULT_ADDR`` / ``CA_RESULT_NONCE``). Still no file.

Messages are newline-delimited JSON. Progress messages are value-free; the single
``bundle`` message carries the captured values and exists only in memory on both ends.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from connection_assistant.security.files import sanitize_text

_ADDON_PATH = Path(__file__).resolve().parent / "mitm_addon.py"
_SRC_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOST = "127.0.0.1"


# --------------------------------------------------------------------------- #
# Child side (imported inside the mitmproxy addon)
# --------------------------------------------------------------------------- #
class _ChildWriter:
    """Serializes JSON messages back to the parent over the channel."""

    def __init__(self, send_bytes, close_fn) -> None:  # noqa: ANN001
        self._send_bytes = send_bytes
        self._close_fn = close_fn
        self._lock = threading.Lock()
        self._closed = False

    def send(self, message: dict[str, Any]) -> None:
        try:
            data = (json.dumps(message) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._closed:
                return
            try:
                self._send_bytes(data)
            except OSError:
                self._closed = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._close_fn()
        except OSError:
            pass


class _NullWriter:
    """No-op writer used when the addon runs without a configured channel."""

    def send(self, message: dict[str, Any]) -> None:  # noqa: D401
        return

    def close(self) -> None:
        return


def open_child_writer() -> _ChildWriter | _NullWriter:
    """Called by the addon to obtain the writer end of the parent's channel."""
    fd_env = os.environ.get("CA_RESULT_FD")
    if fd_env:
        fd = int(fd_env)

        def _send(data: bytes) -> None:
            os.write(fd, data)

        def _close() -> None:
            os.close(fd)

        return _ChildWriter(_send, _close)

    addr = os.environ.get("CA_RESULT_ADDR")
    nonce = os.environ.get("CA_RESULT_NONCE")
    if addr and nonce:
        host, _, port = addr.rpartition(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall((nonce + "\n").encode("utf-8"))

        return _ChildWriter(sock.sendall, sock.close)

    return _NullWriter()


# --------------------------------------------------------------------------- #
# Parent side
# --------------------------------------------------------------------------- #
class ResultChannel:
    """Parent end of the anonymous capture channel."""

    def __init__(self) -> None:
        self._use_socket = os.name != "posix"
        self._read_fd: int | None = None
        self._child_fd: int | None = None
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._env: dict[str, str] = {}
        self._pass_fds: tuple[int, ...] = ()

    def open(self) -> None:
        if self._use_socket:
            self._open_socket()
        else:
            self._open_pipe()

    def _open_pipe(self) -> None:
        r, w = os.pipe()
        os.set_inheritable(w, True)
        self._read_fd, self._child_fd = r, w
        self._env = {"CA_RESULT_FD": str(w)}
        self._pass_fds = (w,)

    def _open_socket(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        self._listener = listener
        self._nonce = secrets.token_urlsafe(24)
        self._env = {"CA_RESULT_ADDR": f"{host}:{port}", "CA_RESULT_NONCE": self._nonce}

    @property
    def child_env(self) -> dict[str, str]:
        return dict(self._env)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return self._pass_fds

    def after_spawn(self) -> None:
        """Close the child's fd copy in the parent so EOF propagates on exit."""
        if self._child_fd is not None:
            os.close(self._child_fd)
            self._child_fd = None

    def messages(self) -> Iterator[dict[str, Any]]:
        """Yield decoded JSON messages until the channel closes (blocking)."""
        reader = self._make_reader()
        if reader is None:
            return
        with reader:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, TypeError):
                    continue

    def _make_reader(self):  # noqa: ANN202
        if not self._use_socket:
            return os.fdopen(self._read_fd, "r", encoding="utf-8")  # type: ignore[arg-type]
        # Socket: accept one connection, verify the nonce, then read lines.
        assert self._listener is not None
        self._listener.settimeout(30)
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return None
        self._conn = conn
        stream = conn.makefile("r", encoding="utf-8")
        first = stream.readline().strip()
        if first != self._nonce:
            stream.close()
            conn.close()
            return None
        return stream

    def close(self) -> None:
        for closer in (self._listener, self._conn):
            if closer is not None:
                try:
                    closer.close()
                except OSError:
                    pass
        if self._read_fd is not None:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            self._read_fd = None


class CaptureProcess:
    """Runs ``mitmdump`` with the capture addon and relays result-channel messages.

    Ownership rule: whoever constructs this must call :meth:`stop` on *every* exit
    path (success, cancel, error). The orchestrator guarantees that.
    """

    def __init__(
        self,
        *,
        mitmdump_path: str = "mitmdump",
        listen_host: str = LOOPBACK_HOST,
        listen_port: int = 8080,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._mitmdump = mitmdump_path
        self._host = listen_host
        self._port = listen_port
        self._extra_env = extra_env or {}
        self._proc: subprocess.Popen | None = None
        self._channel = ResultChannel()
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._stderr_lock = threading.Lock()

    @property
    def listen_port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        self._channel.open()
        env = os.environ.copy()
        # Ensure the addon can import the package.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_SRC_ROOT}{os.pathsep}{existing}" if existing else str(_SRC_ROOT)
        )
        env.update(self._channel.child_env)
        env.update(self._extra_env)
        # The newer Tinder client opens many hosts concurrently. Give transient
        # resolver failures a few quick retries instead of failing the request.
        env.setdefault("RES_OPTIONS", "attempts:5 timeout:1")
        cmd = [
            self._mitmdump,
            "--listen-host",
            self._host,
            "--listen-port",
            str(self._port),
            "-q",
            "-s",
            str(_ADDON_PATH),
        ]
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=self._channel.pass_fds,
        )
        self._channel.after_spawn()
        if self._proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                name="capture-stderr",
                daemon=True,
            )
            self._stderr_thread.start()
        self._wait_until_listening()

    def _drain_stderr(self) -> None:
        """Continuously drain mitmdump stderr into a small sanitized ring buffer.

        Leaving a subprocess PIPE unread eventually fills the kernel buffer and
        blocks the proxy. That presents inside Android as a total loss of internet.
        """
        proc = self._proc
        stream = proc.stderr if proc is not None else None
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, b""):
                line = sanitize_text(raw_line.decode("utf-8", errors="replace"), max_len=300)
                if line:
                    with self._stderr_lock:
                        self._stderr_lines.append(line)
        except (OSError, ValueError):
            pass

    def _wait_until_listening(self, timeout: float = 8.0) -> None:
        """Refuse to proxy the emulator unless this mitmdump owns a live listener."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                detail = self.stderr_tail()
                self.stop()
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"capture proxy exited during startup{suffix}")
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.25):
                    return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"capture proxy did not listen on port {self._port} within {timeout:g}s")

    def messages(self) -> Iterator[dict[str, Any]]:
        """Iterate result-channel messages emitted by the addon."""
        yield from self._channel.messages()

    def stop(self, timeout: float = 8.0) -> None:
        """Terminate mitmdump so the addon's ``done`` hook flushes the bundle."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # SIGTERM lets mitmproxy shut down cleanly and run addon.done().
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        self._channel.close()
        stderr_thread = self._stderr_thread
        if stderr_thread is not None and stderr_thread is not threading.current_thread():
            stderr_thread.join(timeout=2)
        self._stderr_thread = None
        self._proc = None

    def stderr_tail(self) -> str:
        """Sanitized stderr, for surfacing a launch failure without leaking paths."""
        stderr_thread = self._stderr_thread
        if stderr_thread is not None and not self.running:
            stderr_thread.join(timeout=1)
        with self._stderr_lock:
            return sanitize_text(" ".join(self._stderr_lines))


def python_can_import_mitmproxy() -> bool:
    """Smoke helper: is a usable mitmdump importable in this interpreter?"""
    try:
        import mitmproxy  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":  # pragma: no cover - manual debug aid
    print(f"addon: {_ADDON_PATH}")
    print(f"src root: {_SRC_ROOT}")
    print(f"python: {sys.version.split()[0]}")
