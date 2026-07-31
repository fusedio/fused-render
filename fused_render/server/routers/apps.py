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

POST /api/apps/new scaffolds ``<workspace>/local/<name>/`` from the packaged
app starter kit (``fused_render/app_starter/`` — an ``index.html`` entry view
plus a ``CLAUDE.md``) and — when the request carries a prompt — starts a
detached Claude Code session in the new folder via the claude template's own
backend (templates/claude/agent.py), so the session lands in the sidecar next
to ``index.html`` and the existing claude template UI lists and resumes it
with no new machinery. "local" is just this feature's own tag — nothing about
the listing side treats it specially.

An app folder carries no ``.claude/`` of its own (D185): the canonical skills
are synced to Claude Code's user-level skills dir instead (user_skills.py) —
once per machine, refreshed at server startup and again here at create time —
and the starter ``CLAUDE.md`` references them by name.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from fastapi import APIRouter, Body, Header

from fused_render.server.common import _error, _require_fused
from fused_render.shell.seed import fused_dir

router = APIRouter()

# The packaged app starter kit: index.html + CLAUDE.md, both committed. No
# .claude/ ships in it (D185) — skills live at the user level (user_skills.py).
_APP_STARTER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app_starter"
)


# ------------------------------------------------------------------- listing

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _entry_title(entry_html: str) -> str | None:
    """The <title> of an entry file, from its first 4 KiB — cheap enough to run
    per app on every listing. None when absent, empty, or unreadable."""
    try:
        with open(entry_html, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    match = _TITLE_RE.search(head)
    if not match:
        return None
    title = html.unescape(match.group(1).decode("utf-8", "replace"))
    return " ".join(title.split()) or None


def _updated_at(dir_path: str) -> float | None:
    """When the app was last touched, as an epoch float (st_mtime).

    Max of the dir's own mtime and its DIRECT children's — the dir mtime alone
    only moves on add/remove/rename, so editing index.html in place wouldn't
    register; a deep walk is unbounded work per listing for marginal gain
    (edits in an app land overwhelmingly in top-level files). One extra stat
    per child, no recursion. None when nothing stats (racing delete)."""
    latest = None
    try:
        latest = os.stat(dir_path).st_mtime
        with os.scandir(dir_path) as it:
            for child in it:
                try:
                    latest = max(latest, child.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return latest


def _app_entry(dir_path: str) -> str | None:
    """The app's entry file: the single non-hidden direct-child .html, or None
    when the folder has zero or several (ambiguous — the UI opens the folder).
    Raises OSError when the dir can't be listed — the caller skips those."""
    children = os.listdir(dir_path)
    htmls = [
        c for c in sorted(children)
        if not c.startswith(".")
        and c.lower().endswith(".html")
        and os.path.isfile(os.path.join(dir_path, c))
    ]
    if len(htmls) != 1:
        return None
    return os.path.abspath(os.path.join(dir_path, htmls[0]))


@router.get("/api/apps")
def api_apps():
    root = fused_dir()
    apps = []
    try:
        tag_names = os.listdir(root)
    except OSError:
        # No workspace yet (first run before seeding) — an empty Home, not a 500.
        return {"apps": []}
    for tag in tag_names:
        if tag.startswith("."):
            continue
        tag_path = os.path.join(root, tag)
        try:
            if not os.path.isdir(tag_path):
                continue
            names = os.listdir(tag_path)
        except OSError:
            continue  # unreadable/racing tag dir: skip, never fail the listing
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(tag_path, name)
            try:
                if not os.path.isdir(path):
                    continue
                entry_html = _app_entry(path)
            except OSError:
                continue  # unreadable/racing entry: skip, never fail the listing
            apps.append({
                "name": name,
                "tag": tag,
                "path": os.path.abspath(path),
                "entry_html": entry_html,
                "title": _entry_title(entry_html) if entry_html else None,
                "updated_at": _updated_at(path),
            })
    apps.sort(key=lambda a: (a["tag"].lower(), a["name"].lower()))
    return {"apps": apps}


# ------------------------------------------------------------------ creation

def _app_name_error(name) -> str | None:
    """Why `name` is not usable as an app folder name, or None if it is.
    Mirrors the frontend's appNameError (Home.tsx) so the modal's inline check
    and the server agree; the server stays authoritative."""
    if not isinstance(name, str) or not name.strip():
        return "'name' must be a non-empty string"
    if "/" in name or "\\" in name:
        return "invalid app name: no '/' or '\\'"
    if name.startswith("."):
        return "invalid app name: no leading '.'"
    return None


def _agent_path() -> str:
    """The claude template backend (agent.py) — the staged core copy
    (server.templates.TEMPLATES_DIR), the same file the claude template page
    itself executes, so the runs dir, sidecar shape, and permission_server
    path stay in step with what the page will poll."""
    from fused_render.server import templates as _server_templates

    return os.path.join(_server_templates.TEMPLATES_DIR, "claude", "agent.py")


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
    """Poll the detached run until its session id lands in the sidecar.

    agent._poll is what writes the sidecar (first poll that sees the session
    id records it, one-shot via the run's `recorded` marker) — but nobody is
    polling until the user opens the new app's claude chat, which may be
    never. This background loop does the minimum: poll until recorded or the
    run ends, so the session is listed when the user does look."""
    for _ in range(300):  # ~10 min at 2 s — a session id arrives in seconds
        try:
            data = agent._poll(run_id)
        except Exception:
            return  # bookkeeping only; never let it matter
        if data.get("session_id") or data.get("done"):
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


def _spawn_session_helper(entry_html: str, prompt: str) -> dict:
    """Run agent._start in the fork-safe helper; return its result dict.

    close_fds=False + no cwd + no start_new_session keeps THIS Popen on the
    posix_spawn path (no atfork handlers — same discipline as executor.py's
    worker spawn). The helper itself detaches claude with setsid; it is a
    bare python where fork() is safe."""
    proc = subprocess.run(
        [sys.executable, "-c", _SESSION_HELPER],
        input=json.dumps(
            {"agent": _agent_path(), "file": entry_html, "message": prompt,
             "permission_mode": _APP_SESSION_PERMISSION_MODE}),
        capture_output=True, text=True, timeout=60, close_fds=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return {"error": "session helper failed: " + (tail[-1] if tail else "unknown")}
    return json.loads(proc.stdout)


def _start_app_session(entry_html: str, prompt: str) -> tuple[str | None, str | None]:
    """Start a detached Claude Code session on the new app's entry file.

    The seam the tests stub. Reuses agent._start (via the fork-safe helper
    above) — cwd = the app folder, stream-json log, sidecar keyed to
    entry_html — with the prompt over stdin (message_via_stdin) so user text
    never enters argv. Returns (run_id, error), exactly one of them set: a
    missing claude CLI or spawn failure must not fail the creation that
    already succeeded, but the reason rides back so the UI isn't silent about
    it, and the run_id lets the caller attach to the live run."""
    try:
        res = _spawn_session_helper(entry_html, prompt)
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

    # Refresh the user-level skills the starter CLAUDE.md references (D185).
    # Startup already synced them; doing it again here repairs a deletion
    # before the session below starts. Best-effort inside — never fails
    # creation over a skill copy.
    from fused_render.user_skills import sync_user_skills

    sync_user_skills()

    entry_html = os.path.join(dest, "index.html")
    run_id, session_error = None, None
    if prompt.strip():
        run_id, session_error = _start_app_session(entry_html, prompt)

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
