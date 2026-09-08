"""Visual regression — screenshot baseline comparison engine.

Pillow is an **optional** dependency (``pip install cdpbrowser[visual]``):

- With Pillow: per-pixel delta tolerance, mismatch ratio, ``.diff.png``
  artifact.
- Without Pillow: **downgraded to exact-match** — byte-equality only.
  Requiring tolerance or a diff artifact raises
  :class:`VisualUnsupportedError` with install guidance.

Comparison contract:

- ``pixel_delta``: maximum tolerated per-channel (RGBA) delta (0-255).
  A pixel counts as "mismatched" if any channel exceeds it.
- ``max_mismatch_ratio``: upper bound (0.0-1.0) on
  mismatched pixels / total pixels.
- PASS requires satisfying both conditions.
- A size difference counts as a full mismatch (the diff image is shown
  side by side).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__all__ = [
    "VisualUnsupportedError",
    "VisualDiff",
    "images_equal",
    "compare_images",
    "render_diff_png",
    "load_png",
    "require_pillow",
]


class VisualUnsupportedError(RuntimeError):
    """Raised when tolerance/diff features are requested without Pillow."""


def _pil():
    try:
        from PIL import Image  # noqa: PLC0415 — lazy import of optional dep
    except ImportError as exc:
        raise VisualUnsupportedError(
            "visual comparison features require Pillow — "
            "install with: pip install 'cdpbrowser[visual]'"
        ) from exc
    return Image


def require_pillow() -> None:
    """Check Pillow availability explicitly (guidance error if missing)."""
    _pil()


def load_png(data: bytes):
    """Decode PNG bytes into a Pillow image."""
    return _pil().open(io.BytesIO(data)).convert("RGBA")


@dataclass
class VisualDiff:
    """Comparison result.

    Attributes:
        equal: Whether the images match within tolerance.
        mismatched_pixels: Count of mismatched pixels (total pixel count on
            a size mismatch).
        total_pixels: Total number of compared pixels.
        size_mismatch: Whether the two image sizes differ.
        region: Bounding box (x0, y0, x1, y1) of the mismatched-pixel cluster,
            or None.
        rows_affected: Number of rows containing mismatched pixels (vertical
            spread).
        cols_affected: Number of columns containing mismatched pixels
            (horizontal spread).
    """

    equal: bool
    mismatched_pixels: int = 0
    total_pixels: int = 0
    size_mismatch: bool = False
    region: Optional[tuple] = None
    rows_affected: int = 0
    cols_affected: int = 0
    _diff_png: Optional[bytes] = field(default=None, repr=False)

    @property
    def ratio(self) -> float:
        if not self.total_pixels:
            return 0.0
        return self.mismatched_pixels / self.total_pixels

    def summary(self) -> str:
        """Human-readable one-line summary — for logs/AssertionError messages."""
        if self.size_mismatch:
            return (
                f"image sizes differ ({self.mismatched_pixels} mismatched "
                f"of {self.total_pixels} px)"
            )
        if self.region:
            x0, y0, x1, y1 = self.region
            box = f"region x:{x0}-{x1} y:{y0}-{y1}"
        else:
            box = "no region"
        return (
            f"{self.mismatched_pixels} mismatched px "
            f"({self.ratio:.3%} of {self.total_pixels}) — {box}"
        )

    def diagnose(self) -> str:
        """Classify the shape of the mismatch **in words** — guess the cause
        without opening the screenshot.

        The three patterns are different bugs:

        - ``localized``: the change is one cluster (a single element changed
          — text or icon).
        - ``global-shift``: the whole screen differs (layout/theme/background
          change).
        - ``scattered``: spread all over (font/antialiasing and rendering
          noise family).
        """
        if self.size_mismatch:
            return "image sizes differ — viewport or DPR changed between runs"
        if not self.mismatched_pixels or not self.region:
            return "no differences"
        x0, y0, x1, y1 = self.region
        box_area = max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
        fill = self.mismatched_pixels / box_area
        side = max(1, int(math.isqrt(self.total_pixels)))
        row_ratio = self.rows_affected / side
        col_ratio = self.cols_affected / side

        if self.ratio > 0.60:
            return (
                "global-shift: the whole image differs "
                f"({self.ratio:.1%} of pixels) — layout/theme/background change"
            )
        if fill > 0.75 and row_ratio < 0.5 and col_ratio < 0.5:
            return (
                f"localized: one solid region at x:{x0}-{x1} y:{y0}-{y1} "
                f"({fill:.0%} of its box) — a single element changed"
            )
        if row_ratio > 0.5 or col_ratio > 0.5:
            return (
                "scattered-wide: changes span most rows/columns "
                f"(rows {self.rows_affected}/{side}, cols {self.cols_affected}"
                f"/{side}) — font or rendering shift"
            )
        return (
            f"scattered: {self.mismatched_pixels} px across "
            f"{self.rows_affected} rows — multiple small changes"
        )


def images_equal(baseline: bytes, actual: bytes) -> bool:
    """Exact match (byte equality); works without Pillow."""
    return baseline == actual


def compare_images(
    baseline: bytes,
    actual: bytes,
    pixel_delta: int = 0,
    max_mismatch_ratio: float = 0.0,
) -> VisualDiff:
    """Compare two PNGs and return a :class:`VisualDiff`.

    Without Pillow and with default parameters (exact match), falls back to
    a byte comparison. Tolerance set without Pillow raises
    :class:`VisualUnsupportedError`.
    """
    if pixel_delta == 0 and max_mismatch_ratio <= 0.0:
        # Exact mode — path that does not need Pillow.
        if images_equal(baseline, actual):
            return VisualDiff(equal=True, total_pixels=0)
        # Content differs — enrich with Pillow, minimal info without it.
        try:
            require_pillow()
        except VisualUnsupportedError:
            return VisualDiff(equal=False)
    require_pillow()

    base = load_png(baseline)
    act = load_png(actual)
    if base.size != act.size:
        total = base.size[0] * base.size[1]
        return VisualDiff(
            equal=False,
            mismatched_pixels=total,
            total_pixels=total,
            size_mismatch=True,
        )

    width, height = base.size
    base_px, act_px = base.load(), act.load()
    threshold = int(pixel_delta)
    mismatched = 0
    min_x = min_y = 1 << 30
    max_x = max_y = -1
    rows_seen = set()
    cols_seen = set()
    mask = _pil().new("1", base.size, 0)
    mask_px = mask.load()
    for y in range(height):
        for x in range(width):
            b, a = base_px[x, y], act_px[x, y]
            if (
                abs(b[0] - a[0]) > threshold
                or abs(b[1] - a[1]) > threshold
                or abs(b[2] - a[2]) > threshold
                or abs(b[3] - a[3]) > threshold
            ):
                mismatched += 1
                mask_px[x, y] = 1
                rows_seen.add(y)
                cols_seen.add(x)
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    total = width * height
    ratio = mismatched / total if total else 0.0
    region = (
        (min_x, min_y, max_x, max_y)
        if mismatched and max_x >= min_x
        else None
    )
    diff = VisualDiff(
        equal=ratio <= max_mismatch_ratio,
        mismatched_pixels=mismatched,
        total_pixels=total,
        region=region,
        rows_affected=len(rows_seen),
        cols_affected=len(cols_seen),
    )
    diff._diff_png = _compose_diff(base, act, mask)
    return diff


def _compose_diff(base, actual, mask) -> bytes:
    """Diff PNG bytes with mismatched pixels highlighted in red."""
    red = (255, 0, 0, 255)
    blended = base.copy()
    px, base_px, act_px = blended.load(), base.load(), actual.load()
    mask_px = mask.load()
    width, height = blended.size
    for y in range(height):
        for x in range(width):
            if mask_px[x, y]:
                px[x, y] = red
    buffer = io.BytesIO()
    blended.save(buffer, format="PNG")
    return buffer.getvalue()


def render_diff_png(diff: VisualDiff) -> Optional[bytes]:
    """Return the diff artifact bytes produced by the comparison (None if absent)."""
    return diff._diff_png


def save_visual_artifacts(
    directory: Path, name: str, actual_png: bytes, diff: Optional[VisualDiff]
) -> List[Path]:
    """Save ``{name}.actual.png`` (plus ``{name}.diff.png``) artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    written = [directory / f"{name}.actual.png"]
    written[0].write_bytes(actual_png)
    if diff is not None:
        diff_bytes = render_diff_png(diff)
        if diff_bytes:
            path = directory / f"{name}.diff.png"
            path.write_bytes(diff_bytes)
            written.append(path)
    return written
