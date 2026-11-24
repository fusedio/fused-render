"""Away access, the computer's side: the way in beside the Wi-Fi.

Driven by one preference (shell/prefs.py): ``lan_remote_enabled``, the switch
under Phone access (meaningless while Phone access itself is off).

One route, carrying the SAME pinned TLS session as the Wi-Fi:

- **tailscale** — nothing to run: the listener already binds every interface;
  lan.py admits the tailnet name and addresses (tailscale.py).

lan.py asks this module two things: is away access on (``enabled``), and
which host names may address the listener (``hosts``).

A route is only told to a phone once this computer has reached its own
listener through it (``routes(probe)``): the tailnet name first, then the
tailnet address when the name does not answer here (MagicDNS off). A probe
from this computer cannot see what the phone's own system would refuse; it
catches the listener not answering on the tailnet at all.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from fused_render.tunnel import tailscale

logger = logging.getLogger("fused_render.tunnel.host")

#: Can this computer reach its own listener on ``host``? False: no. None:
#: nothing to try (the listener is not up). lan.py supplies it.
Probe = Callable[[str], "bool | None"]


class _Remote:
    def __init__(self):
        self._lock = threading.Lock()
        self.enabled = False

    def apply(self, enabled: bool) -> None:
        """Turn away access on or off to match the preference. Idempotent."""
        with self._lock:
            self.enabled = enabled

    def stop(self) -> None:
        """Phone access went off: no away routes until ``apply`` again."""
        with self._lock:
            self.enabled = False

    def tailnet(self) -> dict | None:
        return tailscale.status() if self.enabled else None

    def hosts(self) -> set[str]:
        """Names and addresses away routes may put in ``Host``."""
        ts = self.tailnet() if self.enabled else None
        if not ts:
            return set()
        out: set[str] = set(ts.get("ips") or [])
        if ts.get("host"):
            out.add(ts["host"])
        return out

    def _reachable(self, probe: Probe | None) -> tuple[str | None, str | None]:
        """``(host to tell phones, probe verdict)``. The verdict is None with
        no tailnet, ``"ok"`` when the name answered, ``"address"`` when only
        the address did, ``"failed"`` when neither did. With no ``probe`` the
        first candidate is taken on trust."""
        ts = self.tailnet() if self.enabled else None
        if not ts:
            return None, None
        # IPv4 only for the fallback: the phone's host check takes names and
        # dotted addresses.
        candidates = [h for h in (ts.get("host"), *(ip for ip in ts.get("ips") or [] if ":" not in ip)) if h]
        if not candidates:
            return None, None
        if probe is None:
            return candidates[0], "ok"
        for i, host in enumerate(candidates):
            if probe(host) is not False:  # None: nothing to try, not a failure
                return host, "ok" if i == 0 and host == ts.get("host") else "address"
        return None, "failed"

    def routes(self, probe: Probe | None = None) -> list[dict]:
        """The away routes a phone should know, in the order to try them."""
        host, _ = self._reachable(probe)
        return [{"kind": "tailscale", "host": host}] if host else []

    def status(self, probe: Probe | None = None) -> dict:
        ts = self.tailnet()
        route, verdict = self._reachable(probe)
        return {
            "enabled": self.enabled,
            "tailscale": ts,
            # What phones are told to dial, and how this computer's own try
            # at it went ("ok", "address", "failed"; None with no tailnet).
            "tailscale_route": route,
            "tailscale_probe": verdict,
            # Why there is (not) a tailnet, for the Preferences row: "missing"
            # (not installed), "off" (not running or signed out), "running".
            # None while away access is off: nothing was asked.
            "tailscale_state": (tailscale.RUNNING if ts else tailscale.state()) if self.enabled else None,
        }


remote = _Remote()
