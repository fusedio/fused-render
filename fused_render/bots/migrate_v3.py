"""One-shot lift of the bots from the legacy home-side layout into one folder
per bot under the workspace (docs/bots.md §1, BUILD_SPEC §4).

Legacy (what this moves FROM):

    <home>/bots/data/bots/<id>/    bot.json, events.jsonl, memory.md, skills/, files/, downloads/, inbox/, profile/…
    <home>/bots/cache/bots/<id>/   shot.png, window.json, steps/, badjson/, …
    ~/Fused/bots/<slug>/<task>/    the old Inbox (+ index.jsonl), pinned per bot as meta["artifacts_dir"]

Target (what it moves TO): `~/Fused/bots/<folder>/` with the public half of
the meta in `bot.json`, `skills/` beside it, and everything private or
transient under `<folder>/.fused/` (state.json, events, memory, files,
downloads, `inbox/<task>/`, `cache/`). The machine-level state under
`<home>/bots/` (browsers, ledgers, dock) is not touched.

Rungs, each a precondition of the next:

1. Nothing to do: there is no legacy bot (a fresh install stamps and never
   backs up). The stamp alone does not end it: a FusedBot import on a later
   start writes the legacy layout again, and those bots are lifted the same way.
2. Backup EVERYTHING bots-related first (owner rule): `<home>/bots/` and
   `~/Fused/bots/` copied whole under `<base_home>/backup/bot-migration-v3/<ts>/`
   and verified per root by a digest over every (relpath, size) copied, with
   no unreadable entry on either side. Any doubt -> `failed.json` beside it,
   stop here, move nothing. The backup goes to the UNNESTED home
   (`base_home_dir()`), never a dev branch's nested one, and is never pruned by
   code. A verified backup of the very same trees (per-root digests over
   (relpath, size, mtime_ns) equal to a fresh walk, and the copy re-walked
   against its manifest) is reused instead of copied again.
3. Place: a bot whose id already has a folder (another branch home migrated it
   first; dev branch homes share one `~/Fused`) is skipped and recorded. One
   that cannot be placed (unreadable bot.json, an id that is not 8 hex) is
   parked as-is under `<bots_root>/.unmigrated/<id>/` and recorded, so it never
   blocks the stamp.
4. Move: `paths.place()` picks the folder (slug of the name, `-<id4>` on a
   clash, an old Inbox folder adopted); then the legacy files are moved in
   file by file: rename, or across volumes copy + verify size + unlink, and
   any error RAISES (the bot fails, nothing of it is deleted). A destination
   that already exists is a clash: the legacy file goes to
   `<folder>/.fused/legacy-leftovers/…` instead, never over anything. A bot
   counts as moved only when its legacy dirs hold nothing but `bot.json`. A
   bot already placed by another home sharing `~/Fused` has its legacy files
   lifted the same way into `legacy-leftovers/`, so nothing of it is deleted
   either. The meta is written LAST, so a bot interrupted mid-move has no
   bot.json yet and the next start re-adopts its own folder.
5. Stamp + remove: only when every bot moved (or was skipped as already
   migrated) AND the legacy trees are verified drained (bot.json files and
   empty dirs only) are they renamed aside atomically, `.layout-v3` written,
   and the renamed dirs removed. Anything failed -> no stamp, nothing removed,
   the next start retries. A cross-process flock on `<bots_root>/.migrate.lock`
   keeps two homes sharing one `~/Fused` from lifting at once.

Reserved names at a bot folder's root (`_FOLDER_OWN`): `.fused`, `bot.json`,
`skills`, `README.md`, `.gitignore`. An old Inbox task folder with one of those
names is not lifted (logged); it stays in the backup.

Runs from `registry._become_owner()` after the FusedBot import (which still
writes the legacy layout) and before any Bot is built. Never raises.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time

try:
    import fcntl
except ImportError:  # Windows: no shared-workspace locking there (one home per machine)
    fcntl = None

from fused_render.bots import paths
from fused_render.bots import store
from fused_render.shell import storage

log = logging.getLogger(__name__)

STAMP = ".layout-v3"
MANIFEST = "manifest.json"
MANIFEST_VERSION = 3
# Mirrors fusedbot_import._IGNORE (keep the two in step): Chrome's runtime files under profile/ are not data --
# SingletonSocket is a Unix socket (copytree raises on it), SingletonLock/SingletonCookie are symlinks naming the
# Chrome that held the profile, RunningChromeVersion likewise; *.lock / *.tmp are ours (atomic-write temps, flocks).
_SKIP = ("Singleton*", "RunningChromeVersion", "*.lock", "*.tmp")
# What an adopted Inbox folder keeps at its root: the new layout's own files. Every other child is a task folder
# (or index.jsonl) and belongs under .fused/inbox/.
_FOLDER_OWN = frozenset({".fused", "bot.json", "skills", "README.md", ".gitignore"})
# A legacy folder that cannot become a bot (unreadable bot.json, an id place() refuses) is parked here, under a
# dot-folder the folder index never scans, so it cannot block the stamp (and a full re-backup) on every start.
UNMIGRATED = ".unmigrated"
# Where a legacy file goes when its destination already exists (never over it), relative to the bot's .fused/.
LEFTOVERS = "legacy-leftovers"
FAILED = "failed.json"
LOCK = ".migrate.lock"
_TASK_RE = re.compile(r"^\d{8}-\d{4}-")  # a task folder name: <YYYYmmdd>-<HHMM>-<slug>
_ID_RE = re.compile(r"[0-9a-f]{8}")  # paths.place()'s id rule


def stamp_path() -> str:
    return os.path.join(paths.root(), STAMP)


def _result(moved: int = 0, skipped: list | None = None, backup: str | None = None, stamped: bool = False) -> dict:
    return {"moved": moved, "skipped": list(skipped or []), "backup": backup, "stamped": stamped}


def migrate_once() -> dict:
    """Lift the legacy per-bot trees into `~/Fused/bots/<folder>/`, once.

    Returns `{"moved", "skipped": [{"id", "reason"}], "backup", "stamped"}`. Never raises: a
    migration bug must not stop the server from serving, and every destructive
    step is gated on a verified backup, so the worst outcome of an exception
    here is "bots hidden until the next start retries"."""
    try:
        return _migrate()
    except Exception:  # noqa: BLE001
        log.exception("bots v3 migration: failed (nothing removed; the next start retries)")
        return _result()


# ------------------------------------------------------------- the copy ---
def _skip_entry(parent: str, name: str) -> bool:
    """One predicate for the backup's ignore AND its verify walk, so the two
    count the same files. Name patterns apply to regular files only: a
    DIRECTORY named like a lock would otherwise be absent from the backup yet
    moved and then removed with the legacy tree. Sockets/FIFOs are skipped by
    type: copytree cannot copy them and they carry no data."""
    try:
        mode = os.lstat(os.path.join(parent, name)).st_mode
    except OSError:
        return False  # counted as unreadable by _walk, which fails the backup
    if stat.S_ISDIR(mode):
        return False
    if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in _SKIP)


def _ignore(parent: str, names: list[str]) -> list[str]:
    return [n for n in names if _skip_entry(parent, n)]


def _lenient_copy(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    """copy2 that logs and carries on: one unreadable file must not abort a
    whole copytree (the verify walk then reports the shortfall, which is what
    stops the migration -- loudly, with nothing moved)."""
    try:
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    except OSError as e:
        log.warning("bots v3 migration: could not copy %s (%s)", src, e)
        return dst


def _walk(top: str) -> tuple[list[tuple[str, int, int, str | None]], int]:
    """Every entry under `top` the backup covers, as (relpath, size, mtime_ns,
    link target or None), plus how many entries could not be stat'ed or listed.
    Symlinks are measured by lstat (copytree(symlinks=True) copies the link,
    not the target), so the same tree measures the same on both sides."""
    rows: list[tuple[str, int, int, str | None]] = []
    unreadable = 0
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            unreadable += 1
            continue
        for e in entries:
            if _skip_entry(d, e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
                continue
            target = None
            if stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(e.path)
                except OSError:
                    unreadable += 1
                    continue
            rows.append((os.path.relpath(e.path, top), st.st_size, st.st_mtime_ns, target))
    rows.sort()
    return rows, unreadable


def _digest(rows, *, with_mtime: bool) -> str:
    h = hashlib.sha256()
    for rel, size, mtime, _t in rows:
        h.update(f"{rel}\0{size}\0{mtime if with_mtime else ''}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _summary(top: str) -> dict:
    """What one root looks like, for the manifest and for verification:
    `digest` is over (relpath, size, mtime_ns) -- "the same tree as before",
    for reusing a backup; `copy_digest` is over (relpath, size) -- "the copy
    holds every file", since a copy across filesystems may not keep mtime_ns."""
    rows, unreadable = _walk(top)
    return {"files": len(rows), "bytes": sum(r[1] for r in rows), "unreadable": unreadable,
            "digest": _digest(rows, with_mtime=True), "copy_digest": _digest(rows, with_mtime=False),
            "links": [[r[0], r[3]] for r in rows if r[3] is not None]}


def _totals(top: str) -> tuple[int, int]:
    """(files, bytes) under `top` -- the owner-facing numbers in the manifest."""
    rows, _ = _walk(top)
    return len(rows), sum(r[1] for r in rows)


def _backup_base() -> str:
    return os.path.join(storage.base_home_dir(), "backup", "bot-migration-v3")


def _backup_dir() -> str:
    base = _backup_base()
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest, n = os.path.join(base, ts), 0
    while os.path.lexists(dest):  # two starts in one second (tests, a crash loop): never write into an old backup
        n += 1
        dest = os.path.join(base, f"{ts}-{n}")
    return dest


def _roots() -> list[tuple[str, str]]:
    """(source root, subdir name in the backup) for every bots root that exists."""
    pairs = [(paths.root(), "home-bots")]
    fused = paths.bots_root()
    if os.path.isdir(fused):
        pairs.append((fused, "fused-bots"))
    return pairs


def _reusable_backup() -> str | None:
    """The newest verified backup of exactly the trees on disk now, if any. A
    run that backed up and then failed (or a crash before the stamp) would
    otherwise copy every browser profile again on every start. Three checks,
    all per root: the manifest exists and no `failed.json` sits beside it; its
    source digest (relpath, size, mtime_ns) equals a fresh walk of the source;
    and a fresh walk of the COPY equals the manifest's copy digest (a backup
    partly deleted after its manifest was written is not trusted)."""
    base = _backup_base()
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return None
    pairs = _roots()
    fresh: dict[str, dict] | None = None
    for n in names:
        dest = os.path.join(base, n)
        if os.path.exists(os.path.join(dest, FAILED)):
            continue
        man = _read_json(os.path.join(dest, MANIFEST))
        if not isinstance(man, dict) or not isinstance(man.get("roots"), dict):
            continue
        if man.get("home_bots") != paths.root() or man.get("fused_bots") != paths.bots_root():
            continue
        if fresh is None:
            fresh = {sub: _summary(src) for src, sub in pairs}
            if any(v["unreadable"] for v in fresh.values()):
                return None
        if set(man["roots"]) != set(fresh):
            continue
        ok = True
        for sub, now in fresh.items():
            rec = man["roots"].get(sub) or {}
            if rec.get("digest") != now["digest"]:
                ok = False
                break
            copy = _summary(os.path.join(dest, sub))
            if copy["unreadable"] or copy["copy_digest"] != rec.get("copy_digest"):
                ok = False
                break
        if ok:
            return dest
    return None


def _recopy_diff(src: str, dst: str, want: dict, got: dict) -> None:
    """Copy again every regular file whose (relpath, size) differs between source and copy, and drop copies of
    files the source no longer has. Symlinks are re-made. Errors are logged (the next verify decides)."""
    rows_src, _ = _walk(src)
    rows_dst, _ = _walk(dst)
    have = {(r[0], r[1]) for r in rows_dst}
    for rel, size, _m, target in rows_src:
        if (rel, size) in have and target is None:
            continue
        a, b = os.path.join(src, rel), os.path.join(dst, rel)
        try:
            os.makedirs(os.path.dirname(b), exist_ok=True)
            if os.path.lexists(b):
                os.unlink(b)
            if target is not None:
                os.symlink(target, b)
            else:
                _lenient_copy(a, b, follow_symlinks=False)  # the same copier copytree used; the verify walk judges
        except OSError as e:
            log.warning("bots v3 migration: re-copy of %s failed (%s)", a, e)
    gone = {r[0] for r in rows_dst} - {r[0] for r in rows_src}
    for rel in gone:
        try:
            os.unlink(os.path.join(dst, rel))
        except OSError:
            pass


def _mark_failed(dest: str, reason: str) -> None:
    try:
        os.makedirs(dest, exist_ok=True)
        store.write_json_atomic(os.path.join(dest, FAILED), {"reason": reason, "at": time.time()})
    except OSError:
        log.debug("bots v3 migration: could not mark %s failed", dest, exc_info=True)


def _backup(entries: list[dict]) -> tuple[str | None, bool]:
    """Copy every bots root under a fresh backup dir and verify it per root:
    no unreadable entry on either side, and the copy's (relpath, size) digest
    equal to the source's. Returns (dest, ok). A failed or short backup is
    LEFT on disk with a `failed.json` beside it (the owner's rule is that bot
    backups are never pruned by code; the marker keeps it from being reused)."""
    dest = _backup_dir()
    pairs = _roots()
    try:
        os.makedirs(dest)
        roots: dict[str, dict] = {}
        files = size = 0
        for src, sub in pairs:
            want = _summary(src)
            if want["unreadable"]:
                _mark_failed(dest, f"{want['unreadable']} entries under {src} could not be read")
                log.error("bots v3 migration: %d entries under %s could not be read; backup failed, "
                          "bots hidden until it succeeds", want["unreadable"], src)
                return dest, False
            for rel, target in want["links"]:
                if os.path.isabs(target) and not any(os.path.normcase(os.path.realpath(target)).startswith(
                        os.path.normcase(os.path.realpath(r)) + os.sep) for r, _ in pairs):
                    log.warning("bots v3 migration: %s/%s is a link to %s, outside the bots roots; the link is "
                                "backed up, its target is not", src, rel, target)
            shutil.copytree(src, os.path.join(dest, sub), symlinks=True, ignore=_ignore,
                            copy_function=_lenient_copy, ignore_dangling_symlinks=True)
            got = _summary(os.path.join(dest, sub))
            for _round in range(3):
                if not got["unreadable"] and got["copy_digest"] == want["copy_digest"]:
                    break
                # A Chrome still writing its profile (the other app's) changes files between the walk and the copy:
                # re-walk the source and re-copy only what differs, a bounded number of times, before giving up.
                want = _summary(src)
                if want["unreadable"]:
                    break
                _recopy_diff(src, os.path.join(dest, sub), want, got)
                got = _summary(os.path.join(dest, sub))
            if got["unreadable"] or got["copy_digest"] != want["copy_digest"]:
                _mark_failed(dest, f"copy of {src} is short ({got['files']}/{got['bytes']} of "
                                   f"{want['files']}/{want['bytes']})")
                log.error("bots v3 migration: backup of %s is short (%d files/%d bytes, copied %d/%d); "
                          "backup failed, bots hidden until it succeeds", src, want["files"], want["bytes"],
                          got["files"], got["bytes"])
                return dest, False
            roots[sub] = {k: want[k] for k in ("files", "bytes", "digest", "copy_digest", "links")}
            files, size = files + want["files"], size + want["bytes"]
        # Written after the verify walks so it is not counted as a copied file.
        store.write_json_atomic(os.path.join(dest, MANIFEST), {
            "version": MANIFEST_VERSION, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "home_bots": paths.root(), "fused_bots": paths.bots_root(), "roots": roots,
            "entries": entries, "files": files, "bytes": size})
        return dest, True
    except Exception as e:  # noqa: BLE001
        _mark_failed(dest, f"{type(e).__name__}: {e}")
        log.exception("bots v3 migration: backup to %s failed, bots hidden until it succeeds", dest)
        return dest, False


# ------------------------------------------------------------ the scan ---
def _folder_name(bid: str, name) -> str:
    """The folder place() would try first. `slug("")` is "app" (tools.py's fallback for app paths); a bot's
    fallback is its id, as in place()."""
    slug = paths.slug(name) if str(name or "").strip() else "app"
    return bid if slug == "app" else slug


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _placed_ids(root: str) -> dict[str, str]:
    """id -> folder for every bot already in the new layout. Scanned directly rather than through
    paths.folders(): this runs before anything else reads the bots and must see disk, not a cache."""
    try:
        found, _names = paths._scan(root)  # the SAME rules the runtime applies: no symlinked folder or .fused, no dot-name
    except OSError:
        return {}
    return dict(found)


def _legacy_bots() -> list[str]:
    try:
        names = sorted(os.listdir(paths.legacy_data_dir()))
    except OSError:
        return []
    base = paths.legacy_data_dir()
    # A symlinked `<id>` would be backed up as a link and then emptied THROUGH the link: not a bot, a stray to park.
    return [n for n in names if not n.startswith(".") and not os.path.islink(os.path.join(base, n))
            and os.path.isfile(os.path.join(base, n, store.BOT_FILE))]


def _is_old_inbox(d: str) -> bool:
    """A folder under bots_root that LOOKS like a pre-v3 Inbox: a real directory with no bot.json, holding an
    index.jsonl, or only task-shaped children (`<YYYYmmdd>-<HHMM>-…`), or nothing. A user's own folder that
    happens to carry a bot's name is none of those and is left alone (logged by the caller)."""
    if not os.path.isdir(d) or os.path.islink(d) or os.path.lexists(os.path.join(d, store.BOT_FILE)):
        return False
    if os.path.lexists(os.path.join(d, ".fused")):
        return False  # a bot folder in the making (a run that died before its bot.json), not an Inbox
    try:
        names = [n for n in os.listdir(d) if not n.startswith(".")]
    except OSError:
        return False
    if "index.jsonl" in names:
        return True
    return all(_TASK_RE.match(n) and os.path.isdir(os.path.join(d, n)) for n in names)


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _old_inboxes(metas: dict[str, dict], root: str) -> dict[str, str]:
    """id -> that bot's old Inbox folder. The pinned `artifacts_dir` wins: it is
    where the bot actually wrote, and it differs from slug(name) after a rename
    or a same-name clash (`<slug>-<id4>`). Only then the slug folder, if no
    other bot pinned it. Knowing every bot's inbox up front is what keeps one
    bot's tasks out of another's folder when place() adopts a folder by name."""
    out: dict[str, str] = {}
    notes: list[dict] = []
    pins: dict[str, list[str]] = {}
    for bid, meta in metas.items():
        pin = meta.get("artifacts_dir")
        if not (isinstance(pin, str) and pin):
            continue
        pin = os.path.normpath(pin)
        if not _same_dir(os.path.dirname(pin), root):
            notes.append({"id": bid, "reason": f"inbox pinned outside the bots root, left at {pin}"})
            log.warning("bots v3 migration: bot %s pinned its Inbox at %s, outside %s; left there, pin dropped",
                        bid, pin, root)
            continue
        if _is_old_inbox(pin):
            pins.setdefault(os.path.normcase(pin), []).append(bid)
    for key, bids in pins.items():
        if len(bids) == 1:
            out[bids[0]] = key
            continue
        # Two bots pinned one Inbox (a rename collision of old): it goes to the one whose name it carries;
        # nobody's otherwise, and it stays where it is (in the backup too).
        name = os.path.basename(key)
        match = [b for b in bids if _folder_name(b, metas[b].get("name")) == name]
        if len(match) == 1:
            out[match[0]] = key
            others = [b for b in bids if b != match[0]]
        else:
            others = bids
        for b in others:
            notes.append({"id": b, "reason": f"inbox {key} shared with {', '.join(x for x in bids if x != b)}; left in place"})
            log.warning("bots v3 migration: Inbox %s is pinned by %s; left in place", key, ", ".join(bids))
    claimed = {os.path.normcase(p) for p in out.values()}
    for bid, meta in metas.items():
        if bid in out:
            continue
        d = os.path.join(root, _folder_name(bid, meta.get("name")))
        if os.path.normcase(d) in claimed:
            continue
        if _is_old_inbox(d):
            out[bid] = d
            claimed.add(os.path.normcase(d))
        elif os.path.isdir(d) and not os.path.lexists(os.path.join(d, store.BOT_FILE)):
            log.info("bots v3 migration: %s carries bot %s's name but is not an Inbox; not adopted", d, bid)
    out["__notes__"] = notes  # type: ignore[assignment]
    return out


def _predict(bid: str, name, root: str, taken: dict[str, str], claimed: set | None = None) -> str:
    """The folder place() will pick, for the backup manifest (written before anything is placed): the name's slug,
    `-<id4>` when a bot.json with another id (or an earlier bot of this run) holds it."""
    base = _folder_name(bid, name)
    for cand in (base, f"{base}-{bid[:4]}", f"{base}-{bid}"):
        path = os.path.join(root, cand)
        if taken.get(cand) not in (None, bid):
            continue
        if os.path.isdir(path) and not os.path.lexists(os.path.join(path, store.BOT_FILE)) \
                and os.path.normcase(path) not in (claimed or set()):
            continue  # a bot.json-less folder that is nobody's Inbox (a user folder): place() treats it as taken
        if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
            continue
        holder = paths._holder(path) if os.path.isdir(path) else None  # the same rule place() applies
        if holder in (None, bid):
            taken[cand] = bid
            return path
    taken[f"{base}-{bid}"] = bid
    return os.path.join(root, f"{base}-{bid}")


# ------------------------------------------------------------ the move ---
def _move_file(src: str, dst: str) -> None:
    """One file or symlink, `src` -> `dst` (which must not exist): rename, or
    across volumes copy + verify size + unlink. Every error RAISES: nothing
    is ever unlinked unless its copy is verified in place."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "destination exists", dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    mode = os.lstat(src).st_mode
    if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
        os.unlink(src)  # a socket or FIFO carries no data; across volumes copy2 would refuse it
        return
    shutil.copy2(src, dst, follow_symlinks=False)
    a, b = os.lstat(src), os.lstat(dst)
    if stat.S_ISLNK(a.st_mode) != stat.S_ISLNK(b.st_mode) or (not stat.S_ISLNK(a.st_mode) and a.st_size != b.st_size):
        raise OSError(errno.EIO, f"copy of {src} to {dst} did not verify")
    os.unlink(src)


def _move_tree(src: str, dst: str, leftovers: str) -> int:
    """Move `src` (file, link or directory) onto `dst`, never over anything:
    a free destination takes a file whole; two directories merge child by
    child; a file whose destination exists is a clash and goes to
    `leftovers/<same relative path>` instead (the backup has it too). Returns
    the clash count. Raises on any failure (a half-moved bot must fail loud,
    not count as moved); a merged source dir is removed only once empty."""
    if os.path.isdir(src) and not os.path.islink(src):
        if os.path.lexists(dst) and (not os.path.isdir(dst) or os.path.islink(dst)):
            # a file where a directory should go: the whole subtree is a clash
            return _move_tree(src, os.path.join(leftovers, os.path.basename(src)), leftovers)
        os.makedirs(dst, exist_ok=True)
        clashes = 0
        for n in sorted(os.listdir(src)):
            clashes += _move_tree(os.path.join(src, n), os.path.join(dst, n), os.path.join(leftovers, n))
        os.rmdir(src)  # raises when anything is left: that is a failure, not a pass
        return clashes
    if os.path.lexists(dst):
        if os.path.normcase(os.path.abspath(dst)).startswith(os.path.normcase(os.path.abspath(leftovers))):
            raise FileExistsError(errno.EEXIST, "leftover destination exists too", dst)
        log.warning("bots v3 migration: %s exists; %s goes to %s instead", dst, src, leftovers)
        _move_file(src, leftovers)
        return 1
    _move_file(src, dst)
    return 0


def _lift_inbox(src: str, inbox: str, leftovers: str, *, own: bool) -> int:
    """Every child of an old Inbox folder except the new layout's own names goes under .fused/inbox/. `own`:
    the Inbox IS the bot's new folder, so its reserved names are the bot's files, not stranded tasks."""
    clashes = 0
    for n in sorted(os.listdir(src)):
        if n in _FOLDER_OWN:
            if own:
                continue  # the bot's own new files
            # A task output that happens to carry a reserved name (pre-v3 Super Bot ran `claude` in its Inbox, so a
            # README.md or skills/ there is plausible): kept, under legacy-leftovers, never dropped and never a
            # reason to fail the bot.
            log.warning("bots v3 migration: %s/%s carries a reserved name; kept under %s", src, n, leftovers)
            clashes += _move_tree(os.path.join(src, n), os.path.join(leftovers, "inbox", n),
                                  os.path.join(leftovers, "inbox-clash", n))
            continue
        clashes += _move_tree(os.path.join(src, n), os.path.join(inbox, n), os.path.join(leftovers, "inbox", n))
    return clashes


def _leftover_dir(folder: str) -> str:
    return os.path.join(folder, ".fused", LEFTOVERS)


def _drain_check(d: str, keep: frozenset = frozenset()) -> list[str]:
    """Relative paths of everything still under `d` other than `keep` names at its root and empty directories.
    Empty directories are removed on the way (os.rmdir, bottom-up)."""
    left: list[str] = []
    if not os.path.isdir(d) or os.path.islink(d):
        return left
    for dp, dns, fns in os.walk(d, topdown=False):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), d)
            if dp == d and fn in keep:
                continue
            left.append(rel)
        for dn in dns:
            p = os.path.join(dp, dn)
            if os.path.islink(p):
                left.append(os.path.relpath(p, d))
                continue
            try:
                os.rmdir(p)
            except OSError:
                pass  # not empty: its contents are already listed
    return left


def _app_source(app_dir: str) -> dict:
    # package.py is written by another slice; until it lands (or if it raises) every app reads as bundled, which is
    # what the export dialog would fall back to anyway.
    try:
        from fused_render.bots.package import resolve_source
    except ImportError:
        return {"kind": "bundled"}
    try:
        src = resolve_source(app_dir)
        return src if isinstance(src, dict) and src.get("kind") else {"kind": "bundled"}
    except Exception:  # noqa: BLE001
        log.debug("bots v3 migration: resolve_source(%s) failed", app_dir, exc_info=True)
        return {"kind": "bundled"}


def seed_apps(meta: dict) -> list[dict]:
    """The bot's `apps` manifest (BUILD_SPEC §3), seeded once from what the bot
    already uses: its trusted apps, the apps it built that still exist, and its
    preset's starter apps when installed. Only folders that exist under the
    apps root count: a row for a missing folder is something the export dialog
    could only show as broken."""
    root = paths.apps_root()
    try:
        names = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    except OSError:
        return []
    try:
        from fused_render.bots.apptools import norm_app
    except Exception:  # noqa: BLE001
        def norm_app(s):
            return (s or "").strip().lower()
    by_norm = {norm_app(n): n for n in names}
    picked: list[str] = []

    def add(folder) -> None:
        if not isinstance(folder, str) or not folder:
            return
        real = folder if folder in names else by_norm.get(norm_app(folder))
        if real and norm_app(real) not in {norm_app(p) for p in picked}:
            picked.append(real)

    for t in meta.get("trusted_apps") or []:
        add(t)
    for bd in meta.get("builds") or []:
        d = bd.get("dir") if isinstance(bd, dict) else None
        if isinstance(d, str) and d:
            add(os.path.basename(os.path.normpath(d)))
    key = meta.get("preset")
    if isinstance(key, str) and key:
        try:
            from fused_render.bots import presets, starters
            p = presets.get(key)
            for k in (p or {}).get("apps") or []:
                if starters.get(k):  # starters.install copies starter <k> to <apps_root>/<k>
                    add(k)
        except Exception:  # noqa: BLE001
            log.debug("bots v3 migration: preset %r apps not read", key, exc_info=True)

    try:
        from fused_render.app_id import app_id as _app_id
    except Exception:  # noqa: BLE001
        def _app_id(_p):
            return None
    return [{"folder": f, "name": f, "app_id": _app_id(os.path.join(root, f, "index.html")),
             "source": _app_source(os.path.join(root, f))} for f in picked]


def _move_bot(bid: str, meta: dict, *_unused) -> str:
    """Place one bot and move its legacy files in. Returns its folder. Raises on
    a real failure (the caller records it and withholds the stamp). After the
    moves the legacy data and cache dirs must hold nothing but bot.json: a
    leftover means the bot FAILED, whatever was moved."""
    # Every bot's old Inbox was STAGED out of the way by _stage_inboxes before any bot is placed, so the bot
    # lands under its current name whatever folder its Inbox occupied: "Foo" renamed to "Bar" lands in bar/,
    # and a namesake's Inbox is never adopted. A crash after staging leaves `.inbox-<id>`, lifted the same way.
    # adopt=True: a folder of this name with no bot.json and no state.json is a run that died before writing the
    # meta (the meta is written last); its half-moved files are this bot's, and anything that clashes goes to
    # legacy-leftovers rather than being lost.
    root = paths.bots_root()
    staged = os.path.join(root, f".inbox-{bid}")
    folder = paths.place(bid, meta.get("name"), adopt=True)
    # Joined here rather than through paths.bot_private(): the bot has no bot.json until the meta is written last.
    private = os.path.join(folder, ".fused")
    inbox = os.path.join(private, "inbox")
    leftovers = _leftover_dir(folder)
    os.makedirs(inbox, exist_ok=True)

    # The staged old Inbox(es): every task folder (and index.jsonl) goes under .fused/inbox/.
    for stg in (staged, f"{staged}-clash"):
        if not os.path.isdir(stg):
            continue
        for n in sorted(os.listdir(stg)):
            src = os.path.join(stg, n)
            if os.path.isdir(src) and not os.path.islink(src):
                _lift_inbox(src, inbox, leftovers, own=False)
                os.rmdir(src)  # emptied, or a reserved-named entry stayed: then this raises and the bot fails
            else:
                _move_tree(src, os.path.join(inbox, n), os.path.join(leftovers, "inbox", n))
        os.rmdir(stg)

    data = os.path.join(paths.legacy_data_dir(), bid)
    for n in sorted(os.listdir(data)):
        s = os.path.join(data, n)
        if n == store.BOT_FILE:
            continue
        if n == "skills":
            _move_tree(s, os.path.join(folder, "skills"), os.path.join(leftovers, "skills"))
        elif n == "inbox" and os.path.isdir(s) and not os.path.islink(s):
            for c in sorted(os.listdir(s)):
                _move_tree(os.path.join(s, c), os.path.join(inbox, c), os.path.join(leftovers, "inbox", c))
            os.rmdir(s)
        else:
            _move_tree(s, os.path.join(private, n), os.path.join(leftovers, n))

    cache = os.path.join(paths.legacy_cache_dir(), bid)
    if os.path.isdir(cache):
        for n in sorted(os.listdir(cache)):
            _move_tree(os.path.join(cache, n), os.path.join(private, "cache", n), os.path.join(leftovers, "cache", n))

    left = _drain_check(data, keep=frozenset({store.BOT_FILE})) + _drain_check(cache)
    if left:
        raise RuntimeError(f"legacy files left behind for bot {bid}: {', '.join(left[:5])}"
                           + (" …" if len(left) > 5 else ""))

    meta = dict(meta)
    meta.pop("artifacts_dir", None)  # the Inbox is .fused/inbox now, never a pinned path
    meta["id"] = bid  # the folder index finds a bot by bot.json's id
    if "apps" not in meta:
        meta["apps"] = seed_apps(meta)
    store.write_meta(bid, meta)
    return folder


def _stage_inboxes(bid: str, srcs) -> None:
    """Move this bot's old Inbox folder(s) to `<bots_root>/.inbox-<id>/<name>/` (a dot-name the folder index
    never scans). Done for EVERY bot before ANY is placed, so a slug folder still holding another bot's tasks
    never decides where a namesake lands."""
    staged = os.path.join(paths.bots_root(), f".inbox-{bid}")
    for src in dict.fromkeys(p for p in srcs if p and os.path.isdir(p)):
        name = os.path.basename(os.path.normpath(src))
        # The clash dir mirrors the staged shape (`<stage>/<inbox name>/<task>/…`) so _move_bot lifts both alike.
        _move_tree(src, os.path.join(staged, name), os.path.join(f"{staged}-clash", name))


def _lift_leftovers(bid: str, folder: str) -> int:
    """A bot already placed (another home sharing `~/Fused` migrated it first): this home's legacy files for it
    are not merged into the live bot (its history is the other home's) but they are never deleted either — they
    go under `<folder>/.fused/legacy-leftovers/<home tag>/`. Returns how many entries moved."""
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", storage.home_dir().strip(os.sep)) + "-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = os.path.join(_leftover_dir(folder), tag)
    moved = 0
    data = os.path.join(paths.legacy_data_dir(), bid)
    for n in sorted(os.listdir(data)):
        if n == store.BOT_FILE:
            continue
        _move_tree(os.path.join(data, n), os.path.join(dest, "data", n), os.path.join(dest, "data-clash", n))
        moved += 1
    cache = os.path.join(paths.legacy_cache_dir(), bid)
    if os.path.isdir(cache):
        for n in sorted(os.listdir(cache)):
            _move_tree(os.path.join(cache, n), os.path.join(dest, "cache", n), os.path.join(dest, "cache-clash", n))
            moved += 1
    left = _drain_check(data, keep=frozenset({store.BOT_FILE})) + _drain_check(cache)
    if left:
        raise RuntimeError(f"legacy files left behind for already-placed bot {bid}: {', '.join(left[:5])}")
    return moved


# ---------------------------------------------------------- the rungs ---
def _park(bid: str, root: str) -> str:
    """Move a legacy bot that cannot be placed, as-is, to `<bots_root>/.unmigrated/<id>/` (cache under `cache/`).
    Called only after the backup verified; never overwrites (a clash lands beside it under `clash/`)."""
    dst = os.path.join(root, UNMIGRATED, bid)
    _move_tree(os.path.join(paths.legacy_data_dir(), bid), dst, os.path.join(dst, "clash"))
    cache = os.path.join(paths.legacy_cache_dir(), bid)
    if os.path.isdir(cache):
        _move_tree(cache, os.path.join(dst, "cache"), os.path.join(dst, "cache-clash"))
    return dst


def _park_strays(root: str) -> list[str]:
    """Entries in the legacy trees that are not a bot (no bot.json: a torn copy, a stray file) are never deleted
    unseen: they go under `.unmigrated/stray-<ts>/`. Returns what moved."""
    out: list[str] = []
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for d, sub in ((paths.legacy_data_dir(), "data"), (paths.legacy_cache_dir(), "cache")):
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            stray = n.startswith(".") or os.path.islink(p)  # never a bot: _legacy_bots skips both
            if not stray and sub == "data" and os.path.isfile(os.path.join(p, store.BOT_FILE)):
                continue  # a bot: moved or parked by name elsewhere
            if not stray and sub == "cache" and os.path.isdir(os.path.join(paths.legacy_data_dir(), n)) \
                    and not os.path.islink(os.path.join(paths.legacy_data_dir(), n)):
                continue  # a bot's cache: handled with the bot
            dst = os.path.join(root, UNMIGRATED, f"stray-{ts}", sub, n)
            _move_tree(p, dst, dst + ".clash")
            out.append(p)
    return out


def _lock(root: str):
    """A non-blocking flock on `<bots_root>/.migrate.lock`, or None when another process holds it (two homes
    sharing one `~/Fused` both starting). Returns the open file to keep the lock, or False when not held."""
    if fcntl is None:
        return True
    fh = open(os.path.join(root, LOCK), "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    return fh


def _migrate() -> dict:
    ids = _legacy_bots()
    stamped_before = os.path.exists(stamp_path())
    if not ids:
        if stamped_before:
            return _result(stamped=True)
        # Nothing is removed here: whatever sits in the legacy trees without a bot.json was never backed up.
        store.write_json_atomic(stamp_path(), {"at": time.time(), "backup": None, "moved": 0, "skipped": []})
        return _result(stamped=True)
    if stamped_before:
        # Legacy bots after the stamp: a FusedBot import (which writes the legacy layout) ran on a later start.
        # Same flow as the first time, backup included; the stamp is rewritten at the end.
        log.info("bots v3 migration: %d legacy bots appeared after the stamp; lifting them", len(ids))

    root = paths.bots_root()
    held = _lock(root)
    if held is False:
        log.warning("bots v3 migration: another process holds %s; not migrating in this one", os.path.join(root, LOCK))
        return _result()
    try:
        return _migrate_locked(ids, root)
    finally:
        if held is not True:
            held.close()


def _migrate_locked(ids: list[str], root: str) -> dict:
    metas: dict[str, dict] = {}
    unplaceable: dict[str, str] = {}
    failed: list[str] = []
    for bid in ids:
        meta = _read_json(os.path.join(paths.legacy_data_dir(), bid, store.BOT_FILE))
        if not isinstance(meta, dict):
            unplaceable[bid] = "unreadable bot.json"
        elif not _ID_RE.fullmatch(bid):
            unplaceable[bid] = "id is not 8 hex"
        else:
            metas[bid] = meta

    placed = _placed_ids(root)
    skipped = [{"id": bid, "reason": f"already at {placed[bid]}"} for bid in metas if bid in placed]
    todo = {bid: m for bid, m in metas.items() if bid not in placed}
    inboxes = _old_inboxes(todo, root)
    skipped.extend(inboxes.pop("__notes__", []))  # type: ignore[arg-type]
    claimed = {os.path.normcase(p) for p in inboxes.values()}

    taken: dict[str, str] = {}
    entries = []
    for bid, meta in todo.items():
        cache = os.path.join(paths.legacy_cache_dir(), bid)
        entries.append({"id": bid, "name": meta.get("name"), "src_data": os.path.join(paths.legacy_data_dir(), bid),
                        "src_cache": cache if os.path.isdir(cache) else None, "src_inbox": inboxes.get(bid),
                        "dst_folder": _predict(bid, meta.get("name"), root, taken, claimed)})
    for bid in metas:
        if bid in placed:
            cache = os.path.join(paths.legacy_cache_dir(), bid)
            entries.append({"id": bid, "name": metas[bid].get("name"), "src_data": os.path.join(paths.legacy_data_dir(), bid),
                            "src_cache": cache if os.path.isdir(cache) else None, "src_inbox": None,
                            "dst_folder": os.path.join(placed[bid], ".fused", LEFTOVERS), "already_placed": True})
    for bid, why in unplaceable.items():
        cache = os.path.join(paths.legacy_cache_dir(), bid)
        entries.append({"id": bid, "name": None, "src_data": os.path.join(paths.legacy_data_dir(), bid),
                        "src_cache": cache if os.path.isdir(cache) else None, "src_inbox": None,
                        "dst_folder": os.path.join(root, UNMIGRATED, bid), "unmigrated": why})

    # ---- rung 2: the backup, verified, before anything below touches a file
    dest = _reusable_backup()
    reused = dest is not None
    if reused:
        log.info("bots v3 migration: reusing verified backup %s", dest)
    else:
        dest, ok = _backup(entries)
        if not ok:
            return _result(skipped=skipped, backup=dest)

    for bid, why in unplaceable.items():
        try:
            where = _park(bid, root)
            skipped.append({"id": bid, "reason": why})
            log.warning("bots v3 migration: bot %s not migrated (%s); parked at %s, original in backup %s",
                        bid, why, where, dest)
        except Exception:  # noqa: BLE001
            log.exception("bots v3 migration: bot %s (%s) could not be parked", bid, why)
            failed.append(bid)

    # Phase A: every bot's Inbox out of the way; phase B: place and move. Two loops on purpose (see _stage_inboxes).
    orphans: dict[str, str | None] = {}
    for bid, meta in todo.items():
        # A folder of this bot's name that no bot pinned is an orphaned Inbox: it lifts with this bot.
        guess = os.path.join(root, _folder_name(bid, meta.get("name")))
        orphans[bid] = guess if _is_old_inbox(guess) and os.path.normcase(guess) not in claimed else None
        if orphans[bid]:
            claimed.add(os.path.normcase(guess))
        try:
            _stage_inboxes(bid, (inboxes.get(bid), orphans[bid]))
        except Exception:  # noqa: BLE001
            log.exception("bots v3 migration: bot %s's Inbox could not be staged; the bot is not moved", bid)
            failed.append(bid)
    moved = 0
    for e in entries:
        bid = e["id"]
        if bid not in todo or bid in failed:
            continue  # parked above, already placed (below), or its Inbox did not stage
        meta = todo[bid]
        try:
            e["dst_folder"] = _move_bot(bid, meta, inboxes.get(bid), orphans.get(bid))
            moved += 1
        except Exception:  # noqa: BLE001
            log.exception("bots v3 migration: bot %s not moved (its files stay in the legacy tree)", bid)
            failed.append(bid)
    for bid in metas:
        if bid in placed:
            try:
                n = _lift_leftovers(bid, placed[bid])
                log.info("bots v3 migration: bot %s is already at %s; %d legacy entries kept under %s",
                         bid, placed[bid], n, _leftover_dir(placed[bid]))
            except Exception:  # noqa: BLE001
                log.exception("bots v3 migration: bot %s is already placed but its legacy files could not be kept", bid)
                failed.append(bid)
    try:  # the manifest names where each bot actually landed (the prediction can differ on a clash)
        manifest = None if reused else _read_json(os.path.join(dest, MANIFEST))  # a reused backup is left as written
        if isinstance(manifest, dict):
            manifest["entries"] = entries
            store.write_json_atomic(os.path.join(dest, MANIFEST), manifest)
    except Exception:  # noqa: BLE001
        log.debug("bots v3 migration: manifest not updated", exc_info=True)

    if failed:
        log.error("bots v3 migration: %d moved, %d failed (%s); no stamp, legacy tree kept, the next start retries",
                  moved, len(failed), ", ".join(failed))
        return _result(moved=moved, skipped=skipped, backup=dest)

    # ---- rung 5: strays aside, trees verified drained, renamed aside, stamp, then removed
    try:
        strays = _park_strays(root)
    except Exception:  # noqa: BLE001
        log.exception("bots v3 migration: stray legacy entries could not be set aside; no stamp")
        return _result(moved=moved, skipped=skipped, backup=dest)
    left = _legacy_leftovers()
    if left:
        log.error("bots v3 migration: legacy trees still hold %s; no stamp, nothing removed", ", ".join(left[:5]))
        return _result(moved=moved, skipped=skipped, backup=dest)
    aside = _rename_legacy_aside()
    if aside is None:
        return _result(moved=moved, skipped=skipped, backup=dest)
    store.write_json_atomic(stamp_path(), {"at": time.time(), "backup": dest, "moved": moved, "skipped": skipped,
                                           "strays": strays})
    for d in aside:
        try:
            shutil.rmtree(d)
        except OSError:
            log.exception("bots v3 migration: could not remove %s (already set aside; safe to delete by hand)", d)
    log.info("bots v3 migration: %d bots moved to %s, %d skipped as already there; backup at %s",
             moved, root, len(skipped), dest)
    return _result(moved=moved, skipped=skipped, backup=dest, stamped=True)


def _legacy_leftovers() -> list[str]:
    """Anything under the legacy trees other than bot.json files and empty dirs (removed on the way)."""
    out: list[str] = []
    data = paths.legacy_data_dir()
    if os.path.isdir(data):
        for n in sorted(os.listdir(data)):
            out += [os.path.join(n, r) for r in _drain_check(os.path.join(data, n), keep=frozenset({store.BOT_FILE}))]
    out += _drain_check(paths.legacy_cache_dir())
    return out


def _rename_legacy_aside() -> list[str] | None:
    """Atomically rename the two legacy trees to dot-names beside themselves, so a half-finished rmtree can never
    leave a tree that rung 1 misreads as "no legacy bots" while events still sit inside. None on failure."""
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out: list[str] = []
    for d in (paths.legacy_data_dir(), paths.legacy_cache_dir()):
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        aside = os.path.join(os.path.dirname(d), f".{os.path.basename(d)}-removed-{ts}")
        try:
            os.rename(d, aside)
        except OSError:
            log.exception("bots v3 migration: could not set %s aside; no stamp", d)
            for a in out:  # put back what was renamed so the next start sees the same tree
                try:
                    os.rename(a, os.path.join(os.path.dirname(a), os.path.basename(a).split("-removed-")[0][1:]))
                except OSError:
                    log.exception("bots v3 migration: could not restore %s", a)
            return None
        out.append(aside)
    return out
