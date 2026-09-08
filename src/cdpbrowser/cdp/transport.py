"""CDP WebSocket transport core.

Design: the Robot main thread is synchronous, so each library keeps one
dedicated background asyncio loop thread; keywords (calling threads) submit
coroutines and wait on ``Future.result(timeout)``.

Routing rules (flattened-session support):
- Responses (with ``id``): routed to the pending Future by id. In flattened
  sessions the response may carry a ``sessionId`` — id matching still wins.
- Events (with ``method``): delivered to subscriber Queues by ``method`` string
  matching. Incoming messages with a ``sessionId`` belong to that session
  (passed through with it); without one they are browser-level (sessionId=None).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import queue
import re
import threading
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .errors import (
    CdpConnectionClosedError,
    CdpError,
    CdpRemoteError,
    CdpTimeoutError,
)

DEFAULT_TIMEOUT = 30.0
_DEVTOOLS_URL_RE = re.compile(r"DevTools listening on (ws://\S+)")


def parse_devtools_url(stderr_text: str) -> Optional[str]:
    """Extracts the ``DevTools listening on ws://...`` URL from Chrome stderr.

    Kept as a module function so the Chrome launcher can reuse it.
    Returns None when not found.
    """
    match = _DEVTOOLS_URL_RE.search(stderr_text)
    return match.group(1) if match else None


@dataclass(frozen=True)
class CdpEvent:
    """A received CDP event.

    ``session_id`` None means a browser(-endpoint)-level event; a string means
    the event belongs to that flattened session.
    """

    method: str
    params: Dict[str, Any]
    session_id: Optional[str] = None


class CdpConnection:
    """A CDP WebSocket connection with a synchronous blocking API.

    Connects immediately in the constructor; on failure the exception propagates
    and internal resources are cleaned up. Close instances with ``close()`` (idempotent).
    """

    def __init__(
        self,
        ws_url: str,
        *,
        connect_timeout: float = 15.0,
        max_size: Optional[int] = None,
    ) -> None:
        self._ws_url = ws_url
        self._closed = False
        self._close_lock = threading.Lock()

        self._next_id = 0
        # id allocation + pending-table guard (accessed from loop and calling threads)
        self._id_lock = threading.Lock()
        self._pending: Dict[int, asyncio.Future] = {}

        self._subs_lock = threading.Lock()
        self._subscriptions: List[Tuple[str, "queue.Queue[CdpEvent]"]] = []

        # asyncio primitives bind to the loop on first use, so creating them here is safe (py3.10+)
        self._send_lock = asyncio.Lock()
        self._recv_task: Optional[asyncio.Task] = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="cdpbrowser-cdp-loop",
            daemon=True,
        )
        self._thread.start()
        try:
            self._conn = self._submit(
                self._open_connection(connect_timeout, max_size)
            ).result(connect_timeout + 10.0)
            self._submit(self._start_recv()).result(10.0)
        except BaseException:
            self.close()
            raise

    async def _open_connection(
        self, connect_timeout: float, max_size: Optional[int]
    ) -> Any:
            # websockets.asyncio.client.connect() returns an awaitable, not a coroutine
            # function — wrap it in a real coroutine for run_coroutine_threadsafe.
        return await connect(
            self._ws_url, max_size=max_size, open_timeout=connect_timeout
        )

    # ------------------------------------------------------------------
    # Public API (synchronous, runs on calling threads)
    # ------------------------------------------------------------------

    @property
    def ws_url(self) -> str:
        return self._ws_url

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[Dict[str, Any]]:
        """Sends a CDP command and synchronously waits for the ``result``.

        When ``session_id`` is given the payload carries a ``sessionId`` field
        (flattened session). Raises :class:`CdpTimeoutError` past the deadline,
        :class:`CdpRemoteError` when the browser answers with an error, and
        :class:`CdpConnectionClosedError` when the connection is closed.
        """
        if self._closed:
            raise CdpConnectionClosedError(
                "CDP connection is closed; cannot send %r" % (method,)
            )
        future = self._submit(self._send(method, params, session_id, timeout))
        try:
            return future.result(timeout + 10.0)
        except FutureTimeoutError as exc:
                    # The loop thread itself is unresponsive — liveness backstop.
            raise CdpTimeoutError(
                f"No result within {timeout + 10.0:.1f}s for {method!r} "
                "(event loop unresponsive)"
            ) from exc
        except RuntimeError as exc:
            raise CdpConnectionClosedError(
                f"CDP event loop is no longer running while sending {method!r}"
            ) from exc

    def subscribe(self, method_pattern: str) -> "queue.Queue[CdpEvent]":
        """Subscribes to events. Only events whose ``method`` matches reach this Queue.

        Accepts exact method names as well as fnmatch wildcards such as
        ``"Page.*"``. Events from other domains never mix in.
        """
        q: "queue.Queue[CdpEvent]" = queue.Queue()
        with self._subs_lock:
            self._subscriptions.append((method_pattern, q))
        return q

    def unsubscribe(self, q: "queue.Queue[CdpEvent]") -> None:
        """Unsubscribes — no further events are delivered to this Queue."""
        with self._subs_lock:
            self._subscriptions = [
                (pattern, sub_q)
                for pattern, sub_q in self._subscriptions
                if sub_q is not q
            ]

    def close(self, timeout: float = 10.0) -> None:
        """Cleans up the connection and the background loop. Idempotent."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        error = CdpConnectionClosedError("CDP connection closed by close()")
        try:
            self._submit(self._shutdown_connection()).result(timeout)
        except Exception:  # noqa: BLE001 — cleanup paths never block callers
            pass
        self._fail_pending(error)
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout)
        try:
            self._loop.close()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Internals — coroutines running on the loop thread
    # ------------------------------------------------------------------

    def _submit(self, coro: Any) -> ConcurrentFuture:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _start_recv(self) -> None:
        self._recv_task = self._loop.create_task(self._recv_loop())

    async def _send(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        session_id: Optional[str],
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        with self._id_lock:
            self._next_id += 1
            message_id = self._next_id
        future: asyncio.Future = self._loop.create_future()
        with self._id_lock:
            self._pending[message_id] = future
        payload: Dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id
        try:
            try:
                async with self._send_lock:
                    await self._conn.send(json.dumps(payload))
            except ConnectionClosed as exc:
                raise CdpConnectionClosedError(
                    f"Connection closed while sending {method!r}"
                ) from exc
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError as exc:
                raise CdpTimeoutError(
                    f"Timed out after {timeout}s waiting for a response to {method!r}"
                ) from exc
        finally:
            with self._id_lock:
                self._pending.pop(message_id, None)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._conn:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — no receive-loop error kills the process
            pass
        finally:
            self._fail_pending(
                CdpConnectionClosedError("CDP connection was closed by the peer")
            )

    async def _shutdown_connection(self) -> None:
        task = self._recv_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        connection = getattr(self, "_conn", None)
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if "id" in message:
            self._dispatch_response(message)
            return
        method = message.get("method")
        if isinstance(method, str) and method:
            self._dispatch_event(message, method)

    def _dispatch_response(self, message: Dict[str, Any]) -> None:
        message_id = message["id"]
        with self._id_lock:
            future = self._pending.get(message_id)
        if future is None or future.done():
            return  # unknown or already-timed-out id — ignore
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                CdpRemoteError(
                    error.get("code"),
                    error.get("message", ""),
                    error.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    def _dispatch_event(self, message: Dict[str, Any], method: str) -> None:
        event = CdpEvent(
            method=method,
            params=message.get("params") or {},
            session_id=message.get("sessionId"),
        )
        with self._subs_lock:
            subscriptions = list(self._subscriptions)
        for pattern, q in subscriptions:
            if fnmatch.fnmatchcase(method, pattern):
                q.put_nowait(event)

    def _fail_pending(self, error: CdpError) -> None:
        """Fails every pending command Future with the given error.

        Safe from both the loop thread and calling threads; never raises even
        when the loop is already stopped.
        """
        with self._id_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if future.done():
                continue
            try:
                self._loop.call_soon_threadsafe(future.set_exception, error)
            except RuntimeError:
                pass
