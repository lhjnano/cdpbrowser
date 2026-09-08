"""Cookie keyword real-Chrome integration tests.

file:// cookies are special-cased in Chrome, so a tiny threaded HTTP server
provides the http origin. Verifies Get/Set/Delete/Delete All.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.cdp.errors import CdpError
from cdpbrowser.page import PageSession

PAGE = b"<!DOCTYPE html><html><body><h1>cookie fixture</h1></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):  # stay quiet
        pass


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


@pytest.fixture(scope="module")
def http_origin():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def page(chrome_process, http_origin):
    connection = CdpConnection(chrome_process.ws_url)
    session = PageSession(connection).attach()
    session.delete_all_cookies()
    session.navigate(http_origin)
    yield session
    session.close()
    connection.close()


def names(cookies):
    return sorted(c["name"] for c in cookies)


class TestCookies:
    def test_set_and_get_roundtrip(self, page, http_origin):
        page.set_cookie(http_origin, "session", "abc123")
        cookies = page.get_cookies([http_origin])
        assert names(cookies) == ["session"]
        cookie = cookies[0]
        assert cookie["value"] == "abc123"
        assert "127.0.0.1" in cookie["domain"]

    def test_multiple_cookies_and_url_filter(self, page, http_origin):
        page.set_cookie(http_origin, "a", "1")
        page.set_cookie(http_origin, "b", "2")
        assert names(page.get_cookies([http_origin])) == ["a", "b"]
        # Filtering by an unrelated URL yields nothing.
        assert page.get_cookies(["http://example.invalid/"]) == []

    def test_optional_attributes_persist(self, page, http_origin):
        page.set_cookie(
            http_origin, "secure-one", "v", http_only=True, secure=False,
            same_site="Lax",
        )
        cookie = page.get_cookies([http_origin])[0]
        assert cookie["httpOnly"] is True
        assert cookie["sameSite"] == "Lax"

    def test_delete_cookie_by_name(self, page, http_origin):
        page.set_cookie(http_origin, "keep", "1")
        page.set_cookie(http_origin, "drop", "2")
        page.delete_cookie("drop", http_origin)
        assert names(page.get_cookies([http_origin])) == ["keep"]

    def test_delete_all_cookies(self, page, http_origin):
        page.set_cookie(http_origin, "x", "1")
        page.set_cookie(http_origin, "y", "2")
        page.delete_all_cookies()
        assert page.get_cookies([http_origin]) == []

    def test_invalid_url_fails_loudly(self, page):
        with pytest.raises(CdpError):
            page.set_cookie("not a url", "bad", "v")

    def test_page_javascript_sees_cookie(self, page, http_origin):
        page.set_cookie(http_origin, "js-visible", "42")
        page.navigate(http_origin)
        assert page.evaluate("document.cookie") == "js-visible=42"
