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
    #: Whether the CLI accepts a reasoning effort for this model. False makes
    #: the spawn drop the effort rather than fail (Copilot 1.0.102 refuses
    #: `--reasoning-effort` for `auto` and its Claude models at `session/new`).
    reasoning: bool = True
    #: The effort levels THIS model takes, when the CLI said (Copilot's
    #: `models.list` names them per model); None means "the backend's list"
    #: when `reasoning`, and nothing when not. The pickers offer exactly
    #: this list and hide the control when it is empty.
    efforts: Optional[tuple[str, ...]] = None
    #: The level the CLI uses when none is asked for, when it said; "" = unknown.
    default_effort: str = ""
    #: Facts the CLI published beside the id, carried for a label or a
    #: grouping, never acted on: Copilot's picker category ("lightweight" /
    #: "versatile" / "powerful"), vendor, and billing multiplier.
    category: Optional[str] = None
    vendor: Optional[str] = None
    multiplier: Optional[float] = None

    def effort_list(self, backend_efforts: tuple[str, ...]) -> list[str]:
        """What the catalog serves as this model's `efforts`."""
        if not self.reasoning:
            return []
        if self.efforts is not None:
            return list(self.efforts)
        return list(backend_efforts)

    def to_catalog(self, backend_efforts: tuple[str, ...]) -> dict:
        efforts = self.effort_list(backend_efforts)
        return {
            "id": self.id,
            "label": self.label,
            "reasoning": bool(efforts),
            "efforts": efforts,
            "default_effort": self.default_effort if self.default_effort in efforts else "",
            "category": self.category,
            "vendor": self.vendor,
            "multiplier": self.multiplier,
        }


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
    #: Lazy: `(health_snapshot | None) -> tuple[Model, ...] | None` — the
    #: models the CLI itself said THIS account may run (Copilot's list moves
    #: with the plan and the release), read off the cached health snapshot,
    #: never probed. None means "nothing cached": the static `models` stand.
    #: The catalog, `model_ids(discovered=True)` and so the foreign-model
    #: gate read through it, so a model the CLI offers is never refused as
    #: another agent's.
    discovered_models: Optional[Callable[[Optional[dict]], Optional[tuple]]] = None

    def effective_models(self, health: Optional[dict] = None,
                         discovered: bool = True) -> tuple[Model, ...]:
        """The discovered list when there is one and it is asked for, else
        the static one. A hook that raises reads as "nothing cached"."""
        if discovered and self.discovered_models is not None:
            try:
                found = self.discovered_models(health)
            except Exception:  # noqa: BLE001 - a cache read is never a 500
                found = None
            if found:
                return tuple(found)
        return self.models

    def model_ids(self, discovered: bool = False) -> tuple[str, ...]:
        """The static ids, or with `discovered=True` the ids the account may
        run when the cache knows them. The static ids are NOT folded into a
        discovered list: an id the plan does not carry fails at spawn exactly
        as an uncataloged one would, and a reader that still names one (an
        old record) passes the foreign-model gate as "on nobody's list"."""
        return tuple(m.id for m in self.effective_models(discovered=discovered))

    def to_catalog(self, enabled: bool, health: Optional[dict] = None,
                   models: Optional[tuple] = None) -> dict:
        """The shape `GET /api/agent-backends` serves per backend. `models`
        is the discovered list when the caller has one (registry.catalog
        reads it off the same cached snapshot it hands in as `health`)."""
        return {
            "id": self.id,
            "label": self.label,
            "enabled": enabled,
            "models": [m.to_catalog(self.efforts) for m in (models or self.models)],
            "default_model": self.default_model,
            "efforts": list(self.efforts),
            "modes": list(self.modes),
            "caps": self.caps.__dict__.copy(),
            "health": health,
        }
