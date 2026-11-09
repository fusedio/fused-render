"""Share your apps with the Fused Render app on a phone on the same Wi-Fi.

Off by default (the ``lan_enabled`` preference, shell/prefs.py). When on, the
server opens ONE https listener on every interface, and Preferences shows a
QR code naming this computer's own local name::

    https://<LocalHostName>.local:1889/pair?t=<token>&fp=<cert sha256>&ip=<lan ip>

The name is the one macOS (mDNSResponder) and Linux (avahi) already
advertise for the computer: it survives sleep, wake, Wi-Fi changes and a
new DHCP address without a line of code here, and the phone resolves it
natively. fused-render advertises nothing itself. ``ip`` rides along so the
app has an address to fall back to on a network that filters mDNS, and
``fp`` is the SHA-256 of the one self-signed certificate (lan_tls.py) the
app pins — the certificate never has to name an address, so it is never
reissued, and nothing is fetched before pairing.

https only: WebKit withholds the camera, the microphone and the clipboard
from a page that is not a secure context, and those must work in the app.
A phone browser cannot pin a certificate, so there is no http listener and
no browser support — the app is the client. (Earlier versions carried a
second http listener, two mDNS names registered through python-zeroconf, a
watcher thread re-announcing them after every interface flap, and a private
CA reissuing a leaf for every new address; see docs/LAN_SIMPLIFICATION.md
for what each of those cost and why none is needed.)

**The loopback server is untouched.** D3's one security freebie — bind
127.0.0.1 — stays exactly as it is for the desktop; this module never calls
``set_server_origin_env``/``write_server_json``, so runPython children keep
talking to loopback. The LAN listener serves ``LanApp``, an ASGI wrapper
around the same FastAPI app that admits ONLY what an app page needs, plus
the phone bots door ``/api/phone/bots`` (docs/bots.md §14: the bots' chat,
stamped with the paired device's id; never the desktop's ``/api/bots``), and
refuses everything else with 404 — the explorer, the shell, Claude sessions,
the file index, git, config writes, and any path outside ``allowed_roots``
(``~/Fused``, the linked folders, a short list of app-data leaves under the
state dir). Nothing on the LAN side can widen what the wrapper forwards
without editing the allowlist below.

Scoping rule: every path-bearing argument (``path``, ``base``, ``py``,
``html``, ``image``, ``images``, ``paths``) is resolved the way the inner
route resolves it (relative against the page named by ``html``/``base``),
realpath'd, and must land inside one of the roots. A page outside them — or
a page inside reaching outside — gets 404, not 403: from the phone's point
of view those files do not exist.

Who gets in: only devices that scanned the QR code in Preferences (see the
pairing section below). Unpaired requests get a how-to-pair page at ``/``
and 401 everywhere else; a Host allowlist (the local name and the
computer's own addresses) closes DNS rebinding. No PIN and no
approve-on-laptop dialog, by the owner's call — the QR is the whole UX.

What the computer can tell about reachability on its own (no address, a
VPN, the firewall, the name not resolving, a rename since pairing) is
lan_doctor.py; ``status()`` carries its ``warnings`` to Preferences.

Lifecycle: ``attach(app)`` at either entry point (cli.py / app.py) hands the
inner app over; ``apply(enabled)`` — called from the prefs PUT and once at
startup — starts or stops the listener on a daemon thread. Stopping is
``should_exit`` + a bounded join; the process quit ladder (D184) is
deliberately not entered from here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)

from fused_render import lan_doctor
from fused_render._platform import is_within
from fused_render.logs import uvicorn_log_config
from fused_render.shell.seed import fused_dir

logger = logging.getLogger("fused_render.lan")

#: The https listener's port. Fixed and high so no root is needed and the
#: fallbacks are only for a taken port; the app does not care that the URL
#: carries one.
PORT_CANDIDATES = (1889, 1890, 18890)

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
#: also holds the certificate's private key (``lan_tls/``), every Claude transcript,
#: logs, search indexes and gigabytes of venvs and git
#: branches — none of an app's business, and a paired phone that could read
#: ``lan_tls/server.key`` could impersonate the computer's https forever. So the
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
    # The chat's attachment folder (claude_agent/agent.py SHOTS): what a phone
    # uploads into a task chat (`/api/fs/upload`) and the pictures the
    # transcript draws back (`/api/fs/raw`). One bounded folder, pruned by its
    # owner; never the runs tree beside it.
    try:
        from fused_render.claude_agent.agent import SHOTS
        roots.append(SHOTS)
    except Exception:  # noqa: BLE001 — no chat module, no attachments
        logger.debug("lan: shots root unavailable", exc_info=True)
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


def _inside_root(path: str, roots: list[str] | None = None) -> bool:
    try:
        real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        roots = [os.path.realpath(r) for r in (allowed_roots() if roots is None else roots)]
    except (OSError, ValueError):
        return False
    return any(is_within(real, root, fs_root_ok=False) for root in roots)


_TASK_ROOTS_TTL_S = 5.0
_task_roots_cache: tuple[float, list[str]] | None = None


def task_roots() -> list[str]:
    """Every task's folder (`project`, and `target`'s folder), from the tasks
    listing the desktop's own Tasks page reads. The phone draws that page
    (D1468), and the page STATS each task's folder to pick its chat template
    and LISTS it for the peek — folders anywhere on the disk, not under
    ~/Fused. So these join the READ scope (`/api/fs/stat`, `/api/fs/list`,
    `/api/fs/raw`) and never the write scope: a phone reads what a task
    reads, and writes only where every LAN page could already write."""
    global _task_roots_cache
    import time

    now = time.monotonic()
    if _task_roots_cache is not None and now - _task_roots_cache[0] < _TASK_ROOTS_TTL_S:
        return _task_roots_cache[1]
    roots: list[str] = []
    try:
        from fused_render.server.routers.tasks import _listing
        rows, _gen = _listing()
        for row in rows:
            for key in ("project", "target"):
                value = row.get(key)
                if not isinstance(value, str) or not value:
                    continue
                folder = value if key == "project" or os.path.isdir(value) else os.path.dirname(value)
                if folder and folder not in roots:
                    roots.append(folder)
    except Exception:  # noqa: BLE001 — no listing, no task folders: the roots alone
        logger.debug("lan: task roots unavailable", exc_info=True)
    _task_roots_cache = (now, roots)
    return roots


def read_roots() -> list[str]:
    """The read scope: the write roots plus every task's folder."""
    return allowed_roots() + task_roots()


#: File routes whose only effect is a read: scoped to `read_roots()`.
_READ_ROUTES = frozenset({"/api/fs/raw", "/api/fs/stat", "/api/fs/list", "/api/fs/walk", "/api/fs/conditions",
                          "/api/fs/git-repo"})


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


def _args_in_scope(args: dict, roots: list[str] | None = None) -> bool:
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
            if resolved is None or not _inside_root(resolved, roots):
                return False
    return True


# ---- the allowlist ----------------------------------------------------------

#: Exact paths (method set) forwarded to the inner app after scoping. Anything
#: not here — and not under a prefix below — is a 404 on the LAN side.
_EXACT: dict[str, frozenset[str]] = {
    "/render": frozenset({"GET"}),
    "/api/run": frozenset({"POST"}),
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
#:
#: **`/v1` is deliberately NOT here, nor in `_EXACT`** (docs/
#: LOCAL_MODEL_GATEWAY.md §1): the local model gateway is for agent CLIs on
#: THIS machine holding a bearer key, not for a paired phone — a LAN client
#: must never be able to load a model and spend the Mac's memory and GPU
#: through it. `tests/test_v1_gateway.py` pins the absence.
_PREFIXES = ("/static/", "/template-assets/", "/template-shared/", "/api/engines/", "/api/jobs/")
#: THE PHONE DOOR (docs/bots.md §14, D1468). The iOS app renders the desktop's
#: own bots app and task chat (frontend/src/lan/main.tsx mounts `@apps/bots`
#: and the shell's Tasks page), so the routes it reaches are the DESKTOP'S OWN,
#: not a trimmed phone shape: one UI, one wire. What keeps a paired phone from
#: driving the Mac is this table, which fails closed — a route not matched
#: here (and not in `_EXACT`/`_PREFIXES`) is a 404 on the LAN. Every forward
#: is `_AsDevice`-stamped (`X-Fused-Device` from the pairing record, a
#: client-sent one dropped) so the desktop handlers can tell a phone from
#: the Mac without ever trusting the client for it.
#:
#: Not here on purpose (the owner's call, 2026-11-06): everything that drives
#: the Mac's screen, terminal, disk or worktrees — bots takeover / giveback /
#: popout / dock / wake / profile / reveal / package / export / clone, the
#: browsers, iMessage, builds, usage and workspace routes, `/api/bot-apps`;
#: the agent's snapshot_* / terminal_command / set_mode / app_state /
#: canvas_shot actions; `/api/tasks/erase`, `/api/claude/worktrees` create and
#: remove, `/api/capture/*`, desktop `/api/prefs` writes. A row is one line;
#: a reviewer reads a new line here as "the phone can now do this".
_DOOR_BOT_OPS = ("pause", "resume", "stop", "clear", "open", "settings", "routines", "skills", "seen",
                 "react", "flag", "attach", "goto", "nav", "tab", "tool", "viewport", "failures")
_DOOR_AGENT_ACTIONS = frozenset({
    "start", "send", "send_now", "poll", "cancel", "history", "cards", "commands", "decide", "defaults",
    "sessions", "shots_dir", "image_to_png", "task_background", "task_output", "task_stop", "tasks",
    "live_run", "live_host"})
#: A bot id — never one of the collection's own names under the same prefix
#: (`/api/bots/browsers` is not bot "browsers").
_BOT = r"(?!(?:browsers|imessage|builds|usage|roster|workspace|profiles|presets|pulse|setup)(?:/|$))[A-Za-z0-9_-]+"
#: (path pattern, methods, body gate) — the gate is ``(key, allowed values)``
#: read from the JSON body, the way ``/api/claude/agent`` multiplexes on
#: ``action``; None forwards the body unread.
_DOORS: tuple[tuple[re.Pattern, frozenset[str], tuple[str, frozenset[str]] | None], ...] = (
    (re.compile(r"^/api/bots$"), frozenset({"GET", "POST"}), None),
    (re.compile(r"^/api/bots/(pulse|presets|setup)$"), frozenset({"GET"}), None),
    (re.compile(rf"^/api/bots/{_BOT}$"), frozenset({"DELETE"}), None),
    (re.compile(rf"^/api/bots/{_BOT}/(shot|app_events|tools|failures)$"), frozenset({"GET"}), None),
    (re.compile(rf"^/api/bots/{_BOT}/steps/[A-Za-z0-9_.-]+$"), frozenset({"GET"}), None),
    (re.compile(rf"^/api/bots/{_BOT}/send$"), frozenset({"POST"}), None),
    (re.compile(rf"^/api/bots/{_BOT}/({'|'.join(_DOOR_BOT_OPS)})$"), frozenset({"POST"}), None),
    (re.compile(r"^/api/claude/agent$"), frozenset({"POST"}), ("action", _DOOR_AGENT_ACTIONS)),
    (re.compile(r"^/api/claude/artifacts$"), frozenset({"POST"}), ("action", frozenset({"list", "live"}))),
    (re.compile(r"^/api/claude/worktrees$"), frozenset({"POST"}), ("action", frozenset({"list"}))),
    (re.compile(r"^/api/claude/app-entry$"), frozenset({"POST"}), None),
    (re.compile(r"^/api/claude-sessions(/history|/recap|/defaults|/summaries|/liveness)?$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/tasks$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/tasks/(changes|pulse|scheduled|settings)$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/tasks/[^/]+/messages$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/tasks/(read|seen|rename|archive|unarchive|delete|settings|holds|ports|running|idle"
                r"|favourite|pr-status|queue/admit|queue/decide|queue/force|queue/skip|queue/event)$"),
     frozenset({"POST"}), None),
    (re.compile(r"^/api/phone/tasks/new-app$"), frozenset({"POST"}), None),
    (re.compile(r"^/api/schedule$"), frozenset({"GET", "POST"}), None),
    (re.compile(r"^/api/schedule/(cancel|resend|run-now|shot|restore|queue/cancel|events/ack)$"), frozenset({"POST"}), None),
    (re.compile(r"^/api/schedule/(queue|events)$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/drafts$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/drafts/(chat|task)/.+$"), frozenset({"PUT", "DELETE"}), None),
    (re.compile(r"^/api/ui-layout$"), frozenset({"GET", "PUT", "POST", "PATCH"}), None),
    (re.compile(r"^/api/prefs$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/recents$"), frozenset({"GET", "POST"}), None),
    # What the Tasks page and the chat read on load: the agent catalog, the
    # desk (a task opened on an app folder puts that app on the desk, the way
    # the Mac does), the capture capabilities the composer's mic asks about.
    (re.compile(r"^/api/agent-backends$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/current-apps$"), frozenset({"GET"}), None),
    (re.compile(r"^/api/current-apps/ensure$"), frozenset({"POST"}), None),
    (re.compile(r"^/api/capture$"), frozenset({"GET"}), None),
)


#: WHERE A PHONE MAY START AN AGENT. `start`/`send` on the agent route carry
#: the folder the agent runs in (`file`) and the folders it may read this turn
#: (`read_dirs`, a JSON array); a `POST /api/schedule` carries `target`. The
#: phone picks these from lists the Mac showed it, so each must lie in the
#: read scope (`read_roots()`: the workspace, the linked apps, SHOTS, every
#: task's folder) — a paired device never turns an agent loose on `~` or `/`.
#: The old phone door kept the same rule (`phone_tasks._known_folder`).
_SCOPED_AGENT_ACTIONS = frozenset({"start", "send"})


def _in_read_scope(value) -> bool:
    """``value`` (a folder, or a file whose folder counts) lies in the read
    scope. A miss retries once on fresh task roots: a task made a moment ago
    sits in the 5 s cache's blind spot."""
    global _task_roots_cache
    if not isinstance(value, str) or not value.strip():
        return False
    if _inside_root(value, read_roots()):
        return True
    _task_roots_cache = None
    return _inside_root(value, read_roots())


def _door_in_scope(path: str, method: str, args: dict) -> bool:
    """Whether the folders a door body names are in scope (see
    `_SCOPED_AGENT_ACTIONS`). Routes with no folder in their body are always
    in scope; a missing value is left to the inner route's own validation."""
    values: list = []
    if path == "/api/claude/agent":
        if args.get("action") not in _SCOPED_AGENT_ACTIONS:
            return True
        values.append(args.get("file"))
        raw = args.get("read_dirs")
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError:
                return False
        if isinstance(raw, list):
            values.extend(raw)
        elif raw not in (None, ""):
            return False
    elif path == "/api/schedule" and method == "POST":
        values.append(args.get("target"))
    return all(v in (None, "") or _in_read_scope(v) for v in values)


def _door_for(path: str, method: str):
    """The `_DOORS` row for ``path``/``method``, or None."""
    for pat, methods, gate in _DOORS:
        if pat.match(path) and method in methods:
            return pat, methods, gate
    return None


#: The lan page's own routes beside ``/``: the bots app (`/bots`, `/bots/<id>`,
#: its tasks / apps / preferences pages) and the tasks page (`/tasks`,
#: `/tasks/<key>` — a key is a session id or ``pending:<entry>``; the colon is
#: a plain path character). Both are the desktop's own React pages mounted by
#: lan.html (frontend/src/lan/main.tsx).
_BOTS_PAGE = re.compile(r"^/bots(?:/[^/]+){0,2}$")
_TASKS_PAGE = re.compile(r"^/tasks(?:/[^/]+)?$")


def _lan_page(path: str) -> bool:
    """Is ``path`` one of the lan page's own routes (the grid, bots, tasks)?"""
    return path in ("/", "/index.html") or bool(_BOTS_PAGE.match(path)) or bool(_TASKS_PAGE.match(path))


def _not_found() -> Response:
    return PlainTextResponse("not found", status_code=404)


# ---- pairing: which devices may use the listener ----------------------------
#
# QR only, by the owner's call: Preferences shows a QR of
# https://<name>.local:<port>/pair?t=<token>&fp=…&ip=…; the app scans it,
# loads /pair, which sets a long-lived device cookie and lands on the grid. No
# PIN, no approval dialog. The token is minted over LOOPBACK (the desktop's
# own API — a LAN peer cannot mint one), is good for five minutes and pairs
# ONE device: Preferences shows a fresh code the moment a pairing lands (it
# watches the device list) and every five minutes regardless. Single-use is
# the owner's call (2026-08-28): a code photographed off the screen must not
# pair a second device. The cookie is 256 bits of randomness; the store keeps
# its SHA-256 plus a name, so a stolen store file cannot forge a cookie and
# the Preferences list can say "iPhone, last seen 2h ago" and revoke it.
#
# The app copies the cookie value into its Keychain after /pair lands and
# sets it for whatever host it talks to next (the name, or the fallback
# address), so a renamed computer or an mDNS-less network never means pairing
# again. Nothing here cares which host the cookie arrives under: only the
# hash of the value is checked.
#
# What this does and does not cover: it answers "is this MY device", not "is
# this network safe". The cookie only ever travels over the pinned https
# listener (`Secure`), never in clear.

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


def _device_name(user_agent: str, declared: str = "") -> str:
    """The app sends its own name (``X-Fused-Device-Name``) on the /pair
    request; a bare user agent gets a plain kind."""
    declared = (declared or "").strip()
    if declared:
        return declared[:60]
    ua = user_agent or ""
    for kind in ("iPhone", "iPad", "Android", "Macintosh", "Windows", "Linux"):
        if kind in ua:
            return {"Macintosh": "Mac", "Windows": "Windows PC"}.get(kind, kind)
    return "Device"


def _pair_device(user_agent: str, declared_name: str = "") -> tuple[str, dict]:
    """Register a new device; returns (cookie secret, public record). The
    computer's local name at pairing time is kept so lan_doctor can say when
    the computer was renamed since."""
    import secrets
    import time

    secret = secrets.token_urlsafe(32)
    now = time.time()
    record = {
        "id": secrets.token_hex(6),
        "hash": _hash(secret),
        "name": _device_name(user_agent, declared_name),
        "paired_at": now,
        "last_seen": now,
        "hostname": lan_doctor.local_hostname(),
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
    """True when the request carries a cookie for a stored device (see
    ``_paired_record``)."""
    return _paired_record(scope) is not None


def _paired_record(scope) -> dict | None:
    """The stored device record (a copy) the request's cookie belongs to, else
    None. Bumps that device's last_seen at most every ten minutes (a write per
    request would churn the file on every asset). The phone bots door
    (``_AsDevice``) reads the record's ``id``."""
    import time

    secret = _cookie_secret(scope)
    if not secret:
        return None
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
                return dict(d)
    return None


def _host_ok(scope) -> bool:
    """Only this computer's own names and addresses may address the listener
    — a website in a LAN browser that rebinds its own DNS to us would
    otherwise arrive same-origin."""
    host = _header(scope, b"host").split(":")[0].strip().lower().rstrip(".")
    if not host:
        return False
    name = (_controller.hostname or "").lower().rstrip(".")
    if name and host in (name, name.removesuffix(".local")):
        return True
    # The addresses as last seen by status(), then a live read for an address
    # the computer only just got (a new lease, before any prefs read): the
    # read is cached for a few seconds in lan_doctor, so an unpaired peer
    # hammering the listener cannot turn it into a subprocess per request.
    return host in _controller.ips or host in lan_doctor.lan_ips()


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
    if _lan_page(scope["path"]):
        return _pair_page(
            "Scan to pair this phone",
            "On the computer, open Preferences → Phone access and scan the QR code "
            "with the Fused Render app.",
            "This phone is not paired yet.")
    return JSONResponse({"error": "not paired"}, status_code=401)


def _with_cookie(response: Response, secret: str) -> Response:
    # `Secure`: the listener is https only, and a cookie that could travel
    # over a plain http link inside a page would replay in cleartext.
    response.set_cookie(COOKIE, secret, max_age=COOKIE_MAX_AGE_S, httponly=True,
                        samesite="lax", path="/", secure=True)
    return response


def _handle_pair(scope) -> Response:
    """A live token pairs THIS client and lands it on the grid. SINGLE use
    (owner's call, 2026-08-28): one scan, one device; Preferences shows a fresh
    code the moment a pairing lands. A code that was already used, or has run
    out, gets the expired page."""
    query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    token = (query.get("t") or [""])[0]
    if not token or not _spend_pair_token(token):
        return _pair_page(
            "That code has expired",
            "Each code pairs one phone and lasts five minutes. Open Preferences → Phone "
            "access on the computer — it shows a fresh one — and scan again.",
            "Not paired.")
    secret, record = _pair_device(_header(scope, b"user-agent"), _header(scope, b"x-fused-device-name"))
    logger.info("lan: paired %s", record.get("name"))
    return _with_cookie(RedirectResponse("/", status_code=302), secret)


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
            # /api/terminal/{sid}/stream is deliberately NOT allowlisted here —
            # a paired LAN peer gets a 1008 close instead of a shell. This is a
            # UX/scope call, not a security barrier (/api/run already executes
            # arbitrary Python for any client that reaches the loopback
            # server), so the omission is intentional, not an oversight the
            # next socket added here should "fix".
            # /api/run/ws is refused too: POST /api/run is scoped per request
            # from its body (`_route`), and the socket would need the same
            # check per MESSAGE (a frame-inspecting receive wrapper). The
            # runtime falls back to the POST when this socket never opens, and
            # the 6-connection WebKit cap it exists for is a loopback-windows
            # problem, not a phone's.
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
        device = _paired_record(scope)
        if device is None:
            return _unauthorized(scope)

        if path == "/" or path == "/index.html" or (method == "GET" and _lan_page(path)):
            # The grid is a second Vite page (frontend/lan.html → shell-dist/
            # lan.html); its assets resolve under /static/shell-dist/, which the
            # /static/ prefix below already forwards. `/bots/*` and `/tasks/*`
            # are the same page: it mounts the desktop's bots app and Tasks
            # page (docs/bots.md §14).
            page = os.path.join(os.path.dirname(_STATIC_DIR), "shell-dist", "lan.html")
            if not os.path.isfile(page):
                return PlainTextResponse(
                    "phone grid not built (frontend/lan.html → shell-dist/lan.html); "
                    "run: cd frontend && npm run build", status_code=503)
            return FileResponse(page, headers={"Cache-Control": "no-store"})
        door = _door_for(path, method)
        if door is not None:
            # The phone door (`_DOORS`): the desktop's own routes, device
            # stamped. No dot segment: the inner app forwards the bots prefix
            # to the process holding the bots (bots/routes.forward_middleware),
            # whose client normalises the URL, so `/api/bots/x/../../bots/<id>/
            # takeover` would land on an op this table never admitted.
            if any(seg in (".", "..") for seg in path.split("/")):
                return _not_found()
            device_id = str(device.get("id") or "")
            gate = door[2]
            scoped = path in ("/api/claude/agent", "/api/schedule") and method == "POST"
            if gate is None and not scoped:
                return _AsDevice(self.inner, device_id)
            body = await _read_body(receive)
            try:
                args = json.loads(body) if body.strip() else {}
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                return _not_found()
            if gate is not None:
                # A string, then a member: a list or dict here is unhashable
                # and would raise out of the membership test as a 500.
                value = args.get(gate[0])
                if not isinstance(value, str) or value not in gate[1]:
                    logger.info("lan: refused %s %s %s=%r (not in the door's gate)", method, path, gate[0], value)
                    return _not_found()
            if scoped and not _door_in_scope(path, method, args):
                logger.info("lan: refused %s %s (a folder outside the read scope)", method, path)
                return _not_found()
            return _AsDevice(_Forward(self.inner, body), device_id)
        if path == "/api/lan/apps" and method == "GET":
            return JSONResponse({"apps": lan_apps(), "host": _controller.host_for_url()})
        if path.startswith("/a/"):
            return self._app_shortcut(path[3:])
        if any(path.startswith(p) for p in _PREFIXES):
            return None
        allowed = _EXACT.get(path)
        if allowed is None or method not in allowed:
            # Named in the log: a page that reaches for a route the door never
            # admitted is the first thing to look for when the phone shows a
            # blank where the Mac shows a pane.
            if path.startswith("/api/"):
                logger.info("lan: refused %s %s (not in _DOORS / _EXACT)", method, path)
            return _not_found()

        # EVERY value of a repeated key is scoped, not the last one: which
        # duplicate the inner route reads is its business, and a
        # `?path=<in-root>&path=/etc/passwd` must fail whichever it is.
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"),
                         keep_blank_values=True)
        if not _args_in_scope(query, read_roots() if path in _READ_ROUTES else None):
            logger.info("lan: refused %s %s (a path outside the roots)", method, path)
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
            logger.info("lan: refused %s %s (a path outside the roots)", method, path)
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


class _AsDevice:
    """Forward to the inner app with ``X-Fused-Device: <device id>`` set from
    the pairing record and any client-sent ``x-fused-device`` dropped, on a copy
    of the scope (the caller's stays as the request arrived)."""

    def __init__(self, inner, device_id: str):
        self.inner = inner
        self.device_id = device_id

    async def __call__(self, scope, receive, send):
        headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-fused-device"]
        headers.append((b"x-fused-device", self.device_id.encode("latin-1", "replace")))
        await self.inner({**scope, "headers": headers}, receive, send)


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
    from fused_render import app_listing
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
            "linked": not is_within(
                folder, os.path.realpath(fused_dir()), strict=True),
            "recency": recency,
            "url": "/render?" + urlencode({"path": entry_path}),
            # The author's preview.png (app_dict resolves it), full-bleed on the
            # tile; falls back to icon.svg, then to the monogram, on the page.
            "preview": ("/api/fs/raw?" + urlencode({"path": app["preview_image"]})
                        if app.get("preview_image") else None),
            # The icon bytes themselves via /api/fs/raw (an <img> subresource,
            # so the route's document-load downgrade does not apply). Same
            # precedence as every other surface (app_listing.ICON_NAMES): svg,
            # else png — the page clips a png to a rounded square.
            "icon": ("/api/fs/raw?" + urlencode({"path": icon["icon"]})
                     if (icon := app_listing.app_icon(folder)) else None),
        })
    rows.sort(key=lambda r: (-r["recency"], (r["title"] or r["name"]).lower(), r["name"].lower()))
    return rows


# ---- the listener ----------------------------------------------------------


class _Controller:
    def __init__(self):
        self._lock = threading.Lock()
        self._inner = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.error: str | None = None
        #: The name the QR carries (lan_doctor.local_hostname()), read when
        #: the listener starts; None when the OS advertises none.
        self.hostname: str | None = None
        #: The addresses `_host_ok` accepts beside the name, refreshed by
        #: status() so a new DHCP lease is admitted without a restart.
        self.ips: list[str] = []
        # The stored preference as last applied — see apply().
        self._want = False

    # -- wiring
    def attach(self, inner) -> None:
        self._inner = inner

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.port is not None

    def host_for_url(self) -> str | None:
        return self.hostname or lan_doctor.best_ip()

    def url(self) -> str | None:
        host = self.host_for_url()
        if not self.running or not host:
            return None
        return f"https://{host}:{self.port}/"

    def fingerprint(self) -> str | None:
        try:
            from fused_render import lan_tls

            return lan_tls.fingerprint()
        except ModuleNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            logger.warning("lan: certificate unavailable", exc_info=True)
            return None

    def status(self) -> dict:
        ips = lan_doctor.lan_ips()
        # The listener's thread is gone but the preference is still on
        # (INCIDENT 2026-09-07 — why it died is not known). `apply()` only
        # runs when the preference changes, so this read is the one place
        # that notices, and a stale `port` is what let a pairing code
        # advertise a dead listener.
        if self._want and self._thread is not None and not self._thread.is_alive():
            with self._lock:
                if self._want and self._thread is not None and not self._thread.is_alive():
                    logger.warning("lan: listener stopped on its own; restarting")
                    self._server = self._thread = None
                    self.port = None
                    self._start()
        if self.running:
            self.ips = ips
            if ips and self.error == "no network address found":
                self.error = None  # the computer joined a network since
            self.hostname = lan_doctor.local_hostname() or self.hostname
        fingerprint = self.fingerprint() if self.running else None
        devices = list_devices()
        ip = lan_doctor.best_ip() if ips else None
        try:
            warnings = lan_doctor.warnings(running=self.running, port=self.port, ip=ip,
                                           fingerprint=fingerprint, hostname=self.hostname,
                                           devices=_read_devices())
        except Exception:  # noqa: BLE001 — diagnostics never break prefs
            logger.warning("lan: doctor failed", exc_info=True)
            warnings = []
        return {
            "running": self.running,
            "url": self.url(),
            "hostname": self.hostname,
            "ip": ip,
            "port": self.port,
            "error": self.error,
            "fingerprint": fingerprint,
            "devices": devices,
            "warnings": warnings,
        }

    # -- start/stop
    def apply(self, enabled: bool) -> None:
        with self._lock:
            # What the preference asks for, so status()'s restart of a dead
            # listener cannot resurrect sharing the user switched off.
            self._want = enabled
            if enabled and not self.running:
                self._start()
            elif not enabled and self._thread is not None:
                self._stop()

    def _start(self) -> None:
        import uvicorn

        self.error = None
        if self._inner is None:
            self.error = "server not attached"
            return
        try:
            from fused_render import lan_tls

            lan_tls.ensure_cert()
            cert, key = lan_tls.cert_path(), lan_tls.key_path()
        except ModuleNotFoundError:
            self.error = ("LAN sharing needs the optional cryptography dependency "
                          "([bundled]/[fused]); not available on this install.")
            return
        except Exception as e:  # noqa: BLE001
            self.error = f"certificate: {e}"
            logger.warning("lan: certificate failed: %s", e)
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
        # The context is built here, not by uvicorn from `ssl.SSLContext`,
        # which `_system_trust` replaces with a client-side wrapper whose
        # server handshake fails on macOS (D1442). uvicorn rejects a stdlib
        # context from `ssl_context_factory` (its isinstance check sees the
        # wrapper), so load the config without TLS and attach it afterwards.
        # `log_config` routes uvicorn's records into the app log (logs.py).
        config = uvicorn.Config(LanApp(self._inner), host="0.0.0.0", port=self.port,
                                log_level="warning", lifespan="on",
                                log_config=uvicorn_log_config())
        config.load()
        config.ssl = lan_tls.server_ssl_context(
            cert, key, getattr(config.http_protocol_class, "alpn_protocols", None))
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                  daemon=True, name="fused-lan")
        thread.start()
        self._server, self._thread = server, thread
        self.hostname = lan_doctor.local_hostname()
        self.ips = lan_doctor.lan_ips()
        if not self.ips:
            self.error = "no network address found"
        logger.info("lan: sharing at %s (%s)", self.url(), ", ".join(self.ips))

    def _stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self.port = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5.0)
        logger.info("lan: sharing stopped")


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
    """Mint a one-time pairing token and the URL to put in the QR code.

    NEVER a made-up base (INCIDENT 2026-09-07: a code naming a listener whose
    thread had gone pointed the phone at a port nothing answered on). The
    URL names the computer's local name when the OS has one, else its LAN
    address; `ip` rides along either way so the app can fall back to it."""
    base = _controller.url()
    fingerprint = _controller.fingerprint() if base else None
    if not base or not fingerprint:
        return JSONResponse(
            {"error": _controller.error or "local-network sharing is not running"},
            status_code=503)
    token = mint_pair_token()
    params = {"t": token, "fp": fingerprint}
    ip = lan_doctor.best_ip()
    if ip and _controller.hostname:
        params["ip"] = ip
    return {"url": base + "pair?" + urlencode(params), "ttl_s": PAIR_TOKEN_TTL_S,
            "fingerprint": fingerprint, "hostname": _controller.hostname, "ip": ip}


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
    startup path (a bind and a certificate can take a moment)."""
    from fused_render.shell import prefs

    if prefs.lan_enabled():
        threading.Thread(target=lambda: apply(True), daemon=True, name="fused-lan-boot").start()
