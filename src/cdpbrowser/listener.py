"""Built-in Robot listener — per-test evidence context and auto-capture.

In Robot Framework 7, a listener object exposed on the library's
:attr:`ROBOT_LIBRARY_LISTENER` is registered automatically (API v3). It:

* ``start_test`` — initializes the per-test evidence counter/directory
  context. The ``NNNN`` numbers of ``Numbered Screenshot`` restart from 0001
  whenever the test changes.
* ``end_test`` — when the evidence mode is ``on-failure`` and the test failed,
  captures a screenshot of the last screen. Only captures while the browser is
  alive; when it is dead (already cleaned up / failed to start) it skips
  quietly — no resurrecting browsers from cleanup paths.
* ``close`` — guarantees browser cleanup at library shutdown.

Evidence file paths are ``{outputdir}/evidence/{suite}/{test}/NNNN-{label}.png``.
Resolving outputdir is the library's job
(:meth:`CdpBrowser._resolve_outputdir`); the listener only tracks context
(suite/test names).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from robot.api import logger

if TYPE_CHECKING:  # avoid circular import — not needed at runtime (duck typing).
    from .library import CdpBrowser

__all__ = ["EvidenceListener", "EvidenceTracker", "safe_name"]

ROBOT_LISTENER_API_VERSION = 3


def safe_name(text: Any) -> str:
    """Turns an arbitrary name (suite/test/label) into a filename-safe slug.

Runs of characters other than word chars, dots and hyphens (``/``, whitespace,
anything else — Hangul counts as a word character and survives) collapse into
a single underscore. Empty results normalize to ``"unnamed"``.
    """
    cleaned = re.sub(r"[^\w.-]+", "_", str(text).strip())
    return cleaned or "unnamed"


class EvidenceTracker:
    """Execution context for evidence capture (mode, suite/test dirs, counter).

    Library keywords (``Numbered Screenshot``, every-step auto-capture) and the
    listener **share one instance**, so they use the same number sequence.
    """

    def __init__(self, mode: str = "on-failure") -> None:
        self.mode = mode
        self.suite_dir = "unknown-suite"
        self.test_dir = "unknown-test"
        self._counter = 0

    def start_test(self, suite_name: str, test_name: str) -> None:
        """Resets to a fresh test context — numbering restarts from 0001."""
        self.suite_dir = safe_name(suite_name)
        self.test_dir = safe_name(test_name)
        self._counter = 0

    def next_number(self) -> int:
        """Issues the next capture ordinal (starts at 1, reset per test)."""
        self._counter += 1
        return self._counter


class EvidenceListener:
    """The built-in listener of ``CdpBrowser`` (library listener API v3).

    The library exposes this object in ``__init__`` via
    ``self.ROBOT_LIBRARY_LISTENER = EvidenceListener(self)``; the library
    reference is injected via the constructor (no cdpbrowser imports here).
    """

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, library: "CdpBrowser") -> None:
        self._library = library

    # ------------------------------------------------------------------
    # Listener callbacks (v3 signatures from robot/api/interfaces.py)
    # ------------------------------------------------------------------

    def start_test(self, data: Any, result: Any) -> None:
        suite = getattr(getattr(data, "parent", None), "name", None) or "unknown-suite"
        test = getattr(data, "name", None) or "unknown-test"
        self._library._evidence.start_test(suite, test)
        logger.debug(f"Evidence context started: {suite!r} / {test!r}")

    def end_test(self, data: Any, result: Any) -> None:
        failed = bool(getattr(result, "failed", False)) or (
            getattr(result, "status", None) == "FAIL"
        )
        if not failed:
            return
        # Spec contract: automatic failure capture only in on-failure mode.
        # (every-step already leaves a screenshot of the last screen.)
        if self._library._evidence.mode != "on-failure":
            return
        self._library._auto_capture("failure")

    def close(self) -> None:
        """Library shutdown — guarantees browser cleanup (idempotent)."""
        try:
            self._library.close_browser()
        except Exception as exc:  # noqa: BLE001 — shutdown paths never raise
            logger.debug(f"Ignoring error during listener close: {exc}")
