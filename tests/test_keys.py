"""Pure unit tests for cdpbrowser.keys (no I/O, no mocks/Chrome needed)."""

from cdpbrowser.keys import KEY_EVENTS, char_events, resolve_key_token


def test_special_keys_vk_codes():
    assert KEY_EVENTS["ENTER"]["windows_virtual_key_code"] == 13
    assert KEY_EVENTS["TAB"]["windows_virtual_key_code"] == 9
    assert KEY_EVENTS["ESCAPE"]["windows_virtual_key_code"] == 27
    assert KEY_EVENTS["BACKSPACE"]["windows_virtual_key_code"] == 8
    assert KEY_EVENTS["DELETE"]["windows_virtual_key_code"] == 46
    assert KEY_EVENTS["HOME"]["windows_virtual_key_code"] == 36
    assert KEY_EVENTS["END"]["windows_virtual_key_code"] == 35
    assert KEY_EVENTS["PAGE_UP"]["windows_virtual_key_code"] == 33
    assert KEY_EVENTS["PAGE_DOWN"]["windows_virtual_key_code"] == 34
    assert KEY_EVENTS["SPACE"]["windows_virtual_key_code"] == 32


def test_arrow_keys_vk_codes():
    assert KEY_EVENTS["ARROW_UP"]["windows_virtual_key_code"] == 38
    assert KEY_EVENTS["ARROW_DOWN"]["windows_virtual_key_code"] == 40
    assert KEY_EVENTS["ARROW_LEFT"]["windows_virtual_key_code"] == 37
    assert KEY_EVENTS["ARROW_RIGHT"]["windows_virtual_key_code"] == 39


def test_function_keys_excluded():
    for name in ("F1", "F5", "F12"):
        assert name not in KEY_EVENTS
        assert resolve_key_token(name) is None


def test_chrome_standard_key_and_code_strings():
    assert KEY_EVENTS["ARROW_UP"]["key"] == "ArrowUp"
    assert KEY_EVENTS["ARROW_UP"]["code"] == "ArrowUp"
    assert KEY_EVENTS["ESCAPE"]["key"] == "Escape"
    assert KEY_EVENTS["ENTER"]["key"] == "Enter"
    assert KEY_EVENTS["ENTER"]["code"] == "Enter"
    assert KEY_EVENTS["SPACE"]["key"] == " "
    assert KEY_EVENTS["SPACE"]["code"] == "Space"


def test_text_fields():
    assert KEY_EVENTS["ENTER"]["text"] == "\r"
    assert KEY_EVENTS["TAB"]["text"] == "\t"
    assert KEY_EVENTS["SPACE"]["text"] == " "
    assert "text" not in KEY_EVENTS["ESCAPE"]


def test_alias_esc_and_ctrl_point_to_same_entry():
    assert KEY_EVENTS["ESC"] == KEY_EVENTS["ESCAPE"]
    assert KEY_EVENTS["CTRL"] == KEY_EVENTS["CONTROL"]
    assert resolve_key_token("ESC") == resolve_key_token("ESCAPE")
    assert resolve_key_token("CTRL") == resolve_key_token("CONTROL")
    assert KEY_EVENTS["CTRL"]["key"] == "Control"
    assert KEY_EVENTS["CTRL"]["code"] == "ControlLeft"


def test_modifiers_are_left_variants_with_location():
    for token in ("CONTROL", "SHIFT", "ALT", "META"):
        entry = KEY_EVENTS[token]
        assert entry["location"] == 1
        assert entry["code"].endswith("Left")


def test_char_events_lowercase_letter():
    down, up = char_events("a")
    assert down["type"] == "keyDown"
    assert down["key"] == "a"
    assert down["code"] == "KeyA"
    assert down["windows_virtual_key_code"] == 65
    assert down["native_virtual_key_code"] == 65
    assert down["text"] == "a"
    assert up["type"] == "keyUp"
    assert up["key"] == "a"
    assert up["windows_virtual_key_code"] == 65
    assert "text" not in up


def test_char_events_uppercase_letter():
    down, _ = char_events("A")
    assert down["key"] == "A"
    assert down["code"] == "KeyA"
    assert down["windows_virtual_key_code"] == 65
    assert down["text"] == "A"


def test_char_events_digit():
    down, _ = char_events("5")
    assert down["key"] == "5"
    assert down["code"] == "Digit5"
    assert down["windows_virtual_key_code"] == 53
    assert down["text"] == "5"


def test_char_events_symbol_and_ascii_boundaries():
    bang, _ = char_events("!")
    assert bang["windows_virtual_key_code"] == 33
    assert bang["text"] == "!"
    assert bang["code"] == ""

    space, _ = char_events(" ")
    assert space["key"] == " "
    assert space["windows_virtual_key_code"] == 32

    tilde, _ = char_events("~")
    assert tilde["windows_virtual_key_code"] == 126


def test_char_events_rejects_non_ascii_and_control_chars():
    assert char_events("\u00fc") is None  # non-ASCII is not per-key typed
    assert char_events("\n") is None
    assert char_events("\t") is None
    assert char_events("\x1f") is None
    assert char_events("") is None
    assert char_events("ab") is None


def test_resolve_key_token_is_case_insensitive():
    assert resolve_key_token("enter") == KEY_EVENTS["ENTER"]
    assert resolve_key_token("Enter") == KEY_EVENTS["ENTER"]
    assert resolve_key_token("aRrOw_Up") == KEY_EVENTS["ARROW_UP"]


def test_resolve_key_token_unknown_returns_none():
    assert resolve_key_token("NoSuch") is None
    assert resolve_key_token("") is None


def test_every_entry_has_required_fields():
    required = ("key", "code", "windows_virtual_key_code", "native_virtual_key_code")
    for name, entry in KEY_EVENTS.items():
        for field in required:
            assert field in entry, f"{name}: missing {field}"
        assert isinstance(entry["windows_virtual_key_code"], int)
        assert entry["windows_virtual_key_code"] == entry["native_virtual_key_code"]
