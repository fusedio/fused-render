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
    that's the actual leak regression guard. `created_count` additionally
    counts every instance ever constructed (even ones already closed), so a
    rate-limiting test can tell "0 attempts" apart from "N attempts, all
    cleaned up"."""

    live: list["FakeZeroconf"] = []
    created_count = 0
    fail_register = False
    fail_unregister = False

    def __init__(self):
        self.closed = False
        self.registered = []
        self.unregister_calls = 0
        type(self).live.append(self)
        type(self).created_count += 1

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
    FakeZeroconf.created_count = 0
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


def test_advertise_does_not_record_ips_on_failure(fake_zeroconf):
    """An earlier fix recorded `_ips` on a failed advertise to stop
    `status()`/`_watch` from re-attempting (and re-leaking) on every poll.
    That disabled the only mDNS recovery path: both gate re-advertise on
    `ips != self._ips`, so once `_ips` equals the current (unreachable)
    addresses, mDNS never recovers even after Wi-Fi comes back on the SAME
    address — the common wake-from-sleep case. The leak is now bounded via a
    retry backoff instead (see the ADVERTISE_RETRY_INTERVAL_S tests below),
    so `_ips`/`_ip` are left exactly as `_unadvertise()` set them
    (empty/None) on failure — which also keeps them consistent with each
    other (`_ip` is documented as "the first of `_ips`")."""
    fake_zeroconf.fail_register = True
    c = _controller()

    c._advertise(["10.0.0.5"])

    assert c._ips == []
    assert c._ip is None


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


# -- bounded retry (D871 follow-up: bounded leak MUST keep the recovery path)


class _FakeClock:
    """A controllable stand-in for time.time(), monkeypatched onto lan_mod's
    `time` module so tests can advance past ADVERTISE_RETRY_INTERVAL_S without
    a real sleep."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(lan_mod.time, "time", clock)
    return clock


def test_same_address_flap_recovers_via_reannounce(fake_zeroconf, fake_clock, monkeypatch):
    """The user's scenario: Wi-Fi flaps down and back up at the SAME address.
    `register_service` raises on the dead socket during the flap; `status()`/
    `_watch` both drive re-advertise through `_reannounce`, which must
    actually retry (and succeed) once the interface — and the backoff — have
    had time to recover, even though the address never changed."""
    fake_zeroconf.fail_register = True
    c = _controller()
    monkeypatch.setattr(c, "_start_tls", lambda ips: None)  # TLS is not under test here

    c._reannounce(["10.0.0.5"])  # advertise fails during the flap
    assert c.error and "mDNS advertise failed" in c.error
    assert c._zeroconf is None

    fake_clock.advance(lan_mod.ADVERTISE_RETRY_INTERVAL_S + 1)
    fake_zeroconf.fail_register = False  # the interface is back
    c._reannounce(["10.0.0.5"])  # same address, but backoff has elapsed

    assert c.error is None, "mDNS must recover on the next poll once the backoff elapses"
    assert c._ips == ["10.0.0.5"]
    assert c._ip == "10.0.0.5"


def test_retry_is_rate_limited_across_rapid_polls(fake_zeroconf, fake_clock, monkeypatch):
    """N rapid polls against a still-failing interface must produce far
    fewer than N actual advertise attempts."""
    fake_zeroconf.fail_register = True
    c = _controller()
    monkeypatch.setattr(c, "_start_tls", lambda ips: None)

    n = 25
    for _ in range(n):
        c._reannounce(["10.0.0.5"])
        fake_clock.advance(1.0)  # much shorter than ADVERTISE_RETRY_INTERVAL_S

    assert fake_zeroconf.created_count < n // 2, (
        f"expected far fewer than {n} advertise attempts, got {fake_zeroconf.created_count}"
    )
    assert fake_zeroconf.created_count >= 1


def test_zeroconf_population_stays_flat_across_many_backed_off_retries(fake_zeroconf, fake_clock, monkeypatch):
    """The original regression guard (no leaked live instances) must still
    hold now that retry is restored: each retry, spaced a full backoff
    interval apart so every one of them actually attempts, must still close
    its Zeroconf instance on failure."""
    fake_zeroconf.fail_register = True
    c = _controller()
    monkeypatch.setattr(c, "_start_tls", lambda ips: None)

    for _ in range(25):
        fake_clock.advance(lan_mod.ADVERTISE_RETRY_INTERVAL_S + 1)
        c._reannounce(["10.0.0.5"])

    assert fake_zeroconf.created_count == 25, "every backed-off retry should have actually attempted"
    assert len(fake_zeroconf.live) == 0, (
        "repeated failing (but rate-limited) retries leaked live Zeroconf instances"
    )


def test_advertise_warns_again_on_a_different_failure_message(fake_zeroconf, caplog):
    """Once the first failure has been logged and dampened, a DIFFERENT
    failure (e.g. the Zeroconf() constructor itself starts failing where
    only register_service was before) must still be logged, not silently
    swallowed by the same-message dampening."""
    c = _controller()
    fake_zeroconf.fail_register = True

    with caplog.at_level("WARNING", logger="fused_render.lan"):
        c._advertise(["10.0.0.5"])  # first failure mode: register_service

        original_init = fake_zeroconf.__init__

        def bad_init(self):
            raise RuntimeError("Zeroconf() constructor failed (simulated)")

        fake_zeroconf.__init__ = bad_init
        try:
            c._advertise(["10.0.0.5"])  # a different failure mode
        finally:
            fake_zeroconf.__init__ = original_init

    warnings = [r for r in caplog.records if "mDNS advertise failed" in r.message]
    assert len(warnings) == 2, (
        f"a different failure message must be logged, not dampened; got {len(warnings)} warnings"
    )
