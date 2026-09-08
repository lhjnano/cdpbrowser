"""Unit tests for cdpbrowser.cdp.transport — mock-server based, all synchronous.

The transport itself is a synchronous API, so no asyncio.run wrappers are
needed (the venv also lacks pytest-asyncio).
"""

import queue
import time

import pytest

from cdp_mock import MockCdpServer
from cdpbrowser.cdp import (
    CdpConnection,
    CdpConnectionClosedError,
    CdpError,
    CdpEvent,
    CdpRemoteError,
    CdpTimeoutError,
)


@pytest.fixture()
def server():
    srv = MockCdpServer()
    yield srv
    srv.close()


@pytest.fixture()
def conn(server):
    connection = CdpConnection(server.ws_url)
    yield connection
    connection.close()


class TestRoundtrip:
    def test_connect_and_command_roundtrip(self, conn):
        result = conn.send("Page.navigate", {"url": "https://example.test/"})
        assert result["echo_method"] == "Page.navigate"
        assert result["echo_params"] == {"url": "https://example.test/"}
        assert result["echo_session_id"] is None

    def test_command_without_params(self, conn):
        result = conn.send("Browser.getVersion")
        assert result["echo_method"] == "Browser.getVersion"
        assert result["echo_params"] is None

    def test_ids_increment(self, server, conn):
        conn.send("First.command")
        conn.send("Second.command")
        assert len(server.seen_ids) == 2
        assert server.seen_ids[1] > server.seen_ids[0]

    def test_send_with_session_id(self, conn):
        result = conn.send("Runtime.evaluate", {"expression": "1+1"}, session_id="S-42")
        assert result["echo_session_id"] == "S-42"
        assert result["echo_params"] == {"expression": "1+1"}

    def test_remote_error_response(self, conn):
        with pytest.raises(CdpRemoteError) as excinfo:
            conn.send("Mock.fail", {"oops": True})
        assert excinfo.value.code == -32601
        assert excinfo.value.message == "mock failure"
        assert issubclass(CdpRemoteError, CdpError)


class TestEvents:
    def test_browser_and_session_events_distinguished(self, server, conn):
        q = conn.subscribe("Mock.topic.*")

        server.broadcast_event("Mock.topic.browser", {"n": 1})
        browser_event = q.get(timeout=5)
        assert isinstance(browser_event, CdpEvent)
        assert browser_event.method == "Mock.topic.browser"
        assert browser_event.params == {"n": 1}
        assert browser_event.session_id is None

        server.broadcast_event("Mock.topic.session", {"n": 2}, session_id="S-9")
        session_event = q.get(timeout=5)
        assert session_event.method == "Mock.topic.session"
        assert session_event.session_id == "S-9"
        assert session_event.params == {"n": 2}

    def test_event_domain_isolation(self, server, conn):
        qa = conn.subscribe("Page.eventA")
        qb = conn.subscribe("Page.eventB")

        server.broadcast_event("Page.eventA", {"x": 1})
        event = qa.get(timeout=5)
        assert event.method == "Page.eventA"

        with pytest.raises(queue.Empty):
            qb.get(timeout=0.3)

    def test_unsubscribe_stops_delivery(self, server, conn):
        q = conn.subscribe("Mock.topic.*")
        conn.unsubscribe(q)

        server.broadcast_event("Mock.topic.after", {"n": 1})
        time.sleep(0.3)
        assert q.empty()

    def test_multiple_subscribers_same_pattern(self, server, conn):
        qa = conn.subscribe("Mock.multi")
        qb = conn.subscribe("Mock.multi")
        server.broadcast_event("Mock.multi", {"k": "v"})
        assert qa.get(timeout=5).params == {"k": "v"}
        assert qb.get(timeout=5).params == {"k": "v"}


class TestTimeout:
    def test_silent_server_times_out(self, server):
        server.ignore_ids = True
        connection = CdpConnection(server.ws_url)
        try:
            with pytest.raises(CdpTimeoutError):
                connection.send("Silent.command", timeout=0.4)
        finally:
            connection.close()

    def test_timeout_does_not_poison_next_command(self, server, conn):
        # Even after one command times out, its late response is cleaned from
        # the pending table and ignored; later commands keep working.
        server.delay_methods["No.such.command"] = 1.0  # delays the reply by 1.0s
        with pytest.raises(CdpTimeoutError):
            conn.send("No.such.command", timeout=0.4)
        # Late reply (at 1.0s) arrives after the 0.4s timeout — must be reaped and ignored
        time.sleep(1.2)
        result = conn.send("Alive.command", timeout=5)
        assert result["echo_method"] == "Alive.command"


class TestConnectionClosed:
    def test_server_close_fails_pending_command(self):
        server = MockCdpServer(close_immediately=True)
        connection = CdpConnection(server.ws_url)
        try:
            with pytest.raises(CdpConnectionClosedError):
                connection.send("Whatever.command", timeout=5)
        finally:
            connection.close()
            server.close()

    def test_send_after_close_raises(self, server):
        connection = CdpConnection(server.ws_url)
        connection.close()
        with pytest.raises(CdpConnectionClosedError):
            connection.send("Too.late")

    def test_close_is_idempotent(self, conn):
        conn.close()
        conn.close()  # calling twice raises nothing
        conn.close()

    def test_connection_error_hierarchy(self):
        assert issubclass(CdpTimeoutError, CdpError)
        assert issubclass(CdpConnectionClosedError, CdpError)


class TestLifecycle:
    def test_close_cleans_background_thread(self, server):
        connection = CdpConnection(server.ws_url)
        thread = connection._thread
        connection.close()
        assert not thread.is_alive()

    def test_repeated_send_after_close_is_deterministic(self, server):
        connection = CdpConnection(server.ws_url)
        connection.close()
        for _ in range(3):
            with pytest.raises(CdpConnectionClosedError):
                connection.send("X.y")
