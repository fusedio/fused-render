"""Runner-aware admission for the Hub search pane (D1274/D1275/D1276/D1287+).

The pool/live search paths admit a row on library/format alone
(`hub_models._model_row`'s own drop rules) — that answers "is this repo the
right KIND of thing", never "will SOMETHING here actually open it once
downloaded". Three runners narrow further, after their own download-and-load
step, in ways this module's own facts describe:

* `mflux-image` only builds the exact repo ids in
  `runners.formats.MFLUX_VARIANTS` — everything else refuses at load,
  after the download (`mflux_image/worker.py`'s own variant lookup).
* `mlx-text` (mlx-vlm) refuses at load any config whose `model_type` has no
  module in its own installed `mlx_vlm.models` package
  (`mlx_text/worker.py:_unsupported_architecture`).
* `ltx-video` only opens mlx-forge's curated split-conversion layout —
  `runners.formats.has_ltx_split_layout` plus a resolvable distilled
  transformer file (`runners.formats.distilled_transformer_filename`);
  everything else (a Diffusers or torch LTX repo) refuses (`ltx_video/
  worker.py::download`'s own refusal, mirrored here at search time).

Every other runner opens any repo of its format — GGUF/llama.cpp chief among
them — so the fourth kind, `"any"`, is the default and the common case.

**Judged against every AVAILABLE runner for the capability, not only the
ACTIVE one (D1287, item 3 of the architecture-detection brief).**
`registry.for_capability` answers "which one is ACTIVE right now", which is
the wrong question for "would ANYTHING here load this once downloaded" —
`registry.available_runners`'s own docstring makes exactly this argument
about format (GGUF vs. safetensors on one capability), and this module now
asks it for the SAME reason about a runner's own narrower admission rule.
The chip fires only when EVERY available runner refuses; a row that only the
non-preferred runner can open no longer gets flagged at all.

**When nothing admits, the reason NAMES what would load it** rather than
repeating one blanket sentence — `hub_architecture.resolve`'s own `engine`/
`shipped`/`name` facts are what make that possible: "needs Diffusers" (or
"needs Diffusers (MiniMaxH3Pipeline)") when the repo's own architecture maps
to an engine that IS wired into this app but is not among the runners
available right now; "needs Diffusers — not supported yet" when the engine
is recognised but no runner code is wired to it at all (`Architecture.
shipped` is False); "no engine loads <name> yet" when an architecture was
named but maps to no known engine; "no engine here loads this" as the final
fallback when nothing about the repo's shape was recognised at all. The one
exception is mlx-vlm's own `"<model_type> not supported by mlx-vlm"` —
preserved verbatim, and only, when mlx-text is the SOLE available runner and
is the one refusing: that sentence already names the exact runner and the
exact mismatch, and a generic architecture-shaped reason would say less, not
more.

**This module never DROPS a row.** Per the scope correction on D1274's own
item 2 (see DECISIONS.md), an unloadable row still renders — `admission()`
returns a flag and a short reason the frontend shows as a chip, and a row
already cached on this disk (downloaded/partial) is exempt from even that:
the drawer already reports a load failure for it, so a second "won't run
here" chip over a repo someone already has would be redundant, not helpful.
"""
from __future__ import annotations

import glob
import os

from fused_render.ai.hub_architecture import Architecture
from fused_render.ai.runners import formats
from fused_render.ai.runners.formats import MFLUX_VARIANTS

#: runner venv dir -> the `model_type`s its installed `mlx_vlm.models`
#: package can open, or `None` if that venv is not installed yet. Cached
#: in-process: this is a filesystem listing, and the search route calls it
#: once per row batch, not once per row.
_MLX_VLM_TYPES_CACHE: dict[str, frozenset | None] = {}


def reset_cache() -> None:
    """Test hook — a real install/uninstall between test cases must not leak
    a stale reading across them."""
    _MLX_VLM_TYPES_CACHE.clear()


def mlx_vlm_model_types() -> frozenset | None:
    """The `model_type` strings this machine's installed mlx-vlm venv can
    open — one entry per module in that venv's own `mlx_vlm/models/`
    package, since mlx-vlm's own layout names each architecture's module
    after the `model_type` string it handles (verified against
    `_unsupported_architecture`'s own `get_model_and_args` call, which
    dispatches on that same field).

    Returns `None` — "unknown", meaning no filtering — when the venv has not
    been installed yet, NEVER an empty set: an uninstalled venv must not
    read as "this runner can load nothing", which would hide every row for
    a machine that simply has not finished its first mlx-text download.
    """
    from fused_render import envinstall
    from fused_render.ai import registry

    runner = registry.by_code("mlx-text")
    if runner is None:
        return None
    venv_dir = envinstall.venv_dir_for(runner.folder)
    if venv_dir in _MLX_VLM_TYPES_CACHE:
        return _MLX_VLM_TYPES_CACHE[venv_dir]
    pattern = os.path.join(venv_dir, "lib", "python3.*", "site-packages", "mlx_vlm", "models")
    matches = glob.glob(pattern)
    result: frozenset | None = None
    if matches:
        names: set[str] = set()
        try:
            entries = os.listdir(matches[0])
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith(("_", ".")):
                continue
            full = os.path.join(matches[0], entry)
            if entry.endswith(".py") and os.path.isfile(full):
                names.add(entry[:-3])
            elif os.path.isdir(full) and os.path.exists(os.path.join(full, "__init__.py")):
                names.add(entry)
        if names:
            result = frozenset(names)
    _MLX_VLM_TYPES_CACHE[venv_dir] = result
    return result


def loadable_kind(code: str) -> tuple[str, frozenset[str] | None]:
    """`("allowlist", frozenset[str])` | `("model_types", frozenset[str] | None)`
    | `("file_layout", None)` | `("any", None)` for runner `code` — the one
    fact `admission()` checks a row against. Declared here rather than
    hard-coded per capability in `hub_models.py`, for `Runner.hub_filter_
    tags`'s own reason: a future runner with its own narrower loadable set
    should not require editing the search module, only this function.
    """
    if code == "mflux-image":
        return ("allowlist", frozenset(MFLUX_VARIANTS))
    if code == "mlx-text":
        return ("model_types", mlx_vlm_model_types())
    if code == "ltx-video":
        return ("file_layout", None)
    return ("any", None)


def _admits(kind: str, data, *, model_id: str, model_type: str | None,
            names: frozenset[str]) -> tuple[bool, str | None]:
    """Whether ONE runner (already reduced to its `loadable_kind`) would open
    this row, and the bespoke reason to use IF this turns out to be the only
    available runner and it refuses (see `admission`'s own handling of the
    single-runner case) — `None` when no bespoke wording applies and the
    generic architecture-shaped reason should win instead."""
    if kind == "allowlist":
        if isinstance(data, frozenset) and model_id in data:
            return True, None
        return False, None
    if kind == "model_types":
        if not isinstance(data, frozenset) or model_type is None or model_type in data:
            return True, None
        return False, f"{model_type} not supported by mlx-vlm"
    if kind == "file_layout":
        if (formats.has_ltx_split_layout(names)
                and formats.distilled_transformer_filename(names) is not None):
            return True, None
        return False, None
    return True, None


def _generic_reason(architecture: Architecture | None) -> str:
    """The reason to show when EVERY available runner refused and no
    bespoke, runner-specific wording applies — built from `hub_architecture.
    resolve`'s own facts about the repo, never a single blanket sentence."""
    if architecture is None or architecture.engine is None:
        if architecture is not None and architecture.name:
            return f"no engine loads {architecture.name} yet"
        return "no engine here loads this"
    if not architecture.shipped:
        return f"needs {architecture.engine} — not supported yet"
    if architecture.name:
        return f"needs {architecture.engine} ({architecture.name})"
    return f"needs {architecture.engine}"


def admission(*, runner_codes: tuple[str, ...], model_id: str,
              model_type: str | None,
              names: frozenset[str] = frozenset(),
              architecture: Architecture | None = None) -> tuple[bool, str | None]:
    """`(loadable, reason)` for one row, judged against EVERY runner
    AVAILABLE for its capability right now (`runner_codes` — the caller
    already resolved `registry.available_runners`; this module has no
    opinion on which runners are registered, only on what a given runner
    code can open).

    Loadable the moment ANY available runner would admit it — a row is only
    flagged when NONE of them would. `reason` is the short clause the
    frontend's chip shows after "Won't run here · " — never that full
    sentence itself, so the two stay reusable independently. Always `None`
    when `loadable` is True.

    `names`: the row's own sibling filenames (top-level), needed for
    `ltx-video`'s `"file_layout"` kind — empty for a caller that has not
    read a repo's file listing (every OTHER kind ignores it).

    `architecture`: `hub_architecture.resolve(raw)`'s own reading of the
    row, used ONLY to build the reason when every available runner refused
    and no bespoke per-runner wording applies (see `_generic_reason`).
    `None` reads the same as an unresolved architecture — "no engine here
    loads this".

    An empty `runner_codes` (no runner registered for this capability at
    all, which should not reach this function in production — `_model_row`
    only calls it when `for_capability` found one) reads as loadable: an
    unknown reading of "what would load this" must never itself become a
    reason to flag a row (see the module docstring's "never drops a row").
    """
    if not runner_codes:
        return True, None
    refusals: list[tuple[str, str, str | None]] = []
    for code in runner_codes:
        kind, data = loadable_kind(code)
        loadable, bespoke_reason = _admits(
            kind, data, model_id=model_id, model_type=model_type, names=names)
        if loadable:
            return True, None
        refusals.append((code, kind, bespoke_reason))
    if len(refusals) == 1:
        _code, kind, bespoke_reason = refusals[0]
        if kind == "model_types" and bespoke_reason is not None:
            return False, bespoke_reason
    return False, _generic_reason(architecture)
