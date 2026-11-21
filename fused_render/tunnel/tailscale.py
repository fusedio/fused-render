"""Tailscale, when the computer is on a tailnet: a second way in, no relay.

The phone listener (lan.py) binds every interface, so a phone on the same
tailnet can already open a TCP connection to the computer's tailnet address.
What it could not do before is get past ``_host_ok``: the tailnet name and
address are not LAN names. With away access on, lan.py admits them, the
pairing code names the MagicDNS name (``ts=``), and the phone tries it after
the Wi-Fi routes. Encryption is WireGuard underneath and the same pinned TLS
on top.

Read from ``tailscale status --json`` (cached; a missing CLI or a stopped
daemon is simply "no tailnet").
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
_cache: tuple[float, dict | None] | None = None
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


def _read() -> dict | None:
    cli = _cli()
    if cli is None:
        return None
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True,
                             timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return parse(data)


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


def status() -> dict | None:
    """The computer's tailnet identity, or None (no CLI, not running)."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache is not None and now - _cache[0] < _TTL_S:
            return _cache[1]
    value = _read()
    with _lock:
        _cache = (now, value)
    return value


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
