"""A third-party index manifest — the declaration surface a folder uses to
register its own `IndexKind`, plus the propose/confirm store that keeps
adding one "app proposes, user confirms" rather than silent
(SPEC-index-plugins.md decision #8).

The manifest is its own TOML table, not a new key on `[tool.fused-render.app]`
(`background_apps.load_manifest`'s table): an indexer is a different kind of
opt-in from a background daemon — it runs at every scan, not on demand — and
conflating the two would mean a folder declaring only `daemon =` suddenly
also needed to say "and no, don't index me", when saying nothing already
means that today.

    [tool.fused-render.index]
    module = "indexer.py"   # resolved inside the folder, like app.daemon/app.main
    kind = "widgets"        # the IndexKind name this module's register_kind() adds

`module` is expected to expose a `register_kind(*, replace: bool = False)`
function that calls `fused_render.index.kinds.register` — never a bare
`register`, which the module's own `from fused_render.index.kinds import
... register` (the shape the reference plugin uses) already binds to
something else in the module's namespace.

`module` is intentionally NOT imported by this file. Parsing a manifest is
cheap, side-effect-free introspection — the same posture `load_manifest`
already has for background apps — while actually importing a third party's
`indexer.py` executes arbitrary code, which is exactly the kind of act
decision #8 says must never happen silently. Importing (and calling
whatever the module needs to register its `IndexKind`) is the CALLER's job,
gated on `confirmed_folders()` — this module answers "is there a valid,
well-formed declaration here", never "run it".
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from dataclasses import dataclass

from fused_render.index import kinds
from fused_render.shell import storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexManifest:
    #: Absolute path to the app folder (the `pyproject.toml`'s directory).
    folder: str
    #: Absolute path to the module the folder declares, resolved and
    #: containment-checked the same way `background_apps._resolve` does.
    module: str
    #: The `IndexKind` name this module's own registration call is expected
    #: to add — carried here so a caller can verify after import that the
    #: module did what it declared, rather than trust it silently.
    kind: str


def load_manifest(folder: str) -> IndexManifest | None:
    """The folder's index manifest, or None when it does not declare one,
    declares an unresolvable `module`, or omits `kind`. Never raises — same
    posture as `background_apps.load_manifest`: a missing/corrupt
    `pyproject.toml` or an unreadable folder is simply "no manifest"."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            logger.warning(
                "neither tomllib (Python 3.11+) nor tomli is available; "
                "pyproject.toml files cannot be read"
            )
            return None
    folder = os.path.abspath(folder)
    pyproject = os.path.join(folder, "pyproject.toml")
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    tool = data.get("tool")
    table = tool.get("fused-render") if isinstance(tool, dict) else None
    section = table.get("index") if isinstance(table, dict) else None
    if not isinstance(section, dict):
        return None
    module_name = section.get("module")
    kind = section.get("kind")
    if not isinstance(module_name, str) or not module_name:
        return None
    if not isinstance(kind, str) or not kind:
        return None

    target = os.path.normpath(os.path.join(folder, module_name))
    real_folder = os.path.realpath(folder)
    real_target = os.path.realpath(target)
    # Containment, realpath-resolved — the same guard shape as
    # `background_apps.load_manifest._resolve`: a `module` value that climbs
    # out of the folder via `../` or a symlink is refused rather than trusted
    # just because the string join looked contained.
    if real_target != real_folder and not real_target.startswith(real_folder + os.sep):
        return None
    if not os.path.isfile(real_target):
        return None
    return IndexManifest(folder=folder, module=target, kind=kind)


# --------------------------------------------------- propose/confirm store


def _store_path() -> str:
    return os.path.join(storage.home_dir(), "index_proposals.json")


def _read() -> dict:
    data = storage.read_json(_store_path())
    if not isinstance(data, dict):
        return {"pending": [], "confirmed": []}
    pending = data.get("pending")
    confirmed = data.get("confirmed")
    return {
        "pending": [p for p in pending if isinstance(p, str)] if isinstance(pending, list) else [],
        "confirmed": [p for p in confirmed if isinstance(p, str)] if isinstance(confirmed, list) else [],
    }


def _write(pending: list, confirmed: list) -> None:
    storage.write_json(_store_path(), {"pending": pending, "confirmed": confirmed})


def pending_folders() -> list[str]:
    """Folders proposed but not yet confirmed by the user, realpath-normalized."""
    return list(_read()["pending"])


def confirmed_folders() -> list[str]:
    """Folders the user has confirmed — the ONLY ones a caller may actually
    import and register."""
    return list(_read()["confirmed"])


def propose_index(folder: str) -> bool:
    """Record *folder* as proposing an index, so the user can be asked to
    confirm it. Returns False (nothing recorded) when *folder* has no valid
    `[tool.fused-render.index]` manifest — a folder cannot propose what it
    hasn't declared.

    Sticky confirmation: a folder the user already confirmed stays
    confirmed — re-proposing it (an app re-announcing itself on every
    startup, say) must not demote it back to "pending" and reprompt. That
    would defeat the entire point of "confirmed" meaning confirmed, and it
    is exactly the reprompt-fatigue decision #8 exists to prevent."""
    if load_manifest(folder) is None:
        return False
    real = os.path.realpath(folder)
    state = _read()
    if real in state["confirmed"]:
        return True
    if real not in state["pending"]:
        state["pending"].append(real)
        _write(state["pending"], state["confirmed"])
    return True


def confirm_index(folder: str) -> bool:
    """Move *folder* from pending to confirmed. Returns False when *folder*
    was never proposed and is not already confirmed — there is nothing here
    for the user to have confirmed. Idempotent: confirming an
    already-confirmed folder is a no-op success."""
    real = os.path.realpath(folder)
    state = _read()
    if real in state["confirmed"]:
        return True
    if real not in state["pending"]:
        return False
    pending = [p for p in state["pending"] if p != real]
    confirmed = state["confirmed"] + [real]
    _write(pending, confirmed)
    return True


def refuse_index(folder: str) -> None:
    """Remove *folder* from both pending and confirmed — a user's "no" to a
    proposal, or a later revocation of an index they had confirmed. Never
    raises; refusing a folder that was in neither list is a no-op."""
    real = os.path.realpath(folder)
    state = _read()
    pending = [p for p in state["pending"] if p != real]
    confirmed = [p for p in state["confirmed"] if p != real]
    _write(pending, confirmed)


# ------------------------------------------------------- import + register
#
# The other half of decision #8: `confirmed_folders()` names the folders a
# user has granted, but nothing was actually IMPORTING and registering their
# `IndexKind` — a confirmed proposal only ever rewrote this file's own JSON
# store, so a confirmed kind never appeared in `kinds.registered()` (and so
# never in `GET /api/index/kinds`) and could never be scanned. This is the
# "caller" the module docstring above always deferred to: called once from
# `routers/index_manifest.py`'s confirm route (so the server process that
# just handled the confirm sees the kind immediately), once at
# `routers/index.py` import time (so a server that restarts with an
# already-confirmed folder still has it), and once from `worker.py` (the
# detached scan subprocess never imports either of those, so it has to do
# its own registration too).


def _call_register(fn) -> None:
    """Call a plugin module's `register_kind` entrypoint. `replace=True`
    when it accepts one — a server/worker restart, or re-confirming an
    already-confirmed folder, must re-register cleanly rather than raise on
    a name collision, the same reason `kinds.register`'s own `replace` flag
    exists — falling back to a bare call for a
    simpler `register_kind()` with no such parameter (the shape
    tests/test_index_manifest.py's own fixtures use)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "replace" in params:
        fn(replace=True)
    else:
        fn()


def _import_and_register(folder: str) -> bool:
    """Import *folder*'s declared module and register its `IndexKind`.
    Returns whether `manifest.kind` ended up in `kinds.registered()`
    afterward. Best-effort and never raises: a confirmed folder whose
    manifest has since gone missing or invalid, whose module fails to
    import, whose `register_kind` raises, or that simply never calls
    `kinds.register` despite declaring a `kind` name, is logged and skipped
    — the same "a plugin must never take the host down" rule `extract()`
    itself follows, applied to import time instead of scan time.

    The entry point a module must expose is `register_kind(*, replace=False)`
    — a name deliberately DIFFERENT from `kinds.register`, the name the
    reference plugin (and every real-shaped one) imports at module scope
    via `from fused_render.index.kinds import ... register`. Looking up a
    bare `register` here used to pick up that IMPORTED name whenever a
    plugin's own entrypoint happened to be called anything else (the
    reference plugin's own shape) — `getattr` returned `kinds.register`
    itself, called as `fn(replace=True)`, raising `TypeError: register()
    missing 1 required positional argument: 'kind'`, silently swallowed
    below. `register_kind` cannot collide with that import."""
    m = load_manifest(folder)
    if m is None:
        logger.warning(
            "confirmed folder %s no longer has a valid index manifest; "
            "not registering", folder)
        return False
    if m.kind in kinds.registered():
        return True  # already registered in this process — nothing to do
    # A unique, never-reused module name per folder: two confirmed folders
    # could otherwise declare a `module` with the same basename (both named
    # `indexer.py`), and `importlib`'s own module cache is keyed by name, not
    # by path — reusing one would silently serve the wrong folder's module on
    # a second confirm. Never a package-style `import`, since a third
    # party's folder is never on `sys.path` (specs/index-plugins.md §5).
    mod_name = "_fused_render_index_plugin_" + str(abs(hash(m.module)))
    try:
        spec = importlib.util.spec_from_file_location(mod_name, m.module)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {m.module}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        register_fn = getattr(module, "register_kind", None)
        if callable(register_fn):
            _call_register(register_fn)
    except Exception:
        logger.exception(
            "failed to import/register confirmed index module %s "
            "(folder %s)", m.module, folder)
        return False
    if m.kind not in kinds.registered():
        logger.warning(
            "confirmed folder %s declared kind %r but importing %s did not "
            "register it", folder, m.kind, m.module)
        return False
    return True


def register_confirmed_kinds() -> None:
    """Import and register every confirmed folder's declared `IndexKind`,
    in THIS process. Safe to call repeatedly (a no-op past the first
    successful import of a given kind) and safe to call with nothing
    confirmed (a no-op entirely) — cheap enough to call unconditionally at
    import time in every process that needs a confirmed kind registered."""
    for folder in confirmed_folders():
        _import_and_register(folder)
