"""GET/PUT /api/prefs — user preferences at ~/.fused-render/prefs.json.

The Preferences page's backend (SPEC §20): a tiny persisted preference store
(shell/storage, beside bookmarks.json/deployments.json) plus the derived,
read-only facts the page shows next to it (engine availability/forcing).

The app's own log is deliberately NOT in this payload: it is disposable
temp-dir output (D68) whose only affordance was a reveal button, and the
desktop tray's "Open app logs" already covers that. Since the call store moved to
~/.fused-render/logs, a second "Logs" heading here read as the call log's
settings rather than as a separate thing.

**engines** is persisted too — the per-capability INFERENCE engine choice
(D302), which is a different thing from the execution engine below however
similar the word is: it says which local-model backend serves a capability when
more than one can, and `"auto"` (the default, and the absence of a key) means
the registry's own ordering decides. See ``inference_engines``; the resolution,
including what happens to a preference this machine cannot honour, belongs to
``ai/registry.py``.

Five more preferences are persisted: **reader_enabled** (whether the Reader
listen-to-files accessibility mode is offered — opt-in, default off; see
``reader_enabled``), **canvases_enabled** (whether the Canvases feature is
offered at all — opt-in, default off; see ``canvases_enabled``),
**app_sharing_enabled** (whether the unified Share sheet replaces the plain
Export action — opt-in, default off; see ``app_sharing_enabled``),
**live_previews_enabled** (whether card thumbnails may render the live app
in a scaled iframe — opt-in, default off; see ``live_previews_enabled``),
**monitor_enabled** (whether the process Monitor — status-bar System chip
and /monitor page — is offered; opt-in, default off; see ``monitor_enabled``),
**marketplace_enabled** (whether the Marketplace — sidebar row and the
/marketplace pages — is offered; opt-in, default off; see
``marketplace_enabled``),
**github_link_enabled** (whether the Share sheet offers a GitHub install link
for an app whose folder is its own pushed GitHub repo — opt-in, default off;
see ``github_link_enabled``),
**indexing_enabled** (whether
background file-index scanning may run — default ON, see ``indexing_enabled``),
**chromium_windows_enabled** (whether the macOS app's NEW windows are hosted
by the on-demand Chromium engine instead of WKWebView — experimental, opt-in,
default off; see ``chromium_windows_enabled`` and `fused_render.chromium`),
and the **execution engine** for /api/run:

  * ``"fused"`` (default, D204) — the fused local compute backend (engine.py):
    a folder's ``pyproject.toml`` dependencies resolved into cached venvs,
    ``@fused.udf``/``result`` entrypoints. Effective only while the ``fused``
    package is importable (``fused_engine_available``); otherwise execution
    falls back to builtin and the page says so, which is what keeps a default
    that depends on the environment from being a default that breaks in one.
  * ``"builtin"`` — the built-in executor: fresh subprocess per call, the
    environment that launched the server. Storing it PINS it: D204 flipped the
    default, not the choice, and an importable ``fused`` must not override a
    user who picked builtin — that is precisely D70's surprise.

D204 knowingly re-accepts the install-order-dependent default D70 and D80 both
rejected: the same install can run pages under two contracts depending on what
else shares the environment. The owner's call (2026-08-03) is that the fused
engine's UX has improved enough, and both engines run locally, that the contract
difference is now a much smaller surprise than it was in July.

The preference is read per request (server.py's /api/run dispatch), so a
switch applies to the next run with no restart — the same no-restart
discipline as the template registries (CT-5). The ``FUSED_RENDER_ENGINE``
environment variable stays the *process-level* override: when set it wins
over the pref entirely (server.py validates it at startup), and the page
shows the pref as locked.

No import of server.py (server includes this router — keep it acyclic); the
X-Fused guard and the small env-var effective-engine mirror are duplicated
locally like shell/bookmarks.py's guard is.
"""
import os
import re
import threading

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import _flavor
from fused_render.shell import storage

router = APIRouter()

VALID_ENGINES = ("builtin", "fused")
#: What a capability's INFERENCE engine preference says when nobody has chosen:
#: let the registry's table order decide. Spelled here as well as in
#: `ai.registry.AUTO` rather than imported, because this module is imported by
#: `calls.py` on a hot path and must not drag the AI registry in behind it —
#: the registry reaches for prefs, not the other way round. The two are pinned
#: equal by `test_shell_prefs.py`, since a drift would read as a preference for
#: a runner named "auto" and be dropped as unknown.
AUTO_ENGINE = "auto"
VALID_CALLS_PARAMS = ("full", "keys", "off")
# The Claude model SHORT NAMES a session or task may be started with — not API
# model ids — with `""` a first-class member meaning "no flag, keep the
# session's own default". The claude template's own selector list (frontend/
# src/apps/claude composer MODELS) and the CLI's aliases. Once also the value
# set of a `default_model` preference, retired with its Preferences control:
# the global model is `model` in ~/.claude/settings.json (server/ai.py
# `default_model`). The relay wants a full API id, so the short→id mapping
# lives THERE, in one place, next to the caller that needs it.
VALID_DEFAULT_MODELS = ("", "fable", "opus", "sonnet", "haiku")
DEFAULT_CALLS_RETENTION_DAYS = 14
#: How long a resident local model may sit idle before the reaper unloads it
#: (SPEC AI-13). Minutes, not seconds — a sensible window is measured in
#: minutes and a seconds control invites off-by-1000 mistakes. `0` disables
#: the reaper entirely, same "0 = off" shape as `calls_retention_days`.
DEFAULT_AI_IDLE_UNLOAD_MINUTES = 5
#: The env var a *set, parsable* value of which overrides the stored pref —
#: same precedence as `FUSED_RENDER_CALLS_RETENTION_DAYS`, so a machine-level
#: policy (a shared workstation someone wants to keep more aggressive than
#: whatever a user dials in) can win without touching prefs.json. Clamped to
#: a non-negative integer, same as the calls one, and deliberately no UPPER
#: clamp either: `=100000` is honoured as-is rather than silently capped at
#: 1440, which would make the override lie about what it is forcing.
AI_IDLE_MINUTES_ENV = "FUSED_RENDER_AI_IDLE_MINUTES"


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "prefs.json")


def read_prefs() -> dict:
    data = storage.read_json(_path())
    return data if isinstance(data, dict) else {}


# Serialises every read-modify-write of prefs.json in this process (`put_prefs`
# and `write_pref`). Re-entrant so a writer that reads through `read_prefs`
# under the lock does not deadlock on a future helper that takes it too.
_PREFS_LOCK = threading.RLock()


def write_pref(key: str, value) -> dict:
    """Store ONE preference and hand back the record as written — the same
    read-modify-write `put_prefs` does, for a door outside this router that
    owns one switch (`/api/tasks/queue` and `project_queue_enabled`). The
    caller has already checked the value; this only keeps the file whole, and
    `_PREFS_LOCK` keeps it whole against a `put_prefs` in flight."""
    with _PREFS_LOCK:
        prefs = read_prefs()
        prefs[key] = value
        storage.write_json(_path(), prefs)
        return prefs


def selected_engine() -> str:
    """The persisted engine preference; unset/unknown values read as **fused**.

    D204 flipped this from builtin. It is only the SELECTION —
    ``effective_engine()`` ANDs it with live availability, so "fused by default"
    can never mean "broken by default" on a machine without the package; and a
    stored ``"builtin"`` still pins builtin, because that is a choice the user
    made and not a default to override.
    """
    value = read_prefs().get("engine")
    return value if value in VALID_ENGINES else "fused"


def reader_enabled() -> bool:
    """Whether the Reader (listen-to-files) mode is offered (default off — opt-in).

    Reader is an accessibility affordance: a preview mode that reads text files
    and PDFs aloud. It's off until the user turns it on from the Preferences
    page, at which point the reader template's condition.py gate (SPEC CT-12)
    starts allowing the mode on the file types it's bound to. This is a global
    feature switch, not a per-file sniff — the gate ignores the target path and
    consults only this value. Any non-`true` stored value (missing/legacy)
    reads as off.
    """
    return read_prefs().get("reader_enabled") is True


def canvases_enabled() -> bool:
    """Whether the Canvases feature is offered at all (default off — opt-in, D427).

    Canvases is the legacy-workbench bridge: a Fused account, a listing of
    remote canvases, and a per-canvas workspace with an embedded live workbench.
    Most machines never open one, so it is off until the user turns it on from
    the Preferences page, at which point the shell starts OFFERING it — the
    sidebar row (once there is also an account behind it) and the Settings menu
    entry.

    A SWITCH OVER THE ENTRY POINTS, NOT A ROUTE GUARD: `/canvases` and
    `/canvases/<env>/<name>` keep answering while this is off, the same way the
    signed-out state has always left them reachable and let the page explain
    itself. A deep link, a bookmark, or a workspace someone still has open is
    not what a reader turning this off asked to lose; the nav entries for a
    feature they do not use is. Any non-`true` stored value (missing/legacy)
    reads as off, so an existing install — signed in or not — has to opt in.
    """
    return read_prefs().get("canvases_enabled") is True


def native_windows_enabled() -> bool:
    """Whether the macOS app shows the shell in its own native windows
    (mac_window.py) instead of browser tabs (default ON — opt-out).

    Same idiom as `indexing_enabled`: absence and any non-`false` stored
    value both read as ON, so a fresh install and one that never touched the
    checkbox get windows; only a stored `false` keeps the browser. The
    launcher (⌥Space) is NOT behind this switch — it stays on either way and
    simply opens its pick in a browser tab while this is off. Read by
    `app.py` at boot and applied live through
    `window_policy.native_hooks["apply"]` on a PUT.
    """
    return read_prefs().get("native_windows_enabled") is not False


def chromium_windows_enabled() -> bool:
    """Whether a NEW macOS app window is hosted by the Chromium (CEF) engine
    instead of WKWebView (experimental, opt-in, default off — same `is True`
    idiom as the other optional features).

    Read by `mac_window.WindowManager` at EVERY window open, not once at
    boot: flipping it changes what the next window gets, and open windows
    keep the engine they were born with. It only takes effect when the
    engine is on disk (`fused_render.chromium.status()["state"] == "ready"`);
    a PUT that turns it on starts the download, and until that lands new
    windows keep using WebKit. The dock, launcher, pinned popover and
    recording pill are never behind this switch.
    """
    return read_prefs().get("chromium_windows_enabled") is True


def app_sharing_enabled() -> bool:
    """Whether the unified Share sheet is offered (default off — opt-in).

    ON, every surface that lets a reader take an app out of this machine — the
    /apps card's hover chip and right-click menu, the app page header, the
    explorer kebab, the explorer folder row — shows ONE "Share" entry, and the
    sheet behind it holds both routes: a public link on the reader's Fused
    account (share_app.py) and the `.fused` file download (SPEC §43). OFF, the
    same five surfaces show the plain Export / Download action they carried
    before the sheet existed: the `.fused` file lands in Downloads and a toast
    says where. The link route is not reachable at all while this is off — the
    sheet is the only thing that opens it, and nothing opens the sheet.

    A SWITCH OVER THE ENTRY POINTS, like `canvases_enabled`: the /api/share/*
    routes keep answering, so a script or a page that already talks to them is
    not what a reader turning this off asked to lose. Any non-`true` stored
    value (missing/legacy) reads as off, so every install has to opt in.
    """
    return read_prefs().get("app_sharing_enabled") is True


def live_previews_enabled() -> bool:
    """Whether card thumbnails may render the LIVE app (default off — opt-in).

    The /apps hub's cards (AppPreviewCard) and the explorer's bookmark, recent
    and folder cards (BookmarkCards) can show a scaled-down iframe of the page
    itself when there is no authored `preview.png` — and a still-thumbed app
    card swaps that iframe in on hover. Each one is a whole sandboxed page plus
    its JS runtime, so by default every thumbnail is a still or a plain
    placeholder mark and nothing boots on scroll or hover; a reader who wants
    the live grid turns it on here.

    Same idiom as `canvases_enabled`: only a stored `true` turns it on, so a
    preference file that predates this setting, or one that never touched the
    checkbox, reads as off. Read by the client once per page load and
    republished by the Preferences page on toggle — not consulted by any
    server route; the /render and embed URLs the thumbnails point at keep
    answering regardless.
    """
    return read_prefs().get("live_previews_enabled") is True


def monitor_enabled() -> bool:
    """Whether the process Monitor is offered (default off — opt-in).

    ON, the status bar carries the System chip (shell/SystemDock.tsx: this
    app's CPU and memory, a sparkline, the top processes, "Open Monitor") and
    the shell answers `/monitor` with the process monitor page
    (shell/monitor/MonitorPage.tsx). OFF, the chip is not rendered and
    `/monitor` shows a one-line notice pointing at Preferences, so a bookmark
    to it says what to do rather than opening a page the reader has not
    turned on.

    A SWITCH OVER THE ENTRY POINTS, like `app_sharing_enabled`: the
    /api/system/activity routes keep answering regardless, and the sampler
    costs nothing until something polls it. Any non-`true` stored value
    (missing/legacy) reads as off, so every install has to opt in.
    """
    return read_prefs().get("monitor_enabled") is True


def marketplace_enabled() -> bool:
    """Whether the Marketplace is offered (default off — opt-in).

    ON, the shell offers its entry points: a sidebar row and the `/marketplace`
    pages (browse, installed/updates, app detail) that install community apps
    from the fused-render-community-apps repo into `<workspace>/app/`.
    OFF, those entry points are hidden and `/marketplace` shows a notice
    pointing at Preferences.

    A SWITCH OVER THE ENTRY POINTS, like `monitor_enabled`: the
    /api/marketplace routes (fused_render/marketplace.py) keep answering
    regardless. Any non-`true` stored value (missing/legacy) reads as off, so
    every install has to opt in.
    """
    return read_prefs().get("marketplace_enabled") is True


def github_link_enabled() -> bool:
    """Whether the Share sheet offers its GitHub link card (default off — opt-in).

    ON, the sheet shows a third card: a udf.ai link that installs the app from
    its GitHub repo, copyable once the folder is the repo root, has a GitHub
    remote, and has nothing unpushed (share_app.github_link_state). OFF, the
    card is not rendered. A switch over the entry point only: GET
    /api/share/github keeps answering. Any non-`true` stored value reads as off.
    """
    return read_prefs().get("github_link_enabled") is True


def task_peek_enabled() -> bool:
    """Whether the Tasks page opens a task in a SIDE PANEL beside the list
    instead of navigating to the Explorer — ALWAYS, since 2026-09-20 (Akshil:
    "remove the flag of ... sidebar peek"). The switch is gone from
    Preferences and the PUT no longer accepts `task_peek_enabled`; a stored
    value from an older build is ignored, so nobody is stranded on the
    navigate-away path with no way back. Kept as a function, and kept in the
    GET payload as `task_peek.enabled`, because the client's flag module still
    reads it — a server that sends nothing reads as ON there too.
    """
    return True


def project_peek_enabled() -> bool:
    """Whether the APP PAGE's Tasks tab (`/apps/<folder>?_tab=tasks`) opens a
    task in the same side panel `/tasks` does — ALWAYS, since 2026-09-21
    (Akshil: the flag came off once the surface settled). The switch is gone
    from Preferences and the PUT no longer accepts `project_peek_enabled`; a
    stored value from the flagged build is ignored. Kept as a function, and
    kept in the GET payload as `task_peek.project`, because the client's type
    still names it — a server that sends nothing reads as ON there too.
    """
    return True


def notify_terminal_sessions_enabled() -> bool:
    """Whether a finished-task notification fires for a session started from
    an INTERACTIVE TERMINAL (`claude` typed by hand) rather than only one
    started through fused-render's own Claude template (default off).

    Every `~/.claude/projects` transcript's `type: "user"` records carry an
    `entrypoint` — "cli" for an interactive terminal, "sdk-cli" for a
    headless/programmatic spawn (what claude_agent/agent.py produces).
    That is the whole signal there is, and it is a PROXY, not proof: an
    unrelated SDK-driven session also reports "sdk-cli", so this preference
    cannot be "only notify about fused-render's own sessions" — it can only
    be "also notify about the ones we're fairly sure are someone's own
    terminal". Default OFF because the reported bug was exactly a plain
    terminal session raising a fused-render notification unasked; a user who
    wants those back opts in explicitly.

    Same idiom as `project_queue_enabled` below: only a stored `true` is on,
    everything else (missing, legacy, junk) stays off. No env override — there
    is nothing a process needs to pin about a Preferences toggle.
    """
    return read_prefs().get("task_notify_terminal_sessions") is True


def project_queue_enabled() -> bool:
    """Whether one folder runs one task at a time — everything else queues
    (default off — opt-in while the queue is in beta).

    Same idiom as `canvases_enabled`: only a stored `true` is on, any other
    value (missing/legacy/junk) reads as off. Off, every
    path behaves exactly as it did before the queue existed — chat sends spawn,
    run-now runs, the scheduler holds per SESSION and not per folder, and the
    `queued` status never appears on a row.

    NO ENV OVERRIDE, deliberately: this is a gate in front of work that already
    runs, and a second env var nobody remembers setting is how a machine ends
    up serialising its tasks for a reason its owner cannot find.
    """
    return read_prefs().get("project_queue_enabled") is True


def task_recap_titles_enabled() -> bool:
    """Whether every finished Claude turn gets a short haiku recap that titles
    its Tasks row (default off — opt-in beta: it spends one haiku call per
    finished turn, chat open or not).

    Same idiom as `project_queue_enabled`: only a stored `true` is on. Off, the
    listing carries no `recap` field, nothing is queued for haiku, and the
    away fold's recap is never written to `session_settings.json` — the rows
    and the fold are main's, byte for byte.
    """
    return read_prefs().get("task_recap_titles_enabled") is True


def task_summary_titles_enabled() -> bool:
    """Whether Tasks rows are titled ONLY by the server's stable `title` (a
    user-set title, else Claude Code's `ai-title` summary, else the first
    line of the first message) — never by the newest message, the haiku
    recap or Claude's reply (default off; only a stored `true` is on).
    Rows then carry `title_mode: "summary"`; the key is absent when off."""
    return read_prefs().get("task_summary_titles_enabled") is True
# ---- moved off localStorage (D1386) -----------------------------------------
#
# The four below were browser localStorage keys until D1386. localStorage is
# per ORIGIN, and the desktop app walks ports 1777..1787 then an ephemeral one
# (shell/onboarding.py) — so a relaunch on another port is a new origin with
# empty storage and every one of these silently fell back to its default. The
# client imports its old key once (push here, then remove it) and reads them
# off `/api/prefs` from then on.

#: Tasks-page view tab ids: short lower-case identifiers (the client's
#: `TasksView` names). Unknown-but-well-formed ids are kept — the client drops
#: what it does not know, so a newer client's tab survives an older server.
_TASKS_VIEW_TAB_RE = re.compile(r"^[a-z_]{1,24}$")
MAX_TASKS_VIEW_TABS = 8


def bots_autostage() -> bool:
    """"Open the browser when a bot needs me": a bot-initiated hand-over on
    the selected bot opens the Bots page's Stage by itself (default ON — only
    a stored ``false`` turns it off). Was localStorage
    ``browser-bot.autostage``."""
    return read_prefs().get("bots_autostage") is not False


def bots_sounds() -> bool:
    """Whether a hand-over plays the Bots page's chime (default ON — only a
    stored ``false`` turns it off). Was localStorage ``browser-bot.sounds``."""
    return read_prefs().get("bots_sounds") is not False


def bots_clear_enabled() -> bool:
    """Whether the bot chat header shows its Clear context button (a feature
    flag, default OFF — only a stored ``true`` turns it on). Was localStorage
    ``browser-bot.clear``."""
    return read_prefs().get("bots_clear_enabled") is True


def tasks_view_tabs() -> list[str]:
    """The Tasks page's chosen view tabs, in order. ``[]`` — unset, empty or
    unreadable — means "the client's defaults"; never raises."""
    value = read_prefs().get("tasks_view_tabs")
    if not isinstance(value, list):
        return []
    tabs = [t for t in value if isinstance(t, str) and _TASKS_VIEW_TAB_RE.fullmatch(t)]
    return list(dict.fromkeys(tabs))[:MAX_TASKS_VIEW_TABS]


def claude_strict_mcp_enabled() -> bool:
    """Whether chat and Tasks-page Claude sessions start lean —
    `--strict-mcp-config`, so the user's own MCP servers and claude.ai
    connectors are not launched (about 220 MB less per session; enabled
    plugins' MCP servers are kept). Default ON: only a stored `false` turns it
    off. `claude_agent/agent.py::_strict_mcp_pref` reads the same key."""
    return read_prefs().get("claude_strict_mcp_enabled") is not False


VALID_CLAUDE_PERMISSIONS = ("plan", "prompt", "acceptEdits", "auto")


def claude_default_permission() -> str:
    """Approvals mode a new Claude chat starts in (`claude_default_permission`
    pref): plan | prompt | acceptEdits | auto, `"auto"` when unset or junk.
    `claude_agent/agent.py::_default_permission_pref` reads the same key."""
    value = read_prefs().get("claude_default_permission")
    return value if value in VALID_CLAUDE_PERMISSIONS else "auto"


def lan_enabled() -> bool:
    """Whether the user's apps (everything under ~/Fused plus linked folders)
    are shared with the local network (default off — opt-in). The switch
    fused_render/lan.py reads: on, a second listener on every interface serves
    those apps to phones on the same Wi-Fi as http://render.fused.local/. A
    feature flag, not a route guard: the loopback server is untouched either
    way."""
    return read_prefs().get("lan_enabled") is True


#: The three agent switches (D17, docs/COPILOT_BACKEND.md; the third from
#: docs/CODEX_BACKEND.md). Claude Code is on unless a stored `false` turns it
#: off; Copilot and Codex are off unless a stored `true` turns them on.
#: Asymmetric defaults on purpose — "Claude by default" is the owner's rule —
#: and `put_prefs` refuses the write that would clear all three.
#: `VALID_DEFAULT_BACKENDS` is in `registry.ORDER`: the PUT's default re-point
#: walks it and takes the first agent that is on.
AGENT_BACKEND_KEYS = {"claude": "claude_enabled", "copilot": "copilot_enabled",
                      "codex": "codex_enabled"}
VALID_DEFAULT_BACKENDS = ("claude", "copilot", "codex")


def backend_enabled(backend: str) -> bool:
    """Whether `backend` ("claude" | "copilot" | "codex") may start chats and
    tasks. Unknown backend reads as off. See
    `agent_backends.registry.enabled_ids` for the never-empty guarantee this
    feeds."""
    key = AGENT_BACKEND_KEYS.get(backend)
    if key is None:
        return False
    value = read_prefs().get(key)
    if backend == "claude":
        return value is not False
    return value is True


def claude_enabled() -> bool:
    return backend_enabled("claude")


def copilot_enabled() -> bool:
    return backend_enabled("copilot")


def codex_enabled() -> bool:
    return backend_enabled("codex")


def default_backend() -> str:
    """The agent new chats and tasks start from, as stored — `"claude"` when
    unset or junk. Whether it is ENABLED is the registry's question
    (`agent_backends.registry.default_id`), which re-points to an enabled one."""
    value = read_prefs().get("default_backend")
    return value if value in VALID_DEFAULT_BACKENDS else "claude"


def _agents_state() -> dict:
    """`{"claude": bool, "copilot": bool, "codex": bool, "default": id}` for
    `GET /api/prefs`. Imported lazily: the registry reads this module for the
    switches. The REGISTRY's answer, not the raw switches: a hand-edited
    prefs.json that turns all three off reads as Claude on there, and this
    page must agree."""
    from fused_render.agent_backends import registry

    return {"claude": registry.is_enabled("claude"),
            "copilot": registry.is_enabled("copilot"),
            "codex": registry.is_enabled("codex"),
            "default": registry.default_id()}


def inference_engines() -> dict:
    """The per-capability INFERENCE engine preferences, as stored (D302).

    A different thing entirely from ``selected_engine()`` above, which is about
    /api/run — this one says which local-model backend serves a capability when
    two can. `{capability: "auto" | runner code}`, and an absent capability
    means "auto", so the empty dict is the default and every machine starts
    there.

    **Returned raw, and validated at the point of USE rather than here.** A
    value naming a runner that cannot run on this machine is a legitimate thing
    to have stored — prefs.json travels between machines in a synced home
    directory — and `registry.resolve()` is what decides it cannot be honoured
    today. Correcting it here would silently rewrite a choice the user made on
    another machine and would make it un-restorable; the AI Models and
    Preferences pages instead show it as stored, with the reason it is not in
    force. Non-string values are dropped, because those cannot have come from
    this app and the one thing that must not happen is an arbitrary object
    reaching a resolution.
    """
    stored = read_prefs().get("engines")
    if not isinstance(stored, dict):
        return {}
    return {str(k): v for k, v in stored.items() if isinstance(v, str)}


def engine_for_capability(capability: str) -> str:
    """One capability's engine preference — `AUTO_ENGINE` when unset.

    `registry.preferred_code()` is the only caller, and calls it on every
    resolution: a preference is a file, and changing one must apply to the next
    load with no restart (the same no-restart discipline as the template
    registries, CT-5).
    """
    value = inference_engines().get(capability)
    return value if isinstance(value, str) and value else AUTO_ENGINE


def _valid_engine_choice(capability: str, code: str) -> str | None:
    """None if `code` is a legal choice for `capability`, else why not.

    Legal is a much weaker claim than USABLE, deliberately: a runner that this
    machine cannot run is still a legal choice to store (see
    `inference_engines`). What is refused is a value that could never mean
    anything — an unknown capability, or a runner that does not serve the
    capability it is being set on — because those cannot be honoured on any
    machine and there is nothing for a page to explain about them.
    """
    from fused_render.ai import registry

    if capability not in registry.capabilities():
        return f"{capability!r} is not a capability this build knows"
    if code == AUTO_ENGINE:
        return None
    runner = registry.by_code(code)
    if runner is None:
        return f"{code!r} is not a runner this build knows"
    if runner.capability != capability:
        return f"{code!r} does not serve {capability!r}"
    return None


#: Process-level switch for file indexing, same shape as `FUSED_RENDER_CALLS`
#: (calls.enabled_override): unset → the preference decides; an off-word
#: ("0", "false", "no", "off") forces indexing off; any other value forces it
#: on. The force-ON side exists for dev runs, which default OFF (below).
INDEXING_ENV = "FUSED_RENDER_INDEXING"


def indexing_override() -> bool | None:
    raw = os.environ.get(INDEXING_ENV)
    if raw is None:
        return None
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_dev_run() -> bool:
    """A `scripts/dev.sh` server (it exports FUSED_RENDER_DEV=1; nothing in a
    packaged app ever sets it — routers/config.py reports the same bit as
    `dev`). pytest does not set it, so test defaults are untouched."""
    return os.environ.get("FUSED_RENDER_DEV") == "1"


def indexing_enabled() -> bool:
    """Whether background file-index scanning may run at all (default ON
    for the installed app, default OFF for a dev run).

    Mirrors `calls_enabled()`'s idiom: absence and any non-`false` stored
    value both read as enabled, so a preference file that predates this
    setting — every existing install — keeps scanning exactly as before.

    Dev runs (`is_dev_run`) default OFF with NO stored preference: every
    worktree server used to watch the whole home dir and compact the index
    alongside the installed app, which is what put a 10-core Mac at load
    ~500 and the desktop app past its boot cutoff (2026-10-16 diagnostics).
    A dev server that needs the index opts in — Preferences → Indexing
    (stores `indexing_enabled: true`) or `FUSED_RENDER_INDEXING=1` for the
    process — and the stored preference still wins over the dev default, so
    a developer who turned it on stays on across restarts.

    Turning this off does not delete the on-disk index or stop
    `/api/index/rank` from answering it — only new scans are refused. Every
    trigger that can start one (the startup scheduler, the on-demand scan
    routes, the freshness cadence, mutation-triggered rescans) reads this
    per call, same as `reader_enabled`, so a toggle applies to the very next
    one with no server restart.
    """
    override = indexing_override()
    if override is not None:
        return override
    stored = read_prefs().get("indexing_enabled")
    if stored is None and is_dev_run():
        return False
    return stored is not False


def git_auto_sync_enabled() -> bool:
    """Whether the app pulls and pushes its own git commits (default ON).

    Same idiom as `indexing_enabled`: absence and any non-`false` stored value
    read as enabled. Read per call by `git_upstream`'s auto-sync, so a toggle
    applies to the very next trigger with no restart. Off means today's
    behaviour exactly: the "Update" card for behind repos and manual flows.
    """
    return read_prefs().get("git_auto_sync_enabled") is not False


def auto_download_updates_enabled() -> bool:
    """Whether a found app update is downloaded without a click (default OFF).

    Only a stored `true` is on. Read per check by the update manager, so a
    toggle applies to the very next check with no restart. Restart stays
    user-initiated either way: this only moves "available" to "installed".
    """
    return read_prefs().get("auto_download_updates") is True


def index_low_impact_enabled() -> bool:
    """Whether the file index runs in *low-impact* mode (default OFF — opt-in
    like `task_recap_titles_enabled`).

    On, the index trades freshness for a quiet machine: one watcher owner per
    Mac (a machine-wide lease the installed :1777 app wins), longer and
    load-aware rescan floors, one scan worker at a time at a lower priority,
    watcher events dropped for search-hidden dot-folders and the app's own
    support dir, the startup scan deferred until after the server is ready,
    and compaction rewriting only the touched partitions. Read per call —
    the gate acts on the next trigger, no restart needed, except the worker
    which receives it through its run spec (`index/runner.py`).
    """
    return read_prefs().get("index_low_impact_enabled") is True


def ranked_search_enabled() -> bool:
    """Whether index-backed search orders hits by relevance score (default ON
    — D720). Off means the SQL branch that drops scoring entirely: hits come
    back `depth ASC, rel ASC` (shallowest, then alphabetical) instead.

    A sibling of `indexing_enabled`, NOT a member of `IndexConfig`/
    config.json: `POST /api/index/config` reconciles ignore-rule fingerprints
    and can kick off a rescan, and this is a query-time DISPLAY preference
    that must never be able to trigger one. Same idiom as `indexing_enabled`
    and `calls_enabled`: absence and any non-`false` stored value both read
    as enabled, so a preference file that predates this setting — every
    existing install — keeps the scored ordering exactly as before.
    """
    return read_prefs().get("ranked_search_enabled") is not False


def _warm_run_env() -> bool | None:
    """`FUSED_RENDER_WARM_RUN` as a tri-state (None = unset/unrecognised)."""
    raw = os.environ.get("FUSED_RENDER_WARM_RUN")
    if raw is None:
        return None
    raw = raw.strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return None


def warm_run_enabled() -> bool:
    """Whether `/api/run` may claim a pre-spawned idle interpreter (SPEC PY-20,
    default ON). `FUSED_RENDER_WARM_RUN` wins over the stored pref, the way
    `FUSED_RENDER_ENGINE` does; POSIX only, so Windows is always off. Same
    idiom as `ranked_search_enabled`: absence and any non-`false` stored value
    read as enabled."""
    if os.name != "posix":
        return False
    forced = _warm_run_env()
    if forced is not None:
        return forced
    return read_prefs().get("warm_run_enabled") is not False


def calls_enabled() -> bool:
    """Whether the app call log records anything (default ON — see calls.py).

    On by default because a diagnostic that has to be switched on before the
    thing you wanted to diagnose is worthless: the interesting call already
    happened. `FUSED_RENDER_CALLS=0` is the process-level off switch that beats
    this pref. Any non-`false` stored value (including missing) reads as on.
    """
    return read_prefs().get("calls_enabled") is not False


def calls_params_mode() -> str:
    """How much of a run's params the log keeps: full | keys | off.

    Default `full`: params are the inputs the author's own code already
    received and are usually the whole repro (the same named trade-off the
    serve plane's error records make), and locally they are already sitting in
    the URL bar. `keys` keeps only the key names for a page whose param is a
    secret; `off` keeps nothing.
    """
    value = read_prefs().get("calls_params")
    return value if value in VALID_CALLS_PARAMS else "full"


def calls_retention_days() -> int:
    """How long call-log files are kept (default 14, matching the serve plane's
    `errors/` lifecycle rule). 0 disables age-based pruning — the directory
    size cap in calls.sweep() still applies."""
    value = read_prefs().get("calls_retention_days")
    if isinstance(value, int) and 0 <= value <= 3_650:
        return value
    return DEFAULT_CALLS_RETENTION_DAYS


DEFAULT_CLAUDE_MAX_CONCURRENT_SESSIONS = 10


def claude_max_concurrent_sessions() -> int:
    """How many Claude sessions may run at once before the scheduler queues the
    rest (default 10). 0 = unlimited. Read by `session_cap.cap`."""
    value = read_prefs().get("claude_max_concurrent_sessions")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 64:
        return value
    return DEFAULT_CLAUDE_MAX_CONCURRENT_SESSIONS


def ai_idle_unload_minutes() -> int:
    """How long a resident model may sit idle before the reaper unloads it
    (default 5). 0 disables the reaper — see `ai.supervisor.reap_idle`."""
    value = read_prefs().get("ai_idle_unload_minutes")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_440:
        return value
    return DEFAULT_AI_IDLE_UNLOAD_MINUTES


def ai_idle_unload_minutes_override() -> int | None:
    """The window `FUSED_RENDER_AI_IDLE_MINUTES` actually imposes, or None when
    it imposes nothing — unset, empty, or not an integer.

    Same two-questions shape as `calls.retention_days_override()`: "is the
    variable set" and "is it in force" differ for an unparsable value, and a
    `forced_by` derived from presence alone would disable the page's control
    and blame a variable that decided nothing (D150).
    """
    raw = os.environ.get(AI_IDLE_MINUTES_ENV)
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def effective_ai_idle_unload_minutes() -> int:
    """What the reaper actually uses right now — the resolver `supervisor`
    itself calls, so the page can never report a window the reaper isn't
    honouring."""
    override = ai_idle_unload_minutes_override()
    return ai_idle_unload_minutes() if override is None else override


def _ai_idle_state() -> dict:
    """The `ai_idle` block of GET /api/prefs — same shape as `_calls_effective`:
    the stored choice, what is actually in force, and what's forcing it."""
    return {
        "minutes": ai_idle_unload_minutes(),
        "effective_minutes": effective_ai_idle_unload_minutes(),
        "forced_by": _forced_by(AI_IDLE_MINUTES_ENV, ai_idle_unload_minutes_override()),
    }


def fused_engine_available() -> bool:
    """Whether the fused backend is importable, resolved off the request path:
    engine.warm() caches it at startup, a mid-session install flips it via
    invalidate()."""
    from fused_render import engine as _engine

    return _engine.available_nonblocking()


def effective_engine() -> str:
    """The engine /api/run uses **right now** — the single resolver both the
    server's dispatch (`server.current_engine`) and the Preferences page
    (`engine_state`) go through, so the "currently running" label can never
    disagree with what actually executes a page.

    `FUSED_RENDER_ENGINE` overrides the pref (validated at startup by
    `server._forced_engine`, which fails loudly for `=fused` when the package
    is missing); otherwise the persisted pref decides. Availability is
    resolved **live** on every call — an `=auto` override, or a `fused` pref,
    both reflect a mid-session install/removal without a server restart
    (the earlier startup-frozen resolution let the page and dispatch drift
    after an install).
    """
    from fused_render import engine as _engine

    forced = _engine.forced_override()
    if forced is not None:
        if forced == "builtin":
            return "builtin"
        # auto / fused: fused iff importable now (=fused was startup-validated).
        return "fused" if fused_engine_available() else "builtin"
    return "fused" if (selected_engine() == "fused" and fused_engine_available()) else "builtin"


def engine_state() -> dict:
    """The engine block of GET /api/prefs.

    ``effective`` is `effective_engine()` — the SAME resolver the server's
    /api/run dispatch uses, so the page never reports a different running
    engine than the one executing pages. ``forced_by`` is the raw
    FUSED_RENDER_ENGINE value when set (the process override that beats the
    pref).
    """
    return {
        "selected": selected_engine(),
        "effective": effective_engine(),
        "forced_by": os.environ.get("FUSED_RENDER_ENGINE"),
        "fused_available": fused_engine_available(),
    }


def _lan_status() -> dict:
    try:
        from fused_render import lan

        return lan.status()
    except Exception as e:  # noqa: BLE001 — a status read must never fail prefs
        return {"running": False, "url": None, "error": str(e)}


def _workbench_state() -> dict:
    # Imported here, not at module top: fusedcli is the heavier module and
    # calls.py imports prefs on a hot path.
    from fused_render import fusedcli

    return {
        "env": fusedcli.workbench_env(),
        "locked": fusedcli.workbench_env_locked(),
        "choices": list(fusedcli.WORKBENCH_ENV_CHOICES),
    }


def _prefs_response() -> dict:
    return {
        "engine": engine_state(),
        # Whether the Reader (listen-to-files) accessibility mode is offered (opt-in).
        "reader": {"enabled": reader_enabled()},
        # Whether the Canvases feature is offered at all — its sidebar row and
        # its Settings menu entry (opt-in, D427). Not a route guard; see
        # `canvases_enabled`.
        "canvases": {"enabled": canvases_enabled()},
        # Whether the unified Share sheet (public link + .fused file) is offered
        # in place of the plain Export / Download action (opt-in, default off).
        # Not a route guard; see `app_sharing_enabled`.
        "app_sharing": {"enabled": app_sharing_enabled()},
        # Whether card thumbnails may render the live app in a scaled iframe
        # (opt-in, default off). Off, every thumbnail is a still or a
        # placeholder mark; see `live_previews_enabled`.
        "live_previews": {"enabled": live_previews_enabled()},
        # Whether the process Monitor (status-bar System chip + /monitor page)
        # is offered (opt-in, default off). Not an API guard; see
        # `monitor_enabled`.
        "monitor": {"enabled": monitor_enabled()},
        # Whether the Marketplace (sidebar row + /marketplace pages) is offered
        # (opt-in, default off). Not an API guard; see `marketplace_enabled`.
        "marketplace": {"enabled": marketplace_enabled()},
        # Whether the Share sheet offers the GitHub install-link card (opt-in,
        # default off). Not an API guard; see `github_link_enabled`.
        "github_link": {"enabled": github_link_enabled()},
        # The DEFAULT Fused environment (fusedcli.workbench_env): the canvas
        # catalog's initial pick, the `fused` wrapper outside a clone, share
        # uploads. Canvases carry their own env, so writing it (PUT below)
        # touches no sync manager. `locked` means FUSED_RENDER_WORKBENCH_ENV
        # pins it for the process.
        "workbench": _workbench_state(),
        # Whether the macOS app opens the shell in native windows rather than
        # browser tabs (default ON, opt-out). `available` says whether THIS
        # process can honour it — the packaged macOS app installs the hook; a
        # `fused-render serve` or another platform has no windows to offer, so
        # the Preferences section stays hidden there.
        "native_windows": _native_windows_state(),
        # The experimental Chromium engine for the macOS windows (opt-in):
        # `available` is "this process has native windows on macOS", and
        # `engine` is the download state the page shows next to the toggle.
        "chromium_windows": _chromium_windows_state(),
        # A task on the Tasks page opens in a side panel beside the list —
        # always, since 2026-09-20 (`task_peek_enabled`). Still sent, because
        # the client's flag module reads it.
        # …and `project`: the APP PAGE's Tasks tab does the same — always,
        # since 2026-09-21 (`project_peek_enabled`). Still sent, because the
        # client's type names it.
        "task_peek": {"enabled": task_peek_enabled(), "project": project_peek_enabled()},
        # Whether a finished-task notification fires for a session that
        # entered from an interactive terminal (default off, opt-in) — see
        # `notify_terminal_sessions_enabled`'s own doc comment for why this
        # can only ever be a best-effort signal, not a guarantee.
        "task_notify": {"terminal_sessions": notify_terminal_sessions_enabled()},
        # Whether one folder runs one task at a time (opt-in beta). Its own key
        # rather than a third field under `chat`: the gate governs the
        # scheduler and the Tasks board as much as it does a chat send, and
        # filing it under the chat would say it was the composer's setting.
        # No `forced_by` twin — there is no env override to report; see
        # `project_queue_enabled`.
        "queue": {"enabled": project_queue_enabled()},
        # Whether finished turns are recapped by haiku and the Tasks rows
        # titled by it (opt-in beta, `task_recap_titles_enabled`).
        "task_recap": {"enabled": task_recap_titles_enabled()},
        # Summary-only Tasks titles (`task_summary_titles_enabled`).
        "task_summary_titles": {"enabled": task_summary_titles_enabled()},
        # Lean Claude sessions (`claude_strict_mcp_enabled`, default on).
        "claude_strict_mcp": {"enabled": claude_strict_mcp_enabled()},
        "claude_default_permission": claude_default_permission(),
        # The Bots page's three switches and the Tasks page's view tabs —
        # localStorage until D1386 (see the readers' section comment).
        "bots": {"autostage": bots_autostage(), "sounds": bots_sounds(),
                 "clear_enabled": bots_clear_enabled()},
        "tasks_view_tabs": tasks_view_tabs(),
        # Local-network sharing of ~/Fused/local (lan.py): the STORED switch plus
        # the live listener state (url once it is up, error when it is not), so
        # the Preferences section can show the address a phone types.
        "lan": {"enabled": lan_enabled(), **_lan_status()},
        # The three agent switches and the agent new chats/tasks start from
        # (docs/COPILOT_BACKEND.md D17, docs/CODEX_BACKEND.md D17). `default`
        # is the EFFECTIVE one — the stored choice re-pointed to an enabled
        # agent by the registry — so the page never shows a default that is
        # switched off.
        "agents": _agents_state(),
        # Whether background file-index scanning may run (default ON), and
        # whether index-backed search orders hits by relevance score (default
        # ON — D720; off is `depth ASC, rel ASC` instead of scored).
        "indexing": {"enabled": indexing_enabled(), "ranked": ranked_search_enabled(),
                     "low_impact": index_low_impact_enabled()},
        # Whether /api/run may claim a pre-spawned idle interpreter (default
        # ON, PY-20); `forced` = FUSED_RENDER_WARM_RUN decides, not the pref.
        "warm_run": {"enabled": warm_run_enabled(), "forced": _warm_run_env() is not None},
        # Whether app-made git commits are auto-pushed and behind repos are
        # auto fast-forwarded on app open (default ON).
        "git": {"auto_sync": git_auto_sync_enabled()},
        # Whether a found app update is downloaded unattended (default OFF).
        "update": {"auto_download": auto_download_updates_enabled()},
        # Which local-model backend serves each capability (D302). The STORED
        # choice, what is actually resolving, and — when those differ — why, in
        # the registry's own words. Same discipline as `engine` above and
        # `calls` below: the control shows the choice, a line beside it reports
        # reality, and the two are allowed to differ because a preference set on
        # one machine can arrive on another that cannot honour it.
        "engines": _inference_engines_state(),
        # The app call log (calls.py): capture state, param redaction, retention.
        # `dir` lets the page reveal the store through the existing
        # /api/fs/reveal, exactly as `log.path` does.
        "calls": {
            # The STORED prefs — what a PUT round-trips, and what applies once
            # any process override is removed. What is actually in force is the
            # `effective_*` pair below; they differ whenever an env var wins.
            "enabled": calls_enabled(),
            "params": calls_params_mode(),
            "retention_days": calls_retention_days(),
            **_calls_store(),
            **_calls_effective(),
        },
        "claude_sessions": {"max_concurrent": claude_max_concurrent_sessions()},
        # How long an idle local AI model stays resident before the reaper
        # unloads it (SPEC AI-13). Same store/effective/forced_by shape as
        # `calls` above, minus a separate `_store()` helper — there is no
        # directory fact to report alongside this one.
        "ai_idle": _ai_idle_state(),
        # The macOS launcher's shortcuts (fused_render/launcher.py): the panel
        # hotkey and the row modifier, with display forms, plus whether the
        # running app could bind them (`bound` / `pinned_bound`: None until
        # something tried — a `fused-render serve` never does). `available`
        # says whether the panel exists on this platform at all; the
        # Preferences section renders only then.
        "launcher": _launcher_state(),
    }


def _chromium_windows_state() -> dict:
    from fused_render import chromium
    from fused_render import window_policy

    hooks = window_policy.native_hooks
    engine = chromium.status()
    # Only the macOS app has windows to put the engine in: the hook says this
    # process shows native windows, `status` says the platform can host CEF.
    available = ("apply" in hooks and engine["state"] != chromium.UNSUPPORTED
                 and hooks.get("usable", lambda: True)())
    enabled = chromium_windows_enabled()
    # Open windows keep the engine they were born with: how many are not on
    # the one a NEW window gets now (the row offers "Reopen windows"). The
    # target is computed here, cheaply, never through `engine_for_new_window`:
    # that may load CEF, which only the main thread can do.
    target = "chromium" if enabled and engine["state"] == chromium.READY else "webkit"
    count = hooks.get("windows_not_on")
    stale = count(target) if count is not None else 0
    return {"enabled": enabled, "available": available, "engine": engine,
            "stale_windows": stale}


def _native_windows_state() -> dict:
    from fused_render import window_policy

    hooks = window_policy.native_hooks
    # One hook contract, not a platform if/elif/else: `apply` installed at all
    # is necessary (macOS's app.py and Linux's linux_windows.install both set
    # it; anywhere else hooks stays {} and this is False). Linux
    # additionally installs `usable` — the window host is a separate process
    # that can be absent or have died, so it is asked rather than trusted —
    # while macOS's windows live in-process and installs no such hook; the
    # default `lambda: True` keeps that platform's behavior exactly as before.
    available = "apply" in hooks and hooks.get("usable", lambda: True)()
    return {"enabled": native_windows_enabled(), "available": available}


def _launcher_state() -> dict:
    from fused_render import launcher

    return launcher.settings()


def _inference_engines_state() -> dict:
    """The engines block of GET /api/prefs — the registry's own answer, relayed.

    Nothing is re-derived here. `registry.describe_engines()` is the same
    resolution a LOAD goes through, so the Preferences page cannot show an
    engine as serving a capability while a different one actually does — the
    failure `engine_state()` exists to prevent, one feature over. The
    per-capability rows carry their own `choices`, each with the availability
    reason the disabled control shows.

    Imported lazily: the AI registry is not something a preferences read should
    pull in until a page asks about it, and `calls.py` imports this module.
    """
    from fused_render.ai import registry

    return {"capabilities": registry.describe_engines(), "auto": AUTO_ENGINE}


def _calls_store() -> dict:
    """Where the call store is, and whether it is there yet.

    The writer creates the directory on its first append, so between "capture
    on" and "a page actually called something" the path in `dir` does not
    exist. `dir_exists` is reported rather than papered over by creating it
    here: this is a GET, and a read that provisions storage is a side effect in
    the wrong place — the lazy create belongs to the writer (`_append`), which
    is also what keeps an empty store from appearing for someone who never
    records a call. The UI uses the flag to say "nothing recorded yet" instead
    of sending the explorer to a path that will fail to stat.
    """
    # Imported lazily: calls.py imports this module (for the prefs above), so a
    # module-scope import here would be a cycle.
    from fused_render import calls

    path = calls.store_dir()
    return {"dir": path, "dir_exists": os.path.isdir(path)}


def _calls_effective() -> dict:
    """What capture and retention are *actually* doing, and what forced them.

    `FUSED_RENDER_CALLS` and `FUSED_RENDER_CALLS_RETENTION_DAYS` beat the stored
    prefs inside `calls.enabled()`/`calls.retention_days()`, so reporting only
    the stored values lets the page show capture as on while the process has it
    off — the exact failure `engine_state()` exists to prevent, in the same
    payload. Same treatment here: `effective_*` comes from **the resolvers the
    writer itself calls**, never a second copy of the precedence rule, so the
    page cannot report a state the log isn't in; `*_forced_by` carries the raw
    env value so the UI can name what is overriding — and is null unless that
    value is genuinely in force, which is not the same as being set (`_forced_by`).

    Only these two are overridable — the param-redaction mode has no env var, so
    it gets no pair rather than a misleading always-null one.
    """
    # Imported lazily: calls.py imports this module, so a module-scope import
    # here would be a cycle.
    from fused_render import calls

    return {
        "effective_enabled": calls.enabled(),
        "enabled_forced_by": _forced_by(calls.DISABLE_ENV, calls.enabled_override()),
        "effective_retention_days": calls.retention_days(),
        "retention_forced_by": _forced_by(
            calls.RETENTION_DAYS_ENV, calls.retention_days_override()
        ),
    }


def _forced_by(env_name: str, override) -> str | None:
    """The raw value of `env_name` when it is what's actually in force, else None.

    Gated on the writer's override resolver rather than on the variable merely
    being *set*, because for retention those differ: `calls.retention_days()`
    ignores an empty or non-numeric FUSED_RENDER_CALLS_RETENTION_DAYS and keeps
    using the pref. A `forced_by` derived from presence alone then disables the
    UI control and blames the variable for a window it is not setting — leaving
    the user unable to change retention from the page and unable to fix it by
    editing a variable that was never in force (D148). Same shape as the
    `effective_*` values above: ask the writer, don't re-derive.
    """
    return os.environ.get(env_name) if override is not None else None


@router.get("/api/prefs")
def get_prefs():
    return _prefs_response()


@router.put("/api/prefs")
def put_prefs(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Partial update: apply only the keys present, so the page can PUT one
    # setting without echoing the others (the engine radio and the reader
    # toggle are independent controls).
    # ONE WRITER AT A TIME (review, 2026-10-04): this is a read-modify-write
    # over the whole file, and `write_pref` above is another. Two requests in
    # flight — this toggling an engine, `/api/tasks/queue` toggling the queue
    # — would otherwise each write the record they read, and the slower one
    # erases the faster one's change. The lock covers the pair, not just the
    # write `storage.write_json` already makes atomic.
    with _PREFS_LOCK:
        prefs = read_prefs()
        changed = False
        if "engine" in body:
            engine = body.get("engine")
            if engine not in VALID_ENGINES:
                return JSONResponse(
                    {"error": f"'engine' must be one of: {', '.join(VALID_ENGINES)}"}, status_code=400
                )
            prefs["engine"] = engine
            changed = True
        if "reader_enabled" in body:
            value = body.get("reader_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'reader_enabled' must be a boolean"}, status_code=400)
            prefs["reader_enabled"] = value
            changed = True
        if "canvases_enabled" in body:
            value = body.get("canvases_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'canvases_enabled' must be a boolean"}, status_code=400)
            prefs["canvases_enabled"] = value
            changed = True
        if "app_sharing_enabled" in body:
            value = body.get("app_sharing_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'app_sharing_enabled' must be a boolean"}, status_code=400)
            prefs["app_sharing_enabled"] = value
            changed = True
        if "live_previews_enabled" in body:
            value = body.get("live_previews_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'live_previews_enabled' must be a boolean"}, status_code=400)
            prefs["live_previews_enabled"] = value
            changed = True
        if "monitor_enabled" in body:
            value = body.get("monitor_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'monitor_enabled' must be a boolean"}, status_code=400)
            prefs["monitor_enabled"] = value
            changed = True
        if "marketplace_enabled" in body:
            value = body.get("marketplace_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'marketplace_enabled' must be a boolean"}, status_code=400)
            prefs["marketplace_enabled"] = value
            changed = True
        if "github_link_enabled" in body:
            value = body.get("github_link_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'github_link_enabled' must be a boolean"}, status_code=400)
            prefs["github_link_enabled"] = value
            changed = True
        if "native_windows_enabled" in body:
            value = body.get("native_windows_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'native_windows_enabled' must be a boolean"}, status_code=400)
            # Written to prefs.json BEFORE `apply`: the Linux window host reads
            # this preference from prefs.json once it is listening (see
            # window_host.py's on_listening callback), so a write that lands
            # first is always visible to a host `apply` could not yet reach —
            # the host never has to keep its spawn-time state once the write is
            # on disk. Only an explicit `False` is a refusal: macOS's `apply`
            # returns None (it only queues work on the main thread). A host that
            # IS reachable but refuses (`apply` -> False) must not be saved as a
            # clean switch — prefs.json and the live host would then disagree for
            # as long as that host keeps running — so the previous value is
            # written back and the PUT fails instead (the page's existing error
            # banner for this toggle covers it; an unreachable host is not this
            # case, see `apply`).
            had_previous = "native_windows_enabled" in prefs
            previous = prefs.get("native_windows_enabled")
            prefs["native_windows_enabled"] = value
            storage.write_json(_path(), prefs)

            from fused_render import window_policy

            apply_windows = window_policy.native_hooks.get("apply")
            if apply_windows is not None and apply_windows(value) is False:
                if had_previous:
                    prefs["native_windows_enabled"] = previous
                else:
                    del prefs["native_windows_enabled"]
                storage.write_json(_path(), prefs)
                return JSONResponse(
                    {"error": "the window host refused the native-windows preference; try again"},
                    status_code=409,
                )
            changed = True
        if "chromium_windows_enabled" in body:
            value = body.get("chromium_windows_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'chromium_windows_enabled' must be a boolean"}, status_code=400)
            prefs["chromium_windows_enabled"] = value
            changed = True
            if value:
                # Turning it on is also "get the engine": the download runs
                # in the background and the page polls GET /api/prefs for
                # its progress. A no-op when it is unsupported, unpublished,
                # already there or already downloading (`install`).
                from fused_render import chromium

                chromium.install()
        if "task_notify_terminal_sessions" in body:
            value = body.get("task_notify_terminal_sessions")
            if not isinstance(value, bool):
                return JSONResponse(
                    {"error": "'task_notify_terminal_sessions' must be a boolean"}, status_code=400)
            prefs["task_notify_terminal_sessions"] = value
            changed = True
        if "project_queue_enabled" in body:
            value = body.get("project_queue_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'project_queue_enabled' must be a boolean"},
                                    status_code=400)
            prefs["project_queue_enabled"] = value
            changed = True
        if "task_recap_titles_enabled" in body:
            value = body.get("task_recap_titles_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'task_recap_titles_enabled' must be a boolean"},
                                    status_code=400)
            prefs["task_recap_titles_enabled"] = value
            changed = True
        if "task_summary_titles_enabled" in body:
            value = body.get("task_summary_titles_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'task_summary_titles_enabled' must be a boolean"},
                                    status_code=400)
            prefs["task_summary_titles_enabled"] = value
            changed = True
        for key in ("bots_autostage", "bots_sounds", "bots_clear_enabled"):
            if key in body:
                value = body.get(key)
                if not isinstance(value, bool):
                    return JSONResponse({"error": f"'{key}' must be a boolean"},
                                        status_code=400)
                prefs[key] = value
                changed = True
        if "tasks_view_tabs" in body:
            value = body.get("tasks_view_tabs")
            if (not isinstance(value, list) or len(value) > MAX_TASKS_VIEW_TABS
                    or not all(isinstance(t, str) and _TASKS_VIEW_TAB_RE.fullmatch(t)
                               for t in value)):
                return JSONResponse(
                    {"error": "'tasks_view_tabs' must be a list of at most "
                              f"{MAX_TASKS_VIEW_TABS} tab ids ([a-z_], 1-24 chars)"},
                    status_code=400)
            # Order kept, repeats dropped; [] stores "use the defaults".
            prefs["tasks_view_tabs"] = list(dict.fromkeys(value))
            changed = True
        if "claude_strict_mcp_enabled" in body:
            value = body.get("claude_strict_mcp_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'claude_strict_mcp_enabled' must be a boolean"},
                                    status_code=400)
            prefs["claude_strict_mcp_enabled"] = value
            changed = True
        if "claude_default_permission" in body:
            value = body.get("claude_default_permission")
            if value not in VALID_CLAUDE_PERMISSIONS:
                return JSONResponse(
                    {"error": "'claude_default_permission' must be one of: "
                              + ", ".join(repr(v) for v in VALID_CLAUDE_PERMISSIONS)},
                    status_code=400)
            prefs["claude_default_permission"] = value
            changed = True
        if "lan_enabled" in body:
            value = body.get("lan_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'lan_enabled' must be a boolean"}, status_code=400)
            # Fused Bot has no LAN sharing surface (boot never starts the
            # listener either): known key, nothing stored, `lan.apply` below skipped.
            if not _flavor.is_bot():
                prefs["lan_enabled"] = value
            changed = True
        if "engines" in body:
            # A MAP, applied key by key onto whatever is stored — the page changes
            # one capability's engine and must not have to echo the others, which is
            # the same partial-update rule this whole handler follows one level up.
            value = body.get("engines")
            if not isinstance(value, dict):
                return JSONResponse(
                    {"error": "'engines' must be an object of capability -> runner code"},
                    status_code=400,
                )
            engines = inference_engines()
            for capability, code in value.items():
                if not isinstance(code, str):
                    return JSONResponse(
                        {"error": f"'engines[{capability}]' must be a runner code or "
                                  f"{AUTO_ENGINE!r}"},
                        status_code=400,
                    )
                problem = _valid_engine_choice(str(capability), code)
                if problem:
                    return JSONResponse({"error": problem}, status_code=400)
                engines[str(capability)] = code
            prefs["engines"] = engines
            changed = True
        if "indexing_enabled" in body:
            value = body.get("indexing_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'indexing_enabled' must be a boolean"},
                                    status_code=400)
            prefs["indexing_enabled"] = value
            changed = True
            if value is False:
                # A scan running at the moment of toggle-off is cancelled outright
                # rather than merely refused going forward — the behavior contract
                # says "no scan ever starts" as soon as the pref is off, and a run
                # already in flight is exactly the case where "starts" happened a
                # moment too early. Imported lazily: the index router pulls this
                # module in (via `indexing_enabled` below), so a module-scope
                # import here would be a cycle.
                from fused_render.server.routers.index import cancel_all_scans

                cancel_all_scans()
        if "ranked_search_enabled" in body:
            value = body.get("ranked_search_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'ranked_search_enabled' must be a boolean"},
                                    status_code=400)
            prefs["ranked_search_enabled"] = value
            changed = True
        if "warm_run_enabled" in body:
            value = body.get("warm_run_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'warm_run_enabled' must be a boolean"},
                                    status_code=400)
            prefs["warm_run_enabled"] = value
            changed = True
        if "index_low_impact_enabled" in body:
            value = body.get("index_low_impact_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'index_low_impact_enabled' must be a boolean"},
                                    status_code=400)
            prefs["index_low_impact_enabled"] = value
            changed = True
        if "git_auto_sync_enabled" in body:
            value = body.get("git_auto_sync_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'git_auto_sync_enabled' must be a boolean"},
                                    status_code=400)
            prefs["git_auto_sync_enabled"] = value
            changed = True
        if "auto_download_updates" in body:
            value = body.get("auto_download_updates")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'auto_download_updates' must be a boolean"},
                                    status_code=400)
            prefs["auto_download_updates"] = value
            changed = True
        if "calls_enabled" in body:
            value = body.get("calls_enabled")
            if not isinstance(value, bool):
                return JSONResponse({"error": "'calls_enabled' must be a boolean"}, status_code=400)
            prefs["calls_enabled"] = value
            changed = True
        if "calls_params" in body:
            value = body.get("calls_params")
            if value not in VALID_CALLS_PARAMS:
                return JSONResponse(
                    {"error": f"'calls_params' must be one of: {', '.join(VALID_CALLS_PARAMS)}"},
                    status_code=400,
                )
            prefs["calls_params"] = value
            changed = True
        if "calls_retention_days" in body:
            value = body.get("calls_retention_days")
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3_650:
                return JSONResponse(
                    {"error": "'calls_retention_days' must be an integer between 0 and 3650"},
                    status_code=400,
                )
            prefs["calls_retention_days"] = value
            changed = True
        if "claude_max_concurrent_sessions" in body:
            value = body.get("claude_max_concurrent_sessions")
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 64:
                return JSONResponse(
                    {"error": "'claude_max_concurrent_sessions' must be an integer between 0 and 64"},
                    status_code=400,
                )
            prefs["claude_max_concurrent_sessions"] = value
            changed = True
        if "ai_idle_unload_minutes" in body:
            value = body.get("ai_idle_unload_minutes")
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_440:
                return JSONResponse(
                    {"error": "'ai_idle_unload_minutes' must be an integer between 0 and 1440"},
                    status_code=400,
                )
            prefs["ai_idle_unload_minutes"] = value
            changed = True
        if "workbench_env" in body:
            # Only the DEFAULT environment (see `_workbench_state`): no canvas
            # follows it, so there is nothing to stop first. Refused while
            # the env var pins it, since the write would change nothing.
            from fused_render import fusedcli

            value = body.get("workbench_env")
            if value not in fusedcli.WORKBENCH_ENV_CHOICES:
                return JSONResponse(
                    {"error": "'workbench_env' must be one of: "
                              + ", ".join(fusedcli.WORKBENCH_ENV_CHOICES)},
                    status_code=400)
            if fusedcli.workbench_env_locked():
                return JSONResponse(
                    {"error": f"the environment is pinned by {fusedcli.WORKBENCH_ENV_VAR}; "
                              "unset it to use the preference"},
                    status_code=423)
            prefs["workbench_env"] = value
            changed = True
        launcher_rebind: str | None = None
        launcher_changed = False
        if "launcher_hotkey" in body:
            # Canonicalised before storing (`alt+space`, modifiers in display
            # order) and refused whole when malformed — a spec with no modifier
            # would be a key taken from every app on the system. The wire name
            # is the same in both apps; it is STORED under the flavor's key
            # (`launcher.hotkey_key()`: `bot_launcher_hotkey` under Fused Bot),
            # because both apps share this file and run side by side, and
            # Carbon refuses a combo another process already holds.
            from fused_render import hotkey, launcher

            try:
                value = launcher.canonical_hotkey(body.get("launcher_hotkey"))
            except hotkey.SpecError as exc:
                return JSONResponse({"error": f"'launcher_hotkey': {exc}"}, status_code=400)
            prefs[launcher.hotkey_key()] = value
            launcher_rebind = value
            changed = launcher_changed = True
        if "launcher_row_modifier" in body:
            from fused_render import hotkey, launcher

            try:
                value = launcher.canonical_modifiers(body.get("launcher_row_modifier"))
            except hotkey.SpecError as exc:
                return JSONResponse({"error": f"'launcher_row_modifier': {exc}"}, status_code=400)
            prefs[launcher.row_modifier_key()] = value  # flavor's key, as above
            changed = launcher_changed = True
        agent_keys = tuple(AGENT_BACKEND_KEYS[b] for b in VALID_DEFAULT_BACKENDS)
        if any(k in body for k in (*agent_keys, "default_backend")):
            # THE FOUR AGENT KEYS TOGETHER (D17, invariant 11): three switches
            # and the default. Each is applied onto the merged record first
            # and the RESULT is judged, so one PUT that turns Claude off and
            # Codex on in the same body is fine, and so is one that turns two
            # off while the third stays on, while one that would leave all
            # three off is refused whole — there is no "no agent" state.
            # `default_backend` naming an agent that ends up off is re-pointed
            # to the first one that is on (in `VALID_DEFAULT_BACKENDS` order,
            # which is `registry.ORDER`), in the same write, so a reader never
            # finds a default nothing can honour.
            for key in agent_keys:
                if key in body:
                    value = body.get(key)
                    if not isinstance(value, bool):
                        return JSONResponse({"error": f"'{key}' must be a boolean"},
                                            status_code=400)
                    prefs[key] = value
            if "default_backend" in body:
                value = body.get("default_backend")
                if value not in VALID_DEFAULT_BACKENDS:
                    return JSONResponse(
                        {"error": "'default_backend' must be one of: "
                                  + ", ".join(repr(v) for v in VALID_DEFAULT_BACKENDS)},
                        status_code=400)
                prefs["default_backend"] = value
            # The same defaults `backend_enabled` reads: Claude on unless
            # stored False, Copilot and Codex off unless stored True.
            on = {"claude": prefs.get("claude_enabled") is not False,
                  "copilot": prefs.get("copilot_enabled") is True,
                  "codex": prefs.get("codex_enabled") is True}
            if not any(on.values()):
                return JSONResponse(
                    {"error": "turn on another agent first"}, status_code=400)
            wanted = prefs.get("default_backend")
            if wanted not in VALID_DEFAULT_BACKENDS:
                wanted = "claude"
            if not on[wanted]:
                prefs["default_backend"] = next(
                    b for b in VALID_DEFAULT_BACKENDS if on[b])
            changed = True
        if not changed:
            return JSONResponse(
                {"error": "no known preference in request (expected 'engine', "
                          "'engines', 'reader_enabled', 'canvases_enabled', 'app_sharing_enabled', 'live_previews_enabled', 'monitor_enabled', 'marketplace_enabled', 'github_link_enabled', "
                          "'native_windows_enabled', 'chromium_windows_enabled', "
                          "'task_notify_terminal_sessions', "
                          "'project_queue_enabled', "
                          "'task_recap_titles_enabled', "
                          "'task_summary_titles_enabled', "
                          "'claude_strict_mcp_enabled', 'claude_default_permission', "
                          "'bots_autostage', 'bots_sounds', 'bots_clear_enabled', "
                          "'tasks_view_tabs', "
                          "'lan_enabled', "
                          "'claude_enabled', 'copilot_enabled', 'codex_enabled', "
                          "'default_backend', 'indexing_enabled', 'ranked_search_enabled', "
                          "'warm_run_enabled', "
                          "'index_low_impact_enabled', "
                          "'git_auto_sync_enabled', 'auto_download_updates', "
                          "'calls_enabled', "
                          "'calls_params', 'calls_retention_days', "
                          "'claude_max_concurrent_sessions', "
                          "'ai_idle_unload_minutes', 'workbench_env', 'launcher_hotkey' and/or "
                          "'launcher_row_modifier')"},
                status_code=400,
            )
        storage.write_json(_path(), prefs)
    if "workbench_env" in body:
        # AFTER the write: the wrapper bakes workbench_env() in as the
        # FUSED_ENV a session's `fused` defaults to outside a clone, and it
        # reads the stored preference.
        from fused_render import fusedcli

        fusedcli.export_fused_cli_env()
    if launcher_changed:
        # AFTER the write, like `lan_enabled`: the app rebinds from the
        # stored preference on its main thread a tick later, so the
        # `launcher.bound` in THIS response is the previous state; the page
        # re-reads shortly after.
        from fused_render import launcher

        launcher.notify_settings_changed(launcher_rebind)
    if "indexing_enabled" in body:
        # AFTER the write, for the same reason as `lan_enabled` below: the
        # watcher threads read the gate from the stored preference, so a
        # resume before the write would see "off" on its first poll and
        # sleep a full GATE_POLL_S before opening. Off pauses the live
        # watcher (server/index_watch.py) — `cancel_all_scans` above only
        # stops scan workers, and a watch already open does not re-check
        # the gate until it ends. Lazy import: index_watch imports the index
        # router, which imports this module.
        from fused_render.server import index_watch

        if prefs["indexing_enabled"]:
            index_watch.resume()
        else:
            index_watch.pause()
    if "index_low_impact_enabled" in body:
        # AFTER the write: the owner waiter, the watcher child spec and the
        # rescan queue all read the flag back from the stored preference.
        # On: compete for the machine-wide watcher lease (stopping this
        # process's free-running watcher until it wins); off: give the lease
        # back and run the watcher as before. A no-op on a server that never
        # started the watcher. Lazy import, same cycle as above.
        from fused_render.server import index_owner

        index_owner.on_flag_changed(bool(prefs.get("index_low_impact_enabled")))
    if "lan_enabled" in body and not _flavor.is_bot():
        # AFTER the write, like `engines` below: the listener follows the stored
        # preference, and a failure to bind is reported in the response's
        # `lan.error` rather than failing the PUT.
        from fused_render import lan

        lan.apply(prefs["lan_enabled"])
    unloaded: list[str] | None = None
    if "engines" in body:
        # AFTER the write, because the reconciliation reads the new preference
        # rather than being told what changed — see `evict_stale_engines`. One
        # capability holds one resident model, and it belongs to the backend
        # that loaded it: leaving it there would spend gigabytes on a runner
        # nothing will route to again.
        from fused_render.ai import supervisor

        # KEPT, not discarded. The page tells the user "Loaded model unloaded."
        # and nothing in the prefs payload could say whether one was: residency
        # is not part of `describe_engines`, and the usual case — a fresh app
        # with nothing loaded yet — evicts nothing at all, so the page was
        # confidently reporting an eviction that never happened. Only the
        # server knows, so only the server can answer.
        unloaded = supervisor.evict_stale_engines()
    # The call log caches this snapshot for a second to keep prefs.json off its
    # hot path; drop it so a toggle here is visible on the very next call.
    from fused_render import calls as _calls

    _calls.invalidate_prefs_cache()
    # The new state, so the page re-renders from the response (the engine pref
    # is persisted even while FUSED_RENDER_ENGINE forces — it applies once the
    # override is removed; the response's forced_by says so).
    response = _prefs_response()
    if unloaded is not None:
        # Added here rather than inside `_prefs_response` because it is not
        # STATE: it is what this one request did, and a GET reporting `[]`
        # would invite the page to say "nothing was unloaded" about a read.
        response["engines"]["unloaded"] = unloaded
    return response


# ---- the Chromium engine (experimental; see fused_render.chromium) ----------

@router.get("/api/prefs/chromium")
def get_chromium_engine():
    """The engine's download state alone — what the Preferences row polls
    while a download runs, cheaper than the whole prefs document."""
    return _chromium_windows_state()


@router.post("/api/prefs/chromium/install")
def post_chromium_install(x_fused: str | None = Header(default=None)):
    """Start (or retry after a failure) the engine download."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import chromium

    chromium.install()
    return _chromium_windows_state()


@router.post("/api/prefs/chromium/reopen-windows")
def post_chromium_reopen_windows(x_fused: str | None = Header(default=None)):
    """Close every app window and open one Home window on the engine new
    windows get now — the way the preference takes effect for what is open."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import window_policy

    reopen = window_policy.native_hooks.get("reopen_windows")
    if reopen is None or not reopen():
        return JSONResponse({"error": "no window host in this process"}, status_code=409)
    return _chromium_windows_state()


@router.post("/api/prefs/chromium/remove")
def post_chromium_remove(x_fused: str | None = Header(default=None)):
    """Delete the unpacked engine from disk and switch the preference off.
    Open windows keep running on it until they close."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import chromium

    with _PREFS_LOCK:
        prefs = read_prefs()
        if prefs.get("chromium_windows_enabled") is not None:
            prefs["chromium_windows_enabled"] = False
            storage.write_json(_path(), prefs)
    chromium.remove()
    return _chromium_windows_state()
