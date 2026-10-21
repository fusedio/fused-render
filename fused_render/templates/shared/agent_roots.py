"""Where each agent backend keeps its transcripts and its live-session registry,
and how a session id is traced back to the backend that owns it.

Two backends drive Tasks and Sessions: Claude Code (`claude`) and GitHub
Copilot CLI (`copilot`). Claude Code writes its own transcript to
`~/.claude/projects/<munge(cwd)>/<sid>.jsonl` and its own live registry to
`~/.claude/sessions/<pid>.json`. Copilot writes nothing fused can read in
place, so fused's Copilot host (`claude_agent/copilot_host.py`) writes the
SAME two shapes under a root fused owns:

    <home>/copilot/projects/<munge(cwd)>/<sid>.jsonl
    <home>/copilot/sessions/<pid>.json

Same bucket layout, same munge rule, same row fields the tasks readers parse
— so every reader that used to open ONE projects dir opens the list this
module returns and otherwise does not change. The root a transcript sits
under is the authority on which backend owns the session: both kinds of id
are uuids (fused mints Claude's with --session-id; Copilot mints its own in
session/new, because --acp refuses --session-id), so nothing about the id
itself says which one it is.

Stdlib only, never imports `fused_render` (this file is loaded BY PATH from
`claude_agent/agent.py`, the two session hosts and the test suite, in
processes where the package is not importable — see the shared-utility
rules in `.claude/skills/adding-a-shared-template-utility`). The one fact it
needs from the app, the home dir, comes through the `appenv` sibling, loaded
by file location rather than by name so a user's own `appenv.py` cannot
shadow it.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import re

BACKENDS = ("claude", "copilot")
DEFAULT_BACKEND = "claude"


def _load_sibling_appenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_agent_roots_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_appenv = _load_sibling_appenv()


def claude_dir() -> str:
    """Claude Code's own data dir — the SAME one the CLI uses, so
    CLAUDE_CONFIG_DIR wins where the user set it."""
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def copilot_dir() -> str:
    """fused's own root for Copilot sessions. Under the UNNESTED home
    (`FUSED_RENDER_HOME` or `~/.fused-render`), not the branch-resolved one:
    a transcript is the user's conversation and must not vanish when the app
    is run from a different branch, exactly as `~/.claude` does not."""
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(home, "copilot")


def base_dir(backend: str) -> str:
    if backend == "copilot":
        return copilot_dir()
    return claude_dir()


def projects_dir(backend: str) -> str:
    return os.path.join(base_dir(backend), "projects")


def sessions_dir(backend: str) -> str:
    return os.path.join(base_dir(backend), "sessions")


def transcript_roots() -> list[tuple[str, str]]:
    """`[(backend, projects_dir), ...]` in BACKENDS order. Always both — a
    disabled backend's old transcripts still belong in the task list (read-
    only); the prefs gate is at the spawn sites, not here."""
    return [(b, projects_dir(b)) for b in BACKENDS]


def registry_roots() -> list[tuple[str, str]]:
    return [(b, sessions_dir(b)) for b in BACKENDS]


def munge(path: str) -> str:
    """A cwd's bucket name under a projects dir: every non-alphanumeric char
    becomes '-' (Claude Code's own rule, which the Copilot root copies so the
    tasks index's `_decode_project_dir` keeps working)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def backend_for_transcript(path: str) -> str:
    """Which backend's root `path` sits under; `claude` when under neither
    (a monkeypatched test root, or a path handed in from elsewhere)."""
    real = os.path.realpath(path)
    for backend, root in transcript_roots():
        r = os.path.realpath(root)
        if real == r or real.startswith(r + os.sep):
            return backend
    return DEFAULT_BACKEND


def find_transcript(session_id: str, workdir: str | None = None) -> tuple[str, str] | None:
    """`(backend, path)` of the transcript for `session_id`, or None.

    With `workdir`, the exact bucket is tried first under every root (cheap,
    and the common case); then a glob across every root's buckets, which is
    what `session_liveness.transcript_path` did for one root. The caller is
    responsible for `session_id` being a safe id (no separators)."""
    if workdir:
        m = munge(workdir)
        for backend, root in transcript_roots():
            p = os.path.join(root, m, session_id + ".jsonl")
            if os.path.isfile(p):
                return backend, p
    for backend, root in transcript_roots():
        hits = glob.glob(os.path.join(root, "*", session_id + ".jsonl"))
        if hits:
            return backend, max(hits, key=os.path.getmtime)
    return None


def backend_for_session(session_id: str, workdir: str | None = None,
                        hint: str | None = None) -> str:
    """The backend that owns `session_id`. `hint` is a recorded value (the
    session-settings record, a scheduler entry, a run dir's meta.json) and
    wins when it names a known backend; the transcript root is the fallback
    for a session that has a transcript but no record; `claude` otherwise
    (every pre-existing session is a Claude session)."""
    if hint in BACKENDS:
        return hint
    found = find_transcript(session_id, workdir)
    if found:
        return found[0]
    return DEFAULT_BACKEND
