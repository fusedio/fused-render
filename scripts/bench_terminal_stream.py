#!/usr/bin/env python3
"""Throughput of the terminal WebSocket stream, in-process (no server).

Runs `head -c 20000000 /dev/zero | tr '\\0' 'a'` through the real
`/api/terminal/{sid}/stream` route via Starlette's TestClient, the way
tests/test_terminal_routes.py drives it, and prints bytes, frames and wall
time. Run it from a checkout (`.venv/bin/python scripts/bench_terminal_stream.py`);
the numbers are only comparable between runs on the same machine.
"""
import json
import os
import sys
import tempfile
import time

from fastapi.testclient import TestClient

from fused_render import pty_session
from fused_render.server import create_app
from fused_render.terminal_profiles import TerminalProfile

TOTAL = 20_000_000


def main() -> int:
    tmp = tempfile.mkdtemp()
    # The sleep lets the socket attach before the flood, so every byte is
    # counted from the live stream rather than the scrollback replay.
    cmd = f"sleep 0.5; head -c {TOTAL} /dev/zero | tr '\\0' 'a'"
    pty_session.resolve_profile = lambda cwd=None: TerminalProfile(
        shell="/bin/sh", argv=["/bin/sh", "-c", cmd], env=dict(os.environ), cwd=tmp)
    client = TestClient(create_app(start_dir=tmp))
    sid = client.post("/api/terminal", json={}, headers={"X-Fused": "1"}).json()["id"]

    got = frames = 0
    exit_frame = None
    with client.websocket_connect(f"/api/terminal/{sid}/stream") as ws:
        t0 = None
        while True:
            msg = ws.receive()
            if msg.get("bytes") is not None:
                if t0 is None and msg["bytes"]:
                    t0 = time.perf_counter()
                got += len(msg["bytes"])
                frames += 1
            elif msg.get("text") is not None:
                exit_frame = json.loads(msg["text"])
                break
        elapsed = time.perf_counter() - (t0 or time.perf_counter())
    pty_session.REGISTRY.shutdown_all()
    print(f"bytes={got} frames={frames} wall={elapsed:.2f}s "
          f"MB/s={got / 1e6 / elapsed:.1f} exit={exit_frame}")
    return 0 if got == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
