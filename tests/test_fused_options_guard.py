"""raise_request_timeout must tolerate a `fused` with no `options` (fakes, older SDKs)."""

import sys
import types

from fused_render._fused_options import raise_request_timeout


def test_no_options_attribute_is_a_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "fused", types.ModuleType("fused"))
    raise_request_timeout()


def test_options_without_request_timeout_is_a_noop(monkeypatch):
    fake = types.ModuleType("fused")
    fake.options = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "fused", fake)
    raise_request_timeout()
    assert not hasattr(fake.options, "request_timeout")
