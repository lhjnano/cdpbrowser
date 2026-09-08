"""Pure unit tests for polling.poll_until — no browser/bridge needed."""

from __future__ import annotations

import time

import pytest

from cdpbrowser.polling import PollTimeoutError, poll_until


class TestSuccessPaths:
    def test_truthy_value_returned_as_is(self):
        assert poll_until(lambda: "done", timeout=1.0, interval=0.01) == "done"

    def test_zero_is_falsy_but_none_is_not_success(self):
        # 0 is falsy -> retryable. So is None.
        assert poll_until(lambda: 1, timeout=0.1, interval=0.01) == 1

    def test_successful_triple_returned_whole(self):
        outcome = (True, None, None)
        assert poll_until(lambda: outcome, timeout=1.0, interval=0.01) is outcome

    def test_empty_string_is_retried_not_returned(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            return "" if attempts["n"] < 2 else "ok"

        assert poll_until(fn, timeout=1.0, interval=0.01) == "ok"
        assert attempts["n"] == 2


class TestRetries:
    def test_retries_until_condition_flips(self):
        state = {"n": 0, "calls": 0}

        def fn():
            state["calls"] += 1
            state["n"] += 1
            if state["n"] >= 5:
                return (True, None, None)
            return (False, "not-found", None)

        result = poll_until(fn, timeout=5.0, interval=0.001)
        assert result[0] is True
        assert state["calls"] == 5

    def test_plain_falsy_values_are_retried_without_reason(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return None if calls["n"] < 3 else "value"

        assert poll_until(fn, timeout=1.0, interval=0.001) == "value"
        assert calls["n"] == 3


class TestTimeout:
    def test_raises_poll_timeout_error_with_desc_and_timeout(self):
        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(lambda: False, timeout=0.05, interval=0.01, desc="Click 'css:#x'")
        message = str(exc_info.value)
        assert "Click 'css:#x'" in message
        assert "0.05s" in message
        assert "elapsed" in message

    def test_last_reason_is_reported(self):
        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(
                lambda: (False, "not-visible", None), timeout=0.05, interval=0.01
            )
        assert exc_info.value.reason == "not-visible"
        assert "not-visible" in str(exc_info.value)

    def test_describe_diagnostics_accumulate(self):
        describes = [{"tag": "div", "n": 1}, {"tag": "div", "n": 2}, {"tag": "div", "n": 3}]
        index = {"i": 0}

        def fn():
            describe = describes[index["i"] % len(describes)]  # cycles until the deadline
            index["i"] += 1
            return (False, "not-enabled", describe)

        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(fn, timeout=0.05, interval=0.001)
        error = exc_info.value
        assert error.diagnostics[-1] == {"tag": "div", "n": 3}
        # The last diagnostic also shows up in the message — RF log traceability.
        assert "'n': 3" in str(error)

    def test_describe_none_is_not_accumulated(self):
        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(lambda: (False, "not-found", None), timeout=0.05, interval=0.01)
        assert exc_info.value.diagnostics == []

    def test_plain_falsy_resets_reason_to_none(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                return (False, "not-found", None)
            return False  # failure without reason info

        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(fn, timeout=0.05, interval=0.01)
        assert exc_info.value.reason is None

    def test_timeout_zero_still_attempts_once(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return False

        with pytest.raises(PollTimeoutError):
            poll_until(fn, timeout=0.0, interval=0.01)
        assert calls["n"] == 1

    def test_first_attempt_succeeding_within_zero_timeout(self):
        assert poll_until(lambda: True, timeout=0.0, interval=0.01) is True

    def test_elapsed_is_measured_not_assumed(self):
        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(lambda: False, timeout=0.1, interval=0.02)
        assert 0.1 <= exc_info.value.elapsed < 2.0

    def test_exceptions_from_fn_propagate_immediately(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise RuntimeError("connection dead")

        with pytest.raises(RuntimeError, match="connection dead"):
            poll_until(fn, timeout=5.0, interval=0.001)
        assert calls["n"] == 1  # not absorbed by retries


class TestTiming:
    def test_interval_governs_retry_spacing(self):
        # timeout 0.1s + interval 0.03s -> roughly up to 4 attempts in 0.1s.
        # A busy loop that ignored the interval would make far more calls.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return False

        with pytest.raises(PollTimeoutError):
            poll_until(fn, timeout=0.1, interval=0.03)
        assert 2 <= calls["n"] <= 8

    def test_sleep_never_overshoots_deadline_significantly(self):
        # When the remaining time is below the interval, only the remainder is
        # slept -> elapsed does not greatly exceed the timeout.
        with pytest.raises(PollTimeoutError) as exc_info:
            poll_until(lambda: False, timeout=0.08, interval=0.5)
        assert exc_info.value.elapsed < 1.0


class TestArgumentValidation:
    @pytest.mark.parametrize("timeout", [-0.1, -1])
    def test_negative_timeout_rejected(self, timeout):
        with pytest.raises(ValueError, match="timeout"):
            poll_until(lambda: True, timeout=timeout, interval=0.01)

    @pytest.mark.parametrize("interval", [0, -0.01])
    def test_non_positive_interval_rejected(self, interval):
        with pytest.raises(ValueError, match="interval"):
            poll_until(lambda: False, timeout=0.1, interval=interval)

    def test_timeout_and_interval_accept_robot_converted_floats(self):
        # library.py converts timestr_to_secs("5s") -> 5.0 before calling.
        assert poll_until(lambda: "x", timeout=5.0, interval=0.1) == "x"
