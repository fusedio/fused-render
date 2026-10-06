"""The native windows an in-app restart carries over to its successor.

`fused-render://relaunch` quits this process and a detached shell `open`s the
bundle again with `fused-render://launch`, which by design opens NO window
(D128 — it was made for a browser tab that survives the restart). With native
windows on, every window is closed on the way out, so the successor boots with
nothing on screen and the user has to click the app again.

So the dying instance writes down what was open (`write_snapshot`, BEFORE the
windows are closed) and the successor reads it once it is serving
(`take_snapshot`) and opens the same things again. Pure and platform-neutral —
no AppKit, no app.py import — so the string handling is testable on every CI
lane; the AppKit half lives in mac_window.py / app.py.

The file stores a PATH + QUERY, never an origin: the successor may bind a
different port than this process did.
"""
from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlsplit

logger = logging.getLogger("fused_render")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def targets_from_urls(urls, port: int) -> list[str]:
    """Origin-free ``/path?query#fragment`` for each of OUR pages in ``urls``,
    in order. Anything else (about:blank, an external page, another local
    server) is not ours to reopen and is dropped."""
    out: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            host, url_port = parts.hostname, parts.port
        except ValueError:
            continue
        if parts.scheme != "http" or host not in _LOOPBACK_HOSTS or url_port != port:
            continue
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        if parts.fragment:
            target += "#" + parts.fragment
        out.append(target)
    return out


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def write_snapshot(path: str, targets: list[str], *, pid: int | None = None,
                   now: float | None = None) -> bool:
    """Record ``targets`` for the successor. An empty list records nothing and
    clears any earlier file, so a restart from a browser tab (or with native
    windows off) leaves the successor's behaviour exactly as it was. Never
    raises; True only if a file was written."""
    if not targets:
        _remove(path)
        return False
    payload = {
        "pid": os.getpid() if pid is None else pid,
        "at": time.time() if now is None else now,
        "windows": list(targets),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not record the open windows for the successor",
                       exc_info=True)
        _remove(tmp)
        return False
    return True


def discard_snapshot(path: str) -> None:
    _remove(path)


# What a restored window shows from the instant it opens until the new server
# answers. Static and self-contained (no script, no request): the server it
# would fetch from is exactly what is not up yet. Colours are the shell's own
# tokens (`--bg` in frontend/src/styles/tokens.css; mac_window.TITLEBAR_BG), so
# the window chrome and the page read as one surface, and it follows the OS
# light/dark like the titlebar does.
RESTARTING_HTML = (
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=color-scheme content='dark light'>"
    "<title>Restarting\u2026</title><style>"
    "html,body{height:100%;margin:0}"
    "body{display:flex;align-items:center;justify-content:center;"
    "background:#131417;color:#9aa0aa;"
    "font:15px -apple-system,BlinkMacSystemFont,sans-serif}"
    "@media (prefers-color-scheme:light){body{background:#fff;color:#5b6270}}"
    ".d{width:14px;height:14px;margin-right:12px;border-radius:50%;"
    "border:2px solid currentColor;border-top-color:transparent;"
    "animation:s 1s linear infinite}"
    "@keyframes s{to{transform:rotate(360deg)}}"
    "</style></head><body><div class=d></div>Restarting\u2026</body></html>"
)


def take_snapshot(path: str, *, max_age_s: float, now: float | None = None,
                  on_age=None) -> list[str]:
    """Consume the snapshot: the file is deleted BEFORE it is parsed, so it is
    one-shot whatever it holds (a crash while reopening must not replay it on
    every later launch). Returns the targets to reopen, or [] when there is
    no file, it is older than ``max_age_s``, or it is unreadable.

    ``on_age`` (optional) is called with the snapshot's age in seconds — the
    time since the predecessor's press — for the relaunch timing log."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    _remove(path)
    try:
        data = json.loads(raw)
        at = float(data["at"])
        windows = data["windows"]
    except (ValueError, TypeError, KeyError):
        logger.info("ignoring an unreadable window snapshot")
        return []
    now = time.time() if now is None else now
    if not isinstance(windows, list) or not (0 <= now - at <= max_age_s):
        logger.info("ignoring a stale window snapshot (%.0fs old)", now - at)
        return []
    # A path, not a URL: the caller prepends its own origin, so anything that
    # could re-point it ("//host", "http://…") is dropped.
    if on_age is not None:
        on_age(now - at)
    return [w for w in windows
            if isinstance(w, str) and w.startswith("/") and not w.startswith("//")]
