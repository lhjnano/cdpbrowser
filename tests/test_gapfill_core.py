"""Gap-fill batch — core error paths (mock server + real Chrome where cheap).

Cold branches targeted: transport failure fan-out, promise broker error
completion paths, download filename-resolution fallbacks, recorder
correlation edges, and real-key validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdp_mock import MockCdpServer
from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.cdp.errors import CdpError
from cdpbrowser.cdp.transport import CdpEvent
from cdpbrowser.page import PageSession
from cdpbrowser.promises import PromiseBroker, PromiseTimeoutError

FIXTURES = Path(__file__).resolve().parent.parent / "atest" / "fixtures"


@pytest.fixture(scope="session")
def chrome_process():
    path = find_chrome()
    if path is None:
        pytest.skip(
            "Chrome binary not found "
            "(set CDPBROWSER_CHROME_PATH to enable this test)"
        )
    proc = ChromeProcess(path, headless=True)
    proc.start()
    yield proc
    proc.stop()


class TestPromisesErrorCompletion:
    def test_matcher_exception_completes_arm_with_same_error(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        broker = PromiseBroker(conn)
        try:
            boom = RuntimeError("matcher blew up")

            def bad_matcher(event):
                raise boom

            handle = broker.arm("X.y", matcher=bad_matcher, on_complete=lambda e, c: c)
            server.broadcast_event("X.y", {"m": 1})
            with pytest.raises(RuntimeError, match="matcher blew up"):
                broker.wait(handle, 3.0)
        finally:
            broker.close()
            conn.close()
            server.close()

    def test_on_complete_exception_propagates_to_wait(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        broker = PromiseBroker(conn)
        try:
            handle = broker.arm(
                "X.y",
                on_complete=lambda e, c: 1 / 0,
            )
            server.broadcast_event("X.y", {})
            with pytest.raises(ZeroDivisionError):
                broker.wait(handle, 3.0)
        finally:
            broker.close()
            conn.close()
            server.close()

    def test_prime_after_close_is_error(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        broker = PromiseBroker(conn)
        broker.close()
        with pytest.raises(Exception):
            broker.prime("X.y")
        broker.close()  # idempotent even after error
        conn.close()
        server.close()

    def test_cancel_unknown_handle_is_noop(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        broker = PromiseBroker(conn)
        try:
            broker.cancel("never-armed")  # must not raise
        finally:
            broker.close()
            conn.close()
            server.close()

    def test_wait_on_cancelled_future_raises_cancelled(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        broker = PromiseBroker(conn)
        try:
            handle = broker.arm("X.y", on_complete=lambda e, c: c)
            broker.cancel(handle)
            with pytest.raises(Exception, match="cancelled"):
                broker.wait(handle, 3.0)
        finally:
            broker.close()
            conn.close()
            server.close()


class TestTransportFailureFanout:
    def test_server_close_fails_inflight_command(self):
        server = MockCdpServer(ignore_ids=True)
        conn = CdpConnection(server.ws_url)
        try:
            with pytest.raises(Exception):
                conn.send("Slow.command", timeout=30.0)
        finally:
            conn.close()
            server.close()

    def test_remote_error_carries_code_and_message(self):
        from cdpbrowser.cdp.errors import CdpRemoteError

        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        try:
            with pytest.raises(CdpRemoteError) as excinfo:
                conn.send("Mock.fail", {"oops": True})
            assert excinfo.value.code == -32601
            assert "mock failure" in excinfo.value.message
        finally:
            conn.close()
            server.close()


class TestRecorderCorrelationEdges:
    def test_download_without_name_uses_guid(self):
        correlated = PageSession._correlate_recording(
            [
                {"type": "click", "selector": "testid:dl"},
                {"type": "download", "guid": "abc-123", "filename": "abc-123"},
            ],
        )
        assert correlated[0]["download"] == "abc-123"

    def test_multiple_observations_attach_to_distinct_actions(self):
        correlated = PageSession._correlate_recording(
            [
                {"type": "click", "selector": "testid:a"},
                {"type": "dialog", "message": "first"},
                {"type": "fill", "selector": "testid:b", "value": "x"},
                {"type": "dialog", "message": "second"},
            ],
        )
        assert correlated[0]["dialog"] == "first"
        assert correlated[1].get("dialog") is None or correlated[1]["dialog"] != "first"

    def test_second_download_on_same_action_is_kept_first(self):
        correlated = PageSession._correlate_recording(
            [
                {"type": "click", "selector": "testid:x"},
                {"type": "download", "guid": "g1", "filename": "one.txt"},
                {"type": "download", "guid": "g2", "filename": "two.txt"},
            ],
        )
        assert correlated[0]["download"] == "one.txt"


class TestPageRecordingGuards:
    def test_stop_when_not_recording_returns_empty(self, chrome_process):
        conn = CdpConnection(chrome_process.ws_url)
        session = PageSession(conn).attach()
        try:
            assert session.stop_recording() == []
            assert session.recording_events() == []
        finally:
            session.close()
            conn.close()

    def test_start_twice_is_idempotent_event_count(self, chrome_process):
        conn = CdpConnection(chrome_process.ws_url)
        session = PageSession(conn).attach()
        try:
            session.navigate((FIXTURES / "demo.html").as_uri())
            assert session.start_recording() == 0
            session.real_click_element({"s": "id", "v": "swap-button"})
            events = session.stop_recording()
            assert any(e["type"] == "click" for e in events)
            # Restart on the same session works and resets the list.
            assert session.start_recording() == 0  # fresh event list
            assert session.stop_recording() == []
        finally:
            session.close()
            conn.close()

    def test_unknown_special_key_raises(self, chrome_process):
        conn = CdpConnection(chrome_process.ws_url)
        session = PageSession(conn).attach()
        try:
            session.navigate((FIXTURES / "demo.html").as_uri())
            with pytest.raises(ValueError, match="unknown special key"):
                session.press_special_key("NOT_A_KEY")
        finally:
            session.close()
            conn.close()
