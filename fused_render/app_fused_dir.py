"""The `.fused/` folder: an app's own place to keep state (D548, SPEC §47).

An app folder is authored content — the entry `.html`, its `.py` data files,
assets. Anything the app *accumulates while running* had nowhere to go, so
pages invented a place each time: a JSON file dropped beside `index.html`, a
scratch dir under the workspace, `~/.myapp`. All three are wrong in the same
way — the state is not authored content, it is not portable, and nothing else
in the system knows to leave it alone.

The convention is one hidden folder at the app's root::

    <app>/.fused/
        data/       persistent state the app OWNS  — survives forever
        cache/      derived bytes the app can REBUILD — safe to delete
        meta.json   {"version": 1, "app_dir": "<abs>", "created_at": "<iso>",
                     "migrations": [...]}   # only once the folder has moved

`data` vs `cache` is the whole point of the split, and it is a promise made to
the user, not a naming preference: everything under `cache/` must be
reconstructible from `data/` plus the outside world, so a "clear the cache"
sweep (by the user, by us, by a disk cleaner) can never destroy something the
app cannot get back. Anything that fails that test is data.

**`meta.json` records where the app was set up.** The live path always wins —
this module resolves everything from the `app_dir` argument it is given — so
the recorded path is not a lookup key, it is a WITNESS. A mismatch means the
folder was moved or copied since the state was created, which is the one fact
an app cannot otherwise learn about itself, and it is exactly the moment a
path-keyed cache entry or an absolute path stored in `data/` has quietly gone
stale. A COPY (the recorded folder still exists) leaves the record alone;
deciding what it means is the app's call. A MOVE (the recorded folder is gone)
is settled by the server: the Claude sessions about the old path and its
subtree are carried to the new one (`claude_session_move.relocate`), and once
they all are, `app_dir` is repointed and the move appended to `migrations`
(`[{"from", "to", "at", "sessions"}]`) — the evidence is kept, not erased. A
relocate that had to leave a live session behind leaves the record stale so
the next open retries.

Creation is the SERVER's job, not the app's: `record_app_open` calls `ensure`
whenever a page carrying the fused-app marker is rendered (routers/apps.py), so
every app has the folders the first time it is opened — including the ones
authored before this convention existed, which is most of them. An app
therefore never has to `makedirs` before its first write, and the convention
holds for hand-made folders that never went near the starter kit.

Nothing here is exported: `.fused` is a hidden name, so `appfile._iter_app_files`
already drops it from a `.fused` app file, and `app_git._GITIGNORE` keeps it out
of the app's git history. Machine-local state stays on the machine.

Best-effort throughout, like `app_git`: an app folder on a read-only medium, a
permission error, a wedged mount — none of that may fail the render that
triggered it. `ensure` returns False and says why at DEBUG level.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The folder's name at the app root. Hidden, so every walk in this codebase
#: that skips dotted names (app_listing's workspace walk, appfile's exporter)
#: already leaves it alone with no new rule.
DIRNAME = ".fused"
DATA = "data"
CACHE = "cache"
META = "meta.json"

#: Bumped only for a change a reader must branch on. `version` exists so an
#: app that finds an unfamiliar number can decline rather than misread.
META_VERSION = 1


def dot_fused(app_dir: str) -> str:
    """`<app_dir>/.fused` — the convention's root for this app."""
    return os.path.join(app_dir, DIRNAME)


def data_dir(app_dir: str) -> str:
    """Where the app keeps state it cannot rebuild."""
    return os.path.join(dot_fused(app_dir), DATA)


def cache_dir(app_dir: str) -> str:
    """Where the app keeps bytes it can rebuild. Deletable at any time."""
    return os.path.join(dot_fused(app_dir), CACHE)


def meta_path(app_dir: str) -> str:
    return os.path.join(dot_fused(app_dir), META)


def read_meta(app_dir: str) -> dict | None:
    """The parsed `meta.json`, or None when there is none to read.

    None for every failure — absent, unreadable, malformed, or holding
    something that is not an object — because a caller can do nothing
    different with those four, and this file is user-writable so all four
    are reachable without a bug.
    """
    try:
        with open(meta_path(app_dir), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def recorded_app_dir(app_dir: str) -> str | None:
    """The path `meta.json` says this app lives at, or None if unrecorded.

    Compare it against `app_dir` to learn whether the folder has been moved or
    copied since its state was created — see the module docstring on why this
    is a witness rather than a lookup.
    """
    meta = read_meta(app_dir)
    if meta is None:
        return None
    recorded = meta.get("app_dir")
    return recorded if isinstance(recorded, str) and recorded else None


def ensure(app_dir: str) -> bool:
    """Materialise `.fused/data`, `.fused/cache` and `.fused/meta.json` under
    `app_dir`. True when the folder is in place afterwards.

    Idempotent and additive: existing directories are left alone, and an
    existing `meta.json` is rewritten only to settle a MOVE after its sessions
    were carried over (`_after_move`); a copy's divergence is left for the app
    to see (module docstring). Otherwise only a missing file is written.

    Refuses a mount-backed folder outright. A remote mount is not an app's
    private disk, `makedirs` on one is a network round trip on the render path,
    and a recursive-walk-shaped access pattern against a wedged mount is how
    this codebase has repeatedly killed the mount itself. The prefix check is
    pure string work, so the common local case pays nothing for it.
    """
    try:
        if not os.path.isdir(app_dir):
            logger.debug("ensure .fused skipped: %s is not a directory", app_dir)
            return False
        from fused_render.shell import mounts as shell_mounts

        if shell_mounts.is_mount_backed(app_dir):
            logger.debug("ensure .fused skipped: %s is mount-backed", app_dir)
            return False
        os.makedirs(data_dir(app_dir), exist_ok=True)
        os.makedirs(cache_dir(app_dir), exist_ok=True)
        _ensure_meta(app_dir)
        return True
    except Exception:
        logger.debug("ensure .fused failed for %s", app_dir, exc_info=True)
        return False


def _ensure_meta(app_dir: str) -> None:
    """Write `meta.json` if and only if there isn't a readable one already.

    `x` mode, not a check-then-write: two renders of the same app can arrive
    concurrently, and losing that race is a normal outcome, not an error — the
    winner wrote the same three fields. A file that exists but does not parse
    is left as it is too; overwriting it would destroy whatever the user (or a
    future version) put there, and nothing here needs it to succeed.
    """
    recorded = recorded_app_dir(app_dir)
    if recorded is not None:
        if os.path.normcase(os.path.abspath(recorded)) != os.path.normcase(
                os.path.abspath(app_dir)):
            # The witness fired: this folder is not where its state was made.
            logger.info(
                ".fused/meta.json in %s records app_dir=%s — the app was moved "
                "or copied", app_dir, recorded)
            _after_move(app_dir, recorded)
        return
    if os.path.exists(meta_path(app_dir)):
        return  # present but unreadable/malformed — the user's file, not ours
    meta = {
        "version": META_VERSION,
        "app_dir": os.path.abspath(app_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(meta_path(app_dir), "x", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            f.write("\n")
    except FileExistsError:
        pass


def _after_move(app_dir: str, recorded: str) -> None:
    """The witness fired. Decide copy from move, and settle a move.

    The recorded folder still existing means this is a COPY — the sessions
    belong to the original, and the record stays as evidence for the app. Gone
    means MOVED: the Claude sessions about the old path (and any folder under
    it) are carried to the new one (`claude_session_move.relocate`), and only
    once every one of them has gone across is `meta.json` repointed — with the
    move appended to `migrations`, so the evidence the witness held is kept
    rather than erased. A relocate that had to leave a live session behind
    leaves the record stale too, and the next open tries again.
    """
    if os.path.isdir(recorded):
        logger.info("%s still exists — a copy, not a move; record left intact", recorded)
        return
    from fused_render import claude_session_move

    new = os.path.abspath(app_dir)
    meta = read_meta(app_dir) or {}
    migrations = meta.get("migrations")
    if not isinstance(migrations, list):
        migrations = []
    # Every place the sessions may be sitting: the recorded origin, plus the
    # destination of each earlier hop that could not finish (a live session
    # held it, the folder moved again before it ended). Those hops already
    # carried the idle transcripts to their intermediate path, so a search
    # from the origin alone would never see them again.
    sources = [recorded] + [m["to"] for m in migrations
                            if isinstance(m, dict) and m.get("pending")
                            and isinstance(m.get("to"), str)]
    moved, pending = [], []
    for source in dict.fromkeys(sources):
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(new):
            continue
        result = claude_session_move.relocate(source, new)
        moved += result["moved"]
        pending += result["pending"]
    entry = {
        "from": recorded,
        "to": new,
        "at": datetime.now(timezone.utc).isoformat(),
        "sessions": len(moved),
    }
    if pending:
        # Recorded so the next hop knows to look here too; `app_dir` stays
        # at the origin so the witness keeps firing until this is settled.
        entry["pending"] = pending
        migrations.append(entry)
        meta["migrations"] = migrations
        logger.info("app moved %s -> %s: %d session(s) still live, retrying "
                    "on the next open", recorded, app_dir, len(pending))
    else:
        for m in migrations:
            if isinstance(m, dict):
                m.pop("pending", None)
        migrations.append(entry)
        meta.update(app_dir=new, migrations=migrations)
        logger.info("app moved %s -> %s: %d Claude session(s) carried over",
                    recorded, app_dir, len(moved))
    tmp = meta_path(app_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.replace(tmp, meta_path(app_dir))
