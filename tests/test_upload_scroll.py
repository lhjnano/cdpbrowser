"""Scroll + file upload real-Chrome integration tests.

Verifies Scroll By (real wheel events), Scroll To Element, and Upload
File (DOM.setFileInputFiles — change fires automatically).
Uses fixtures/upload.html (4000px spacer + multiple file input)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "upload.html"


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


class TestScroll:
    def test_wheel_scrolls_down_and_up(self, page):
        page.evaluate("window.scrollTo(0, 0)")
        assert page.evaluate("window.scrollY") == 0
        page.scroll_by(0, 1200)
        assert page.evaluate("window.scrollY") > 800
        page.scroll_by(0, -600)
        assert page.evaluate("window.scrollY") < 900

    def test_scroll_to_element_brings_target_into_viewport(self, page):
        page.evaluate("window.scrollTo(0, 0)")
        page.scroll_to_element({"s": "id", "v": "deep-target"})
        box = page.call("rect", {"s": "id", "v": "deep-target"})
        assert 0 <= box["y"] < 700
        assert box["y"] + box["height"] > 0


class TestUploadFile:
    def test_upload_fires_change_with_name_and_size(self, page, tmp_path):
        payload = tmp_path / "report.csv"
        payload.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        page.set_input_files('[data-testid="file-input"]', [str(payload)])
        logged = page.call("getText", {"s": "id", "v": "upload-log"})["text"]
        assert logged == f"report.csv:{payload.stat().st_size}"

    def test_multiple_files_all_attached(self, page, tmp_path):
        one = tmp_path / "one.txt"
        two = tmp_path / "two.txt"
        one.write_text("111", encoding="utf-8")
        two.write_text("22222", encoding="utf-8")
        page.set_input_files('[data-testid="file-input"]', [str(one), str(two)])
        logged = page.call("getText", {"s": "id", "v": "upload-log"})["text"]
        assert logged == f"one.txt:{one.stat().st_size}|two.txt:{two.stat().st_size}"
