"""Escape-hatch keyword real-Chrome integration tests.

Verifies Run Javascript (evaluate delegation), Execute CDP Command
(session/browser-level routing), and Insert Text (Input.insertText into the focused element).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import BridgeEvaluationError, PageSession

DEMO = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "demo.html"


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


@pytest.fixture()
def page(chrome_process):
    connection = CdpConnection(chrome_process.ws_url)
    session = PageSession(connection).attach()
    session.navigate(DEMO.as_uri())
    yield session
    session.close()
    connection.close()


class TestRunJavascript:
    def test_arithmetic_expression_roundtrip(self, page):
        assert page.evaluate("1 + 1") == 2

    def test_string_and_object_values(self, page):
        assert page.evaluate("'ab' + 'cd'") == "abcd"
        assert page.evaluate("JSON.parse('{\"k\": 7}')") == {"k": 7}

    def test_promise_is_awaited(self, page):
        result = page.evaluate("Promise.resolve('resolved-value')")
        assert result == "resolved-value"

    def test_page_error_is_promoted(self, page):
        with pytest.raises(BridgeEvaluationError):
            page.evaluate("null.prototype.crash")


class TestExecuteCdpCommand:
    def test_browser_level_command_needs_no_session(self, page):
        result = page.connection.send("Browser.getVersion")
        assert str(result.get("product", "")).startswith("Chrome/")

    def test_session_level_command_roundtrip(self, page):
        result = page.connection.send(
            "Runtime.evaluate",
            {"expression": "2 * 21", "returnByValue": True},
            session_id=page.session_id,
        )
        assert result["result"]["value"] == 42


class TestInsertText:
    def test_text_lands_in_focused_input(self, page):
        # Contract: the element must be focused before insertion.
        page.evaluate("document.querySelector('input').focus()")
        page.evaluate("document.querySelector('input').value = ''")
        page.connection.send(
            "Input.insertText",
            {"text": "Ünïcødé ✓"},
            session_id=page.session_id,
        )
        value = page.evaluate("document.querySelector('input').value")
        assert value == "Ünïcødé ✓"
