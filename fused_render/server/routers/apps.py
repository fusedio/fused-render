"""The Home view's apps backend: list the app folders in the Fused workspace
and scaffold new ones.

Apps live two levels under the workspace (``fused_dir()``, ~/Documents/Fused):
``<workspace>/<tag>/<name>/``. A "tag" is simply any non-hidden top-level
directory in the workspace — there is no registry or whitelist, so a new tag
is just a new folder, discovered on the next listing. An "app" is any
non-hidden directory directly inside a tag dir. Its entry is the single
direct-child ``.html`` file when there is exactly one — zero or several means
the folder still lists, but opens as a directory instead of a view
(``entry_html: null``).

The walk itself lives in ``fused_render/app_listing.py``, which also defines
what one listed app looks like. Each app reports its entry twice: ``entry`` is
the file a card opens and previews, ``entry_html`` the narrower claim that the
entry is a renderable page (the only one the HTML-only ``/render`` iframe may be
pointed at). For an app of this shape they are the same file.

POST /api/apps/new scaffolds ``<workspace>/local/<name>/`` from the packaged
app starter kit (``fused_render/app_starter/`` — an ``index.html`` entry view
plus a ``CLAUDE.md``) and — when the request carries a prompt — starts a
detached Claude Code session in the new folder via the claude template's own
backend (templates/claude/agent.py), so the session lands in the sidecar next
to ``index.html`` and the existing claude template UI lists and resumes it
with no new machinery. "local" is just this feature's own tag — nothing about
the listing side treats it specially.

An app folder carries no ``.claude/`` of its own (D185); the starter
``CLAUDE.md`` references the canonical skills by name and fused-render supplies
them. The scaffolding session below gets them the way every session
fused-render spawns does — from the plugin root under the app's home dir,
loaded with ``--plugin-dir`` (skill_plugin.py, D216) — and the user-level copy
(user_skills.py) covers the user's own later ``claude`` in the folder. Both are
refreshed at server startup and again here at create time.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header

from fused_render import app_listing
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


@router.get("/api/apps")
def api_apps():
    apps = app_listing.two_level_apps(fused_dir())
    apps.sort(key=lambda a: (a["tag"].lower(), a["name"].lower()))
    return {"apps": apps}


# ------------------------------------------------------------------- recents
#
# App-builder recents at ~/.fused-render/app_recents.json — its OWN store,
# fully independent of the explorer's recents.json (shell/recents.py). Entries
# identify an app by (tag, name), newest-first, deduped, capped. GET filters
# entries whose app folder is gone (read-only — the folder may come back).
# The workspace is always local, so plain isdir checks are safe here.

APP_RECENTS_CAP = 20


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
            if isinstance(e, dict)
            and isinstance(e.get("tag"), str)
            and isinstance(e.get("name"), str)
        ]
    }


@router.get("/api/apps/recents")
def api_app_recents():
    root = fused_dir()
    entries = [
        e
        for e in _read_app_recents()["entries"]
        if os.path.isdir(os.path.join(root, e["tag"], e["name"]))
    ]
    return {"entries": entries}


@router.post("/api/apps/recents/open")
def api_app_recent_open(
    body: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render.shell import storage

    tag, name = body.get("tag"), body.get("name")
    if not isinstance(tag, str) or not isinstance(name, str) or not tag or not name:
        return _error("tag and name required", 400)
    # Only real app folders are recorded — same benign no-op posture as the
    # explorer's POST /api/recents/open for a non-file url.
    if "/" in tag or "/" in name or tag.startswith(".") or name.startswith("."):
        return {"recorded": False}
    if not os.path.isdir(os.path.join(fused_dir(), tag, name)):
        return {"recorded": False}
    title_raw = body.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None
    data = _read_app_recents()
    # Dedupe by (tag, name); a title-less re-record keeps the last known title.
    existing_title = None
    kept = []
    for e in data["entries"]:
        if e["tag"] == tag and e["name"] == name:
            t = e.get("title")
            if existing_title is None and isinstance(t, str) and t:
                existing_title = t
            continue
        kept.append(e)
    entry = {
        "tag": tag,
        "name": name,
        "openedAt": datetime.now(timezone.utc).isoformat(),
    }
    if title is not None:
        entry["title"] = title
    elif existing_title is not None:
        entry["title"] = existing_title
    data["entries"] = [entry, *kept][:APP_RECENTS_CAP]
    storage.write_json(_app_recents_path(), data)
    return {"recorded": True}


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


def _agent_path() -> str:
    """The claude_split template backend (agent.py) — the staged core copy
    (server.templates.TEMPLATES_DIR), the same file the split app view
    executes, so the runs dir, sidecar shape (.claude-split.json inside the
    app folder), and permission_server path stay in step with what the page
    will poll. A newly CREATED app lands folder-first in claude_split (opening
    an existing one lands in the plain `app` view instead), so the scaffolding
    session must be recorded at the folder level too."""
    from fused_render.server import templates as _server_templates

    return os.path.join(_server_templates.TEMPLATES_DIR, "claude_split", "agent.py")


def _claude_agent():
    """Load agent.py as a module, for in-process READ paths only (_poll).
    The spawn goes through _SESSION_HELPER in a subprocess — see
    _start_app_session for why calling agent._start in this process crashes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fused_render_apps_claude_agent", _agent_path())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _record_session_when_ready(agent, run_id: str) -> None:
    """Poll the detached run until it finishes.

    agent._poll is what writes the sidecar (first poll that sees the session
    id records it, one-shot via the run's `recorded` marker) AND what commits
    the finished turn into the app's repo (one-shot via `committed`) — but
    nobody is polling until the user opens the new app's claude chat, which
    may be never. This background loop polls all the way to `done` so both
    happen regardless: the session is listed when the user does look, and the
    scaffolding turn's work is committed."""
    for _ in range(1800):  # ~1 h at 2 s — a scaffolding turn can run long
        try:
            data = agent._poll(run_id)
        except Exception:
            return  # bookkeeping only; never let it matter
        if data.get("done"):
            return
        time.sleep(2)


# The helper the spawn runs in. agent._start cannot be called in THIS process:
# its Popen sets cwd + start_new_session, which forces CPython off posix_spawn
# onto fork()+exec, and the server has libproj resident with a live proj.db
# SQLite handle — fork() runs PROJ's pthread_atfork child handler, which
# sqlite3_close()es that now-invalid handle and SIGSEGVs the child before exec
# (the exact crash test_worker_forksafe.py locks out of the executor; verified
# live: empty out.jsonl, dead pid, a Python .ips crash report with the server
# as parent). So the _start happens one hop away, in a bare python that has no
# libproj loaded and can fork freely. Args ride over stdin as JSON (never
# argv — the prompt is user text); the result comes back as one JSON line.
_SESSION_HELPER = """\
import importlib.util, json, sys
req = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location("claude_agent", req["agent"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps(mod._start(req["file"], req["message"], "", "", "",
                            permission_mode=req["permission_mode"],
                            message_via_stdin=True)))
"""

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

    close_fds=False + no cwd + no start_new_session keeps THIS Popen on the
    posix_spawn path (no atfork handlers — same discipline as executor.py's
    worker spawn). The helper itself detaches claude with setsid; it is a
    bare python where fork() is safe."""
    proc = subprocess.run(
        [sys.executable, "-c", _SESSION_HELPER],
        input=json.dumps(
            {"agent": _agent_path(), "file": target, "message": prompt,
             "permission_mode": _APP_SESSION_PERMISSION_MODE}),
        capture_output=True, text=True, timeout=60, close_fds=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return {"error": "session helper failed: " + (tail[-1] if tail else "unknown")}
    return json.loads(proc.stdout)


def _start_app_session(app_dir: str, prompt: str) -> tuple[str | None, str | None]:
    """Start a detached Claude Code session on the new app's FOLDER.

    The seam the tests stub. Reuses the claude_split agent's _start (via the
    fork-safe helper above) — cwd = the app folder, stream-json log, sidecar
    at <app_dir>/.claude-split.json, the same place the split view the app
    opens in lists and resumes from — with the prompt over stdin
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
