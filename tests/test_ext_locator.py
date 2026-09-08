"""ext: framework-registry locator real-Chrome integration tests.

Proves the ComponentQuery path with the fake-Ext fixture
(window.Ext.ComponentQuery) — no real Ext JS needed. Verifies registry
lookup, descendant chains, interaction, and the explicit error when Ext is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import BridgeEvaluationError, PageSession

EXT_FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "ext.html"
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
    session.navigate(EXT_FIXTURE.as_uri())
    yield session
    session.close()
    connection.close()


def arg(selector: str) -> dict:
    return {"s": "e", "v": selector}


class TestExtLocator:
    def test_xtype_lookup(self, page):
        assert page.call("exists", arg("grid")) is not None

    def test_item_id_lookup(self, page):
        found = page.call("getText", arg("#save"))
        assert found == {"ok": True, "text": "Save"}

    def test_attribute_match(self, page):
        found = page.call("getText", arg("button[text=Cancel]"))
        assert found == {"ok": True, "text": "Cancel"}

    def test_descendant_chain(self, page):
        # A button inside the grid — chain matching.
        found = page.call("getText", arg("grid[itemId=users] button[text=Save]"))
        assert found == {"ok": True, "text": "Save"}
        # The toolbar button is queried separately.
        assert page.call("exists", arg("toolbar button[text=New]")) is not None

    def test_click_through_registry(self, page):
        assert page.call("click", arg("button[text=Save]")) == {"ok": True}

    def test_count_reports_registry_matches(self, page):
        assert page.call("count", arg("button")) == {"count": 3}

    def test_no_match_returns_not_found(self, page):
        assert page.call("exists", arg("button[text=Nope]")) is None

    def test_missing_ext_raises_explicit_error(self, chrome_process):
        connection = CdpConnection(chrome_process.ws_url)
        session = PageSession(connection).attach()
        session.navigate(DEMO.as_uri())  # a page without Ext
        try:
            with pytest.raises(BridgeEvaluationError, match="window.Ext"):
                session.call("exists", arg("button"))
        finally:
            session.close()
            connection.close()
