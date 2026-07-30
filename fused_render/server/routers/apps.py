"""The Home view's apps backend: list the app folders in the Fused workspace
and scaffold new ones.

An "app" is simply a non-hidden top-level directory inside the workspace
(``fused_dir()``, ~/Documents/Fused). Its entry is the single direct-child
``.html`` file when there is exactly one — zero or several means the folder
still lists, but opens as a directory instead of a view (``entry_html: null``).

POST /api/apps/new scaffolds ``<workspace>/<name>/`` from the packaged app
starter kit (``fused_render/app_starter/`` — an ``index.html`` entry view plus
a ``CLAUDE.md``), copies the authoring skill in the same way the template
scaffold does (templates_api._ensure_starter_skills), and — when the request
carries a prompt — starts a detached Claude Code session in the new folder via
the claude template's own backend (templates/claude/agent.py), so the session
lands in the sidecar next to ``index.html`` and the existing claude template UI
lists and resumes it with no new machinery.
"""
import html
import os
import re
import shutil
import threading
import time

from fastapi import APIRouter, Body, Header

from fused_render.server.common import _error, _require_fused
from fused_render.shell.seed import fused_dir

router = APIRouter()

# The packaged app starter kit. Committed files (index.html, CLAUDE.md) ship
# with the package; .claude/skills/ inside it is a build-time copy of the
# repo-level skill (gitignored, shipped via pyproject's `artifacts` glob) —
# the same pattern as template_starter (D106).
_APP_STARTER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "app_starter"
)
# Apps get the authoring skill only: registering preview templates
# (fused-render-custom-templates) is a template concern, not an app one.
_APP_SKILLS = ("fused-render-authoring",)


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
        names = os.listdir(root)
    except OSError:
        # No workspace yet (first run before seeding) — an empty Home, not a 500.
        return {"apps": []}
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path):
                continue
            entry_html = _app_entry(path)
        except OSError:
            continue  # unreadable/racing entry: skip, never fail the listing
        apps.append({
            "name": name,
            "path": os.path.abspath(path),
            "entry_html": entry_html,
            "title": _entry_title(entry_html) if entry_html else None,
        })
    apps.sort(key=lambda a: a["name"].lower())
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


def _claude_agent():
    """Load the claude template's backend (agent.py) as a module.

    By file path, not import: templates are script folders, not packages, and
    the staged core copy (server.templates.TEMPLATES_DIR) is the one the
    claude template page itself executes — loading the same file keeps the
    runs dir, sidecar shape, and permission_server path in step with what the
    page will poll. Lazy import of server.templates keeps this module
    import-safe from app.py's top (server.templates is fine, but the lazy
    style matches how create_app pulls in templates_api)."""
    import importlib.util

    from fused_render.server import templates as _server_templates

    path = os.path.join(_server_templates.TEMPLATES_DIR, "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("fused_render_apps_claude_agent", path)
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


def _start_app_session(entry_html: str, prompt: str) -> bool:
    """Start a detached Claude Code session on the new app's entry file.

    The seam the tests stub. Reuses agent._start — cwd = the app folder,
    stream-json log, sidecar keyed to entry_html — with the prompt over stdin
    (message_via_stdin) so user text never enters argv. Returns whether a
    session actually started; a missing claude CLI or spawn failure must not
    fail the creation that already succeeded."""
    try:
        agent = _claude_agent()
        res = agent._start(entry_html, prompt, "", "", "", message_via_stdin=True)
    except Exception:
        return False
    if res.get("error") or not res.get("run_id"):
        return False
    threading.Thread(
        target=_record_session_when_ready, args=(agent, res["run_id"]),
        daemon=True, name="fused-app-session-record",
    ).start()
    return True


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
    dest = os.path.join(root, name)
    if os.path.exists(dest):
        return _error(f"{name!r} already exists in the workspace", status=409)

    os.makedirs(root, exist_ok=True)
    try:
        shutil.copytree(_APP_STARTER_DIR, dest)
    except FileExistsError:
        # TOCTOU: dest was created between the exists-check above and here.
        return _error(f"{name!r} already exists in the workspace", status=409)
    except Exception as exc:
        # A partial copy leaves a half-created folder behind; remove it so a
        # retry sees a clean slate and the exists-check stays meaningful.
        shutil.rmtree(dest, ignore_errors=True)
        return _error(f"failed to create app {name!r}: {exc}")

    # Editable installs have no packaged skills in the starter kit; resolve
    # them from the repo skills/ dir — same helper, same best-effort rule
    # (a missing skill never fails creation) as the template scaffold.
    from fused_render.templates_api import _ensure_starter_skills

    _ensure_starter_skills(dest, _APP_SKILLS)

    entry_html = os.path.join(dest, "index.html")
    session_started = False
    if prompt.strip():
        session_started = _start_app_session(entry_html, prompt)

    return {
        "path": os.path.abspath(dest),
        "entry_html": os.path.abspath(entry_html),
        "session_started": session_started,
    }
