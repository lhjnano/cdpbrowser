"""Page (page-target) session — automatic bridge injection and primitive calls.

:class:`PageSession` corresponds to one browser tab (page target).
On top of a browser-level :class:`CdpConnection` it:

1. creates a new tab with ``Target.createTarget`` (about:blank),
2. obtains a flattened session via ``Target.attachToTarget``(flatten=True), then
3. registers bridge.js with ``Page.addScriptToEvaluateOnNewDocument`` so every
   subsequent document (including navigations) gets the bridge injected.

The document current at attach() time (about:blank) is also evaluated with
the bridge immediately, so primitives work without any navigation.

``call(name, *args)`` runs ``window.__cdpb.<name>(...)`` via
``Runtime.evaluate`` (awaitPromise + returnByValue) and returns the JSON
value as-is. Page-side JS exceptions are promoted to
:class:`BridgeEvaluationError`.

JS dialogs (alert/confirm/prompt): under the CDP protocol, **while a dialog
is open, other CDP commands on that session are blocked**. Therefore
:meth:`PageSession.arm_dialog` must precede the trigger, and arm returns
immediately. The response (``Page.handleJavaScriptDialog``) is performed by
the Promise broker's background dispatcher, so it never races the calling
thread running the trigger via a blocking evaluate. Dialogs opened without
an arm are auto-dismissed by the :class:`DialogCoordinator` backstop after
``dialog_timeout`` (deadlock prevention).

Usage::

    session = PageSession(connection).attach()
    try:
        session.navigate("file:///tmp/page.html")
        result = session.call("click", {"s": "css", "v": "#submit"})
    finally:
        session.close()
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
from importlib.resources import files as _resource_files
from pathlib import Path as _Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .cdp.errors import CdpError, CdpTimeoutError
from .cdp.transport import CdpConnection, CdpEvent, DEFAULT_TIMEOUT
from .keys import KEY_EVENTS, char_events, resolve_key_token
from .promises import PromiseBroker, PromiseError

__all__ = [
    "BridgeEvaluationError",
    "DialogCoordinator",
    "DownloadCoordinator",
    "FrameNotFoundError",
    "NavigationError",
    "PageSession",
    "build_call_expression",
    "load_bridge_source",
    "normalize_dialog_action",
]

_LOG = logging.getLogger(__name__)

#: JS identifiers (Object members) forbidden as bridge method names.
_FORBIDDEN_NAMES = frozenset(
    {"__proto__", "constructor", "prototype", "__defineGetter__", "__defineSetter__"}
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")

_BRIDGE_RESOURCE = "bridge.js"
_BRIDGE_SOURCE: Optional[str] = None

#: Lists the frame-creating elements (iframe/frame) of the current frame's document,
#: in document order. name/id attributes feed selector-DSL identifier matching;
_ENUMERATE_FRAME_ELEMENTS = (
    "(function(){var els=document.querySelectorAll('iframe,frame');"
    "var out=[];for(var i=0;i<els.length;i++){"
    "out.push({name:els[i].getAttribute('name')||'',"
    "id:els[i].getAttribute('id')||''});}return out;})()"
)

#: Finds a frame-creating element itself by CSS selector, returning its document-order index.
#: Invalid CSS syntax surfaces as a querySelectorAll-stage SyntaxError.
#: The selector is substituted as a string literal where the ``__SELECTOR__``
#: token sits — str.format is NOT used because it treats JS object braces as fields.
_LOCATE_FRAME_BY_CSS = (
    "(function(sel){var matched=document.querySelectorAll(sel);"
    "var els=document.querySelectorAll('iframe,frame');"
    "for(var i=0;i<matched.length;i++){"
    "var idx=Array.prototype.indexOf.call(els,matched[i]);"
    "if(idx>=0){return idx;}}return -1;})(__SELECTOR__)"
)


def load_bridge_source() -> str:
    """Reads the package data (bridge.js). Cached after the first call."""
    global _BRIDGE_SOURCE
    if _BRIDGE_SOURCE is None:
        _BRIDGE_SOURCE = (_resource_files("cdpbrowser") / _BRIDGE_RESOURCE).read_text(
            encoding="utf-8"
        )
    return _BRIDGE_SOURCE


def load_resource_js(name: str) -> str:
    """Reads a bundled JS resource (bridge.js, recorder.js) by file name."""
    return (_resource_files("cdpbrowser") / name).read_text(encoding="utf-8")


class BridgeEvaluationError(CdpError):
    """``Runtime.evaluate`` failed with a page-side JS exception (exceptionDetails)."""

    def __init__(self, description: str) -> None:
        self.description = description
        super().__init__(f"JavaScript evaluation failed: {description}")


class NavigationError(CdpError):
    """``Page.navigate`` returned ``errorText`` (e.g. net::ERR_*)."""


class FrameNotFoundError(CdpError):
    """``switch_to_frame`` could not find the requested frame.

    Raised when the identifier (index/name/id/css) matches no iframe element
    in the current frame. The message lists the current frame's candidates
    (name/id) to help diagnose typos.
    """


def build_call_expression(name: str, args: Sequence[Any] = ()) -> str:
    """Builds the JS expression ``window.__cdpb.<name>(<args...>)``.

    Each argument is serialized with ``json.dumps`` — a JSON string literal is
    also a valid JS string literal (non-ASCII/U+2028/2029 are \\u-escaped, so
    this is safe). ``name`` must pass identifier validation, making argument
    injection structurally impossible.

    Raises:
        TypeError: ``name`` is not a str, or an argument is not serializable.
        ValueError: ``name`` fails the identifier-syntax/forbidden-name checks.
    """
    if not isinstance(name, str):
        raise TypeError(f"bridge method name must be str, got {type(name).__name__}")
    if not _IDENTIFIER_RE.fullmatch(name) or name in _FORBIDDEN_NAMES:
        raise ValueError(f"invalid bridge method name: {name!r}")
    rendered = ", ".join(json.dumps(arg) for arg in args)
    return f"window.__cdpb.{name}({rendered})"


def _format_exception_details(details: Dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    description = (
        exception.get("description")
        or exception.get("value")
        or details.get("text")
        or "unknown JS error"
    )
    return str(description)


#: Actions accepted by ``arm_dialog`` / ``Promise Next Alert``.
DIALOG_ACTIONS = ("ACCEPT", "DISMISS")

#: Subscription pattern for ``Page.javascriptDialogOpening`` events.
_DIALOG_OPENING = "Page.javascriptDialogOpening"


def normalize_dialog_action(action: str) -> bool:
    """Maps an ``ACCEPT``/``DISMISS`` string to ``Page.handleJavaScriptDialog.accept``.

    Case and surrounding whitespace are ignored. Unknown values raise
    ``ValueError`` — validation that rejects bad requests before browser start.
    """
    normalized = str(action or "").strip().upper()
    if normalized not in DIALOG_ACTIONS:
        raise ValueError(
            f"Dialog action must be one of {', '.join(DIALOG_ACTIONS)}, got {action!r}"
        )
    return normalized == "ACCEPT"


class DialogCoordinator:
    """Per-session JS dialog coordination — arm/wait plus a backstop auto-dismiss.

    **Arm-before-trigger invariant**: while ``javascriptDialogOpening`` is open,
    CDP commands on this session (including Runtime.evaluate) are blocked, so
    arm must precede the trigger. :meth:`arm` only registers the subscription
    and issues a handle, returning immediately; the actual response
    (``Page.handleJavaScriptDialog``) is performed by the Promise broker's
    background dispatcher — it never races the calling thread running the trigger.

    Dialogs opened without an arm are handled by the backstop: if no arm claims
    the event within the seconds returned by ``timeout_provider``, it is
    auto-dismissed (accept=False) with a warning. The delayed dismiss absorbs
    the race window of arming right after the trigger, but a late arm never
    receives the already-broadcast event and times out — the contract is arm-first.
    """

    def __init__(
        self,
        connection: CdpConnection,
        session_id: str,
        *,
        timeout_provider: Callable[[], float],
    ) -> None:
        self._connection = connection
        self._session_id = session_id
        self._timeout_provider = timeout_provider
        self._broker: Optional[PromiseBroker] = None
        self._timers: List[threading.Timer] = []
        self._timers_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        """Prepares ``Page.enable`` and the event broker (idempotent).

        Without ``Page.enable`` no ``javascriptDialogOpening`` is broadcast.
        The command is idempotent, so re-calling is safe.
        """
        if self._broker is not None:
            return
        self._connection.send("Page.enable", session_id=self._session_id)
        self._broker = PromiseBroker(
            self._connection,
            session_id=self._session_id,
            on_unclaimed=self._on_unclaimed_dialog,
        )
        # Broker subscriptions are created lazily at arm time. To also handle
        # dialogs opened without an arm via the backstop, the event channel must
        # be opened here — otherwise events are lost at the transport and the timer never starts.
        self._broker.prime(_DIALOG_OPENING)

    def arm(self, action: str = "ACCEPT", prompt_text: Optional[str] = None) -> str:
        """Reserves the next dialog and returns the (string) handle immediately.

        ``action`` is ``ACCEPT`` or ``DISMISS``. ``prompt_text`` is the
        ``prompt()`` answer, passed as ``promptText`` on ``ACCEPT`` + prompt
        dialogs (ignored on DISMISS).
        """
        accept = normalize_dialog_action(action)
        self.start()
        assert self._broker is not None
        return self._broker.arm(
            _DIALOG_OPENING,
            matcher=self._claim_any_dialog,
            on_complete=self._make_responder(accept, prompt_text),
            label="dialog",
        )

    def wait(self, handle: str, timeout: float) -> str:
        """Waits for the armed dialog to be handled; returns the dialog text."""
        if self._broker is None:
            raise PromiseError(f"no promise armed for handle {handle!r}")
        result = self._broker.wait(handle, timeout)
        return str(result)

    def cancel(self, handle: str) -> None:
        """Cancels the arm. No-op for already-completed handles."""
        if self._broker is None:
            return
        self._broker.cancel(handle)

    def close(self) -> None:
        """Cleans up pending backstop timers and the broker (idempotent)."""
        if self._closed:
            return
        self._closed = True
        with self._timers_lock:
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()
        if self._broker is not None:
            self._broker.close()
            self._broker = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_any_dialog(event: CdpEvent) -> Dict[str, Any]:
        """Claims any dialog matching the pattern at once (next-dialog semantics)."""
        return dict(event.params or {})

    def _make_responder(
        self, accept: bool, prompt_text: Optional[str]
    ) -> Callable[[CdpEvent, Dict[str, Any]], str]:
        def respond(event: CdpEvent, claim: Dict[str, Any]) -> str:
            params: Dict[str, Any] = {"accept": accept}
            if prompt_text is not None:
                params["promptText"] = str(prompt_text)
            # handleJavaScriptDialog is the only response path allowed while a
            # dialog is open — it must be sent with this session id.
            self._connection.send(
                "Page.handleJavaScriptDialog",
                params,
                session_id=self._session_id,
            )
            return str(claim.get("message") or "")

        return respond

    def _on_unclaimed_dialog(self, event: CdpEvent) -> None:
        """A dialog claimed by no arm — starts the backstop timer."""
        if self._closed:
            return
        timeout = max(0.0, float(self._timeout_provider()))
        timer = threading.Timer(timeout, self._backstop_dismiss, args=(event, timeout))
        timer.daemon = True
        with self._timers_lock:
            if self._closed:
                timer.cancel()
                return
            self._timers.append(timer)
        timer.start()

    def _backstop_dismiss(self, event: CdpEvent, waited: float) -> None:
        if self._closed:
            return
        params = event.params or {}
        kind = params.get("type") or "dialog"
        message = params.get("message") or ""
        _LOG.warning(
            "Unhandled %s dialog opened without a Promise arm — auto-dismissing "
            "after %.1fs backstop (message: %r). Arm before the trigger with "
            "arm_dialog()/Promise Next Alert to capture it instead.",
            kind,
            waited,
            message,
        )
        with self._timers_lock:
            timer = threading.current_thread()
            if timer in self._timers:
                self._timers.remove(timer)
        try:
            self._connection.send(
                "Page.handleJavaScriptDialog",
                {"accept": False},
                session_id=self._session_id,
            )
        except Exception as exc:  # noqa: BLE001 — already-handled dialogs etc. are ignored
            _LOG.debug("Backstop dismiss failed (dialog may be handled): %s", exc)


#: Download begin/progress event subscription patterns (browser-level).
_DOWNLOAD_BEGIN = "Browser.downloadWillBegin"
_DOWNLOAD_PROGRESS = "Browser.downloadProgress"


class DownloadCoordinator:
    """Per-session file download coordination — arm/wait plus guid-to-path resolution.

    Configures browser-wide download behavior with
    ``Browser.setDownloadBehavior(allowAndName)``, and wires
    ``downloadWillBegin`` (guid->suggestedFilename mapping) and
    ``downloadProgress`` (completion detection) into the Promise broker.

    Downloads need not arm before the trigger (unlike dialogs they do not
    block the session), but the same arm -> trigger -> wait order is
    recommended by convention.
    """

    def __init__(
        self,
        connection: CdpConnection,
        session_id: str,
        *,
        download_dir_provider: Callable[[], str],
    ) -> None:
        self._connection = connection
        self._session_id = session_id
        self._download_dir_provider = download_dir_provider
        self._broker: Optional[PromiseBroker] = None
        self._begin_queue: Optional["queue.Queue[CdpEvent]"] = None
        # guid -> suggestedFilename (collected from downloadWillBegin).
        self._names: Dict[str, str] = {}
        self._names_lock = threading.Lock()
        self._closed = False

    @property
    def download_dir(self) -> str:
        return self._download_dir_provider()

    def start(self) -> None:
        """Configures browser download behavior and opens the event channels (idempotent).

        ``setDownloadBehavior`` is a browser-level command (no session_id).
        Saves with ``allowAndName`` into the directory this coordinator
        manages — in-flight files are stored under the guid name and receive
        their final suggestedFilename on completion.
        """
        if self._broker is not None:
            return
        path = self._download_dir_provider()
        os.makedirs(path, exist_ok=True)
        self._connection.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "eventsEnabled": True,
                "downloadPath": path,
            },
        )
        self._begin_queue = self._connection.subscribe(_DOWNLOAD_BEGIN)
        # Download events are browser-level — they arrive without a sessionId.
        # Binding the broker to a session would hide them from arms, so we do not bind.
        self._broker = PromiseBroker(self._connection)
        self._broker.prime(_DOWNLOAD_PROGRESS)

    def arm(self) -> str:
        """Reserves the next download **completion**; returns a handle immediately.

        The matcher only claims progress events with ``state == "completed"``.
        On completion :meth:`wait` returns the saved file's full path.
        """
        self.start()
        assert self._broker is not None
        return self._broker.arm(
            _DOWNLOAD_PROGRESS,
            matcher=self._claim_completed,
            on_complete=self._resolve_file,
            label="download",
        )

    def wait(self, handle: str, timeout: float) -> str:
        """Waits for the armed download; returns the saved file path."""
        if self._broker is None:
            raise PromiseError(f"no promise armed for handle {handle!r}")
        return str(self._broker.wait(handle, timeout))

    def cancel(self, handle: str) -> None:
        """Cancels the arm. No-op for already-completed handles."""
        if self._broker is not None:
            self._broker.cancel(handle)

    def close(self) -> None:
        """Cleans up the broker and subscriptions (idempotent). Files are kept."""
        if self._closed:
            return
        self._closed = True
        if self._broker is not None:
            self._broker.close()
            self._broker = None
        if self._begin_queue is not None:
            try:
                self._connection.unsubscribe(self._begin_queue)
            except Exception:  # noqa: BLE001 — cleanup path
                pass
            self._begin_queue = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _claim_completed(self, event: CdpEvent) -> Optional[str]:
        params = event.params or {}
        if params.get("state") == "completed":
            return str(params.get("guid") or "")
        return None

    def _record_begins(self) -> None:
        """Drains the ``downloadWillBegin`` queue, recording guid->suggestedFilename."""
        if self._begin_queue is None:
            return
        while True:
            try:
                event = self._begin_queue.get_nowait()
            except queue.Empty:
                return
            params = event.params or {}
            guid = params.get("guid")
            if guid:
                with self._names_lock:
                    self._names[str(guid)] = str(params.get("suggestedFilename") or guid)

    def _resolve_file(self, event: CdpEvent, guid: str) -> str:
        """Resolves the on-disk path of a completed download (dispatcher thread).

        Despite ``allowAndName``, this Chrome build leaves the file under its
        guid name after completion — the ``downloadWillBegin``
        suggestedFilename mapping is used to **rename it directly**
        (``os.replace``); existing names are uniquified as ``name (1).ext``.
        """
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            self._record_begins()
            with self._names_lock:
                suggested = self._names.get(guid)
            directory = _Path(self._download_dir_provider())
            guid_path = directory / guid
            suggested_path = directory / suggested if suggested else None
            # This build finalized the name itself: already present under the suggested name.
            if suggested_path is not None and suggested_path.is_file():
                return str(suggested_path)
            if guid_path.is_file():
                if suggested is None:
                    return str(guid_path)  # suggested name unseen — return the guid path
                target = suggested_path
                stem, suffix = target.stem, target.suffix
                counter = 1
                while target.exists():
                    target = directory / f"{stem} ({counter}){suffix}"
                    counter += 1
                try:
                    os.replace(guid_path, target)
                except OSError as exc:
                    raise PromiseError(
                        f"failed to finalize download {guid!r} as {target}: {exc}"
                    ) from None
                return str(target)
            time.sleep(0.05)
        raise PromiseError(
            f"download {guid!r} completed but no file appeared in {directory} "
            "within 3s"
        )


class PageSession:
    """A CDP session attached to one browser tab. Synchronous blocking API.

    Mere construction sends no CDP commands. :meth:`attach` performs
    tab creation -> flattened session -> bridge-injection registration.
    """

    def __init__(self, connection: CdpConnection) -> None:
        if not isinstance(connection, CdpConnection):
            raise TypeError(
                f"connection must be CdpConnection, got {type(connection).__name__}"
            )
        self._conn = connection
        self._target_id: Optional[str] = None
        self._session_id: Optional[str] = None
        self._closed = False
        # Frame-scope state:
        #   _frame_stack — stack of active frame ids (empty list = main frame).
        #   _contexts — frameId -> Runtime.executionContextId (via auxData.frameId).
        #   _context_events — Runtime.* event subscription queue (lazy, first frame access).
        self._frame_stack: List[str] = []
        self._contexts: Dict[str, int] = {}
        self._context_events: Optional["queue.Queue"] = None
        # JS dialog coordination — created lazily on first arm.
        self._dialogs: Optional[DialogCoordinator] = None
        # Backstop dismiss wait (seconds) for dialogs opened without an arm.
        # Reflects the library's Set Timeout value.
        self._dialog_timeout: float = 5.0
        # File download coordination — created lazily on first arm_download.
        self._downloads: Optional[DownloadCoordinator] = None
        # Download directory (None -> temporary dir on first download).
        # The library sets it via the evidence convention (results/downloads/{suite}/{test}).
        self._download_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def connection(self) -> CdpConnection:
        return self._conn

    @property
    def target_id(self) -> Optional[str]:
        """id of the page target created by attach(). ``None`` before attach."""
        return self._target_id

    @property
    def session_id(self) -> Optional[str]:
        """flattened session id. ``None`` before attach or after close."""
        return self._session_id

    @property
    def attached(self) -> bool:
        return self._session_id is not None and not self._closed

    def attach(self) -> "PageSession":
        """Creates a new tab, attaches with a flattened session, registers the bridge.

        Re-calling on an attached instance does nothing and returns ``self``
        (re-entrancy safe). On failure the created tab is cleaned up first.
        """
        if self._closed:
            raise CdpError("PageSession is closed; cannot attach")
        if self._session_id is not None:
            return self

        result = self._conn.send("Target.createTarget", {"url": "about:blank"})
        target_id = (result or {}).get("targetId")
        if not target_id:
            raise CdpError(
                f"Target.createTarget did not return a targetId: {result!r}"
            )
        self._target_id = target_id
        try:
            attach_result = self._conn.send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = (attach_result or {}).get("sessionId")
            if not session_id:
                raise CdpError(
                    "Target.attachToTarget did not return a sessionId: "
                    f"{attach_result!r}"
                )
            self._session_id = session_id
            self._register_bridge()
            # Start the dialog coordinator eagerly — with lazy creation, a session
            # that never armed would lose javascriptDialogOpening events and the
            # backstop auto-dismiss would never run (no subscription at all).
            self._dialog_coordinator()
        except BaseException:
            self._session_id = None
            self._discard_target()
            self._target_id = None
            raise
        return self

    def enable(self) -> None:
        """Turns on ``Page.enable`` + ``Runtime.enable`` for this session.

        Both are idempotent commands. navigate() enables the Page domain
        itself to receive load events.
        """
        self._ensure_attached()
        self._conn.send("Page.enable", session_id=self._session_id)
        self._conn.send("Runtime.enable", session_id=self._session_id)

    def call(self, name: str, *args: Any, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Runs the bridge primitive ``window.__cdpb.<name>(*args)``.

        Evaluated via ``Runtime.evaluate``(awaitPromise=True,
        returnByValue=True); the JSON value is returned as-is. Page-side
        exceptions are promoted to :class:`BridgeEvaluationError`, command
        timeouts to :class:`CdpTimeoutError`.
        """
        expression = build_call_expression(name, args)
        return self._evaluate(
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout,
        )

    # ------------------------------------------------------------------
    # Real key input — Input.dispatchKeyEvent (physical key events)
    # ------------------------------------------------------------------

    def _dispatch_key(self, params: dict) -> None:
        self._ensure_attached()
        # keys.py defines snake_case fields, but the CDP wire is camelCase.
        wire = {
            (
                {"windows_virtual_key_code": "windowsVirtualKeyCode"}.get(k, k)
                if k != "native_virtual_key_code"
                else "nativeVirtualKeyCode"
            ): v
            for k, v in params.items()
        }
        self._conn.send(
            "Input.dispatchKeyEvent",
            wire,
            session_id=self._session_id,
        )

    def press_special_key(self, token: str) -> None:
        """Presses a special key (ENTER/TAB/ARROW_* etc.) with real keyDown/keyUp.

        The token is a :data:`cdpbrowser.keys.KEY_EVENTS` name (case-insensitive).
        Unknown tokens raise ``ValueError``.
        """
        fields = resolve_key_token(token)
        if fields is None:
            raise ValueError(
                f"unknown special key {token!r} — known: "
                + ", ".join(sorted(KEY_EVENTS))
            )
        # CDP rule: keys carrying text (ENTER/TAB/SPACE) must use "keyDown";
        # other special keys must use "rawKeyDown" for the browser to react.
        down_type = "keyDown" if fields.get("text") else "rawKeyDown"
        self._dispatch_key({"type": down_type, **fields})
        key_up = dict(fields)
        key_up.pop("text", None)  # the text hint is keyDown-only
        self._dispatch_key({"type": "keyUp", **key_up})

    def type_character(self, ch: str) -> bool:
        """Types one printable ASCII character via real key events.

        Returns True when typed. Characters that cannot be typed directly
        (non-ASCII such as Hangul, control chars) return False — the caller
        then decides on the ``Input.insertText`` fallback.
        """
        pair = char_events(ch)
        if pair is None:
            return False
        key_down, key_up = pair
        self._dispatch_key(key_down)
        self._dispatch_key(key_up)
        return True

    def insert_text(self, text: str) -> None:
        """Inserts text into the focused element (IME bypass — not physical keys)."""
        self._ensure_attached()
        self._conn.send(
            "Input.insertText", {"text": str(text)}, session_id=self._session_id
        )

    def focus_element(self, locator: dict) -> None:
        """Focuses an element via the bridge ``focus`` primitive."""
        result = self.call("focus", locator)
        if not (isinstance(result, dict) and result.get("ok")):
            reason = result.get("reason") if isinstance(result, dict) else None
            raise CdpError(f"focus failed for locator {locator!r}: {reason}")

    def type_text(self, locator: dict, text: str) -> int:
        """Focuses the element, then types the text character by character with real keys.

        Printable ASCII goes through keyDown/keyUp pairs; the rest (Hangul
        etc.) falls back to ``Input.insertText``. Returns the count typed.
        """
        self.focus_element(locator)
        typed = 0
        for ch in str(text):
            if not self.type_character(ch):
                self.insert_text(ch)
            typed += 1
        return typed

    # ------------------------------------------------------------------
    # Real mouse track — Input.dispatchMouseEvent (real pointer events)
    # ------------------------------------------------------------------

    def _dispatch_mouse(self, params: dict) -> None:
        self._ensure_attached()
        self._conn.send(
            "Input.dispatchMouseEvent",
            params,
            session_id=self._session_id,
        )

    def _locator_center(self, locator: dict) -> Tuple[float, float]:
        """Scrolls the element into the viewport, then returns its center (viewport coords)."""
        scrolled = self.call("scrollIntoView", locator)
        if not (isinstance(scrolled, dict) and scrolled.get("ok")):
            reason = scrolled.get("reason") if isinstance(scrolled, dict) else None
            raise CdpError(f"scrollIntoView failed for {locator!r}: {reason}")
        box = self.call("rect", locator)
        if not (isinstance(box, dict) and box.get("ok")):
            reason = box.get("reason") if isinstance(box, dict) else None
            raise CdpError(f"rect failed for {locator!r}: {reason}")
        return (
            float(box["x"]) + float(box["width"]) / 2.0,
            float(box["y"]) + float(box["height"]) / 2.0,
        )

    def real_click_element(self, locator: dict) -> Tuple[float, float]:
        """Clicks the element center with **real mouse events**.

        scrollIntoView -> rect center -> mousePressed/mouseReleased.
        For paths needing a real pointer: CSS ``:hover``, ``isTrusted``
        checks, occlusion. Returns the coordinates (evidence/debugging).
        """
        x, y = self._locator_center(locator)
        self.real_click_at(x, y)
        return (x, y)

    def real_click_at(self, x: float, y: float) -> None:
        """Clicks viewport coordinates (x, y) with a real mouse — **the canvas partial-support path**."""
        self._dispatch_mouse(
            {
                "type": "mousePressed",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            }
        )
        self._dispatch_mouse(
            {
                "type": "mouseReleased",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            }
        )

    def real_hover(self, locator: dict) -> Tuple[float, float]:
        """Sends a real mouseMoved to the element center, triggering a genuine CSS ``:hover``."""
        x, y = self._locator_center(locator)
        self._dispatch_mouse(
            {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0}
        )
        return (x, y)

    def real_drag(self, source: dict, target: dict) -> None:
        """Drags from the source center to the target center, then releases.

        **Raw mouse sequence** (mousePressed -> mouseMoved (with
        waypoints) -> mouseReleased) — HTML5 drag-and-drop event synthesis
        (dragstart/drop) is out of scope.
        """
        sx, sy = self._locator_center(source)
        tx, ty = self._locator_center(target)
        self._dispatch_mouse(
            {
                "type": "mousePressed",
                "x": sx,
                "y": sy,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            }
        )
        # Waypoints — long moves resemble real input better with intermediate events.
        steps = 6
        for i in range(1, steps + 1):
            self._dispatch_mouse(
                {
                    "type": "mouseMoved",
                    "x": sx + (tx - sx) * i / steps,
                    "y": sy + (ty - sy) * i / steps,
                    "button": "left",
                    "buttons": 1,
                }
            )
        self._dispatch_mouse(
            {
                "type": "mouseReleased",
                "x": tx,
                "y": ty,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            }
        )

    # ------------------------------------------------------------------
    # Scrolling · file upload
    # ------------------------------------------------------------------

    def scroll_by(self, delta_x: float, delta_y: float) -> float:
        """Scrolls with a real mouse-wheel event at the viewport center.

        Positive deltaY scrolls down, positive deltaX right. For elements
        that only enter the DOM after scrolling (virtualized grids), combine
        this with polling assertions. Wheel input applies asynchronously, so
        the method waits briefly and returns the final ``window.scrollY``.
        """
        self._ensure_attached()
        before = self.evaluate("window.scrollY")
        size = self.evaluate("[window.innerWidth, window.innerHeight]")
        cx = float(size[0]) / 2.0
        cy = float(size[1]) / 2.0
        self._dispatch_mouse(
            {
                "type": "mouseWheel",
                "x": cx,
                "y": cy,
                "deltaX": float(delta_x),
                "deltaY": float(delta_y),
            }
        )
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if self.evaluate("window.scrollY") != before:
                break
            time.sleep(0.02)
        return float(self.evaluate("window.scrollY"))

    def scroll_to_element(self, locator: dict) -> None:
        """Scrolls the element into the viewport (only when needed)."""
        result = self.call("scrollIntoView", locator)
        if not (isinstance(result, dict) and result.get("ok")):
            reason = result.get("reason") if isinstance(result, dict) else None
            raise CdpError(f"scrollIntoView failed for {locator!r}: {reason}")

    def capture_screenshot_png(self, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Captures the current viewport as PNG bytes (visual-regression input)."""
        self._ensure_attached()
        result = self._conn.send(
            "Page.captureScreenshot",
            {"format": "png"},
            session_id=self._session_id,
            timeout=timeout,
        )
        import base64

        data = (result or {}).get("data")
        if not data:
            raise CdpError(
                f"Page.captureScreenshot returned no image data: {result!r}"
            )
        return base64.b64decode(data)

    def set_input_files(self, css_selector: str, absolute_paths: Sequence[str]) -> None:
        """Sets files on an ``<input type=file>`` (no file-chooser dialog).

        ``DOM.setFileInputFiles`` — the browser reads the files directly
        without an OS picker (the change event fires immediately after).
        ``css_selector`` is a CSS selector string (the library converts the
        testid strategy to CSS). nodeIds go stale on every DOM change, so
        resolution happens at call time.
        """
        self._ensure_attached()
        document = self._conn.send(
            "DOM.getDocument", {}, session_id=self._session_id
        )
        root_id = (document or {}).get("root", {}).get("nodeId")
        if not root_id:
            raise CdpError(f"DOM.getDocument returned no root nodeId: {document!r}")
        node = self._conn.send(
            "DOM.querySelector",
            {"nodeId": root_id, "selector": css_selector},
            session_id=self._session_id,
        )
        node_id = (node or {}).get("nodeId") or 0
        if not node_id:
            raise CdpError(
                f"DOM.querySelector found no node for {css_selector!r} "
                "(upload input must match a CSS selector)"
            )
        self._conn.send(
            "DOM.setFileInputFiles",
            {"files": list(absolute_paths), "nodeId": node_id},
            session_id=self._session_id,
        )

    # ------------------------------------------------------------------
    # Cookies — Network.* (current tab session)
    # ------------------------------------------------------------------

    def get_cookies(self, urls: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """Returns the current context's cookies as a list of dicts.

        ``urls`` limits the result to cookies related to those URLs. This is
        not event-based, so ``Network.enable`` is not required.
        """
        self._ensure_attached()
        params: Dict[str, Any] = {}
        if urls:
            params["urls"] = [str(u) for u in urls]
        result = self._conn.send(
            "Network.getCookies", params, session_id=self._session_id
        )
        return list((result or {}).get("cookies") or [])

    def set_cookie(
        self,
        url: str,
        name: str,
        value: str,
        *,
        expires: Optional[float] = None,
        http_only: bool = False,
        secure: bool = False,
        same_site: Optional[str] = None,
    ) -> None:
        """Sets a cookie — ``Network.setCookie``; CdpError on failure.

        ``url`` is the origin the cookie belongs to (e.g. ``http://localhost:8000/``).
        ``expires`` is Unix epoch seconds; ``same_site`` is Strict|Lax|Extended.
        """
        self._ensure_attached()
        params: Dict[str, Any] = {
            "url": str(url),
            "name": str(name),
            "value": str(value),
        }
        if expires is not None:
            params["expires"] = float(expires)
        if http_only:
            params["httpOnly"] = True
        if secure:
            params["secure"] = True
        if same_site is not None:
            params["sameSite"] = str(same_site)
        result = self._conn.send(
            "Network.setCookie", params, session_id=self._session_id
        )
        if not (result or {}).get("success", False):
            raise CdpError(f"Network.setCookie returned failure: {result!r}")

    def delete_cookie(self, name: str, url: Optional[str] = None) -> None:
        """Deletes cookies by name — ``Network.deleteCookies``.

        With ``url`` only that origin's cookies; without it, every match.
        """
        self._ensure_attached()
        params: Dict[str, Any] = {"name": str(name)}
        if url is not None:
            params["url"] = str(url)
        self._conn.send(
            "Network.deleteCookies", params, session_id=self._session_id
        )

    def delete_all_cookies(self) -> None:
        """Deletes every cookie in the browser.

        ``Network.clearBrowserCookies`` is currently exposed **only through
        a page session** (absent on the browser endpoint — a domain with a
        history of moves). The effect applies to browser-global cookies.
        """
        self._ensure_attached()
        self._conn.send(
            "Network.clearBrowserCookies", {}, session_id=self._session_id
        )

    # ------------------------------------------------------------------
    # Viewport / mobile emulation — Emulation.*
    # ------------------------------------------------------------------

    def set_viewport(
        self,
        width: int,
        height: int,
        device_scale_factor: float = 1.0,
        mobile: bool = False,
    ) -> None:
        """Overrides the viewport size/device metrics.

        ``Emulation.setDeviceMetricsOverride`` — for responsive-layout and
        mobile-viewport verification. Per-tab (session) state. ``width=0,
        height=0`` means "full screen" and is rejected by this interface.
        """
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError(
                f"viewport dimensions must be positive, got {width}x{height}"
            )
        self._ensure_attached()
        self._conn.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(width),
                "height": int(height),
                "deviceScaleFactor": float(device_scale_factor),
                "mobile": bool(mobile),
            },
            session_id=self._session_id,
        )
        if mobile:
            # The mobile hint alone does not enable touch APIs — turn on touch
            # event emulation for a substantive mobile emulation.
            self._conn.send(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 5},
                session_id=self._session_id,
            )

    def reset_viewport(self) -> None:
        """Clears the viewport override and restores the default state.

        Clearing applies asynchronously, so the method waits up to 0.5s for
        the layout metrics to change before returning — when no override was
        active the metrics stay put and it waits out the deadline (harmless).
        """
        self._ensure_attached()
        overridden = self.evaluate("[window.innerWidth, window.innerHeight]")
        self._conn.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": False},
            session_id=self._session_id,
        )
        self._conn.send(
            "Emulation.clearDeviceMetricsOverride", {}, session_id=self._session_id
        )
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if self.evaluate("[window.innerWidth, window.innerHeight]") != overridden:
                return
            time.sleep(0.02)

    def evaluate(self, expression: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Evaluates an arbitrary JS expression and returns the JSON value (diagnostics/tests).
        Applies the same result-parsing rules as ``call()``.
        """
        if not isinstance(expression, str):
            raise TypeError(
                f"expression must be str, got {type(expression).__name__}"
            )
        return self._evaluate(
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout,
        )

    # ------------------------------------------------------------------
    # Frame scope
    # ------------------------------------------------------------------

    @property
    def current_frame_id(self) -> Optional[str]:
        """frameId of the current scope. ``None`` for the main frame."""
        return self._frame_stack[-1] if self._frame_stack else None

    @property
    def current_context_id(self) -> Optional[int]:
        """``Runtime.executionContextId`` of the current scope (``None`` on main).

        When set, ``_evaluate`` passes ``contextId`` to ``Runtime.evaluate``
        so the bridge runs inside that frame's document.
        """
        if not self._frame_stack:
            return None
        return self._contexts.get(self._frame_stack[-1])

    def switch_to_frame(self, frame: Union[str, int]) -> None:
        """Pushes a frame scope onto ``frame`` (nesting stack).

        Subsequent ``call()``/``evaluate()`` — and every bridge primitive
        layered on them — run inside that frame's document.

        The ``frame`` identifier is resolved against the current scope:

        1. number/numeric string — document-order index of the frame-creating
           element (iframe/frame) in the current frame.
        2. string — exact match on the iframe's ``name`` attribute, then
           ``id`` attribute, then a CSS selector (matching the iframe itself).

        Only same-process (same-site) frames are supported. OOPIFs
        (cross-site iframes rendered in a separate process) expose no
        execution context to this session and fail with a
        :class:`CdpError` ("No execution context").

        Raises:
            FrameNotFoundError: the identifier matches no frame-creating
                element. The message lists the candidates (name/id).
            CdpError: the frame was found but no execution context could be
                obtained (including OOPIFs).
        """
        self._ensure_attached()
        tree = self._frame_tree()
        node = self._find_frame_node(tree, self.current_frame_id)
        children = [
            child.get("frame") or {}
            for child in (node.get("childFrames") or [])
        ]
        index = self._resolve_frame_index(frame, children)
        if not 0 <= index < len(children):
            raise FrameNotFoundError(
                f"Frame not found: {frame!r} — candidates in the current "
                f"frame: {[(c.get('name') or '', c.get('id') or '') for c in children]}"
            )
        frame_id = children[index].get("id")
        if not frame_id:
            raise FrameNotFoundError(
                f"frame tree child at index {index} has no frameId: "
                f"{children[index]!r}"
            )
        self._frame_stack.append(frame_id)
        try:
            # Ensure a context (polling) then guarantee the bridge — on failure, restore the scope.
            self._context_id_for(frame_id)
            self._ensure_bridge_in_frame()
        except BaseException:
            self._frame_stack.pop()
            raise

    def reset_frame(self, scope: str = "") -> None:
        """Pops one level of frame scope (the parent frame).

        ``scope="ALL"`` clears the whole stack, returning to the main frame
        (case-insensitive). When the stack is already empty (main frame)
        this is a no-op.
        """
        self._ensure_attached()
        normalized = str(scope or "").strip().upper()
        if normalized == "ALL":
            self._frame_stack.clear()
            return
        if normalized:
            raise ValueError(f"scope must be 'ALL' or empty, got {scope!r}")
        if self._frame_stack:
            self._frame_stack.pop()

    # ------------------------------------------------------------------
    # Internals — frame tracking/resolution
    # ------------------------------------------------------------------

    def _ensure_context_tracking(self) -> None:
        """Starts listening for ``Runtime.executionContextCreated`` and drains
        residual events.

        ``Runtime.enable`` re-broadcasts every existing execution context as
        events, so the subscribe -> enable -> drain order catches up on all
        current documents even when started late.
        """
        self._ensure_attached()
        if self._context_events is None:
            self._context_events = self._conn.subscribe("Runtime.*")
            self._conn.send("Runtime.enable", session_id=self._session_id)
        self._drain_context_events()

    def _drain_context_events(self) -> None:
        """Applies accumulated Runtime context events into the ``_contexts`` map.

        When several contexts are created for one frameId (document
        replacement), the last one wins. destroyed/cleared events purge
        stale entries.
        """
        events = self._context_events
        if events is None:
            return
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event.session_id != self._session_id:
                continue
            method = event.method
            params = event.params or {}
            if method == "Runtime.executionContextCreated":
                context = params.get("context") or {}
                frame_id = (context.get("auxData") or {}).get("frameId")
                context_id = context.get("id")
                if frame_id and context_id is not None:
                    self._contexts[frame_id] = context_id
            elif method == "Runtime.executionContextDestroyed":
                context_id = params.get("executionContextId")
                self._contexts = {
                    fid: cid
                    for fid, cid in self._contexts.items()
                    if cid != context_id
                }
            elif method == "Runtime.executionContextsCleared":
                self._contexts.clear()

    def _context_id_for(self, frame_id: str, timeout: float = 10.0) -> int:
        """Polls for the execution context id of ``frame_id`` and returns it.

        Absorbs the short race window where the frame tree already knows the
        frame but its context-created event has not arrived. If it never
        appears within ``timeout``, raises :class:`CdpError` — not same-process (OOPIF).
        """
        self._ensure_context_tracking()
        deadline = time.monotonic() + timeout
        while True:
            self._drain_context_events()
            context_id = self._contexts.get(frame_id)
            if context_id is not None:
                return context_id
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(
                    f"No execution context found for frame {frame_id!r} "
                    f"within {timeout:.1f}s — the frame is likely rendered "
                    "in a separate process (cross-origin OOPIF), which is "
                    "not supported by frame scoping"
                )
            time.sleep(min(0.05, remaining))

    def _frame_tree(self) -> Dict[str, Any]:
        self._ensure_attached()
        result = self._conn.send(
            "Page.getFrameTree", session_id=self._session_id
        ) or {}
        tree = result.get("frameTree")
        if not isinstance(tree, dict):
            raise CdpError(
                f"Page.getFrameTree returned no frameTree: {result!r}"
            )
        return tree

    @staticmethod
    def _find_frame_node(
        tree: Dict[str, Any], frame_id: Optional[str]
    ) -> Dict[str, Any]:
        """Returns the ``frame_id`` node from the frame tree (root when None)."""
        if frame_id is None:
            return tree
        stack = [tree]
        while stack:
            node = stack.pop()
            if (node.get("frame") or {}).get("id") == frame_id:
                return node
            stack.extend(node.get("childFrames") or [])
        raise FrameNotFoundError(
            f"frame {frame_id!r} is no longer present in the frame tree"
        )

    def _resolve_frame_index(
        self, frame: Union[str, int], children: List[Dict[str, Any]]
    ) -> int:
        """Resolves an identifier to a child index of the current frame (-1 when absent).

        Resolution order: numeric index -> name attribute -> id attribute -> CSS selector.
        CSS matching is evaluated in the current frame document; invalid CSS
        syntax is promoted to :class:`BridgeEvaluationError` (not a silent not-found).
        """
        identifier = str(frame)
        if identifier.isdigit():
            return int(identifier)
        elements = self._evaluate(
            {"expression": _ENUMERATE_FRAME_ELEMENTS, "returnByValue": True},
            DEFAULT_TIMEOUT,
        )
        if isinstance(elements, list):
            for index, element in enumerate(elements):
                if isinstance(element, dict) and element.get("name") == identifier:
                    return index
            for index, element in enumerate(elements):
                if isinstance(element, dict) and element.get("id") == identifier:
                    return index
        expression = _LOCATE_FRAME_BY_CSS.replace(
            "__SELECTOR__", json.dumps(identifier)
        )
        result = self._evaluate(
            {"expression": expression, "returnByValue": True},
            DEFAULT_TIMEOUT,
        )
        index = result if isinstance(result, int) else -1
        return index

    def _ensure_bridge_in_frame(self) -> None:
        """Checks whether the current frame context has the bridge; injects it if not.

        ``Page.addScriptToEvaluateOnNewDocument`` also covers same-process
        iframe documents, so it usually exists already; this fallback is a
        safety net for missed injections (documents created before registration).
        """
        result = self._evaluate(
            {"expression": "typeof window.__cdpb === 'object'", "returnByValue": True},
            DEFAULT_TIMEOUT,
        )
        if result is True:
            return
        self._evaluate(
            {"expression": load_bridge_source(), "returnByValue": False},
            DEFAULT_TIMEOUT,
        )


    def navigate(self, url: str, timeout: float = 30.0) -> None:
        """Navigates with ``Page.navigate`` and waits for ``Page.loadEventFired``.

        Handles every URL the browser can, including file:// and data:.
        Raises :class:`CdpTimeoutError` when no load event arrives within
        ``timeout``, and :class:`NavigationError` when the browser returns
        ``errorText``.
        """
        self._ensure_attached()
        self._record_navigation(url)
        # Guarantee loadEventFired reception — Page.enable is idempotent.
        self._conn.send("Page.enable", session_id=self._session_id)
        events = self._conn.subscribe("Page.loadEventFired")
        try:
            self._drain_events(events)
            result = self._conn.send(
                "Page.navigate",
                {"url": url},
                session_id=self._session_id,
                timeout=timeout,
            )
            error_text = (result or {}).get("errorText")
            if error_text:
                raise NavigationError(
                    f"navigation to {url!r} failed: {error_text}"
                )
            self._wait_for_load(events, url, timeout)
        finally:
            self._conn.unsubscribe(events)
        # Navigation resets the frame stack and context map — the new
        # document tree's contexts get re-collected by later Runtime events
        # (main-frame evaluation uses the default context, so it works immediately).
        self._frame_stack.clear()
        self._contexts.clear()

    def close(self) -> None:
        """Closes the tab. Idempotent.

        Cleanup completes quietly even when the connection is already gone.
        """
        if self._closed:
            return
        self._closed = True
        if self._dialogs is not None:
            try:
                self._dialogs.close()
            except Exception:  # noqa: BLE001 — cleanup stays quiet
                pass
            self._dialogs = None
        if self._downloads is not None:
            try:
                self._downloads.close()
            except Exception:  # noqa: BLE001 — cleanup stays quiet
                pass
            self._downloads = None
        if self._context_events is not None:
            try:
                self._conn.unsubscribe(self._context_events)
            except Exception:  # noqa: BLE001 — cleanup stays quiet
                pass
            self._context_events = None
        if getattr(self, "_recording", False):
            self._recording = False
        for queue_name in (
            "_recording_queue",
            "_recording_dialog_q",
            "_recording_dl_begin_q",
            "_recording_dl_progress_q",
        ):
            queue = getattr(self, queue_name, None)
            if queue is not None:
                try:
                    self._conn.unsubscribe(queue)
                except Exception:  # noqa: BLE001 — cleanup stays quiet
                    pass
                setattr(self, queue_name, None)
        self._frame_stack.clear()
        self._contexts.clear()
        self._session_id = None
        self._discard_target()

    def __enter__(self) -> "PageSession":
        if self._session_id is None and not self._closed:
            self.attach()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # JS dialogs
    # ------------------------------------------------------------------

    @property
    def dialog_timeout(self) -> float:
        """Backstop dismiss wait (seconds) for dialogs opened without an arm."""
        return self._dialog_timeout

    @dialog_timeout.setter
    def dialog_timeout(self, seconds: float) -> None:
        self._dialog_timeout = max(0.0, float(seconds))

    def arm_dialog(self, action: str = "ACCEPT", prompt_text: Optional[str] = None) -> str:
        """Reserves the next JS dialog and returns the (string) handle immediately.

        **Must be called before the trigger** — while
        ``javascriptDialogOpening`` is open, CDP commands on this session
        are blocked, so arm-then-trigger is the only safe order. arm issues
        one idempotent command (``Page.enable``) plus a subscription and returns.

        ``action`` is ``ACCEPT`` or ``DISMISS``; ``prompt_text`` is the
        ``prompt()`` answer. The result text comes back via :meth:`wait_dialog`.
        """
        return self._dialog_coordinator().arm(action, prompt_text)

    def wait_dialog(self, handle: str, timeout: float) -> str:
        """Waits for the armed dialog; returns the dialog text."""
        return self._dialog_coordinator().wait(handle, timeout)

    def cancel_dialog(self, handle: str) -> None:
        """Cancels the arm — for handles whose trigger never ran."""
        if self._dialogs is not None:
            self._dialogs.cancel(handle)

    def _dialog_coordinator(self) -> DialogCoordinator:
        self._ensure_attached()
        if self._dialogs is None:
            self._dialogs = DialogCoordinator(
                self._conn,
                self._session_id,  # type: ignore[arg-type]
                timeout_provider=lambda: self._dialog_timeout,
            )
            # Start eagerly — the subscription/backstop must exist from session
            # start (not first arm) so un-armed dialogs are handled too. Idempotent.
            self._dialogs.start()
        return self._dialogs

    # ------------------------------------------------------------------
    # File downloads
    # ------------------------------------------------------------------

    @property
    def download_dir(self) -> Optional[str]:
        """Download directory. ``None`` means a temp dir on first download."""
        return self._download_dir

    @download_dir.setter
    def download_dir(self, path: Union[str, "_Path"]) -> None:
        self._download_dir = str(path)

    def arm_download(self) -> str:
        """Reserves the next download **completion**; returns a handle immediately.

        Downloads do not block the session, so arm-before-trigger is not
        strictly required, but the same arm -> trigger -> wait convention
        applies. On completion :meth:`wait_download` returns the saved
        file's full path.
        """
        return self._download_coordinator().arm()

    def wait_download(self, handle: str, timeout: float) -> str:
        """Waits for the armed download; returns the saved file's full path."""
        return self._download_coordinator().wait(handle, timeout)

    def cancel_download(self, handle: str) -> None:
        """Cancels a download arm — for handles whose trigger never ran."""
        if self._downloads is not None:
            self._downloads.cancel(handle)

    def _download_coordinator(self) -> DownloadCoordinator:
        self._ensure_attached()
        if self._downloads is None:
            if self._download_dir is None:
                self._download_dir = tempfile.mkdtemp(prefix="cdpbrowser-dl-")
            self._downloads = DownloadCoordinator(
                self._conn,
                self._session_id,  # type: ignore[arg-type]
                download_dir_provider=lambda: self._download_dir or "",
            )
        return self._downloads

    # ------------------------------------------------------------------
    # Recording — record-and-replay support
    # ------------------------------------------------------------------

    #: CDP binding name the recorder.js pushes events through.
    _RECORDER_BINDING = "cdpbRecordEvent"
    _RECORDER_JS_RESOURCE = "recorder.js"

    def start_recording(self) -> int:
        """Starts recording user/agent input on this session.

        Installs recorder.js (every future document plus the current one),
        exposes the ``cdpbRecordEvent`` CDP binding, and subscribes to
        ``Runtime.bindingCalled`` to collect events. Navigations driven
        through :meth:`navigate` are recorded too. Returns the number of
        events collected before this call (usually 0).

        Main-frame events only (v1 boundary). Regular typing collapses into
        debounced ``fill`` events; standalone special keys become ``press``.
        """
        self._ensure_attached()
        if getattr(self, "_recording", False):
            return len(self._recording_events)
        self._recording_events: List[Dict[str, Any]] = []
        # Runtime.enable is what actually delivers bindingCalled events —
        # the binding is callable on the page before this, but silent.
        self._conn.send("Runtime.enable", session_id=self._session_id)
        self._recording_queue = self._conn.subscribe("Runtime.bindingCalled")

        def _drain(queue, handler) -> None:
            while True:
                try:
                    event = queue.get_nowait()
                except Exception:  # noqa: BLE001 — queue.Empty
                    return
                handler(event)

        def _pump() -> None:
            # Drain all recording queues on demand — no dedicated thread.
            _drain(self._recording_queue, self._collect_binding_event)
            _drain(self._recording_dl_begin_q, self._collect_dl_begin)
            _drain(self._recording_dl_progress_q, self._collect_dl_progress)
            _drain(self._recording_dialog_q, self._collect_dialog_event)

        self._recording_pump = _pump
        # The binding must exist before pages can call it; idempotent.
        self._conn.send(
            "Runtime.addBinding",
            {"name": self._RECORDER_BINDING},
            session_id=self._session_id,
        )
        # Observe dialogs and downloads WITHOUT interfering: separate
        # subscriptions alongside the coordinators/backstop. Correlated to
        # the nearest preceding action at stop time.
        self._recording_dialog_q = self._conn.subscribe(_DIALOG_OPENING)
        self._recording_dl_begin_q = self._conn.subscribe(_DOWNLOAD_BEGIN)
        self._recording_dl_progress_q = self._conn.subscribe(_DOWNLOAD_PROGRESS)
        self._recording_dl_names: Dict[str, str] = {}
        # Download observation requires event-enabled download behavior —
        # the coordinator is lazy (first arm), so start it eagerly here.
        self._download_coordinator().start()
        source = load_resource_js(self._RECORDER_JS_RESOURCE)
        self._conn.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
            session_id=self._session_id,
        )
        # The arm flag must survive navigation — register it as its own
        # new-document script (tracked for removal on stop) instead of a
        # one-shot evaluate that dies with the current document.
        arm = self._conn.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "window.__cdpbRecOn = true; 1"},
            session_id=self._session_id,
        )
        self._recorder_arm_id = (arm or {}).get("identifier")
        self.evaluate(source)
        self.evaluate("window.__cdpbRecOn = true; window.__cdpbRec.install(); 1")
        self._recording = True
        return 0

    def stop_recording(self) -> List[Dict[str, Any]]:
        """Stops recording and returns the collected events.

        Flushes the pending debounced fill, disarms the page-side toggle,
        drains remaining binding events, and returns the event list. The
        injected listeners stay but are inert until the next start.
        """
        if not getattr(self, "_recording", False):
            return list(getattr(self, "_recording_events", []))
        self.evaluate(
            "window.__cdpbRec && window.__cdpbRec.flush();"
            " window.__cdpbRecOn = false; 1"
        )
        arm_id = getattr(self, "_recorder_arm_id", None)
        if arm_id:
            try:
                self._conn.send(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": arm_id},
                    session_id=self._session_id,
                )
            except Exception:  # noqa: BLE001 — best-effort removal
                pass
            self._recorder_arm_id = None
        # Wait briefly for the flushed fill event to arrive, then drain.
        # The generous tail also lets late dialog/download observations
        # (fired around the last action) land before correlation.
        deadline = time.monotonic() + 1.2
        seen = len(self._recording_events)
        while time.monotonic() < deadline:
            self._recording_pump()
            if len(self._recording_events) > seen:
                seen = len(self._recording_events)
                deadline = min(deadline, time.monotonic() + 0.5)
            time.sleep(0.05)
        self._recording_pump()
        self._recording = False
        for queue_name in (
            "_recording_queue",
            "_recording_dialog_q",
            "_recording_dl_begin_q",
            "_recording_dl_progress_q",
        ):
            queue = getattr(self, queue_name, None)
            if queue is not None:
                try:
                    self._conn.unsubscribe(queue)
                except Exception:  # noqa: BLE001 — cleanup path
                    pass
                setattr(self, queue_name, None)
        return self._correlate_recording(list(self._recording_events))

    def recording_events(self) -> List[Dict[str, Any]]:
        """Pumps and returns the events collected so far (without stopping)."""
        if getattr(self, "_recording", False):
            self._recording_pump()
        return list(getattr(self, "_recording_events", []))

    def _collect_binding_event(self, event: CdpEvent) -> None:
        params = event.params or {}
        if params.get("name") != self._RECORDER_BINDING:
            return
        payload = params.get("payload")
        if not payload:
            return
        try:
            record = json.loads(str(payload))
        except (TypeError, ValueError):
            return
        if isinstance(record, dict) and record.get("type"):
            self._recording_events.append(record)

    def _collect_dialog_event(self, event: CdpEvent) -> None:
        params = event.params or {}
        message = params.get("message")
        if message is None:
            return
        self._recording_events.append(
            {
                "type": "dialog",
                "message": str(message),
                "kind": str(params.get("type") or "alert"),
            }
        )

    def _collect_dl_begin(self, event: CdpEvent) -> None:
        params = event.params or {}
        guid = params.get("guid")
        name = params.get("suggestedFilename")
        if guid and name:
            self._recording_dl_names[str(guid)] = str(name)

    def _collect_dl_progress(self, event: CdpEvent) -> None:
        params = event.params or {}
        if params.get("state") != "completed":
            return
        guid = str(params.get("guid") or "")
        self._recording_events.append(
            {
                "type": "download",
                "guid": guid,
                "filename": self._recording_dl_names.get(guid, guid),
            }
        )

    _ACTION_TYPES = ("navigate", "click", "fill", "press")

    @staticmethod
    def _correlate_recording(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attaches dialog/download observations to their triggering action.

        Dialog and download events arrive on separate CDP channels; the
        reliable causal anchor is the nearest preceding *action* event. The
        observation is stored on that action (``dialog`` / ``download``
        keys) and the standalone entries are dropped.
        """
        out: List[Dict[str, Any]] = []
        for event in events:
            kind = event.get("type")
            if kind in ("dialog", "download"):
                for prior in reversed(out):
                    if prior.get("type") in PageSession._ACTION_TYPES:
                        if kind == "dialog" and "dialog" not in prior:
                            prior["dialog"] = event["message"]
                        elif kind == "download" and "download" not in prior:
                            prior["download"] = event.get("filename")
                        break
                continue  # standalone observation consumed
            out.append(event)
        return out

    def _record_navigation(self, url: str) -> None:
        if getattr(self, "_recording", False):
            self._recording_events.append({"type": "navigate", "url": str(url)})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_attached(self) -> None:
        if self._closed:
            raise CdpError("PageSession is closed")
        if self._session_id is None:
            raise CdpError("PageSession is not attached; call attach() first")

    def _register_bridge(self) -> None:
        source = load_bridge_source()
        # Register so every new document gets the injection automatically.
        self._conn.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
            session_id=self._session_id,
        )
        # Also inject into the current document (about:blank) — usable before any navigate.
        self._evaluate(
            {"expression": source, "returnByValue": False},
            DEFAULT_TIMEOUT,
        )

    def _evaluate(self, params: Dict[str, Any], timeout: float) -> Any:
        self._ensure_attached()
        if self._context_events is not None:
        # Keep the context map fresh — prevents evaluating with contextIds
        # stale after executionContextsCleared/Destroyed.
            self._drain_context_events()
        context_id: Optional[int] = None
        if self._frame_stack:
            frame_id = self._frame_stack[-1]
            context_id = self._contexts.get(frame_id)
            if context_id is None:
                # Short race window right after entering a scope — brief polling retry.
                context_id = self._context_id_for(frame_id, timeout=5.0)
        if context_id is not None:
            params = {**params, "contextId": context_id}
        result = self._conn.send(
            "Runtime.evaluate",
            params,
            session_id=self._session_id,
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise CdpError(
                f"Runtime.evaluate returned no result: {result!r}"
            )
        details = result.get("exceptionDetails")
        if details:
            raise BridgeEvaluationError(_format_exception_details(details))
        remote = result.get("result")
        return (remote or {}).get("value")

    def _discard_target(self) -> None:
        target_id = self._target_id
        if target_id is None:
            return
        try:
            self._conn.send(
                "Target.closeTarget", {"targetId": target_id}, timeout=10
            )
        except CdpError:
            pass  # even on disconnect/timeout, cleanup continues

    @staticmethod
    def _drain_events(events: "queue.Queue") -> None:
        # Drain residual events from the previous document so the navigate wait does not false-trigger.
        try:
            while True:
                events.get_nowait()
        except queue.Empty:
            pass

    def _wait_for_load(self, events: "queue.Queue", url: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpTimeoutError(
                    f"Timed out after {timeout}s waiting for "
                    f"Page.loadEventFired for {url!r}"
                )
            try:
                event = events.get(timeout=remaining)
            except queue.Empty:
                continue
            if event.session_id == self._session_id:
                return
            # load event from another session — ignore and keep waiting
