"""Contract tests for the cdp transport exception hierarchy (pure logic)."""

from __future__ import annotations

import pytest

from cdpbrowser.cdp.errors import (
    CdpConnectionClosedError,
    CdpError,
    CdpRemoteError,
    CdpTimeoutError,
    ChromeLaunchError,
)

SUBCLASSES = [
    CdpTimeoutError,
    CdpConnectionClosedError,
    ChromeLaunchError,
    CdpRemoteError,
]


class TestHierarchy:
    def test_cdp_error_derives_from_exception(self):
        assert issubclass(CdpError, Exception)

    def test_all_transport_errors_derive_from_cdp_error(self):
        for cls in SUBCLASSES:
            assert issubclass(cls, CdpError), cls

    def test_siblings_are_not_subclasses_of_each_other(self):
        for a in SUBCLASSES:
            for b in SUBCLASSES:
                if a is b:
                    continue
                assert not issubclass(a, b), (a, b)

    @pytest.mark.parametrize(
        "cls",
        [CdpTimeoutError, CdpConnectionClosedError, ChromeLaunchError, CdpRemoteError],
    )
    def test_each_is_catchable_as_cdp_error(self, cls):
        with pytest.raises(CdpError):
            raise cls("boom")

    def test_plain_cdp_error_is_not_a_remote_error(self):
        with pytest.raises(CdpError):
            raise CdpError("plain")
        assert not isinstance(CdpError("plain"), CdpRemoteError)

    def test_base_error_passes_message_through(self):
        assert str(CdpError("plain")) == "plain"


class TestCdpRemoteErrorContract:
    def test_defaults(self):
        err = CdpRemoteError()
        assert err.code is None
        assert err.message == ""
        assert err.data is None
        assert str(err) == "CDP error None: "

    def test_basic_text_format(self):
        err = CdpRemoteError(code=-32000, message="boom")
        assert str(err) == "CDP error -32000: boom"

    def test_attributes_preserved(self):
        err = CdpRemoteError(code=-32601, message="nope", data={"k": 1})
        assert err.code == -32601
        assert err.message == "nope"
        assert err.data == {"k": 1}

    def test_data_is_appended_with_repr(self):
        err = CdpRemoteError(code=1, message="m", data={"x": 1})
        assert str(err).endswith(" ({'x': 1})")

    def test_none_data_is_not_appended(self):
        err = CdpRemoteError(code=1, message="m", data=None)
        assert not str(err).endswith(")")
        assert "(" not in str(err)

    @pytest.mark.parametrize("data", [0, "", [], False])
    def test_falsy_but_not_none_data_is_appended(self, data):
        # The contract keys on identity (data is not None), not truthiness.
        err = CdpRemoteError(code=1, message="m", data=data)
        assert str(err).endswith(f" ({data!r})")

    def test_args_carry_the_rendered_text(self):
        err = CdpRemoteError(code=-1, message="m")
        assert err.args == (str(err),)

    def test_string_payload_data_renders_quoted(self):
        err = CdpRemoteError(code=-1, message="m", data="detail")
        assert str(err).endswith(" ('detail')")
