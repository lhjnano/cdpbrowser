"""CdpBrowser — a framework-independent pure-CDP browser automation core.

The core (ChromeProcess, CdpConnection, PageSession, and everything under
``cdpbrowser.*``) is plain Python + ``websockets`` with **no framework
dependency** — usable from scripts, pytest, or any tool.

The Robot Framework keyword adapter (``cdpbrowser.CdpBrowser``) is optional:
install it with ``pip install 'cdpbrowser[robot]'``.
"""

from importlib.util import find_spec as _find_spec

__version__ = "0.1.0"

#: Guidance shown when the Robot adapter is accessed without robotframework.
ROBOT_INSTALL_HINT = (
    "cdpbrowser.CdpBrowser is the optional Robot Framework adapter — "
    "install robotframework with: pip install 'cdpbrowser[robot]'. "
    "The framework-free core (PageSession & friends) needs no extras."
)


def _robot_available() -> bool:
    """Whether ``robotframework`` is importable (tolerates broken installs)."""
    try:
        return _find_spec("robot") is not None
    except Exception:  # noqa: BLE001 — a raising finder means "unavailable"
        return False


# --- Core exports: eager, framework-free --------------------------------
from .cdp.chrome import ChromeProcess, find_chrome  # noqa: E402
from .cdp.transport import CdpConnection  # noqa: E402
from .page import PageSession  # noqa: E402


# --- Robot adapter: lazy, guarded (PEP 562) ------------------------------
def __getattr__(name):
    if name == "CdpBrowser":
        if not _robot_available():
            raise ImportError(ROBOT_INSTALL_HINT)
        try:
            from .library import CdpBrowser
        except ImportError as exc:
            raise ImportError(ROBOT_INSTALL_HINT) from exc
        return CdpBrowser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return [
        "__version__",
        "ChromeProcess",
        "find_chrome",
        "CdpConnection",
        "PageSession",
        "CdpBrowser",
    ]


__all__ = [
    "__version__",
    "ChromeProcess",
    "find_chrome",
    "CdpConnection",
    "PageSession",
    "CdpBrowser",
]
