"""Visual regression engine unit tests — synthetic PNGs, no real browser needed."""

from __future__ import annotations

import io

import pytest

from cdpbrowser import visual
from cdpbrowser.visual import (
    VisualDiff,
    VisualUnsupportedError,
    compare_images,
    images_equal,
    load_png,
    render_diff_png,
    save_visual_artifacts,
)


def png_bytes(color, size=(8, 8)) -> bytes:
    from PIL import Image

    img = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def png_with_patch(base_color, patch_color, box=(2, 2, 4, 4), size=(8, 8)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", size, base_color)
    ImageDraw.Draw(img).rectangle(box, fill=patch_color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)
NEAR_RED = (253, 0, 0, 255)


class TestCompare:
    def test_identical_images_pass_exact(self):
        a = png_bytes(RED)
        assert compare_images(a, a).equal is True

    def test_different_images_fail_exact(self):
        diff = compare_images(png_bytes(RED), png_bytes(BLUE))
        assert diff.equal is False
        assert diff.mismatched_pixels == 64  # the whole 8x8
        assert diff.ratio == 1.0

    def test_pixel_delta_absorbs_minor_noise(self):
        base = png_bytes(RED)
        noisy = png_bytes(NEAR_RED)
        assert compare_images(base, noisy, pixel_delta=2).equal is True
        assert compare_images(base, noisy).equal is False  # mismatch without tolerance

    def test_ratio_limit_absorbs_small_patch(self):
        base = png_bytes(RED)
        patched = png_with_patch(RED, BLUE, box=(2, 2, 3, 3))  # 2x2=4px/64=6.25%
        assert compare_images(base, patched, max_mismatch_ratio=0.10).equal is True
        assert compare_images(base, patched, max_mismatch_ratio=0.03).equal is False

    def test_region_bounding_box(self):
        base = png_bytes(RED)
        patched = png_with_patch(RED, BLUE, box=(2, 3, 4, 5))  # inclusive → 3x3
        diff = compare_images(base, patched)
        assert diff.region == (2, 3, 4, 5)

    def test_size_mismatch_counts_all(self):
        diff = compare_images(png_bytes(RED), png_bytes(RED, size=(4, 4)))
        assert diff.equal is False
        assert diff.size_mismatch is True
        assert diff.mismatched_pixels == 64

    def test_summary_mentions_numbers_and_region(self):
        diff = compare_images(png_bytes(RED), png_with_patch(RED, BLUE, box=(2, 2, 3, 3)))
        text = diff.summary()
        assert "4 mismatched px" in text
        assert "region" in text

    def test_diff_artifact_rendered(self):
        diff = compare_images(png_bytes(RED), png_with_patch(RED, BLUE, box=(2, 2, 3, 3)))
        artifact = render_diff_png(diff)
        assert artifact is not None and artifact[:8] == b"\x89PNG\r\n\x1a\n"

    def test_artifacts_saved_to_disk(self, tmp_path):
        diff = compare_images(png_bytes(RED), png_with_patch(RED, BLUE, box=(2, 2, 3, 3)))
        written = save_visual_artifacts(tmp_path, "shot", png_bytes(RED), diff)
        names = sorted(p.name for p in written)
        assert names == ["shot.actual.png", "shot.diff.png"]


class TestNoPillowFallback:
    def test_exact_bytes_equality_without_pillow(self, monkeypatch):
        import sys

        a = png_bytes(RED)
        b = png_bytes(BLUE)
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        assert images_equal(a, a) is True
        assert images_equal(a, b) is False

    def test_tolerance_without_pillow_raises_guidance(self, monkeypatch):
        import sys

        base = png_bytes(RED)
        noisy = png_bytes(NEAR_RED)
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        with pytest.raises(VisualUnsupportedError, match=r"cdpbrowser\[visual\]"):
            compare_images(base, noisy, pixel_delta=2)

    def test_visual_diff_defaults(self):
        d = VisualDiff(equal=False)
        assert d.summary()  # produces a string even in the empty state
        assert d.ratio == 0.0
