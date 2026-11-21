"""The relay tunnel (fused_render/tunnel, docs/REMOTE_ACCESS.md): SNI parsing
of hostile bytes, self-certifying host ids, and a real relay ↔ agent ↔ phone
round trip over sockets in which the relay only ever carries ciphertext."""
import asyncio
import contextlib
import hashlib
import socket
import ssl
import threading
import time

import pytest

from fused_render.tunnel import agent as A
from fused_render.tunnel import protocol as P
from fused_render.tunnel import relay as R

DOMAIN = "relay.test"


# ---- protocol -------------------------------------------------------------------


def _client_hello(server_name: str | None) -> bytes:
    """A real ClientHello from the stdlib, captured through a memory BIO."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


def test_sni_is_read_from_a_real_client_hello():
    hello = _client_hello("abc.relay.test")
    assert P.parse_client_hello(hello) == "abc.relay.test"


def test_sni_needs_more_bytes_for_a_partial_hello_at_every_cut():
    hello = _client_hello("abc.relay.test")
    for cut in range(1, len(hello)):
        got = P.parse_client_hello(hello[:cut])
        assert got is P.NEED_MORE or got == "abc.relay.test", cut


def test_sni_reassembles_a_hello_split_across_records():
    hello = _client_hello("split.relay.test")
    body = hello[5:]
    half = len(body) // 2
    rec = lambda b: b"\x16\x03\x01" + len(b).to_bytes(2, "big") + b  # noqa: E731
    assert P.parse_client_hello(rec(body[:half]) + rec(body[half:])) == "split.relay.test"


@pytest.mark.parametrize("junk", [
    b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
    b"\x16\x03\x01\x00\x00",                 # empty record
    b"\x16\x03\x01\x40\x01" + b"\x00" * 10,  # record longer than TLS allows
    b"\x16\x03\x01\x00\x05\x02\x00\x00\x01\x00",  # a ServerHello
])
def test_sni_parser_refuses_garbage(junk):
    with pytest.raises(P.HelloError):
        P.parse_client_hello(junk)


def test_sni_parser_refuses_a_hello_without_sni():
    with pytest.raises(P.HelloError):
        P.parse_client_hello(_client_hello(None))


def test_sni_parser_survives_random_mutations():
    import random

    rng = random.Random(1234)
    hello = bytearray(_client_hello("fuzz.relay.test"))
    for _ in range(3000):
        mutated = bytearray(hello)
        for _ in range(rng.randint(1, 6)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        try:
            got = P.parse_client_hello(bytes(mutated[: rng.randint(1, len(mutated))]))
        except P.HelloError:
            continue
        assert got is P.NEED_MORE or isinstance(got, str)


def test_split_sni_admits_only_a_host_id_label_under_the_domain():
    hid = "a" * 26
    assert P.split_sni(f"{hid}.relay.test", DOMAIN) == hid
    assert P.split_sni(f"{hid}.relay.test.evil", DOMAIN) is None
    assert P.split_sni(f"x.{hid}.relay.test", DOMAIN) is None
    assert P.split_sni("relay.test", DOMAIN) is None
    assert P.split_sni(f"{'a' * 25}.relay.test", DOMAIN) is None


def test_host_id_is_a_stable_dns_label_of_the_public_key(tmp_path):
    key = A.load_or_create_key(str(tmp_path / "host.key"))
    again = A.load_or_create_key(str(tmp_path / "host.key"))
    hid = P.host_id(A.public_raw(key))
    assert hid == P.host_id(A.public_raw(again))
    assert P.HOST_ID_RE.match(hid)
    import os
    import stat

    assert stat.S_IMODE(os.stat(tmp_path / "host.key").st_mode) == 0o600


@pytest.mark.parametrize("setting,expected", [
    ("relay.example.com", ("relay.example.com", 8443, None)),
    ("Relay.Example.com:9000", ("relay.example.com", 9000, None)),
    ("relay.example.com:9000#" + "AB" * 32, ("relay.example.com", 9000, "ab" * 32)),
    ("tls://relay.example.com:1/", ("relay.example.com", 1, None)),
])
def test_parse_relay(setting, expected):
    assert A.parse_relay(setting) == expected


@pytest.mark.parametrize("bad", ["", "relay.example.com:0", "relay.example.com:x",
                                 "relay.example.com#abc", "rel ay.com", "a/b.com"])
def test_parse_relay_refuses(bad):
    with pytest.raises(ValueError):
        A.parse_relay(bad)


# ---- a real relay -----------------------------------------------------------------


class _Loop:
    """An asyncio loop on a thread, for servers a test talks to over sockets."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


async def _echo_tls_server(ctx):
    """The stand-in for the phone listener: TLS with the computer's own
    certificate, echoes one line back prefixed."""
    async def on(reader, writer):
        try:
            line = await reader.readline()
            writer.write(b"computer says: " + line)
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(on, "127.0.0.1", 0, ssl=ctx)


def _wait_for(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path):
    """A relay, a computer (TLS echo server + agent) and the certificates."""
    relay_cert, relay_key = R._self_signed(DOMAIN, str(tmp_path / "relay"))
    computer_cert, computer_key = R._self_signed("computer", str(tmp_path / "computer"))
    lp = _Loop()
    relay = R.Relay(DOMAIN, agent_ssl=R.server_context(relay_cert, relay_key), public_host="127.0.0.1",
                    public_port=0, agent_host="127.0.0.1", agent_port=0)
    lp.run(relay.start())
    echo = lp.run(_echo_tls_server(R.server_context(computer_cert, computer_key)))
    local_port = echo.sockets[0].getsockname()[1]
    key = A.load_or_create_key(str(tmp_path / "host.key"))
    agent = A.TunnelAgent("127.0.0.1", relay.agent_port, key, lambda: local_port,
                          relay_fingerprint=R.cert_fingerprint(relay_cert))
    world = type("World", (), {})()
    world.relay, world.agent, world.lp = relay, agent, lp
    world.computer_fp = R.cert_fingerprint(computer_cert)
    world.local_port = local_port
    world.tmp = tmp_path
    try:
        yield world
    finally:
        agent.stop()

        async def close_echo():
            echo.close()

        lp.run(close_echo())
        lp.run(relay.close())
        lp.close()


def _phone(port: int, sni: str, payload: bytes = b"hi\n") -> tuple[str, bytes]:
    """A phone: TLS to the relay's public port naming ``sni``, trusting only
    the pinned fingerprint (as TLSTrust.swift does). Returns (fingerprint, reply)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=sni) as tls:
            fp = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            tls.sendall(payload)
            data = b""
            while chunk := tls.recv(4096):
                data += chunk
    return fp, data


def test_phone_reaches_the_computer_end_to_end_through_the_relay(world):
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")
    st = world.agent.status()
    assert st["host"] == f"{world.agent.host_id}.{DOMAIN}"
    assert st["port"] == world.relay.public_port
    fp, reply = _phone(world.relay.public_port, st["host"])
    # The certificate the phone saw is the COMPUTER's, not the relay's: the
    # relay passed the TLS session through without terminating it.
    assert fp == world.computer_fp
    assert reply == b"computer says: hi\n"


def test_the_relay_only_carries_ciphertext(world, monkeypatch):
    seen = bytearray()
    real = R.splice

    async def spy(r1, w1, r2, w2):
        orig_read = r1.read

        async def read(n=-1):
            data = await orig_read(n)
            seen.extend(data)
            return data

        r1.read = read
        return await real(r1, w1, r2, w2)

    monkeypatch.setattr(R, "splice", spy)
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")
    secret = b"Cookie: fused_lan_device=TOPSECRET-" + b"x" * 20 + b"\n"
    _fp, reply = _phone(world.relay.public_port, world.agent.status()["host"], secret)
    assert reply.endswith(secret)
    assert seen and b"TOPSECRET" not in bytes(seen)


def test_the_computer_sees_the_phone_address_on_its_loopback_leg(world, monkeypatch):
    """lan.py rewrites a relayed request's client from this map: the loopback
    leg's source port → the phone address the relay reported."""
    captured = {}
    real_stream = world.agent._stream

    async def spy(sid, peer):
        captured["peer"] = peer
        # The running stream is held strongly (a weakly held task can be
        # collected mid-request: the phone saw an empty response).
        captured["held"] = asyncio.current_task() in world.agent._stream_tasks
        await real_stream(sid, peer)

    world.agent._stream = spy
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")
    _phone(world.relay.public_port, world.agent.status()["host"])
    assert captured["peer"] == "127.0.0.1"
    assert captured["held"]
    _wait_for(lambda: not world.agent._peers, what="stream cleaned up")


def test_an_unknown_host_id_is_dropped_without_a_handshake(world):
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        _phone(world.relay.public_port, f"{'b' * 26}.{DOMAIN}")


def test_a_host_id_cannot_be_claimed_without_its_key(world):
    """A second key gets ITS OWN id; nobody can be handed another's name."""
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")
    other = A.TunnelAgent("127.0.0.1", world.relay.agent_port,
                          A.load_or_create_key(str(world.tmp / "other.key")), lambda: world.local_port,
                          relay_fingerprint=world.agent.relay_fingerprint)
    try:
        other.start()
        _wait_for(lambda: other.connected, what="other agent connected")
        assert other.host_id != world.agent.host_id
        assert other.status()["host"].startswith(other.host_id + ".")
        assert world.agent.connected  # the first one was not displaced
    finally:
        other.stop()


def test_a_forged_signature_is_refused(world):
    async def forge():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", world.relay.agent_port, ssl=ctx)
        hello = P.decode(await reader.readline())
        assert hello["t"] == "hello"
        victim = A.public_raw(A.load_or_create_key(str(world.tmp / "victim.key")))
        writer.write(P.encode({"t": "auth", "pub": P.b64(victim), "sig": P.b64(b"\x00" * 64)}))
        await writer.drain()
        answer = P.decode(await reader.readline())
        writer.close()
        return answer

    assert world.lp.run(forge()) == {"t": "error", "error": "authentication failed"}
    assert world.relay.stats()["agents"] == 0


def test_a_replayed_signature_is_refused(world):
    """The nonce is fresh per connection: a signature over an old one fails."""
    key = A.load_or_create_key(str(world.tmp / "replay.key"))

    async def attempt(sig_for_nonce=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", world.relay.agent_port, ssl=ctx)
        hello = P.decode(await reader.readline())
        nonce = P.unb64(hello["nonce"])
        sig = key.sign(P.challenge(DOMAIN, sig_for_nonce or nonce))
        writer.write(P.encode({"t": "auth", "pub": P.b64(A.public_raw(key)), "sig": P.b64(sig)}))
        await writer.drain()
        answer = P.decode(await reader.readline())
        writer.close()
        return nonce, answer

    old_nonce, first = world.lp.run(attempt())
    assert first["t"] == "ok"
    _n, second = world.lp.run(attempt(sig_for_nonce=old_nonce))
    assert second == {"t": "error", "error": "authentication failed"}


def test_the_agent_refuses_a_relay_whose_certificate_is_not_the_pinned_one(world):
    world.agent.relay_fingerprint = "0" * 64
    world.agent._ssl = world.agent._client_context()
    world.agent.start()
    _wait_for(lambda: world.agent.error and "pinned" in world.agent.error, what="pin refusal")
    assert not world.agent.connected


def test_an_unclaimed_data_connection_is_closed(world):
    async def stray():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", world.relay.agent_port, ssl=ctx)
        await reader.readline()
        writer.write(P.encode({"t": "data", "sid": "f" * 32}))
        await writer.drain()
        got = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return got

    assert world.lp.run(stray()) == b""


def test_per_ip_connection_rate_is_limited():
    bucket = R._Bucket(per_min=60, burst=3)
    assert [bucket.take("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    assert bucket.take("5.6.7.8")


def test_the_agent_reconnects_after_the_relay_drops_it(world):
    world.agent.start()
    _wait_for(lambda: world.agent.connected, what="agent connected")

    async def kick():
        for a in list(world.relay._agents.values()):
            a.close()

    world.lp.run(kick())
    _wait_for(lambda: not world.agent.connected, what="agent noticed the drop")
    _wait_for(lambda: world.agent.connected, timeout=15, what="agent reconnected")
    _fp, reply = _phone(world.relay.public_port, world.agent.status()["host"])
    assert reply == b"computer says: hi\n"
