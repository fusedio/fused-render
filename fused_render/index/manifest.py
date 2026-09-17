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
    kind = "widgets"        # the IndexKind name this module's register() adds

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

import logging
import os
from dataclasses import dataclass

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
