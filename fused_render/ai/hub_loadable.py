"""Runner-aware admission for the Hub search pane (D1274/D1275/D1276).

The pool/live search paths admit a row on library/format alone
(`hub_models._model_row`'s own drop rules) — that answers "is this repo the
right KIND of thing", never "will the runner actually serving this
capability RIGHT NOW open it". Two runners narrow further, after their own
download-and-load step, in ways this module's own facts describe:

* `mflux-image` only builds the exact repo ids in
  `runners.formats.MFLUX_VARIANTS` — everything else refuses at load,
  after the download (`mflux_image/worker.py`'s own variant lookup).
* `mlx-text` (mlx-vlm) refuses at load any config whose `model_type` has no
  module in its own installed `mlx_vlm.models` package
  (`mlx_text/worker.py:_unsupported_architecture`).

Every other runner opens any repo of its format — GGUF/llama.cpp chief among
them — so the third kind, `"any"`, is the default and the common case.

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


def loadable_kind(code: str) -> tuple[str, object]:
    """`("allowlist", frozenset[str])` | `("model_types", frozenset[str] | None)`
    | `("any", None)` for runner `code` — the one fact `admission()` checks a
    row against. Declared here rather than hard-coded per capability in
    `hub_models.py`, for `Runner.hub_filter_tags`'s own reason: a future
    runner with its own narrower loadable set should not require editing the
    search module, only this function.
    """
    if code == "mflux-image":
        return ("allowlist", frozenset(MFLUX_VARIANTS))
    if code == "mlx-text":
        return ("model_types", mlx_vlm_model_types())
    return ("any", None)


def admission(*, runner_code: str, runner_short: str, model_id: str,
              model_type: str | None) -> tuple[bool, str | None]:
    """`(loadable, reason)` for one row, judged against the runner ACTIVE for
    its capability right now (`runner_code`/`runner_short` — the caller
    already resolved `for_capability`, this module has no opinion on which
    runner is active, only on what a given runner can open).

    `reason` is the short clause the frontend's chip shows after "Won't run
    here · " — never that full sentence itself, so the two stay reusable
    independently (a hover title wants the same clause without the chip
    label repeated). Always `None` when `loadable` is True.
    """
    kind, data = loadable_kind(runner_code)
    if kind == "allowlist":
        if model_id in data:
            return True, None
        return False, f"{runner_short} only loads FLUX.2 Klein"
    if kind == "model_types":
        if data is None or model_type is None or model_type in data:
            return True, None
        return False, f"{model_type} not supported by mlx-vlm"
    return True, None
