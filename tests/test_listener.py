"""Unit tests for the built-in Robot evidence listener (no browser needed).

The listener keeps no cdpbrowser imports on purpose (duck-typed library
reference), so every branch — including the 5 coverage-missing lines
(end_test failure capture paths and the close() error swallow) — is
exercised against a lightweight fake library. The last section verifies
the real ``CdpBrowser`` wiring, which is safe with no browser open.
"""

from __future__ import annotations

from types import SimpleNamespace

from cdpbrowser.library import CdpBrowser
from cdpbrowser.listener import (
    ROBOT_LISTENER_API_VERSION,
    EvidenceListener,
    EvidenceTracker,
    safe_name,
)


class FakeLibrary:
    """Duck-typed stand-in exposing exactly the surface listener.py uses."""

    def __init__(self, mode: str = "on-failure") -> None:
        self._evidence = EvidenceTracker(mode=mode)
        self.captures: list[str] = []
        self.close_calls = 0
        self.close_error: Exception | None = None

    def _auto_capture(self, label: str) -> None:
        self.captures.append(label)

    def close_browser(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _data(name="my test", parent_name="my suite"):
    parent = SimpleNamespace(name=parent_name) if parent_name is not ... else None
    return SimpleNamespace(name=name, parent=parent)


def _result(failed=False, status="PASS"):
    return SimpleNamespace(failed=failed, status=status)


class TestSafeName:
    def test_non_word_runs_collapse_to_single_underscore(self):
        assert safe_name("Suite Name / Test") == "Suite_Name_Test"

    def test_dots_and_hyphens_survive(self):
        assert safe_name("a.b-c d") == "a.b-c_d"

    def test_hangul_is_a_word_character(self):
        assert safe_name("회원가입 테스트") == "회원가입_테스트"

    def test_empty_or_whitespace_normalizes_to_unnamed(self):
        assert safe_name("") == "unnamed"
        assert safe_name("   ") == "unnamed"

    def test_symbols_only_collapse_to_underscore_slug(self):
        # re.sub yields "_" (truthy), so the "unnamed" fallback stays unused.
        assert safe_name("///") == "_"

    def test_non_string_input_is_coerced(self):
        assert safe_name(42) == "42"


class TestEvidenceTracker:
    def test_defaults(self):
        tracker = EvidenceTracker()
        assert tracker.mode == "on-failure"
        assert tracker.suite_dir == "unknown-suite"
        assert tracker.test_dir == "unknown-test"

    def test_custom_mode(self):
        assert EvidenceTracker(mode="every-step").mode == "every-step"

    def test_next_number_starts_at_one_and_increments(self):
        tracker = EvidenceTracker()
        assert [tracker.next_number() for _ in range(3)] == [1, 2, 3]

    def test_start_test_resets_context_and_counter(self):
        tracker = EvidenceTracker()
        tracker.next_number()
        tracker.start_test("My Suite", "My Test")
        assert tracker.suite_dir == "My_Suite"
        assert tracker.test_dir == "My_Test"
        assert tracker.next_number() == 1


class TestEvidenceListenerApiVersion:
    def test_module_and_class_api_version_is_3(self):
        assert ROBOT_LISTENER_API_VERSION == 3
        assert EvidenceListener.ROBOT_LISTENER_API_VERSION == 3


class TestEvidenceListenerCallbacks:
    def test_start_test_sets_evidence_context(self):
        lib = FakeLibrary()
        lib._evidence.next_number()  # dirty the counter
        EvidenceListener(lib).start_test(_data(), _result())
        assert lib._evidence.suite_dir == "my_suite"
        assert lib._evidence.test_dir == "my_test"
        assert lib._evidence.next_number() == 1

    def test_start_test_falls_back_when_names_missing(self):
        lib = FakeLibrary()
        data = SimpleNamespace(parent=SimpleNamespace(name=None), name=None)
        EvidenceListener(lib).start_test(data, _result())
        assert lib._evidence.suite_dir == "unknown-suite"
        assert lib._evidence.test_dir == "unknown-test"

    def test_start_test_falls_back_when_parent_missing(self):
        lib = FakeLibrary()
        data = SimpleNamespace(name="t", parent=None)
        EvidenceListener(lib).start_test(data, _result())
        assert lib._evidence.suite_dir == "unknown-suite"
        assert lib._evidence.test_dir == "t"

    def test_end_test_pass_does_not_capture(self):
        lib = FakeLibrary()
        EvidenceListener(lib).end_test(_data(), _result(failed=False, status="PASS"))
        assert lib.captures == []

    def test_end_test_failed_flag_triggers_failure_capture(self):
        lib = FakeLibrary()
        EvidenceListener(lib).end_test(_data(), _result(failed=True, status="PASS"))
        assert lib.captures == ["failure"]

    def test_end_test_fail_status_triggers_failure_capture(self):
        lib = FakeLibrary()
        EvidenceListener(lib).end_test(_data(), _result(failed=False, status="FAIL"))
        assert lib.captures == ["failure"]

    def test_end_test_failed_but_not_on_failure_mode_skips_capture(self):
        lib = FakeLibrary(mode="every-step")
        EvidenceListener(lib).end_test(_data(), _result(failed=True, status="FAIL"))
        assert lib.captures == []

    def test_close_calls_close_browser_once(self):
        lib = FakeLibrary()
        EvidenceListener(lib).close()
        assert lib.close_calls == 1

    def test_close_swallows_close_browser_errors(self):
        lib = FakeLibrary()
        lib.close_error = RuntimeError("already gone")
        EvidenceListener(lib).close()  # must not raise
        assert lib.close_calls == 1


class TestRealLibraryWiring:
    def test_real_library_exposes_listener_and_context_flows(self):
        lib = CdpBrowser()
        listener = lib.ROBOT_LIBRARY_LISTENER
        assert isinstance(listener, EvidenceListener)
        listener.start_test(_data("wired test", "wired suite"), _result())
        assert lib._evidence.suite_dir == "wired_suite"
        assert lib._evidence.test_dir == "wired_test"
        listener.end_test(_data("wired test", "wired suite"), _result(status="PASS"))
        listener.close()  # close_browser is safe with nothing open
