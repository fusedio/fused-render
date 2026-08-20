"""The apps backends: list app folders and scaffold new ones.

``GET /api/apps`` is the exhaustive catalog used by the Apps hub. The Home
page's ``GET /api/apps/home`` is recent-first: it hydrates explicit paths from
the two recents stores and invokes that exhaustive workspace walk only when the
valid recents do not fill Home's single row.

Apps live ONE TO THREE levels under the workspace (``fused_dir()``,
~/Fused), found by a bounded recursive walk whose per-level rules are
written down in ``app_listing.workspace_apps``: A DECLARED PAGE IS WHAT MAKES A
FOLDER AN APP — its entry is the first non-hidden direct-child ``.html``
carrying ``<meta name="fused-app">``, the one signal at every depth (D301;
filenames, ``index.html`` included, declare nothing). A page-less folder is a
SHELF: it is never a card, but it IS walked, which is how the apps inside it
are found. A "tag" is the FIRST path segment — there is no registry or
whitelist, so a new tag is just a new folder, discovered on the next listing,
and a third-level app files under the same tag as its second-level neighbours.
The entry rule is shared (D269), so the card, the preview pane and the
templates all resolve one folder to one page.

The walk itself lives in ``fused_render/app_listing.py``, which also defines
what one listed app looks like. Each app reports its entry twice: ``entry`` is
the file a card opens and previews, ``entry_html`` the narrower claim that the
entry is a renderable page (the only one the HTML-only ``/render`` iframe may be
pointed at). For an app of this shape they are the same file. ``preview_image``
is a third, unrelated path: an authored ``preview.png`` at the folder's root,
which a card shows INSTEAD of rendering the entry live.

POST /api/apps/new scaffolds ``<workspace>/local/<name>/`` from the packaged
app starter kit (``fused_render/app_starter/`` — an ``index.html`` entry view
plus a ``CLAUDE.md``) and — when the request carries a prompt — starts a
detached Claude Code session in the new folder via the claude template's own
backend (templates/claude/agent.py), so the session's transcript lands in the
folder's ~/.claude/projects dir and the existing claude template UI lists and
resumes it with no new machinery. "local" is just this feature's own tag — nothing about
the listing side treats it specially.

An app folder carries no ``.claude/`` of its own (D185); the starter
``CLAUDE.md`` references the canonical skills by name and fused-render supplies
them. The scaffolding session below gets them the way every session
fused-render spawns does — from the plugin root under the app's home dir,
loaded with ``--plugin-dir`` (skill_plugin.py, D216) — and the user-level copy
(user_skills.py) covers the user's own later ``claude`` in the folder. Both are
refreshed at server startup and again here at create time.
"""
import os
import shutil
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header

from fused_render import app_listing, claude_spawn
from fused_render.server.common import _error, _require_fused
from fused_render.shell.seed import fused_dir

router = APIRouter()

# The packaged app starter kit: index.html + CLAUDE.md, both committed. No
# .claude/ ships in it (D185) — skills are supplied by fused-render instead
# (skill_plugin.py for the sessions it spawns, user_skills.py for the rest).
_APP_STARTER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app_starter"
)


# ------------------------------------------------------------------- listing

# The walk and the entry contract live in `app_listing` rather than in this
# handler: they are the part worth testing directly (and reusing) — a route is
# not the right place for the rules about what an app IS. See that module.


def _workspace_apps() -> list[dict]:
    """The exhaustive workspace listing with its stored open timestamps."""
    root = fused_dir()
    apps = list(app_listing.workspace_apps(root))
    opened = _opened_at_by_app()
    for a in apps:
        a["opened_at"] = opened.get(_workspace_rel(root, a["path"]))
    return apps


@router.get("/api/apps")
def api_apps():
    from fused_render import registered_apps

    apps = _workspace_apps()
    # External folders the user opened through "Open app" — the registry's own
    # `openedAt` already rides in as `opened_at` (registered_apps.py), so these
    # sort by recency exactly as workspace apps do.
    apps.extend(registered_apps.registered_apps())
    # Exported .fused files anywhere on disk, from the file index (D392).
    # Index unavailable degrades to zero rows — never to a failed listing.
    from fused_render import exported_apps

    apps.extend(exported_apps.exported_apps())
    apps.sort(key=lambda a: (a["tag"].lower(), a["name"].lower()))
    return {"apps": apps}


# Home renders one row, not the complete app catalog. Its common path follows
# the two stores that are already newest-first and hydrates only their explicit
# paths. The recursive workspace walk is a fallback for a cold/incomplete store:
# that is what discovers never-opened local apps and the ordinary `showcase/`
# workspace tag without charging every returning Home visit for discovery.
HOME_APPS_LIMIT = 12


def _workspace_path_for_recent(root: str, rel: str) -> str | None:
    """Resolve a recents key under ``root`` without allowing it to escape.

    The store is user-writable. Keep this pure string work: the caller performs
    the targeted filesystem probes only after the path has passed this gate.
    """
    parts = rel.replace(os.sep, "/").split("/")
    if (os.path.isabs(rel) or rel.startswith(".") or ".." in parts
            or not 1 <= len(parts) <= app_listing.MAX_APP_DEPTH):
        return None
    if any(not p or os.path.isabs(p) or os.path.splitdrive(p)[0] for p in parts):
        return None
    path = os.path.abspath(os.path.join(root, *parts))
    try:
        if os.path.commonpath((os.path.abspath(root), path)) != os.path.abspath(root):
            return None
    except ValueError:
        return None
    return path


def _recent_workspace_apps(limit: int) -> list[dict]:
    """Hydrate at most ``limit`` valid workspace recents in stored order."""
    from fused_render.index.ignore import MountGuard

    root = fused_dir()
    guard = MountGuard()
    if guard.blocks(root):
        return []
    apps: list[dict] = []
    for recent in _read_app_recents()["entries"]:
        opened_at = _opened_epoch(recent.get("openedAt"))
        if opened_at is None:
            continue
        path = _workspace_path_for_recent(root, recent["path"])
        if path is None or guard.blocks(path):
            continue
        try:
            if not os.path.isdir(path):
                continue
            entry_html = app_listing.app_entry(path)
        except OSError:
            continue
        if entry_html is None:
            continue
        parts = recent["path"].replace(os.sep, "/").split("/")
        app = app_listing.app_dict(
            path, os.path.basename(path), parts[0], entry_html,
            include_updated_at=False,
        )
        app["opened_at"] = opened_at
        apps.append(app)
        if len(apps) >= limit:
            break
    return apps


def _app_recency(app: dict) -> float:
    opened = app.get("opened_at")
    return opened if isinstance(opened, (int, float)) else (app.get("updated_at") or 0)


@router.get("/api/apps/home")
def api_home_apps(limit: int = HOME_APPS_LIMIT):
    """Recent-first app cards for Home, with exhaustive discovery as fallback.

    A warm Home visit touches only explicit paths from the two recents stores.
    When those do not fill its single row, the ordinary workspace listing runs
    once and fills the holes; because showcase is an ordinary workspace tag,
    that fallback preserves unopened showcase cards as well as new local apps.
    """
    from fused_render import registered_apps

    limit = max(1, min(limit, HOME_APPS_LIMIT))
    recent = _recent_workspace_apps(limit)
    recent.extend(
        registered_apps.registered_apps(
            limit=limit, include_updated_at=False, opened_only=True
        )
    )
    # Opened .fused files (D392): their recents store is already newest-first
    # and every entry carries openedAt, so they merge exactly as the other two.
    from fused_render import exported_apps

    recent.extend(exported_apps.recent_exported_apps(limit))
    recent.sort(
        key=lambda a: (-_app_recency(a), a["tag"].lower(), a["name"].lower())
    )
    recent = recent[:limit]
    if len(recent) >= limit:
        return {"apps": recent}

    seen = {os.path.normcase(os.path.abspath(a["path"])) for a in recent}
    discovered = sorted(
        _workspace_apps(),
        key=lambda a: (-_app_recency(a), a["tag"].lower(), a["name"].lower()),
    )
    for app in discovered:
        identity = os.path.normcase(os.path.abspath(app["path"]))
        if identity in seen:
            continue
        recent.append(app)
        seen.add(identity)
        if len(recent) >= limit:
            break
    return {"apps": recent}


def _workspace_rel(root: str, path: str) -> str | None:
    """`path` as a workspace-relative, forward-slash key, or None when it isn't
    inside the workspace. The store's identity: unique at every depth the walk
    lists (1-3), where (tag, name) is not — two depth-3 apps under different
    shelves of one tag share both. Normalized to "/" so a key written on
    Windows matches the split in _app_folder_exists; the replace is os.sep-
    conditional because on POSIX a backslash is a legal filename character."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        # Windows: relpath across drives has no relative form — that is just
        # "not inside the workspace", not an error.
        return None
    if rel == "." or rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def _opened_at_by_app() -> dict[str, float]:
    """Workspace-relative app path → last-open time as epoch seconds
    (updated_at's unit), from the recents store. The file is user-writable, so
    a malformed openedAt just drops that entry — a bad timestamp must never
    fail the listing."""
    out: dict[str, float] = {}
    for e in _read_app_recents()["entries"]:
        ts = e.get("openedAt")
        if not isinstance(ts, str):
            continue
        try:
            out[e["path"]] = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
    return out


def _opened_epoch(value) -> float | None:
    """An ISO open timestamp as epoch seconds, or None when user-corrupt."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------- recents
#
# App-builder recents at ~/.fused-render/app_recents.json — its OWN store,
# fully independent of the explorer's recents.json (shell/recents.py). Entries
# identify an app by its WORKSPACE-RELATIVE path (`path`, e.g. "local/demo" or
# "tag/shelf/app") — unique at every depth the walk lists, where the previous
# (tag, name) key was not — newest-first, deduped, capped. GET filters entries
# whose app folder is gone (read-only — the folder may come back). The
# workspace is always local, so plain isdir checks are safe here.

# The store is the sort input for /home and /apps (opened_at in GET /api/apps),
# not just a short recents row — so the cap must comfortably exceed the number
# of apps a user actively cycles through, or open #N+1 silently loses its rank.
APP_RECENTS_CAP = 200


def _app_recents_path() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "app_recents.json")


def _read_app_recents() -> dict:
    from fused_render.shell import storage

    data = storage.read_json(_app_recents_path())
    if not isinstance(data, dict):
        return {"entries": []}
    entries = data.get("entries")
    return {
        "entries": [
            e
            for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict) and isinstance(e.get("path"), str)
        ]
    }


def _app_folder_exists(rel: str) -> bool:
    """Does the workspace-relative app path currently resolve to a folder on
    disk? Rejects a key that would escape the workspace — the store is
    user-writable, so `rel` cannot be trusted to stay under it."""
    # Split on the OS separator too: a user-edited backslash key on Windows
    # must not smuggle `..` past a "/"-only split. Segments are then vetted
    # individually — a drive-relative segment like "C:foo" would make a
    # starred os.path.join discard the workspace base entirely, so anything
    # carrying a drive or absolute form is rejected, and the join happens as
    # ONE "/"-joined string (a legal separator on Windows as well) so no
    # segment can ever reset the base.
    parts = rel.replace(os.sep, "/").split("/")
    if os.path.isabs(rel) or rel.startswith(".") or ".." in parts:
        return False
    if any(not p or os.path.isabs(p) or os.path.splitdrive(p)[0] for p in parts):
        return False
    return os.path.isdir(os.path.join(fused_dir(), "/".join(parts)))


@router.get("/api/apps/entry")
def api_app_entry(path: str):
    """The folder's app entry (its first tagged top-level page — the one rule,
    `app_listing.app_entry`) or null. The explorer's "Open app" button asks
    THIS instead of re-deriving the rule from filenames client-side: under the
    marker rule (D301) a name tells the client nothing, and a second copy of
    the rule in the shell is a copy that drifts. Any folder may be asked,
    workspace or not; an unreadable or entry-less one is `entry: null`."""
    from fused_render.index.ignore import MountGuard

    if not isinstance(path, str) or not os.path.isabs(path):
        return {"entry": None}
    if MountGuard().blocks(path):
        return {"entry": None}
    try:
        if not os.path.isdir(path):
            return {"entry": None}
        return {"entry": app_listing.app_entry(path)}
    except OSError:
        return {"entry": None}


@router.get("/api/apps/recents")
def api_app_recents():
    entries = [
        e for e in _read_app_recents()["entries"] if _app_folder_exists(e["path"])
    ]
    return {"entries": entries}


def record_app_open(path: str, title: str | None = None) -> bool:
    """Record that the app folder at absolute `path` was just opened.

    THE CALLER IS GET /render (D301): a page carrying the fused-app marker
    being rendered IS the open — every surface that shows an app renders it,
    so recording here needs no cooperation from any button or client post
    (the shell's "Open app" flow, D297, no longer records anything). Inside
    the workspace the open lands in the recents store (keyed workspace-
    relative); outside, opening IS registering — `registered_apps.record_open`
    puts the folder on the /apps hub and stores the open time itself.
    """
    from fused_render.shell import storage

    rel = _workspace_rel(fused_dir(), path)
    if rel is None:
        # Validation (exists, has a declared entry, not behind a wedged mount)
        # is the module's.
        from fused_render import registered_apps

        return registered_apps.record_open(path)
    if not _app_folder_exists(rel):
        return False
    data = _read_app_recents()
    # Dedupe by path; a title-less re-record keeps the last known title.
    existing_title = None
    kept = []
    for e in data["entries"]:
        if e["path"] == rel:
            t = e.get("title")
            if existing_title is None and isinstance(t, str) and t:
                existing_title = t
            continue
        kept.append(e)
    entry = {
        "path": rel,
        "openedAt": datetime.now(timezone.utc).isoformat(),
    }
    if title is not None:
        entry["title"] = title
    elif existing_title is not None:
        entry["title"] = existing_title
    data["entries"] = [entry, *kept][:APP_RECENTS_CAP]
    storage.write_json(_app_recents_path(), data)
    return True


@router.post("/api/apps/recents/open")
def api_app_recent_open(
    body: dict = Body(...), x_fused: str | None = Header(default=None)
):
    # Kept for older clients: the shell no longer posts here (D301 — the open
    # is recorded by GET /render when it serves a marker-carrying page).
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path")
    if not isinstance(path, str) or not path:
        return _error("path required", 400)
    title_raw = body.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None
    return {"recorded": record_app_open(path, title)}


# ------------------------------------------------------------------ creation

def _app_name_error(name) -> str | None:
    """Why `name` is not usable as an app folder name, or None if it is.
    Mirrors the frontend's appNameError (Home.tsx) so the modal's inline check
    and the server agree; the server stays authoritative."""
    if not isinstance(name, str) or not name.strip():
        return "'name' must be a non-empty string"
    # The caller creates the folder from name.strip(), so the trimmed value is
    # what reaches the filesystem — " .hidden" must be rejected like ".hidden".
    trimmed = name.strip()
    if "/" in trimmed or "\\" in trimmed:
        return "invalid app name: no '/' or '\\'"
    if trimmed.startswith("."):
        return "invalid app name: no leading '.'"
    return None


# The spawn machinery (where agent.py lives, why _start cannot be called in
# this process, and the poll that gets a run's turn committed) is shared with
# scheduled messages and lives in fused_render/claude_spawn.py.
#
# These two are re-bound as module-level names rather than called through
# `claude_spawn.` at the use site, because `_start_app_session` resolves them as
# globals — which is what lets a test swap either one out.
_claude_agent = claude_spawn.load_agent
_record_session_when_ready = claude_spawn.record_session_when_ready


# The permission mode the scaffolding session runs in, passed EXPLICITLY rather
# than left to agent.py's default ("prompt", the strictest).
#
# A template chat is watched: the page polls, a card appears, the user answers.
# This session has no page yet — it starts from an HTTP POST and nobody is
# polling `decide`, so under "prompt" the first tool call parks a request in
# the run's perm/ dir and blocks there until PERMISSION_WAIT (an hour by
# default) expires and the server denies it on the user's behalf. From the
# outside that is indistinguishable from the crash this module's helper fixes:
# a folder full of untouched boilerplate.
#
# "auto" is the broadest mode the template offers (bypassPermissions is
# deliberately not among PERMISSION_MODES at all): the CLI's own classifier
# approves what it judges safe and escalates the rest to a card. So the
# first-pass scaffolding work proceeds unattended, and anything the classifier
# won't take on itself still parks a request — answerable once the user opens
# the app's chat, which `run_id` in the response is there to let the UI do.
_APP_SESSION_PERMISSION_MODE = "auto"


def _spawn_session_helper(target: str, prompt: str) -> dict:
    """Run agent._start in the fork-safe helper; return its result dict.

    Thin wrapper over claude_spawn.spawn_helper, which holds the posix_spawn
    discipline this call depends on. What stays here is the one policy choice:
    the permission mode above, and a FRESH session always — an app is being
    scaffolded, so there is no prior conversation to resume."""
    return claude_spawn.spawn_helper(
        target, prompt, _APP_SESSION_PERMISSION_MODE)


def _start_app_session(app_dir: str, prompt: str) -> tuple[str | None, str | None]:
    """Start a detached Claude Code session on the new app's FOLDER.

    The seam the tests stub. Reuses the claude agent's _start (via the
    fork-safe helper above) — cwd = the app folder, stream-json log, the
    transcript in the same project dir the split view the app opens in lists
    and resumes from — with the prompt over stdin
    (message_via_stdin) so user text never enters argv. Returns
    (run_id, error), exactly one of them set: a missing claude CLI or spawn
    failure must not fail the creation that already succeeded, but the reason
    rides back so the UI isn't silent about it, and the run_id lets the
    caller attach to the live run."""
    try:
        res = _spawn_session_helper(app_dir, prompt)
    except Exception as exc:
        return None, f"failed to start Claude session: {exc}"
    if res.get("error") or not res.get("run_id"):
        return None, str(res.get("error") or "failed to start Claude session")
    threading.Thread(
        target=_record_session_when_ready, args=(_claude_agent(), res["run_id"]),
        daemon=True, name="fused-app-session-record",
    ).start()
    return str(res["run_id"]), None


@router.post("/api/apps/new")
def api_new_app(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    name = body.get("name")
    name_err = _app_name_error(name)
    if name_err is not None:
        return _error(name_err)
    name = name.strip()

    prompt = body.get("prompt", "")
    if not isinstance(prompt, str):
        return _error("'prompt' must be a string")

    root = fused_dir()
    dest = os.path.join(root, "local", name)
    if os.path.exists(dest):
        return _error(f"{name!r} already exists in the workspace", status=409)

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        # ignore .claude: apps carry no skills of their own (D185), and a dev
        # checkout may still hold a stale pre-D185 build copy in the starter.
        shutil.copytree(
            _APP_STARTER_DIR, dest, ignore=shutil.ignore_patterns(".claude")
        )
    except FileExistsError:
        # TOCTOU: dest was created between the exists-check above and here.
        return _error(f"{name!r} already exists in the workspace", status=409)
    except Exception as exc:
        # A partial copy leaves a half-created folder behind; remove it so a
        # retry sees a clean slate and the exists-check stays meaningful.
        shutil.rmtree(dest, ignore_errors=True)
        return _error(f"failed to create app {name!r}: {exc}")

    # Refresh the skills the starter CLAUDE.md references: the plugin root the
    # scaffolding session below is handed (D216) and the user-level copy for the
    # user's own sessions (D185). Startup already synced both; doing it again
    # here repairs a deletion in the window before that session starts.
    # Best-effort inside — never fails creation over a skill copy.
    from fused_render.skill_plugin import export_skill_plugin_env
    from fused_render.user_skills import sync_user_skills

    export_skill_plugin_env()
    sync_user_skills()

    # Version control from birth: every new app is a git repo whose first
    # commit is the untouched starter, BEFORE any session runs — so the
    # scaffolding turn's work diffs against the boilerplate, not nothing.
    # Best-effort (no git on the machine still gets a working app).
    from fused_render import app_git

    app_git.init_repo(dest)

    entry_html = os.path.join(dest, "index.html")
    run_id, session_error = None, None
    if prompt.strip():
        run_id, session_error = _start_app_session(dest, prompt)

    return {
        "path": os.path.abspath(dest),
        "entry_html": os.path.abspath(entry_html),
        "session_started": run_id is not None,
        # The live run, for a caller that wants to attach to the session it
        # just started (the claude template's own `run` param re-attach path:
        # poll it and answer any approval the "auto" classifier escalated).
        # None when no prompt was given or the spawn failed.
        "run_id": run_id,
        # Why the session did NOT start (claude CLI missing, spawn failure) —
        # the app itself was created fine, but the UI shouldn't be silent
        # about the prompt going nowhere. None when started or no prompt.
        "session_error": session_error,
    }
