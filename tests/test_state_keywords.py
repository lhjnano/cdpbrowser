"""Get Attribute / Get Element Count real-Chrome integration tests.

Reuses the existing fixture (a11y.html) — no new fixtures.
Fixture layout: three buttons (#btn-save/#btn-cancel/#btn-aria), two label[for]-bound
inputs (#email/#pw), #ph-input with only a placeholder, plus h2/a[href]/select."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURES = Path(__file__).resolve().parent.parent / "atest" / "fixtures"


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
    session.navigate((FIXTURES / "a11y.html").as_uri())
    yield session
    session.close()
    connection.close()


class TestGetAttribute:
    def test_reads_aria_label_from_button(self, page):
        result = page.call("getAttr", {"s": "css", "v": "#btn-aria"}, "aria-label")
        assert result == {"ok": True, "value": "Admin Settings"}

    def test_missing_attribute_returns_null_value(self, page):
        result = page.call("getAttr", {"s": "css", "v": "#btn-save"}, "aria-label")
        assert result == {"ok": True, "value": None}

    def test_not_found_reason_when_element_absent(self, page):
        result = page.call("getAttr", {"s": "id", "v": "ghost"}, "id")
        assert result == {"ok": False, "reason": "not-found"}

    def test_works_with_role_locator(self, page):
        result = page.call("getAttr", {"s": "r", "v": "button Save"}, "id")
        assert result == {"ok": True, "value": "btn-save"}


class TestCount:
    def test_css_count_matches_dom(self, page):
        assert page.call("count", {"s": "css", "v": "button"}) == {"count": 3}

    def test_zero_for_absent_matches(self, page):
        assert page.call("count", {"s": "id", "v": "ghost"}) == {"count": 0}

    def test_xpath_snapshot_count(self, page):
        result = page.call("count", {"s": "x", "v": "//h2"})
        assert result["count"] >= 1

    def test_role_strategy_counts_all_matching(self, page):
        result = page.call("count", {"s": "r", "v": "button"})
        # Implicit roles (incl. input[type=submit|button]) — at least the css button count.
        assert result["count"] >= 3

    def test_role_with_name_narrows_count(self, page):
        assert page.call("count", {"s": "r", "v": "button Save"}) == {"count": 1}
