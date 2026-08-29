"""Cleanup contract: proxy cleared, processes stopped, bundle wiped on every exit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import connection_assistant.orchestrator as orchestrator_module
from connection_assistant.models import PairingRequest, PairingState, SessionBundle, SessionProfile
from connection_assistant.orchestrator import AssistantConfig, Orchestrator


class FakeController:
    def __init__(self) -> None:
        self.proxy_cleared = 0

    def clear_proxy(self) -> None:
        self.proxy_cleared += 1


class FakeEmulator:
    def __init__(self, *, raise_on_stop: bool = False) -> None:
        self.stopped = 0
        self._raise = raise_on_stop

    def stop(self) -> None:
        self.stopped += 1
        if self._raise:
            raise RuntimeError("boom")


class FakeCapture:
    def __init__(self, *, raise_on_stop: bool = False) -> None:
        self.stopped = 0
        self._raise = raise_on_stop

    def stop(self, *args, **kwargs) -> None:
        self.stopped += 1
        if self._raise:
            raise RuntimeError("boom")


def _orch_with_active_run(**kwargs) -> tuple[Orchestrator, FakeController, FakeEmulator, FakeCapture]:
    orch = Orchestrator(AssistantConfig())
    controller = FakeController()
    emulator = FakeEmulator(raise_on_stop=kwargs.get("emu_raise", False))
    capture = FakeCapture(raise_on_stop=kwargs.get("cap_raise", False))
    st = orch._state  # noqa: SLF001 - test seam
    st.controller = controller
    st.emulator = emulator
    st.started_emulator = True
    st.capture = capture
    st.proxy_set = True
    st.bundle = SessionBundle(
        auth_token="fabricated-token",
        session_profile=SessionProfile(app_version="9999"),
    )
    return orch, controller, emulator, capture


def test_cleanup_clears_proxy_stops_processes_and_wipes_bundle():
    orch, controller, emulator, capture = _orch_with_active_run()
    orch.cleanup()
    assert controller.proxy_cleared == 1
    assert capture.stopped == 1
    assert emulator.stopped == 1
    assert orch.bundle.has_usable_session() is False
    assert orch.bundle.auth_token is None


def test_cleanup_is_resilient_to_capture_stop_exception():
    orch, controller, emulator, _capture = _orch_with_active_run(cap_raise=True)
    orch.cleanup()  # must not raise
    # Even though capture.stop raised, proxy is still cleared and emulator stopped.
    assert controller.proxy_cleared == 1
    assert emulator.stopped == 1
    assert orch.bundle.auth_token is None


def test_cleanup_is_resilient_to_emulator_stop_exception():
    orch, controller, _emulator, capture = _orch_with_active_run(emu_raise=True)
    orch.cleanup()  # must not raise
    assert controller.proxy_cleared == 1
    assert capture.stopped == 1
    assert orch.bundle.auth_token is None


def test_cleanup_is_idempotent():
    orch, controller, emulator, capture = _orch_with_active_run()
    orch.cleanup()
    orch.cleanup()  # second call is a no-op, never raises
    assert controller.proxy_cleared == 1
    assert emulator.stopped == 1


def test_cancel_then_cleanup_after_started_run():
    orch, controller, emulator, capture = _orch_with_active_run()
    orch.cancel()
    assert orch.is_cancelled() is True
    orch.cleanup()
    assert controller.proxy_cleared == 1


def test_does_not_stop_emulator_it_did_not_start():
    orch, controller, emulator, _capture = _orch_with_active_run()
    orch._state.started_emulator = False  # noqa: SLF001
    orch.cleanup()
    assert emulator.stopped == 0  # we must not kill a user's pre-existing emulator
    assert controller.proxy_cleared == 1


def test_stop_emulator_preserves_bundle_and_stops_owned_process():
    orch, controller, emulator, capture = _orch_with_active_run()
    bundle = orch.stop_emulator()
    assert controller.proxy_cleared == 1
    assert capture.stopped == 1
    assert emulator.stopped == 1
    assert bundle.has_usable_session() is True
    assert orch.capture_active is False
    assert orch.emulator_started is False


def test_stop_emulator_leaves_preexisting_emulator_running():
    orch, controller, emulator, capture = _orch_with_active_run()
    orch._state.started_emulator = False  # noqa: SLF001
    bundle = orch.stop_emulator()
    assert controller.proxy_cleared == 1
    assert capture.stopped == 1
    assert emulator.stopped == 0
    assert bundle.has_usable_session() is True


def test_combined_start_captures_before_proxying_and_launching(monkeypatch):
    orch = Orchestrator(AssistantConfig())
    calls = []
    monkeypatch.setattr(orch, "start_emulator", lambda: calls.append("emulator"))
    monkeypatch.setattr(
        orch,
        "prepare_capture",
        lambda apk_path, *, launch_app: calls.append(("prepare", apk_path, launch_app)),
    )
    monkeypatch.setattr(orch, "start_capture", lambda: calls.append("capture"))
    monkeypatch.setattr(
        orch,
        "_configure_proxy_and_launch",
        lambda: calls.append("proxy-and-launch"),
    )

    orch.start_emulator_and_capture("/tmp/fabricated.apk")

    assert calls == [
        "emulator",
        ("prepare", "/tmp/fabricated.apk", False),
        "capture",
        "proxy-and-launch",
    ]


def test_proxy_uses_private_adb_loopback_tunnel():
    orch = Orchestrator(AssistantConfig(mitm_port=8765))
    calls = []

    class ProxyController:
        def __init__(self):
            self.proxy = None
            self.launched = False

        def set_loopback_proxy(self, port):
            self.proxy = port

        def force_stop_app(self):
            calls.append("force-stop")

        def launch_app(self):
            self.launched = True

    selected = ProxyController()
    orch._state.controller = selected  # noqa: SLF001

    orch._configure_proxy_and_launch()  # noqa: SLF001

    assert selected.proxy == 8765
    assert selected.launched is True
    assert calls == ["force-stop"]


def test_unexpected_capture_exit_restores_direct_internet(monkeypatch):
    events = []
    orch = Orchestrator(AssistantConfig(), on_progress=events.append)
    controller = FakeController()
    orch._state.controller = controller  # noqa: SLF001
    orch._state.proxy_set = True  # noqa: SLF001
    orch._state.toolchain = SimpleNamespace(mitmdump="mitmdump")  # noqa: SLF001

    class EndingCapture:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def messages(self):
            return iter(())

        def stderr_tail(self):
            return "fabricated proxy failure"

    monkeypatch.setattr(orchestrator_module, "CaptureProcess", EndingCapture)

    orch.start_capture()
    orch._capture_thread.join(timeout=2)  # noqa: SLF001

    assert orch.capture_active is False
    assert controller.proxy_cleared == 1
    assert any(
        event.level == "error" and "proxy was removed" in event.message
        for event in events
    )


def test_context_manager_cleans_up_on_exit():
    orch, controller, emulator, capture = _orch_with_active_run()
    with orch:
        pass
    assert controller.proxy_cleared == 1
    assert capture.stopped == 1


def test_save_locally_requires_usable_session(tmp_path):
    orch = Orchestrator(AssistantConfig())
    result = orch.save_locally(tmp_path / "x.json")
    assert result.ok is False


def test_save_locally_writes_owner_only(tmp_path):
    orch = Orchestrator(AssistantConfig())
    orch._state.bundle = SessionBundle(auth_token="fabricated-token")  # noqa: SLF001
    result = orch.save_locally(tmp_path / "session-bundle.json")
    assert result.ok is True
    assert result.saved_path is not None
    import json

    data = json.loads((tmp_path / "session-bundle.json").read_text())
    assert data == {"auth_token": "fabricated-token"}


def test_create_emulator_requires_license_acceptance(monkeypatch):
    orch = Orchestrator(AssistantConfig(licenses_accepted=False))
    monkeypatch.setattr(orch, "list_emulators", lambda: [])

    with pytest.raises(RuntimeError, match="license"):
        orch.create_emulator("new_capture")


def test_create_emulator_uses_validated_name_and_selects_it(monkeypatch):
    orch = Orchestrator(AssistantConfig(licenses_accepted=True))
    inventories = iter([[], ["new_capture"]])
    monkeypatch.setattr(orch, "list_emulators", lambda: next(inventories))
    monkeypatch.setattr(orch, "setup_environment", lambda: None)

    assert orch.create_emulator("new_capture") == "new_capture"
    assert orch.config.avd_name == "new_capture"


def test_pairing_progress_reports_pending_state_only_once(monkeypatch):
    events = []
    orch = Orchestrator(AssistantConfig(), on_progress=events.append)
    orch._state.bundle = SessionBundle(auth_token="fabricated-token")  # noqa: SLF001

    class FakePairingClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def create_pairing(self, *, consent_ack):
            assert consent_ack is True
            return PairingRequest(
                pairing_id="fabricated-pairing",
                verification_uri="https://t.me/example_bot?startapp=fabricated",
                upload_token="fabricated-upload-token",
            )

        def wait_for_approval(self, _pairing_id, *, should_cancel, on_poll):
            assert should_cancel() is False
            on_poll(PairingState.PENDING)
            on_poll(PairingState.PENDING)
            on_poll(PairingState.APPROVED)

        def upload_session(self, _pairing_id, _upload_token, _bundle):
            pass

    monkeypatch.setattr(orchestrator_module, "PairingClient", FakePairingClient)

    result = orch.send_securely(consent_ack=True)

    assert result.ok is True
    messages = [event.message for event in events]
    assert messages.count("pairing state: pending") == 1
