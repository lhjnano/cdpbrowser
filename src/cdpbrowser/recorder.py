"""Recorded-event renderers — turn a recording into replayable scripts.

Input: the event list produced by :meth:`cdpbrowser.page.PageSession.stop_recording`
(each event is a small dict: ``navigate``/``click``/``fill``/``press``).
Output: a self-contained script that replays the flow.

Two renderers:

- :func:`to_robot_script` — a ``.robot`` file using the ``CdpBrowser``
  keyword adapter (poll-by-default makes the replay resilient).
- :func:`to_python_script` — a plain-Python script against the framework-free
  core, for users outside Robot.

Renderers are pure functions (no I/O) — unit-testable without a browser.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

__all__ = ["to_robot_script", "to_python_script", "render_summary"]


def _require(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(events, (list, tuple)):
        raise TypeError(f"events must be a list, got {type(events).__name__}")
    out: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or "type" not in event:
            raise ValueError(f"invalid recording event: {event!r}")
        out.append(event)
    return out


def render_summary(events: Sequence[Dict[str, Any]]) -> str:
    """One-line human summary of a recording (counts per event type)."""
    counts: Dict[str, int] = {}
    for event in _require(events):
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    parts = [f"{count} {kind}" for kind, count in sorted(counts.items())]
    return f"{len(parts) and sum(counts.values()) or 0} events: " + ", ".join(parts)


def _robot_escape(value: Any) -> str:
    """Escapes a value for a Robot Framework cell.

    Robot keeps single spaces inside a cell verbatim, so plain values need
    no quoting — quotes would become part of the string. Only the syntax
    characters Robot interprets are escaped (``$``/``@``/``&`` variable
    starts, backslashes, and ``#`` comments).
    """
    text = str(value)
    if text == "":
        return "${EMPTY}"
    text = text.replace("\\", "\\\\")
    text = text.replace("$", "\\$")
    text = text.replace("@", "\\@")
    text = text.replace("&", "\\&")
    text = text.replace("#", "\\#")
    return text


def _python_escape(value: Any) -> str:
    return repr(str(value))


def _bridge_arg_expr(selector: str) -> str:
    """A recorder selector -> a Python dict-literal expression for the bridge."""
    if ":" in selector:
        strategy, _, value = selector.partition(":")
        codes = {"testid": "id", "css": "css", "text": "t", "label": "l"}
        code = codes.get(strategy)
        if code is not None:
            return '{"s": %s, "v": %s}' % (_python_escape(code), _python_escape(value))
    return '{"s": "css", "v": %s}' % _python_escape(selector)


def to_robot_script(
    events: Sequence[Dict[str, Any]],
    name: str = "Recorded Flow",
    timeout: str = "10s",
) -> str:
    """Renders the recording as a standalone ``.robot`` test file.

    The replay relies on poll-by-default keywords, so replay timing does
    not need to match recording timing. ``navigate`` events become
    ``Go To``; ``click`` → ``Click``; ``fill`` → ``Fill Text``;
    ``press`` → ``Press Keys``.

    Actions recorded with a **dialog** observation (a JS dialog followed
    them) are wrapped with the arm-before-trigger contract
    (``Promise Next Alert`` / ``Wait For`` + text assertion). Actions with
    a **download** observation are wrapped with ``Promise Next Download`` /
    ``Wait For``.
    """
    _require(events)
    lines = [
        "*** Settings ***",
        f"Documentation     Auto-generated replay of a recorded browser flow — {name}.",
        "...               Edit freely; keywords poll by default, so timing",
        "...               differences from the recording are absorbed.",
        "Library           CdpBrowser",
        "",
        "*** Test Cases ***",
        str(name).replace("\n", " "),
        "    [Teardown]    Close Browser",
        f"    Set Timeout    {timeout}",
    ]
    pending = ""  # reserved for future block-style wrapping
    del pending
    for event in events:
        kind = event["type"]
        if kind not in ("navigate", "click", "fill", "press"):
            raise ValueError(f"cannot render event type {kind!r}: {event!r}")
        has_dialog = "dialog" in event
        has_download = "download" in event
        if has_dialog:
            lines.append("    ${alert}=    Promise Next Alert    action=ACCEPT")
        if has_download:
            lines.append("    ${dl}=    Promise Next Download")

        action = _render_action(kind, event)
        indent = "    " if (has_dialog or has_download) else "    "
        for line in action:
            lines.append(indent + line)

        if has_download:
            lines.append("    ${file}=    Wait For    ${dl}")
            lines.append("    Log    downloaded: ${file}")
        if has_dialog:
            lines.append("    ${text}=    Wait For    ${alert}")
            lines.append(
                f"    Should Be Equal As Strings    ${{text}}    {_robot_escape(event['dialog'])}"
            )
    return "\n".join(lines) + "\n"


def _render_action(kind: str, event: Dict[str, Any]) -> List[str]:
    if kind == "navigate":
        return [f"Go To    {_robot_escape(event['url'])}"]
    if kind == "click":
        return [f"Click    {_robot_escape(event['selector'])}"]
    if kind == "fill":
        return [
            f"Fill Text    {_robot_escape(event['selector'])}"
            f"    {_robot_escape(event.get('value', ''))}"
        ]
    if kind == "press":
        return [f"Press Keys    {_robot_escape(event['key'])}"]
    raise ValueError(kind)


def to_python_script(
    events: Sequence[Dict[str, Any]],
    name: str = "recorded_flow",
) -> str:
    """Renders the recording as a plain-Python replay script (core API)."""
    _require(events)
    lines = [
        '"""Auto-generated replay — edit freely. Core API, no Robot needed."""',
        "",
        "import pathlib",
        "",
        "from cdpbrowser import ChromeProcess, CdpConnection, PageSession, find_chrome",
        "",
        "",
        f"def {name}() -> None:",
        "    chrome = ChromeProcess(find_chrome(), headless=True)",
        "    chrome.start()",
        "    session = None",
        "    try:",
        "        session = PageSession(CdpConnection(chrome.ws_url)).attach()",
    ]
    for event in events:
        kind = event["type"]
        if kind == "navigate":
            lines.append(
                f"        session.navigate({_python_escape(event['url'])})"
            )
        elif kind == "click":
            lines.append(
                f"        assert session.call(\"click\", {_bridge_arg_expr(event['selector'])}) "
                "== {\"ok\": True}"
            )
        elif kind == "fill":
            lines.append(
                f"        assert session.call(\"setValue\", {_bridge_arg_expr(event['selector'])}, "
                f"{_python_escape(event.get('value', ''))}) == {{\"ok\": True}}"
            )
        elif kind == "press":
            lines.append(
                f"        session.press_special_key({_python_escape(event['key'])})"
            )
        else:
            raise ValueError(f"cannot render event type {kind!r}: {event!r}")
    lines.extend(
        [
        "    finally:",
        "        if session is not None:",
        "            session.close()",
        "        chrome.stop()",
        "",
        "",
        'if __name__ == "__main__":',
        f"    {name}()",
        ]
    )
    return "\n".join(lines) + "\n"
