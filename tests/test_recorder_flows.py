"""Recording dialogs and downloads — capture + correlated replay.

Records a click that opens a real JS dialog (arm/backstop interplay) and a
click that triggers a download, then verifies:
- the observation lands on the triggering action (correlation),
- the generated Robot replay handles both patterns and passes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession
from cdpbrowser.recorder import to_robot_script

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
def page(chrome_process):
    connection = CdpConnection(chrome_process.ws_url)
    session = PageSession(connection).attach()
    yield session
    session.close()
    connection.close()


class TestDialogCapture:
    def test_dialog_attaches_to_triggering_click(self, page):
        page.dialog_timeout = 1.0  # quick backstop during recording
        page.start_recording()
        page.navigate((FIXTURES / "alert.html").as_uri())
        # No arm — the backstop dismisses; the recorder observes the dialog.
        page.real_click_element({"s": "id", "v": "alert-button"})
        events = page.stop_recording()
        clicks = [e for e in events if e["type"] == "click"]
        assert len(clicks) == 1
        assert clicks[0]["dialog"] == "Passwords don't match"
        # The standalone observation is consumed — no dialog entries remain.
        assert all(e["type"] != "dialog" for e in events)

    def test_replay_of_dialog_flow_passes(self, page, tmp_path):
        page.dialog_timeout = 1.0
        page.start_recording()
        page.navigate((FIXTURES / "alert.html").as_uri())
        page.real_click_element({"s": "id", "v": "alert-button"})
        events = page.stop_recording()
        replay = tmp_path / "dialog_replay.robot"
        replay.write_text(to_robot_script(events, name="Dialog Replay"))
        result = subprocess.run(
            [sys.executable, "-m", "robot", "--outputdir", str(tmp_path / "out"), str(replay)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert "1 passed" in result.stdout, result.stdout + result.stderr


class TestDownloadCapture:
    def test_download_attaches_to_triggering_click(self, page, tmp_path):
        page.download_dir = tmp_path / "rec-dl"
        page.start_recording()
        page.navigate((FIXTURES / "downloads.html").as_uri())
        page.real_click_element({"s": "id", "v": "blob-download"})
        events = page.stop_recording()
        clicks = [e for e in events if e["type"] == "click"]
        assert len(clicks) == 1
        assert clicks[0]["download"] == "report.txt"
        assert all(e["type"] != "download" for e in events)

    def test_replay_of_download_flow_passes(self, page, tmp_path):
        page.download_dir = tmp_path / "rec-dl"
        page.start_recording()
        page.navigate((FIXTURES / "downloads.html").as_uri())
        page.real_click_element({"s": "id", "v": "blob-download"})
        events = page.stop_recording()
        replay = tmp_path / "download_replay.robot"
        replay.write_text(to_robot_script(events, name="Download Replay"))
        result = subprocess.run(
            [sys.executable, "-m", "robot", "--outputdir", str(tmp_path / "out2"), str(replay)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert "1 passed" in result.stdout, result.stdout + result.stderr


class TestCorrelationUnit:
    def test_observation_without_action_is_dropped(self):
        from cdpbrowser.page import PageSession

        events = [
            {"type": "dialog", "message": "orphan"},
            {"type": "navigate", "url": "https://x/"},
        ]
        correlated = PageSession._correlate_recording(events)
        assert correlated == [{"type": "navigate", "url": "https://x/"}]
