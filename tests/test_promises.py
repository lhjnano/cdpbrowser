"""PromiseBroker unit tests — event-to-future matching via the mock CDP server.

Pins the contract of the infrastructure shared by the dialog and download
keywords: arm (FIFO claiming) -> wait (timeout/idempotence) -> cancel/close.
"""

from __future__ import annotations

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.promises import (
    PromiseBroker,
    PromiseCancelledError,
    PromiseTimeoutError,
)
from cdp_mock import MockCdpServer

PATTERN = "Page.javascriptDialogOpening"


@pytest.fixture()
def mock():
    server = MockCdpServer()
    yield server
    server.close()


@pytest.fixture()
def broker(mock):
    connection = CdpConnection(mock.ws_url)
    yield PromiseBroker(connection), connection
    connection.close()


class TestArmAndComplete:
    def test_armed_handle_completes_on_matching_event(self, mock, broker):
        b, _ = broker
        handle = b.arm(PATTERN, on_complete=lambda e, c: e.params["message"])
        mock.broadcast_event(PATTERN, {"message": "Passwords don't match"})
        assert b.wait(handle, timeout=3.0) == "Passwords don't match"

    def test_matcher_skips_non_matching_events(self, mock, broker):
        b, _ = broker
        handle = b.arm(
            PATTERN,
            matcher=lambda e: e.params["message"] if e.params.get("message") == "want" else None,
            on_complete=lambda e, c: c,
        )
        mock.broadcast_event(PATTERN, {"message": "skip me"})
        with pytest.raises(PromiseTimeoutError):
            b.wait(handle, timeout=0.3)
        mock.broadcast_event(PATTERN, {"message": "want"})
        assert b.wait(handle, timeout=3.0) == "want"

    def test_completed_handle_is_idempotent(self, mock, broker):
        b, _ = broker
        handle = b.arm(PATTERN, on_complete=lambda e, c: "once")
        mock.broadcast_event(PATTERN, {"message": "hi"})
        assert b.wait(handle, timeout=3.0) == "once"
        assert b.wait(handle, timeout=0.01) == "once"


class TestFifoClaiming:
    def test_same_pattern_arms_claim_events_in_fifo_order(self, mock, broker):
        b, _ = broker
        h1 = b.arm(PATTERN, on_complete=lambda e, c: "first:" + e.params["m"])
        h2 = b.arm(PATTERN, on_complete=lambda e, c: "second:" + e.params["m"])
        mock.broadcast_event(PATTERN, {"m": "a"})
        mock.broadcast_event(PATTERN, {"m": "b"})
        assert b.wait(h1, timeout=3.0) == "first:a"
        assert b.wait(h2, timeout=3.0) == "second:b"


class TestTimeoutCancelClose:
    def test_wait_times_out_without_event(self, broker):
        b, _ = broker
        handle = b.arm(PATTERN, on_complete=lambda e, c: c)
        with pytest.raises(PromiseTimeoutError):
            b.wait(handle, timeout=0.1)

    def test_cancelled_handle_fails_wait(self, mock, broker):
        b, _ = broker
        handle = b.arm(PATTERN, on_complete=lambda e, c: c)
        b.cancel(handle)
        mock.broadcast_event(PATTERN, {"m": "late"})
        with pytest.raises(PromiseCancelledError):
            b.wait(handle, timeout=1.0)

    def test_unknown_handle_raises_keyerror(self, broker):
        b, _ = broker
        with pytest.raises(KeyError):
            b.wait("no-such-handle", timeout=0.1)

    def test_close_is_idempotent_and_fails_pending(self, broker):
        b, conn = broker
        handle = b.arm(PATTERN, on_complete=lambda e, c: c)
        b.close()
        b.close()  # re-entry safe
        # close removes handles from the registry, so wait fails with KeyError
        # (lookup fails before the future ever sees PromiseCancelledError).
        # library.wait_for converts both into user-friendly messages.
        with pytest.raises((PromiseCancelledError, KeyError)):
            b.wait(handle, timeout=1.0)
        conn.close()  # double close is safe too
