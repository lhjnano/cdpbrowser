"""Exception hierarchy for the CDP transport layer."""

from __future__ import annotations

from typing import Any, Optional


class CdpError(Exception):
    """Base class for all errors in the cdpbrowser CDP layer."""


class CdpTimeoutError(CdpError):
    """Raised when a command response does not arrive within its deadline.

    A liveness backstop so an unresponsive browser can never block a
    keyword (the calling thread) forever.
    """


class CdpConnectionClosedError(CdpError):
    """The connection is closed or was dropped mid-communication."""


class ChromeLaunchError(CdpError):
    """Raised when starting or stopping the Chrome process fails.

    Covers all launcher-level failures: binary not found, exec failure,
    DevTools endpoint timeout, early exit, and so on.
    """


class CdpRemoteError(CdpError):
    """Remote-side (browser) error carried in the ``error`` field of a CDP response."""

    def __init__(
        self,
        code: Optional[int] = None,
        message: str = "",
        data: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        text = f"CDP error {code}: {message}"
        if data is not None:
            text += f" ({data!r})"
        super().__init__(text)
