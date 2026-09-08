"""Selector DSL parser.

A pure-function module that parses string selectors into :class:`Locator`
objects and converts them into the JSON wire form passed to the CDP bridge
(bridge.js). No I/O, no side effects.

Selector syntax — the prefix is decided by the *first* colon:

==================  ==========  =============================================
Input               strategy    Meaning
==================  ==========  =============================================
``#id`` / ``.a>b``  ``css``     No prefix -> CSS selector (default)
``testid:submit``   ``testid``  Matches the :data:`TESTID_ATTR` attribute
``text:Log in``     ``text``    Text matching (exact/partial decided by the bridge)
``x://button[1]``   ``xpath``   XPath
``css:#id``         ``css``     Explicit CSS escape hatch
``role:button``     ``role``    ARIA role matching — explicit role attribute
                                first, plus an implicit role map (button/
                                input/select/h1..h6 etc.), case-insensitive
                                exact match. The value may carry a second
                                part after the first space, as in
                                ``role:button Save`` (name = accessible name).
``label:User name`` ``label``   Accessible name (simplified accname),
                                case-insensitive exact match
==================  ==========  =============================================

Rules and edge cases:

* Prefixes are **case-sensitive**. ``Testid:...`` is an unknown prefix and
  is therefore passed through as CSS.
* The value is *everything after the first colon*. The value itself may
  contain colons (``testid:form:user`` -> ``form:user``).
* **An unknown prefix is interpreted as CSS, not an error.** CSS selectors
  legitimately contain colons (``a:hover``, ``a[href^="http:"]``), so a
  colon alone cannot be an error. Note instead that a typo prefix
  (``tid:submit``) fails later at the browser stage as a CSS selector error.
* ``x:`` is reserved for xpath, so CSS of the form "tag x + pseudo-class"
  (``x:hover``) must escape explicitly as ``css:x:hover``.
* Leading/trailing whitespace is stripped before parsing; whitespace inside
  the value is preserved.
* A whitespace-only selector, or a prefixed selector with an empty value
  (``testid:``, ``text:``, ``x:``, ``css:``), raises
  :class:`InvalidSelectorError`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace as _replace

__all__ = [
    "TESTID_ATTR",
    "Locator",
    "InvalidSelectorError",
    "parse",
    "to_bridge_arg",
]

#: The HTML attribute name matched by the ``testid:`` strategy.
TESTID_ATTR = "data-testid"

_CSS_PREFIX = "css:"
_TESTID_PREFIX = "testid:"
_TEXT_PREFIX = "text:"
_XPATH_PREFIX = "x:"
_ROLE_PREFIX = "role:"
_LABEL_PREFIX = "label:"

#: Strategy codes understood by the bridge (bridge.js). Compact wire format.
_BRIDGE_CODES = {
    "css": "css",
    "testid": "id",
    "text": "t",
    "xpath": "x",
    "role": "r",
    "label": "l",
    "ext": "e",
}


class InvalidSelectorError(ValueError):
    """Raised when a selector string cannot be parsed."""


@dataclass(frozen=True)
class Locator:
    """A parsed selector.

    Attributes:
        strategy: ``"css"`` | ``"testid"`` | ``"text"`` | ``"xpath"`` |
            ``"role"`` | ``"label"``.
        value: The value passed to the strategy (everything after the prefix).
        deep: The ``deep:`` modifier — pierce open shadow DOM (opt-in).
    """

    strategy: str
    value: str
    deep: bool = False


_DEEP_PREFIX = "deep:"
_EXT_PREFIX = "ext:"


def parse(selector: str) -> Locator:
    """Parse a selector string into a :class:`Locator`.

    See the module docstring for syntax and edge cases. The ``deep:``
    modifier, placed before any other strategy, extends matching into open
    shadow DOM (e.g. ``deep:testid:submit``, ``deep:css:.btn > span``).
    Nesting (``deep:deep:...``) is ignored.

    Raises:
        TypeError: ``selector`` is not a str.
        InvalidSelectorError: The selector is empty/whitespace-only, or a
            prefixed selector has an empty value (``testid:`` etc.).
    """
    if not isinstance(selector, str):
        raise TypeError(f"selector must be str, got {type(selector).__name__}")

    s = selector.strip()
    if not s:
        raise InvalidSelectorError("selector must not be empty")

    deep = False
    while s.startswith(_DEEP_PREFIX):
        deep = True
        s = s[len(_DEEP_PREFIX):].strip()
    if not s:
        raise InvalidSelectorError(
            f"{_DEEP_PREFIX!r} selector requires an inner selector (got {selector!r})"
        )

    if s.startswith(_CSS_PREFIX):
        return _replace(_prefixed("css", s, _CSS_PREFIX), deep=deep)
    if s.startswith(_XPATH_PREFIX):
        return _replace(_prefixed("xpath", s, _XPATH_PREFIX), deep=deep)
    if s.startswith(_TESTID_PREFIX):
        return _replace(_prefixed("testid", s, _TESTID_PREFIX), deep=deep)
    if s.startswith(_TEXT_PREFIX):
        return _replace(_prefixed("text", s, _TEXT_PREFIX), deep=deep)
    if s.startswith(_ROLE_PREFIX):
        return _replace(_prefixed("role", s, _ROLE_PREFIX), deep=deep)
    if s.startswith(_LABEL_PREFIX):
        return _replace(_prefixed("label", s, _LABEL_PREFIX), deep=deep)
    if s.startswith(_EXT_PREFIX):
        return _replace(_prefixed("ext", s, _EXT_PREFIX), deep=deep)

    # Unknown prefixes (regardless of colon presence) pass through as CSS.
    return Locator("css", s, deep=deep)


def _prefixed(strategy: str, s: str, prefix: str) -> Locator:
    value = s[len(prefix):].strip()
    if not value:
        raise InvalidSelectorError(
            f"{prefix!r} selector requires a value (got {s!r})"
        )
    return Locator(strategy, value)


def to_bridge_arg(locator: Locator) -> dict:
    """Serialize a :class:`Locator` into the dict passed to bridge.js.

    Shape: ``{"s": <strategy code>, "v": <value>}`` (plus ``"p": 1`` when
    piercing shadow DOM). Code mapping: css -> ``"css"``, testid -> ``"id"``,
    text→``"t"``, xpath→``"x"``, role→``"r"``, label→``"l"``.

    Raises:
        TypeError: ``locator`` is not a Locator instance.
        InvalidSelectorError: The Locator has an unknown strategy.
    """
    if not isinstance(locator, Locator):
        raise TypeError("to_bridge_arg expects a Locator; call parse() first")
    try:
        code = _BRIDGE_CODES[locator.strategy]
    except KeyError:
        raise InvalidSelectorError(
            f"unknown strategy: {locator.strategy!r}"
        ) from None
    arg = {"s": code, "v": locator.value}
    if locator.deep:
        arg["p"] = 1
    return arg
