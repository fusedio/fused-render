"""The computer's side of the relay: one outbound connection, many streams.

``TunnelAgent`` runs its own asyncio loop on a daemon thread. It dials the
relay's agent port over TLS (verified against the system trust store, or
pinned with ``#<sha256>`` for a self-hosted relay with a self-signed
certificate), proves it owns its host id by signing the relay's nonce, and
waits. For each ``open`` the relay sends it dials TWO connections — a fresh
one to the relay (``data`` + the stream id) and one to the phone listener on
127.0.0.1 — and copies bytes between them. It never looks inside: the bytes
are the phone's TLS session with the listener.

The listener must be able to tell a relayed connection from the computer
itself dialing loopback: ``peer_for(port)`` maps the local source port of
each loopback leg to the phone's address the relay reported, and lan.py
rewrites such a request's client to that address (``via == "relay"``). A
relayed request is NEVER treated as local.

A malicious relay can open streams but each one is only a TCP connection to
the phone listener — what anyone on the Wi-Fi gets — so ``max_streams``
bounds the load and nothing else needs trusting.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import ssl
import threading
import time
from typing import Callable

from fused_render.tunnel import protocol as P
from fused_render.tunnel.relay import splice

logger = logging.getLogger("fused_render.tunnel.agent")


class TunnelError(Exception):
    """The relay refused us, or spoke something we do not understand."""


def parse_relay(setting: str) -> tuple[str, int, str | None]:
    """``host[:port][#sha256]`` → (host, port, pinned fingerprint or None).
    Raises ValueError on anything else."""
    setting = (setting or "").strip()
    if setting.startswith(("tls://", "relay://")):
        setting = setting.split("://", 1)[1]
    setting = setting.rstrip("/")
    pin = None
    if "#" in setting:
        setting, pin = setting.split("#", 1)
        pin = pin.strip().lower().replace(":", "")
        if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise ValueError("the #fingerprint must be 64 hex characters (SHA-256)")
    host, port = setting, 8443
    if setting.count(":") == 1:
        host, raw = setting.split(":")
        if not raw.isdigit() or not 0 < int(raw) < 65536:
            raise ValueError("bad relay port")
        port = int(raw)
    host = host.strip().lower().rstrip(".")
    if not host or not all(c.isalnum() or c in ".-" for c in host):
        raise ValueError("the relay must be a host name, like relay.example.com:8443")
    return host, port, pin


# ---- the host key ---------------------------------------------------------------


def load_or_create_key(path: str):
    """The computer's Ed25519 identity on relays (``<state>/tunnel/host.key``,
    0600). Its hash is the host id; losing it means a new id and phones
    re-learning the relay route (they keep working on the Wi-Fi)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            if isinstance(key, Ed25519PrivateKey):
                return key
            logger.warning("tunnel: %s is not an Ed25519 key - replacing it", path)
        except Exception:  # noqa: BLE001
            logger.warning("tunnel: host key unreadable - replacing it", exc_info=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    tmp = path + ".tmp"
    # A leftover temp file keeps its old mode through O_CREAT: start clean.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    os.replace(tmp, path)
    return key


def public_raw(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ---- the agent -------------------------------------------------------------------


def clean_peer(value) -> str:
    """The phone address the relay reported, as the computer will log and
    present it — only ever a real, non-local IP address. Anything else (a
    malicious relay naming 127.0.0.1, a log-injection string, a hostname)
    becomes the placeholder "relay": a relayed request must never look local."""
    import ipaddress

    if isinstance(value, str) and len(value) <= 64:
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return "relay"
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        if not (addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast):
            return str(addr)
    return "relay"


def domain_matches(relay_host: str, domain: str) -> bool:
    """Does the domain a relay names in its hello belong to the relay we
    dialled? We sign over that domain, so a relay must not be able to name
    another relay's: that would let it replay our signature there and claim
    our name on it."""
    relay_host, domain = relay_host.lower().rstrip("."), domain.lower().rstrip(".")
    return bool(domain) and (relay_host == domain or relay_host.endswith("." + domain))


class TunnelAgent:
    def __init__(self, relay_host: str, relay_port: int, key, local_port: Callable[[], int | None], *,
                 relay_fingerprint: str | None = None, max_streams: int = 256, max_streams_per_peer: int = 32,
                 stream_idle_s: float = 300.0, ssl_context: ssl.SSLContext | None = None):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.relay_fingerprint = relay_fingerprint
        self._key = key
        self.host_id = P.host_id(public_raw(key))
        self._local_port = local_port
        self._max_streams = max_streams
        self._max_per_peer = max_streams_per_peer
        self._stream_idle_s = stream_idle_s
        self._per_peer: dict[str, int] = {}
        self._ssl = ssl_context or self._client_context()
        self._peers: dict[int, str] = {}
        self._peers_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._streams = 0
        # Strong references to the stream tasks: the loop holds only weak
        # ones, and a stream task collected mid-request drops the phone's
        # connection with no error anywhere but "Task was destroyed".
        self._stream_tasks: set[asyncio.Task] = set()
        # status, read from other threads
        self.connected = False
        self.public_host: str | None = None
        self.public_port: int | None = None
        self.error: str | None = None
        self.connected_since: float | None = None

    # -- status
    def status(self) -> dict:
        return {"connected": self.connected, "host": self.public_host if self.connected else None,
                "port": self.public_port if self.connected else None, "error": self.error,
                "host_id": self.host_id, "relay": f"{self.relay_host}:{self.relay_port}",
                "streams": self._streams}

    def peer_for(self, local_port: int) -> str | None:
        """The phone address behind a loopback connection from ``local_port``,
        when that connection is one of ours; else None."""
        with self._peers_lock:
            return self._peers.get(local_port)

    # -- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._task = loop.create_task(self._run())
            ready.set()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(self._task)
            # Let the streams' cancellations finish before the loop goes.
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name="fused-tunnel")
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        loop, task, thread = self._loop, self._task, self._thread
        if loop is not None and task is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        if thread is not None:
            thread.join(timeout=5)
        self._thread = self._loop = self._task = None
        self.connected = False

    # -- the control connection
    def _client_context(self) -> ssl.SSLContext:
        if self.relay_fingerprint:
            # Pinned: the certificate's hash (checked in `_dial_relay`) is the
            # trust, nothing else. The shared helper, not a hand-built
            # context: once truststore is injected the stdlib setters recurse.
            from fused_render._system_trust import pinned_client_ssl_context

            return pinned_client_ssl_context()
        return ssl.create_default_context()  # system trust (truststore when injected)

    async def _dial_relay(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.relay_host, self.relay_port, ssl=self._ssl,
                                    server_hostname=self.relay_host, limit=P.MAX_LINE), 15)
        if self.relay_fingerprint:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if not der or hashlib.sha256(der).hexdigest() != self.relay_fingerprint:
                writer.close()
                raise TunnelError("the relay's certificate does not match the pinned fingerprint")
        hello = P.decode(await asyncio.wait_for(reader.readline(), 15))
        if not hello or hello.get("t") != "hello" or hello.get("v") != P.VERSION:
            writer.close()
            raise TunnelError("the relay does not speak this protocol version")
        return reader, writer, hello

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except TunnelError as e:
                self.error = str(e)
            except (OSError, ConnectionError, asyncio.TimeoutError, ssl.SSLError) as e:
                self.error = f"relay unreachable: {e.__class__.__name__}: {e}".rstrip(": ")
            except Exception as e:  # noqa: BLE001
                logger.exception("tunnel: session failed")
                self.error = f"tunnel: {e}"
            finally:
                self.connected = False
            if time.monotonic() - started > 60:
                backoff = 1.0  # it was up for a while: retry promptly
            logger.info("tunnel: disconnected (%s); retrying in %.0fs", self.error, backoff)
            await asyncio.sleep(backoff * (0.75 + random.random() / 2))
            backoff = min(backoff * 2, 60.0)

    async def _session(self) -> None:
        reader, writer, hello = await self._dial_relay()
        try:
            domain, nonce = hello.get("domain"), P.unb64(hello.get("nonce"))
            if not isinstance(domain, str) or not nonce or len(nonce) < 16:
                raise TunnelError("malformed relay hello")
            # Sign only for the relay we dialled. A pinned relay is identified
            # by its certificate (often dialled by address), so the pin stands
            # in for the name there.
            if not self.relay_fingerprint and not domain_matches(self.relay_host, domain):
                raise TunnelError(f"the relay at {self.relay_host} claims to be {domain!r}")
            sig = self._key.sign(P.challenge(domain, nonce))
            writer.write(P.encode({"t": "auth", "pub": P.b64(public_raw(self._key)), "sig": P.b64(sig)}))
            await writer.drain()
            ok = P.decode(await asyncio.wait_for(reader.readline(), 15))
            if not ok or ok["t"] == "error":
                raise TunnelError(f"relay refused: {(ok or {}).get('error') or 'no answer'}")
            host = ok.get("host")
            if (ok["t"] != "ok" or not isinstance(host, str) or host != f"{self.host_id}.{domain}"
                    or not isinstance(ok.get("port"), int)):
                raise TunnelError("the relay answered with someone else's name")
            self.public_host, self.public_port = host, ok["port"]
            self.connected, self.error, self.connected_since = True, None, time.time()
            logger.info("tunnel: reachable at https://%s:%s/", host, ok["port"])
            lock = asyncio.Lock()
            while True:
                line = await asyncio.wait_for(reader.readline(), 120)
                if not line:
                    raise ConnectionError("relay closed the connection")
                msg = P.decode(line)
                if msg is None:
                    raise TunnelError("garbled message from the relay")
                if msg["t"] == "ping":
                    async with lock:
                        writer.write(P.encode({"t": "pong"}))
                        await writer.drain()
                elif msg["t"] == "open" and isinstance(msg.get("sid"), str):
                    sid, peer = msg["sid"][:64], clean_peer(msg.get("peer"))
                    if (self._streams >= self._max_streams
                            or self._per_peer.get(peer, 0) >= self._max_per_peer):
                        # Full (one address holding many silent connections
                        # cannot take every slot): tell the relay at once.
                        logger.info("tunnel: refused a stream from %s (%d open, %d from it)",
                                    peer, self._streams, self._per_peer.get(peer, 0))
                        async with lock:
                            writer.write(P.encode({"t": "refuse", "sid": sid}))
                            await writer.drain()
                        continue
                    self._streams += 1
                    self._per_peer[peer] = self._per_peer.get(peer, 0) + 1
                    task = asyncio.get_running_loop().create_task(self._stream(sid, peer))
                    self._stream_tasks.add(task)
                    task.add_done_callback(self._stream_tasks.discard)
                elif msg["t"] == "error":
                    raise TunnelError(f"relay: {msg.get('error')}")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _stream(self, sid: str, peer: str) -> None:
        """One phone connection. The slot (``_streams``, ``_per_peer``) was
        taken by the caller and is given back here, whatever happens."""
        from fused_render.tunnel.relay import close_soon

        lw = dw = None
        src = None
        try:
            port = self._local_port()
            if port is None:
                return
            lr, lw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
            src = lw.get_extra_info("sockname")[1]
            with self._peers_lock:
                self._peers[src] = peer
            dr, dw, _hello = await self._dial_relay()
            dw.write(P.encode({"t": "data", "sid": sid}))
            await dw.drain()
            await splice(lr, lw, dr, dw, self._stream_idle_s)
        except (OSError, ConnectionError, asyncio.TimeoutError, ssl.SSLError, TunnelError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("tunnel: stream failed")
        finally:
            # The slot goes back as soon as no bytes can flow (closing below
            # is bounded on its own): a phone making short requests back to
            # back must not be refused by its own connections still closing.
            self._streams -= 1
            left = self._per_peer.get(peer, 1) - 1
            if left > 0:
                self._per_peer[peer] = left
            else:
                self._per_peer.pop(peer, None)
            # Closed BEFORE the source port leaves the map: while the
            # listener can still read from this socket, its requests must
            # still classify as relayed.
            for w in (lw, dw):
                if w is not None:
                    await close_soon(w)
            if src is not None:
                with self._peers_lock:
                    self._peers.pop(src, None)
