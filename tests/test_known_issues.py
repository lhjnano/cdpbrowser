"""Fixes driven by real-suite feedback (documents/cdpbrowser-known-issues.md).

Issue 1: DevTools handshake timeout carries the locked-display hint.
Issue 2: Execute Javascript skips result serialization.
Issue 3: Element Should Not Exist failure points at Get Element Count.
Issue 4: CdpConnection.send retries timed-out commands once by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdp_mock import MockCdpServer
from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.cdp.errors import ChromeLaunchError, CdpTimeoutError
from cdpbrowser.cdp.transport import CdpTimeoutError as TransportTimeout
from cdpbrowser.page import PageSession

FIXTURES = Path(__file__).resolve().parent.parent / "atest" / "fixtures"


# ---------------------------------------------------------------------------
# Issue 4: timeout-only send retry
# ---------------------------------------------------------------------------


class TestSendRetry:
    def test_one_shot_delay_retries_then_succeeds(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        try:
            server.one_shot_delays["Slow.command"] = 1.0
            result = conn.send("Slow.command", timeout=0.4)
            assert result["echo_method"] == "Slow.command"
            # Two attempts: the first timed out, the retry succeeded.
            assert len(server.seen_ids) == 2
        finally:
            conn.close()
            server.close()

    def test_persistent_delay_exhausts_retries(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        try:
            server.delay_methods["Always.slow"] = 1.0
            with pytest.raises(CdpTimeoutError):
                conn.send("Always.slow", timeout=0.3)
            assert len(server.seen_ids) == 2  # initial + one retry
        finally:
            conn.close()
            server.close()

    def test_retry_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("CDPBROWSER_SEND_RETRIES", "0")
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        try:
            server.one_shot_delays["Once.slow"] = 1.0
            with pytest.raises(CdpTimeoutError):
                conn.send("Once.slow", timeout=0.3)
            assert len(server.seen_ids) == 1  # no retry attempted
        finally:
            conn.close()
            server.close()

    def test_remote_errors_are_not_retried(self):
        server = MockCdpServer()
        conn = CdpConnection(server.ws_url)
        try:
            with pytest.raises(Exception):
                conn.send("Mock.fail", {"oops": True})
            assert len(server.seen_ids) == 1
        finally:
            conn.close()
            server.close()


# ---------------------------------------------------------------------------
# Issue 1: locked-display hints
# ---------------------------------------------------------------------------


class TestHandshakeHints:
    def test_launch_timeout_message_mentions_display_lock(self, tmp_path):
        script = tmp_path / "chrome-like"
        # Ignores SIGTERM so the launcher's cleanup cannot race the failure
        # message into the "exited early" branch.
        script.write_text("#!/bin/sh\ntrap '' TERM\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)
        proc = ChromeProcess(str(script), headless=True, devtools_timeout=0.5)
        with pytest.raises(ChromeLaunchError) as excinfo:
            proc.start()
        message = str(excinfo.value)
        assert "DevTools endpoint" in message
        assert "locked or sleeping host display" in message
        assert "accepted, requests never read" in message


# ---------------------------------------------------------------------------
# Issue 2: Execute Javascript / return_by_value
# ---------------------------------------------------------------------------


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


class TestExecuteJavascript:
    def test_side_effects_run_and_object_result_is_none(self, chrome_process):
        from cdpbrowser.library import CdpBrowser

        lib = CdpBrowser()
        lib.open_browser()
        try:
            lib.go_to((FIXTURES / "demo.html").as_uri())
            # A DOM/jQuery-like object as the final value: Run Javascript
            # would ask CDP to serialize it; Execute Javascript must not.
            lib.execute_javascript(
                "window.__marker = 41; document.body;"
            )
            assert lib.run_javascript("window.__marker") == 41
            # Promises are still awaited on the no-return path.
            lib.execute_javascript(
                "window.__resolved = Promise.resolve(7).then("
                "function(v){ window.__promiseMarker = v; }); 'ok'"
            )
            assert lib.run_javascript("window.__promiseMarker") == 7
        finally:
            lib.close_browser()

    def test_empty_script_rejected(self):
        from cdpbrowser.library import CdpBrowser

        lib = CdpBrowser()
        with pytest.raises(ValueError, match="non-empty"):
            lib.execute_javascript("   ")


# ---------------------------------------------------------------------------
# Issue 3: Element Should Not Exist guidance
# ---------------------------------------------------------------------------


class TestElementShouldNotExistGuidance:
    def test_failure_message_mentions_alternative(self):
        from cdpbrowser.library import CdpBrowser

        class FakeSession:
            attached = True
            session_id = "fake"

            def call(self, name, *args):
                return {"exists": False}  # element never present

        lib = CdpBrowser()
        lib._session = FakeSession()
        lib._timeout = 0.2
        lib._poll_interval = 0.01
        with pytest.raises(AssertionError, match="Get Element Count"):
            lib.element_should_not_exist("testid:ghost")
