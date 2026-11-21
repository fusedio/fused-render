"""Away access, the computer's side: which ways in are open beside the Wi-Fi.

Driven by two preferences (shell/prefs.py): ``lan_remote_enabled`` (the
switch under Phone access; meaningless while Phone access itself is off) and
``lan_relay`` (``host[:port][#sha256]``, the relay's agent address, with a
pin for a self-hosted relay's self-signed certificate). ``FUSED_RENDER_RELAY``
overrides the stored relay.

Two routes, both carrying the SAME pinned TLS session as the Wi-Fi:

- **relay** — ``TunnelAgent`` (agent.py) keeps an outbound connection to the
  relay; the phone dials ``<host-id>.<relay domain>``.
- **tailscale** — nothing to run: the listener already binds every interface;
  lan.py admits the tailnet name and addresses (tailscale.py).

lan.py asks this module three things: is away access on (``enabled``), which
host names may address the listener (``hosts``), and whether a loopback
connection is one of the relay's (``peer_for``).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from fused_render.tunnel import tailscale

logger = logging.getLogger("fused_render.tunnel.host")

#: The relay phones use when the user has not named one. Empty until a
#: Fused-operated relay exists; Preferences then asks for one.
DEFAULT_RELAY = ""


def key_path() -> str:
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), "tunnel", "host.key")


def relay_setting(stored: str | None) -> str:
    """The relay to use: the environment, then the stored preference, then
    the built-in default (possibly empty: no relay)."""
    env = os.environ.get("FUSED_RENDER_RELAY")
    if env is not None:
        return env.strip()
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return DEFAULT_RELAY


class _Remote:
    def __init__(self):
        self._lock = threading.Lock()
        self._agent = None
        self._agent_key: tuple | None = None
        self.enabled = False
        self.setting = ""
        self.error: str | None = None

    def apply(self, enabled: bool, setting: str, local_port: Callable[[], int | None]) -> None:
        """Start, keep or stop the relay agent to match. Idempotent."""
        with self._lock:
            self.enabled = enabled
            self.setting = setting
            self.error = None
            want = None
            if enabled and setting:
                try:
                    from fused_render.tunnel.agent import parse_relay

                    want = parse_relay(setting)
                except ValueError as e:
                    self.error = f"relay setting: {e}"
            if self._agent is not None and self._agent_key != want:
                self._agent.stop()
                self._agent = self._agent_key = None
            if want is not None and self._agent is None:
                try:
                    from fused_render.tunnel.agent import TunnelAgent, load_or_create_key

                    key = load_or_create_key(key_path())
                    host, port, pin = want
                    self._agent = TunnelAgent(host, port, key, local_port, relay_fingerprint=pin)
                    self._agent_key = want
                    self._agent.start()
                    logger.info("tunnel: away access via relay %s:%s", host, port)
                except ModuleNotFoundError:
                    self.error = ("away access needs the optional cryptography dependency "
                                  "([bundled]/[fused]); not available on this install.")
                except Exception as e:  # noqa: BLE001
                    logger.warning("tunnel: could not start", exc_info=True)
                    self.error = f"tunnel: {e}"

    def stop(self) -> None:
        """Phone access went off: no away routes until ``apply`` again."""
        with self._lock:
            self.enabled = False
            if self._agent is not None:
                self._agent.stop()
            self._agent = self._agent_key = None

    def peer_for(self, local_port: int) -> str | None:
        agent = self._agent
        return agent.peer_for(local_port) if agent is not None else None

    def relay_endpoint(self) -> tuple[str, int] | None:
        """(host, port) phones dial through the relay, while connected."""
        agent = self._agent
        if agent is None or not agent.connected or not agent.public_host:
            return None
        return agent.public_host, int(agent.public_port or 443)

    def tailnet(self) -> dict | None:
        return tailscale.status() if self.enabled else None

    def hosts(self) -> set[str]:
        """Names and addresses away routes may put in ``Host``."""
        if not self.enabled:
            return set()
        out: set[str] = set()
        endpoint = self.relay_endpoint()
        if endpoint:
            out.add(endpoint[0])
        ts = self.tailnet()
        if ts:
            if ts.get("host"):
                out.add(ts["host"])
            out.update(ts.get("ips") or [])
        return out

    def routes(self) -> list[dict]:
        """The away routes a phone should know, in the order to try them."""
        if not self.enabled:
            return []
        routes = []
        ts = self.tailnet()
        if ts and (ts.get("host") or ts.get("ips")):
            routes.append({"kind": "tailscale", "host": ts.get("host") or ts["ips"][0]})
        endpoint = self.relay_endpoint()
        if endpoint:
            routes.append({"kind": "relay", "host": endpoint[0], "port": endpoint[1]})
        return routes

    def status(self) -> dict:
        agent = self._agent
        relay = agent.status() if agent is not None else None
        ts = self.tailnet()
        return {
            "enabled": self.enabled,
            "relay_setting": self.setting,
            "relay_from_env": os.environ.get("FUSED_RENDER_RELAY") is not None,
            "relay": relay,
            "tailscale": ts,
            "error": self.error,
        }


remote = _Remote()
