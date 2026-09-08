"""Top-level shim for the bare-name ``Library    CdpBrowser`` import.

Robot Framework interprets a library name without a dot as a top-level
module. The actual implementation lives in :mod:`cdpbrowser.library`; this
module is a re-export that only assists name resolution.
``Library    cdpbrowser.CdpBrowser`` behaves identically.

The adapter is optional packaging-wise: importing this shim without
robotframework installed fails with install guidance.
"""

from cdpbrowser import ROBOT_INSTALL_HINT, _robot_available

if not _robot_available():
    raise ImportError(ROBOT_INSTALL_HINT)

from cdpbrowser.library import CdpBrowser  # noqa: E402

__all__ = ["CdpBrowser"]
