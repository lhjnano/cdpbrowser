"""PageSession + bridge.js real-Chrome integration tests.

On the launcher/transport layers built up in tests 1-3 (ChromeProcess,
CdpConnection), verifies page-session management and real bridge-primitive
round trips. file:// and data: URLs only — no external network.

- Chrome binaries are detected via ``find_chrome()``; pytest.skip when absent.
- bridge.js JS unit tests are covered by the page integration instead
  (test_bridge_unit.py is intentionally omitted).
"""

from __future__ import annotations

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.cdp.errors import CdpError
from cdpbrowser.locators import parse, to_bridge_arg
from cdpbrowser.page import (
    BridgeEvaluationError,
    NavigationError,
    PageSession,
    build_call_expression,
    load_bridge_source,
)

PAGE_ONE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>page one</title></head>
<body>
  <h1 id="title">Page One</h1>
  <span id="by-css">css-el</span>
  <b data-testid="tid-text">tid-body</b>
  <i id="x-el">xpath-el</i>
  <a href="#" id="login-link"><span>Log in</span></a>
  <form onsubmit="return false;">
    <input id="name" data-testid="name-input" value="">
    <input id="secret" style="display:none" value="hidden-value">
    <button id="go" data-testid="go-btn" type="button">Go</button>
    <button id="off" disabled type="button">Off</button>
    <div id="out"></div>
  </form>
  <script>
    var nameInput = document.getElementById('name');
    var out = document.getElementById('out');
    nameInput.addEventListener('input', function () {
      out.setAttribute('data-got-input', 'yes');
    });
    nameInput.addEventListener('change', function () {
      out.setAttribute('data-got-change', 'yes');
    });
    document.getElementById('go').addEventListener('click', function () {
      out.textContent = 'clicked:' + document.getElementById('name').value;
    });
  </script>
</body>
</html>
"""

PAGE_TWO = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>page two</title></head>
<body>
  <p id="second">second-page</p>
</body>
</html>
"""


def arg(selector: str) -> dict:
    """Converts a locators DSL string into a bridge argument (task-1 integration path)."""
    return to_bridge_arg(parse(selector))


def write_page(tmp_path, filename: str, html: str) -> str:
    path = tmp_path / filename
    path.write_text(html, encoding="utf-8")
    return path.as_uri()


# ----------------------------------------------------------------------
# Pure units (no Chrome needed)
# ----------------------------------------------------------------------


class TestExpressionBuilder:
    def test_builds_call_expression_from_json_args(self):
        expression = build_call_expression("click", [{"s": "css", "v": "#a b"}])
        assert expression == 'window.__cdpb.click({"s": "css", "v": "#a b"})'

    def test_escapes_special_characters_in_values(self):
        expression = build_call_expression("setValue", [{"s": "t", "v": 'he "said" </script>'}, "x\ny"])
        assert '"he \\"said\\" </script>"' in expression
        assert '"x\\ny"' in expression

    def test_no_args_renders_empty_call(self):
        assert build_call_expression("ping") == "window.__cdpb.ping()"

    @pytest.mark.parametrize(
        "name", ["", "a b", "a;b", "constructor", "__proto__", "prototype", "ünïcödé"]
    )
    def test_rejects_invalid_or_forbidden_names(self, name):
        with pytest.raises(ValueError):
            build_call_expression(name, [])

    def test_rejects_non_string_name(self):
        with pytest.raises(TypeError):
            build_call_expression(123)  # type: ignore[arg-type]


class TestBridgeSource:
    def test_source_loads_from_package_data(self):
        source = load_bridge_source()
        assert "window.__cdpb" in source
        assert '"use strict"' in source


# ----------------------------------------------------------------------
# Real-Chrome integration
# ----------------------------------------------------------------------


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
    yield session
    session.close()
    connection.close()


class TestBridgePrimitives:
    def test_set_value_then_click_roundtrip(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        assert page.call("setValue", arg("css:#name"), "hello") == {"ok": True}
        assert page.call("click", arg("testid:go-btn")) == {"ok": True}
        assert page.call("getText", arg("css:#out")) == {
            "ok": True,
            "text": "clicked:hello",
        }

    def test_set_value_dispatches_input_and_change_events(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        page.call("setValue", arg("testid:name-input"), "evt")
        # input/change listeners leave an attribute on #out — the React-compatible path.
        assert page.call("exists", arg("css:#out[data-got-input]")) is not None
        assert page.call("exists", arg("css:#out[data-got-change]")) is not None

    def test_state_report(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        assert page.call("exists", arg("css:#go")) == {
            "exists": True,
            "visible": True,
            "enabled": True,
        }
        assert page.call("visible", arg("css:#secret"))["visible"] is False
        assert page.call("enabled", arg("css:#off"))["enabled"] is False
        assert page.call("exists", arg("css:#does-not-exist")) is None

    def test_find_one_across_all_strategies(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        assert page.call("getText", arg("css:#by-css"))["text"] == "css-el"
        assert page.call("getText", arg("testid:tid-text"))["text"] == "tid-body"
        assert page.call("getText", arg("x://i[@id='x-el']"))["text"] == "xpath-el"
        assert page.call("getText", arg("text:Log in"))["text"] == "Log in"

    def test_text_strategy_partial_and_case_insensitive_fallback(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        # Falls back to case-insensitive partial match when no exact match exists.
        assert page.call("getText", arg("text:log"))["text"] == "Log in"

    def test_click_failure_reasons(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        missing = page.call("click", arg("css:#nope"))
        assert missing["ok"] is False
        assert missing["reason"] == "not-found"
        assert "describe" not in missing

        hidden = page.call("click", arg("css:#secret"))
        assert hidden["ok"] is False
        assert hidden["reason"] == "not-visible"
        assert hidden["describe"]["tag"] == "input"
        assert hidden["describe"]["id"] == "secret"

        disabled = page.call("click", arg("css:#off"))
        assert disabled["ok"] is False
        assert disabled["reason"] == "not-enabled"
        assert disabled["describe"]["text"] == "Off"

    def test_set_value_failure_reasons(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        missing = page.call("setValue", arg("css:#nope"), "v")
        assert missing == {"ok": False, "reason": "not-found"}

        hidden = page.call("setValue", arg("css:#secret"), "v")
        assert hidden["ok"] is False
        assert hidden["reason"] == "not-visible"
        assert hidden["describe"]["id"] == "secret"

    def test_get_text_not_found(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        assert page.call("getText", arg("css:#nope")) == {
            "ok": False,
            "reason": "not-found",
        }

    def test_bad_selector_raises_evaluation_error(self, page, tmp_path):
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        # locators.py documented rule: invalid CSS must surface as a browser-stage
        # SyntaxError (not a silent not-found).
        with pytest.raises(BridgeEvaluationError):
            page.call("exists", {"s": "css", "v": "#[invalid::css"})


class TestNavigation:
    def test_bridge_injection_survives_new_documents(self, page, tmp_path):
        # Already injected into the document current at attach() time (about:blank).
        assert page.evaluate("typeof window.__cdpb") == "object"
        page.navigate(write_page(tmp_path, "one.html", PAGE_ONE))
        assert page.evaluate("typeof window.__cdpb") == "object"
        page.navigate(write_page(tmp_path, "two.html", PAGE_TWO))
        assert page.evaluate("typeof window.__cdpb") == "object"
        assert page.call("getText", arg("css:#second"))["text"] == "second-page"

    def test_navigate_data_url(self, page):
        page.navigate("data:text/html,<h1 data-testid='h'>data-page</h1>")
        assert page.call("getText", arg("testid:h"))["text"] == "data-page"

    def test_navigate_error_text_raises(self, page):
        with pytest.raises(NavigationError, match="net::ERR_FILE_NOT_FOUND"):
            page.navigate("file:///nonexistent/cdpbrowser-missing-page.html")


class TestSessionLifecycle:
    def test_attach_reports_ids(self, page):
        assert page.attached is True
        assert isinstance(page.session_id, str) and page.session_id
        assert isinstance(page.target_id, str) and page.target_id

    def test_close_is_idempotent_and_guards_calls(self, chrome_process):
        connection = CdpConnection(chrome_process.ws_url)
        session = PageSession(connection).attach()
        target_id = session.target_id
        session.close()
        session.close()  # re-entrancy safe
        assert session.attached is False
        assert session.session_id is None
        with pytest.raises(CdpError):
            session.call("exists", arg("css:#anything"))
        # Confirm at browser level that the tab really closed.
        # Target.closeTarget is async — getTargets may keep listing a closing
        # target briefly, so poll until it disappears.
        from cdpbrowser.polling import poll_until

        def target_gone():
            targets = connection.send("Target.getTargets")
            return target_id not in [
                info["targetId"] for info in targets["targetInfos"]
            ]

        poll_until(target_gone, 5.0, 0.05, "closed target to disappear")
        connection.close()
