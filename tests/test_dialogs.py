"""JS dialog real-Chrome integration tests.

Verifies the arm-before-trigger contract (Promise Next Alert -> trigger ->
Wait For), confirm/prompt response reflection, the backstop auto-dismiss
for un-armed dialogs, and Wait For timeouts. Loads fixtures/alert.html via file://.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession

FIXTURE = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "alert.html"


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


def click(page, testid):
    assert page.call("click", {"s": "id", "v": testid}) == {"ok": True}


def text(page, testid):
    return page.call("getText", {"s": "id", "v": testid})["text"]


class TestArmedDialogs:
    def test_alert_text_is_captured_on_accept(self, page):
        handle = page.arm_dialog("ACCEPT")
        click(page, "alert-button")
        assert page.wait_dialog(handle, 5.0) == "Passwords don't match"

    def test_confirm_accept_is_reflected_in_page(self, page):
        handle = page.arm_dialog("ACCEPT")
        click(page, "confirm-button")
        assert page.wait_dialog(handle, 5.0) == "Proceed?"
        assert text(page, "confirm-result") == "confirmed"

    def test_confirm_dismiss_is_reflected_in_page(self, page):
        handle = page.arm_dialog("DISMISS")
        click(page, "confirm-button")
        assert page.wait_dialog(handle, 5.0) == "Proceed?"
        assert text(page, "confirm-result") == "dismissed"

    def test_prompt_text_is_answered(self, page):
        handle = page.arm_dialog("ACCEPT", prompt_text="grace")
        click(page, "prompt-button")
        assert page.wait_dialog(handle, 5.0) == "Your name?"
        assert text(page, "prompt-result") == "name=grace"


class TestBackstop:
    def test_unarmed_dialog_is_auto_dismissed_without_deadlock(self, page):
        # A dialog opened without an arm is auto-dismissed by the backstop
        # after dialog_timeout — Click returns without deadlocking. In real
        # use, the library's Set Timeout drives this value.
        page.dialog_timeout = 1.0
        click(page, "confirm-button")
        assert text(page, "confirm-result") == "dismissed"

    def test_wait_for_times_out_when_no_trigger(self, page):
        handle = page.arm_dialog("ACCEPT")
        with pytest.raises(Exception, match="did not complete"):
            page.wait_dialog(handle, 0.3)
        page.cancel_dialog(handle)
