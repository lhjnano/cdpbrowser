"""Packaging guards (option B) — core usable without robotframework.

Runs subprocesses with a meta_path blocker that makes every ``robot.*``
import fail, then verifies:
- the framework-free core imports and resolves normally,
- accessing ``cdpbrowser.CdpBrowser`` raises the install guidance,
- the bare-name ``CdpBrowser`` shim fails with the same guidance.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

BLOCKER = """
import sys

class _RobotBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "robot" or fullname.startswith("robot."):
            raise ImportError("blocked for test: " + fullname)
        return None

sys.meta_path.insert(0, _RobotBlocker())
"""


def run_blocked(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCKER + textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCoreWithoutRobot:
    def test_core_imports_and_exports(self):
        result = run_blocked(
            """
            import cdpbrowser
            assert cdpbrowser.PageSession is not None
            assert cdpbrowser.CdpConnection is not None
            assert cdpbrowser.ChromeProcess is not None
            assert cdpbrowser.find_chrome is not None
            assert cdpbrowser.__version__
            print("core-ok")
            """
        )
        assert result.returncode == 0, result.stderr
        assert "core-ok" in result.stdout

    def test_cdobrowser_attribute_raises_guidance(self):
        result = run_blocked(
            """
            import cdpbrowser
            try:
                cdpbrowser.CdpBrowser
            except ImportError as exc:
                assert "cdpbrowser[robot]" in str(exc), str(exc)
                print("guard-ok")
            else:
                raise SystemExit("CdpBrowser must raise without robotframework")
            """
        )
        assert result.returncode == 0, result.stderr
        assert "guard-ok" in result.stdout

    def test_star_import_behaves_like_documented(self):
        # Documented: `from cdpbrowser import *` resolves __all__, and the
        # lazy CdpBrowser entry raises the guidance without robotframework.
        result = run_blocked(
            """
            try:
                from cdpbrowser import *
            except ImportError as exc:
                assert "cdpbrowser[robot]" in str(exc), str(exc)
                print("star-guard-ok")
            else:
                raise SystemExit("star import must surface the adapter guard")
            """
        )
        assert result.returncode == 0, result.stderr
        assert "star-guard-ok" in result.stdout

    def test_bare_shim_raises_guidance(self):
        result = run_blocked(
            """
            try:
                import CdpBrowser
            except ImportError as exc:
                assert "cdpbrowser[robot]" in str(exc), str(exc)
                print("shim-guard-ok")
            else:
                raise SystemExit("shim import must fail without robotframework")
            """
        )
        assert result.returncode == 0, result.stderr
        assert "shim-guard-ok" in result.stdout


class TestAdapterWithRobot:
    def test_cdobrowser_resolves_when_robot_present(self):
        # This environment has robotframework installed (dev extras).
        from cdpbrowser import CdpBrowser  # noqa: F401 — resolution itself is the test
        import cdpbrowser

        assert cdpbrowser.CdpBrowser is not None
        assert "CdpBrowser" in dir(cdpbrowser)
