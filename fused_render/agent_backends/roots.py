"""App-side door onto `templates/shared/agent_roots.py` — where each agent
backend keeps its transcripts and its live-session registry.

The shared file is stdlib-only and loaded BY PATH by `claude_agent/agent.py`,
the two session hosts and `permission_server.py`, in processes where this
package is not importable; it is deliberately not a package module (shared
files never are — `.claude/skills/adding-a-shared-template-utility`). The app
side cannot `from fused_render.templates.shared import agent_roots` for the
same reason, so this module loads that one file the way `claude_spawn` loads
agent.py and re-exports its names. Every reader in the app that used to
hard-code `~/.claude/projects` imports the list from here, so there is one
rule for which roots exist (docs/COPILOT_BACKEND.md §1, invariant 7;
docs/CODEX_BACKEND.md §1 adds the third).

The two helpers at the bottom are the app's own: a module that keeps its
Claude root as a module-level constant for the tests that monkeypatch it
(`tasks_store.PROJECTS_DIR`, `tasks_watch.SESSIONS_DIR`, …) builds its root
list through them, so a patched constant still takes effect and the Copilot
and Codex roots ride alongside it.
"""
from __future__ import annotations

import importlib.util
import os

from fused_render._platform import is_within

_SHARED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "shared", "agent_roots.py")


def _load():
    spec = importlib.util.spec_from_file_location("_fused_agent_roots", _SHARED_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load()

BACKENDS = _shared.BACKENDS
DEFAULT_BACKEND = _shared.DEFAULT_BACKEND
claude_dir = _shared.claude_dir
copilot_dir = _shared.copilot_dir
codex_dir = _shared.codex_dir
base_dir = _shared.base_dir
projects_dir = _shared.projects_dir
sessions_dir = _shared.sessions_dir
transcript_roots = _shared.transcript_roots
registry_roots = _shared.registry_roots
munge = _shared.munge
backend_for_transcript = _shared.backend_for_transcript
find_transcript = _shared.find_transcript
backend_for_session = _shared.backend_for_session


def projects_roots(claude_projects_dir: str) -> list[tuple[str, str]]:
    """`[("claude", <given>), ("copilot", <copilot projects dir>),
    ("codex", <codex projects dir>)]` — the transcript root list for a
    module whose Claude root is a constant it exposes for tests to patch.
    The Copilot and Codex roots are read live, as the shared file reads them
    (`FUSED_RENDER_HOME` is redirected per test by conftest). Same order as
    `BACKENDS`, so a reader that walks the list meets the roots in the order
    `transcript_roots()` gives."""
    return [("claude", claude_projects_dir),
            ("copilot", projects_dir("copilot")),
            ("codex", projects_dir("codex"))]


def sessions_roots(claude_sessions_dir: str) -> list[tuple[str, str]]:
    """`projects_roots`'s twin for the live-session registries."""
    return [("claude", claude_sessions_dir),
            ("copilot", sessions_dir("copilot")),
            ("codex", sessions_dir("codex"))]


def is_under(path: str, root: str) -> bool:
    """Whether `path` resolves to somewhere inside `root` (realpath on both,
    so a project dir that is itself a symlink still counts as inside)."""
    return is_within(os.path.realpath(path), os.path.realpath(root),
                     fs_root_ok=False)
