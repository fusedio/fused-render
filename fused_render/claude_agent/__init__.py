"""The Claude chat backend, in-process.

Until 0.6.5 this lived at `templates/claude/agent.py` and ran as a FRESH Python
interpreter per call through `POST /api/run`, the same door every user `.py`
uses: fork, import, read a few files under the run dir, print JSON, exit — some
150 ms of CPU for a 400 ms poll, and under host pressure the single largest
source of interpreter churn the server had (the 2026-10-04 diagnostics bundle:
0.9 interpreters/s per open chat, 60 % of all `/api/run` traffic). The React
chat (`frontend/src/apps/claude`) had already replaced the template's iframe UI;
this package finishes that migration on the server side.

Layout:

- `agent.py`             the backend proper: every action handler, the run-dir
                         file contract, the session-host spawn. Imported here as
                         ONE module instance for the whole server. Also loadable
                         by file path (session_host.py and the tests do), so it
                         never imports `fused_render` and keeps the
                         sibling-import idiom for `templates/shared`.
- `session_host.py`      the long-lived child that owns the CLI's stdin. Run BY
                         PATH with `sys.executable`; cannot import the package.
- `permission_server.py` the stdio MCP server the CLI launches for permission
                         cards. Also run by path, also package-free.
- `artifacts.py`         the artifacts list/live reads the chat's strip shows.
- `gate.py`              the folder-busy admission gate for start/send (was
                         `server/routers/run.py`'s claude-only branch).
- `pool.py`              the bounded executor the router runs handlers on.

The HTTP surface is `server/routers/claude_agent.py`.
"""
from __future__ import annotations

import importlib
import os
import threading
from types import ModuleType

HERE = os.path.dirname(os.path.abspath(__file__))

#: Path of the session host script, launched by path from `agent._start`.
SESSION_HOST_PATH = os.path.join(HERE, "session_host.py")
#: Path of the permission MCP server, launched by path from the CLI.
PERMISSION_SERVER_PATH = os.path.join(HERE, "permission_server.py")
#: Path of agent.py itself, for the few callers that load it by path.
AGENT_PATH = os.path.join(HERE, "agent.py")

_MOD: ModuleType | None = None
_LOCK = threading.Lock()


def agent_module() -> ModuleType:
    """THE agent module, imported once.

    A plain import of `fused_render.claude_agent.agent`, memoised so every
    in-process reader — the router, the project queue, the tasks listing,
    canvases, the scheduler, sysmon — shares one instance of the module's
    caches and its inbox-ordering counter. Before this package existed the
    server held several copies (each `claude_spawn.load_agent()` exec'd the
    file afresh), each with its own `_echo_cache` and `_last_inbox_stamp_ns`.

    Raises on import failure rather than caching None: an agent module that
    does not import is a packaging bug, and the server should say so loudly on
    the first request that needs it.
    """
    global _MOD
    if _MOD is None:
        with _LOCK:
            if _MOD is None:
                _MOD = importlib.import_module("fused_render.claude_agent.agent")
    return _MOD
