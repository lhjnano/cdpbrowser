"""Chrome launcher tests.

- find_chrome: filesystem-based detection (env precedence, version sorting, absence)
- resolve_headless / needs_no_sandbox: pure unit tests
- ChromeProcess: real launch (start -> ws_url -> CdpConnection
  Browser.getVersion round trip -> stop -> process exit verified). Real
  launches pass headless=True explicitly to stay deterministic regardless of DISPLAY.
"""

from __future__ import annotations

import os
import re
import time

import pytest

from cdpbrowser.cdp import (
    CdpConnection,
    ChromeLaunchError,
    ChromeProcess,
    find_chrome,
    needs_no_sandbox,
    resolve_headless,
)
from cdpbrowser.cdp import chrome as chrome_mod

#: Chrome for Testing 131 in this environment (path stated by the task)
REAL_CHROME = (
    "/home/lhjnano/.cache/puppeteer/chrome/"
    "linux-131.0.6778.204/chrome-linux64/chrome"
)


@pytest.fixture()
def real_chrome_path():
    path = os.environ.get("CDPBROWSER_CHROME_PATH", REAL_CHROME)
    if not path or not os.path.isfile(path):
        pytest.skip(
            "Chrome for Testing binary not found "
            "(set CDPBROWSER_CHROME_PATH to enable this test)"
        )
    return path


def _write_executable(path: str) -> str:
    """Creates an executable dummy binary (a shell script exiting immediately)."""
    with open(path, "wb") as fh:
        fh.write(b"#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


# ----------------------------------------------------------------------
# find_chrome
# ----------------------------------------------------------------------


class TestFindChrome:
    def test_cdpbrowser_chrome_path_has_highest_priority(
        self, tmp_path, real_chrome_path
    ):
        fake = _write_executable(str(tmp_path / "my-chrome"))
        result = find_chrome(
            {
                "CDPBROWSER_CHROME_PATH": fake,
                "CHROME_PATH": real_chrome_path,
            }
        )
        assert result == fake

    def test_chrome_path_used_when_cdpbrowser_unset(self, real_chrome_path):
        result = find_chrome({"CHROME_PATH": real_chrome_path})
        assert result == real_chrome_path

    def test_nonexistent_overrides_fall_through_to_puppeteer_glob(
        self, monkeypatch, real_chrome_path
    ):
        # Empties common-path candidates so only the glob decides
        monkeypatch.setattr(chrome_mod, "_COMMON_PATHS", [])
        result = find_chrome(
            {
                "CDPBROWSER_CHROME_PATH": "/nonexistent/chrome/xyz",
                "CHROME_PATH": "/nonexistent/chrome/abc",
            }
        )
        assert result == real_chrome_path

    def test_puppeteer_glob_prefers_newest_version(self, tmp_path, monkeypatch):
        cache = tmp_path / "chrome"
        old = cache / "linux-120.0.6099.5" / "chrome-linux"
        new = cache / "linux-131.0.6778.204" / "chrome-linux64"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        old_binary = _write_executable(str(old / "chrome"))
        new_binary = _write_executable(str(new / "chrome"))

        monkeypatch.setattr(chrome_mod, "_PUPPETEER_CACHE_ROOTS", [str(tmp_path)])
        monkeypatch.setattr(chrome_mod, "_COMMON_PATHS", [])
        result = find_chrome({})
        assert result == new_binary

    def test_returns_none_when_nothing_exists(self, tmp_path, monkeypatch):
        empty_root = tmp_path / "empty-cache"
        empty_root.mkdir()
        monkeypatch.setattr(chrome_mod, "_PUPPETEER_CACHE_ROOTS", [str(empty_root)])
        monkeypatch.setattr(chrome_mod, "_COMMON_PATHS", [])
        result = find_chrome(
            {
                "CDPBROWSER_CHROME_PATH": "/nonexistent/chrome/xyz",
                "CHROME_PATH": "/nonexistent/chrome/abc",
            }
        )
        assert result is None

    def test_default_env_detects_real_chrome(self, monkeypatch, real_chrome_path):
        # Without env overrides, the standard detection chain finds the real Chrome
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CDPBROWSER_CHROME_PATH", "CHROME_PATH")
        }
        result = find_chrome(env)
        assert result is not None
        assert os.path.isfile(result)


# ----------------------------------------------------------------------
# headless / no-sandbox detection (pure unit tests)
# ----------------------------------------------------------------------


class TestResolveHeadless:
    @pytest.mark.parametrize("setting", ["auto", None])
    def test_auto_ci_env_is_headless(self, setting):
        assert resolve_headless(setting, {"CI": "true", "DISPLAY": ":0"}) is True

    def test_auto_with_display_and_no_ci_is_not_headless(self):
        assert resolve_headless("auto", {"DISPLAY": ":0"}) is False

    def test_auto_without_display_is_headless(self):
        assert resolve_headless("auto", {}) is True

    def test_auto_with_empty_display_is_headless(self):
        assert resolve_headless("auto", {"DISPLAY": ""}) is True

    def test_explicit_true_overrides_display(self):
        assert resolve_headless(True, {"DISPLAY": ":0"}) is True

    def test_explicit_false_overrides_ci(self):
        assert resolve_headless(False, {"CI": "1"}) is False


class TestNeedsNoSandbox:
    @pytest.mark.skipif(
        not hasattr(os, "geteuid"), reason="geteuid is POSIX-only"
    )
    def test_root_uid_requires_no_sandbox(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert needs_no_sandbox() is True

    @pytest.mark.skipif(
        not hasattr(os, "geteuid"), reason="geteuid is POSIX-only"
    )
    def test_non_root_uid_does_not_need_no_sandbox(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert needs_no_sandbox() is False


# ----------------------------------------------------------------------
# ChromeProcess real launch
# ----------------------------------------------------------------------

_WS_URL_RE = re.compile(r"^ws://[\d.]+:\d+/devtools/browser/\S+$")


@pytest.fixture()
def launch_chrome(real_chrome_path):
    """Creates a really-launched ChromeProcess, cleaned up after the test."""
    processes = []

    def _launch(**kwargs):
        kwargs.setdefault("headless", True)
        kwargs.setdefault("extra_args", ["--disable-dev-shm-usage"])
        proc = ChromeProcess(real_chrome_path, **kwargs)
        processes.append(proc)
        return proc

    yield _launch
    for proc in processes:
        try:
            proc.stop()
        except Exception:  # noqa: BLE001 — a failed cleanup must not block the next one
            pass


class TestChromeProcessReal:
    def test_start_ws_url_get_version_roundtrip_and_stop(self, launch_chrome):
        proc = launch_chrome()
        proc.start()

        assert proc.ws_url is not None
        assert _WS_URL_RE.match(proc.ws_url), f"unexpected ws_url: {proc.ws_url!r}"

        conn = CdpConnection(proc.ws_url)
        try:
            result = conn.send("Browser.getVersion", timeout=10)
        finally:
            conn.close()
        assert result is not None
        assert "Chrome" in result.get("product", "")

        proc.stop()
        assert proc.process is not None
        assert proc.process.poll() is not None

    def test_stop_is_idempotent(self, launch_chrome):
        proc = launch_chrome()
        proc.start()
        proc.stop()
        proc.stop()  # re-calling raises nothing
        assert proc.process.poll() is not None

    def test_context_manager_cleans_up(self, launch_chrome):
        with launch_chrome() as proc:
            assert proc.ws_url is not None
            user_data_dir = proc.user_data_dir
            assert user_data_dir is not None and os.path.isdir(user_data_dir)
            conn = CdpConnection(proc.ws_url)
            try:
                assert conn.send("Browser.getVersion", timeout=10) is not None
            finally:
                conn.close()

        assert proc.process.poll() is not None
        # user-data-dir cleanup (with a small delay allowance)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and os.path.exists(user_data_dir):
            time.sleep(0.05)
        assert not os.path.exists(user_data_dir)

    def test_explicit_no_sandbox_false_is_respected(self, launch_chrome):
        proc = launch_chrome(no_sandbox=False)
        proc.start()
        assert proc.ws_url is not None
        proc.stop()
        assert proc.process.poll() is not None

    def test_start_twice_raises(self, launch_chrome):
        proc = launch_chrome()
        proc.start()
        with pytest.raises(ChromeLaunchError):
            proc.start()
        proc.stop()


class TestChromeProcessFailures:
    def test_bad_binary_raises_with_diagnostics_and_no_leak(self, tmp_path):
        proc = ChromeProcess(
            "/bin/false", headless=True, no_sandbox=True, devtools_timeout=5.0
        )
        with pytest.raises(ChromeLaunchError) as excinfo:
            proc.start()
        message = str(excinfo.value)
        assert "exited early" in message
        assert "/bin/false" in message
        assert proc.process is not None
        assert proc.process.poll() is not None  # no process leak
        # Even on the failure path, user-data-dir is cleaned up
        assert proc.user_data_dir is not None
        assert not os.path.exists(proc.user_data_dir)

    def test_nonexistent_binary_raises(self):
        proc = ChromeProcess("/no/such/dir/chrome-binary", headless=True)
        with pytest.raises(ChromeLaunchError):
            proc.start()
        # Popen itself failed, so there is no process object
        assert proc.process is None

    def test_unresolvable_path_raises_at_construction(self, tmp_path, monkeypatch):
        empty_root = tmp_path / "no-chrome-cache"
        empty_root.mkdir()
        monkeypatch.setattr(chrome_mod, "_PUPPETEER_CACHE_ROOTS", [str(empty_root)])
        monkeypatch.setattr(chrome_mod, "_COMMON_PATHS", [])
        with pytest.raises(ChromeLaunchError, match="not found"):
            ChromeProcess(None, headless=True)
