"""Where the bots keep their state (docs/bots.md §1).

Two roots. Everything that belongs to ONE bot lives in that bot's folder in
the user's workspace; machine state shared across bots stays in the app home:

    <workspace>/bots/<folder>/            bot_folder(bid)   PUBLIC (shareable, git-friendly)
        bot.json                          the public half of the bot's meta (store.PUBLIC_KEYS + its `_gen` stamp)
        skills/<name>.md  README.md       skills; README is the user's, never generated
        .gitignore                        ".fused/\n", seeded once by place()
        .fused/                           bot_dir(bid) == bot_private(bid)   PRIVATE
            state.json  events.jsonl      every other meta key (+ `_public_gen`); the transcript
            memory.md  files/  downloads/
            inbox/<task>/                 bot_inbox(bid): the Inbox (index.jsonl lives here); never drained
            drop/*.txt                    bot_drop(bid): botsend.py's drop folder, drained into tasks by the scheduler
            cache/                        bot_cache_dir(bid): shot.png, window.json, steps/, … (deletable)

    <home>/bots/data/browsers/<bid>/      profile, browser.json (browsers.py; may be shared by bots)
    <home>/bots/cache/browsers/<bid>/     session.json
    <home>/bots/data/usage.jsonl          the usage ledger
    <home>/bots/data/builds.json          the Builds panel's list
    <home>/bots/data/imessage*.json       the iMessage bridge's cursor, state and lock
    <home>/bots/cache/slow.jsonl          slow-call log

`<workspace>` is `~/Fused` (`FUSED_RENDER_DIR` overrides, as everywhere in
fused-render); `<home>` is `shell.storage.home_dir()` (`~/.fused-render`,
`FUSED_RENDER_HOME` overrides, branch-nested in a dev worktree). A folder is
named for the bot's name when it is created (`place()`) and never renamed
after: the `id` (8 hex, the API key everywhere) is read from bot.json, so the
id -> folder map is a scan of `<workspace>/bots/*/bot.json` (`folders()`).

Legacy: until bots v3 a bot lived at `<home>/bots/data/bots/<id>/` +
`<home>/bots/cache/bots/<id>/` with its Inbox at `<workspace>/bots/<slug>/`.
`migrate_v3` lifts that tree into the layout above (backup first);
`legacy_data_dir()`/`legacy_cache_dir()` name it for the migration and the
FusedBot import, nothing else. FusedBot 0.11.x shipped an even flatter
layout (`bots/data/<id>`, `bots/cache/<id>`, the ledgers at `bots/`);
`migrate_layout()` still moves such a tree into the legacy layout first.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat as stat_mod
import threading
import time

from fused_render.shell import storage as _storage

log = logging.getLogger(__name__)


def root() -> str:
    """`<home>/bots` — made on first use."""
    p = os.path.join(_storage.home_dir(), "bots")
    os.makedirs(p, exist_ok=True)
    return p


def data_root() -> str:
    """`<home>/bots/data`: the bots folder plus the ledgers (OpenBot `.fused/data`)."""
    p = os.path.join(root(), "data")
    os.makedirs(p, exist_ok=True)
    return p


def cache_root() -> str:
    """`<home>/bots/cache`: the bots' caches plus slow.jsonl (OpenBot `.fused/cache`)."""
    p = os.path.join(root(), "cache")
    os.makedirs(p, exist_ok=True)
    return p


def legacy_data_dir() -> str:
    """`<home>/bots/data/bots`: where a pre-v3 bot's bot.json, events.jsonl, memory.md, skills/ … lived.
    Only migrate_v3 (reads it) and fusedbot_import (writes the legacy layout that migrate_v3 then lifts) use it;
    not made here, so probing it never resurrects a tree the migration removed."""
    return os.path.join(root(), "data", "bots")


def legacy_cache_dir() -> str:
    """`<home>/bots/cache/bots`: a pre-v3 bot's shot.png, session.json, steps/. Not made here (see legacy_data_dir)."""
    return os.path.join(root(), "cache", "bots")


def usage_path() -> str:
    """One line per model call (the usage ledger): `<home>/bots/data/usage.jsonl`."""
    return os.path.join(data_root(), "usage.jsonl")


def slow_log_path() -> str:
    """`<home>/bots/cache/slow.jsonl` (beside the bots' caches, not inside them)."""
    return os.path.join(cache_root(), "slow.jsonl")


def builds_path() -> str:
    """The Builds panel's list (`GET/POST /api/bots/builds`): `<home>/bots/data/builds.json`."""
    return os.path.join(data_root(), "builds.json")


def imessage_dir() -> str:
    """imessage.json (cursor), imessage.lock, imessage-state.json: `<home>/bots/data`."""
    return data_root()


# ----------------------------------------------------- 0.11.x layout move ---
_LEDGERS = ("usage.jsonl", "builds.json", "imessage.json", "imessage-state.json")
# Written under bots/ once a pass found nothing left to move: the sweep never runs again on that install, so a
# folder a later feature adds under data/ or cache/ (browsers/, #1483) can never be mistaken for a 0.11.x bot folder.
_LAYOUT_DONE = ".layout-v2"


def migrate_layout() -> int:
    """One-shot: move a 0.11.x home (`bots/data/<id>`, `bots/cache/<id>`,
    ledgers at `bots/`) to the OpenBot layout above. Returns how many entries
    moved. Idempotent; never overwrites (an occupied destination is logged and
    the source left alone); never raises. Runs at server startup (the
    `_startup_bots` hook in fused_render/server/app.py) BEFORE
    `registry.start()`. No bot runs yet at that point, so no Chrome holds a
    profile open."""
    log = logging.getLogger(__name__)
    moved = 0
    try:
        base = os.path.join(_storage.home_dir(), "bots")
        if not os.path.isdir(base) or os.path.exists(os.path.join(base, _LAYOUT_DONE)):
            return 0
        for sub in ("data", "cache"):
            old = os.path.join(base, sub)
            if not os.path.isdir(old):
                continue
            new = os.path.join(old, "bots")
            for name in sorted(os.listdir(old)):
                src = os.path.join(old, name)
                # `browsers` is a folder of the NEW layout (browsers.py: data/browsers/<id>/profile, cache/browsers/<id>/
                # session.json); moving it under cache/bots/ loses every shared browser's session.json, and the next
                # launch then collides with the Chrome still holding the profile ("Chrome did not come up").
                if name == "bots" or name.startswith(".") or not os.path.isdir(src):
                    continue
                # Only a bot's own folder is a 0.11.x leftover: under data/ it carries bot.json; under cache/ it is
                # named for a bot that exists (data/ moved first). `browsers` (#1483) and any folder a later feature
                # adds stay where they are.
                if sub == "data" and not os.path.exists(os.path.join(src, "bot.json")):
                    continue  # not a bot folder; leave it
                if sub == "cache" and not (os.path.isdir(os.path.join(base, "data", "bots", name))
                                           or os.path.isfile(os.path.join(base, "data", name, "bot.json"))):
                    continue  # not a bot's cache; leave it
                os.makedirs(new, exist_ok=True)
                dst = os.path.join(new, name)
                if os.path.exists(dst):
                    log.warning("bots layout: %s exists, leaving %s in place", dst, src)
                    continue
                shutil.move(src, dst)
                moved += 1
        for name in _LEDGERS:
            src, dst = os.path.join(base, name), os.path.join(base, "data", name)
            if not os.path.isfile(src):
                continue
            if os.path.exists(dst):
                log.warning("bots layout: %s exists, leaving %s in place", dst, src)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved += 1
        stale_lock = os.path.join(base, "imessage.lock")
        if os.path.isfile(stale_lock):
            os.remove(stale_lock)  # the lock holder recreates it at the new path
        if moved:
            log.info("bots layout: moved %d entries to the OpenBot layout under %s", moved, base)
        else:
            # Nothing left of the old layout: mark it so this never runs again (a pass that moved something runs
            # once more at the next boot, in case an occupied destination left a source behind).
            with open(os.path.join(base, _LAYOUT_DONE), "w", encoding="utf-8") as f:
                f.write("1\n")
    except Exception:  # noqa: BLE001
        log.exception("bots layout: migration failed (continuing with what moved)")
    return moved


def workspace_dir() -> str:
    """fused-render's workspace root: `FUSED_RENDER_DIR`, else `~/Fused`
    (`fused_render.shell.seed.fused_dir`, the one resolution every other
    fused-render reader uses)."""
    from fused_render.shell.seed import fused_dir

    return fused_dir()


def apps_root() -> str:
    """`<workspace>/app`: where builds land and every bot's APPS live."""
    return os.path.join(workspace_dir(), "app")


def _bots_root_path() -> str:
    """`<workspace>/bots` without creating it: predicates (is_bot_workdir, called per transcript by the Tasks
    page) must not make `~/Fused/bots` appear on a machine that never made a bot."""
    return os.path.join(workspace_dir(), "bots")


def bots_root() -> str:
    """`<workspace>/bots`: one folder per bot (made on first use)."""
    p = _bots_root_path()
    os.makedirs(p, exist_ok=True)
    return p


# ------------------------------------------------------- id -> folder map ---
_META = "bot.json"
_PRIVATE = ".fused"
# `.fused/.bot-id`: the id place() wrote the moment it claimed the folder (one line, the 8-hex id). The only
# proof a bot.json-less folder is a half-made bot folder rather than a user's own folder that happens to hold a
# `.fused/` (an app, a clone): place(adopt=True) adopts nothing else. The id -> folder index still keys on bot.json.
_MARKER = ".bot-id"
_ID_RE = re.compile(r"[0-9a-f]{8}")
_index_lock = threading.RLock()
# The id -> folder map, per bots root: keyed by the root path so a test (or anything) that redirects
# FUSED_RENDER_DIR gets a fresh scan instead of another workspace's folders. `names` is the directory listing the
# map was built from: folders() relists (one listdir, what list_ids always cost) and rescans when it changed, so a
# folder dropped in or removed by hand is seen without a restart.
_index: dict = {"root": None, "map": {}, "names": None, "stat": {}}


def _reset_folders() -> None:
    """Drop the cached map (tests; anything that rewrote bots_root behind this process's back)."""
    with _index_lock:
        _index.update(root=None, map={}, names=None, stat={})


# (path, mtime_ns) of bot.json files already warned about: a scan reruns on every listing change and lookup miss,
# and one bad file must not repeat its warning each time (it warns again once it is edited).
_warned: set = set()
# bid -> monotonic time of the last rescan a MISS for it triggered: the page polling a deleted bot would
# otherwise rescan the whole root (one bot.json read per folder) on every call, under the registry lock.
_missed: dict = {}
_MISS_RESCAN_S = 2.0


def _skip(meta: str, why: str) -> None:
    try:
        key = (meta, os.stat(meta).st_mtime_ns)
    except OSError:
        key = (meta, None)
    if key not in _warned:
        _warned.add(key)
        log.warning("bots: skipping %s (%s)", meta, why)


def _read_id(meta: str) -> str | None:
    """The `id` of one bot.json, or None (with a warning, once per file version) when it is unreadable or not
    8 hex: a hand-edited or half-copied folder must not take the bots page down, nor pose as another bot."""
    try:
        with open(meta, encoding="utf-8") as f:
            bid = (json.load(f) or {}).get("id")
    except (OSError, ValueError, AttributeError) as e:
        _skip(meta, f"unreadable bot.json: {e}")
        return None
    if not isinstance(bid, str) or not _ID_RE.fullmatch(bid):
        _skip(meta, f"id {bid!r} is not 8 hex")
        return None
    return bid


def _scan(root: str) -> tuple[dict, frozenset]:
    """Build `{id: folder}` from `root/*/bot.json`. A folder counts only when it is a real directory (a symlink
    could point a "bot" anywhere on disk) holding a regular bot.json. Two folders claiming one id (a folder
    copied by hand): the first by name wins, the other is warned about. Raises OSError when `root` cannot be
    listed (list_ids(strict=True) must see that, not "no bots")."""
    out: dict[str, str] = {}
    dupes: dict[str, str] = {}
    with os.scandir(root) as it:
        entries = sorted(it, key=lambda e: e.name)
    names = frozenset(e.name for e in entries)
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        meta = os.path.join(e.path, _META)
        if os.path.islink(meta) or not os.path.isfile(meta):
            continue
        if os.path.islink(os.path.join(e.path, _PRIVATE)):
            _skip(meta, ".fused is a symlink; state, memory and inbox writes could escape the workspace")
            continue
        bid = _read_id(meta)
        if bid is None:
            continue
        if bid in out or bid in dupes:
            # A folder copied by hand carries the same id: resolving either would let the copy hijack the bot
            # (copy sorts first) or resurrect a deleted one on the next miss-rescan. Neither resolves until the
            # user removes one; warned once per pair.
            _skip(meta, f"id {bid} is also claimed by {dupes.get(bid) or out.get(bid)}; neither folder resolves")
            dupes.setdefault(bid, out.pop(bid, e.path))
            continue
        out[bid] = e.path
    return out, names


def raw_ids() -> set[str]:
    """Every id any folder on disk claims, read straight off disk with no index: for a minter or create(bot_id=)
    asking "is this id free?". The index (folders/list_ids) drops an id two folders claim (`_scan`), so it would
    read as free and a third folder could be written under it. Counts `<bots_root>/*/bot.json` (no symlink or
    `.fused` filters: anything claiming the id takes it), `.fused/.bot-id` markers (a placed folder whose
    bot.json is not written yet), and the legacy tree and `.unmigrated/` parked folders (the migration lifts
    those under their own id). Unreadable entries are skipped; never raises."""
    out: set[str] = set()

    def claim(meta: str) -> None:
        try:
            with open(meta, encoding="utf-8") as f:
                bid = (json.load(f) or {}).get("id")
        except (OSError, ValueError, AttributeError, TypeError, RecursionError):
            return
        if isinstance(bid, str) and _ID_RE.fullmatch(bid):
            out.add(bid)

    root = _bots_root_path()
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        entries = []
    for e in entries:
        if e.name.startswith("."):
            continue
        claim(os.path.join(e.path, _META))
        try:
            with open(os.path.join(e.path, _PRIVATE, _MARKER), encoding="utf-8") as f:
                text = f.read(64).strip()
            if _ID_RE.fullmatch(text):
                out.add(text)
        except (OSError, ValueError):
            pass
    for base in (legacy_data_dir(), os.path.join(root, ".unmigrated")):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            if _ID_RE.fullmatch(n):
                out.add(n)  # legacy and parked dirs are named by id
            claim(os.path.join(base, n, _META))
    return out


def _rescan_locked(root: str) -> None:
    """Rescan `root` into the cache. Keeps a folder place() registered whose bot.json is not written yet (place
    runs before store.write_meta), as long as its directory still exists and no scanned bot claims it."""
    fresh, names = _scan(root)
    if _index["root"] == root:
        claimed = set(fresh.values())
        for bid, folder in _index["map"].items():
            if bid in fresh or folder in claimed or not os.path.isdir(folder) or os.path.islink(folder):
                continue
            meta = os.path.join(folder, _META)
            if not os.path.exists(meta):
                fresh[bid] = folder  # placed, bot.json not written yet (store.write_meta follows place())
            elif not os.path.islink(meta) and _read_id(meta) is None:
                # A torn bot.json (an editor mid-save, a half copy): the cache said this folder was the bot and the
                # unreadable file is no evidence otherwise, so the live bot keeps resolving (its own save repairs it).
                fresh[bid] = folder
    _index.update(root=root, map=fresh, names=names)
    _index["stat"] = {k: v for k, v in _index["stat"].items() if k in fresh}


def folders(*, strict: bool = False) -> dict[str, str]:
    """`{id: folder path}` for every bot folder under bots_root(). Cached per process; relisted on each call and
    rescanned when the listing changed. `strict`: an unlistable bots_root raises OSError instead of reading as
    "no bots" (registry.all must not call an empty scan complete)."""
    root = bots_root()
    with _index_lock:
        try:
            names = frozenset(os.listdir(root))
            if _index["root"] != root or _index["names"] != names:
                _rescan_locked(root)
        except OSError:
            if strict:
                raise
            log.warning("bots: cannot list %s", root, exc_info=True)
            return {}
        return dict(_index["map"])


def bot_folder(bid: str) -> str:
    """`<workspace>/bots/<folder>`: the bot's PUBLIC folder. Raises LookupError when no folder carries `bid`
    (a cache miss, or a cached folder that is gone, rescans once first). Callers that ask "does this bot
    exist?" go through store.meta_path in a try/except LookupError."""
    if not bid or bid in (".", "..") or os.sep in bid or (os.altsep and os.altsep in bid):
        raise LookupError(f"no such bot {bid!r}")
    root = bots_root()
    with _index_lock:
        if _index["root"] == root:
            folder = _index["map"].get(bid)
            if folder and _still_holds(folder, bid):
                return folder
        # A miss rescans the whole root (a bot.json read per folder) at most once per _MISS_RESCAN_S per bid —
        # unless the root's listing changed since the last scan (one listdir, no reads): a folder another process
        # just created must resolve at once.
        now = time.monotonic()
        last = _missed.get(bid)
        try:
            unchanged = _index["root"] == root and frozenset(os.listdir(root)) == _index["names"]
        except OSError:
            unchanged = True
        if unchanged and last is not None and now - last < _MISS_RESCAN_S:
            raise LookupError(f"no such bot {bid!r}")
        _missed[bid] = now
        if len(_missed) > 256:
            _missed.clear()
        try:
            _rescan_locked(root)
        except OSError as e:
            raise LookupError(f"no such bot {bid!r} ({root} unreadable: {e})") from e
        folder = _index["map"].get(bid)
        if folder and _still_holds(folder, bid):
            _missed.pop(bid, None)
            return folder
    raise LookupError(f"no such bot {bid!r}")


def _still_holds(folder: str, bid: str) -> bool:
    """A cached hit is re-checked cheaply: the folder is a real directory whose bot.json (if written yet) still
    says `bid` and whose `.fused` is not a symlink — a hand edit or a folder swap must not route one bot's saves
    into another's files. The bot.json is re-read only when its stat changed (bot_dir is called ~130 places; a
    JSON parse per call under the index lock was 20x a stat). A torn or unreadable bot.json is NOT eviction: the
    cache said this folder was the bot, and only a DIFFERENT valid id proves otherwise."""
    if os.path.islink(folder) or not os.path.isdir(folder) or os.path.islink(os.path.join(folder, _PRIVATE)):
        return False
    meta = os.path.join(folder, _META)
    try:
        st = os.lstat(meta)
    except FileNotFoundError:
        return True  # placed, bot.json not written yet (store.write_meta follows place())
    except OSError:
        return True  # unreadable right now: no evidence against the cache
    if stat_mod.S_ISLNK(st.st_mode):
        return False
    sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _index["stat"].get(bid) == sig:
        return True
    other = _read_id(meta)
    if other is not None and other != bid:
        return False
    _index["stat"][bid] = sig
    return True


def bot_private(bid: str) -> str:
    """`<folder>/.fused`: everything private or transient (state.json, events, memory, inbox, cache). Made on
    first use; gitignored by the `.gitignore` place() seeds."""
    folder = bot_folder(bid)
    p = os.path.join(folder, _PRIVATE)
    if not os.path.lexists(p):
        # A cloned/copied folder never went through place(). The ignore line first: when it cannot be written
        # this raises before any private file exists for `git add` to pick up.
        _seed_gitignore(os.path.join(folder, ".gitignore"))
        _mkdir_real(p)
    elif os.path.islink(p):
        raise OSError(f"{p} is a symlink; refusing to use it as a bot folder")
    return p


def bot_dir(bid: str) -> str:
    """Where a bot's private files live (== bot_private). The name predates the split and ~130 bot.py sites
    use it; it means "the bot's own private folder", now `<folder>/.fused`."""
    return bot_private(bid)


def bot_cache_dir(bid: str) -> str:
    """`<folder>/.fused/cache`: shot.png, window.json, steps/, badjson/, mcp.json, … Deletable any time."""
    p = os.path.join(bot_private(bid), "cache")
    _mkdir_real(p)
    return p


def bot_inbox(bid: str) -> str:
    """`<folder>/.fused/inbox`: the bot's Inbox, one subfolder per task (+ index.jsonl)."""
    p = os.path.join(bot_private(bid), "inbox")
    _mkdir_real(p)
    return p


def bot_drop(bid: str) -> str:
    """`<folder>/.fused/drop`: botsend.py's drop folder. Any local process drops a `<name>.txt` task here and the
    scheduler consumes it (Bot.drain_file_inbox). Never the Inbox: that one holds the user's files and Super Bot's
    working directory, and nothing in it is ever drained."""
    p = os.path.join(bot_private(bid), "drop")
    _mkdir_real(p)
    return p


def _mkdir_real(p: str) -> None:
    """Make ONE directory level, refusing to follow a symlink of that name (makedirs would), so no write under
    `.fused/` can be redirected outside the workspace by a planted link."""
    if os.path.islink(p):
        raise OSError(f"{p} is a symlink; refusing to use it as a bot folder")
    try:
        os.mkdir(p)
    except FileExistsError:
        if not os.path.isdir(p):
            raise


def _holder(folder: str) -> str | None:
    """Who owns `folder` for place(): the id in its bot.json, else the id the cache registered there (placed,
    bot.json not written yet), else None (free, or an old Inbox folder to adopt)."""
    meta = os.path.join(folder, _META)
    if os.path.islink(meta):
        return "?"  # a planted link: somebody's, never adopt
    if os.path.isfile(meta):
        return _read_id(meta) or "?"  # an unreadable bot.json is somebody's: never adopt over it
    for bid, f in _index["map"].items():
        if f == folder:
            return bid
    return None


def place(bid: str, name: str | None, *, adopt: bool = False) -> str:
    """Choose and make the folder for a NEW bot (or one the migration lifts); returns it.

    Named for the bot's name now and never renamed after (the app-folder rule): slug(name), or the id when the
    slug says nothing (empty -> slug's "app" fallback, which is a word for app paths, not a bot name). A folder
    of that name owned by ANOTHER bot (its bot.json, or registered here and not written yet) -> `<slug>-<id4>`;
    one owned by this same id is reused (migrate_v3 reruns); one with no bot.json is adopted ONLY with
    `adopt=True` and only when its `.fused/.bot-id` marker names THIS id — i.e. a folder this function claimed
    for a run that died before its bot.json was written (migrate_v3 writes the meta last). Any other
    bot.json-less folder (no `.fused/`, or a `.fused/` with no marker or another id's) is a user's own folder
    whatever its name: it counts as taken and the next candidate is tried, so a stray `~/Fused/bots/notes/`
    is never claimed as a bot (and never rmtree'd by its delete()). migrate_v3 stages every old Inbox out of
    the way BEFORE placing, so adoption only ever meets its own half-made folders.
    A symlink or a file of that name is never used. The folder is claimed with os.mkdir, so two processes
    placing the same slug at once get different folders. `.gitignore`: seeded with ".fused/" when absent;
    when the user owns one that lacks the line, the line is appended (other lines are never touched); when
    that append fails, OSError is raised before `.fused/` or the marker is written."""
    if not bid or not _ID_RE.fullmatch(bid):
        raise ValueError(f"bad bot id {bid!r}")
    root = bots_root()
    with _index_lock:
        if _index["root"] != root:
            try:
                _rescan_locked(root)
            except OSError:
                log.warning("bots: cannot list %s", root, exc_info=True)
        known = _index["map"].get(bid) if _index["root"] == root else None
        claimed = False
        if known and _still_holds(known, bid):
            folder = known
        else:
            base = slug(name)
            if base == "app":
                base = bid
            folder = None
            for cand in (base, f"{base}-{bid[:4]}", f"{base}-{bid}"):
                path = os.path.join(root, cand)
                if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
                    continue
                if not os.path.lexists(path):
                    try:
                        os.mkdir(path)  # the claim: a second process placing the same slug sees EEXIST
                    except FileExistsError:
                        continue
                    folder, claimed = path, True
                    break
                holder = _holder(path)
                if holder == bid:
                    folder = path
                    break
                # Adoptable: a folder THIS function claimed for this id (its marker says so) whose bot.json was
                # never written — a run that died mid-move. Never a user's own folder, whatever its name or
                # whether it holds a `.fused/` of its own.
                if holder is None and adopt and _marker_id(path) == bid:
                    folder = path
                    break
            if folder is None:
                raise ValueError(f"no free folder for bot {bid} under {root}")
        try:
            # The ignore line first, so a .gitignore that cannot take it fails placement before any private
            # file exists; then `.fused/` and the ownership marker (the claim adopt=True honours).
            _seed_gitignore(os.path.join(folder, ".gitignore"))
            _mkdir_real(os.path.join(folder, _PRIVATE))
            _write_marker(folder, bid)
        except OSError:
            if claimed:
                # A claim that could not be furnished must not read as "taken" next time. Only what this call
                # made is removed (the folder was empty when os.mkdir claimed it).
                for child in (os.path.join(_PRIVATE, _MARKER), _PRIVATE, ".gitignore"):
                    c = os.path.join(folder, child)
                    try:
                        os.rmdir(c) if child == _PRIVATE else os.remove(c)
                    except OSError:
                        pass
                try:
                    os.rmdir(folder)
                except OSError:
                    pass
            raise
        if _index["root"] != root:
            _index.update(root=root, map={}, names=None)
        for other in [k for k, v in _index["map"].items() if v == folder and k != bid]:
            _index["map"].pop(other)  # a stale id mapped here must not save itself into this bot's files
        _index["map"][bid] = folder
        _index["names"] = None  # the listing changed (maybe): the next folders() rescans, keeping this entry
        return folder


def _marker_id(folder: str) -> str | None:
    """The id in `<folder>/.fused/.bot-id`, or None (absent, unreadable, a symlink anywhere on the way, or not
    an id)."""
    priv = os.path.join(folder, _PRIVATE)
    marker = os.path.join(priv, _MARKER)
    if os.path.islink(priv) or not os.path.isdir(priv) or os.path.islink(marker):
        return None
    try:
        with open(marker, encoding="utf-8") as f:
            text = f.read(64).strip()
    except (OSError, ValueError):
        return None
    return text if _ID_RE.fullmatch(text) else None


def _write_marker(folder: str, bid: str) -> None:
    """Write `<folder>/.fused/.bot-id` = `bid` (atomically; a no-op when it already says so)."""
    if _marker_id(folder) == bid:
        return
    marker = os.path.join(folder, _PRIVATE, _MARKER)
    if os.path.islink(marker):
        raise OSError(f"{marker} is a symlink; refusing to write it")
    tmp = f"{marker}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(bid + "\n")
        os.replace(tmp, marker)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _seed_gitignore(ignore: str) -> None:
    """`.fused/` must be ignored whatever else the file says: create it when absent; when the user owns one
    without the line, append it (never rewrite their lines). A symlinked .gitignore is left alone. Raises
    OSError when the line cannot be written (the callers then write no private state: `git add` would commit
    it)."""
    if os.path.islink(ignore):
        return
    try:
        fd = os.open(ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        try:
            with open(ignore, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if any(ln.strip().lstrip("/").rstrip("/*") == ".fused" for ln in text.splitlines()):
                return
            with open(ignore, "a", encoding="utf-8") as f:
                f.write(("" if not text or text.endswith("\n") else "\n") + ".fused/\n")
        except OSError as e:
            raise OSError(f"cannot add .fused/ to {ignore} ({e}); refusing to write private bot state there") from e
        return
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(".fused/\n")


def forget(bid: str) -> None:
    """Drop `bid` from the map (bot.delete, after its folder is removed), so a late lookup raises LookupError
    instead of answering a folder that no longer holds the bot."""
    with _index_lock:
        _index["map"].pop(bid, None)
        _missed.pop(bid, None)


def is_bot_workdir(cwd: str | None) -> bool:
    """Is `cwd` a folder the bots run `claude` in?

    The Tasks page is derived from every transcript under `~/.claude/projects`
    (routers/tasks `_collect`), and a bot's conversation IS a CLI session on
    disk (docs/bots.md §6). So without this test every bot turn is a task row,
    titled with the bot's preamble. The cwd is the one fact that separates the
    two: nobody but `agent_engine.run` works in these folders.

    Three tests, all by path component, never a substring search (a user
    project that happens to contain `bots/cache/bots/x` must stay a task —
    CodeRabbit, PR #1594):
    * today: anywhere under `<workspace>/bots/<folder>/.fused/` (the cache dir
      every bot runs in, Super Bot's `.fused/inbox`). Checked on the path as
      given and resolved, since the CLI may record the realpath.
    * legacy home side: `bots/cache/bots/<id>` off the BASE home, optionally
      behind `branches/<ref>/` — the projects dir holds transcripts from every
      branch install and from before v3, which must stay hidden.
    * legacy Inbox: any direct child of `<workspace>/bots/` — pre-v3 Super Bot
      ran in its Inbox folder `~/Fused/bots/<slug>`, and migrate_v3 ADOPTS that
      folder as the bot's v3 folder (bot.json and all), so a "no bot.json" test
      would bring every pre-migration Super Bot turn back as a task row. The
      cost, accepted as before v3: a human who opens Claude Code in a bot's
      folder root is hidden too. `skills/` and other public subfolders stay
      tasks: no bot run works there."""
    if not cwd:
        return False
    path = os.path.normcase(os.path.normpath(cwd))
    home = os.path.normcase(os.path.normpath(_storage.base_home_dir()))
    if path.startswith(home + os.sep):
        parts = path[len(home) + 1:].split(os.sep)
        if parts[:1] == [os.path.normcase("branches")]:
            parts = parts[2:]  # branches/<ref>/…
        cache = [os.path.normcase(s) for s in ("bots", "cache", "bots")]
        if parts[:3] == cache and len(parts) >= 4 and parts[3]:
            return True
    root_raw = _bots_root_path()
    for p, r in ((path, root_raw), (os.path.normcase(os.path.realpath(cwd)), os.path.realpath(root_raw))):
        r = os.path.normcase(os.path.normpath(r))
        if not p.startswith(r + os.sep):
            continue
        parts = p[len(r) + 1:].split(os.sep)
        if len(parts) >= 2 and parts[0] and parts[1] == os.path.normcase(_PRIVATE):
            return True
        if len(parts) == 1 and parts[0]:
            return True
    return False


def slug(name: str | None) -> str:
    """OpenBot `_slug`: lower-case, `-` for runs of anything else, 40 chars, `app` when empty."""
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (name or "").lower()))[:40] or "app"


def server_json_path() -> str:
    """`<home>/server.json`, the file `fused_render.server.app.write_server_json`
    writes at startup (same join as its `_server_json_path`, not imported: that
    module is the whole server)."""
    return os.path.join(_storage.home_dir(), "server.json")


def server_origin() -> str:
    """Where this server listens (`FUSED_RENDER_ORIGIN`, which
    `fused_render.server.app.set_server_origin_env` exports before serving),
    else what `server.json` says. Raises RuntimeError when neither is known."""
    o = os.environ.get("FUSED_RENDER_ORIGIN")
    if o:
        return o.rstrip("/")
    try:
        with open(server_json_path(), encoding="utf-8") as f:
            o = (json.load(f).get("origin") or "").rstrip("/")
    except (OSError, ValueError):
        o = ""
    if not o:
        raise RuntimeError("the Fused Render server is not running (no origin known)")
    return o


def server_origin_quiet() -> str:
    try:
        return server_origin()
    except RuntimeError:
        return ""
