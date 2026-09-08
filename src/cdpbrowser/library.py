"""Robot Framework library core — session keywords and lazy browser startup.

:class:`CdpBrowser` wraps the CDP transport layer (:mod:`cdpbrowser.cdp`)
and page sessions (:class:`cdpbrowser.page.PageSession`) as Robot Framework
keywords. Design principles:

- **Lazy startup**: the browser starts automatically at the first keyword
  call when needed (find_chrome → ChromeProcess.start → CdpConnection →
  PageSession.attach). ``Open Browser`` exists for explicit startup and is
  a no-op when the browser is already open.
- **GLOBAL scope**: one library instance and one browser are reused for
  their entire lifetime. After ``Close Browser``, the next keyword call
  starts the browser lazily again.
- **Zombie Chrome prevention**: on the first successful startup an atexit
  hook is registered so cleanup is guaranteed even at interpreter exit.
  ``__del__`` also attempts a final cleanup.

Robot usage example::

    *** Settings ***
    Library    CdpBrowser

    *** Test Cases ***
    Example
        Go To    file:///tmp/demo.html
        ${title}=    Get Title
        Should Be Equal    ${title}    Demo
        Close Browser
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, Union

from robot.api import logger
from robot.utils import timestr_to_secs

from . import __version__
from .cdp.chrome import ChromeProcess, find_chrome
from .cdp.errors import ChromeLaunchError
from .cdp.transport import CdpConnection
from .keys import resolve_key_token
from .listener import EvidenceListener, EvidenceTracker, safe_name
from .locators import InvalidSelectorError, TESTID_ATTR, parse, to_bridge_arg
from .page import FrameNotFoundError, PageSession, normalize_dialog_action
from .polling import PollTimeoutError, poll_until
from .promises import PromiseError, PromiseTimeoutError
from .visual import (
    VisualUnsupportedError,
    compare_images,
    save_visual_artifacts,
)

__all__ = ["CdpBrowser"]

#: Modes accepted by ``Set Evidence Mode``.
EVIDENCE_MODES = ("off", "on-failure", "every-step")

#: Domains that Execute CDP Command sends to the browser endpoint without a session_id.
_BROWSER_LEVEL_DOMAINS = frozenset(
    {
        "Browser",
        "Target",
        "SystemInfo",
        "Tethering",
        "Performance",  # browser-global performance domain, also outside the session
    }
)


class CdpBrowser:
    """Pure CDP-based browser automation library.

    The browser starts lazily — no Chrome process exists until the first
    keyword call, though you may start it early with ``Open Browser``.

    = Import Arguments =

    - ``chrome_path``: path to the Chrome executable. The default follows
      the detection order (``CDPBROWSER_CHROME_PATH`` → ``CHROME_PATH`` →
      standard install locations → puppeteer cache).
    - ``headless``: ``True``/``False``/``"auto"``. With ``auto``, the browser
      starts headless when the ``CI`` environment variable is set or
      ``DISPLAY`` is missing.
    - ``timeout``: default timeout for the element-waiting keywords (Robot
      time string, e.g. ``5s``). Polling keywords retry until this
      deadline; change it at runtime with the ``Set Timeout`` keyword.
    - ``poll_interval``: condition polling interval (Robot time string,
      e.g. ``0.1s``).

    = Keywords =

    Session: ``Open Browser``, ``Close Browser``, ``Go To``, ``Get Title``,
    ``Get Current Url``

    Frames: ``Switch To Frame``, ``Reset Frame`` — same-site iframe scope.
    After a switch, interaction and assertion keywords operate inside the
    current frame without modification.

    Interaction (poll-by-default): ``Click``, ``Fill Text``, ``Get Text``

    Assertions (poll-by-default): ``Element Should Be Visible``,
    ``Element Should Exist``, ``Element Should Not Exist``

    JS dialogs (Promise): ``Promise Next Alert``, ``Wait For``,
    ``Handle Alert`` — CDP protocol constraint: **the arm must precede the
    trigger** (see the keyword docstrings).

    Evidence/settings: ``Numbered Screenshot``, ``Set Evidence Mode``,
    ``Set Timeout``

    = Evidence Chain =

    The evidence mode defaults to ``on-failure``: on test failure the
    built-in listener saves the last screen to
    ``{outputdir}/evidence/{suite}/{test}/NNNN-failure.png``.
    ``every-step`` captures automatically after every successful
    ``Click``/``Fill Text``; ``off`` disables all automatic captures. The
    ``NNNN`` number resets per test and is shared between ``Numbered
    Screenshot`` and automatic captures.
    """

    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_VERSION = __version__

    def __init__(
        self,
        chrome_path: Optional[str] = None,
        headless: Union[bool, str, None] = "auto",
        timeout: str = "5s",
        poll_interval: str = "0.1s",
        baseline_dir: Optional[str] = None,
    ) -> None:
        self._chrome_path_setting = chrome_path
        self._headless_setting = headless
        self._timeout = timestr_to_secs(timeout)
        self._poll_interval = timestr_to_secs(poll_interval)
        # Visual regression baseline root — suites should pass a ${CURDIR}-relative path.
        self._baseline_dir = (
            Path(baseline_dir) if baseline_dir else Path("visual-baselines")
        )
        self._lock = threading.RLock()
        self._process: Optional[ChromeProcess] = None
        self._connection: Optional[CdpConnection] = None
        self._session: Optional[PageSession] = None
        # Tab registry — in creation order. self._session is always one of them.
        self._sessions: list = []
        self._atexit_registered = False
        # Built-in listener: per-test evidence context + failure auto-capture + exit cleanup.
        self._evidence = EvidenceTracker()
        self.ROBOT_LIBRARY_LISTENER = EvidenceListener(self)

    # ------------------------------------------------------------------
    # Keywords
    # ------------------------------------------------------------------

    def open_browser(self) -> None:
        """Starts the browser explicitly.

        Does nothing when it is already open (no-op). Lazy automatic
        startup is the default, so calling this keyword is optional.
        """
        with self._lock:
            if self._session is not None:
                logger.info("Browser is already open — reusing the current session.")
                return
            self._start_browser()

    def close_browser(self) -> None:
        """Cleans up the current tab, the CDP connection, and the Chrome process.

        Safe even when nothing is open (re-entrant). Later keyword calls
        start the browser lazily again.
        """
        with self._lock:
            self._teardown()

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    def new_tab(self, url: Optional[str] = None) -> int:
        """Creates a new tab, **switches to it**, and returns its 0-based index.

        Each tab is an independent page target/session — dialog arming,
        downloads, and frame scope state are isolated per tab. If ``url``
        is given, navigates immediately. Tab indexes follow creation
        order; closing a tab shifts later ones forward.
        """
        with self._lock:
            self._ensure_session()  # guarantee lazy browser startup
            assert self._connection is not None
            session = PageSession(self._connection).attach()
            self._sessions.append(session)
            self._session = session
            if url:
                session.navigate(str(url))
            return len(self._sessions) - 1

    def switch_to_tab(self, tab: Union[int, str]) -> int:
        """Switches the current tab to ``tab`` and returns its index.

        ``tab`` is a 0-based index (number) or a **title/URL substring** —
        a substring switches to the first matching open tab. Fails when
        nothing matches or the value is ambiguous (empty string).
        """
        with self._lock:
            index = self._resolve_tab_index(tab)
            self._session = self._sessions[index]
            return index

    def close_tab(self, tab: Optional[Union[int, str]] = None) -> int:
        """Closes a tab and switches to one of the remaining tabs. Returns the remaining index.

        Omitting ``tab`` closes the current tab. **The last remaining tab
        cannot be closed** — use ``Close Browser`` to tear the browser down
        entirely. Closing the current tab switches to the previous index
        (or tab 0 if none).
        """
        with self._lock:
            if len(self._sessions) <= 1:
                raise AssertionError(
                    "Close Tab: this is the last open tab — use Close Browser "
                    "to tear the browser down instead."
                )
            index = (
                self._resolve_tab_index(tab)
                if tab is not None
                else self._sessions.index(self._session)  # type: ignore[arg-type]
            )
            closing = self._sessions.pop(index)
            try:
                closing.close()
            finally:
                if self._session is closing:
                    self._session = self._sessions[max(0, index - 1)]
            return max(0, index - 1)

    def _resolve_tab_index(self, tab: Union[int, str]) -> int:
        raw = str(tab).strip()
        if raw.lstrip("-").isdigit():
            index = int(raw)
            if 0 <= index < len(self._sessions):
                return index
            raise AssertionError(
                f"Tab index {index} out of range — {len(self._sessions)} tab(s) open."
            )
        if not raw:
            raise AssertionError("Tab identifier must be a non-empty index or title/URL substring.")
        needle = raw.lower()
        for i, session in enumerate(self._sessions):
            try:
                title = str(session.evaluate("document.title") or "")
                href = str(session.evaluate("location.href") or "")
            except Exception:  # noqa: BLE001 — skip closed tabs, etc.
                continue
            if needle in title.lower() or needle in href.lower():
                return i
        raise AssertionError(
            f"No open tab matches {tab!r} (by title or URL substring)."
        )

    def go_to(self, url: str) -> str:
        """Navigates to ``url``, waits for the load to finish, and returns the final URL.

        Handles any URL the browser can process, including ``file://``.
        Fails when the load does not complete or the browser reports a
        navigation error.
        """
        session = self._ensure_session()
        session.navigate(url)
        return session.evaluate("location.href")

    def get_title(self) -> str:
        """Returns the ``document.title`` of the current page."""
        return self._ensure_session().evaluate("document.title")

    def get_current_url(self) -> str:
        """Returns the ``location.href`` of the current page."""
        return self._ensure_session().evaluate("location.href")

    # ------------------------------------------------------------------
    # Frame scope keywords
    # ------------------------------------------------------------------

    def switch_to_frame(self, frame: Union[str, int]) -> None:
        """Enters the frame scope — all subsequent keywords operate inside that frame.

        ``frame`` is resolved relative to the current frame: a number or
        numeric string is the document-order index of the frame element
        (iframe/frame); a string is matched against the iframe's ``name``
        attribute, then the ``id`` attribute, then as a CSS selector for
        the iframe element itself. Calls stack, so calling ``Switch To
        Frame`` again inside a frame enters a nested frame.

        Navigating to a new document with ``Go To`` resets the frame scope
        to the main frame. Use ``Reset Frame`` to go back.

        Support boundary: only same-process (same-site) frames are
        supported. Cross-site iframes rendered in a separate process due to
        site isolation (OOPIF) are unsupported and fail with an error on
        entry.

        Raises:
            AssertionError: when the frame cannot be found (promoted to Robot).
        """
        session = self._ensure_session()
        try:
            session.switch_to_frame(frame)
        except FrameNotFoundError as exc:
            raise AssertionError(str(exc)) from None

    def reset_frame(self, scope: str = "") -> None:
        """Steps the frame scope back one level (to the parent frame).

        When ``scope`` is ``ALL``, clears the entire stack and returns to
        the main frame (case-insensitive). No-op when already on the main
        frame.
        """
        self._ensure_session().reset_frame(scope)

    # ------------------------------------------------------------------
    # Interaction keywords — poll-by-default
    # ------------------------------------------------------------------

    def set_timeout(self, timeout: str) -> float:
        """Changes the default timeout of polling keywords and returns the previous value in seconds.

        ``timeout`` is a Robot time string (``"1s"``, ``"500 ms"``, ...) or
        a number. The change applies immediately to the GLOBAL-scope library
        instance; the same value also serves as the wait before the
        backstop auto-dismiss of a JS dialog opened without an arm.
        """
        seconds = timestr_to_secs(timeout)
        if seconds <= 0:
            raise ValueError(f"timeout must be positive, got {timeout!r}")
        previous = self._timeout
        self._timeout = seconds
        if self._session is not None:
            self._session.dialog_timeout = seconds
        return previous

    def click(self, selector: str) -> None:
        """Clicks the ``selector`` element — polls until the click succeeds.

        Follows the selector DSL (``css:#id``, ``testid:x``, ``text:Log in``,
        ``x://button[1]``; no prefix means CSS). Retries until the ``Set
        Timeout`` deadline while the element is missing, invisible, or
        disabled, then fails with the last reason and element diagnostics.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def attempt() -> Tuple[bool, Optional[str], Any]:
            return self._interpret_action(session.call("click", arg))

        self._poll(f"Click {selector!r}", attempt)
        if self._evidence.mode == "every-step":
            self._auto_capture("after-click")

    def fill_text(self, selector: str, text: str) -> None:
        """Fills the ``selector`` input element with ``text`` — polls until it succeeds.

        Uses the bridge's React-compatible ``setValue`` (native setter +
        bubbling input/change events). Follows the same polling and
        diagnostics rules as ``Click``.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def attempt() -> Tuple[bool, Optional[str], Any]:
            return self._interpret_action(session.call("setValue", arg, str(text)))

        self._poll(f"Fill Text {selector!r}", attempt)
        if self._evidence.mode == "every-step":
            self._auto_capture("after-fill")

    def get_text(self, selector: str) -> str:
        """Returns the ``textContent`` of the ``selector`` element — polls until found.

        A missing element (not-found) is also polled. Once found, the text
        is returned as-is even when it is an empty string. An ``<input>``
        value is not its ``textContent`` and cannot be read with this
        keyword — to verify values, prefer an output element that mirrors
        the value via an input event.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def attempt() -> Tuple[bool, Optional[str], Any]:
            result = session.call("getText", arg)
            if isinstance(result, dict) and result.get("ok"):
                return (True, str(result.get("text") or ""), None)
            if isinstance(result, dict):
                return (False, result.get("reason"), result.get("describe"))
            return (False, None, None)

        outcome = self._poll(f"Get Text {selector!r}", attempt)
        return outcome[1] or ""

    def get_attribute(self, selector: str, name: str) -> Optional[str]:
        """Returns the attribute value of the ``selector`` element — polls until found.

        A missing element (not-found) is polled; when the element is found
        but lacks the attribute, ``None`` is returned (not a failure).
        Attribute values are always returned as strings.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()
        attr_name = str(name)

        def attempt() -> Tuple[bool, Optional[Any], Any]:
            result = session.call("getAttr", arg, attr_name)
            if isinstance(result, dict) and result.get("ok"):
                return (True, result.get("value"), None)
            if isinstance(result, dict):
                return (False, result.get("reason"), result.get("describe"))
            return (False, None, None)

        outcome = self._poll(f"Get Attribute {selector!r}[{attr_name!r}]", attempt)
        return outcome[1]

    def get_element_count(self, selector: str) -> int:
        """Returns the number of elements currently matching ``selector`` — **snapshot (no polling)**.

        Counts the current DOM immediately, so 0 is a valid result. To wait
        "until the element appears", use ``Element Should Exist`` first or
        wrap this keyword in ``Wait Until Keyword Succeeds``. Supports all
        selector strategies (css/testid/text/xpath/role/label).
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()
        result = session.call("count", arg)
        if isinstance(result, dict) and isinstance(result.get("count"), int):
            return int(result["count"])
        raise AssertionError(
            f"Get Element Count {selector!r}: bridge returned {result!r}"
        )

    def element_should_be_visible(self, selector: str) -> None:
        """Assertion that polls until the ``selector`` element becomes visible.

        Retries until the deadline with ``not-found`` (element missing) or
        ``not-visible`` (present but hidden) as the reason; on failure it
        raises ``AssertionError`` carrying the last reason plus final
        element diagnostics.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def attempt() -> Tuple[bool, Optional[str], Any]:
            status = session.call("exists", arg)
            if isinstance(status, dict) and status.get("visible"):
                return (True, None, None)
            if status is None:
                return (False, "not-found", None)
            return (False, "not-visible", None)

        self._assert_poll(f"Element Should Be Visible {selector!r}", attempt, session, arg)

    def element_should_exist(self, selector: str) -> None:
        """Assertion that polls until the ``selector`` element exists.

        Only checks DOM presence; visibility is not required.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def attempt() -> Tuple[bool, Optional[str], Any]:
            status = session.call("exists", arg)
            if isinstance(status, dict) and status.get("exists"):
                return (True, None, None)
            return (False, "not-found", None)

        self._assert_poll(f"Element Should Exist {selector!r}", attempt, session, arg)

    def element_should_not_exist(self, selector: str) -> None:
        """Negative polling assertion that waits until the ``selector`` element disappears.

        Runs in two phases for reliability:

        1. First observes that the element is present (avoids a vacuum
           pass — prevents the trap of instantly passing due to a selector
           typo). If the element is never observed within the deadline,
           the keyword fails.
        2. Then waits for the element to disappear within the same timeout budget.

        Each phase gets its own ``Set Timeout`` deadline, so the worst case
        is twice the timeout. To statically verify the absence of an
        element that never exists at all, structure the test to guarantee
        the state *before* this keyword instead.
        """
        arg = self._bridge_arg(selector)
        session = self._ensure_session()

        def presence() -> Tuple[bool, Optional[str], Any]:
            status = session.call("exists", arg)
            if isinstance(status, dict) and status.get("exists"):
                return (True, None, None)
            return (False, "not-found", None)

        def absence() -> Tuple[bool, Optional[str], Any]:
            status = session.call("exists", arg)
            if status is None:
                return (True, None, None)
            return (False, "still-exists", None)

        try:
            poll_until(
                presence,
                self._timeout,
                self._poll_interval,
                f"Element Should Not Exist {selector!r} (confirming initial presence)",
            )
        except PollTimeoutError as exc:
            raise AssertionError(
                f"{exc} — the element was never observed. "
                "Element Should Not Exist waits for a present element to "
                "disappear; verify the selector and page state first."
            ) from None

        self._assert_poll(
            f"Element Should Not Exist {selector!r} (waiting for removal)",
            absence,
            session,
            arg,
        )

    # ------------------------------------------------------------------
    # JS dialogs — Promise (arm-before-trigger contract)
    # ------------------------------------------------------------------

    def promise_next_alert(
        self, action: str = "ACCEPT", prompt_text: Optional[str] = None
    ) -> str:
        """Arms the next JS dialog (alert/confirm/prompt) and returns its handle.

        ⚠️ **Arm-before-trigger contract**: per the CDP protocol, while a
        ``javascriptDialogOpening`` event is pending, every other CDP
        command on this session (including the evaluate inside ``Click``)
        is blocked. Therefore this keyword MUST be called **before** the
        trigger keyword that opens the dialog. An arm issued after the
        trigger misses the already-broadcast event and times out; the
        dialog is then resolved by the backstop (wait for the ``Set
        Timeout`` value, then auto-dismiss).

        This keyword returns immediately — it does not wait for the
        dialog. Harvest the result with ``Wait For``:

        | ``Promise Next Alert    action=ACCEPT``
        | ``Click    testid:delete-button``
        | ``${text}=    Wait For    ${handle}``

        ``action`` is ``ACCEPT`` (default) or ``DISMISS``. ``prompt_text``
        is the input value for a ``prompt()`` dialog (delivered only on
        ``ACCEPT``). ``Wait For`` returns the dialog text (e.g.
        ``Passwords don't match``).
        """
        normalize_dialog_action(action)  # validate arguments before launching the browser
        return self._ensure_session().arm_dialog(action, prompt_text)

    def wait_for(self, handle: str, timeout: Optional[str] = None) -> str:
        """Waits for the armed promise (dialog or download) to complete and returns its result.

        The result of a dialog promise (``dialog-*`` handle) is the dialog
        text; the result of a download promise (``download-*`` handle) is
        the full path of the saved file. Fails with an AssertionError when
        the deadline passes (dialog failures include the arm-before-trigger
        contract hint). Waiting again on an already-completed handle
        returns the same result immediately. Omitting ``timeout`` uses the
        ``Set Timeout`` value. Unknown or cancelled handles also fail.

        (Name collision with BuiltIn has been checked — Robot BuiltIn has
        no ``Wait For``; its only wait-family keyword is ``Wait Until
        Keyword Succeeds``.)
        """
        seconds = self._timeout if timeout is None else timestr_to_secs(timeout)
        session = self._ensure_session()
        if str(handle).startswith("download-"):
            waiter = session.wait_download
        else:
            waiter = session.wait_dialog
        try:
            return waiter(handle, seconds)
        except PromiseTimeoutError as exc:
            hint = (
                "Promise Next Alert must be issued BEFORE the keyword that "
                "triggers the dialog (CDP blocks session commands while a "
                "dialog is open)."
                if waiter is session.wait_dialog
                else "Promise Next Download must be issued before the download "
                "completes for its result to be captured."
            )
            raise AssertionError(f"{exc} — the armed promise never completed. {hint}") from None
        except PromiseError as exc:
            raise AssertionError(str(exc)) from None
        except KeyError:
            raise AssertionError(
                f"Unknown promise handle {handle!r} — it was never armed, "
                "already cancelled, or the browser was restarted."
            ) from None

    def promise_next_download(self) -> str:
        """Arms the next file download **completion** and returns its handle immediately.

        On the first call, configures the browser with
        ``Browser.setDownloadBehavior(allowAndName)`` so downloads are
        saved into the evidence-convention directory
        (``results/downloads/{suite}/{test}/``). Unlike dialogs, downloads
        do not block the session, so the arm does not strictly need to
        precede the trigger — but the same arm → trigger → ``Wait For``
        convention is followed:

        | ``${p}=    Promise Next Download``
        | ``Click    testid:download-link``
        | ``${file}=    Wait For    ${p}``

        ``Wait For`` returns the **full path** of the saved file.
        """
        session = self._ensure_session()
        if session.download_dir is None:
            session.download_dir = (
                self._resolve_outputdir()
                / "downloads"
                / self._evidence.suite_dir
                / self._evidence.test_dir
            )
        return session.arm_download()

    def handle_alert(
        self,
        action: str = "ACCEPT",
        *trigger: str,
        text: Optional[str] = None,
        prompt_text: Optional[str] = None,
    ) -> str:
        """Convenience keyword that performs arm → trigger → wait → text assertion in one call.

        A one-line pattern that structurally honors the **arm-before-trigger
        contract**. It takes the trigger keyword and its arguments as
        trailing positional arguments and runs them after arming:

        | ``${text}=    Handle Alert    ACCEPT    Click    testid:delete-button``
        | ``Handle Alert    DISMISS    text=Really?    Click    testid:confirm-button``

        ``trigger`` is required (ValueError when omitted) — using this
        keyword without an action that opens a dialog would wait forever.
        Even if the trigger blocks while the dialog is open, the background
        dispatcher handles the response, so this is safe. Returns the
        dialog text; when ``text`` is given, asserts a match
        (AssertionError on mismatch).
        """
        if not trigger:
            raise ValueError(
                "Handle Alert requires the trigger keyword (with its arguments) "
                "as trailing positional arguments, e.g. `Handle Alert    ACCEPT    "
                "Click    testid:button` — arming without a trigger would wait "
                "forever, and triggering before arming deadlocks the session."
            )
        handle = self.promise_next_alert(action, prompt_text)
        from robot.libraries.BuiltIn import BuiltIn  # local import — avoids a circular import

        BuiltIn().run_keyword(*trigger)
        message = self.wait_for(handle, None)
        if text is not None and str(text) != message:
            raise AssertionError(
                f"Alert text mismatch: expected {str(text)!r}, "
                f"dialog said {message!r}"
            )
        return message

    # ------------------------------------------------------------------
    # Recording — record & replay
    # ------------------------------------------------------------------

    def start_recording(self) -> int:
        """Starts recording user/agent input on the current tab.

        Installs the recorder into every future document (plus the current
        one) and streams page events back through a CDP binding. Interactions
        driven by *any* means are captured — manual use of a headed browser,
        ``Click With Real Mouse``, ``Type Text``, or fast-track keywords.

        Recorded event kinds: ``navigate`` (Go To), ``click``, ``fill``
        (typing collapses into one fill per element with the final value),
        and ``press`` (standalone special keys). Main frame only (v1).

        Stop with ``Stop Recording`` (returns the Robot replay script) or
        ``Save Recording`` (writes it to a file).
        """
        return self._ensure_session().start_recording()

    def stop_recording(self, name: str = "Recorded Flow") -> str:
        """Stops recording and returns a replayable Robot script.

        The generated test uses poll-by-default keywords, so replay timing
        does not need to match the recording. Pair with ``Save Recording``
        to persist, or assign the return value for inline use.
        """
        from .recorder import render_summary, to_robot_script

        events = self._ensure_session().stop_recording()
        logger.info(f"Recording stopped — {render_summary(events)}")
        return to_robot_script(events, name=name)

    def save_recording(
        self, path: str, name: str = "Recorded Flow", format: str = "Robot"
    ) -> str:
        """Stops recording and writes the replay script to ``path``.

        ``format`` is ``Robot`` (default, a ``.robot`` replay test) or
        ``Python`` (a plain-Python script against the framework-free core).
        Returns the absolute path written. Parent directories are created.
        """
        from .recorder import to_python_script, to_robot_script

        events = self._ensure_session().stop_recording()
        normalized = str(format).strip().lower()
        if normalized == "robot":
            content = to_robot_script(events, name=name)
        elif normalized == "python":
            content = to_python_script(events, name=name.replace(" ", "_").lower())
        else:
            raise ValueError(f"format must be Robot or Python, got {format!r}")
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info(f"Recording saved: {target}")
        return str(target)

    # ------------------------------------------------------------------
    # Escape hatches — direct CDP/JS access
    # ------------------------------------------------------------------

    def run_javascript(self, expression: str) -> Any:
        """Evaluates an arbitrary JS expression on the page and returns the JSON-serialized result.

        Built on ``Runtime.evaluate`` (returnByValue + awaitPromise), so:
        return values are JSON-serialized (non-serializable values such as
        ``undefined`` or DOM nodes fail), and a ``Promise`` is awaited for
        resolution within the ``Set Timeout`` value. Page-side exceptions
        are promoted to failures as-is. When a frame scope is active, the
        expression is evaluated in the current frame context.
        """
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("Run Javascript requires a non-empty expression")
        return self._ensure_session().evaluate(expression, timeout=self._timeout)

    def execute_cdp_command(self, method: str, params_json: str = "") -> Any:
        """Sends a raw CDP command and returns the response as-is (last-resort escape hatch).

        ``method`` is a CDP Domain.command (e.g.
        ``Emulation.setDeviceMetricsOverride``); ``params_json`` is a JSON
        object string (empty string = ``{}``). The result is the CDP
        response dict verbatim — the gateway to every CDP capability the
        library does not yet expose as keywords (mobile emulation, auth
        details, tracing, etc.).

        Domains outside the page session (``Browser.*``, ``Target.*``,
        ``SystemInfo.*``, ...) are sent to the browser endpoint without a
        session_id; the rest go to the current tab session. CDP error
        responses are promoted to exceptions.
        """
        if not isinstance(method, str) or "." not in method:
            raise ValueError(
                f"Execute CDP Command requires 'Domain.method', got {method!r}"
            )
        raw: dict = {}
        if params_json:
            try:
                parsed = json.loads(params_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"params_json is not valid JSON: {exc}"
                ) from None
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"params_json must be a JSON object, got {type(parsed).__name__}"
                )
            raw = parsed
        session = self._ensure_session()
        if method.split(".", 1)[0] in _BROWSER_LEVEL_DOMAINS:
            return session.connection.send(method, raw)
        return session.connection.send(method, raw, session_id=session.session_id)

    def insert_text(self, text: str) -> None:
        """Inserts finished composed text into the currently focused element (IME bypass).

        Based on ``Input.insertText`` — inserts text directly without
        physical key events, making it the practical input path for IME
        composed text such as Korean (reproducing actual IME composition
        is outside the support boundary). The element must already be
        focused for insertion — call it after ``Click``.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        session = self._ensure_session()
        session.insert_text(text)

    def type_text(self, selector: str, text: str) -> int:
        """Types text into the ``selector`` element via **real key events**.

        Focuses the element, then sends an ``Input.dispatchKeyEvent``
        keyDown/keyUp pair per character — well suited to apps that verify
        ``isTrusted`` or UIs that need keydown listeners. Only printable
        ASCII is typed as real keys; characters that cannot be typed
        directly on a keyboard (Korean, etc.) automatically fall back to
        ``Input.insertText`` (documented boundary). Returns the number of
        characters typed. Existing text is not cleared — clear it first
        (e.g. with ``Fill Text``) or use BACKSPACE.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        session = self._ensure_session()
        return session.type_text(self._bridge_arg(selector), text)

    def press_keys(self, *keys: str) -> None:
        """Presses a sequence of special keys/characters as **real key events**.

        Each argument is a special key name (case-insensitive — ``ENTER``,
        ``TAB``, ``ESC``, ``ARROW_DOWN``, ``BACKSPACE``, ...) or a plain
        string (typed as real key presses, one per character). It targets
        the currently focused element, so it usually follows ``Click`` or
        ``Type Text``:

        | ``Type Text    testid:query    hello``
        | ``Press Keys    ENTER``

        Modifier combinations (CTRL+C style) are not yet provided — if
        needed, compose them directly with ``Execute CDP Command
        Input.dispatchKeyEvent``.
        """
        if not keys:
            raise ValueError("Press Keys requires at least one key")
        session = self._ensure_session()
        for token in keys:
            if resolve_key_token(token) is not None:
                session.press_special_key(token)
                continue
            for ch in str(token):
                if not session.type_character(ch):
                    session.insert_text(ch)

    # ------------------------------------------------------------------
    # Real mouse-event track
    # ------------------------------------------------------------------

    def click_with_real_mouse(self, selector: str) -> str:
        """Clicks the ``selector`` with **real mouse events**.

        Scrolls the element into the viewport (scrollIntoViewIfNeeded), then
        sends mousePressed/mouseReleased at its center coordinates — for
        paths that need a real pointer: ``isTrusted`` checks, ``:hover``
        interplay, occlusion, etc. Returns the click coordinates as an
        ``"x,y"`` string. For ordinary clicks, ``Click`` is the fast
        JS-simulation track.
        """
        x, y = self._ensure_session().real_click_element(self._bridge_arg(selector))
        return f"{x:.0f},{y:.0f}"

    def click_at_coordinates(self, x: float, y: float) -> None:
        """Clicks viewport coordinates ``x, y`` with the real mouse — **canvas partial support path**.

        For clicking coordinates of drawn content in canvas/WebGL apps that
        have no DOM element anchor (partial support: what was clicked must
        be recorded by the app to be verifiable). Combine with
        ``Page.captureScreenshot`` + visual regression (P3) to verify
        canvas state.
        """
        self._ensure_session().real_click_at(float(x), float(y))

    def hover(self, selector: str) -> None:
        """Moves the real mouse over the element to trigger genuine CSS ``:hover``.

        The fast JS track cannot activate the hover pseudo-class — this
        keyword moves the real pointer via ``Input.dispatchMouseEvent
        mouseMoved``.
        """
        self._ensure_session().real_hover(self._bridge_arg(selector))

    def drag_from_to(self, source: str, target: str) -> None:
        """Presses at the center of ``source`` and drags to the center of ``target``.

        A **raw mouse sequence** (press → intermediate moves → release) —
        for drag UIs built on ordinary mousedown/mousemove/mouseup.
        Synthesizing HTML5 drag-and-drop (dragstart/drop events) is outside
        the support boundary.
        """
        session = self._ensure_session()
        session.real_drag(self._bridge_arg(source), self._bridge_arg(target))

    # ------------------------------------------------------------------
    # Scrolling · file upload
    # ------------------------------------------------------------------

    def scroll_by(self, x: float = 0.0, y: float = 0.0) -> None:
        """Scrolls with the real mouse wheel from the viewport center.

        Positive ``y`` = down, positive ``x`` = right (pixels). For
        elements that only enter the DOM after scrolling — virtualized
        grids/infinite scroll — use the ``Scroll By`` → polling assertion
        (``Element Should Exist``) combination:

        | ``Scroll By    y=800``
        | ``Element Should Exist    testid:row-42``

        Wheel input is applied asynchronously, so this waits internally for
        it to settle and returns the final ``window.scrollY``.
        """
        return self._ensure_session().scroll_by(float(x), float(y))

    def scroll_to_element(self, selector: str) -> None:
        """Scrolls the ``selector`` element into the viewport (all selector strategies)."""
        self._ensure_session().scroll_to_element(self._bridge_arg(selector))

    def upload_file(self, selector: str, *file_paths: str) -> None:
        """Sets files on a file input (``<input type=file>``).

        Sets them directly via ``DOM.setFileInputFiles`` without opening
        the OS file picker — the browser fires a ``change`` event right
        after the setting. Paths must be **absolute**. Only
        ``css=``/``testid:`` selectors are supported (DOM.querySelector
        limitation) — other strategies fail with a clear error. Passing
        multiple files sets all of them on a multiple input.
        """
        if not file_paths:
            raise ValueError("Upload File requires at least one file path")
        from pathlib import Path as _P

        absolute = []
        for raw in file_paths:
            p = _P(str(raw)).expanduser().resolve()
            if not p.is_file():
                raise ValueError(f"file not found: {raw!r} (resolved {p})")
            absolute.append(str(p))

        locator = parse(selector)
        if locator.strategy == "testid":
            css = f'[{TESTID_ATTR}="{locator.value}"]'
        elif locator.strategy == "css":
            css = locator.value
        else:
            raise ValueError(
                f"Upload File supports css=/testid: selectors only "
                f"(DOM.querySelector limitation), got {selector!r}"
            )
        self._ensure_session().set_input_files(css, absolute)

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    def get_cookies(self, urls: str = "") -> list:
        """Returns the cookies of the current context as a list of dicts.

        Passing comma-separated URLs in ``urls`` restricts the result to
        cookies related to those origins. Each dict carries the CDP cookie
        fields verbatim: ``name``/``value``/``domain``/``path``/``expires``
        etc.
        """
        url_list = [u.strip() for u in str(urls).split(",") if u.strip()]
        return self._ensure_session().get_cookies(url_list)

    def set_cookie(
        self,
        url: str,
        name: str,
        value: str,
        expires: float = None,
        http_only: bool = False,
        secure: bool = False,
        same_site: str = None,
    ) -> None:
        """Sets a cookie.

        ``url`` is the origin the cookie belongs to (e.g.
        ``http://localhost:8000/``) — cookies cannot be set on ``file://``
        pages, so use an http(s) origin. ``expires`` is Unix epoch seconds;
        ``same_site`` is Strict|Lax|Extended. Setting failures (invalid
        origin, etc.) fail the keyword.
        """
        self._ensure_session().set_cookie(
            url,
            name,
            value,
            expires=expires,
            http_only=bool(http_only),
            secure=bool(secure),
            same_site=same_site,
        )

    def delete_cookie(self, name: str, url: str = None) -> None:
        """Deletes a cookie by name. With ``url``, only that origin's cookie."""
        self._ensure_session().delete_cookie(name, url)

    def delete_all_cookies(self) -> None:
        """Deletes every browser cookie (browser-level — independent of tabs)."""
        self._ensure_session().delete_all_cookies()

    # ------------------------------------------------------------------
    # Viewport / mobile emulation
    # ------------------------------------------------------------------

    def set_viewport_size(
        self,
        width: int,
        height: int,
        device_scale_factor: float = 1.0,
        mobile: bool = False,
    ) -> None:
        """Overrides the viewport size and device metrics (per tab).

        ``Emulation.setDeviceMetricsOverride`` — for responsive layouts
        and mobile viewport checks. Example:
        ``Set Viewport Size    375    812    mobile=${TRUE}``.
        **Pin the viewport before visual regression** captures — rendering
        determinism depends on the viewport. Undo with ``Reset Viewport``.
        """
        self._ensure_session().set_viewport(
            int(width), int(height), float(device_scale_factor), bool(mobile)
        )

    def reset_viewport(self) -> None:
        """Removes the viewport override and returns to the default state."""
        self._ensure_session().reset_viewport()

    # ------------------------------------------------------------------
    # Visual regression
    # ------------------------------------------------------------------

    def page_should_match_baseline(
        self,
        name: str,
        pixel_delta: int = 0,
        max_mismatch_ratio: float = 0.0,
        mask: str = "",
    ) -> None:
        """Asserts that the current viewport screenshot matches the baseline.

        The baseline lives at ``{baseline_dir}/{name}.png`` (``baseline_dir``
        is an import argument, default ``visual-baselines/`` — prefer a
        ``${CURDIR}``-relative path in suites). **Pin the viewport with
        ``Set Viewport Size`` before comparing** — rendering determinism
        depends on the viewport.

        - ``pixel_delta``: allowed per-channel delta (absorbs anti-aliasing).
        - ``max_mismatch_ratio``: upper bound on the mismatched pixel ratio (0.0-1.0).
        - ``mask``: comma-separated selectors — a black overlay is applied
          over matching elements before capture (excludes volatile regions
          such as timestamps). **Using the same mask for the baseline and
          the actual** excludes that region from the comparison. All
          selector strategies (+``deep:``) are supported; the entire
          matching element is masked.
        - Missing baseline: failure + a creation guide message.
        - On failure, leaves ``{name}.actual.png`` + ``{name}.diff.png``
          artifacts under ``{outputdir}/visual/{suite}/{test}/`` and
          **diagnoses the mismatch shape in words**
          (localized/global-shift/scattered).
        - **Baseline refresh**: with ``CDPBROWSER_UPDATE_BASELINES=1``
          set, a mismatch/missing baseline is rewritten and the keyword
          passes.

        Environments without Pillow degrade to exact-match (byte
        comparison) and fail with a ``pip install 'cdpbrowser[visual]'``
        hint when a tolerance is requested. Canvas apps have no element
        anchor, so this keyword + ``Click At Coordinates`` is the
        verification path.
        """
        session = self._ensure_session()
        safe = safe_name(str(name))
        baseline_path = self._baseline_dir / f"{safe}.png"
        update_mode = bool(os.environ.get("CDPBROWSER_UPDATE_BASELINES"))

        mask_selectors = [s.strip() for s in str(mask).split(",") if s.strip()]
        masked = False
        if mask_selectors:
            result = session.call(
                "mask", [self._bridge_arg(s) for s in mask_selectors]
            )
            if not (isinstance(result, dict) and result.get("ok")):
                raise AssertionError(
                    f"masking failed for {mask_selectors!r}: {result!r}"
                )
            masked = True
        try:
            actual = session.capture_screenshot_png()
        finally:
            if masked:
                session.call("unmask")

        if not baseline_path.is_file():
            if update_mode:
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                baseline_path.write_bytes(actual)
                logger.info(f"Created baseline: {baseline_path}")
                return
            raise AssertionError(
                f"Baseline not found: {baseline_path} — run once with "
                "CDPBROWSER_UPDATE_BASELINES=1 (or Update Baseline keyword) "
                "to create it."
            )

        baseline = baseline_path.read_bytes()
        try:
            diff = compare_images(
                baseline,
                actual,
                pixel_delta=int(pixel_delta),
                max_mismatch_ratio=float(max_mismatch_ratio),
            )
        except VisualUnsupportedError as exc:
            raise AssertionError(str(exc)) from None

        if diff.equal:
            return

        if update_mode:
            baseline_path.write_bytes(actual)
            logger.warn(
                f"Baseline updated for {safe!r} — previous diff: {diff.summary()}"
            )
            return

        artifacts_dir = (
            self._resolve_outputdir()
            / "visual"
            / self._evidence.suite_dir
            / self._evidence.test_dir
        )
        try:
            written = save_visual_artifacts(artifacts_dir, safe, actual, diff)
            artifact_note = " | artifacts: " + ", ".join(str(p) for p in written)
        except Exception as exc:  # noqa: BLE001 — an artifact save failure must not change the verdict
            artifact_note = f" | (artifact save failed: {exc})"
        raise AssertionError(
            f"Visual mismatch {safe!r}: {diff.summary()} | diagnosis: "
            f"{diff.diagnose()}{artifact_note}"
        ) from None

    def update_baseline(self, name: str, mask: str = "") -> None:
        """(Re)creates the baseline from the current viewport screenshot.

        Keyword form of the environment-variable mode
        (``CDPBROWSER_UPDATE_BASELINES=1``) applied to a single baseline.
        ``mask`` is the same selector masking as ``Page Should Match
        Baseline`` — create the baseline with the **same mask** as the
        comparison so masking stays consistent.
        """
        session = self._ensure_session()
        safe = safe_name(str(name))
        mask_selectors = [s.strip() for s in str(mask).split(",") if s.strip()]
        masked = False
        if mask_selectors:
            session.call("mask", [self._bridge_arg(s) for s in mask_selectors])
            masked = True
        try:
            png = session.capture_screenshot_png()
        finally:
            if masked:
                session.call("unmask")
        path = self._baseline_dir / f"{safe}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        logger.info(f"Baseline updated: {path}")

    # ------------------------------------------------------------------
    # Evidence keywords
    # ------------------------------------------------------------------

    def set_evidence_mode(self, mode: str) -> str:
        """Sets the evidence mode and returns the previous mode.

        - ``off``: no automatic capture (``Numbered Screenshot`` keeps working).
        - ``on-failure``: (default) the built-in listener captures
          automatically on test failure.
        - ``every-step``: captures automatically after every successful
          ``Click``/``Fill Text``.

        Case and surrounding whitespace are ignored; unknown values raise
        ``ValueError``.
        """
        normalized = str(mode).strip().lower()
        if normalized not in EVIDENCE_MODES:
            raise ValueError(
                f"Evidence mode must be one of {', '.join(EVIDENCE_MODES)}, "
                f"got {mode!r}"
            )
        previous = self._evidence.mode
        self._evidence.mode = normalized
        return previous

    def numbered_screenshot(self, label: str = "step") -> str:
        """Saves the current viewport screenshot as evidence and returns the saved path.

        Captures via ``Page.captureScreenshot`` (viewport-based, not
        fullPage) and saves to
        ``{outputdir}/evidence/{suite}/{test}/NNNN-{label}.png``. ``NNNN``
        is a 4-digit sequence reset per test and shares its numbering with
        automatic captures. ``label`` is normalized to be filename-safe.
        """
        session = self._ensure_session()
        return str(self._capture_screenshot(session, label))

    # ------------------------------------------------------------------
    # Internal — polling/evidence helpers
    # ------------------------------------------------------------------

    def _bridge_arg(self, selector: str) -> dict:
        """Converts a selector DSL string into a bridge argument.

        Parse failures are promoted immediately without starting the
        browser (a bad request must not launch Chrome).
        """
        try:
            return to_bridge_arg(parse(selector))
        except InvalidSelectorError as exc:
            raise InvalidSelectorError(f"Invalid selector {selector!r}: {exc}") from None

    def _poll(self, desc: str, attempt: Callable[[], Tuple[bool, Optional[str], Any]]) -> Any:
        """Runs the poll — promotes timeouts into an AssertionError with a rich failure message."""
        try:
            return poll_until(attempt, self._timeout, self._poll_interval, desc)
        except PollTimeoutError as exc:
            raise AssertionError(str(exc)) from None

    def _assert_poll(
        self,
        desc: str,
        attempt: Callable[[], Tuple[bool, Optional[str], Any]],
        session: PageSession,
        arg: dict,
    ) -> None:
        """Polling for assertions — appends the final element diagnostics to the message on failure."""
        try:
            poll_until(attempt, self._timeout, self._poll_interval, desc)
        except PollTimeoutError as exc:
            message = str(exc)
            diagnostic = self._best_effort_describe(session, arg)
            if diagnostic:
                message += f" | last element state: {diagnostic!r}"
            raise AssertionError(message) from None

    @staticmethod
    def _interpret_action(result: Any) -> Tuple[bool, Optional[str], Any]:
        """Normalizes a bridge action result (``{"ok": ...}``) into a poll attempt result."""
        if isinstance(result, dict):
            if result.get("ok"):
                return (True, None, None)
            return (False, result.get("reason"), result.get("describe"))
        return (False, None, None)

    @staticmethod
    def _best_effort_describe(session: PageSession, arg: dict) -> Any:
        """Quietly collects the final element diagnostics at timeout (failures tolerated)."""
        try:
            expression = (
                "window.__cdpb._describe(window.__cdpb.findOne("
                + json.dumps(arg)
                + "))"
            )
            return session.evaluate(expression)
        except Exception as exc:  # noqa: BLE001 — diagnostics must not shadow the real failure
            logger.debug(f"Failed to collect element diagnostics: {exc}")
            return None

    def _resolve_outputdir(self) -> Path:
        """Resolves the outputdir that becomes the evidence root.

        Priority: Robot ``${OUTPUTDIR}`` → the ``CDPBROWSER_OUTPUT_DIR``
        environment variable → the ``results`` fallback. BuiltIn resolution
        can fail even in listener context (``end_test``), hence the
        fallback.
        """
        try:
            from robot.libraries.BuiltIn import BuiltIn

            value = BuiltIn().get_variable_value("${OUTPUTDIR}")
            if value:
                return Path(str(value))
        except Exception as exc:  # noqa: BLE001 — normal path outside Robot (pytest, etc.)
            logger.debug(f"${{OUTPUTDIR}} not resolvable ({exc}); using fallback")
        env_dir = os.environ.get("CDPBROWSER_OUTPUT_DIR")
        if env_dir:
            return Path(env_dir)
        return Path("results")

    def _evidence_dir(self) -> Path:
        return (
            self._resolve_outputdir()
            / "evidence"
            / self._evidence.suite_dir
            / self._evidence.test_dir
        )

    def _capture_screenshot(self, session: PageSession, label: str) -> Path:
        """Captures a viewport screenshot and saves it as an evidence file.

        Propagates exceptions on failure (the explicit ``Numbered
        Screenshot`` contract). Automatic capture goes through
        :meth:`_auto_capture`, which wraps failures quietly.
        """
        result = session.connection.send(
            "Page.captureScreenshot",
            {"format": "png"},
            session_id=session.session_id,
        )
        data = (result or {}).get("data")
        if not data:
            raise ValueError(
                f"Page.captureScreenshot returned no image data: {result!r}"
            )
        number = self._evidence.next_number()
        directory = self._evidence_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{number:04d}-{safe_name(label)}.png"
        path.write_bytes(base64.b64decode(data))
        logger.info(f"Evidence saved: {path}")
        return path

    def _auto_capture(self, label: str) -> Optional[str]:
        """Automatic evidence capture — only while the browser is alive, never raises."""
        session = self._session
        if session is None or not session.attached:
            logger.debug(
                f"Evidence capture skipped ({label}): browser is not alive"
            )
            return None
        try:
            return str(self._capture_screenshot(session, label))
        except Exception as exc:  # noqa: BLE001 — an evidence failure must not block the test itself
            logger.debug(f"Evidence capture failed ({label}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Internal — startup/teardown
    # ------------------------------------------------------------------

    def _ensure_session(self) -> PageSession:
        with self._lock:
            if self._session is None:
                self._start_browser()
            return self._session  # type: ignore[return-value]

    def _start_browser(self) -> None:
        chrome_path = self._chrome_path_setting or find_chrome()
        if not chrome_path:
            raise ChromeLaunchError(
                "Chrome binary not found. Install Chrome, or point to it via "
                "the `chrome_path` import argument or the "
                "CDPBROWSER_CHROME_PATH environment variable."
            )
        logger.info(f"Starting Chrome: {chrome_path}")
        try:
            process = ChromeProcess(
                chrome_path, headless=self._headless_setting
            ).start()
        except ChromeLaunchError as exc:
            raise ChromeLaunchError(
                f"{exc}\nIf this is not the intended binary, set the "
                "`chrome_path` import argument or the "
                "CDPBROWSER_CHROME_PATH environment variable."
            ) from exc
        try:
            connection = CdpConnection(process.ws_url)
            session = PageSession(connection).attach()
        except BaseException:
            try:
                process.stop()
            except Exception:  # noqa: BLE001 — proceed quietly on cleanup paths
                pass
            raise
        self._process = process
        self._connection = connection
        self._session = session
        self._sessions = [session]
        self._register_atexit()

    def _teardown(self) -> None:
        """Tears down all tabs → connection → process, in that order. Safe even from a partially started state."""
        sessions = list(self._sessions)
        connection, process = self._connection, self._process
        self._sessions = []
        self._session = None
        self._connection = None
        self._process = None
        closers = [lambda: s.close() for s in sessions]
        closers.append(lambda: connection.close())
        closers.append(lambda: process.stop())
        for closer in closers:
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 — teardown must never raise
                logger.debug(f"Ignoring error during browser teardown: {exc}")

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self._final_cleanup)

    def _final_cleanup(self) -> None:
        try:
            with self._lock:
                self._teardown()
        except Exception:  # noqa: BLE001 — exit paths must never raise
            pass

    def __del__(self) -> None:
        try:
            self._final_cleanup()
        except Exception:  # noqa: BLE001
            pass
