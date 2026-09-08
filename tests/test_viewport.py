"""Viewport / mobile emulation real-Chrome integration tests.

Verifies Set Viewport Size -> innerWidth/innerHeight reflection,
deviceScaleFactor, the mobile hint, Reset Viewport restoration, and per-tab isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

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
    session.reset_viewport()
    session.close()
    connection.close()


class TestViewport:
    def test_override_changes_layout_metrics(self, page):
        page.set_viewport(480, 800)
        size = page.evaluate("[window.innerWidth, window.innerHeight]")
        assert size[0] == 480
        assert size[1] == 800

    def test_device_scale_factor_affects_dpr(self, page):
        page.set_viewport(400, 700, device_scale_factor=2)
        assert page.evaluate("window.devicePixelRatio") == 2

    def test_mobile_flag_sets_touch_hint(self, page):
        page.set_viewport(375, 812, mobile=True)
        # The mobile hint shows up on navigator.maxTouchPoints.
        assert page.evaluate("navigator.maxTouchPoints") > 0

    def test_reset_restores_default(self, page):
        before = page.evaluate("[window.innerWidth, window.innerHeight]")
        page.set_viewport(320, 480)
        assert page.evaluate("window.innerWidth") == 320
        page.reset_viewport()
        after = page.evaluate("[window.innerWidth, window.innerHeight]")
        assert after == before

    def test_non_positive_dimensions_rejected(self, page):
        with pytest.raises(ValueError, match="positive"):
            page.set_viewport(0, 800)

    def test_viewport_is_per_tab(self, page, chrome_process):
        other = PageSession(page.connection).attach()
        other.navigate(DEMO.as_uri())
        try:
            page.set_viewport(500, 900)
            assert other.evaluate("window.innerWidth") != 500
        finally:
            other.close()
