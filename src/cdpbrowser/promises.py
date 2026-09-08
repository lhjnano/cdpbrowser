"""Shared Promise infrastructure — the generalized "event arrives -> handle completes" layer.

Under the CDP protocol, while a modal event such as
``Page.javascriptDialogOpening`` is open, other CDP commands on that session
are blocked. Callers must therefore **arm before the trigger** (e.g. the
click that calls ``alert()``) — and arm must return immediately.

:class:`PromiseBroker` provides this pattern in a dialog-agnostic general
form (the download Promise reuses it):

1. :meth:`arm` — subscribes to an event pattern, issues a string handle,
   and returns at once. Subscriptions are created at arm time, so there is
   no replay of past events.
2. A background dispatcher thread — consumes the subscription Queues and
   matches events to active arms. On a match it passes the ``matcher`` return
   value (the claim) to ``on_complete``, which performs the response action
   (e.g. ``Page.handleJavaScriptDialog``) and completes the Future. Because
   the response action runs on the dispatcher thread, the dialog resolves
   even while the calling thread is stuck inside the (blocking) trigger.
3. :meth:`wait` — the calling thread awaits ``Future.result(timeout)``.

   Past the deadline it raises :class:`PromiseTimeoutError`.
Events matched by no arm flow to the ``on_unclaimed`` hook — the dialog
backstop auto-dismiss is layered on that hook (the hook is not alert-specific).

Usage::

    broker = PromiseBroker(connection, session_id=sid)
    handle = broker.arm(
        "Page.javascriptDialogOpening",
        matcher=lambda event: event.params,
        on_complete=lambda event, claim: respond(claim),
    )
    # ... the calling thread runs the trigger ...
    message = broker.wait(handle, timeout=10.0)
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Dict, Optional

from .cdp.errors import CdpError
from .cdp.transport import CdpConnection, CdpEvent

__all__ = [
    "PromiseBroker",
    "PromiseCancelledError",
    "PromiseError",
    "PromiseTimeoutError",
]

_LOG = logging.getLogger(__name__)

#: Default matcher that claims any event as-is — "the next event" semantics on arm.
DefaultMatcher = Callable[[CdpEvent], Any]
#: Response action for a matched event. Its return value becomes wait()'s result.
CompleteCallback = Callable[[CdpEvent, Any], Any]
#: Hook for events matched by no arm (backstop etc.).
UnclaimedCallback = Callable[[CdpEvent], None]


class PromiseError(CdpError):
    """Base error for the Promise infrastructure."""


class PromiseTimeoutError(PromiseError):
    """``wait()`` did not see completion within the deadline."""


class PromiseCancelledError(PromiseError):
    """The arm was cancelled or the broker was closed."""


class _Arm:
    """One issued handle — bundles matcher/response action and completion Future."""

    __slots__ = (
        "handle",
        "method_pattern",
        "matcher",
        "on_complete",
        "session_id",
        "future",
    )

    def __init__(
        self,
        handle: str,
        method_pattern: str,
        matcher: Optional[DefaultMatcher],
        on_complete: Optional[CompleteCallback],
        session_id: Optional[str],
    ) -> None:
        self.handle = handle
        self.method_pattern = method_pattern
        self.matcher = matcher
        self.on_complete = on_complete
        self.session_id = session_id
        self.future: ConcurrentFuture = ConcurrentFuture()


class PromiseBroker:
    """The general broker: event subscription -> handle -> Future completion.

    Starts a background dispatcher thread on creation. Clean up with
    ``close()`` (idempotent). With multiple arms on one pattern, events are
    claimed in arm order (FIFO) — the first dialog goes to the first arm, the
    second to the next arm (or the backstop when none is left).
    """

    def __init__(
        self,
        connection: CdpConnection,
        *,
        session_id: Optional[str] = None,
        on_unclaimed: Optional[UnclaimedCallback] = None,
        poll_interval: float = 0.02,
    ) -> None:
        if not isinstance(connection, CdpConnection):
            raise TypeError(
                f"connection must be CdpConnection, got {type(connection).__name__}"
            )
        self._connection = connection
        self._default_session_id = session_id
        self._on_unclaimed = on_unclaimed
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        # method_pattern -> the shared Queue that pattern's events flow through.
        # Arms on the same pattern share one Queue (for FIFO claiming).
        self._channels: Dict[str, "queue.Queue[CdpEvent]"] = {}
        self._arms: Dict[str, _Arm] = {}
        self._completed: Dict[str, _Arm] = {}
        self._counter = itertools.count(1)
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="cdpbrowser-promises", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API (synchronous, calling thread)
    # ------------------------------------------------------------------

    def arm(
        self,
        method_pattern: str,
        *,
        matcher: Optional[DefaultMatcher] = None,
        on_complete: Optional[CompleteCallback] = None,
        session_id: Optional[str] = None,
        label: str = "promise",
    ) -> str:
        """Reserves the next matching event and returns a handle immediately.

        ``matcher(event)`` must return a claim value (anything but ``None``)
        to take the event, or ``None`` to skip it (filtering pattern-matched
        but different-content events). A ``None`` matcher claims any event.

        ``on_complete(event, claim)`` is the response action run on the
        dispatcher thread (e.g. the dialog-response CDP call). Its return
        value becomes the ``wait()`` result; exceptions propagate verbatim.
        """
        if not isinstance(method_pattern, str) or not method_pattern:
            raise ValueError(f"method_pattern must be a non-empty str, got {method_pattern!r}")
        with self._lock:
            if self._closed:
                raise PromiseError("PromiseBroker is closed; cannot arm")
            events = self._channels.get(method_pattern)
            if events is None:
                events = self._connection.subscribe(method_pattern)
                self._channels[method_pattern] = events
            handle = f"{label}-{next(self._counter)}"
            arm = _Arm(
                handle=handle,
                method_pattern=method_pattern,
                matcher=matcher,
                on_complete=on_complete,
                session_id=(
                    session_id if session_id is not None else self._default_session_id
                ),
            )
            self._arms[handle] = arm
        return handle

    def prime(self, method_pattern: str) -> None:
        """Subscribes the pattern's event channel immediately (no arm needed).

        ``arm()`` subscriptions are lazy, so events for never-armed patterns
        are lost at the transport. For ``on_unclaimed`` backstops (e.g.
        auto-dismiss of dialogs opened without an arm) to always work,
        ``prime()`` the channel at session start. Idempotent.
        """
        with self._lock:
            if self._closed:
                raise PromiseError("PromiseBroker is closed; cannot prime")
            if method_pattern not in self._channels:
                self._channels[method_pattern] = self._connection.subscribe(
                    method_pattern
                )

    def wait(self, handle: str, timeout: float) -> Any:
        """Waits for the handle to complete and returns the result.

        Raises :class:`PromiseTimeoutError` past the deadline,
        :class:`PromiseCancelledError` on cancel/broker close; ``on_complete``
        exceptions propagate verbatim. Unknown handles raise ``KeyError``.
        Waiting again on a completed handle returns the same result.
        """
        arm = self._lookup(handle)
        try:
            return arm.future.result(timeout)
        except FutureTimeoutError:
            raise PromiseTimeoutError(
                f"Promise {handle!r} did not complete within {timeout}s"
            ) from None

    def cancel(self, handle: str) -> None:
        """Cancels an active handle. No-op for completed or unknown handles."""
        with self._lock:
            arm = self._arms.pop(handle, None)
        if arm is None:
            return
        with self._lock:
            self._completed[handle] = arm
        if not arm.future.done():
            arm.future.set_exception(
                PromiseCancelledError(f"Promise {handle!r} was cancelled")
            )

    def close(self) -> None:
        """Stops the dispatcher, unsubscribes, and fails active arms.

        Idempotent.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._thread.join(self._poll_interval * 50 + 2.0)
        with self._lock:
            pending = list(self._arms.values())
            self._arms.clear()
            channels = list(self._channels.values())
            self._channels.clear()
        for arm in pending:
            if not arm.future.done():
                arm.future.set_exception(
                    PromiseCancelledError(
                        f"Promise {arm.handle!r}: broker was closed before completion"
                    )
                )
        for events in channels:
            try:
                self._connection.unsubscribe(events)
            except Exception as exc:  # noqa: BLE001 — cleanup stays quiet
                _LOG.debug("Failed to unsubscribe promise channel: %s", exc)

    # ------------------------------------------------------------------
    # Internals — dispatcher thread
    # ------------------------------------------------------------------

    def _lookup(self, handle: str) -> _Arm:
        with self._lock:
            arm = self._arms.get(handle) or self._completed.get(handle)
        if arm is None:
            raise KeyError(
                f"unknown or expired promise handle: {handle!r}"
            )
        return arm

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                channels = list(self._channels.items())
            for pattern, events in channels:
                self._drain_channel(pattern, events)
            self._stop.wait(self._poll_interval)

    def _drain_channel(
        self, pattern: str, events: "queue.Queue[CdpEvent]"
    ) -> None:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                return
            self._dispatch_event(pattern, event)

    def _dispatch_event(self, pattern: str, event: CdpEvent) -> None:
        with self._lock:
            # The channel Queue already fnmatch-filters, but different patterns
            # ("Page.*" vs exact name) can receive the same event at once, so
            # only arms registered for THIS channel pattern are candidates (no double claiming).
            arms = [
                arm
                for arm in self._arms.values()
                if arm.method_pattern == pattern and not arm.future.done()
            ]
        for arm in arms:
            if arm.session_id is not None and event.session_id != arm.session_id:
                continue  # event from another session — irrelevant to this arm
            matcher = arm.matcher or (lambda received: received.params)
            try:
                claim = matcher(event)
            except Exception as exc:  # noqa: BLE001 — matcher errors complete the arm
                self._complete(arm, error=exc)
                continue
            if claim is None:
                continue  # this arm skips — next arm or the backstop takes it
            if arm.on_complete is not None:
                try:
                    result = arm.on_complete(event, claim)
                except Exception as exc:  # noqa: BLE001 — response-action errors complete too
                    self._complete(arm, error=exc)
                else:
                    self._complete(arm, result=result)
            else:
                self._complete(arm, result=claim)
            return
        if self._on_unclaimed is not None:
            try:
                self._on_unclaimed(event)
            except Exception as exc:  # noqa: BLE001 — backstop errors never kill the loop
                _LOG.debug("on_unclaimed hook raised: %s", exc)

    def _complete(
        self, arm: _Arm, *, result: Any = None, error: Optional[BaseException] = None
    ) -> None:
        with self._lock:
            self._arms.pop(arm.handle, None)
            self._completed[arm.handle] = arm
        if arm.future.done():
            return
        if error is not None:
            arm.future.set_exception(error)
        else:
            arm.future.set_result(result)
