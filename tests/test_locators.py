"""Unit tests for cdpbrowser.locators — a pure parser, so no async/plugins needed."""

import dataclasses

import pytest

import cdpbrowser
from cdpbrowser.locators import (
    TESTID_ATTR,
    InvalidSelectorError,
    Locator,
    parse,
    to_bridge_arg,
)


class TestCss:
    def test_id_selector(self):
        assert parse("#login") == Locator("css", "#login")

    def test_class_and_descendant_combinator(self):
        assert parse(".nav > a") == Locator("css", ".nav > a")

    def test_attribute_selector(self):
        assert parse("button.primary[type=submit]") == Locator(
            "css", "button.primary[type=submit]"
        )

    def test_colon_inside_attribute_value_is_not_a_prefix(self):
        # A first colon inside http: must not be mistaken for a prefix
        loc_ = parse('a[href^="http:"]')
        assert loc_.strategy == "css"
        assert loc_.value == 'a[href^="http:"]'

    def test_pseudo_class_is_css(self):
        assert parse("a:hover") == Locator("css", "a:hover")

    def test_surrounding_whitespace_is_stripped(self):
        assert parse("   #id   ") == Locator("css", "#id")

    def test_unknown_prefix_falls_back_to_css(self):
        # Documented rule: an unknown prefix is not an error — it stays CSS
        assert parse("foo:bar") == Locator("css", "foo:bar")

    def test_prefix_matching_is_case_sensitive(self):
        # "Testid:" is an unknown prefix -> CSS
        assert parse("Testid:submit") == Locator("css", "Testid:submit")

    def test_colon_only_selector_is_css(self):
        assert parse("::") == Locator("css", "::")


class TestTestid:
    def test_simple(self):
        assert parse("testid:submit") == Locator("testid", "submit")

    def test_value_may_contain_colon(self):
        assert parse("testid:form:user") == Locator("testid", "form:user")

    def test_only_first_colon_is_the_prefix_separator(self):
        assert parse("testid:a:b:c") == Locator("testid", "a:b:c")

    def test_value_may_contain_spaces(self):
        assert parse("testid:my control") == Locator("testid", "my control")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("testid:")

    def test_attr_constant(self):
        assert TESTID_ATTR == "data-testid"


class TestText:
    def test_simple(self):
        assert parse("text:Log in") == Locator("text", "Log in")

    def test_value_may_contain_colon(self):
        assert parse("text:user: hi") == Locator("text", "user: hi")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("text:")


class TestXpath:
    def test_absolute_path(self):
        assert parse("x://button[@id='go']") == Locator(
            "xpath", "//button[@id='go']"
        )

    def test_bare_double_slash_value(self):
        assert parse("x://") == Locator("xpath", "//")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("x:")


class TestRole:
    def test_simple(self):
        assert parse("role:button") == Locator("role", "button")

    def test_role_and_name_two_parts(self):
        # role/name split on the first space — later spaces stay in the value
        assert parse("role:button Log in") == Locator("role", "button Log in")

    def test_value_may_contain_colon(self):
        assert parse("role:option Page: intro") == Locator(
            "role", "option Page: intro"
        )

    def test_surrounding_whitespace_is_stripped_from_value_ends(self):
        assert parse("  role: heading  ") == Locator("role", "heading")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("role:")

    def test_uppercase_prefix_falls_back_to_css(self):
        assert parse("Role:button") == Locator("css", "Role:button")


class TestLabel:
    def test_simple(self):
        assert parse("label:User name") == Locator("label", "User name")

    def test_value_may_contain_colon(self):
        assert parse("label:user name") == Locator("label", "user name")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("label:")


class TestCssEscapeHatch:
    def test_explicit_prefix(self):
        assert parse("css:#id") == Locator("css", "#id")

    def test_escapes_the_reserved_x_prefix(self):
        # x: is reserved for xpath -> CSS like x:hover needs the css: escape
        assert parse("css:x:hover") == Locator("css", "x:hover")

    def test_empty_value_rejected(self):
        with pytest.raises(InvalidSelectorError):
            parse("css:")


class TestInvalid:
    def test_empty_string(self):
        with pytest.raises(InvalidSelectorError):
            parse("")

    def test_whitespace_only(self):
        with pytest.raises(InvalidSelectorError):
            parse("   \t ")

    def test_non_string_raises_type_error(self):
        with pytest.raises(TypeError):
            parse(123)  # type: ignore[arg-type]

    def test_error_is_value_error_subclass(self):
        assert issubclass(InvalidSelectorError, ValueError)


class TestToBridgeArg:
    def test_css_code(self):
        assert to_bridge_arg(Locator("css", "#id")) == {"s": "css", "v": "#id"}

    def test_testid_code(self):
        assert to_bridge_arg(Locator("testid", "submit")) == {
            "s": "id",
            "v": "submit",
        }

    def test_text_code(self):
        assert to_bridge_arg(Locator("text", "Log in")) == {
            "s": "t",
            "v": "Log in",
        }

    def test_xpath_code(self):
        assert to_bridge_arg(Locator("xpath", "//button")) == {
            "s": "x",
            "v": "//button",
        }

    def test_role_code(self):
        assert to_bridge_arg(Locator("role", "button")) == {
            "s": "r",
            "v": "button",
        }

    def test_label_code(self):
        assert to_bridge_arg(Locator("label", "User name")) == {
            "s": "l",
            "v": "User name",
        }

    @pytest.mark.parametrize(
        "selector,strategy,code,value",
        [
            ("#id", "css", "css", "#id"),
            ("testid:submit", "testid", "id", "submit"),
            ("text:Log in", "text", "t", "Log in"),
            ("x://a[1]", "xpath", "x", "//a[1]"),
            ("role:button", "role", "r", "button"),
            ("label:Email", "label", "l", "Email"),
        ],
    )
    def test_parse_then_bridge_roundtrip(self, selector, strategy, code, value):
        loc_ = parse(selector)
        assert (loc_.strategy, loc_.value) == (strategy, value)
        assert to_bridge_arg(loc_) == {"s": code, "v": value}

    def test_non_locator_rejected(self):
        with pytest.raises(TypeError):
            to_bridge_arg("#id")  # type: ignore[arg-type]

    def test_unknown_strategy_rejected_at_serialization(self):
        with pytest.raises(InvalidSelectorError):
            to_bridge_arg(Locator("regex", ".*"))


class TestLocatorType:
    def test_locator_is_frozen(self):
        loc_ = parse("#id")
        with pytest.raises(dataclasses.FrozenInstanceError):
            loc_.value = "other"

    def test_locator_is_hashable_and_equal(self):
        assert parse("#id") == parse("#id")
        assert len({parse("#id"), parse("#id")}) == 1


class TestModule:
    def test_version(self):
        assert cdpbrowser.__version__ == "0.1.0"

    @pytest.mark.parametrize(
        "selector",
        ["#a", "testid:b", "text:c", "x://d", "css:e", "weird:f", "x", ":"],
    )
    def test_parse_never_returns_unknown_strategy(self, selector):
        assert parse(selector).strategy in {"css", "testid", "text", "xpath"}
