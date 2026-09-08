"""Tab management real-Chrome integration tests.

Verifies New Tab (isolation/index), Switch To Tab (index/title/URL
substring), and Close Tab (current tab / last-tab refusal / remainder
after switching). Tabs are told apart by the titles of two fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.library import CdpBrowser as Lib

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
def lib(chrome_process):
    browser = Lib()
    browser.open_browser()
    yield browser
    browser.close_browser()


def uri(name: str) -> str:
    return (FIXTURES / name).as_uri()


class TestTabs:
    def test_new_tab_isolates_state_and_switches(self, lib):
        lib.go_to(uri("demo.html"))
        assert "demo" in lib.get_current_url()
        index = lib.new_tab(uri("form.html"))
        assert index == 1
        # The new tab is current — form elements visible, demo elements not.
        lib.element_should_exist("testid:password")
        with pytest.raises(AssertionError):
            lib.set_timeout("0.5s")
            try:
                lib.element_should_exist("testid:swap-button")  # demo-only
            finally:
                lib.set_timeout("5s")

    def test_switch_by_index_restores_session(self, lib):
        lib.new_tab(uri("form.html"))
        lib.switch_to_tab(0)
        lib.go_to(uri("demo.html"))
        lib.switch_to_tab(1)
        lib.element_should_exist("testid:password")

    def test_switch_by_title_substring(self, lib):
        lib.new_tab(uri("upload.html"))
        lib.switch_to_tab("Upload")  # <title>upload fixtures</title>
        lib.element_should_exist("testid:file-input")

    def test_switch_by_url_substring(self, lib):
        lib.new_tab(uri("mouse.html"))
        lib.switch_to_tab("mouse.html")
        lib.element_should_exist("testid:deep-button")

    def test_close_current_switches_to_neighbor(self, lib):
        lib.go_to(uri("demo.html"))  # tab 0 becomes demo
        lib.new_tab(uri("form.html"))
        lib.close_tab()  # close the current (= form) tab
        # Switches to the previous index (0) — the demo content is intact.
        lib.element_should_exist("testid:page-title")

    def test_last_tab_refuses_to_close(self, lib):
        with pytest.raises(AssertionError, match="last open tab"):
            lib.close_tab()

    def test_unknown_identifier_fails(self, lib):
        with pytest.raises(AssertionError, match="No open tab matches"):
            lib.switch_to_tab("no-such-page")
