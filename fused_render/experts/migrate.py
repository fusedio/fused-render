"""One-time migration: experts were called specialists until 2026-11-23.

`<workspace>/specialists` becomes `<workspace>/experts`, with every home in it: memory, skills, inbox, the home's
git history and the root's `.state/` (tokens, the seeded list, trash). The owner's rules (2026-11-23):

- Whatever already sits in `<workspace>/experts` is deleted first; the specialists are the record.
- Memory and skills are what matters. The chats are carried too (`claude_session_move.relocate`, which also
  repoints the cwd inside each transcript), because a chat left under the old folder would be a dead row on the
  page: the main chat and the earlier chats are named by session id in `.expert/state.json`.
- Nothing the page shows may point at the old folder afterwards: in each home `specialist.json` becomes
  `expert.json` and `.specialist/` becomes `.expert/`, `.gitignore` and the charter in `CLAUDE.md` are rewritten,
  and the transcript paths kept in `state.json` and in the inbox pointer files are repointed. The server's own
  stores that name a folder (recents, bookmarks, the desk) are re-rooted by `app_state_move.rewrite_stores`.

The stored switch moves with it: `specialists_enabled` in prefs.json becomes `experts_enabled`.

Runs once per machine, before the shipped experts are seeded, whether the switch is on or off. The stamp
(`<home>/experts_migration.json`) is written even when there was nothing to move, so an old build that later
recreates `<workspace>/specialists` can never cause the delete above to run against experts in use. A move that
raised is not stamped and is tried again on the next start. Never raises past `run_once`.
"""
from __future__ import annotations

import logging
import os
import shutil

from fused_render.experts import home as hm
from fused_render.experts import paths

logger = logging.getLogger(__name__)

_STAMP_NAME = "experts_migration.json"
OLD_ROOT = "specialists"
OLD_MANIFEST = "specialist.json"
OLD_APP_DIR = ".specialist"
OLD_PREF = "specialists_enabled"


def _stamp_path() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), _STAMP_NAME)


def _old_root() -> str:
    return os.path.join(os.path.dirname(paths.root()), OLD_ROOT)


def _move_pref() -> None:
    from fused_render.shell import prefs

    with prefs._PREFS_LOCK:
        data = prefs.read_prefs()
        if OLD_PREF not in data:
            return
        data["experts_enabled"] = data.pop(OLD_PREF)
        prefs._store(data)


def _rename(folder: str, old: str, new: str) -> None:
    src, dst = os.path.join(folder, old), os.path.join(folder, new)
    if os.path.lexists(src) and not os.path.lexists(dst):
        os.rename(src, dst)


def _repoint(path: str, old: str, new: str) -> None:
    text = hm.read_text(path)
    if old in text:
        hm.write_text(path, text.replace(old, new))


def _settle_home(home: str, old_home: str) -> None:
    """One moved home: its two renamed entries, then every file of the app's that names the old folder."""
    from fused_render import claude_session_move as csm
    from fused_render.experts import chat, history

    _rename(home, OLD_MANIFEST, paths.MANIFEST)
    _rename(home, OLD_APP_DIR, paths.APP_DIR)
    hm.write_text(os.path.join(home, ".gitignore"), hm.GITIGNORE)
    man = hm.read_json(os.path.join(home, paths.MANIFEST), {})
    if man.get("name"):
        hm.rewrite_charter(home, man["name"], man.get("about") or "")
    # The subject section under the charter is kept as written, and a shipped one names its neighbours by the old
    # word. Memory and skills are the expert's own text and are left as they are.
    for old, new in (("specialist", "expert"), ("Specialist", "Expert")):
        _repoint(os.path.join(home, "CLAUDE.md"), old, new)
    # A transcript is named by its bucket (the cwd, munged) or, in text the session wrote, by the folder itself.
    pairs = ((csm.munge(old_home), csm.munge(home)), (old_home, home))
    files = [os.path.join(home, paths.APP_DIR, "state.json")]
    for sub in ("inbox", "inbox-archive"):
        try:
            files += [os.path.join(home, sub, n) for n in os.listdir(os.path.join(home, sub))]
        except OSError:
            pass
    for path in files:
        for old, new in pairs:
            _repoint(path, old, new)

    # A chat that could not be carried (a turn was open in it) would be a dead row: the page starts a new one.
    def here(sid) -> bool:
        return bool(sid) and os.path.isfile(chat.transcript(home, sid))

    def prune(d):
        d["folded"] = [f for f in d["folded"] if here(f.get("session_id"))]
        if not here(d["main"]):
            d["main"] = None
    chat.change(home, prune)
    history.commit(home, "Specialists are now experts")


def migrate() -> bool:
    """Move the old root across. True when there was one."""
    from fused_render import app_state_move, claude_session_move

    old, new = _old_root(), paths.root()
    if not os.path.isdir(old):
        return False
    if os.path.lexists(new):
        shutil.rmtree(new) if os.path.isdir(new) and not os.path.islink(new) else os.remove(new)
    os.rename(old, new)
    paths._reset()
    app_state_move.rewrite_stores(old, new)
    result = claude_session_move.relocate(old, new)
    for name in sorted(os.listdir(new)):
        home = os.path.join(new, name)
        if name.startswith(".") or not os.path.isdir(home):
            continue
        try:
            _settle_home(home, os.path.join(old, name))
        except Exception:  # noqa: BLE001 — one home's leftovers must not stop the others
            logger.exception("experts migration: could not settle %s", home)
    paths._reset()
    logger.info("experts migration: moved %s to %s (%d chats carried, %d still running)",
                old, new, len(result["moved"]), len(result["pending"]))
    return True


def run_once() -> None:
    stamp = _stamp_path()
    try:
        if hm.read_json(stamp, {}).get("done"):
            return
        _move_pref()
        moved = migrate()
        hm.write_json(stamp, {"done": True, "moved": moved})
    except Exception:  # noqa: BLE001 — the server still starts; the next start tries again
        logger.exception("experts migration failed")
