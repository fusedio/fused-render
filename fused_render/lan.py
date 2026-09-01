"""Share your apps with phones on the same Wi-Fi.

Off by default (the ``lan_enabled`` preference, shell/prefs.py). When on, the
server opens a SECOND listener on every interface and advertises it over
mDNS as ``render.fused.local`` (alias ``render.local``), so a phone on the
same network opens ``http://render.fused.local/`` and lands on a grid of the
apps the /apps hub lists — every folder under ``~/Fused`` plus the linked
external folders (``allowed_roots``). Plain http on purpose: a trusted certificate needs
either a public domain (not local) or a CA profile installed on the phone
(setup friction); both are recorded follow-ups. On plain http iOS withholds
the live microphone and clipboard READ; everything server-side works.

**The loopback server is untouched.** D3's one security freebie — bind
127.0.0.1 — stays exactly as it is for the desktop; this module never calls
``set_server_origin_env``/``write_server_json``, so runPython children keep
talking to loopback. The LAN listener serves ``LanApp``, an ASGI wrapper
around the same FastAPI app that admits ONLY what an app page needs and
refuses everything else with 404 — the explorer, the shell, Claude sessions,
the file index, mounts, git, config writes, and any path outside
``~/Fused/local`` or the app state dir ``~/.fused-render`` (``allowed_roots``;
apps keep per-install data there). Nothing on the LAN side can widen what the
wrapper forwards without editing the allowlist below.

Scoping rule: every path-bearing argument (``path``, ``base``, ``py``,
``html``, ``image``, ``images``, ``paths``) is resolved the way the inner
route resolves it (relative against the page named by ``html``/``base``),
realpath'd, and must land inside the realpath of ``~/Fused/local``. A page
outside it — or a page inside it reaching outside — gets 404, not 403: from
the phone's point of view those files do not exist.

Who gets in: only devices that scanned the QR code in Preferences (see the
pairing section below). Unpaired requests get a how-to-pair page at ``/`` and
401 everywhere else; a Host allowlist closes DNS rebinding. No PIN and no
approve-on-laptop dialog, by the owner's call — the QR is the whole UX.

Lifecycle: ``attach(app)`` at either entry point (cli.py / app.py) hands the
inner app over; ``apply(enabled)`` — called from the prefs PUT and once at
startup — starts or stops the listener + mDNS on a daemon thread. Stopping is
``should_exit`` + a bounded join + zeroconf goodbye; the process quit ladder
(D184) is deliberately not entered from here — a daemon thread dying with the
process costs a stale mDNS cache entry at worst.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)

from fused_render.shell.seed import fused_dir

logger = logging.getLogger("fused_render.lan")

#: The name a phone types. Multi-label ``.local`` names resolve through Apple's
#: mDNSResponder (verified with ``ping render.fused.local`` on macOS 15); the
#: single-label alias covers resolvers that only answer one label.
HOSTNAME = "render.fused.local"
ALIAS_HOSTNAME = "render.local"
#: Port 80 first so the URL carries no port (macOS ≥ 10.14 lets a non-root
#: process bind it); the fallbacks are for Linux/Windows or a taken 80.
PORT_CANDIDATES = (80, 8080, 1888)
#: The https listener beside it (lan_tls.py): trusted by the native shell,
#: which pins our private CA from the pairing QR; browsers keep http. 443
#: first so the app's URL carries no port.
TLS_PORT_CANDIDATES = (443, 8443, 1889)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "lan")


def local_root() -> str:
    """``~/Fused/local`` — where ``/a/<slug>`` shortcuts and default capture
    paths land when an app is not otherwise placed."""
    return os.path.join(fused_dir(), "local")


# ---- path scoping -----------------------------------------------------------

_ROOTS_TTL_S = 2.0
_roots_cache: tuple[float, list[str]] | None = None


#: The ONLY subfolders of the app-state dir (``home_dir()``, i.e.
#: ``~/.fused-render`` or a branch nest of it) the LAN side may reach. That dir
#: also holds the private CA key (``lan_tls/``), every Claude transcript,
#: logs, search indexes, mount definitions and gigabytes of venvs and git
#: branches — none of an app's business, and a paired phone that could read
#: ``lan_tls/ca.key`` could impersonate the computer's https forever. So the
#: state dir is NOT a root; only these leaves are, and everything else there is
#: 404 over the LAN. Apps are not meant to write under the state dir at all —
#: when a feature deliberately needs a new folder here, add its relative path
#: to this list. Kept in sync by hand, on purpose.
_HOME_SUBROOTS = (
    "ai/images",      # /api/ai/image output
    "ai/videos",      # /api/ai/video output
    "ai/transcripts", # /api/ai/transcribe output
    "recordings",     # fused.capture uploads
    "data",           # per-app scratch state (data/<app>/…)
    "canvases",       # the canvases sub-app store
    "slides",         # the slides sub-app store
    "docs",           # the docs sub-app store
    "appfiles",       # .fused app-file staging
    "app-versions",   # per-app version history
)


def allowed_roots() -> list[str]:
    """The folders the LAN side may reach: the whole workspace (``~/Fused`` —
    every tag folder: local, showcase, clones …), a fixed allow-list of
    app-data leaves under the app-state dir (``_HOME_SUBROOTS`` — NOT the whole
    state dir, which holds the CA key, transcripts and logs), and every LINKED
    app — an external folder registered through "Open app" (registered_apps.py),
    which the /apps hub lists beside the workspace. Registered folders come from
    a JSON store, so the list is held for a couple of seconds: one page load
    fans out into many scoped reads."""
    global _roots_cache
    import time

    now = time.monotonic()
    if _roots_cache is not None and now - _roots_cache[0] < _ROOTS_TTL_S:
        return _roots_cache[1]
    from fused_render.shell.storage import home_dir

    home = home_dir()
    roots = [fused_dir()]
    roots.extend(os.path.join(home, sub) for sub in _HOME_SUBROOTS)
    try:
        from fused_render import registered_apps

        roots.extend(a["path"] for a in registered_apps.registered_apps() if a.get("path"))
    except Exception:  # noqa: BLE001 — a broken registry narrows, never widens
        logger.warning("lan: registered apps unreadable", exc_info=True)
    seen: list[str] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    _roots_cache = (now, seen)
    return seen


def _inside_root(path: str) -> bool:
    try:
        real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        roots = [os.path.realpath(r) for r in allowed_roots()]
    except (OSError, ValueError):
        return False
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def _resolve(value, anchor) -> str | None:
    """A path argument as the inner route will see it: absolute as-is, relative
    against the directory of ``anchor`` (the page's own path). None = cannot
    resolve (relative with no anchor) — treated as out of scope."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = os.path.expanduser(value.strip())
    if os.path.isabs(value):
        return value
    if not isinstance(anchor, str) or not os.path.isabs(anchor):
        return None
    return os.path.normpath(os.path.join(os.path.dirname(anchor), value))


#: Body/query keys that name a file. Lists (``images``, ``paths``, a repeated
#: query key) are checked element-wise. ``html``/``base`` are anchors for
#: relative values and are themselves scoped. ``src``/``dst`` (rename, copy),
#: ``from``/``to`` (trash-move) and ``dest`` (compress) are BOTH ends of a
#: move: a file may not leave the roots, and nothing may arrive from outside.
_PATH_KEYS = ("path", "py", "html", "base", "image", "images", "paths",
              "src", "dst", "from", "to", "dest")


def _anchor(args: dict):
    """The page a relative value resolves against — ``html``, else ``base``.
    Query dicts carry lists; a repeated anchor with two different values is
    ambiguous (a relative ``py`` could stay in root against one and escape
    against the other), so it resolves nothing."""
    for key in ("html", "base"):
        value = args.get(key)
        if isinstance(value, list):
            distinct = {v for v in value if isinstance(v, str) and v}
            if len(distinct) > 1:
                return "\0"  # never absolute → every relative value is out of scope
            value = next(iter(distinct), None)
        if isinstance(value, str) and value:
            return value
    return None


def _args_in_scope(args: dict) -> bool:
    anchor = _anchor(args)
    for key in _PATH_KEYS:
        if key not in args:
            continue
        values = args[key]
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if value is None or value == "":
                continue
            resolved = _resolve(value, anchor)
            if resolved is None or not _inside_root(resolved):
                return False
    return True


# ---- the allowlist ----------------------------------------------------------

#: Exact paths (method set) forwarded to the inner app after scoping. Anything
#: not here — and not under a prefix below — is a 404 on the LAN side.
_EXACT: dict[str, frozenset[str]] = {
    "/render": frozenset({"GET"}),
    "/api/run": frozenset({"POST"}),
    "/api/engine": frozenset({"POST"}),
    "/api/engine/forget": frozenset({"POST"}),
    "/api/env/install": frozenset({"GET", "POST"}),
    "/api/env/progress": frozenset({"GET"}),
    "/api/env/cancel": frozenset({"POST"}),
    # Every file operation, scoped to the roots. Not here on purpose: `reveal`
    # (opens Finder on the laptop), `pick-file`/`pick-folder` (native dialogs
    # on the laptop's screen) — they act on the desktop, not on files.
    "/api/fs/raw": frozenset({"GET", "HEAD"}),
    "/api/fs/stat": frozenset({"GET"}),
    "/api/fs/list": frozenset({"GET"}),
    "/api/fs/walk": frozenset({"GET"}),
    "/api/fs/conditions": frozenset({"GET"}),
    "/api/fs/git-repo": frozenset({"GET"}),
    "/api/fs/write": frozenset({"POST"}),
    "/api/fs/mkdir": frozenset({"POST"}),
    "/api/fs/upload": frozenset({"POST"}),
    "/api/fs/delete": frozenset({"POST"}),
    "/api/fs/rename": frozenset({"POST"}),
    "/api/fs/copy": frozenset({"POST"}),
    "/api/fs/trash-move": frozenset({"POST"}),
    "/api/fs/compress": frozenset({"POST"}),
    "/api/apps/py": frozenset({"GET"}),
    "/api/apps/background/status": frozenset({"GET"}),
    "/api/apps/background/start": frozenset({"POST"}),
    "/api/apps/background/stop": frozenset({"POST"}),
    "/api/apps/background/restart": frozenset({"POST"}),
    "/api/apps/background/autostart": frozenset({"POST"}),
    "/api/ai": frozenset({"POST"}),
    "/api/ai/cancel": frozenset({"POST"}),
    "/api/ai/catalog": frozenset({"GET"}),
    "/api/ai/embed": frozenset({"POST"}),
    "/api/ai/image": frozenset({"POST"}),
    "/api/ai/video": frozenset({"POST"}),
    "/api/ai/transcribe": frozenset({"POST"}),
    "/api/ai/runtime": frozenset({"GET"}),
    "/api/ai/runtime/download": frozenset({"POST"}),
    "/api/ai/runtime/load": frozenset({"POST"}),
    "/api/ai/runtime/unload": frozenset({"POST"}),
    "/api/jobs": frozenset({"GET", "POST"}),
    "/api/calls/event": frozenset({"POST"}),
    "/api/config": frozenset({"GET"}),
}
#: Prefixes forwarded without body inspection: the runtime and template
#: assets, the engine proxy (the daemon behind it is validated by
#: engine_host, and the browser never sees its port), job cancel/dismiss.
_PREFIXES = ("/static/", "/template-assets/", "/template-shared/", "/api/engines/", "/api/jobs/")


def _not_found() -> Response:
    return PlainTextResponse("not found", status_code=404)


# ---- pairing: which devices may use the listener ----------------------------
#
# QR only, by the owner's call: Preferences shows a QR of
# http://render.fused.local/pair?t=<token>; the phone scans it, /pair sets a
# long-lived device cookie and lands on the grid. No PIN, no approval dialog.
# The token is minted over LOOPBACK (the desktop's own API — a LAN peer cannot
# mint one), is good for five minutes and pairs ONE device: Preferences shows a
# fresh code the moment a pairing lands (it watches the device list) and every
# five minutes regardless. Single-use is the owner's call (2026-08-28): a code
# photographed off the screen must not pair a second device. The known cost —
# iOS's Control Center scanner opens the URL in a sandboxed in-app browser
# whose "Open in Safari" re-opens the same, now spent, URL — is accepted; the
# Camera app and the native shell pair the right browser first time. The cookie is
# 128 bits of randomness; the store keeps its SHA-256 plus a name derived from
# the user agent, so a stolen store file cannot forge a cookie and the
# Preferences list can say "iPhone · Safari, last seen 2h ago" and revoke it.
#
# What this does and does not cover: it answers "is this MY device", not "is
# this network safe" — over plain http the cookie travels in clear, which
# WPA2/3 home Wi-Fi encrypts per client and an open network does not. The
# Preferences copy says so; https stays the follow-up.

COOKIE = "fused_lan_device"
COOKIE_MAX_AGE_S = 90 * 86400
PAIR_TOKEN_TTL_S = 5 * 60
_LAST_SEEN_STEP_S = 10 * 60

_pair_tokens: dict[str, float] = {}  # token -> expiry (monotonic)
_pair_lock = threading.Lock()
# One lock around every read-modify-write of lan_devices.json: the LAN thread
# bumps last_seen while the loopback thread revokes, and an unlocked bump could
# write back a snapshot that still contains the device just revoked.
_devices_lock = threading.Lock()


def _devices_path() -> str:
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), "lan_devices.json")


def _read_devices() -> list[dict]:
    from fused_render.shell import storage

    data = storage.read_json(_devices_path())
    devices = data.get("devices") if isinstance(data, dict) else None
    return [d for d in (devices or []) if isinstance(d, dict) and d.get("hash")]


def _write_devices(devices: list[dict]) -> None:
    from fused_render.shell import storage

    storage.write_json(_devices_path(), {"devices": devices})


def _hash(secret: str) -> str:
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_pair_token() -> str:
    """A five-minute, single-use pairing token. Loopback callers only (the router
    below is on the inner app; the LAN wrapper never forwards it)."""
    import secrets
    import time

    token = secrets.token_urlsafe(24)
    with _pair_lock:
        # Exactly one live code: Preferences shows one QR, and minting a new
        # one (rotation, "New code", a pairing) must invalidate what the old
        # QR still displays somewhere.
        _pair_tokens.clear()
        _pair_tokens[token] = time.monotonic() + PAIR_TOKEN_TTL_S
    return token


def _spend_pair_token(token: str) -> bool:
    """True once per token: valid and not used before."""
    import time

    with _pair_lock:
        exp = _pair_tokens.pop(token, None)
    return exp is not None and exp >= time.monotonic()


def _device_name(user_agent: str) -> str:
    ua = user_agent or ""
    if "iPhone" in ua:
        device = "iPhone"
    elif "iPad" in ua:
        device = "iPad"
    elif "Android" in ua:
        device = "Android"
    elif "Macintosh" in ua:
        device = "Mac"
    elif "Windows" in ua:
        device = "Windows PC"
    elif "Linux" in ua:
        device = "Linux"
    else:
        device = "Device"
    if "FusedRender-iOS/" in ua:
        # The native shell (ios/) — not a browser, whatever WebKit's UA says.
        browser = "Fused Render app"
    elif "CriOS" in ua or ("Chrome" in ua and "Edg" not in ua):
        browser = "Chrome"
    elif "Edg" in ua:
        browser = "Edge"
    elif "FxiOS" in ua or "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua:
        browser = "Safari"
    else:
        browser = "browser"
    return f"{device} · {browser}"


def _pair_device(user_agent: str) -> tuple[str, dict]:
    """Register a new device; returns (cookie secret, public record)."""
    import secrets
    import time

    secret = secrets.token_urlsafe(32)
    now = time.time()
    record = {
        "id": secrets.token_hex(6),
        "hash": _hash(secret),
        "name": _device_name(user_agent),
        "paired_at": now,
        "last_seen": now,
    }
    with _devices_lock:
        devices = _read_devices()
        devices.append(record)
        _write_devices(devices)
    return secret, _public(record)


def _public(record: dict) -> dict:
    return {k: record[k] for k in ("id", "name", "paired_at", "last_seen") if k in record}


def list_devices() -> list[dict]:
    return [_public(d) for d in _read_devices()]


def revoke_device(device_id: str) -> bool:
    with _devices_lock:
        devices = _read_devices()
        kept = [d for d in devices if d.get("id") != device_id]
        if len(kept) == len(devices):
            return False
        _write_devices(kept)
    return True


def revoke_all_devices() -> None:
    with _devices_lock:
        _write_devices([])


def _cookie_secret(scope) -> str | None:
    raw = _header(scope, b"cookie")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value
    return None


def _paired(scope) -> bool:
    """True when the request carries a cookie for a stored device; bumps that
    device's last_seen at most every ten minutes (a write per request would
    churn the file on every asset)."""
    import time

    secret = _cookie_secret(scope)
    if not secret:
        return False
    digest = _hash(secret)
    with _devices_lock:
        devices = _read_devices()
        for d in devices:
            if d.get("hash") == digest:
                now = time.time()
                if now - float(d.get("last_seen") or 0) > _LAST_SEEN_STEP_S:
                    d["last_seen"] = now
                    try:
                        _write_devices(devices)
                    except OSError:
                        pass
                return True
    return False


def _host_ok(scope) -> bool:
    """Only the names we advertise (and the raw LAN address) may address the
    listener — a website in a LAN browser that rebinds its own DNS to us would
    otherwise arrive same-origin."""
    host = _header(scope, b"host").split(":")[0].strip().lower()
    if not host:
        return False
    if host in (HOSTNAME, ALIAS_HOSTNAME):
        return True
    ip = _controller._ip or lan_ip()
    return bool(ip) and host == ip


_PAIR_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fused apps</title>
<style>
html{color-scheme:light dark;background:#131417;color:#e8eaed;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:light){html{background:#fff;color:#17181a}}
main{max-width:28rem;margin:18vh auto 0;padding:0 24px}h1{font-size:22px;margin:0 0 12px}p{color:#9aa0a6;margin:0 0 10px}
</style><main><h1>%s</h1><p>%s</p><p>%s</p></main>"""


def _pair_page(title: str, line: str, hint: str = "") -> Response:
    from html import escape

    return Response(_PAIR_PAGE % (escape(title), escape(line), escape(hint)),
                    media_type="text/html", status_code=200 if not hint else 403)


def _unauthorized(scope) -> Response:
    """An unpaired request: the grid explains how to pair; everything else is a
    bare 401 (a page's fetches must not get an HTML document back)."""
    if scope["path"] in ("/", "/index.html"):
        return _pair_page(
            "Scan to pair this device",
            "On the computer, open Preferences → Render local network and scan the QR code "
            "with the iPhone's Camera app — not the Control Center QR scanner, whose "
            "in-app browser cannot pair Safari.",
            "This device is not paired yet.")
    return JSONResponse({"error": "not paired"}, status_code=401)


def _with_cookie(response: Response, secret: str, *, secure: bool) -> Response:
    # A cookie minted over https (the app) must never travel over http: without
    # `Secure`, a plain http://render.fused.local link inside a page would replay
    # it in cleartext on the Wi-Fi. Browsers pair over http and keep a plain one.
    response.set_cookie(COOKIE, secret, max_age=COOKIE_MAX_AGE_S, httponly=True,
                        samesite="lax", path="/", secure=secure)
    return response


#: Called with the new device's public record when a phone pairs — the desktop
#: app hooks a notification here ("iPhone · Fused Render app paired"); the CLI
#: leaves it None and logs.
on_paired = None


def _handle_pair(scope) -> Response:
    """A live token pairs THIS browser and lands it on the grid. SINGLE use
    (owner's call, 2026-08-28): one scan, one device; Preferences shows a fresh
    code the moment a pairing lands. A code that was already used, or has run
    out, gets the expired page."""
    query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    token = (query.get("t") or [""])[0]
    if not token or not _spend_pair_token(token):
        return _pair_page(
            "That code has expired",
            "Each code pairs one device and lasts five minutes. Open Preferences → Render "
            "local network on the computer — it shows a fresh one — and scan again.",
            "Not paired.")
    secret, record = _pair_device(_header(scope, b"user-agent"))
    logger.info("lan: paired %s", record.get("name"))
    if on_paired is not None:
        try:
            on_paired(record)
        except Exception:  # noqa: BLE001 — a notification must never fail a pairing
            logger.warning("lan: on_paired hook failed", exc_info=True)
    return _with_cookie(RedirectResponse("/", status_code=302), secret,
                        secure=scope.get("scheme") == "https")


_RUNTIME_PATH = os.path.join(os.path.dirname(_STATIC_DIR), "runtime.js")
_PHONE_PATH = os.path.join(_STATIC_DIR, "runtime-phone.js")
_runtime_cache: tuple[tuple[float, float], bytes] | None = None


def _phone_runtime() -> Response:
    """``/static/runtime.js`` for the phone: the stock runtime followed by
    ``static/lan/runtime-phone.js``, which sets ``fused.device`` and makes
    ``fused.capture.*`` / ``fused.fileIndex`` fail fast with an error naming
    where they ARE available (the computer, or the Fused Render app) — the
    desktop implementations would record the computer, which is nonsense from
    a phone. Same URL the ``/render`` injection already emits, so no
    HTML rewriting; the desktop's loopback server keeps serving the stock file.
    Concatenated once per (mtime, mtime) so a dev edit shows on reload."""
    global _runtime_cache
    try:
        key = (os.path.getmtime(_RUNTIME_PATH), os.path.getmtime(_PHONE_PATH))
    except OSError:
        return _not_found()
    if _runtime_cache is None or _runtime_cache[0] != key:
        with open(_RUNTIME_PATH, "rb") as f:
            stock = f.read()
        with open(_PHONE_PATH, "rb") as f:
            phone = f.read()
        _runtime_cache = (key, stock + b"\n\n" + phone)
    return Response(_runtime_cache[1], media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


class LanApp:
    """ASGI wrapper: answers lifespan itself (never forwarded — the inner app's
    startup/shutdown handlers belong to the loopback server; forwarding them
    would run engine_host's tree-kill when the LAN switch flips OFF), closes
    websockets (``/api/fs/events`` — the page degrades to polling), serves its
    own grid, and forwards allowlisted, scoped HTTP requests to the inner app."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if kind == "websocket":
            # /api/fs/events (the runtime's change feed) is the one socket a
            # page needs; forwarded when every watched `path` is in the roots.
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"),
                             keep_blank_values=True)
            if (_host_ok(scope) and _paired(scope) and scope["path"] == "/api/fs/events"
                    and query.get("path") and _args_in_scope(query)):
                await self.inner(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            return
        response = await self._route(scope, receive)
        if response is None:
            await self.inner(scope, receive, send)
        else:
            await response(scope, receive, send)

    async def _route(self, scope, receive) -> Response | None:
        """A Response to send ourselves, or None to forward (scope may be
        forwarded through ``_Forward`` when the body had to be read)."""
        path = scope["path"]
        method = scope["method"].upper()

        if not _host_ok(scope):
            return _not_found()
        if path == "/pair" and method == "GET":
            return _handle_pair(scope)
        if path == "/lan/ca.pem" and method == "GET":
            # The private CA's PUBLIC certificate, for the native shell to pin
            # after checking it against the fingerprint the QR carried.
            from fused_render import lan_tls

            return Response(lan_tls.ca_pem(), media_type="application/x-pem-file",
                            headers={"Cache-Control": "no-store"})
        if path == "/api/lan/tls" and method == "GET":
            # Where https is and which CA signs it — for a shell that found the
            # computer over Bonjour rather than a QR (trust on first use).
            from fused_render import lan_tls

            return JSONResponse({"https_port": _controller.tls_port,
                                 "ca_fingerprint": lan_tls.ca_fingerprint() if _controller.tls_running else None})
        if not _paired(scope):
            return _unauthorized(scope)

        if path == "/" or path == "/index.html":
            # The grid is a second Vite page (frontend/lan.html → shell-dist/
            # lan.html); its assets resolve under /static/shell-dist/, which the
            # /static/ prefix below already forwards.
            page = os.path.join(os.path.dirname(_STATIC_DIR), "shell-dist", "lan.html")
            if not os.path.isfile(page):
                return PlainTextResponse(
                    "phone grid not built (frontend/lan.html → shell-dist/lan.html); "
                    "run: cd frontend && npm run build", status_code=503)
            return FileResponse(page, headers={"Cache-Control": "no-store"})
        if path == "/api/lan/apps" and method == "GET":
            return JSONResponse({"apps": lan_apps(), "host": HOSTNAME})
        if path.startswith("/a/"):
            return self._app_shortcut(path[3:])
        if path == "/static/runtime.js" and method in ("GET", "HEAD"):
            # The native iOS shell (ios/) marks its user agent: it gets the
            # STOCK runtime — a phone browser gets the "not available here"
            # overrides, the shell installs its own native bridge on top
            # (ios/FusedRender/Resources/runtime-ios.js).
            if "FusedRender-iOS/" not in _header(scope, b"user-agent"):
                return _phone_runtime()

        if any(path.startswith(p) for p in _PREFIXES):
            return None
        allowed = _EXACT.get(path)
        if allowed is None or method not in allowed:
            return _not_found()

        # EVERY value of a repeated key is scoped, not the last one: which
        # duplicate the inner route reads is its business, and a
        # `?path=<in-root>&path=/etc/passwd` must fail whichever it is.
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"),
                         keep_blank_values=True)
        if not _args_in_scope(query):
            return _not_found()

        if method in ("GET", "HEAD"):
            return None

        # POST: read the body once, scope it, and hand the inner app a receive
        # that replays the cached bytes.
        body = await _read_body(receive)
        content_type = _header(scope, b"content-type")
        if content_type.startswith("multipart/form-data"):
            args = await _multipart_fields(scope, body)
        elif body.strip():
            try:
                args = json.loads(body)
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
        else:
            args = {}
        if not _args_in_scope(args):
            return _not_found()
        return _Forward(self.inner, body)

    def _app_shortcut(self, rest: str) -> Response:
        """``/a/<slug>[/<file.html>]`` → 302 to the page's real ``/render`` URL
        (the runtime reads its own path from ``?path=``, so the landing URL has
        to be the canonical one)."""
        parts = [p for p in rest.split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            return _not_found()
        # The slug names an app the grid lists (any tag, or a linked folder);
        # a folder under ~/Fused/local by that name is the fallback.
        folder = next((a["path"] for a in lan_apps() if a["name"] == parts[0]), None) \
            or os.path.join(local_root(), parts[0])
        file = "/".join(parts[1:]) or _entry_for(folder) or "index.html"
        target = os.path.normpath(os.path.join(folder, file))
        if not _inside_root(target) or not os.path.isfile(target):
            return _not_found()
        return RedirectResponse("/render?" + urlencode({"path": target}), status_code=302)


class _Forward:
    """Forward to the inner app with a replayed body."""

    def __init__(self, inner, body: bytes):
        self.inner = inner
        self.body = body

    async def __call__(self, scope, receive, send):
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}

        await self.inner(scope, replay, send)


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


async def _multipart_fields(scope, body: bytes) -> dict:
    """The text fields of a multipart body (``/api/fs/upload`` carries ``path``
    as a form field). Parsed with Starlette's own parser off a replayed body."""
    async def replay():
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        form = await Request(scope, replay).form()
    except Exception:  # noqa: BLE001 — a body we cannot parse is out of scope
        return {"path": "\0"}
    return {k: v for k, v in form.items() if isinstance(v, str)}


# ---- the app grid -----------------------------------------------------------


def _entry_for(folder: str) -> str | None:
    from fused_render.app_listing import app_entry
    try:
        entry = app_entry(folder)
    except Exception:  # noqa: BLE001
        return None
    return os.path.basename(entry) if entry else None


def lan_apps() -> list[dict]:
    """Every app the /apps hub lists, in the hub's order: the workspace listing
    (``_workspace_apps`` — every tag folder under ``~/Fused``, with ``opened_at``
    from the recents store and ``updated_at`` from the folder) plus the linked
    external folders (``registered_apps``), sorted by the hub's rule (frontend
    ``sortApps``: ``opened_at ?? updated_at ?? 0`` newest first, then title,
    then name). Exported ``.fused`` files are not here: they are archives, not
    folders a page can render from. Same source and same rule as the hub so the
    two grids cannot drift. Read each time — a phone opens the grid rarely."""
    from fused_render.server.routers.apps import _workspace_apps

    rows = []
    try:
        apps = list(_workspace_apps())
    except Exception:  # noqa: BLE001 — an unreadable workspace is an empty grid
        logger.warning("lan: workspace listing failed", exc_info=True)
        apps = []
    try:
        from fused_render import registered_apps

        apps.extend(registered_apps.registered_apps())
    except Exception:  # noqa: BLE001
        logger.warning("lan: registered apps unreadable", exc_info=True)
    for app in apps:
        folder = app.get("path") or ""
        if not folder or not _inside_root(folder):
            continue
        entry_path = app.get("entry") or app.get("entry_html")
        if not entry_path:
            continue
        opened, updated = app.get("opened_at"), app.get("updated_at")
        recency = opened if opened is not None else (updated if updated is not None else 0)
        rows.append({
            "name": app.get("name") or os.path.basename(folder),
            "title": app.get("title"),
            "tag": app.get("tag"),
            "path": folder,
            # A linked app lives outside the workspace (registered_apps).
            "linked": not folder.startswith(os.path.realpath(fused_dir()) + os.sep),
            "recency": recency,
            "url": "/render?" + urlencode({"path": entry_path}),
            # The author's preview.png (app_dict resolves it), full-bleed on the
            # tile; falls back to icon.svg, then to the monogram, on the page.
            "preview": ("/api/fs/raw?" + urlencode({"path": app["preview_image"]})
                        if app.get("preview_image") else None),
            # The SVG bytes themselves via /api/fs/raw (an <img> subresource,
            # so the route's document-load downgrade does not apply).
            "icon": ("/api/fs/raw?" + urlencode({"path": os.path.join(folder, "icon.svg")})
                     if os.path.isfile(os.path.join(folder, "icon.svg")) else None),
        })
    rows.sort(key=lambda r: (-r["recency"], (r["title"] or r["name"]).lower(), r["name"].lower()))
    return rows


# ---- the listener + mDNS ----------------------------------------------------


def lan_ip() -> str | None:
    """The address other devices reach us on: the source address of a UDP
    socket "connected" to a private-range target (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


class _Controller:
    def __init__(self):
        self._lock = threading.Lock()
        self._inner = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._zeroconf = None
        self._infos: list = []
        self._ip: str | None = None
        self.port: int | None = None
        self.error: str | None = None
        # The https listener (lan_tls.py) beside the http one.
        self._tls_server = None
        self._tls_thread: threading.Thread | None = None
        self._tls_ip: str | None = None
        self.tls_port: int | None = None
        self.tls_error: str | None = None

    # -- wiring
    def attach(self, inner) -> None:
        self._inner = inner

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.port is not None

    @property
    def tls_running(self) -> bool:
        return self._tls_thread is not None and self._tls_thread.is_alive() and self.tls_port is not None

    def url(self) -> str | None:
        if not self.running:
            return None
        return f"http://{HOSTNAME}/" if self.port == 80 else f"http://{HOSTNAME}:{self.port}/"

    def https_url(self) -> str | None:
        if not self.tls_running:
            return None
        return f"https://{HOSTNAME}/" if self.tls_port == 443 else f"https://{HOSTNAME}:{self.tls_port}/"

    def status(self) -> dict:
        ip = lan_ip()
        if self.running and ip and ip != self._ip:
            # Wi-Fi changed under us: re-advertise the new address, and reissue
            # the certificate (it names the address) on a fresh https listener.
            # Under the controller lock: this runs on whatever thread asks for
            # prefs, and must not race apply()'s start/stop of the same
            # zeroconf and tls state.
            with self._lock:
                if self.running and ip != self._ip:
                    self._advertise(ip)
                    if ip != self._tls_ip:
                        self._stop_tls()
                        self._start_tls(ip)
        from fused_render import lan_tls

        try:
            fingerprint = lan_tls.ca_fingerprint() if self.running else None
        except Exception:  # noqa: BLE001
            fingerprint = None
        return {
            "running": self.running,
            "url": self.url(),
            "host": HOSTNAME,
            "alias": ALIAS_HOSTNAME,
            "ip": ip,
            "port": self.port,
            "error": self.error,
            "https_url": self.https_url(),
            "https_port": self.tls_port,
            "tls_error": self.tls_error,
            "ca_fingerprint": fingerprint,
            "devices": list_devices(),
        }

    def _stop_tls(self) -> None:
        server, thread = self._tls_server, self._tls_thread
        self._tls_server = self._tls_thread = None
        self.tls_port = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5.0)

    # -- start/stop
    def apply(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self.running:
                self._start()
            elif not enabled and self.running:
                self._stop()

    def _start(self) -> None:
        import uvicorn

        self.error = None
        if self._inner is None:
            self.error = "server not attached"
            return
        sock = None
        for port in PORT_CANDIDATES:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
                sock.listen(128)
                self.port = port
                break
            except OSError as e:
                sock.close()
                sock = None
                logger.info("lan: port %s unavailable (%s)", port, e)
        if sock is None:
            self.error = "no free port among " + ", ".join(map(str, PORT_CANDIDATES))
            self.port = None
            return
        config = uvicorn.Config(LanApp(self._inner), host="0.0.0.0", port=self.port,
                                log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                  daemon=True, name="fused-lan")
        thread.start()
        self._server, self._thread = server, thread
        ip = lan_ip()
        # https first: the mDNS record names its port.
        self._start_tls(ip)
        if ip:
            self._advertise(ip)
        else:
            self.error = "no network address found"
        logger.info("lan: sharing at %s / %s (%s)", self.url(), self.https_url(), ip)

    def _start_tls(self, ip: str | None) -> None:
        """The https listener for the native shell (lan_tls.py). Best-effort:
        a failure here leaves http working and is reported in `tls_error`."""
        import uvicorn

        from fused_render import lan_tls

        self.tls_error = None
        try:
            cert, key = lan_tls.ensure_server_cert([HOSTNAME, ALIAS_HOSTNAME], [ip] if ip else [])
            self._tls_ip = ip
        except Exception as e:  # noqa: BLE001
            self.tls_error = f"certificate: {e}"
            logger.warning("lan: tls certificate failed: %s", e)
            return
        sock = None
        for port in TLS_PORT_CANDIDATES:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
                sock.listen(128)
                self.tls_port = port
                break
            except OSError as e:
                sock.close()
                sock = None
                logger.info("lan: tls port %s unavailable (%s)", port, e)
        if sock is None:
            self.tls_error = "no free https port among " + ", ".join(map(str, TLS_PORT_CANDIDATES))
            self.tls_port = None
            return
        config = uvicorn.Config(LanApp(self._inner), host="0.0.0.0", port=self.tls_port,
                                log_level="warning", lifespan="on",
                                ssl_certfile=cert, ssl_keyfile=key)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                  daemon=True, name="fused-lan-tls")
        thread.start()
        self._tls_server, self._tls_thread = server, thread

    def _stop(self) -> None:
        self._unadvertise()
        server, thread = self._server, self._thread
        tls_server, tls_thread = self._tls_server, self._tls_thread
        self._server = self._thread = self._tls_server = self._tls_thread = None
        self.port = self.tls_port = None
        for s in (server, tls_server):
            if s is not None:
                s.should_exit = True
        for t in (thread, tls_thread):
            if t is not None:
                t.join(timeout=5.0)
        logger.info("lan: sharing stopped")

    # -- mDNS
    def _advertise(self, ip: str) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            self.error = "zeroconf not installed"
            return
        self._unadvertise()
        try:
            zc = Zeroconf()
            infos = []
            for host in (HOSTNAME, ALIAS_HOSTNAME):
                info = ServiceInfo(
                    "_http._tcp.local.",
                    f"Fused Render ({host})._http._tcp.local.",
                    addresses=[socket.inet_aton(ip)],
                    port=self.port or 80,
                    server=host + ".",
                    # `https`: the port the native shell should prefer.
                    properties={"path": "/", **({"https": str(self.tls_port)} if self.tls_port else {})},
                )
                zc.register_service(info)
                infos.append(info)
            self._zeroconf, self._infos, self._ip = zc, infos, ip
            # Advertising is the only thing that sets `error` while the
            # listener runs ("no network address found", a failed attempt) —
            # a later success, e.g. status() re-advertising once Wi-Fi is
            # back, is the recovery and must clear it.
            self.error = None
        except Exception as e:  # noqa: BLE001 — advertising is best-effort
            logger.warning("lan: mDNS advertise failed: %s", e)
            self.error = f"mDNS advertise failed: {e}"

    def _unadvertise(self) -> None:
        zc, infos = self._zeroconf, self._infos
        self._zeroconf, self._infos, self._ip = None, [], None
        if zc is None:
            return
        try:
            for info in infos:
                zc.unregister_service(info)
            zc.close()
        except Exception:  # noqa: BLE001
            pass


_controller = _Controller()


def attach(app) -> None:
    """Hand the inner FastAPI app over (once, at either entry point)."""
    _controller.attach(app)


def apply(enabled: bool) -> None:
    """Start or stop sharing to match the preference. Idempotent."""
    _controller.apply(enabled)


def status() -> dict:
    return _controller.status()


# ---- the desktop's own API (loopback; the LAN wrapper never forwards these) --

from fastapi import APIRouter, Header  # noqa: E402

router = APIRouter()


def _require_fused(x_fused: str | None):
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


@router.get("/api/lan/pair-token")
def api_lan_pair_token():
    """Mint a one-time pairing token and the URL to put in the QR code."""
    from fused_render import lan_tls

    token = mint_pair_token()
    base = _controller.url()
    # The QR is an http URL so a browser can use it as-is. Two extra params
    # ride along for the native shell: `ca`, the private CA's fingerprint, and
    # `s`, the https port — it fetches /lan/ca.pem, checks the fingerprint, pins
    # the CA, and pairs over https instead. Browsers ignore both.
    params = {"t": token}
    if _controller.tls_running:
        try:
            params["ca"] = lan_tls.ca_fingerprint()
            params["s"] = str(_controller.tls_port)
        except Exception:  # noqa: BLE001 — https stays optional
            pass
    url = (base or f"http://{HOSTNAME}/") + "pair?" + urlencode(params)
    ip = _controller._ip or lan_ip()
    port = _controller.port
    ip_url = None
    if ip:
        ip_url = f"http://{ip}{'' if port in (None, 80) else ':' + str(port)}/pair?" + urlencode(params)
    return {"url": url, "ip_url": ip_url, "ttl_s": PAIR_TOKEN_TTL_S,
            "https_url": _controller.https_url(), "ca_fingerprint": params.get("ca")}


@router.get("/api/lan/devices")
def api_lan_devices():
    return {"devices": list_devices()}


@router.delete("/api/lan/devices/{device_id}")
def api_lan_device_revoke(device_id: str, x_fused: str | None = Header(default=None)):
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    if not revoke_device(device_id):
        return JSONResponse({"error": "no such device"}, status_code=404)
    return {"devices": list_devices()}


@router.delete("/api/lan/devices")
def api_lan_devices_revoke_all(x_fused: str | None = Header(default=None)):
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    revoke_all_devices()
    return {"devices": []}


def start_if_enabled() -> None:
    """Entry-point hook: honour the stored preference at boot, off the
    startup path (mDNS + a bind can take a moment)."""
    from fused_render.shell import prefs

    if prefs.lan_enabled():
        threading.Thread(target=lambda: apply(True), daemon=True, name="fused-lan-boot").start()
