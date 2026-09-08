"""Pure polling engine — the shared core of the poll-by-default philosophy.

Instead of "wait then act", the library's action/assertion keywords
**retry until the deadline** (no separate Wait keywords). This module provides
just that retry loop as the pure function :func:`poll_until`, plus the timeout
exception :class:`PollTimeoutError`. It has no I/O or browser dependencies,
so it is unit-testable.

Attempt-function contract
--------------

``fn`` returns one of:

* An ``(ok, reason, describe)`` **3-tuple** — mirrors the bridge.js result
  structure. A truthy ``ok`` means success (the tuple is returned as-is);
  falsy means a retryable failure whose ``reason`` is recorded as the last
  cause, and ``describe`` (element diagnostic dict) accumulates into the
  diagnostics list when not ``None``. Identical diagnostics are not
  re-recorded — the list captures observed state changes, not retry counts.
* Any other value — truthy means success (returned as-is), falsy means retry
  (with no reason information).

Exceptions raised by ``fn`` are NOT absorbed by polling — they propagate
immediately: repeating irreversible errors such as a dropped connection until
the deadline would waste resources. Transient evaluation errors caused by
in-flight navigation are promoted by the bridge into exceptionDetails —
normalize them inside ``fn`` into ``(False, reason, None)`` when needed.

All time units are **seconds (float)**. Converting Robot time strings (such as
``"5s"``) is the caller's job (library.py, ``timestr_to_secs``).
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

__all__ = ["PollTimeoutError", "poll_until"]


class PollTimeoutError(AssertionError):
    """Raised when a condition is not met within the deadline.

    Subclasses :class:`AssertionError` so it promotes directly to a Robot
    Framework keyword failure. The message carries the description
    (``desc``), elapsed time, and last failure reason; accumulated
    diagnostics are also exposed on :attr:`diagnostics`.
    """

    def __init__(
        self,
        desc: str,
        timeout: float,
        elapsed: float,
        reason: Optional[str],
        diagnostics: List[Any],
    ) -> None:
        self.desc = desc
        self.timeout = float(timeout)
        self.elapsed = float(elapsed)
        self.reason = reason
        self.diagnostics = list(diagnostics)
        message = f"{desc}: condition not met within {self.timeout:g}s (elapsed {self.elapsed:.2f}s)"
        if reason:
            message += f" — last reason: {reason}"
        if self.diagnostics:
            message += f" — {len(self.diagnostics)} diagnostic(s) collected, last: {self.diagnostics[-1]!r}"
        super().__init__(message)


def poll_until(
    fn: Callable[[], Any],
    timeout: float,
    interval: float,
    desc: str = "condition",
) -> Any:
    """Retries ``fn`` every ``interval`` seconds until it returns success.

    Args:
        fn: zero-argument attempt function. See the module docstring for the contract.
        timeout: overall deadline in seconds (>= 0). The first attempt always runs.
        interval: retry interval in seconds (> 0). Near the deadline only the
            remaining time is slept on, preserving timeout accuracy.
        desc: condition description for the failure message (keyword name +
            selector etc.) — this determines how traceable failures are.

    Returns:
        Whatever ``fn`` returned on success (the tuple, or the raw value).

    Raises:
        ValueError: ``timeout`` is negative or ``interval`` is non-positive.
        PollTimeoutError: no success before the deadline. The exception
            carries the last reason and accumulated diagnostics.
    """
    timeout = float(timeout)
    interval = float(interval)
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0, got {timeout}")
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")

    started = time.monotonic()
    deadline = started + timeout
    last_reason: Optional[str] = None
    diagnostics: List[Any] = []
    while True:
        outcome = fn()
        if _is_triple(outcome):
            ok, reason, describe = outcome
            if ok:
                return outcome
            if reason is None:
                last_reason = None
            else:
                last_reason = reason if isinstance(reason, str) else str(reason)
            if describe is not None and describe not in diagnostics:
                diagnostics.append(describe)
        elif outcome:
            return outcome
        else:
            last_reason = None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PollTimeoutError(
                desc, timeout, time.monotonic() - started, last_reason, diagnostics
            )
        time.sleep(min(interval, remaining))


def _is_triple(outcome: Any) -> bool:
    """Returns True when the value is an ``(ok, reason, describe)`` attempt result."""
    return isinstance(outcome, tuple) and len(outcome) == 3
