"""The relay: a blind TLS pass-through between phones and computers.

Run it on any machine with a public address and a wildcard DNS record
(``*.relay.example.com`` and ``relay.example.com`` → this machine)::

    python -m fused_render.tunnel.relay --domain relay.example.com \\
        --cert fullchain.pem --key privkey.pem            # agent port's cert

It listens twice:

- the PUBLIC port (``--public-port``, default 443) takes phone connections.
  It reads the TLS ClientHello far enough to learn the SNI
  (``<host-id>.<domain>``), asks that computer's agent for a data
  connection, and splices the raw bytes. It never terminates the phone's
  TLS and holds no key that could: the session is between the phone and the
  computer, pinned by the fingerprint in the pairing QR.
- the AGENT port (``--agent-port``, default 8443) takes computers. TLS with
  the relay's own certificate (a public one, or a self-signed one the
  computer pins with ``#<sha256>`` in its relay setting). A computer proves
  it owns ``<host-id>`` by signing a fresh nonce with the Ed25519 key the id
  is a hash of (protocol.py).

What the relay can see: host ids, phone and computer IP addresses, the
timing and size of traffic. What it cannot: a byte of content, a pairing
cookie, a pairing token. What it can do at worst: drop or delay traffic, or
open connections to a computer's phone listener — exactly what any stranger
on the computer's Wi-Fi can already do, and the listener answers those with
401 until the request carries a paired device's cookie.

Abuse limits (all per relay process, in memory): new phone connections per
IP per minute, concurrent phone connections per IP and in total, concurrent
streams per computer, agent handshakes per IP per minute, a deadline on
every pre-splice step, and a bound on every buffer.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import hashlib
import logging
import os
import secrets
import ssl
import time
from dataclasses import dataclass, field

from fused_render.tunnel import protocol as P

logger = logging.getLogger("fused_render.tunnel.relay")


@dataclass
class Limits:
    hello_timeout_s: float = 10.0       # phone: ClientHello must arrive within
    open_timeout_s: float = 10.0        # agent: data connection must arrive within
    auth_timeout_s: float = 10.0        # agent: first line must arrive within
    ping_interval_s: float = 30.0       # relay → agent keepalive
    idle_timeout_s: float = 95.0        # agent control silent this long → dropped
    conn_per_ip_per_min: int = 240      # new phone connections, token bucket
    conn_burst_per_ip: int = 120
    concurrent_per_ip: int = 256
    concurrent_total: int = 8192
    streams_per_host: int = 128
    agent_auth_per_ip_per_min: int = 30


class _Bucket:
    """A token bucket per key (an IP), pruned as it is read."""

    def __init__(self, per_min: int, burst: int):
        self.rate = per_min / 60.0
        self.burst = float(burst)
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if len(self._state) > 100_000:  # never an unbounded map of strangers
            self._state.clear()
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True


@dataclass
class _Agent:
    host_id: str
    writer: asyncio.StreamWriter
    peer: str
    streams: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, msg: dict) -> None:
        async with self.lock:
            self.writer.write(P.encode(msg))
            await self.writer.drain()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


def _peer_ip(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return str(peer[0]) if isinstance(peer, tuple) and peer else "?"


async def splice(r1: asyncio.StreamReader, w1: asyncio.StreamWriter,
                 r2: asyncio.StreamReader, w2: asyncio.StreamWriter) -> int:
    """Copy bytes both ways until either side ends; returns bytes moved."""
    moved = 0

    async def pipe(r, w):
        nonlocal moved
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                moved += len(data)
                w.write(data)
                await w.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            # One side is done: end the other. A TLS transport cannot
            # half-close, and the in-band close_notify already travelled.
            with contextlib.suppress(Exception):
                if w.can_write_eof():
                    w.write_eof()
                else:
                    w.close()

    await asyncio.gather(pipe(r1, w2), pipe(r2, w1))
    return moved


class Relay:
    def __init__(self, domain: str, *, agent_ssl: ssl.SSLContext, public_host: str = "0.0.0.0",
                 public_port: int = 443, agent_host: str = "0.0.0.0", agent_port: int = 8443,
                 advertise_port: int | None = None, limits: Limits | None = None):
        domain = domain.lower().rstrip(".")
        if not domain or "." not in domain:
            raise ValueError("--domain must be a DNS name like relay.example.com")
        self.domain = domain
        self.limits = limits or Limits()
        self._agent_ssl = agent_ssl
        self._binds = ((public_host, public_port), (agent_host, agent_port))
        self._advertise_port = advertise_port
        self._agents: dict[str, _Agent] = {}
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._conn_bucket = _Bucket(self.limits.conn_per_ip_per_min, self.limits.conn_burst_per_ip)
        self._auth_bucket = _Bucket(self.limits.agent_auth_per_ip_per_min, self.limits.agent_auth_per_ip_per_min)
        self._per_ip: collections.Counter = collections.Counter()
        self._total = 0
        self._servers: list[asyncio.base_events.Server] = []

    # -- lifecycle
    async def start(self) -> None:
        (ph, pp), (ah, ap) = self._binds
        self._servers.append(await asyncio.start_server(self._on_public, ph, pp, limit=P.MAX_LINE))
        self._servers.append(await asyncio.start_server(
            self._on_agent, ah, ap, ssl=self._agent_ssl, limit=P.MAX_LINE,
            ssl_handshake_timeout=self.limits.auth_timeout_s))

    @property
    def public_port(self) -> int:
        return self._servers[0].sockets[0].getsockname()[1]

    @property
    def agent_port(self) -> int:
        return self._servers[1].sockets[0].getsockname()[1]

    async def close(self) -> None:
        for agent in list(self._agents.values()):
            agent.close()
        for server in self._servers:
            server.close()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    def stats(self) -> dict:
        return {"agents": len(self._agents), "connections": self._total, "pending": len(self._pending)}

    # -- phones
    async def _on_public(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = _peer_ip(writer)
        lim = self.limits
        if (self._total >= lim.concurrent_total or self._per_ip[ip] >= lim.concurrent_per_ip
                or not self._conn_bucket.take(ip)):
            writer.close()
            return
        self._total += 1
        self._per_ip[ip] += 1
        agent: _Agent | None = None
        try:
            hello, sni = await asyncio.wait_for(self._read_hello(reader), lim.hello_timeout_s)
            hid = P.split_sni(sni, self.domain) if sni else None
            agent = self._agents.get(hid) if hid else None
            if agent is None or agent.streams >= lim.streams_per_host:
                return
            sid = secrets.token_hex(16)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[sid] = (agent.host_id, fut)
            try:
                await agent.send({"t": "open", "sid": sid, "peer": ip})
                dreader, dwriter, done = await asyncio.wait_for(fut, lim.open_timeout_s)
            finally:
                self._pending.pop(sid, None)
            agent.streams += 1
            try:
                dwriter.write(hello)
                await dwriter.drain()
                await splice(reader, writer, dreader, dwriter)
            finally:
                agent.streams -= 1
                done.set()
        except (asyncio.TimeoutError, P.HelloError, ConnectionError, OSError, ssl.SSLError):
            pass
        except Exception:  # noqa: BLE001 — one bad stream never takes the relay down
            logger.exception("relay: public stream failed")
        finally:
            self._total -= 1
            self._per_ip[ip] -= 1
            if self._per_ip[ip] <= 0:
                del self._per_ip[ip]
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_hello(self, reader: asyncio.StreamReader) -> tuple[bytes, str | None]:
        buf = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                raise ConnectionError("closed before ClientHello")
            buf += chunk
            got = P.parse_client_hello(buf)
            if got is not P.NEED_MORE:
                return buf, got
            if len(buf) > P.MAX_HELLO:
                raise P.HelloError("ClientHello too large")

    # -- computers
    async def _on_agent(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = _peer_ip(writer)
        lim = self.limits
        nonce = os.urandom(32)
        try:
            writer.write(P.encode({"t": "hello", "v": P.VERSION, "nonce": P.b64(nonce), "domain": self.domain}))
            await writer.drain()
            msg = P.decode(await asyncio.wait_for(reader.readline(), lim.auth_timeout_s))
            if msg is None:
                return
            if msg["t"] == "data":
                await self._on_data(msg, reader, writer)
                return
            if msg["t"] != "auth" or not self._auth_bucket.take(ip):
                await self._refuse(writer, "unexpected message" if msg["t"] != "auth" else "slow down")
                return
            hid = self._verify(msg, nonce)
            if hid is None:
                logger.info("relay: agent auth failed from %s", ip)
                await self._refuse(writer, "authentication failed")
                return
            await self._serve_agent(hid, ip, reader, writer)
        except (asyncio.TimeoutError, ConnectionError, OSError, ssl.SSLError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("relay: agent connection failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _refuse(self, writer, error: str) -> None:
        with contextlib.suppress(Exception):
            writer.write(P.encode({"t": "error", "error": error}))
            await writer.drain()

    def _verify(self, msg: dict, nonce: bytes) -> str | None:
        pub, sig = P.unb64(msg.get("pub")), P.unb64(msg.get("sig"))
        if pub is None or sig is None or len(pub) != 32 or len(sig) != 64:
            return None
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, P.challenge(self.domain, nonce))
        except (InvalidSignature, ValueError):
            return None
        return P.host_id(pub)

    async def _serve_agent(self, hid: str, ip: str, reader, writer) -> None:
        agent = _Agent(hid, writer, ip)
        old = self._agents.get(hid)
        self._agents[hid] = agent
        if old is not None:  # the same key reconnected (sleep/wake, new network)
            old.close()
        logger.info("relay: agent %s online from %s", hid, ip)
        port = self._advertise_port or self.public_port
        await agent.send({"t": "ok", "host": f"{hid}.{self.domain}", "port": port})
        pinger = asyncio.create_task(self._ping(agent))
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), self.limits.idle_timeout_s)
                if not line:
                    return
                msg = P.decode(line)
                if msg is None:
                    return
                if msg["t"] == "ping":
                    await agent.send({"t": "pong"})
        finally:
            pinger.cancel()
            if self._agents.get(hid) is agent:
                del self._agents[hid]
                logger.info("relay: agent %s offline", hid)

    async def _ping(self, agent: _Agent) -> None:
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(self.limits.ping_interval_s)
                await agent.send({"t": "ping"})

    async def _on_data(self, msg: dict, reader, writer) -> None:
        sid = msg.get("sid")
        entry = self._pending.get(sid) if isinstance(sid, str) else None
        if entry is None or entry[1].done():
            return
        done = asyncio.Event()
        entry[1].set_result((reader, writer, done))
        await done.wait()


# ---- command line -------------------------------------------------------------


def _self_signed(domain: str, folder: str) -> tuple[str, str]:
    """A self-signed certificate for the agent port, for a relay without a
    public one. Computers pin it with ``#<sha256>`` in their relay setting."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(folder, exist_ok=True)
    cert_path, key_path = os.path.join(folder, "agent.pem"), os.path.join(folder, "agent.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
            .sign(key, hashes.SHA256()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


def cert_fingerprint(cert_path: str) -> str:
    with open(cert_path, "rb") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read().decode("ascii"))
    return hashlib.sha256(der).hexdigest()


def server_context(cert: str, key: str) -> ssl.SSLContext:
    try:
        from fused_render._system_trust import ORIGINAL_SSL_CONTEXT as ctx_cls
    except Exception:  # noqa: BLE001 — standalone relay: the stdlib class
        ctx_cls = ssl.SSLContext
    ctx = ctx_cls(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fused_render.tunnel.relay",
                                 description="Blind TLS relay for Fused Render phone access.")
    ap.add_argument("--domain", required=True, help="relay.example.com (phones reach <id>.relay.example.com)")
    ap.add_argument("--public-host", default="0.0.0.0")
    ap.add_argument("--public-port", type=int, default=443)
    ap.add_argument("--advertise-port", type=int, default=None,
                    help="the port phones dial, when it differs from --public-port (a NAT, a load balancer)")
    ap.add_argument("--agent-host", default="0.0.0.0")
    ap.add_argument("--agent-port", type=int, default=8443)
    ap.add_argument("--cert", help="PEM certificate for the agent port (default: a self-signed one)")
    ap.add_argument("--key", help="PEM key for --cert")
    ap.add_argument("--conn-per-ip-per-min", type=int, default=Limits.conn_per_ip_per_min,
                    help="new phone connections per source IP per minute (raise behind carrier NAT)")
    ap.add_argument("--streams-per-host", type=int, default=Limits.streams_per_host)
    ap.add_argument("--state", default=os.path.expanduser("~/.fused-relay"),
                    help="where the self-signed certificate is kept")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if bool(args.cert) != bool(args.key):
        ap.error("--cert and --key go together")
    cert, key = (args.cert, args.key) if args.cert else _self_signed(args.domain, args.state)
    relay = Relay(args.domain, agent_ssl=server_context(cert, key), public_host=args.public_host,
                  public_port=args.public_port, agent_host=args.agent_host, agent_port=args.agent_port,
                  advertise_port=args.advertise_port,
                  limits=Limits(conn_per_ip_per_min=args.conn_per_ip_per_min,
                                conn_burst_per_ip=max(1, args.conn_per_ip_per_min // 2),
                                streams_per_host=args.streams_per_host))

    async def run():
        await relay.start()
        setting = f"{args.domain}:{relay.agent_port}"
        if not args.cert:
            setting += "#" + cert_fingerprint(cert)
        logger.info("relay up: phones → *.%s:%s, computers → %s:%s",
                    args.domain, args.advertise_port or relay.public_port, args.domain, relay.agent_port)
        logger.info("relay setting for Fused Render (Preferences → Phone access → Relay): %s", setting)
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
