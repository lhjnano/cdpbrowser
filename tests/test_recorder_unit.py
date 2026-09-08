"""Recorder renderer unit tests — pure functions, no browser needed."""

from __future__ import annotations

import pytest

from cdpbrowser.recorder import (
    render_summary,
    to_python_script,
    to_robot_script,
)

EVENTS = [
    {"type": "navigate", "url": "https://example.com/"},
    {"type": "click", "selector": "testid:submit-button"},
    {"type": "fill", "selector": "testid:name", "value": "Jane Doe"},
    {"type": "press", "key": "ENTER"},
]


class TestRobotRenderer:
    def test_settings_and_teardown_present(self):
        script = to_robot_script(EVENTS)
        assert "Library           CdpBrowser" in script
        assert "[Teardown]    Close Browser" in script
        assert "*** Test Cases ***" in script

    def test_each_event_renders_as_expected(self):
        script = to_robot_script(EVENTS)
        assert "Go To    https://example.com/" in script
        assert "Click    testid:submit-button" in script
        assert "Fill Text    testid:name    Jane Doe" in script
        assert "Press Keys    ENTER" in script

    def test_values_with_spaces_stay_verbatim(self):
        script = to_robot_script(
            [{"type": "fill", "selector": "testid:q", "value": "two words"}]
        )
        assert "Fill Text    testid:q    two words" in script
        assert '"two words"' not in script  # quotes would become literal

    def test_values_with_robot_syntax_are_escaped(self):
        script = to_robot_script(
            [{"type": "fill", "selector": "testid:q", "value": "${danger}"}]
        )
        assert "Fill Text    testid:q    \\${danger}" in script

    def test_custom_name_and_timeout(self):
        script = to_robot_script(EVENTS, name="Checkout", timeout="30s")
        assert "Checkout" in script
        assert "Set Timeout    30s" in script

    def test_unknown_event_type_raises(self):
        with pytest.raises(ValueError, match="cannot render"):
            to_robot_script([{"type": "scroll", "delta": 100}])


class TestPythonRenderer:
    def test_compiles(self):
        script = to_python_script(EVENTS)
        compile(script, "<generated>", "exec")  # syntax validity

    def test_uses_core_api(self):
        script = to_python_script(EVENTS)
        assert "from cdpbrowser import ChromeProcess" in script
        assert "session.navigate" in script
        assert 'session.call("click"' in script
        assert 'session.call("setValue"' in script
        assert "press_special_key" in script

    def test_testid_selector_maps_to_bridge_id_strategy(self):
        script = to_python_script(
            [{"type": "click", "selector": "testid:go"}]
        )
        assert '{"s": \'id\', "v": \'go\'}' in script


class TestSummaryAndValidation:
    def test_summary_counts_by_type(self):
        assert "1 navigate" in render_summary(EVENTS)
        assert "1 click" in render_summary(EVENTS)

    def test_rejects_non_dict_events(self):
        with pytest.raises(ValueError):
            to_robot_script(["click"])

    def test_rejects_missing_type(self):
        with pytest.raises(ValueError):
            to_robot_script([{"selector": "x"}])
