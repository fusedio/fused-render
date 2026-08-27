"""Share the apps in ``~/Fused/local`` with phones on the same Wi-Fi.

Off by default (the ``lan_enabled`` preference, shell/prefs.py). When on, the
server opens a SECOND listener on every interface and advertises it over
mDNS as ``render.fused.local`` (alias ``render.local``), so a phone on the
same network opens ``http://render.fused.local/`` and lands on a grid of the
apps in ``~/Fused/local``. Plain http on purpose: a trusted certificate needs
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

No pairing/PIN in this cut: anyone on the Wi-Fi can open and run these apps
while the switch is on, and the Preferences copy says so. PIN pairing is the
named follow-up.

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

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "lan")


def local_root() -> str:
    """``~/Fused/local`` — the ONLY folder the LAN side may see."""
    return os.path.join(fused_dir(), "local")


# ---- path scoping -----------------------------------------------------------


def allowed_roots() -> list[str]:
    """The folders the LAN side may reach: the apps themselves, and the app's
    own state dir (``~/.fused-render`` — where apps keep their per-install data
    beside prefs/bookmarks; ``FUSED_RENDER_HOME`` moves it, so both the default
    and the effective dir are listed)."""
    from fused_render.shell.storage import home_dir

    roots = [local_root(), os.path.expanduser("~/.fused-render"), home_dir()]
    seen: list[str] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
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


#: Body/query keys that name a file. Lists (``images``, ``paths``) are
#: checked element-wise. ``html``/``base`` are anchors for relative values
#: and are themselves scoped.
_PATH_KEYS = ("path", "py", "html", "base", "image", "images", "paths")


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
    "/api/fs/raw": frozenset({"GET", "HEAD"}),
    "/api/fs/stat": frozenset({"GET"}),
    "/api/fs/write": frozenset({"POST"}),
    "/api/fs/mkdir": frozenset({"POST"}),
    "/api/fs/upload": frozenset({"POST"}),
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

        if path == "/" or path == "/index.html":
            return FileResponse(os.path.join(_STATIC_DIR, "index.html"),
                                headers={"Cache-Control": "no-store"})
        if path == "/api/lan/apps" and method == "GET":
            return JSONResponse({"apps": lan_apps(), "host": HOSTNAME})
        if path.startswith("/a/"):
            return self._app_shortcut(path[3:])

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
        folder = os.path.join(local_root(), parts[0])
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
    """The apps under ``~/Fused/local`` as the grid shows them, in the /apps
    hub's order: the hub's own listing (``_workspace_apps`` — entry, title,
    ``opened_at`` from the recents store, ``updated_at`` from the folder)
    filtered to the local root, sorted by the hub's rule (frontend
    ``sortApps``: ``opened_at ?? updated_at ?? 0`` newest first, then title,
    then name). Same source and same rule so the two grids cannot drift. Read
    each time — a phone opens the grid rarely."""
    from fused_render.server.routers.apps import _workspace_apps

    root = os.path.realpath(local_root())
    rows = []
    try:
        apps = _workspace_apps()
    except Exception:  # noqa: BLE001 — an unreadable workspace is an empty grid
        logger.warning("lan: workspace listing failed", exc_info=True)
        return rows
    for app in apps:
        folder = app.get("path") or ""
        if not (folder == root or folder.startswith(root + os.sep)):
            continue
        entry_path = app.get("entry") or app.get("entry_html")
        if not entry_path:
            continue
        opened, updated = app.get("opened_at"), app.get("updated_at")
        recency = opened if opened is not None else (updated if updated is not None else 0)
        rows.append({
            "name": app.get("name") or os.path.basename(folder),
            "title": app.get("title"),
            "recency": recency,
            "url": "/render?" + urlencode({"path": entry_path}),
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

    # -- wiring
    def attach(self, inner) -> None:
        self._inner = inner

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.port is not None

    def url(self) -> str | None:
        if not self.running:
            return None
        return f"http://{HOSTNAME}/" if self.port == 80 else f"http://{HOSTNAME}:{self.port}/"

    def status(self) -> dict:
        ip = lan_ip()
        if self.running and ip and ip != self._ip:
            # Wi-Fi changed under us: re-advertise the new address.
            self._advertise(ip)
        return {
            "running": self.running,
            "url": self.url(),
            "host": HOSTNAME,
            "alias": ALIAS_HOSTNAME,
            "ip": ip,
            "port": self.port,
            "error": self.error,
        }

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
        if ip:
            self._advertise(ip)
        else:
            self.error = "no network address found"
        logger.info("lan: sharing ~/Fused/local at %s (%s:%s)", self.url(), ip, self.port)

    def _stop(self) -> None:
        self._unadvertise()
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self.port = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5.0)
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
                    properties={"path": "/"},
                )
                zc.register_service(info)
                infos.append(info)
            self._zeroconf, self._infos, self._ip = zc, infos, ip
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


def start_if_enabled() -> None:
    """Entry-point hook: honour the stored preference at boot, off the
    startup path (mDNS + a bind can take a moment)."""
    from fused_render.shell import prefs

    if prefs.lan_enabled():
        threading.Thread(target=lambda: apply(True), daemon=True, name="fused-lan-boot").start()
