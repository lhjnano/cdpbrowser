"""Visual regression advanced real-Chrome integration tests — masking + diagnosis.

Masking: excluding a volatile element (a timestamp changing every render)
via mask= makes runs match; without the mask they fail. Diagnosis: a
full-background change classifies as global-shift, a local element change as localized."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.chrome import ChromeProcess, find_chrome
from cdpbrowser.library import CdpBrowser
from cdpbrowser.visual import compare_images

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
    browser.set_viewport_size(800, 600)
    browser.go_to(DEMO.as_uri())
    # Inject a volatile timestamp element — a different value every call.
    # Fixed width: mask boxes follow the element rect, so content-length
    # changes must not resize the box (recommended masking practice).
    browser.run_javascript(
        "var d=document.createElement('div');"
        "d.setAttribute('data-testid','live-timestamp');"
        "d.style.cssText='position:fixed;top:8px;right:8px;width:120px;"
        "background:#fff;padding:4px;font:12px monospace;text-align:right';"
        "d.textContent='T='+(window.__n=(window.__n||0)+1);"
        "document.body.appendChild(d); 'ok'"
    )
    yield browser
    browser.reset_viewport()
    browser.close_browser()


class TestMasking:
    def test_unmasked_volatile_element_fails(self, lib):
        browser = lib
        browser.update_baseline("vol", mask="testid:live-timestamp")
        # The baseline is the T=1 state. Forcing a new timestamp mismatches without a mask.
        browser.run_javascript(
            "document.querySelector('[data-testid=live-timestamp]')"
            ".textContent='T=9999'; 'ok'"
        )
        with pytest.raises(AssertionError, match="mismatched px"):
            browser.page_should_match_baseline("vol")

    def test_masked_volatile_element_passes(self, lib):
        browser = lib
        browser.update_baseline("vol-masked", mask="testid:live-timestamp")
        browser.run_javascript(
            "document.querySelector('[data-testid=live-timestamp]')"
            ".textContent='T=424242'; 'ok'"
        )
        browser.page_should_match_baseline("vol-masked", mask="testid:live-timestamp")

    def test_mask_covers_all_matches(self, lib):
        browser = lib
        browser.run_javascript(
            "for (var i=0;i<3;i++){var s=document.createElement('span');"
            "s.setAttribute('data-testid','live-timestamp');"
            "s.style.cssText='position:fixed;left:'+(10+i*40)+'px;bottom:6px;"
            "background:#fff;padding:2px';"
            "s.textContent='S'+i+Math.random();"
            "document.body.appendChild(s);} 'ok'"
        )
        browser.update_baseline("multi-mask", mask="testid:live-timestamp")
        browser.page_should_match_baseline("multi-mask", mask="testid:live-timestamp")

    def test_unmask_restores_dom(self, lib):
        browser = lib
        result = browser.run_javascript(
            "window.__cdpb.mask([{s:'id',v:'live-timestamp'}]); "
            "document.querySelectorAll('.__cdpb_mask__').length"
        )
        assert result == 1
        browser.run_javascript("window.__cdpb.unmask(); 0")
        remaining = browser.run_javascript(
            "document.querySelectorAll('.__cdpb_mask__').length"
        )
        assert remaining == 0


class TestDiagnosis:
    def test_whole_background_change_reads_global_shift(self, lib):
        browser = lib
        browser.update_baseline("diag")
        browser.run_javascript(
            "document.body.style.background = 'rgb(0, 90, 0)'; 'ok'"
        )
        with pytest.raises(AssertionError, match="global-shift") as excinfo:
            browser.page_should_match_baseline("diag")
        assert "layout/theme" in str(excinfo.value)

    def test_localized_change_reads_localized(self, lib):
        browser = lib
        browser.run_javascript("document.body.style.background=''; 'ok'")
        browser.update_baseline("diag-local")
        browser.run_javascript(
            "var b=document.createElement('div');"
            "b.style.cssText='position:fixed;left:100px;top:100px;"
            "width:60px;height:60px;background:#f0f';"
            "document.body.appendChild(b); 'ok'"
        )
        with pytest.raises(AssertionError, match="localized"):
            browser.page_should_match_baseline("diag-local")


class TestDiagnosisUnit:
    """Unit-verifies classification boundaries with synthetic images (no browser)."""

    def png(self, color, size=(64, 64), patch=None):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", size, color)
        if patch:
            ImageDraw.Draw(img).rectangle(patch["box"], fill=patch["color"])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_single_solid_block_is_localized(self):
        base = self.png((10, 10, 10, 255))
        changed = self.png(
            (10, 10, 10, 255), patch={"box": (10, 10, 30, 30), "color": (250, 0, 0, 255)}
        )
        diff = compare_images(base, changed)
        assert diff.diagnose().startswith("localized")

    def test_full_invert_is_global_shift(self):
        diff = compare_images(
            self.png((0, 0, 0, 255)), self.png((255, 255, 255, 255))
        )
        assert diff.diagnose().startswith("global-shift")
