"""One agent backend, described: what the UI may offer for it, what the
chat may expect of it, and where its binary and roots are.

A `Backend` is a static description plus a few resolvers. It is NOT the
thing that spawns or drives a session — that stays in `claude_agent/`
(`session_host.py` for Claude, `copilot_host.py` for Copilot), which is
stdlib-only and loaded by path, and therefore cannot import this package.
`claude_agent/agent.py` picks the host file and builds the argv itself; this
package tells the app layer (prefs, routers, the catalog endpoint, the
scheduler's gate) which backends exist, which are switched on, and what each
can do, so that every surface reads one answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class Caps:
    """What a backend can do, as the chat and the tasks UI need to know it.
    Every flag maps to a surface that is hidden, or an action that answers
    `unsupported`, when it is False. Named in docs/COPILOT_BACKEND.md §caps."""
    plan_mode: bool
    live_model_switch: bool
    live_mode_switch: bool
    snapshots: bool
    artifacts: bool
    worktrees: bool
    bg_tasks: bool
    ask_user: bool
    terminal_mirror: bool
    mid_turn_messages: bool


@dataclass(frozen=True)
class Model:
    id: str
    label: str


@dataclass(frozen=True)
class Backend:
    id: str
    label: str
    models: tuple[Model, ...]
    default_model: str
    efforts: tuple[str, ...]
    modes: tuple[str, ...]
    caps: Caps
    #: Substrings (lower-cased match) of an error text that mean "signed out /
    #: no access" and "usage or credit limit", respectively. agent.py's
    #: `_account_error` and tasks' `_limit_hit` consult these per backend.
    account_error_markers: tuple[str, ...]
    limit_error_markers: tuple[str, ...]
    #: The terminal command that continues `session_id` in `cwd`. Built here so
    #: `_terminal_command`, TaskPeek and the kebab agree on one line.
    terminal_resume: Callable[[str, str], str]
    #: Lazy: `() -> dict` returning the health snapshot the UI's agent row
    #: renders (same keys as `claude_health.summary()`), or None when the
    #: probe module is not available. Lazy because the probe spawns.
    health: Optional[Callable[[], dict]] = None
    #: Lazy: `() -> dict | None`, the cached snapshot WITHOUT measuring. The
    #: catalog reads a DISABLED backend's health through this one, so turning
    #: a switch off also stops its probes (an ACP spawn + `gh auth token` for
    #: Copilot) from running on every catalog read.
    health_cached: Optional[Callable[[], Optional[dict]]] = None
    #: Lazy: the install/login/doctor router module name, for docs only.
    routes_prefix: str = ""
    #: Pref key that switches the backend on; `prefs.<key>()` must exist.
    pref_key: str = ""
    #: Which keys a start/send/create request must carry for this backend.
    extra: dict = field(default_factory=dict)

    def model_ids(self) -> tuple[str, ...]:
        return tuple(m.id for m in self.models)

    def to_catalog(self, enabled: bool, health: Optional[dict] = None) -> dict:
        """The shape `GET /api/agent-backends` serves per backend."""
        return {
            "id": self.id,
            "label": self.label,
            "enabled": enabled,
            "models": [{"id": m.id, "label": m.label} for m in self.models],
            "default_model": self.default_model,
            "efforts": list(self.efforts),
            "modes": list(self.modes),
            "caps": self.caps.__dict__.copy(),
            "health": health,
        }
