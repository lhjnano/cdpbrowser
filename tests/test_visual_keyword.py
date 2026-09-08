"""Visual regression core real-Chrome integration tests.

Fixed viewport -> baseline creation (Update Baseline) -> identical page
PASSes -> mutated page FAILs with .actual/.diff artifacts -> tolerance
absorption path. demo.html is used unmodified and style-mutated."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.library import CdpBrowser

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
def lib(chrome_process, tmp_path, monkeypatch):
    monkeypatch.delenv("CDPBROWSER_UPDATE_BASELINES", raising=False)
    browser = CdpBrowser(baseline_dir=str(tmp_path / "baselines"))
    browser.open_browser()
    browser.set_viewport_size(800, 600)  # visual determinism — fixed viewport
    browser.go_to(DEMO.as_uri())
    yield browser, tmp_path
    browser.reset_viewport()
    browser.close_browser()


class TestBaseline:
    def test_missing_baseline_fails_with_guidance(self, lib):
        browser, _ = lib
        with pytest.raises(AssertionError, match="CDPBROWSER_UPDATE_BASELINES"):
            browser.page_should_match_baseline("no-such-shot")

    def test_update_then_match_then_fail_on_change(self, lib):
        browser, tmp_path = lib
        browser.update_baseline("stable")
        browser.page_should_match_baseline("stable")  # identical page passes
        # Mutation: change the background color.
        browser.run_javascript(
            "document.body.style.background = 'rgb(200, 0, 0)'; 'ok'"
        )
        with pytest.raises(AssertionError, match="mismatched px"):
            browser.page_should_match_baseline("stable")
        # Artifacts are left behind.
        visual_dir = tmp_path / "results" / "visual"
        # The default outputdir is results/ (process CWD). The lib fixture
        # sets nothing else, so artifacts land under results/visual/**.
        artifacts = list(Path("results/visual").rglob("stable.actual.png"))
        assert artifacts, "actual artifact should be written"
        diffs = list(Path("results/visual").rglob("stable.diff.png"))
        assert diffs, "diff artifact should be written (Pillow present)"

    def test_pixel_delta_absorbs_minor_noise(self, lib):
        browser, _ = lib
        browser.update_baseline("tolerant")
        # Subtle mutation: a text shadow — larger than local anti-aliasing deltas.
        browser.run_javascript(
            "document.body.style.textShadow = '0 0 1px rgba(0,0,0,0.05)'; 'ok'"
        )
        # Zero tolerance likely mismatches — absorption verified via the ratio cap only.
        browser.page_should_match_baseline("tolerant", max_mismatch_ratio=0.20)

    def test_env_update_mode_rewrites_and_passes(self, lib, monkeypatch):
        browser, _ = lib
        browser.update_baseline("envmode")
        browser.run_javascript(
            "document.body.style.background = 'rgb(0, 120, 0)'; 'ok'"
        )
        monkeypatch.setenv("CDPBROWSER_UPDATE_BASELINES", "1")
        browser.page_should_match_baseline("envmode")  # passes after update
        monkeypatch.delenv("CDPBROWSER_UPDATE_BASELINES")
        browser.page_should_match_baseline("envmode")  # matches the new baseline
