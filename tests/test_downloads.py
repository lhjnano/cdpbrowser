"""File download real-Chrome integration tests.

Verifies the Promise Next Download (arm) -> trigger -> Wait For (path)
pattern, allowAndName saving, consecutive downloads, and timeouts,
using the Blob download buttons of fixtures/downloads.html.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURE = (
    Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "downloads.html"
)


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
def page(chrome_process, tmp_path):
    connection = CdpConnection(chrome_process.ws_url)
    session = PageSession(connection).attach()
    session.download_dir = tmp_path / "downloads"
    session.navigate(FIXTURE.as_uri())
    yield session
    session.close()
    connection.close()


def click(page, testid):
    assert page.call("click", {"s": "id", "v": testid}) == {"ok": True}


class TestDownloads:
    def test_completed_download_returns_real_file_path(self, page):
        handle = page.arm_download()
        click(page, "blob-download")
        path = Path(page.wait_download(handle, 10.0))
        assert path.name == "report.txt"
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == "quarterly report body"

    def test_two_sequential_downloads_resolve_independently(self, page):
        first = page.arm_download()
        click(page, "blob-download")
        path_one = Path(page.wait_download(first, 10.0))

        second = page.arm_download()
        click(page, "blob-download-2")
        path_two = Path(page.wait_download(second, 10.0))

        assert path_one.name == "report.txt"
        assert path_two.name == "notes.txt"
        assert path_two.read_text(encoding="utf-8") == "meeting notes body"

    def test_wait_times_out_without_trigger(self, page):
        from cdpbrowser.promises import PromiseTimeoutError

        handle = page.arm_download()
        with pytest.raises(PromiseTimeoutError):
            page.wait_download(handle, 0.3)
        page.cancel_download(handle)

    def test_download_dir_is_created_and_used(self, page, tmp_path):
        expected_dir = tmp_path / "downloads"
        handle = page.arm_download()  # start() creates the dir + sets download behavior
        click(page, "blob-download")
        path = Path(page.wait_download(handle, 10.0))
        assert path.parent == expected_dir
