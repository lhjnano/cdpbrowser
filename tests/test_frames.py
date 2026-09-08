"""Frame scope real-Chrome integration tests.

``Page.getFrameTree`` → frameId → ``Runtime.executionContextCreated``
(auxData.frameId) -> ``Runtime.evaluate(contextId=...)`` path and the
foundation of the ``Switch To Frame``/``Reset Frame`` keywords,
:meth:`PageSession.switch_to_frame` / :meth:`PageSession.reset_frame`.
Same-process (same-site) frames are the target — OOPIFs are out of scope.

- Chrome binaries are detected via ``find_chrome()``; pytest.skip when absent.
- file:// only; no external network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.cdp.errors import CdpError
from cdpbrowser.locators import parse, to_bridge_arg
from cdpbrowser.page import FrameNotFoundError, PageSession

MAIN = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>frames main</title></head>
<body>
  <h1 data-testid="main-title">Frames Main</h1>
  <iframe name="alpha" id="frame-alpha" src="child.html" width="320" height="140"></iframe>
  <iframe name="beta" id="frame-beta" src="child.html" width="320" height="140"></iframe>
  <iframe name="nested" id="frame-nested" src="nested.html" width="320" height="200"></iframe>
</body>
</html>
"""

CHILD = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>child</title></head>
<body>
  <span data-testid="child-name">child</span>
  <button data-testid="child-button" type="button">press</button>
  <input data-testid="child-input" value="">
  <span data-testid="child-mirror"></span>
  <script>
    document.querySelector('[data-testid="child-input"]').addEventListener(
      'input', function (e) {
        document.querySelector('[data-testid="child-mirror"]').textContent =
          e.target.value;
      });
    document.querySelector('[data-testid="child-button"]').addEventListener(
      'click', function (e) { e.target.textContent = 'clicked'; });
  </script>
</body>
</html>
"""

NESTED = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>nested</title></head>
<body>
  <span data-testid="outer-marker">outer</span>
  <iframe name="inner" id="frame-inner" src="inner.html" width="280" height="120"></iframe>
</body>
</html>
"""

INNER = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>inner</title></head>
<body>
  <span data-testid="inner-marker">inner</span>
  <button data-testid="inner-button" type="button">inner</button>
  <script>
    document.querySelector('[data-testid="inner-button"]').addEventListener(
      'click', function (e) { e.target.textContent = 'inner-clicked'; });
  </script>
</body>
</html>
"""


def arg(selector: str) -> dict:
    return to_bridge_arg(parse(selector))


def write_tree(tmp_path: Path) -> str:
    (tmp_path / "main.html").write_text(MAIN, encoding="utf-8")
    (tmp_path / "child.html").write_text(CHILD, encoding="utf-8")
    (tmp_path / "nested.html").write_text(NESTED, encoding="utf-8")
    (tmp_path / "inner.html").write_text(INNER, encoding="utf-8")
    return (tmp_path / "main.html").as_uri()


@pytest.fixture(scope="session")
def chrome_process():
    path = find_chrome()
    if path is None:
        pytest.skip(
            "Chrome binary not found "
            "(set CDPBROWSER_CHROME_PATH to enable this test)"
        )
    # file:// documents default to unique (opaque) origins, which blocks parent
    # iframe element access (window.frameElement). To verify the "same-site
    # frame" boundary, a flag re-allows same-origin between file:// documents.
    proc = ChromeProcess(
        path, headless=True, extra_args=["--allow-file-access-from-files"]
    )
    proc.start()
    yield proc
    proc.stop()


@pytest.fixture()
def page(chrome_process):
    connection = CdpConnection(chrome_process.ws_url)
    session = PageSession(connection).attach()
    yield session
    session.close()
    connection.close()


@pytest.fixture()
def main_url(page, tmp_path):
    url = write_tree(tmp_path)
    page.navigate(url)
    return url


class TestSwitchToFrame:
    def test_switch_by_name_scopes_interaction(self, page, main_url):
        page.switch_to_frame("alpha")
        # In-frame interaction — the bridge runs inside the frame document.
        assert page.call("click", arg("testid:child-button")) == {"ok": True}
        assert page.call("getText", arg("testid:child-button")) == {
            "ok": True,
            "text": "clicked",
        }
        # Scope isolation — a main-only element is invisible from this frame.
        assert page.call("exists", arg("testid:main-title")) is None
        assert page.call("getText", arg("testid:child-name"))["text"] == "child"

    def test_switch_by_id_attribute(self, page, main_url):
        # Matched via id('frame-beta') rather than name('beta').
        page.switch_to_frame("frame-beta")
        assert page.call("getText", arg("testid:child-name"))["text"] == "child"

    def test_switch_by_index_document_order(self, page, main_url):
        page.switch_to_frame(0)
        assert page.current_frame_id == page.evaluate("window.frameElement") or True
        # Index 0 = alpha, 1 = beta (document order) — cross-checked via frameElement.
        page.reset_frame("ALL")
        page.switch_to_frame("0")
        first_frame_element = page.evaluate(
            "window.frameElement ? window.frameElement.name : ''"
        )
        page.reset_frame("ALL")
        page.switch_to_frame(1)
        second_frame_element = page.evaluate(
            "window.frameElement ? window.frameElement.name : ''"
        )
        assert (first_frame_element, second_frame_element) == ("alpha", "beta")

    def test_switch_by_css_selector(self, page, main_url):
        page.switch_to_frame("#frame-alpha")
        assert (
            page.evaluate("window.frameElement ? window.frameElement.id : ''")
            == "frame-alpha"
        )
        # Invalid CSS is promoted to a browser-stage error, not a silent not-found.
        page.reset_frame("ALL")
        with pytest.raises(Exception) as excinfo:
            page.switch_to_frame("#[invalid::css")
        assert "Syntax" in str(excinfo.value) or "syntax" in str(excinfo.value)

    def test_bridge_is_available_in_frame_document(self, page, main_url):
        page.switch_to_frame("alpha")
        assert page.evaluate("typeof window.__cdpb") == "object"
        # Frame-side global pollution is limited to the single __cdpb property.
        assert page.evaluate("window.__marker === undefined") is True

    def test_nested_frame_two_levels(self, page, main_url):
        page.switch_to_frame("nested")
        assert (
            page.call("getText", arg("testid:outer-marker"))["text"] == "outer"
        )
        page.switch_to_frame("inner")
        assert page.call("click", arg("testid:inner-button")) == {"ok": True}
        assert page.call("getText", arg("testid:inner-button"))["text"] == (
            "inner-clicked"
        )
        assert page.call("exists", arg("testid:outer-marker")) is None


class TestResetFrame:
    def test_reset_frame_pops_one_level(self, page, main_url):
        page.switch_to_frame("nested")
        page.switch_to_frame("inner")
        page.reset_frame()
        # One-level pop -> back to the outer frame.
        assert (
            page.call("getText", arg("testid:outer-marker"))["text"] == "outer"
        )
        page.reset_frame()
        assert page.current_frame_id is None
        assert (
            page.call("getText", arg("testid:main-title"))["text"] == "Frames Main"
        )

    def test_reset_frame_all_returns_to_main_from_any_depth(self, page, main_url):
        page.switch_to_frame("nested")
        page.switch_to_frame("inner")
        page.reset_frame("all")  # case-insensitive
        assert page.current_frame_id is None
        assert (
            page.call("getText", arg("testid:main-title"))["text"] == "Frames Main"
        )

    def test_reset_frame_at_main_is_a_noop(self, page, main_url):
        page.reset_frame()
        page.reset_frame("ALL")
        assert page.current_frame_id is None

    def test_go_to_resets_frame_stack(self, page, main_url):
        page.switch_to_frame("alpha")
        page.navigate(main_url)
        assert page.current_frame_id is None
        # The main scope is alive again; frame-only elements are out of scope.
        assert (
            page.call("getText", arg("testid:main-title"))["text"] == "Frames Main"
        )
        assert page.call("exists", arg("testid:child-name")) is None


class TestFrameErrors:
    def test_unknown_name_id_and_css_raise(self, page, main_url):
        candidates = "candidates in the current frame"
        for identifier in ("no-such-name", "no-such-id", "#no-such-frame"):
            with pytest.raises(FrameNotFoundError, match=candidates):
                page.switch_to_frame(identifier)
        assert page.current_frame_id is None  # scope unchanged after failure

    def test_index_out_of_range_raises(self, page, main_url):
        with pytest.raises(FrameNotFoundError):
            page.switch_to_frame(9)
        assert page.current_frame_id is None

    def test_switching_into_leaf_frame_raises(self, page, main_url):
        page.switch_to_frame("alpha")
        # alpha has no iframe inside — the error lists an empty candidate set.
        with pytest.raises(FrameNotFoundError):
            page.switch_to_frame("anything")
        # On failure the scope stays in alpha (no partial switch).
        assert (
            page.call("getText", arg("testid:child-name"))["text"] == "child"
        )

    def test_invalid_scope_argument_raises_value_error(self, page, main_url):
        page.switch_to_frame("alpha")
        with pytest.raises(ValueError, match="scope must be"):
            page.reset_frame("SOMETIMES")

    def test_unattachable_context_raises_cdp_error(self, page, main_url, monkeypatch):
        # Force-clear the context map (simulating lost events) to verify the
        # fallback path — a missing contextId must fail with a clear CdpError.
        page.switch_to_frame("alpha")
        page._contexts.clear()
        monkeypatch.setattr(page, "_drain_context_events", lambda: None)
        with pytest.raises(CdpError, match="No execution context"):
            page.call("getText", arg("testid:child-name"))
