"""Runner-aware admission for the Hub search pane (D1274/D1275/D1276/D1287+).

The pool/live search paths admit a row on library/format alone
(`hub_models._model_row`'s own drop rules) — that answers "is this repo the
right KIND of thing", never "will SOMETHING here actually open it once
downloaded". Three runners narrow further, after their own download-and-load
step, in ways this module's own facts describe:

* `mflux-image` only builds a repo id mflux's OWN installed registry
  recognises — derived at search time from mflux's `AVAILABLE_MODELS`
  (statically parsed, never imported; see `mflux_loadable_repos`'s own
  docstring for why), admitting a row when either its own id names a
  registry entry directly, or its `base_model:` tag does (mflux's own
  `explicit_base` resolution rule — the case that matters for every
  `mlx-community` quantized conversion, none of which name themselves
  after the canonical repo). Before this (round: "derive mflux admission
  from the installed engine"), this was a two-entry hand-typed dict
  (`runners.formats.MFLUX_VARIANTS`) that refused every image repo on the
  Hub except the two it happened to name — mflux 0.19.0 ships fifteen
  model families. `MFLUX_VARIANTS` itself is untouched: it is still read
  at LOAD time, by `mflux_image/worker.py`, for the `variant`/`module`/
  `config`/`vae` payload admission has no opinion on.
* `mlx-text` (mlx-vlm) refuses at load any config whose `model_type` has no
  module in its own installed `mlx_vlm.models` package
  (`mlx_text/worker.py:_unsupported_architecture`).
* `ltx-video` only opens mlx-forge's curated split-conversion layout —
  `runners.formats.has_ltx_split_layout` plus a resolvable distilled
  transformer file (`runners.formats.distilled_transformer_filename`);
  everything else (a Diffusers or torch LTX repo) refuses (`ltx_video/
  worker.py::download`'s own refusal, mirrored here at search time).
* `diffusers-image`/`-cuda`/`-rocm` (`runners.formats.DIFFUSERS_RUNNERS`)
  only open a genuine Diffusers repo — `hub_architecture.is_diffusers_repo`'s
  predicate, shared verbatim with `hub_architecture._resolve_engine` so the
  "what engine is this" and "would Diffusers actually load it" questions
  can never disagree. Before this (round: "Diffusers admission is
  unconditional"), these three fell through to the `"any"` default below
  and rubber-stamped EVERY row in the image capability, unconditionally —
  including rows with no Diffusers manifest at all (e.g. an MLX-only repo
  that merely widened into the image slice via the `image-to-image`
  pipeline tag, D1235).

Every other runner opens any repo of its format — GGUF/llama.cpp chief among
them — so the fifth kind, `"any"`, is the default and the common case.

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

import ast
import glob
import os

from fused_render.ai.hub_architecture import Architecture, is_diffusers_repo
from fused_render.ai.runners import formats
from fused_render.ai.runners.formats import DIFFUSERS_RUNNERS

#: runner venv dir -> the `model_type`s its installed `mlx_vlm.models`
#: package can open, or `None` if that venv is not installed yet. Cached
#: in-process: this is a filesystem listing, and the search route calls it
#: once per row batch, not once per row.
_MLX_VLM_TYPES_CACHE: dict[str, frozenset | None] = {}

#: runner venv dir -> the repo ids mflux's own installed `AVAILABLE_MODELS`
#: registry recognises, or `None` if that venv is not installed yet. Same
#: cache shape and same reason as `_MLX_VLM_TYPES_CACHE` above.
_MFLUX_REPOS_CACHE: dict[str, frozenset | None] = {}


def reset_cache() -> None:
    """Test hook — a real install/uninstall between test cases must not leak
    a stale reading across them."""
    _MLX_VLM_TYPES_CACHE.clear()
    _MFLUX_REPOS_CACHE.clear()


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


def _repo_ids_from_available_models(source: str) -> frozenset[str]:
    """Statically parse mflux's `AVAILABLE_MODELS` dict literal out of its
    OWN `model_config.py` source text — every entry's `model_name` keyword
    (the HF repo id each `ModelConfig` loads), plus any `aliases` entry that
    is itself repo-shaped (contains `/`; none currently are, but the brief
    calls for it defensively).

    **Parsed with `ast`, never imported.** Importing `mflux.models.common.
    config.model_config` pulls in `mlx.core` (Metal/Accelerate init) — a
    runner-venv dependency this server process must not carry, per the
    brief's own instruction and mirroring `mlx_vlm_model_types`'s choice to
    read the filesystem rather than import `mlx_vlm`.

    Deliberately a SET, not a `repo_id -> variant` dict: `resolve_key`'s own
    docstring in `config_resolution.py` warns that several registry entries
    SHARE a repo id (the FLUX.1-dev ControlNets; z-image-turbo and its
    ControlNet) and are matched by object identity, not by name — a flat
    dict keyed by repo id cannot represent that, and would have to silently
    pick one entry over another. Admission only asks "would ANY entry with
    this id resolve", never "which one", so a de-duplicating set answers it
    exactly and the identity ambiguity never arises here.

    Returns an empty set on anything unparsable (syntax error, no
    `AVAILABLE_MODELS` assignment found) — the caller reads that the same
    way as "nothing found", i.e. still `None` overall, never "matches
    nothing" for an installed-but-unreadable venv."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    repos: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "AVAILABLE_MODELS" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for value_node in node.value.values:
            if not isinstance(value_node, ast.Call):
                continue
            for kw in value_node.keywords:
                if kw.arg == "model_name":
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        repos.add(kw.value.value)
                elif kw.arg == "aliases":
                    if isinstance(kw.value, ast.List):
                        for elt in kw.value.elts:
                            if (isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                                    and "/" in elt.value):
                                repos.add(elt.value)
    return frozenset(repos)


def mflux_loadable_repos() -> frozenset[str] | None:
    """The repo ids mflux's own installed `AVAILABLE_MODELS` registry
    recognises — read off its `model_config.py` source (see
    `_repo_ids_from_available_models` for why statically, never imported).

    Returns `None` — "unknown", meaning no filtering — when the mflux venv
    has not been installed yet, NEVER an empty set, for the same reason as
    `mlx_vlm_model_types`: an uninstalled venv must not read as "this
    runner can load nothing", which would flag the entire image capability
    on a machine that has not installed mflux yet."""
    from fused_render import envinstall
    from fused_render.ai import registry

    runner = registry.by_code("mflux-image")
    if runner is None:
        return None
    venv_dir = envinstall.venv_dir_for(runner.folder)
    if venv_dir in _MFLUX_REPOS_CACHE:
        return _MFLUX_REPOS_CACHE[venv_dir]
    pattern = os.path.join(venv_dir, "lib", "python3.*", "site-packages", "mflux",
                            "models", "common", "config", "model_config.py")
    matches = glob.glob(pattern)
    result: frozenset[str] | None = None
    if matches:
        try:
            with open(matches[0], encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            source = ""
        repos = _repo_ids_from_available_models(source)
        if repos:
            result = repos
    _MFLUX_REPOS_CACHE[venv_dir] = result
    return result


def loadable_kind(code: str) -> tuple[str, frozenset[str] | None]:
    """`("mflux", frozenset[str] | None)` | `("model_types", frozenset[str] |
    None)` | `("file_layout", None)` | `("diffusers", None)` | `("any",
    None)` for runner `code` — the one fact `admission()` checks a row
    against. Declared here rather than
    hard-coded per capability in `hub_models.py`, for `Runner.hub_filter_
    tags`'s own reason: a future runner with its own narrower loadable set
    should not require editing the search module, only this function.
    """
    if code == "mflux-image":
        return ("mflux", mflux_loadable_repos())
    if code == "mlx-text":
        return ("model_types", mlx_vlm_model_types())
    if code == "ltx-video":
        return ("file_layout", None)
    if code in DIFFUSERS_RUNNERS:
        return ("diffusers", None)
    return ("any", None)


def _admits(kind: str, data, *, model_id: str, model_type: str | None,
            names: frozenset[str],
            library_name: str | None = None,
            base_model: str | None = None) -> tuple[bool, str | None]:
    """Whether ONE runner (already reduced to its `loadable_kind`) would open
    this row, and the bespoke reason to use IF this turns out to be the only
    available runner and it refuses (see `admission`'s own handling of the
    single-runner case) — `None` when no bespoke wording applies and the
    generic architecture-shaped reason should win instead."""
    if kind == "allowlist":
        # Kept as a generic, general-purpose kind (plain id-set membership,
        # no `base_model` fallback) even though no runner's `loadable_kind`
        # currently produces it — `mflux-image` moved to the more capable
        # `"mflux"` kind below this round, but hub_models.py's own
        # integration tests still use `"allowlist"` as a reusable stand-in
        # for "some restrictive membership test" via a direct
        # `loadable_kind` monkeypatch, independent of any specific runner.
        if isinstance(data, frozenset) and model_id in data:
            return True, None
        return False, None
    if kind == "mflux":
        # `data` is `None` — "mflux venv not installed yet, unknown" — the
        # same "unknown means don't filter" convention as `model_types`
        # below: an uninstalled venv must not flag every image row.
        if not isinstance(data, frozenset):
            return True, None
        # Rule 1, mflux's own `exact_match`: the row's own id names a
        # registry entry directly (a straight browse of a canonical repo,
        # e.g. `black-forest-labs/FLUX.2-klein-4B` itself).
        if model_id in data:
            return True, None
        # Rule 2, mflux's own `explicit_base`: the row's `base_model:` tag
        # names a registry entry — the case that matters for every
        # `mlx-community` quantized conversion, none of which are named
        # after the canonical repo mflux actually knows.
        if base_model and base_model in data:
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
    if kind == "diffusers":
        # A Diffusers runner admits a repo the SAME way `hub_architecture.
        # _resolve_engine` recognises one — `model_index.json`,
        # `modular_model_index.json`, or `library_name == "diffusers"`
        # (`is_diffusers_repo`, shared with that module so the two can
        # never drift). The catch: `names`/`library_name` being absent must
        # NEVER read as "therefore not Diffusers" — that would flag every
        # row a caller has not yet read siblings/library for, exactly the
        # module docstring's "never drops a row" guarantee this would
        # otherwise break. Only refuse when at least one of the two signals
        # was actually supplied; both absent (the empty-`names`-default
        # PLUS no `library_name`) stays loadable instead.
        if not names and library_name is None:
            return True, None
        if is_diffusers_repo(names, library_name):
            return True, None
        return False, None
    return True, None


def _generic_reason(architecture: Architecture | None) -> str:
    """The reason to show when EVERY available runner refused and no
    bespoke, runner-specific wording applies — built from `hub_architecture.
    resolve`'s own facts about the repo, never a single blanket sentence.

    **Leads with the ARCHITECTURE, not the engine, when the engine is not
    shipped.** The bug this fixes: every Diffusers runner registered in this
    app serves IMAGE_GENERATION only, so a video row whose `engine` resolves
    to `"Diffusers"` (or an MLX-format video row whose `engine` resolves to
    `"MLX"`, itself text-generation-only) used to read "needs Diffusers —
    not supported yet" / "needs MLX — not supported yet" — naming an engine
    this app can NEVER use for that row's capability, which reads as "install
    Diffusers/MLX and this will run," exactly backwards. When a real
    architecture `name` was recovered (and is not just the bare-library
    fallback stuttering the engine name), the not-shipped case says only
    "<name> not supported yet" — true regardless of which engine happened to
    host this particular republish. The bare-library fallback (`config: {}`,
    no `base_model:` tag, `name` is nothing but the word "diffusers"/"mlx"
    itself) still falls back to naming the engine, since there is nothing
    else to say.

    The SHIPPED case (the engine IS wired to a runner for this row's own
    capability, but every currently AVAILABLE runner still refused — e.g.
    the modular-Diffusers-pipeline story this module's docstring opens
    with) is unchanged: an engine that DOES run here is worth naming
    alongside the architecture, `"needs Diffusers (MiniMaxH3Pipeline)"`."""
    if architecture is None or architecture.engine is None:
        if architecture is not None and architecture.name:
            return f"no engine loads {architecture.name} yet"
        return "no engine here loads this"
    has_real_name = architecture.name and not architecture.name_is_bare_library
    if not architecture.shipped:
        if has_real_name:
            return f"{architecture.name} not supported yet"
        return f"needs {architecture.engine} — not supported yet"
    if has_real_name:
        return f"needs {architecture.engine} ({architecture.name})"
    return f"needs {architecture.engine}"


def _engine_name(code: str) -> str | None:
    """The display name for the engine runner `code` belongs to (e.g.
    `"Diffusers"`, `"MLX FLUX"`) — used only to name the engine in the
    neutral "Runs on <Engine>" info chip (finding 1 of the follow-up
    review). `None` when the registry does not recognise `code` or the
    runner has no `family_label` — this must never raise, only read as
    "nothing to name"."""
    from fused_render.ai import registry

    runner = registry.by_code(code)
    if runner is None or not runner.family_label:
        return None
    return runner.family_label


def admission(*, runner_codes: tuple[str, ...], model_id: str,
              model_type: str | None,
              names: frozenset[str] = frozenset(),
              library_name: str | None = None,
              base_model: str | None = None,
              architecture: Architecture | None = None,
              active_runner_code: str | None = None,
              ) -> tuple[bool, str | None, str | None]:
    """`(loadable, reason, runs_on)` for one row, judged against EVERY
    runner AVAILABLE for its capability right now (`runner_codes` — the
    caller already resolved `registry.available_runners`; this module has
    no opinion on which runners are registered, only on what a given runner
    code can open).

    Loadable the moment ANY available runner would admit it — a row is only
    flagged when NONE of them would. `reason` is the short clause the
    frontend's chip shows after "Won't run here · " — never that full
    sentence itself, so the two stay reusable independently. Always `None`
    when `loadable` is True.

    `runs_on`: the follow-up review's finding 1. Widening admission from the
    single ACTIVE runner to every AVAILABLE one (D1287 item 3) silenced the
    "won't run here" chip for a row that the active runner refuses but
    another available runner admits — correct for `loadable`/`reason` (a
    download still uses the ACTIVE runner via `supervisor.load`'s own
    `_runner_or_raise`, untouched here), but it also silently dropped the
    fact that downloading this row means switching engines first. `runs_on`
    names that other engine (e.g. `"Diffusers"`) ONLY when: (a) the caller
    passes `active_runner_code` (the row's own ACTIVE runner — omitting it,
    as every pre-existing caller does, always reads `runs_on` as `None`,
    preserving old behaviour exactly), (b) at least one runner admits, and
    (c) the active one is not among the admitting runners. It is a plain,
    non-warning fact — never a reason to flag the row, never consulted by
    ranking/sorting/facets/hidden counts/family pull-in.

    `names`: the row's own sibling filenames (top-level), needed for
    `ltx-video`'s `"file_layout"` kind and the Diffusers runners' own
    `"diffusers"` kind — empty for a caller that has not read a repo's file
    listing (every OTHER kind ignores it).

    `library_name`: the row's own `raw["library_name"]`, needed ONLY by the
    Diffusers runners' `"diffusers"` kind (round: "Diffusers admission is
    unconditional") — `None` for a caller that has not read it. Neither
    `names` nor `library_name` being absent flags a row; see `_admits`'s
    own `"diffusers"` branch for the explicit never-drops-a-row guard.

    `base_model`: the row's own `base_model:<relation>:<id>` tag, already
    parsed by the caller (`hub_architecture.parse_base_model_tag`) — needed
    ONLY by `mflux-image`'s `"mflux"` kind, mirroring mflux's own
    `explicit_base` resolution rule: a quantized `mlx-community` conversion
    is never itself a registry entry, but the canonical repo it was
    converted from usually is. `None` for a caller that has not parsed it
    (every pre-existing caller), which the `"mflux"` branch simply skips —
    the row's own id can still admit it via the `exact_match` rule alone.

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
        return True, None, None
    refusals: list[tuple[str, str, str | None]] = []
    admitting: list[str] = []
    for code in runner_codes:
        kind, data = loadable_kind(code)
        loadable, bespoke_reason = _admits(
            kind, data, model_id=model_id, model_type=model_type, names=names,
            library_name=library_name, base_model=base_model)
        if loadable:
            admitting.append(code)
        else:
            refusals.append((code, kind, bespoke_reason))
    if not admitting:
        if len(refusals) == 1:
            _code, kind, bespoke_reason = refusals[0]
            if kind == "model_types" and bespoke_reason is not None:
                return False, bespoke_reason, None
        return False, _generic_reason(architecture), None
    if active_runner_code is None or active_runner_code in admitting:
        return True, None, None
    runs_on: str | None = None
    for code in admitting:
        runs_on = _engine_name(code)
        if runs_on is not None:
            break
    return True, None, runs_on
