"""Real key input (Input.dispatchKeyEvent) real-Chrome integration tests.

Verifies Type Text (ASCII real keys + non-ASCII insertText fallback),
Press Keys (special keys), and the focus primitive. Reuses demo.html and form.html.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURES = Path(__file__).resolve().parent.parent / "atest" / "fixtures"
DEMO = FIXTURES / "demo.html"


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


def input_value(page) -> str:
    return page.evaluate("document.querySelector('input').value")


class TestTypeText:
    def test_ascii_goes_through_real_key_events(self, page):
        # Reality check: a keydown listener records the real events.
        page.evaluate(
            "window.__keys=[];"
            "document.querySelector('input').addEventListener('keydown',"
            " function(e){ window.__keys.push(e.key); });"
        )
        page.type_text({"s": "id", "v": "name-input"}, "hi!")
        assert input_value(page) == "hi!"
        assert page.evaluate("window.__keys") == ["h", "i", "!"]

    def test_non_ascii_falls_back_to_insert_text(self, page):
        page.type_text({"s": "id", "v": "name-input"}, "\u00fcber")
        assert input_value(page) == "\u00fcber"

    def test_appends_without_clearing(self, page):
        page.type_text({"s": "id", "v": "name-input"}, "ab")
        page.type_text({"s": "id", "v": "name-input"}, "cd")
        assert input_value(page) == "abcd"

    def test_input_event_fires_for_mirror(self, page):
        page.type_text({"s": "id", "v": "name-input"}, "hey")
        mirror = page.call("getText", {"s": "id", "v": "name-mirror"})
        assert mirror == {"ok": True, "text": "hey"}


class TestPressKeys:
    def test_backspace_deletes_last_char(self, page):
        page.type_text({"s": "id", "v": "name-input"}, "abc")
        page.press_special_key("BACKSPACE")
        assert input_value(page) == "ab"

    def test_tab_moves_focus(self, page):
        page.evaluate("document.querySelector('input').focus();")
        assert (
            page.evaluate("document.activeElement.dataset.testid") == "name-input"
        )
        page.press_special_key("TAB")
        # Tab moves focus to the next focusable element — no longer the input.
        after = page.evaluate("document.activeElement.dataset.testid")
        assert after != "name-input"

    def test_unknown_key_token_raises(self, page):
        with pytest.raises(ValueError, match="unknown special key"):
            page.press_special_key("NOT_A_KEY")
