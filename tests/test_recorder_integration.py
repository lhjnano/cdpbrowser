"""Recorder real-Chrome integration — record real input, replay the replay.

Flow: start recording -> drive demo.html with REAL input (mouse + keys) ->
stop -> render a Robot replay file -> execute that file with robot as a
subprocess and assert it passes. This is the full record->replay proof.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.page import PageSession
from cdpbrowser.recorder import render_summary, to_robot_script

DEMO = Path(__file__).resolve().parent.parent / "atest" / "fixtures" / "demo.html"


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


class TestRecording:
    def test_records_navigate_click_fill_press(self, page):
        page.start_recording()
        page.navigate(DEMO.as_uri())
        # Real user-input track — the recorder listens to genuine DOM events.
        page.type_text({"s": "id", "v": "name-input"}, "Jane Doe")
        page.press_special_key("ENTER")
        page.real_click_element({"s": "id", "v": "swap-button"})
        events = page.stop_recording()
        summary = render_summary(events)
        assert "1 navigate" in summary
        assert "1 click" in summary
        assert "1 fill" in summary
        assert "1 press" in summary

        kinds = [e["type"] for e in events]
        assert kinds[0] == "navigate"
        assert "fill" in kinds and "click" in kinds and "press" in kinds

        fill = next(e for e in events if e["type"] == "fill")
        assert fill["selector"] == "testid:name-input"
        assert fill["value"] == "Jane Doe"

        click = next(e for e in events if e["type"] == "click")
        assert click["selector"] == "testid:swap-button"

    def test_selector_derivation_prefers_testid(self, page):
        page.navigate(DEMO.as_uri())
        page.start_recording()
        page.real_click_element({"s": "id", "v": "swap-button"})
        events = page.stop_recording()
        click = events[-1]
        assert click["selector"].startswith("testid:")

    def test_typed_burst_collapses_into_one_fill(self, page):
        page.navigate(DEMO.as_uri())
        page.start_recording()
        page.type_text({"s": "id", "v": "name-input"}, "abcdef")
        events = page.stop_recording()
        fills = [e for e in events if e["type"] == "fill"]
        assert len(fills) == 1
        assert fills[0]["value"] == "abcdef"

    def test_recording_is_inert_after_stop(self, page):
        page.navigate(DEMO.as_uri())
        page.start_recording()
        page.real_click_element({"s": "id", "v": "swap-button"})
        page.stop_recording()
        page.real_click_element({"s": "id", "v": "vanish-button"})
        # The list keeps only what was captured while recording was on.
        events = page.recording_events()
        clicks = [e for e in events if e["type"] == "click"]
        assert len(clicks) == 1


class TestReplay:
    def test_generated_robot_file_replays_green(self, page, tmp_path):
        """The full loop: record a real flow -> render -> run the replay."""
        page.start_recording()
        page.navigate(DEMO.as_uri())
        page.type_text({"s": "id", "v": "name-input"}, "replay me")
        page.real_click_element({"s": "id", "v": "swap-button"})
        events = page.stop_recording()

        replay = tmp_path / "replay.robot"
        replay.write_text(to_robot_script(events, name="Recorded Replay"), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "robot",
                "--outputdir",
                str(tmp_path / "out"),
                str(replay),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert "1 passed" in result.stdout, result.stdout + result.stderr
