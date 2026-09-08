"""Key mapping tables for building ``Input.dispatchKeyEvent`` parameters.

Pure data + pure functions — no I/O, no knowledge of the transport or
session layers. Every dict carries only the field set that can be merged
into ``Input.dispatchKeyEvent`` params; the caller adds ``type``.

Usage::

    from cdpbrowser.keys import KEY_EVENTS, char_events, resolve_key_token

    fields = resolve_key_token("ENTER")            # special-key token (case-insensitive)
    conn.send("Input.dispatchKeyEvent", {"type": "keyDown", **fields})
    key_up = {k: v for k, v in fields.items() if k != "text"}
    conn.send("Input.dispatchKeyEvent", {"type": "keyUp", **key_up})

    down, up = char_events("a")                    # one printable ASCII char
    conn.send("Input.dispatchKeyEvent", down)
    conn.send("Input.dispatchKeyEvent", up)

Boundaries:

- Assumes a US (en-US) keyboard layout. ``code`` and the virtual key codes
  refer to physical keys on the US layout; the same physical position maps to
  a different key on other layouts. For layout-independent text input, use
  ``Input.insertText`` instead of this module.
- IME-composed characters (non-ASCII such as Hangul) are not synthesized as
  per-key events. The correct approach is to inject the fully composed string
  in one shot via ``Input.insertText``; ``char_events`` enforces this boundary
  by returning ``None`` for non-ASCII input.
- Chrome (verified on 131) only accepts an event as a real key press when the
  ``rawKeyDown``/``keyDown(text)``/``keyUp`` ordering is followed and both
  ``code`` and ``windowsVirtualKeyCode`` are populated. This module's field
  sets are designed to satisfy that requirement.
- Virtual key codes follow the Windows VK convention (physical key identity,
  case-insensitive). Letters are always based on the uppercase key, so both
  'a' and 'A' are 65 (matching Chrome's expectation of
  windowsVirtualKeyCode=65 for an 'a' keydown); digits and symbols equal
  ord(ch) ('5' -> 53, '!' -> 33).
"""

__all__ = ["KEY_EVENTS", "char_events", "resolve_key_token"]


def _event(
    key: str,
    code: str,
    vk: int,
    text: str | None = None,
    location: int | None = None,
) -> dict:
    fields: dict = {
        "key": key,
        "code": code,
        "windows_virtual_key_code": vk,
        "native_virtual_key_code": vk,
    }
    if text is not None:
        fields["text"] = text
    if location is not None:
        fields["location"] = location
    return fields


KEY_EVENTS: dict[str, dict] = {
    "ENTER": _event("Enter", "Enter", 13, text="\r"),
    "TAB": _event("Tab", "Tab", 9, text="\t"),
    "ESCAPE": _event("Escape", "Escape", 27),
    "BACKSPACE": _event("Backspace", "Backspace", 8),
    "DELETE": _event("Delete", "Delete", 46),
    "ARROW_UP": _event("ArrowUp", "ArrowUp", 38),
    "ARROW_DOWN": _event("ArrowDown", "ArrowDown", 40),
    "ARROW_LEFT": _event("ArrowLeft", "ArrowLeft", 37),
    "ARROW_RIGHT": _event("ArrowRight", "ArrowRight", 39),
    "HOME": _event("Home", "Home", 36),
    "END": _event("End", "End", 35),
    "PAGE_UP": _event("PageUp", "PageUp", 33),
    "PAGE_DOWN": _event("PageDown", "PageDown", 34),
    "SPACE": _event(" ", "Space", 32, text=" "),
    "CONTROL": _event("Control", "ControlLeft", 17, location=1),
    "SHIFT": _event("Shift", "ShiftLeft", 16, location=1),
    "ALT": _event("Alt", "AltLeft", 18, location=1),
    "META": _event("Meta", "MetaLeft", 91, location=1),
}

KEY_EVENTS["ESC"] = KEY_EVENTS["ESCAPE"]
KEY_EVENTS["CTRL"] = KEY_EVENTS["CONTROL"]


def char_events(ch: str) -> tuple[dict, dict] | None:
    """Return the (keyDown, keyUp) parameter pair for one printable ASCII char (0x20-0x7E).

    Alphabetic -> ``Key<UPPER>``, digit -> ``Digit<ch>``, other symbols -> ``code=""``.
    Anything else (empty/multi-char, non-ASCII, control) returns ``None``. The keyUp
    carries no ``text`` (the CDP ``text`` hint is keyDown-only).
    """
    if len(ch) != 1:
        return None
    code_point = ord(ch)
    if not 0x20 <= code_point <= 0x7E:
        return None
    if ch.isalpha():
        code = f"Key{ch.upper()}"
    elif ch.isdigit():
        code = f"Digit{ch}"
    else:
        code = ""
    vk = ord(ch.upper())
    key_down = {
        "type": "keyDown",
        "key": ch,
        "code": code,
        "windows_virtual_key_code": vk,
        "native_virtual_key_code": vk,
        "text": ch,
    }
    key_up = {
        "type": "keyUp",
        "key": ch,
        "code": code,
        "windows_virtual_key_code": vk,
        "native_virtual_key_code": vk,
    }
    return key_down, key_up


def resolve_key_token(token: str) -> dict | None:
    """Resolve an ``"ENTER"``-style token (case-insensitive) into a KEY_EVENTS entry."""
    return KEY_EVENTS.get(token.upper())
