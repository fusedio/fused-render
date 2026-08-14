"""The scan: one-directory scanning, work distribution across processes, and
the run body the detached worker executes.

Ported from OpenIndex's `runner.py` (`_scan_dir_once`, `_scan_subtree`,
`_scan_dirs_threaded`, `_worker`). Two changes beyond de-globalization:

  * every prune decision also consults a `MountGuard`, so the crawler cannot
    descend into an rclone mount even if the ignore list is emptied — a kernel
    scandir/stat there can wedge the mount permanently;
  * the run's configuration travels in `spec.json` rather than being
    re-derived from module state in each pool child.

See specs/scan.md and specs/scan-incremental.md.
"""
import hashlib
import json
import os
import threading
import time

from fused_render.index import fsevents
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import (
    SKIP_DIRS,
    IgnoreRules,
    MountGuard,
    ignored_for_index,
    is_inside_leaf_dir,
    is_leaf_dir,
    norm,
)
from fused_render.index.store import (
    Sink,
    applied_ignore_sig,
    compact,
    load_dir_cache,
    save_applied_ignore,
)


def keep_subdirs(subdirs, rules: IgnoreRules, guard: MountGuard):
    """The subdirectories a walk may hand on: not a hardcoded skip, not
    mount-backed, and not ignored — except that a LEAF dir survives the ignore
    list.

    "Hand on" rather than "descend into", because a leaf dir kept here is not
    descended: scan_dir_once records it and returns no children. Which is exactly
    why the ignore list does not get a veto over one. An ignore entry buys the
    scan two things — no descent and no row — and for a leaf dir the first is
    already true, so all it can still do is delete the row. For `.git` that row
    IS the repo-detection fact /api/git-repos reads, and a user (or an old saved
    config, from back when `.git` shipped in the default ignore list) still
    naming `.git` there would silently empty the homepage's Repos tab while
    saving one stat. SKIP_DIRS and the mount guard keep their veto: those are
    hazards, not preferences.

    Narrow on purpose — this only overrides the verdict on the leaf dir ITSELF.
    A repo inside an ignored tree is still gone, because the walk never reaches
    its parent to offer it here.

    The exemption is NOT spelled out here: it lives in `ignored_for_index`, which
    every ignore gate routes through. It used to be inline, and that is exactly
    how the other two gates went on purging the rows this one kept."""
    return [s for s in subdirs
            if s not in SKIP_DIRS and not guard.blocks(s)
            and not ignored_for_index(rules, s, tree=False)]


def _dir_sig(entries):
    h = hashlib.sha1()
    for name, size, mtime_ns in sorted(entries):
        h.update(f"{name}|{size}|{mtime_ns}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def scan_dir_once(d, cache, rules, guard, devs=None, root_dev=None):
    """Scan one directory. Returns (kind, payload, subdirs): kind "u"
    (unchanged; payload = cached file count), "s" (scanned; payload =
    (sig, file_rows, total_size, mtime_ns, n_subdirs)), or None on error. When
    `devs` is a set, the dir's device id is added to it (multi-volume
    detection).

    `root_dev` confines the walk to the scan root's own filesystem. A mount —
    rclone, iCloud, SMB, an external disk — is always its own device, so this
    one comparison refuses every mount, including the ones no ignore rule or
    guard has been told about. It costs nothing: the `stat` it reads is the
    one this function already takes, and the check happens at the mount's own
    directory rather than at its parent, so no extra stat per child either.
    (`/Volumes`, `/proc` and friends are refused by name as well —
    specs/scan.md §6 — but names only cover the mount points a list can
    predict.)

    This is where every path in the index is born, so every `e.path` leaves
    here through norm() — the whole store, and all matching against it, is in
    canonical form (specs/platform.md §1)."""
    try:
        dst = os.stat(d, follow_symlinks=False)
        d_mtime_ns = dst.st_mtime_ns
        if root_dev is not None and dst.st_dev != root_dev:
            return None, None, []
        if devs is not None:
            devs.add(dst.st_dev)
    except OSError:
        return None, None, []
    if is_leaf_dir(d):
        # A macOS package: RECORDED as one dirs row (this return), never listed.
        # The walk emits `Foo.app` itself as a single leaf entry and nothing
        # inside it, so the index has to do both halves — dropping the package
        # from its parent's descent list instead would leave no dirs row for it
        # at all, and break the same parity in the other direction. Costs the
        # stat above and no scandir, whatever the package holds.
        return "s", (_dir_sig([]), [], 0, d_mtime_ns, 0), []
    cached = cache.get(d)
    subdirs = []
    # cached[2] == -1 means a pre-upgrade row with unknown subdir count:
    # rescan it once so the count backfills (it stays valid after that —
    # adding/removing a subdir always bumps the parent's mtime)
    if cached is not None and d_mtime_ns == cached[0] and cached[2] >= 0:
        if cached[2] == 0:
            return "u", cached[1] or 0, []   # unchanged leaf: stat only
        # Unchanged dir: recurse into subdirs but keep its cached
        # file rows — no per-file stat, no rewrite.
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if not e.is_symlink() and e.is_dir(follow_symlinks=False):
                            subdirs.append(norm(e.path))
                    except OSError:
                        continue
        except OSError:
            return None, None, []
        return "u", cached[1] or 0, keep_subdirs(subdirs, rules, guard)
    sig_entries, frows, dtotal = [], [], 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        subdirs.append(norm(e.path))
                        sig_entries.append((e.name + "/", 0, 0))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        _, ext = os.path.splitext(e.name)
                        frows.append((norm(e.path), d, e.name,
                                      ext.lower().lstrip("."),
                                      st.st_size, st.st_mtime))
                        sig_entries.append((e.name, st.st_size, st.st_mtime_ns))
                        dtotal += st.st_size
                except OSError:
                    continue
    except OSError:
        return None, None, []
    subdirs = keep_subdirs(subdirs, rules, guard)
    return "s", (_dir_sig(sig_entries), frows, dtotal, d_mtime_ns,
                 len(subdirs)), subdirs


# --------------------------------------------------------------- pool children

_CHILD = {}


def _child_init(run_dir, no_cache):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from concurrent.futures import ThreadPoolExecutor
    with open(os.path.join(run_dir, "spec.json")) as f:
        spec = json.load(f)
    cfg = IndexConfig.from_dict(spec.get("config") or {})
    guard = MountGuard(mounts_dir=spec.get("mounts_dir"))
    cache = {} if no_cache else load_dir_cache(cfg, spec["root"], pq)
    _CHILD.update(
        pool=ThreadPoolExecutor(max_workers=16),
        cache=cache,
        rules=cfg.rules,
        guard=guard,
        sink=Sink(os.path.join(run_dir, "shards"), f"c{os.getpid()}", pa, pq,
                  cfg.shard_rows),
        devs=set(),
        # The scan root's filesystem, decided ONCE by the parent and carried
        # here: a child must not re-derive it (a stat of a root that has since
        # become a mount point would hand this process a different answer than
        # its siblings, and the whole point is that no process crosses).
        root_dev=spec.get("root_dev"),
        cancel_flag=os.path.join(run_dir, "cancel"),
        progress=os.path.join(run_dir, f"progress-{os.getpid()}.json"),
    )


def _child_progress(current=""):
    s = _CHILD["sink"]
    tmp = _CHILD["progress"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"dirs": s.dirs, "files": s.files, "reused": s.reused,
                   "udirs": s.udirs, "devs": sorted(_CHILD["devs"]),
                   "current": current}, f)
    os.replace(tmp, _CHILD["progress"])


def _scan_subtree(subroot):
    """Scan one subtree inside a pool worker. Walks single-threaded (fastest
    when metadata is warm), but if dirs start blocking — sandbox containers,
    dataless iCloud dirs — hands the rest of the subtree to a thread pool,
    where the blocked syscalls overlap (they release the GIL while waiting)."""
    sink, cache = _CHILD["sink"], _CHILD["cache"]
    rules, guard = _CHILD["rules"], _CHILD["guard"]
    stack = [subroot]
    i = 0
    t_win = time.time()
    while stack:
        i += 1
        if i % 500 == 0:
            if os.path.exists(_CHILD["cancel_flag"]):
                stack = []
                break
            _child_progress(stack[-1])
        d = stack.pop()
        kind, payload, subdirs = scan_dir_once(d, cache, rules, guard,
                                               _CHILD["devs"], _CHILD["root_dev"])
        stack.extend(subdirs)
        if kind:
            sink.add(d, kind, payload)
        # rolling window: >10ms/dir over the last 100 dirs means the OS is
        # stalling on them (warm metadata is ~0.06ms/dir) — overlap the
        # waits on the thread pool
        if i % 100 == 0:
            now = time.time()
            if stack and now - t_win > 1.0:
                _scan_dirs_threaded(stack)
                break
            t_win = now
    sink.close()
    _child_progress()


def _scan_dirs_threaded(dirs):
    """Finish a latency-bound work list on the child's thread pool; only
    this (the child's main) thread touches the sink."""
    import queue
    import threading
    sink, cache, ex = _CHILD["sink"], _CHILD["cache"], _CHILD["pool"]
    rules, guard = _CHILD["rules"], _CHILD["guard"]
    out_q = queue.Queue()
    pending = [0]
    lock = threading.Lock()
    done = threading.Event()
    cancelled = [False]

    def submit(d):
        with lock:
            pending[0] += 1
        ex.submit(work, d)

    def work(d):
        try:
            if not cancelled[0]:
                kind, payload, subdirs = scan_dir_once(d, cache, rules, guard,
                                                       _CHILD["devs"],
                                                       _CHILD["root_dev"])
                for s in subdirs:
                    submit(s)
                if kind:
                    out_q.put((d, kind, payload))
        finally:
            with lock:
                pending[0] -= 1
                if pending[0] == 0:
                    done.set()

    if not dirs:
        return
    # The whole initial list is counted BEFORE anything is submitted. Counting
    # per-iteration let a fast worker finish dirs[0] while dirs[1] was still
    # unsubmitted: pending hit 0, `done` latched (it is never cleared), and the
    # drain loop's next quiet 0.2s — routine on the slow filesystems that
    # select threaded mode — ended the loop while workers were still producing,
    # silently dropping their entries from the index.
    with lock:
        pending[0] += len(dirs)
    for d in dirs:
        ex.submit(work, d)
    i = 0
    while True:
        try:
            item = out_q.get(timeout=0.2)
        except queue.Empty:
            if done.is_set():
                break
            continue
        sink.add(*item)
        i += 1
        if i % 200 == 0:
            if os.path.exists(_CHILD["cancel_flag"]):
                cancelled[0] = True
            _child_progress(item[0])
    while not out_q.empty():
        sink.add(*out_q.get())


# ------------------------------------------------------------------- the run

def _emit(f, **ev):
    ev["ts"] = round(time.time(), 3)
    f.write(json.dumps(ev) + "\n")
    f.flush()


def run_scan(run_dir: str) -> None:
    """Execute the run described by `<run_dir>/spec.json`, reporting progress
    into `<run_dir>/events.jsonl`. Never raises: a crash is reported as a
    terminal `run_end` event with the traceback, because the only consumer is
    a poller that would otherwise wait forever."""
    import glob as globmod
    import pyarrow as pa
    import pyarrow.parquet as pq
    from collections import deque

    with open(os.path.join(run_dir, "spec.json")) as f:
        spec = json.load(f)
    cfg = IndexConfig.from_dict(spec.get("config") or {})
    guard = MountGuard(mounts_dir=spec.get("mounts_dir"))
    rules = cfg.rules
    root = spec["root"]
    # The filesystem the walk stays on (scan_dir_once). Decided here, before
    # anything is scanned, and written back into the spec so every pool child
    # confines itself to the same one.
    root_dev = spec.get("root_dev")
    if root_dev is None:
        try:
            root_dev = os.stat(root).st_dev
        except OSError:
            root_dev = None
        spec["root_dev"] = root_dev
        with open(os.path.join(run_dir, "spec.json"), "w") as f:
            json.dump(spec, f)
    cancel_flag = os.path.join(run_dir, "cancel")
    shards_dir = os.path.join(run_dir, "shards")
    os.makedirs(shards_dir, exist_ok=True)
    t0 = time.time()

    ev = open(os.path.join(run_dir, "events.jsonl"), "a")
    _emit(ev, type="run_start", msg=root)

    try:
        # A changed rule set invalidates the cache: cached dirs carry subdir
        # counts computed under the old rules, so an incremental scan would keep
        # skipping folders that are no longer ignored.
        #
        # An ABSENT fingerprint counts as changed too, which it did not used to.
        # That was safe while the only rules were ignore PATTERNS: removing a
        # pattern is self-purging through the filtered cache (scan-ignore.md §3),
        # so an unfingerprinted index could be reconciled incrementally. It is not
        # safe for a rule that ADDS rows. `.git` becoming a leaf dir is exactly
        # that: the new row can only appear by visiting the repo directory, and an
        # incremental scan skips it precisely because its mtime has not changed —
        # after which this scan would STAMP the new fingerprint over an index that
        # never grew the rows, and every reader that trusts the stamp (notably
        # /api/git-repos) would be confidently wrong, permanently. One full rescan
        # of an unfingerprinted index is the cheap side of that trade.
        applied = applied_ignore_sig(cfg, root)
        rules_changed = applied != rules.sig()
        if rules_changed:
            _emit(ev, type="phase", msg=(
                "ignore rules changed - full rescan" if applied is not None
                else "no applied rules fingerprint - full rescan"))

        # Journal position captured BEFORE scanning: events during the scan get
        # replayed (harmlessly re-checked) next time instead of being missed.
        # Before the hint too, and before anything is read — an id taken later
        # would silently drop whatever happened in between.
        fs_id0 = fsevents.current_id()
        fs_uuid = fsevents.device_uuid(root) if fs_id0 is not None else None

        # The two setup reads are independent and both spend their time outside
        # the GIL — load_dir_cache is parquet IO, fsevents.hint is a CFRunLoop
        # draining the journal over IPC — so they run together and setup costs
        # max() instead of sum(). Measured on a 588k-file index: the cache read
        # is a flat ~0.75s and the replay 0.1-2.9s depending on how much churn
        # there has been since the last scan, and they used to be paid one after
        # the other on every single run. Threads, not processes: neither holds
        # the GIL for its cost, and a spawn would eat the saving. The replay is
        # not main-thread bound either: it schedules its stream on
        # CFRunLoopGetCurrent(), which gives whichever thread runs it that
        # thread's own run loop (fsevents._replay).
        #
        # The hint is therefore computed UNCONDITIONALLY, even though only an
        # incremental run can use one: whether there IS a cache is not known
        # until the other thread returns, and waiting to find out is exactly the
        # serialization being removed. The wasted replay happens only on a full
        # or rules-changed run — rare — and it is wasted in parallel with a walk
        # that was going to happen anyway. It is discarded below.
        box: dict = {}

        def _hint_thread():
            try:
                box["hint"] = fsevents.hint(cfg, root)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                box["error"] = exc

        hint_thread = threading.Thread(target=_hint_thread, daemon=True,
                                       name="fsevents-hint")
        hint_thread.start()
        try:
            cache = ({} if (spec.get("full") or rules_changed)
                     else load_dir_cache(cfg, root, pq))
        finally:
            hint_thread.join()
        if "error" in box:
            # fsevents.hint answers None on every failure path it knows about,
            # so anything raising out of it is a defect. Re-raised into the run's
            # own handler (a `failed` run_end with the traceback) rather than
            # degraded to "no hint": a silent full walk of a large root reads as
            # a slow scan, not as a bug, and would keep doing so every run.
            raise box["error"]
        incremental = bool(cache)
        hint = box.get("hint") if incremental else None
        _emit(ev, type="phase", msg=(
            "scanning (fsevents journal)" if hint is not None
            else "scanning (incremental)" if incremental else "scanning (full)"))

        devs = set()

        def child_totals():
            agg = {"dirs": 0, "files": 0, "reused": 0, "udirs": 0, "current": ""}
            for p in globmod.glob(os.path.join(run_dir, "progress-*.json")):
                try:
                    with open(p) as fh:
                        j = json.load(fh)
                except Exception:
                    continue
                for k in ("dirs", "files", "reused", "udirs"):
                    agg[k] += j.get(k, 0)
                devs.update(j.get("devs") or [])
                if j.get("current"):
                    agg["current"] = j["current"]
            return agg

        sink = Sink(shards_dir, "p", pa, pq, cfg.shard_rows)
        cancelled = False

        if hint is not None:
            summary = _run_fsevents(cfg, rules, guard, root, hint, cache, sink,
                                    ev, cancel_flag, devs, t0, pa, pq, root_dev)
            if summary is not None and fs_id0 is not None and fs_uuid:
                fsevents.save_state(cfg, root, fs_id0, fs_uuid, devs)
            if summary is not None:
                save_applied_ignore(cfg, root)
                _emit(ev, type="run_end", msg="complete", summary=summary)
            return

        # Walk the top of the tree in-process (breadth-first) until there
        # are enough subtree roots to keep cfg.nproc worker processes busy,
        # then fan the subtrees out to a process pool — the GIL caps a
        # single process at ~15k dirs/s no matter how many threads.
        frontier = deque([root])
        while frontier and len(frontier) < cfg.nproc * 24:
            if sink.dirs % 200 == 0 and os.path.exists(cancel_flag):
                cancelled = True
                break
            d = frontier.popleft()
            kind, payload, subdirs = scan_dir_once(d, cache, rules, guard, devs,
                                                   root_dev)
            frontier.extend(subdirs)
            if kind:
                sink.add(d, kind, payload)

        if cache and frontier:
            # Split subtrees the cache says are huge, so one giant folder
            # (e.g. ~/Library) can't serialize the pool at the tail.
            import bisect
            keys = sorted(cache)

            def subtree_n(d):
                p = d.rstrip("/") + "/"
                return (bisect.bisect_left(keys, p[:-1] + "0")
                        - bisect.bisect_left(keys, p))

            big = deque(d for d in frontier if subtree_n(d) > cfg.split_dirs)
            frontier = deque(d for d in frontier if subtree_n(d) <= cfg.split_dirs)
            guard_count = 0
            while big and guard_count < 20_000 and not cancelled:
                guard_count += 1
                if guard_count % 200 == 0 and os.path.exists(cancel_flag):
                    cancelled = True
                    break
                d = big.popleft()
                kind, payload, subdirs = scan_dir_once(d, cache, rules, guard,
                                                       devs, root_dev)
                if kind:
                    sink.add(d, kind, payload)
                for s2 in subdirs:
                    (big if subtree_n(s2) > cfg.split_dirs
                     else frontier).append(s2)
        sink.close()
        totals = {"dirs": sink.dirs, "files": sink.files,
                  "reused": sink.reused, "udirs": sink.udirs}

        if frontier and not cancelled:
            import multiprocessing as mp
            # "spawn", never fork: a forked child re-runs PROJ's SQLite atfork
            # handler and dies with SIGSEGV (the pyramid-worker crash class).
            ctx = mp.get_context("spawn")
            pool = ctx.Pool(cfg.nproc, initializer=_child_init,
                            initargs=(run_dir, not incremental))
            res = pool.map_async(_scan_subtree, sorted(frontier), chunksize=1)
            while not res.ready():
                res.wait(0.5)
                c = child_totals()
                _emit(ev, type="progress",
                      dirs=totals["dirs"] + c["dirs"],
                      files=totals["files"] + c["files"],
                      reused=totals["reused"] + c["reused"],
                      current=c["current"])
            pool.close()
            pool.join()
            res.get()  # surface any child exception
            c = child_totals()
            for k in ("dirs", "files", "reused", "udirs"):
                totals[k] += c[k]

        cancelled = cancelled or os.path.exists(cancel_flag)
        _emit(ev, type="progress", dirs=totals["dirs"], files=totals["files"],
              reused=totals["reused"], current="")

        if cancelled:
            _emit(ev, type="run_end", msg="cancelled",
                  summary={"dirs": totals["dirs"], "files": totals["files"]})
            return

        summary = compact(cfg, root, shards_dir, pa, pq,
                          emit=lambda **e: _emit(ev, **e),
                          cancel_flag=cancel_flag)
        if summary is None:  # cancelled at the store lock (delete raced us)
            _emit(ev, type="run_end", msg="cancelled",
                  summary={"dirs": totals["dirs"], "files": totals["files"]})
            return
        summary.update(dirs=totals["dirs"], files=totals["files"],
                       reused_files=totals["reused"],
                       unchanged_dirs=totals["udirs"],
                       seconds=round(time.time() - t0, 1))
        if fs_id0 is not None and fs_uuid:
            fsevents.save_state(cfg, root, fs_id0, fs_uuid, devs)
        save_applied_ignore(cfg, root)
        _emit(ev, type="run_end", msg="complete", summary=summary)
    except Exception:
        import traceback
        _emit(ev, type="run_end", msg="failed", error=traceback.format_exc())
    finally:
        ev.close()


def _run_fsevents(cfg, rules, guard, root, hint, cache, sink, ev, cancel_flag,
                  devs, t0, pa, pq, root_dev=None):
    """The FSEvents fast path: visit ONLY the dirs the OS journal reports and
    account explicitly for everything it didn't (specs/scan-incremental.md §4).
    Returns the run summary, or None when the run was cancelled (the caller
    has already emitted nothing; this emits the terminal event itself)."""
    forced, subtrees = hint
    devs.update(fsevents.load_states(cfg).get(root, {}).get("devs") or [])
    children = {}
    for c in cache:
        if c != root:
            children.setdefault(os.path.dirname(c), []).append(c)
    deleted, scanned = [], set()
    stack = [(s, False) for s in subtrees] + [(f, True) for f in forced]
    cancelled = False
    last_beat = t0
    while stack:
        if os.path.exists(cancel_flag):
            cancelled = True
            break
        d, force = stack.pop()
        # is_inside_leaf_dir, and not the is_leaf_dir test scan_dir_once makes:
        # this loop does not descend to `d`, the journal hands it over, and what
        # the journal names inside a package is always a descendant (an app
        # update writes Foo.app/Contents/Resources, Photos writes
        # Foo.photoslibrary/database) — never the package itself. A
        # final-component test therefore lets every package internal in through
        # this path while the walk-driven one drops them, and the tail loop
        # below then carries those rows forward on every later run. The
        # package's own dirs row is unaffected: it is is_leaf_dir's, made when
        # the journal or the walk names the package.
        # ignored_for_index, not rules.is_ignored_tree: a leaf dir the user's
        # ignore list names must still be re-added here, or an incremental pass
        # PURGES the rows the walk-driven scan wrote (the cache filter drops them
        # from the keep list, this gate refuses to recreate them, and the
        # compaction then has neither). tree=True because the journal hands over
        # a path whose ancestors nobody checked.
        if (d in scanned or ignored_for_index(rules, d, tree=True)
                or guard.blocks(d) or is_inside_leaf_dir(d)):
            continue
        scanned.add(d)
        kind, payload, subdirs = scan_dir_once(
            d, {} if force else cache, rules, guard, devs, root_dev)
        if kind is None:
            deleted.append(d)   # unreadable/gone: drop cached subtree
            continue
        sink.add(d, kind, payload)
        actual = set(subdirs)
        for c in children.get(d, ()):
            if c not in actual:
                deleted.append(c)
        for s2 in subdirs:
            if not force:
                stack.append((s2, False))
            elif s2 not in cache:
                stack.append((s2, True))   # new subtree
        now = time.time()
        if now - last_beat >= 0.5:
            last_beat = now
            _emit(ev, type="progress", dirs=sink.dirs, files=sink.files,
                  reused=sink.reused, current=d)
    # keep every cached dir that wasn't visited or deleted
    if not cancelled:
        import bisect
        keys = sorted(cache)
        dead = set()
        for p in sorted(set(deleted)):
            i = bisect.bisect_left(keys, p)
            while i < len(keys) and (keys[i] == p or keys[i].startswith(p + "/")):
                dead.add(keys[i])
                i += 1
        for c in cache:
            if c in scanned or c in dead:
                continue
            sink.keep.append(c)
            sink.dirs += 1
            sink.udirs += 1
            sink.reused += cache[c][1] or 0
    sink.close()
    totals = {"dirs": sink.dirs, "files": sink.files,
              "reused": sink.reused, "udirs": sink.udirs}
    cancelled = cancelled or os.path.exists(cancel_flag)
    _emit(ev, type="progress", dirs=totals["dirs"], files=totals["files"],
          reused=totals["reused"], current="")
    if cancelled:
        _emit(ev, type="run_end", msg="cancelled",
              summary={"dirs": totals["dirs"], "files": totals["files"]})
        return None
    summary = compact(cfg, root, sink.shards_dir, pa, pq,
                      emit=lambda **e: _emit(ev, **e), cancel_flag=cancel_flag)
    if summary is None:  # cancelled at the store lock (delete raced the scan)
        _emit(ev, type="run_end", msg="cancelled",
              summary={"dirs": totals["dirs"], "files": totals["files"]})
        return None
    summary.update(dirs=totals["dirs"], files=totals["files"],
                   reused_files=totals["reused"],
                   unchanged_dirs=totals["udirs"], fsevents=True,
                   seconds=round(time.time() - t0, 1))
    return summary
