"""Tailscale, when the computer is on a tailnet: a second way in, no relay.

The phone listener (lan.py) binds every interface, so a phone on the same
tailnet can already open a TCP connection to the computer's tailnet address.
What it could not do before is get past ``_host_ok``: the tailnet name and
address are not LAN names. With away access on, lan.py admits them, the
pairing code names the MagicDNS name (``ts=``), and the phone tries it after
the Wi-Fi routes. Encryption is WireGuard underneath and the same pinned TLS
on top.

Read from ``tailscale status --json`` (cached; a missing CLI or a stopped
daemon is simply "no tailnet"). ``state()`` says which of those it was, so
Preferences can name the next step: install it, sign in, or nothing.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import threading
import time

_TTL_S = 20.0
#: (when, identity or None, state, every address on the tailnet): one CLI run
#: answers all three questions.
_cache: tuple[float, dict | None, str, frozenset[str]] | None = None
#: A background refresh is already running (``_cached(block=False)``).
_refreshing = False

#: ``state()`` values. MISSING: no CLI on this computer. OFF: installed, but
#: the backend is stopped, signed out or still starting. RUNNING: on a tailnet.
MISSING, OFF, RUNNING = "missing", "off", "running"
_lock = threading.Lock()

#: Where the CLI lives when it is not on PATH (the macOS App Store / standalone app).
_MAC_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

#: Tailscale's address ranges: CGNAT v4 and its ULA v6 prefix.
_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return _MAC_CLI if os.path.exists(_MAC_CLI) else None


_NOBODY: frozenset[str] = frozenset()


def _read() -> tuple[dict | None, str, frozenset[str]]:
    cli = _cli()
    if cli is None:
        return None, MISSING, _NOBODY
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True,
                             timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, OFF, _NOBODY
    if out.returncode != 0:
        return None, OFF, _NOBODY
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None, OFF, _NOBODY
    value = parse(data)
    return value, RUNNING if value else OFF, parse_peers(data) if value else _NOBODY


def parse_peers(data) -> frozenset[str]:
    """Every address on this tailnet, the computer's own included: what tells
    a tailnet source apart from a Wi-Fi one when the local network is numbered
    out of the same range (lan.py ``_via``)."""
    if not isinstance(data, dict):
        return _NOBODY
    nodes = [data.get("Self")]
    peers = data.get("Peer")
    if isinstance(peers, dict):
        nodes.extend(peers.values())
    out = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for ip in node.get("TailscaleIPs") or []:
            if isinstance(ip, str) and is_tailnet_ip(ip):
                out.add(str(ipaddress.ip_address(ip)))
    return frozenset(out)


def parse(data) -> dict | None:
    """``tailscale status --json`` → {"host": MagicDNS name or None, "ips": [...]}
    when the backend is running, else None."""
    if not isinstance(data, dict) or data.get("BackendState") != "Running":
        return None
    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    name = me.get("DNSName")
    host = name.rstrip(".").lower() if isinstance(name, str) and name.strip(".") else None
    ips = [ip for ip in (me.get("TailscaleIPs") or data.get("TailscaleIPs") or [])
           if isinstance(ip, str) and is_tailnet_ip(ip)]
    if not host and not ips:
        return None
    return {"host": host, "ips": ips}


def _refresh() -> tuple[dict | None, str, frozenset[str]]:
    global _cache, _refreshing
    try:
        got = _read()
        with _lock:
            _cache = (time.monotonic(), *got)
        return got
    finally:
        with _lock:
            _refreshing = False


def _cached(block: bool = True) -> tuple[dict | None, str, frozenset[str]]:
    """The last read, made again when it is older than ``_TTL_S``.

    ``block=False`` is for a caller that must not wait — the phone listener's
    event loop, where the CLI's up-to-three-second run would stall every
    phone's request: it gets the last read, however old, and a refresh starts
    on a thread. With nothing read yet it gets "no tailnet"."""
    global _refreshing
    now = time.monotonic()
    with _lock:
        hit = _cache
        if hit is not None and now - hit[0] < _TTL_S:
            return hit[1], hit[2], hit[3]
        if not block:
            if not _refreshing:
                _refreshing = True
                threading.Thread(target=_refresh, name="fused-tailscale", daemon=True).start()
            return (hit[1], hit[2], hit[3]) if hit is not None else (None, OFF, _NOBODY)
        _refreshing = True
    return _refresh()


def status(block: bool = True) -> dict | None:
    """The computer's tailnet identity, or None (no CLI, not running)."""
    return _cached(block)[0]


def state() -> str:
    """Why ``status()`` is what it is: MISSING, OFF or RUNNING. Same cached
    read as ``status()``, so asking for both costs one CLI run."""
    return _cached()[1]


def peer_ips() -> frozenset[str]:
    """Every address on the tailnet as last read. Never waits (``_cached``)."""
    return _cached(block=False)[2]


def warm() -> None:
    """Start a read now, so the first request that needs one has it."""
    _cached(block=False)


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def is_tailnet_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return any(addr in net for net in _NETS)
