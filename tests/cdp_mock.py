"""A mock CDP WebSocket server for tests.

Runs the modern websockets 17.x asyncio server API
(``websockets.asyncio.server.serve``) on its own thread + loop. Modes:

- default (echo): replies to commands with ``{"id", "sessionId", "result": ...echo...}``
- ``Mock.emit``: broadcasts the given params as an event to all clients
- ``Mock.fail``: replies in the CDP ``error``-field form (raises CdpRemoteError)
- ``ignore_ids=True``: drops every id -> client timeout
- ``close_immediately=True``: closes right after accepting -> CdpConnectionClosedError
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from typing import Any, Dict, List, Optional, Set

from websockets.asyncio.server import ServerConnection, serve


def free_port() -> int:
    """Returns an available TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockCdpServer:
    """A tiny CDP server running on a background thread."""

    def __init__(
        self,
        *,
        ignore_ids: bool = False,
        close_immediately: bool = False,
    ) -> None:
        self.ignore_ids = ignore_ids
        self.close_immediately = close_immediately
        # method -> delay seconds. Delays responses accordingly.
        # (For timeout scenarios such as late-response cleanup.)
        self.delay_methods: Dict[str, float] = {}
        self.seen_ids: List[int] = []
        self.port = free_port()

        self._clients: Set[ServerConnection] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mock-cdp-server", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(10.0):
            raise RuntimeError("mock CDP server failed to start")

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------
    # Server thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        self._stop_evt = asyncio.Event()
        server = await serve(self._handler, "127.0.0.1", self.port, max_size=None)
        self._ready.set()
        await self._stop_evt.wait()
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        server.close()
        await server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        try:
            if self.close_immediately:
                await asyncio.sleep(0.02)
                await ws.close()
                return
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self.ignore_ids:
                    continue
                await self._handle_command(ws, message)
        except Exception:  # noqa: BLE001 — handler errors must not kill the thread
            pass
        finally:
            self._clients.discard(ws)

    async def _handle_command(self, ws: ServerConnection, message: Dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is None:
            return
        self.seen_ids.append(message_id)
        method = message.get("method", "")
        params = message.get("params")
        session_id = message.get("sessionId")

        if method == "Mock.emit":
            emit = params or {}
            await self._broadcast(
                emit.get("method"), emit.get("params"), emit.get("sessionId")
            )
            await self._send_json(ws, {"id": message_id, "result": {"emitted": True}})
        elif method == "Mock.fail":
            await self._send_json(
                ws,
                {
                    "id": message_id,
                    "error": {
                        "code": -32601,
                        "message": "mock failure",
                        "data": params,
                    },
                },
            )
        else:
            delay = self.delay_methods.get(method)
            if delay is not None:
                await asyncio.sleep(delay)
            await self._send_json(
                ws,
                {
                    "id": message_id,
                    "sessionId": session_id,
                    "result": {
                        "echo_method": method,
                        "echo_params": params,
                        "echo_session_id": session_id,
                    },
                },
            )

    async def _send_json(self, ws: ServerConnection, payload: Dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001 — sends to closed sockets are ignored
            pass

    async def _broadcast(
        self,
        method: Optional[str],
        params: Optional[Dict[str, Any]],
        session_id: Optional[str],
    ) -> None:
        if method is None:
            return
        event: Dict[str, Any] = {"method": method, "params": params or {}}
        if session_id:
            event["sessionId"] = session_id
        for ws in list(self._clients):
            # Events are sent as the JSON object as-is. Passing a pre-dumped
            # string to _send_json would double-encode it, and the client would
            # parse a str instead of a dict, losing the event.
            await self._send_json(ws, event)

    # ------------------------------------------------------------------
    # Synchronous control API called from test threads
    # ------------------------------------------------------------------

    def broadcast_event(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Broadcasts an event to every connected client (waits for completion)."""
        future = asyncio.run_coroutine_threadsafe(
            self._broadcast(method, params, session_id), self._loop
        )
        future.result(5.0)

    def close(self, timeout: float = 5.0) -> None:
        if self._loop is None or self._stop_evt is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_evt.set)
        except RuntimeError:
            pass
        self._thread.join(timeout)
