"""Shadow DOM piercing (deep: opt-in) real-Chrome integration tests.

Elements inside an open shadow root are invisible to normal traversal
(proof of the compatibility opt-in), and the deep: modifier lets the
strategies (testid/role/text/css) pierce through. Closed roots stay inaccessible (boundary proof)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "shadow.html"

SHADOW_BTN = {"s": "id", "v": "shadow-btn", "p": 1}
PLAIN_BTN = {"s": "id", "v": "shadow-btn"}  # no deep


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
    session.navigate(FIXTURE.as_uri())
    yield session
    session.close()
    connection.close()


class TestOptIn:
    def test_shallow_cannot_see_inside_shadow_root(self, page):
        # Without deep the shadow interior is invisible — opt-in proof.
        assert page.call("exists", PLAIN_BTN) is None

    def test_deep_testid_finds_and_clicks(self, page):
        assert page.call("click", SHADOW_BTN) == {"ok": True}
        assert page.call("getText", SHADOW_BTN) == {
            "ok": True,
            "text": "shadow clicked",
        }

    def test_deep_setValue_and_mirror(self, page):
        arg = {"s": "id", "v": "shadow-input", "p": 1}
        assert page.call("setValue", arg, "typed inside") == {"ok": True}
        mirror = page.call("getText", {"s": "id", "v": "shadow-mirror", "p": 1})
        assert mirror["text"] == "mirror:typed inside"

    def test_deep_role_and_text_strategies(self, page):
        assert page.call("exists", {"s": "r", "v": "button", "p": 1}) is not None
        found = page.call("getText", {"s": "t", "v": "shadow submit", "p": 1})
        assert found["ok"] is True

    def test_deep_css_strategy(self, page):
        status = page.call("exists", {"s": "css", "v": ".inner button", "p": 1})
        assert status is not None

    def test_deep_xpath_finds_inside_shadow_root(self, page):
        # ShadowRoot has no evaluate — passing the root as document.evaluate's
        # context is what performs the piercing (regression pin).
        found = page.call(
            "exists",
            {"s": "x", "v": "//button[@data-testid='shadow-btn']", "p": 1},
        )
        assert found is not None

    def test_deep_xpath_count_spans_roots(self, page):
        shallow = page.call("count", {"s": "x", "v": "//button"})
        deep = page.call("count", {"s": "x", "v": "//button", "p": 1})
        assert shallow["count"] == 1  # light only
        assert deep["count"] == 2  # light + open shadow (closed stays out)

    def test_deep_count_includes_shadow_elements(self, page):
        shallow = page.call("count", {"s": "css", "v": "button"})
        deep = page.call("count", {"s": "css", "v": "button", "p": 1})
        # light 1 + open shadow 1 + closed interior (inaccessible) = shallow 1, deep 2.
        assert shallow["count"] == 1
        assert deep["count"] == 2


class TestClosedRootBoundary:
    def test_closed_shadow_root_is_not_pierced(self, page):
        # Closed roots are inaccessible even via browser APIs — boundary holds.
        assert page.call("exists", {"s": "id", "v": "secret-btn", "p": 1}) is None
