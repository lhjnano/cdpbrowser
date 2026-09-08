"""Gap-fill batch — library validation and error branches (no browser).

Targets the branches the happy-path suites leave cold: keyword argument
validation, promise failure mapping, tab resolution errors, upload guards,
recording persistence, and escape-hatch routing. Uses the FakeSession
pattern from test_library_polling.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from cdpbrowser.cdp.errors import CdpRemoteError
from cdpbrowser.library import CdpBrowser
from cdpbrowser.promises import PromiseCancelledError


class FakeConnection:
    """Minimal connection recording sends, replaying scripted results."""

    def __init__(self, scripted: Optional[Dict[str, Any]] = None):
        self.sent: List[tuple] = []
        self.scripted = scripted or {}

    def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params, session_id))
        if method in self.scripted:
            result = self.scripted[method]
            if isinstance(result, Exception):
                raise result
            return result
        return {"ok": True}


class FakeSession:
    attached = True
    session_id = "fake-session-id"

    def __init__(self, results=None, scripted=None):
        self.calls: List[tuple] = []
        self.results: Dict[str, List[Any]] = results or {}
        self.connection = FakeConnection(scripted)

    def call(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        queue = self.results.get(name)
        if queue:
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            self.results.setdefault(name, []).append(value)
            return value
        return {"ok": True}

    # Promise waiters and evaluate are resolved as attributes by keywords —
    # default to the "unknown handle" contract instead of AttributeError.
    def wait_dialog(self, handle, timeout):
        raise KeyError(f"unknown or expired promise handle: {handle!r}")

    def wait_download(self, handle, timeout):
        raise KeyError(f"unknown or expired promise handle: {handle!r}")

    def evaluate(self, expression, timeout=None):
        return ""


def make_library(session: FakeSession) -> CdpBrowser:
    lib = CdpBrowser()
    lib._session = session  # bypass lazy launch
    lib._timeout = 0.2
    lib._poll_interval = 0.01
    return lib


class TestWaitForFailureMapping:
    def test_unknown_handle_message(self):
        lib = make_library(FakeSession())
        with pytest.raises(AssertionError, match="never armed"):
            lib.wait_for("download-999")

    def test_cancelled_handle_maps_to_assertion(self):
        lib = make_library(FakeSession())

        # A handle that is registered but cancelled -> PromiseError branch.
        import cdpbrowser.promises as promises_mod

        broker = promises_mod.PromiseBroker.__new__(promises_mod.PromiseBroker)
        # Simpler: monkeypatch the session wait methods.
        class S(FakeSession):
            def wait_dialog(self, handle, timeout):
                raise PromiseCancelledError("Promise 'x' was cancelled")

        lib2 = make_library(S())
        with pytest.raises(AssertionError, match="cancelled"):
            lib2.wait_for("dialog-1")

    def test_download_timeout_hint(self):
        class S(FakeSession):
            def wait_download(self, handle, timeout):
                from cdpbrowser.promises import PromiseTimeoutError

                raise PromiseTimeoutError("Promise 'download-1' timed out")

        lib = make_library(S())
        with pytest.raises(AssertionError, match="Promise Next Download"):
            lib.wait_for("download-1")


class TestEscapeHatchValidation:
    def test_execute_cdp_requires_domain(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="Domain.method"):
            lib.execute_cdp_command("notamethod")

    def test_execute_cdp_rejects_bad_json(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="not valid JSON"):
            lib.execute_cdp_command("Browser.getVersion", "{oops")

    def test_execute_cdp_rejects_non_object_json(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="JSON object"):
            lib.execute_cdp_command("Browser.getVersion", "[1,2]")

    def test_execute_cdp_routes_browser_level_without_session(self):
        session = FakeSession()
        lib = make_library(session)
        lib.execute_cdp_command("Browser.getVersion", "")
        method, _, sid = session.connection.sent[-1]
        assert method == "Browser.getVersion"
        assert sid is None  # browser-level

    def test_execute_cdp_routes_session_level(self):
        session = FakeSession()
        lib = make_library(session)
        lib.execute_cdp_command("Runtime.evaluate", '{"expression": "1"}')
        method, params, sid = session.connection.sent[-1]
        assert method == "Runtime.evaluate"
        assert sid == "fake-session-id"

    def test_execute_cdp_promotes_remote_error(self):
        session = FakeSession(
            scripted={"Emulation.fail": CdpRemoteError(-32000, "boom", None)}
        )
        lib = make_library(session)
        with pytest.raises(CdpRemoteError):
            lib.execute_cdp_command("Emulation.fail", "")

    def test_run_javascript_requires_expression(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="non-empty"):
            lib.run_javascript("   ")


class TestUploadGuards:
    def test_requires_at_least_one_path(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="at least one file"):
            lib.upload_file("testid:file")

    def test_missing_file_fails_with_resolved_path(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="file not found"):
            lib.upload_file("testid:file", "/nonexistent/dir/file.bin")

    def test_only_css_and_testid_supported(self):
        lib = make_library(FakeSession())
        with pytest.raises(ValueError, match="css=/testid"):
            lib.upload_file("text:Upload", "/etc/hostname")


class TestSaveRecording:
    def test_writes_robot_and_python_formats(self, tmp_path):
        events = [
            {"type": "navigate", "url": "https://example.com/"},
            {"type": "click", "selector": "testid:go"},
        ]

        class S(FakeSession):
            def stop_recording(self):
                return events

        lib = make_library(S())
        robot_path = lib.save_recording(str(tmp_path / "sub" / "rec.robot"))
        assert robot_path.endswith("rec.robot")
        assert "Click    testid:go" in open(robot_path, encoding="utf-8").read()

        py_path = lib.save_recording(
            str(tmp_path / "rec.py"), name="My Flow", format="Python"
        )
        body = open(py_path, encoding="utf-8").read()
        assert "session.navigate" in body
        assert "def my_flow" in body

    def test_unknown_format_rejected(self, tmp_path):
        class S(FakeSession):
            def stop_recording(self):
                return []

        lib = make_library(S())
        with pytest.raises(ValueError, match="Robot or Python"):
            lib.save_recording(str(tmp_path / "rec.txt"), format="TAP")


class TestTabResolutionErrors:
    def test_index_out_of_range(self):
        lib = make_library(FakeSession())
        lib._sessions = ["a", "b"]
        with pytest.raises(AssertionError, match="out of range"):
            lib.switch_to_tab(5)

    def test_empty_identifier_rejected(self):
        lib = make_library(FakeSession())
        lib._sessions = ["a"]
        with pytest.raises(AssertionError, match="non-empty"):
            lib.switch_to_tab("   ")

    def test_close_named_non_current_tab(self):
        session_a, session_b = FakeSession(), FakeSession()

        class Titled(FakeSession):
            def __init__(self, title):
                super().__init__()
                self.title = title
                self.closed = False

            def evaluate(self, expression, timeout=None):
                return self.title if "title" in expression else "http://x/"

            def close(self):
                self.closed = True

        lib = make_library(session_a)
        lib._sessions = [session_a, Titled("Reports")]
        lib._session = session_a
        named = lib._sessions[1]
        remaining = lib.close_tab("Reports")
        assert remaining == 0
        assert lib._session is session_a  # current untouched
        assert named.closed
        assert lib._sessions == [session_a]


class TestStateKeywordErrors:
    def test_get_element_count_bridge_failure_is_assertion(self):
        class S(FakeSession):
            def call(self, name, *args):
                return {"unexpected": "shape"}

        lib = make_library(S())
        with pytest.raises(AssertionError, match="bridge returned"):
            lib.get_element_count("testid:anything")


class TestInsertTextValidation:
    def test_rejects_non_string(self):
        lib = make_library(FakeSession())
        with pytest.raises(TypeError):
            lib.insert_text(123)
