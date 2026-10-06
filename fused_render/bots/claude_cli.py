"""Which `claude` the bots spawn (docs/bots.md §6).

FusedBot's copy of `claude_health` carried a `runnable()`; fused-render's does
not, and the shared module is not the bots' to grow, so the three lines live
here. Same rule as FusedBot's: the resolved CLI, or None when there is nothing
to RUN — every source but an explicit override was verified during
resolution, and an override (`FUSED_RENDER_CLAUDE_BIN` set by the user) is
taken on faith until checked here.

`claude_health.resolve()` may ask the login shell (`_shell_probe`); the bots
call this once per task (`bot._engine_for`, `agent_engine.run`), never from a
poll.
"""
from __future__ import annotations

from typing import Optional


def runnable() -> Optional[str]:
    from fused_render import claude_health

    path, source = claude_health.resolve()
    if path and (source != "override" or claude_health.executable(path)):
        return path
    return None
