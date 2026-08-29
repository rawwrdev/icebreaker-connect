"""The anonymous result channel carries messages without touching disk."""

from __future__ import annotations

import io
import json
import os
import threading

import pytest

import connection_assistant.capture.process as process_module
from connection_assistant.capture.process import CaptureProcess, ResultChannel, open_child_writer


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe path")
def test_child_writer_serializes_over_a_pipe(monkeypatch):
    r, w = os.pipe()
    monkeypatch.setenv("CA_RESULT_FD", str(w))
    writer = open_child_writer()
    writer.send({"type": "progress", "message": "captured auth token"})
    writer.send({"type": "bundle", "bundle": {"auth_token": "fabricated"}})
    writer.close()  # closes w
    with os.fdopen(r, "r", encoding="utf-8") as reader:
        messages = [json.loads(line) for line in reader if line.strip()]
    assert messages[0]["type"] == "progress"
    assert messages[1]["bundle"] == {"auth_token": "fabricated"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe path")
def test_result_channel_parent_reads_child_messages():
    channel = ResultChannel()
    channel.open()
    child_fd = channel._child_fd  # noqa: SLF001 - test seam

    def child() -> None:
        os.write(child_fd, (json.dumps({"type": "bundle", "bundle": {"auth_token": "fab"}}) + "\n").encode())
        os.close(child_fd)

    channel.after_spawn = lambda: None  # keep child fd open for the test writer
    t = threading.Thread(target=child)
    t.start()
    received = list(channel.messages())
    t.join()
    channel.close()
    assert received == [{"type": "bundle", "bundle": {"auth_token": "fab"}}]


def test_null_writer_when_no_channel_configured(monkeypatch):
    monkeypatch.delenv("CA_RESULT_FD", raising=False)
    monkeypatch.delenv("CA_RESULT_ADDR", raising=False)
    monkeypatch.delenv("CA_RESULT_NONCE", raising=False)
    writer = open_child_writer()
    # Must not raise even with nowhere to send.
    writer.send({"type": "progress", "message": "noop"})
    writer.close()


def test_capture_proxy_listens_on_host_loopback_only(monkeypatch):
    calls = []

    class FakeProcess:
        stderr = io.BytesIO(b"fabricated proxy warning\n")

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(CaptureProcess, "_wait_until_listening", lambda self: None)
    capture = CaptureProcess(mitmdump_path="mitmdump", listen_port=8765)

    capture.start()
    capture.stop()

    command = calls[0][0]
    host_index = command.index("--listen-host") + 1
    assert command[host_index] == "127.0.0.1"
    assert calls[0][1]["env"]["RES_OPTIONS"] == "attempts:5 timeout:1"
    assert capture.stderr_tail() == "fabricated proxy warning"
