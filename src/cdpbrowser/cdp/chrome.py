"""Chrome binary discovery and process launcher.

Design notes:
- Launches with ``--remote-debugging-port=0`` so the OS assigns the port; the
  real endpoint is parsed from the ``DevTools listening on ws://...`` line on
  stderr (reusing ``transport.parse_devtools_url``).
- A daemon thread keeps draining stderr, ruling out the pipe-buffer-full
  stall that would otherwise hang Chrome.
- Starts with ``start_new_session=True`` so signals can reach the whole child
  process group (Chrome tends to leave helper processes behind).
- ``stop()`` is idempotent, and an atexit hook performs the last-resort cleanup.
"""

from __future__ import annotations

import atexit
import glob
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import weakref
from collections import deque
from typing import Deque, List, Mapping, Optional, Sequence, Union

from .errors import ChromeLaunchError
from .transport import parse_devtools_url

__all__ = [
    "ChromeProcess",
    "find_chrome",
    "needs_no_sandbox",
    "resolve_headless",
]

#: Timeout (seconds) used when interpreting headless/auto without a version.
DEFAULT_DEVTOOLS_TIMEOUT = 15.0

_STOP_GRACE = 2.0
_STOP_KILL_GRACE = 5.0
_STDERR_TAIL_LINES = 40

# ----------------------------------------------------------------------
# Detection candidates (module constants so tests can swap them out).
# ----------------------------------------------------------------------

_COMMON_PATHS: List[str] = [
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_PUPPETEER_CACHE_ROOTS: List[str] = [os.path.expanduser("~/.cache/puppeteer")]

_PUPPETEER_CACHE_GLOBS: List[str] = [
    "chrome/*/chrome-linux*/chrome",
    "chrome-headless-shell/*/chrome-headless-shell-linux64/chrome-headless-shell",
]

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", _SIGTERM)


def _version_sort_key(path: str) -> tuple:
    """Extracts the longest version string in a path as the sort key — higher version wins."""
    versions = _VERSION_RE.findall(path)
    if not versions:
        return ()
    best = max(
        versions,
        key=lambda v: (len(v.split(".")), [int(part) for part in v.split(".")]),
    )
    return tuple(int(part) for part in best.split("."))


def _is_executable_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    if os.name != "posix":
        return True
    return os.access(path, os.X_OK)


def find_chrome(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Finds the Chrome binary and returns its absolute path, or ``None``.

    Detection order:
      1. ``CDPBROWSER_CHROME_PATH`` (only when it points to an existing file)
      2. ``CHROME_PATH``
      3. OS-specific standard install paths
      4. puppeteer cache glob (descending version — newest first)
    """
    env = os.environ if env is None else env

    for name in ("CDPBROWSER_CHROME_PATH", "CHROME_PATH"):
        value = env.get(name)
        if value and _is_executable_file(value):
            return value

    for path in _COMMON_PATHS:
        if _is_executable_file(path):
            return path

    candidates: List[str] = []
    for root in _PUPPETEER_CACHE_ROOTS:
        for pattern in _PUPPETEER_CACHE_GLOBS:
            candidates.extend(glob.glob(os.path.join(root, pattern)))
    if candidates:
        candidates.sort(key=_version_sort_key, reverse=True)
        for path in candidates:
            if _is_executable_file(path):
                return path

    return None


def resolve_headless(
    headless: Union[bool, str, None],
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Interprets the headless flag.

    ``True``/``False`` are honored as-is; ``"auto"`` (or ``None``) resolves to
    headless when the ``CI`` env var is set or ``DISPLAY`` is missing.
    """
    env = os.environ if env is None else env
    if headless is True:
        return True
    if headless is False:
        return False
    if env.get("CI"):
        return True
    return not env.get("DISPLAY")


def needs_no_sandbox() -> bool:
    """Whether Chrome must run without its sandbox on this host.

    True when:
    - running under root (euid 0) — sandbox init always fails there; or
    - an explicit override asks for it: ``CDPBROWSER_NO_SANDBOX=1``; or
    - a CI environment is detected (``CI``/``GITHUB_ACTIONS`` etc.) —
      hosted runners abort Chrome (SIGABRT, exit -6) without it because
      unprivileged user namespaces are unavailable.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    env = os.environ
    if str(env.get("CDPBROWSER_NO_SANDBOX", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    return any(
        env.get(name)
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_HOME", "BUILD_ID")
    )


def needs_dev_shm_workaround() -> bool:
    """Whether to add ``--disable-dev-shm-usage`` (small /dev/shm aborts Chrome).

    Hosted CI runners ship a tiny /dev/shm; without this flag Chrome's
    shared-memory regions overflow and the process aborts.
    """
    if str(os.environ.get("CDPBROWSER_DISABLE_DEV_SHM", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    try:
        shm_total = os.statvfs("/dev/shm").f_blocks * os.statvfs("/dev/shm").f_frsize
    except OSError:
        return False
    return shm_total < 1 << 30  # < 1 GiB is the runner signature


# ----------------------------------------------------------------------
# atexit last-resort cleanup
# ----------------------------------------------------------------------

_LIVE_PROCESSES: "weakref.WeakSet[ChromeProcess]" = weakref.WeakSet()
_AT_EXIT_REGISTERED = False


def _register_atexit_hook() -> None:
    global _AT_EXIT_REGISTERED
    if _AT_EXIT_REGISTERED:
        return
    atexit.register(_cleanup_live_processes)
    _AT_EXIT_REGISTERED = True


def _cleanup_live_processes() -> None:
    for process in list(_LIVE_PROCESSES):
        try:
            process.stop()
        except Exception:  # noqa: BLE001 — shutdown paths never raise
            pass


class ChromeProcess:
    """Wrapper around a headless Chrome subprocess (synchronous blocking API).

    Usage::

        chrome = ChromeProcess(headless="auto")
        with chrome:
            conn = CdpConnection(chrome.ws_url)
            try:
                conn.send("Browser.getVersion")
            finally:
                conn.close()
    """

    def __init__(
        self,
        chrome_path: Optional[str] = None,
        *,
        headless: Union[bool, str, None] = "auto",
        no_sandbox: Optional[bool] = None,
        extra_args: Optional[Sequence[str]] = None,
        env: Optional[Mapping[str, str]] = None,
        devtools_timeout: float = DEFAULT_DEVTOOLS_TIMEOUT,
    ) -> None:
        """When ``chrome_path`` is ``None``, falls back to :func:`find_chrome`.

        ``headless``: ``True``/``False``/``"auto"`` — see :func:`resolve_headless`.
        ``no_sandbox``: ``None`` means auto-detect via :func:`needs_no_sandbox`,
        otherwise the explicit value. ``env`` is only used for detection/headless
        resolution; the Chrome subprocess always inherits ``os.environ``.
        """
        self._env = os.environ if env is None else dict(env)

        if chrome_path is None:
            chrome_path = find_chrome(self._env)
        if chrome_path is None:
            raise ChromeLaunchError(
                "Chrome binary not found. Install Chrome or set "
                "CDPBROWSER_CHROME_PATH."
            )
        self._chrome_path = chrome_path
        self._headless_setting = headless
        self._no_sandbox_setting = no_sandbox
        self._extra_args = list(extra_args or [])
        self._devtools_timeout = devtools_timeout

        self._proc: Optional[subprocess.Popen] = None
        self._started = False
        self._stopped = False
        self._ws_url: Optional[str] = None
        self._user_data_dir: Optional[str] = None
        self._stderr_tail: Deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._devtools_event = threading.Event()
        self._stderr_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def ws_url(self) -> Optional[str]:
        """DevTools browser endpoint. ``None`` until start() succeeds."""
        return self._ws_url

    @property
    def chrome_path(self) -> str:
        return self._chrome_path

    @property
    def user_data_dir(self) -> Optional[str]:
        """Temporary user-data-dir. Removed by stop(), but the path is retained."""
        return self._user_data_dir

    @property
    def process(self) -> Optional[subprocess.Popen]:
        return self._proc

    def start(self) -> "ChromeProcess":
        """Starts Chrome and waits for the DevTools endpoint.

        On failure (including early exit) internal resources are cleaned up and a
        :class:`ChromeLaunchError` is raised. Calling start() on an already
        started instance is an error (misuse guard; create a new instance).
        """
        if self._started:
            raise ChromeLaunchError(
                "ChromeProcess.start() called twice; create a new "
                "ChromeProcess instance instead"
            )
        self._started = True

        headless = resolve_headless(self._headless_setting, self._env)
        if self._no_sandbox_setting is None:
            no_sandbox = needs_no_sandbox()
        else:
            no_sandbox = bool(self._no_sandbox_setting)

        self._user_data_dir = tempfile.mkdtemp(prefix="cdpbrowser-chrome-")
        args = [
            self._chrome_path,
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            f"--user-data-dir={self._user_data_dir}",
        ]
        if headless:
            args.append("--headless=new")
        if no_sandbox:
            args.append("--no-sandbox")
        if needs_dev_shm_workaround():
            args.append("--disable-dev-shm-usage")
        args.extend(self._extra_args)

        popen_kwargs = {}
        if os.name == "posix":
            # New session/process group — lets stop() clean up the whole group.
            popen_kwargs["start_new_session"] = True

        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except OSError as exc:
            self._cleanup_user_data_dir()
            raise ChromeLaunchError(
                f"Failed to launch Chrome at {self._chrome_path!r}: {exc}"
            ) from exc

        _LIVE_PROCESSES.add(self)
        _register_atexit_hook()

        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="cdpbrowser-chrome-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        if not self._wait_for_devtools(self._devtools_timeout):
            self._terminate_process()
            self._cleanup_user_data_dir()
            raise ChromeLaunchError(self._build_launch_failure_message())
        return self

    def stop(self) -> None:
        """Stops Chrome and releases resources. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._devtools_event.set()  # unblocks any in-flight wait loop
        try:
            self._terminate_process()
        finally:
            self._cleanup_user_data_dir()

    def __enter__(self) -> "ChromeProcess":
        if self._proc is None:
            self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_stderr(self) -> None:
        proc = self._proc
        stream = proc.stderr if proc is not None else None
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                self._stderr_tail.append(line)
                url = parse_devtools_url(line)
                if url and not self._devtools_event.is_set():
                    self._ws_url = url
                    self._devtools_event.set()
        except Exception:  # noqa: BLE001 — reader failure must not kill the launcher
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _wait_for_devtools(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._devtools_event.wait(0.05):
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                time.sleep(0.2)  # flush the last stderr output
                return self._devtools_event.is_set()
            if time.monotonic() >= deadline:
                return False
        return True

    def _build_launch_failure_message(self) -> str:
        proc = self._proc
        exit_code = proc.poll() if proc is not None else None
        if exit_code is not None:
            detail = f"Chrome exited early with code {exit_code}"
        else:
            detail = (
                f"Timed out after {self._devtools_timeout:.0f}s waiting for "
                "the DevTools endpoint"
            )
        message = f"{detail} (binary: {self._chrome_path})"
        tail = "\n".join(self._stderr_tail).strip()
        if tail:
            message += f"\nLast stderr output:\n{tail}"
        return message

    def _terminate_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            self._signal_group(_SIGTERM)
            try:
                proc.wait(timeout=_STOP_GRACE)
            except subprocess.TimeoutExpired:
                self._signal_group(_SIGKILL)
                try:
                    proc.wait(timeout=_STOP_KILL_GRACE)
                except subprocess.TimeoutExpired:
                    pass  # last-resort failure — leave it to the OS/atexit
        stream = proc.stderr
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _signal_group(self, sig: int) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                return
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    def _cleanup_user_data_dir(self) -> None:
        path = self._user_data_dir
        if path is None:
            return
        shutil.rmtree(path, ignore_errors=True)
