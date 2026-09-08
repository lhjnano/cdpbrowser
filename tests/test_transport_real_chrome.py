"""Integration smoke tests against real Chrome.

The websockets 17.x + real Chrome DevTools Protocol combination cannot be
verified by mocks alone, so Chrome for Testing 131 runs headless for real round trips.

- Chrome path: the ``CDPBROWSER_CHROME_PATH`` env var or the puppeteer-cache default
- pytest.skip when no binary is present
- Parses ``DevTools listening on ws://...`` from stderr (15s timeout)
  — the parser reuses ``cdpbrowser.cdp.transport.parse_devtools_url``,
  sharing logic with the task-3 Chrome launcher."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import List, Optional

import pytest

from cdpbrowser.cdp import CdpConnection
from cdpbrowser.cdp.transport import parse_devtools_url

DEFAULT_CHROME = (
    "/home/lhjnano/.cache/puppeteer/chrome/"
    "linux-131.0.6778.204/chrome-linux64/chrome"
)
DEVTOOLS_TIMEOUT = 15.0


def _chrome_path() -> Optional[str]:
    path = os.environ.get("CDPBROWSER_CHROME_PATH", DEFAULT_CHROME)
    if path and os.path.isfile(path):
        return path
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_devtools_url(proc: subprocess.Popen, timeout: float) -> Optional[str]:
    """Reads stderr on a side thread until the DevTools URL appears."""
    chunks: List[str] = []

    def reader() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                chunks.append(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — stream end from process exit is normal
            pass

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = parse_devtools_url("".join(chunks))
        if url:
            return url
        if proc.poll() is not None:
            time.sleep(0.2)  # flush the last output
            break
        time.sleep(0.05)
    return parse_devtools_url("".join(chunks))


@pytest.fixture()
def chrome():
    path = _chrome_path()
    if path is None:
        pytest.skip(
            "Chrome for Testing binary not found "
            "(set CDPBROWSER_CHROME_PATH to enable this test)"
        )

    port = _free_port()
    user_data_dir = tempfile.mkdtemp(prefix="cdpbrowser-chrome-test-")
    args = [
        path,
        "--headless=new",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        f"--user-data-dir={user_data_dir}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")

    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        ws_url = _read_devtools_url(proc, timeout=DEVTOOLS_TIMEOUT)
    except BaseException:
        _terminate(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise
    if not ws_url:
        _terminate(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        pytest.fail(
            f"Chrome did not report a DevTools endpoint within "
            f"{DEVTOOLS_TIMEOUT}s (exit={proc.poll()})"
        )

    connection = CdpConnection(ws_url)
    try:
        yield connection
    finally:
        connection.close()
        _terminate(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _terminate(proc: subprocess.Popen) -> None:
    """Terminates the Chrome process for sure (zombie prevention)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    if proc.stderr is not None:
        proc.stderr.close()


class TestRealChrome:
    def test_browser_get_version_roundtrip(self, chrome):
        result = chrome.send("Browser.getVersion", timeout=10)
        assert result is not None
        product = result.get("product", "")
        assert "Chrome" in product, f"unexpected product string: {product!r}"

    def test_target_get_targets_roundtrip(self, chrome):
        result = chrome.send("Target.getTargets", timeout=10)
        assert result is not None
        assert isinstance(result.get("targetInfos"), list)
        assert len(result["targetInfos"]) >= 1  # even headless has an about:blank target
