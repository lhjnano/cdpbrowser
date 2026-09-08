"""Real mouse track real-Chrome integration tests.

Verifies Click With Real Mouse (isTrusted + offscreen scrollIntoView),
Click At Coordinates (canvas path), Hover (genuine :hover), and Drag
(raw mouse sequence). Uses fixtures/mouse.html.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "mouse.html"


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


def log(page) -> str:
    return page.call("getText", {"s": "id", "v": "mouse-log"})["text"]


def clear_log(page):
    page.evaluate(
        "document.querySelector('[data-testid=mouse-log]').textContent = ''"
    )


class TestRealClick:
    def test_trusted_events_reach_document(self, page):
        clear_log(page)
        x, y = page.real_click_element({"s": "id", "v": "deep-button"})
        entries = log(page).split(";")
        assert any(e.startswith("mousedown ") for e in entries)
        assert any(e.startswith("click ") for e in entries)
        # The deep button's own handler also fires on trusted events.
        assert "deep true" in log(page)

    def test_offscreen_element_is_scrolled_into_view(self, page):
        # A button under a 3000px spacer — unclickable without scrollIntoView.
        page.evaluate("window.scrollTo(0, 0)")
        clear_log(page)
        page.real_click_element({"s": "id", "v": "deep-button"})
        assert "deep true" in log(page)


class TestCoordinateClick:
    def test_canvas_records_click_coordinates(self, page):
        clear_log(page)
        # Scroll the canvas into the viewport, then click its center (viewport
        # coords) — Click At Coordinates is a viewport-coordinate contract.
        page.call("scrollIntoView", {"s": "id", "v": "pad"})
        box = page.call("rect", {"s": "id", "v": "pad"})
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        assert 0 <= cy < 720, f"canvas not in viewport: {box}"
        page.real_click_at(cx, cy)
        assert "canvas " in log(page)


class TestHover:
    def test_real_mouse_hover_activates_pseudoclass(self, page):
        # :hover only triggers via real pointer movement.
        assert page.real_hover({"s": "id", "v": "hover-zone"}) is not None
        hovered = page.evaluate(
            "document.querySelector('#hover-zone:hover') !== null"
        )
        assert hovered is True


class TestDrag:
    def test_press_move_release_lands_on_target(self, page):
        clear_log(page)
        page.real_drag({"s": "id", "v": "hover-zone"}, {"s": "id", "v": "drop-zone"})
        entries = log(page).split(";")
        assert any(e.startswith("drop-at ") for e in entries)
        assert any(e.startswith("mousedown ") for e in entries)
        assert any(e.startswith("mouseup ") for e in entries)
