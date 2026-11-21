"""The tunnel wire format, shared by the relay and the computer's agent.

STDLIB ONLY (the relay is meant to run on a bare VPS; the one non-stdlib
need — Ed25519 — is imported lazily where it is used).

Two kinds of connection reach a relay:

**Phone connections** arrive on the relay's PUBLIC port and are raw TLS. The
relay reads the first TLS record(s) — the ClientHello — far enough to learn
the SNI (``parse_client_hello``), never answers TLS itself, and splices the
bytes (ClientHello included) into a data connection from the computer. No
plaintext is ever available to it.

**Agent connections** arrive on the relay's AGENT port, are TLS terminated by
the relay with its own certificate, and carry newline-delimited JSON objects
(``encode``/``decode``) until a data connection switches to raw bytes:

    relay → agent  {"t": "hello", "v": 1, "nonce": <b64>, "domain": <relay domain>}
    agent → relay  {"t": "auth", "pub": <b64 raw Ed25519 key>, "sig": <b64>}
                   (control connection: sig over ``challenge(domain, nonce)``)
    relay → agent  {"t": "ok", "host": "<id>.<domain>", "port": 443}
    relay → agent  {"t": "open", "sid": <hex>, "peer": <phone ip>}
    agent → relay  {"t": "data", "sid": <hex>}   (on a NEW agent connection;
                   raw bytes in both directions after this line)
    either way     {"t": "ping"} / {"t": "pong"}
    relay → agent  {"t": "error", "error": <text>}  then close

The host id is ``host_id(pub)`` — a hash of the computer's public key — so a
relay hands a name only to whoever can sign with the matching private key:
names cannot be squatted, and there are no accounts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

VERSION = 1

#: What the agent signs, before the relay's domain and the nonce. Binding the
#: domain means a signature captured by one relay is useless at another.
CHALLENGE_CONTEXT = b"fused-render-tunnel-v1\x00"

#: A host id: 26 lowercase base32 characters (128 bits of SHA-256) — one DNS
#: label, well under the 63-character limit.
HOST_ID_RE = re.compile(r"^[a-z2-7]{26}$")

#: Upper bound for one JSON line on an agent connection. Every legitimate
#: message is well under 1 KiB.
MAX_LINE = 4096

#: Upper bound for the bytes a relay buffers while looking for the SNI. A
#: ClientHello is one record in practice (≈ 0.5–2 KiB, post-quantum key
#: shares included); TLS caps a record at 16 KiB + header.
MAX_HELLO = 16384 + 5

#: Upper bound on the TLS records a ClientHello may be split across. Real
#: clients use one; a stranger sending many tiny records is refused.
MAX_RECORDS = 8


def host_id(public_key_raw: bytes) -> str:
    """The relay name for a raw 32-byte Ed25519 public key."""
    digest = hashlib.sha256(b"fused-render-host-id\x00" + public_key_raw).digest()[:16]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def challenge(domain: str, nonce: bytes) -> bytes:
    """The bytes the agent signs to claim its host id on ``domain``."""
    return CHALLENGE_CONTEXT + domain.lower().encode("ascii") + b"\x00" + nonce


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text) -> bytes | None:
    if not isinstance(text, str) or len(text) > 256:
        return None
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict | None:
    """One JSON object line, or None for anything else (garbage never raises)."""
    if not line or len(line) > MAX_LINE:
        return None
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("t"), str) else None


# ---- SNI ---------------------------------------------------------------------

#: ``parse_client_hello`` needs more bytes before it can answer.
NEED_MORE = object()


class HelloError(ValueError):
    """Not a TLS ClientHello, or one without a usable server name."""


def parse_client_hello(buf: bytes):
    """The SNI host name in the TLS ClientHello at the start of ``buf``.

    Returns the name (lowercase, no trailing dot), ``NEED_MORE`` when ``buf``
    ends before the extension does, or raises ``HelloError``. Handles a
    ClientHello fragmented across several handshake records. Never trusts a
    length without bounds-checking it: this parses bytes from strangers.
    """
    # Reassemble the handshake message from consecutive handshake records.
    hs = bytearray()
    pos = 0
    records = 0
    while True:
        records += 1
        if records > MAX_RECORDS:
            raise HelloError("ClientHello split across too many records")
        if len(buf) < pos + 5:
            if pos == 0 and buf and buf[0] != 0x16:
                raise HelloError("not a TLS handshake")
            return NEED_MORE
        ctype, major, length = buf[pos], buf[pos + 1], int.from_bytes(buf[pos + 3:pos + 5], "big")
        if ctype != 0x16 or major != 3 or length == 0 or length > 16384:
            raise HelloError("not a TLS handshake record")
        if len(buf) < pos + 5 + length:
            hs += buf[pos + 5:]
            if len(hs) >= 4:
                _check_hs_header(hs)
            return NEED_MORE
        hs += buf[pos + 5:pos + 5 + length]
        pos += 5 + length
        if len(hs) >= 4:
            want = _check_hs_header(hs)
            if len(hs) >= 4 + want:
                break
        if pos >= MAX_HELLO:
            raise HelloError("ClientHello too large")
    return _sni_from_hello(bytes(hs[4:4 + _check_hs_header(hs)]))


def _check_hs_header(hs: bytes) -> int:
    if hs[0] != 0x01:
        raise HelloError("not a ClientHello")
    length = int.from_bytes(hs[1:4], "big")
    if length > MAX_HELLO:
        raise HelloError("ClientHello too large")
    return length


def _sni_from_hello(body: bytes) -> str:
    p = 2 + 32  # client_version, random
    def take(n: int) -> bytes:
        nonlocal p
        if p + n > len(body):
            raise HelloError("truncated ClientHello")
        out = body[p:p + n]
        p += n
        return out

    take(take(1)[0])                                   # session_id
    take(int.from_bytes(take(2), "big"))               # cipher_suites
    take(take(1)[0])                                   # compression_methods
    if p == len(body):
        raise HelloError("no extensions (no SNI)")
    ext_len = int.from_bytes(take(2), "big")
    ext_end = p + ext_len
    if ext_end > len(body):
        raise HelloError("truncated extensions")
    while p + 4 <= ext_end:
        etype = int.from_bytes(take(2), "big")
        edata = take(int.from_bytes(take(2), "big"))
        if etype != 0:  # server_name
            continue
        if len(edata) < 2:
            raise HelloError("bad server_name extension")
        lst_len = int.from_bytes(edata[:2], "big")
        q, end = 2, min(2 + lst_len, len(edata))
        while q + 3 <= end:
            ntype = edata[q]
            nlen = int.from_bytes(edata[q + 1:q + 3], "big")
            name = edata[q + 3:q + 3 + nlen]
            q += 3 + nlen
            if ntype == 0 and name:
                try:
                    host = name.decode("ascii").lower().rstrip(".")
                except UnicodeDecodeError as e:
                    raise HelloError("non-ascii server name") from e
                if not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
                    raise HelloError("bad server name")
                return host
        raise HelloError("server_name without a host name")
    raise HelloError("no SNI")


def split_sni(sni: str, domain: str) -> str | None:
    """The host id when ``sni`` is ``<id>.<domain>``, else None."""
    domain = domain.lower().rstrip(".")
    suffix = "." + domain
    if not sni.endswith(suffix):
        return None
    label = sni[: -len(suffix)]
    return label if HOST_ID_RE.match(label) else None
