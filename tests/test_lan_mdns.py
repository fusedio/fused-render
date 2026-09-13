"""Regression tests for the mDNS/Zeroconf lifecycle in fused_render/lan.py.

INCIDENT: a `Zeroconf()` instance whose `register_service` raised (interface
flapped back up mid-registration) was never closed and its reference was
lost — `self._zeroconf` is only assigned after the register loop succeeds.
Each leaked instance keeps its own thread + asyncio loop + ~3 UDP sockets
alive forever, logging nothing useful, and status() re-advertises (and thus
re-leaks) on every prefs poll once `lan_ips()` no longer matches the never
-updated `self._ips`. 20+ hours of this produced >1000 threads and a GIL
convoy that looked like high CPU with zero real work.

These tests fake the `zeroconf` module (there's no test double for it yet in
this repo) so register/unregister failures are fully controllable and the
"a Zeroconf was constructed" instrumentation lives with the fake, not with
real sockets.
"""
import sys
import types

import pytest

import fused_render.lan as lan_mod


class FakeServiceInfo:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class FakeZeroconf:
    """Records every live (not yet closed) instance on the class itself, so a
    test can assert the population stays flat across repeated failures —
    that's the actual leak regression guard."""

    live: list["FakeZeroconf"] = []
    fail_register = False
    fail_unregister = False

    def __init__(self):
        self.closed = False
        self.registered = []
        self.unregister_calls = 0
        type(self).live.append(self)

    def register_service(self, info):
        if type(self).fail_register:
            raise RuntimeError("register_service failed (simulated)")
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregister_calls += 1
        if type(self).fail_unregister:
            raise RuntimeError("unregister_service failed (simulated)")

    def close(self):
        self.closed = True
        if self in type(self).live:
            type(self).live.remove(self)


@pytest.fixture
def fake_zeroconf(monkeypatch):
    """Install a fake `zeroconf` module so `from zeroconf import ...` inside
    lan.py's methods resolves to our controllable fakes instead of touching
    real sockets."""
    FakeZeroconf.live = []
    FakeZeroconf.fail_register = False
    FakeZeroconf.fail_unregister = False
    fake_module = types.ModuleType("zeroconf")
    fake_module.Zeroconf = FakeZeroconf
    fake_module.ServiceInfo = FakeServiceInfo
    monkeypatch.setitem(sys.modules, "zeroconf", fake_module)
    yield FakeZeroconf
    FakeZeroconf.live = []


def _controller():
    c = lan_mod._Controller()
    c.port = 8080
    return c


def test_advertise_closes_zeroconf_when_register_fails(fake_zeroconf):
    fake_zeroconf.fail_register = True
    c = _controller()

    c._advertise(["10.0.0.5"])

    assert fake_zeroconf.live == [], "the failed Zeroconf instance must be closed, not stranded"
    assert c._zeroconf is None
    assert c.error and "mDNS advertise failed" in c.error


def test_advertise_records_ips_even_on_failure(fake_zeroconf):
    """`status()` re-advertises whenever `lan_ips() != self._ips`. If a
    failed advertise never updates `_ips`, a persistent mDNS failure turns
    into a re-advertise (and re-leak) on every single prefs poll."""
    fake_zeroconf.fail_register = True
    c = _controller()

    c._advertise(["10.0.0.5"])

    assert c._ips == ["10.0.0.5"]


def test_repeated_failing_advertise_does_not_accumulate_zeroconf_instances(fake_zeroconf):
    fake_zeroconf.fail_register = True
    c = _controller()

    for _ in range(25):
        c._advertise(["10.0.0.5"])

    assert len(fake_zeroconf.live) == 0, (
        "repeated failing advertise leaked live Zeroconf instances — this is "
        "the actual production incident (1300+ threads from one flapping "
        "interface over 20 hours)"
    )


def test_advertise_warns_once_not_per_call(fake_zeroconf, caplog):
    fake_zeroconf.fail_register = True
    c = _controller()

    with caplog.at_level("WARNING", logger="fused_render.lan"):
        for _ in range(10):
            c._advertise(["10.0.0.5"])

    warnings = [r for r in caplog.records if "mDNS advertise failed" in r.message]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"


def test_advertise_succeeds_and_registers_both_hosts(fake_zeroconf):
    c = _controller()

    c._advertise(["10.0.0.5"])

    assert c._zeroconf is not None
    assert c.error is None
    assert len(c._infos) == 2  # HOSTNAME + ALIAS_HOSTNAME
    assert c._zeroconf.registered == c._infos


def test_unadvertise_still_closes_when_unregister_fails(fake_zeroconf):
    fake_zeroconf.fail_unregister = True
    c = _controller()
    c._advertise(["10.0.0.5"])
    zc = c._zeroconf
    assert zc is not None

    c._unadvertise()

    assert zc.closed, "close() must run even though unregister_service raised"
    assert c._zeroconf is None
    assert c._ips == []


def test_unadvertise_attempts_every_info_even_if_one_unregister_fails(fake_zeroconf):
    fake_zeroconf.fail_unregister = True
    c = _controller()
    c._advertise(["10.0.0.5"])
    zc = c._zeroconf

    c._unadvertise()

    assert zc.unregister_calls == len(zc.registered) == 2, (
        "a raise on the first unregister_service must not skip the second"
    )


def test_unadvertise_is_noop_when_nothing_advertised(fake_zeroconf):
    c = _controller()
    c._unadvertise()  # must not raise
    assert c._zeroconf is None
