"""Token-verified desktop readiness probe (stdlib-only, cross-platform).

A desktop launch must confirm that the server answering on its port is *the
one it started* — not an unrelated process that happened to hold the port
(a stale dev server, another app). Each launch publishes a per-launch 256-bit
token plus the desktop instance id into the child/in-process server's
environment; the server echoes them from `/api/config` (see
`fused_render.paths.desktop_instance` and the `/api/config` handler). This
module polls that endpoint with the token header and reports ready only when
the echoed id + token match.

Shared by both backends: the Windows supervisor (out-of-process child server)
and the macOS app (in-process server thread). urllib + json only, so it stays
importable and testable on every platform.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Callable

# The desktop instance id both platforms advertise. A single constant so the
# server echo, the child env, and the probe all agree on one name.
DESKTOP_INSTANCE_ID = "desktop-v1"


def matching_server(port: int, token: str, instance_id: str = DESKTOP_INSTANCE_ID) -> bool:
    """True iff 127.0.0.1:<port>/api/config answers 200 and echoes our
    desktop instance id AND token. Any failure (refused, timeout, non-200,
    bad JSON, mismatched id/token) is a plain False — the caller keeps
    polling until its own deadline."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config",
        headers={"X-Fused-Desktop-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
        # http.client.HTTPException covers BadStatusLine etc.: a non-HTTP
        # process holding the port answers garbage that urllib surfaces here,
        # and it must be a plain False like every other failure — not escape
        # and (via core._start_ready_server) skip job.close(), orphaning the
        # child.
        return False
    instance_info = payload.get("desktop_instance") or {}
    return instance_info.get("id") == instance_id and instance_info.get("token") == token


def wait_until_ready(
    port: int,
    token: str,
    timeout_s: float,
    *,
    instance_id: str = DESKTOP_INSTANCE_ID,
    poll_interval: float = 0.1,
    on_poll: Callable[[], None] | None = None,
) -> bool:
    """Poll `matching_server` until it succeeds or `timeout_s` elapses.

    Returns True once the matching server answers, False on timeout. `on_poll`,
    if given, runs once per iteration before the readiness check — raise from
    it to abort early (the Windows backend passes a check that raises when the
    child server process has already exited, so a dead child fails fast instead
    of waiting out the whole deadline)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        if matching_server(port, token, instance_id):
            return True
        time.sleep(poll_interval)
    return False
