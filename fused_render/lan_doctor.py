"""What the computer can find out, on its own, about whether a phone on the
Wi-Fi can reach the local-network listener (lan.py).

The phone's own probe (ios/FusedRender/Reachability.swift) is the ground
truth for "can the phone get through"; this module covers the causes that
live on the computer and that the computer can see without a phone: no LAN
address, a VPN hiding it, the macOS firewall, the local hostname not
resolving, the listener not answering on its own address, and a computer
renamed since a phone paired. ``warnings()`` turns those into a list the
Preferences page shows under the sharing switch, one banner per entry.

Everything here shells out to OS tools (``scutil``, ``route``, ``dns-sd``,
``socketfilterfw``, ``ip``, ``avahi-resolve``) with short timeouts and is
cached for a few seconds: ``lan.status()`` runs on every prefs read.
Nothing here is load-bearing for sharing itself — every function degrades
to "unknown" (None / empty) rather than raising.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time

logger = logging.getLogger("fused_render.lan_doctor")

_CACHE_TTL_S = 8.0
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()

#: Interfaces that are tunnels, not LANs: a phone cannot reach an address on
#: one of these, and when the default route goes through one a VPN is hiding
#: the real network.
_TUNNEL_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap", "wg", "gpd", "vpn", "zt", "tailscale")


def _cached(key: str, compute):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    value = compute()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _run(args: list[str], timeout: float = 2.0) -> str | None:
    """stdout of a tool, or None when it is missing, fails or hangs."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _is_mac() -> bool:
    return sys.platform == "darwin"


# ---- the name --------------------------------------------------------------


def local_hostname() -> str | None:
    """The ``.local`` name the OS advertises for this computer, or None.

    macOS: ``scutil --get LocalHostName`` is the Bonjour name mDNSResponder
    keeps valid through sleep, wake and address changes; the phone resolves
    it natively. Linux: the hostname, when an avahi daemon is running to
    answer for it. Nothing else qualifies — a name fused-render cannot vouch
    for would put a code on screen that the phone cannot resolve."""
    return _cached("hostname", _local_hostname)


def _local_hostname() -> str | None:
    if _is_mac():
        out = _run(["scutil", "--get", "LocalHostName"])
        name = (out or "").strip()
        return f"{name}.local" if name else None
    if sys.platform.startswith("linux") and os.path.exists("/run/avahi-daemon/socket"):
        name = socket.gethostname().split(".")[0].strip()
        return f"{name}.local" if name else None
    return None


def hostname_resolves(name: str | None) -> bool | None:
    """Does the local name resolve to one of this computer's own addresses?
    True / False, or None when there is no tool to ask (not a reason to
    warn)."""
    if not name:
        return None
    return _cached(f"resolves:{name}", lambda: _hostname_resolves(name))


def _hostname_resolves(name: str) -> bool | None:
    mine = set(lan_ips())
    if _is_mac():
        # `dns-sd -G v4 <name>` streams until killed: run it for two seconds
        # and read what it printed. A resolved line carries the address in
        # its fifth column.
        try:
            proc = subprocess.Popen(["dns-sd", "-G", "v4", name], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        except OSError:
            return None
        try:
            out, _ = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        found = set(re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", out or ""))
        return bool(found & mine) if (found or mine) else None
    out = _run(["avahi-resolve", "-4", "-n", name], timeout=3.0)
    if out is None:
        return None
    found = set(re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", out))
    return bool(found & mine)


# ---- the addresses ---------------------------------------------------------


def lan_ip() -> str | None:
    """The address of the default route: the source address of a UDP socket
    "connected" to a private-range target (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


def interfaces() -> list[tuple[str, str]]:
    """``(interface, ipv4)`` for every non-loopback, non-link-local IPv4 the
    computer holds, from ``ifconfig`` (macOS) or ``ip`` (Linux). Empty when
    neither tool answers; ``lan_ips()`` then falls back to ``lan_ip()``."""
    return _cached("interfaces", _interfaces)


def _interfaces() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if _is_mac() or sys.platform.startswith(("freebsd", "openbsd")):
        out = _run(["ifconfig", "-a"]) or ""
        name = ""
        for line in out.splitlines():
            m = re.match(r"^([A-Za-z0-9_.-]+):", line)
            if m:
                name = m.group(1)
                continue
            m = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m and name:
                found.append((name, m.group(1)))
    elif sys.platform.startswith("linux"):
        out = _run(["ip", "-4", "-o", "addr", "show"]) or ""
        for line in out.splitlines():
            m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
            if m:
                found.append((m.group(1), m.group(2)))
    return [(n, ip) for n, ip in found if not ip.startswith(("127.", "169.254."))]


def lan_ips() -> list[str]:
    """Every address a phone could dial, default-route first, tunnels last."""
    primary = lan_ip()
    lan = [ip for n, ip in interfaces() if not _is_tunnel(n)]
    tunnels = [ip for n, ip in interfaces() if _is_tunnel(n)]
    ordered: list[str] = []
    for ip in ([primary] if primary else []) + lan + tunnels:
        if ip and ip not in ordered:
            ordered.append(ip)
    return ordered


def best_ip() -> str | None:
    """The address to put in the pairing code: a private address on a real
    interface (Wi-Fi/Ethernet), never a tunnel's — a phone cannot reach a
    VPN address, and ``lan_ip()`` returns exactly that while a VPN owns the
    default route."""
    lan = [ip for n, ip in interfaces() if not _is_tunnel(n) and _is_private(ip)]
    primary = lan_ip()
    if primary in lan:
        return primary
    if lan:
        return lan[0]
    return primary


def _is_tunnel(name: str) -> bool:
    return name.lower().startswith(_TUNNEL_PREFIXES)


def _is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def default_route_interface() -> str | None:
    return _cached("default_iface", _default_route_interface)


def _default_route_interface() -> str | None:
    if _is_mac():
        out = _run(["route", "-n", "get", "default"]) or ""
        m = re.search(r"interface:\s*(\S+)", out)
        return m.group(1) if m else None
    if sys.platform.startswith("linux"):
        out = _run(["ip", "route", "show", "default"]) or ""
        m = re.search(r"\bdev\s+(\S+)", out)
        return m.group(1) if m else None
    return None


# ---- the firewall ----------------------------------------------------------

_FW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def firewall(port: int | None = None) -> dict | None:
    """The host firewall's state, or None when there is none to ask:
    ``{"kind", "on", "block_all", "stealth", "blocked"}``. macOS
    (``kind: "macos"``): ``blocked`` is True when the running executable (the
    .app bundle, or the python binary under ``fused serve``) is listed with
    incoming connections blocked. Linux (``"ufw"`` / ``"firewalld"``):
    ``blocked`` is True when the firewall is active and no rule names
    ``port``; ``block_all``/``stealth`` are always False there."""
    return _cached(f"firewall:{port}", lambda: _firewall(port))


def _firewall(port: int | None) -> dict | None:
    if sys.platform.startswith("linux"):
        return _firewall_linux(port)
    if not _is_mac() or not os.path.exists(_FW):
        return None
    state = _run([_FW, "--getglobalstate"]) or ""
    if not state:
        return None
    def enabled(text: str | None) -> bool:
        t = (text or "").lower()
        return "enabled" in t and "disabled" not in t

    on = enabled(state)
    block_all = enabled(_run([_FW, "--getblockall"]))
    stealth = enabled(_run([_FW, "--getstealthmode"]))
    blocked = False
    apps = _run([_FW, "--listapps"], timeout=3.0) or ""
    exe = os.path.realpath(sys.executable)
    # `--listapps` prints one app per block: a path line, then a line saying
    # "Allow incoming connections" or "Block incoming connections".
    current: str | None = None
    for line in apps.splitlines():
        # Entries are numbered ("1 :  /Applications/Fused Render.app").
        line = re.sub(r"^\d+\s*:\s*", "", line.strip())
        if line.startswith("/") or ".app" in line:
            current = line.split(" (")[0].strip() if ".app" in line else line
            continue
        if current and "block incoming" in line.lower():
            if exe.startswith(os.path.realpath(current)):
                blocked = True
    return {"kind": "macos", "on": on, "block_all": block_all, "stealth": stealth, "blocked": blocked}


def _firewall_linux(port: int | None) -> dict | None:
    """ufw or firewalld, whichever answers. Both usually need root to list
    rules, in which case the tool prints nothing useful and this is None
    (unknown), never a warning."""
    needle = str(port) if port else None
    ufw = _run(["ufw", "status"])
    if ufw and "status: active" in ufw.lower():
        blocked = bool(needle) and needle not in ufw
        return {"kind": "ufw", "on": True, "block_all": False, "stealth": False, "blocked": blocked}
    if ufw and "status: inactive" in ufw.lower():
        return {"kind": "ufw", "on": False, "block_all": False, "stealth": False, "blocked": False}
    state = _run(["firewall-cmd", "--state"])
    if state and "running" in state.lower():
        ports = _run(["firewall-cmd", "--list-ports"], timeout=3.0)
        if ports is None:
            return None  # cannot list: unknown
        blocked = bool(needle) and needle not in ports
        return {"kind": "firewalld", "on": True, "block_all": False, "stealth": False, "blocked": blocked}
    return None


# ---- the listener itself -----------------------------------------------------


def self_connect(ip: str | None, port: int | None, fingerprint: str | None, timeout: float = 2.0) -> bool | None:
    """Can this computer open a TLS connection to its own listener on its LAN
    address, and does the certificate it gets match ``fingerprint``? None
    when there is nothing to try."""
    if not ip or not port or not fingerprint:
        return None
    return _cached(f"self:{ip}:{port}", lambda: _self_connect(ip, port, fingerprint, timeout))


def _self_connect(ip: str, port: int, fingerprint: str, timeout: float) -> bool:
    import hashlib
    import ssl

    # A CLIENT context, so whatever `ssl.SSLContext` currently is (truststore's
    # after _system_trust injected it) is the right class; the stdlib class
    # cannot have its verify_mode set once injection has happened. No
    # verification: the fingerprint below is the check, as on the phone.
    ctx = ssl._create_unverified_context()  # noqa: SLF001
    try:
        with socket.create_connection((ip, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=ip) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return False
    return bool(der) and hashlib.sha256(der).hexdigest() == fingerprint


# ---- the verdict -------------------------------------------------------------


def warnings(*, running: bool, port: int | None, ip: str | None, fingerprint: str | None,
             hostname: str | None, devices: list[dict] | None = None) -> list[dict]:
    """``[{"code", "message"}]`` for everything the computer can see that
    would keep a phone from reaching it. Empty is the good case. Each check
    is independent and best-effort."""
    out: list[dict] = []
    if not running:
        return out  # the listener's own `error` already says why

    try:
        ifaces = interfaces()
    except Exception:  # noqa: BLE001
        ifaces = []
    lan_ifaces = [(n, a) for n, a in ifaces if not _is_tunnel(n) and _is_private(a)]
    if ifaces and not lan_ifaces:
        out.append({"code": "no_lan", "message":
                    "No Wi-Fi or Ethernet address found. Connect this computer to the network the phone uses."})
    elif not ip:
        out.append({"code": "no_lan", "message":
                    "No network address found. Connect this computer to the network the phone uses."})

    try:
        route = default_route_interface()
    except Exception:  # noqa: BLE001
        route = None
    if route and _is_tunnel(route):
        out.append({"code": "vpn", "message":
                    "A VPN is active. Phones usually cannot reach this computer while it is on. "
                    "The code uses the Wi-Fi address, but the VPN may still block it."})

    try:
        fw = firewall(port)
    except Exception:  # noqa: BLE001
        fw = None
    if fw and fw["on"] and (fw["block_all"] or fw["stealth"] or fw["blocked"]):
        if fw.get("kind") == "macos":
            message = ("The macOS firewall may block phones from reaching this computer. Allow incoming "
                       "connections for Fused Render in System Settings → Network → Firewall.")
        else:
            message = (f"The firewall ({fw.get('kind')}) is active and has no rule for port {port}. "
                       f"Allow incoming TCP on {port} so phones can reach this computer.")
        out.append({"code": "firewall", "message": message})

    try:
        resolves = hostname_resolves(hostname)
    except Exception:  # noqa: BLE001
        resolves = None
    if hostname and resolves is False:
        out.append({"code": "hostname", "message":
                    f"This computer's local name ({hostname}) is not resolving. Phones will fall back "
                    "to the IP address."})

    try:
        reach = self_connect(ip, port, fingerprint)
    except Exception:  # noqa: BLE001
        reach = None
    if reach is False:
        out.append({"code": "listener", "message":
                    f"The listener is not answering on {ip}:{port}. Turn sharing off and on again; "
                    "if it persists, send a diagnostics bundle."})

    renamed = sorted({d.get("hostname") for d in (devices or [])
                      if d.get("hostname") and hostname and d.get("hostname") != hostname})
    if renamed:
        out.append({"code": "renamed", "message":
                    f"This computer was renamed since a phone paired (was {', '.join(renamed)}). "
                    "The phone will reconnect by address, or scan a new code."})
    return out
