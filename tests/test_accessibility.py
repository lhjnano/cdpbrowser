"""role:/label: accessibility locator real-Chrome integration tests.

On the launcher/session pattern of test_page.py, verifies the real round
trips of the bridge.js ARIA engine (implicit role map + simplified
accname). The fixture is atest/fixtures/a11y.html — accessibility cases
laid out with document order in mind.

- Chrome binaries are detected via ``find_chrome()``; pytest.skip when absent.
- role value grammar: ``"role"`` or ``"role name"`` (two parts split on the
  first space) — the locators.py ``role:button Save`` DSL feeds the bridge as-is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.locators import parse, to_bridge_arg
from cdpbrowser.page import PageSession

FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "a11y.html"


def arg(selector: str) -> dict:
    """Converts a locators DSL string into a bridge argument."""
    return to_bridge_arg(parse(selector))


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


class TestRoleLocator:
    def test_role_button_returns_first_in_document_order(self, page):
        # Nameless role matching is document-order first — the fixture's first button is #btn-save.
        assert page.call("getText", arg("role:button")) == {
            "ok": True,
            "text": "Save",
        }

    def test_role_link_click_navigates_hash(self, page):
        assert page.call("click", arg("role:link")) == {"ok": True}
        assert page.evaluate("location.hash") == "#detail"

    def test_role_heading_get_text(self, page):
        # The implicit role of h2 is heading — name-from-content does not
        # apply to the tag, but getText reads textContent so the title comes through.
        assert page.call("getText", arg("role:heading")) == {
            "ok": True,
            "text": "Section Title",
        }

    def test_role_combobox_exists(self, page):
        # The implicit role of select is combobox.
        assert page.call("exists", arg("role:combobox")) == {
            "exists": True,
            "visible": True,
            "enabled": True,
        }

    def test_role_checkbox_click_toggles_checked(self, page):
        assert page.call("click", arg("role:checkbox")) == {"ok": True}
        assert page.evaluate("document.getElementById('agree').checked") is True

    def test_role_two_part_name_selects_correct_button(self, page):
        # Two buttons share a role — role+name two-part matching must pick
        # the right one. Click effects are confirmed via #click-log data-clicked.
        assert page.call("click", arg("role:button Save")) == {"ok": True}
        assert page.call("exists", arg('css:#click-log[data-clicked="btn-save"]')) is not None
        assert page.call("click", arg("role:button Cancel")) == {"ok": True}
        assert page.call("exists", arg('css:#click-log[data-clicked="btn-cancel"]')) is not None

    def test_aria_label_beats_name_from_content(self, page):
        # #btn-aria's visible text is "Settings" but its accessible name is
        # the aria-label ("Admin Settings") — aria-label outranks
        # name-from-content, so "Settings" must not match.
        assert page.call("exists", arg("role:button Settings")) is None
        assert page.call("getText", arg("role:button Admin Settings")) == {
            "ok": True,
            "text": "Settings",
        }


class TestLabelLocator:
    def test_label_for_associated_input_set_value(self, page):
        # The input bound to <label for="email"> — setValue via label text.
        assert page.call("setValue", arg("label:Email"), "user@example.com") == {"ok": True}
        assert page.evaluate("document.getElementById('email').value") == "user@example.com"

    def test_label_for_disambiguates_two_inputs(self, page):
        # Two label[for]-bound inputs — each label points only at its own input.
        page.call("setValue", arg("label:Password"), "s3cret")
        assert page.evaluate("document.getElementById('pw').value") == "s3cret"
        assert page.evaluate("document.getElementById('email').value") == ""

    def test_label_placeholder_fallback(self, page):
        # An input with neither label nor aria-label, only a placeholder —
        # accname falls back that far, so the label strategy reaches it.
        assert page.call("setValue", arg("label:Search terms"), "cdp") == {"ok": True}
        assert page.evaluate("document.getElementById('ph-input').value") == "cdp"


class TestMatchingFailure:
    def test_role_no_match_click_not_found(self, page):
        # A failed match is a bare not-found — a nonexistent role.
        result = page.call("click", arg("role:unicorn"))
        assert result == {"ok": False, "reason": "not-found"}
        assert "describe" not in result

    def test_label_no_match(self, page):
        assert page.call("exists", arg("label:No Such Name")) is None
