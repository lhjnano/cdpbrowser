"""Polling keyword unit tests — verified with a fake PageSession, no browser.

Verifies the polling/assertion/evidence wiring of CdpBrowser using a
FakeSession. Real-Chrome round trips (the truth of bridge result shapes)
belong to test_page.py; here we only pin the library-layer contract:

- poll-by-default: failure results such as not-found retry until the deadline
- failure messages carry the selector, last reason, and elapsed time
- selector parse errors surface immediately without a launch
- evidence-mode gating and the shared number sequence
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional

import pytest

import cdpbrowser.library as library_module
from cdpbrowser.library import CdpBrowser
from cdpbrowser.locators import InvalidSelectorError


class FakeConnection:
    """A minimal connection mimicking Page.captureScreenshot echo."""

    def __init__(self) -> None:
        self.sent: List[Any] = []

    def send(self, method: str, params=None, session_id=None, timeout=30.0):
        self.sent.append((method, params, session_id))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"fake-png-bytes").decode("ascii")}
        raise AssertionError(f"unexpected method: {method}")


class FakeSession:
    """A fake session replaying ``call(name, *args)`` results from a script.

    ``results`` is a dict of per-call queues: ``{"click": [...],
    "exists": [...]}`` — each key is a queue of return values consumed one
    per call; when exhausted, the last value repeats.
    per call; when exhausted, the last value repeats.

    It exposes the ``attached``/``session_id`` properties of the real
    ``PageSession`` contract — evidence capture routes screenshots by ``session_id``.
    """

    attached = True
    session_id = "fake-session-id"

    def __init__(self, results: Optional[Dict[str, List[Any]]] = None):
        self.calls: List[tuple] = []
        self.results: Dict[str, List[Any]] = results or {}
        self.connection = FakeConnection()

    def call(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        queue = self.results.get(name)
        if not queue:
            raise AssertionError(f"unexpected bridge call: {name} {args!r}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def evaluate(self, expression: str) -> Any:
        self.calls.append(("evaluate", expression))
        # Diagnostic collection (_best_effort_describe) tolerates failure; None here.
        return None


def make_library(session: FakeSession, timeout: float = 0.5) -> CdpBrowser:
    lib = CdpBrowser()
    lib._session = session  # bypass lazy launch — keyword wiring only, no Chrome
    lib._timeout = timeout
    lib._poll_interval = 0.001
    return lib


class TestClickPolling:
    def test_click_retries_until_bridge_reports_ok(self):
        session = FakeSession(
            {"click": [{"ok": False, "reason": "not-found"}] * 3 + [{"ok": True}]}
        )
        lib = make_library(session)
        lib.click("testid:go-btn")
        # Exactly 4 attempts until ok.
        assert sum(1 for c in session.calls if c[0] == "click") == 4

    def test_click_timeout_message_contains_selector_reason_elapsed(self):
        session = FakeSession({"click": [{"ok": False, "reason": "not-found"}]})
        lib = make_library(session, timeout=0.15)
        with pytest.raises(AssertionError) as exc_info:
            lib.click("css:#ghost")
        message = str(exc_info.value)
        assert "Click" in message and "'css:#ghost'" in message
        assert "not-found" in message
        assert re.search(r"elapsed \d+\.\d+s", message)

    def test_click_hidden_element_reports_describe_diagnostics(self):
        describe = {"tag": "input", "id": "secret"}
        session = FakeSession(
            {"click": [{"ok": False, "reason": "not-visible", "describe": describe}]}
        )
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError) as exc_info:
            lib.click("css:#secret")
        assert "not-visible" in str(exc_info.value)

    @pytest.mark.parametrize(
        "selector, expected_call_arg",
        [
            ("css:#a b", {"s": "css", "v": "#a b"}),
            ("testid:submit", {"s": "id", "v": "submit"}),
            ("text:Log in", {"s": "t", "v": "Log in"}),
            ("x://button[1]", {"s": "x", "v": "//button[1]"}),
            ("#plain", {"s": "css", "v": "#plain"}),
        ],
    )
    def test_selector_dsl_reaches_bridge(self, selector, expected_call_arg):
        session = FakeSession({"click": [{"ok": True}]})
        lib = make_library(session)
        lib.click(selector)
        name, arg = session.calls[0]
        assert (name, arg) == ("click", expected_call_arg)

    def test_invalid_selector_fails_without_touching_session(self):
        lib = CdpBrowser()  # no session injected — the launch attempt itself must fail
        with pytest.raises(InvalidSelectorError, match="Invalid selector"):
            lib.click("testid:")


class TestFillText:
    def test_fill_text_passes_value_and_polls(self):
        session = FakeSession(
            {"setValue": [{"ok": False, "reason": "not-found"}, {"ok": True}]}
        )
        lib = make_library(session)
        lib.fill_text("testid:name", "Jane Doe")
        name, arg, value = session.calls[0]
        assert (name, arg, value) == ("setValue", {"s": "id", "v": "name"}, "Jane Doe")
        assert sum(1 for c in session.calls if c[0] == "setValue") == 2

    def test_fill_text_timeout_message(self):
        session = FakeSession({"setValue": [{"ok": False, "reason": "not-found"}]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="Fill Text 'css:#none'"):
            lib.fill_text("css:#none", "x")


class TestGetText:
    def test_polls_not_found_then_returns_text(self):
        session = FakeSession(
            {
                "getText": [
                    {"ok": False, "reason": "not-found"},
                    {"ok": False, "reason": "not-found"},
                    {"ok": True, "text": "after click"},
                ]
            }
        )
        lib = make_library(session)
        assert lib.get_text("testid:btn") == "after click"

    def test_empty_text_is_a_success(self):
        session = FakeSession({"getText": [{"ok": True, "text": ""}]})
        lib = make_library(session)
        assert lib.get_text("css:#empty") == ""

    def test_timeout_reports_not_found_reason(self):
        session = FakeSession({"getText": [{"ok": False, "reason": "not-found"}]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="not-found"):
            lib.get_text("css:#nope")


class TestShouldAssertions:
    def test_visible_passes_when_status_reports_visible(self):
        session = FakeSession({"exists": [{"exists": True, "visible": True, "enabled": True}]})
        make_library(session).element_should_be_visible("testid:title")
        assert session.calls[0][0] == "exists"

    def test_visible_times_out_with_not_found(self):
        session = FakeSession({"exists": [None]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="not-found"):
            lib.element_should_be_visible("css:#ghost")

    def test_visible_times_out_with_not_visible_and_diagnostic(self):
        session = FakeSession({"exists": [{"exists": True, "visible": False, "enabled": True}]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="not-visible"):
            lib.element_should_be_visible("css:#hidden")

    def test_exist_passes_even_when_invisible(self):
        session = FakeSession({"exists": [{"exists": True, "visible": False, "enabled": True}]})
        make_library(session).element_should_exist("css:#hidden")

    def test_exist_times_out_when_absent(self):
        session = FakeSession({"exists": [None]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="Element Should Exist"):
            lib.element_should_exist("css:#ghost")


class TestShouldNotExistTwoPhase:
    def test_present_then_removed_passes(self):
        session = FakeSession(
            {"exists": [{"exists": True, "visible": True, "enabled": True}, None]}
        )
        make_library(session).element_should_not_exist("testid:btn")

    def test_never_present_fails_with_guidance(self):
        session = FakeSession({"exists": [None]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="never observed"):
            lib.element_should_not_exist("css:#ghost")

    def test_present_but_never_removed_fails_with_still_exists(self):
        session = FakeSession({"exists": [{"exists": True, "visible": True, "enabled": True}]})
        lib = make_library(session, timeout=0.1)
        with pytest.raises(AssertionError, match="still-exists"):
            lib.element_should_not_exist("testid:stubborn")


class TestEvidenceModeAndCapture:
    @pytest.fixture()
    def evidence_root(self, tmp_path, monkeypatch):
        # Under pytest, BuiltIn resolution fails -> verifies the env-var fallback path.
        monkeypatch.setenv("CDPBROWSER_OUTPUT_DIR", str(tmp_path))
        return tmp_path

    def test_set_evidence_mode_normalizes_and_returns_previous(self):
        lib = CdpBrowser()
        assert lib.set_evidence_mode("  EVERY-STEP ") == "on-failure"
        assert lib._evidence.mode == "every-step"
        assert lib.set_evidence_mode("off") == "every-step"

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="Evidence mode must be one of"):
            CdpBrowser().set_evidence_mode("sometimes")

    def test_every_step_captures_after_click(self, evidence_root):
        session = FakeSession({"click": [{"ok": True}]})
        lib = make_library(session)
        lib.set_evidence_mode("every-step")
        lib.click("css:#btn")
        # evidence/{suite}/{test}/0001-after-click.png
        files = list(evidence_root.rglob("0001-after-click.png"))
        assert len(files) == 1
        assert files[0].read_bytes() == b"fake-png-bytes"
        assert "evidence" in files[0].parts

    def test_off_mode_does_not_capture_after_click(self, evidence_root):
        session = FakeSession({"click": [{"ok": True}]})
        lib = make_library(session)
        lib.set_evidence_mode("off")
        lib.click("css:#btn")
        assert list(evidence_root.rglob("*.png")) == []

    def test_on_failure_default_does_not_capture_after_click(self, evidence_root):
        session = FakeSession({"click": [{"ok": True}]})
        make_library(session).click("css:#btn")
        assert list(evidence_root.rglob("*.png")) == []

    def test_numbered_screenshot_shares_counter_with_auto_capture(self, evidence_root):
        session = FakeSession(
            {"click": [{"ok": True}], "setValue": [{"ok": True}]}
        )
        lib = make_library(session)
        lib.set_evidence_mode("every-step")
        lib.click("css:#btn")  # 0001-after-click.png
        lib.fill_text("css:#in", "v")  # 0002-after-fill.png
        path = lib.numbered_screenshot("manual")  # 0003-manual.png
        assert path.endswith("0003-manual.png")

    def test_numbered_screenshot_sanitizes_label(self, evidence_root):
        session = FakeSession()
        lib = make_library(session)
        path = lib.numbered_screenshot("my step/01")
        assert re.search(r"0001-my_step_01\.png$", path)

    def test_capture_failure_is_swallowed_in_auto_capture(self, evidence_root, monkeypatch):
        session = FakeSession({"click": [{"ok": True}]})
        lib = make_library(session)
        lib.set_evidence_mode("every-step")

        def boom(*args, **kwargs):
            raise ValueError("no image data")

        monkeypatch.setattr(library_module.CdpBrowser, "_capture_screenshot", boom)
        assert lib._auto_capture("failure") is None  # exceptions never leak


class TestOutputdirFallback:
    def test_env_var_beats_default_when_robot_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CDPBROWSER_OUTPUT_DIR", str(tmp_path / "custom"))
        lib = CdpBrowser()
        assert lib._resolve_outputdir() == tmp_path / "custom"

    def test_default_fallback_is_results(self, monkeypatch):
        monkeypatch.delenv("CDPBROWSER_OUTPUT_DIR", raising=False)
        lib = CdpBrowser()
        assert str(lib._resolve_outputdir()) == "results"


class TestSetTimeout:
    def test_returns_previous_and_applies_new(self):
        lib = CdpBrowser()
        previous = lib.set_timeout("1s")
        assert previous == pytest.approx(5.0)
        assert lib._timeout == pytest.approx(1.0)

    def test_non_positive_timeout_rejected(self):
        lib = CdpBrowser()
        with pytest.raises(ValueError, match="positive"):
            lib.set_timeout("0s")
        assert lib._timeout == pytest.approx(5.0)  # unchanged
