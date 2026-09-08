"""cdpbrowser.cdp — Chrome DevTools Protocol transport layer.

A framework-independent pure Python module. It provides a synchronous
blocking API; internally it performs CDP WebSocket communication over
websockets (the modern asyncio API) on a dedicated background asyncio
loop thread.
"""

from .chrome import ChromeProcess, find_chrome, needs_no_sandbox, resolve_headless
from .errors import (
    CdpConnectionClosedError,
    CdpError,
    CdpRemoteError,
    CdpTimeoutError,
    ChromeLaunchError,
)
from .transport import CdpConnection, CdpEvent, parse_devtools_url

__all__ = [
    "CdpConnection",
    "CdpEvent",
    "CdpError",
    "CdpTimeoutError",
    "CdpConnectionClosedError",
    "CdpRemoteError",
    "ChromeLaunchError",
    "ChromeProcess",
    "find_chrome",
    "needs_no_sandbox",
    "resolve_headless",
    "parse_devtools_url",
]
