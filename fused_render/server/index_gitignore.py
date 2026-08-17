"""Gitignore parity for the index-backed search corpus.

The live walk prunes gitignored entries as it descends (server/walk.py), and
the WalkEntry contract promises nothing ignored ever reaches search results.
The index scan deliberately knows nothing about git — its ignore rules are
name patterns, and gitignore is a server-layer concern — so an unfiltered
corpus would let the explorer's silent index-first swap flood search with
build junk (a gitignored dist/ of 100k generated files), with results
flipping depending on whether the index or the walk answered. The corpus is
therefore filtered HERE, with the same check-ignore oracle the walk uses.

What is cached is the VERDICTS — which paths git called ignored — not the
filtered entry list, and each verdict is stamped with the ORACLE that decided
it. Those two choices are the whole design:

  * PER INDEX ROOT, NOT PER REQUEST. The cache used to be an LRU of four
    filtered lists keyed on the REQUESTED root. The home page asks for one
    root and kept its slot; the in-folder search asks with the CURRENT FOLDER,
    so every folder was a distinct key and browsing five folders evicted the
    first — re-paying a full check-ignore sweep over that folder's entire
    recursive subtree. Verdicts are keyed on the enclosing INDEX ROOT and the
    entry rels are re-based onto it, so a folder's corpus is answered out of
    its root's accumulated verdicts and browsing costs nothing.
  * A VERDICT IS ONLY REUSABLE UNDER THE ORACLE THAT PRODUCED IT. Scope is
    discovered per request (below), so two requests can consult genuinely
    different rules for the same path — and a narrower one is the one that
    misses rules, never the one that invents them. Reuse is therefore
    conditional on the two requests agreeing about WHICH oracle decides the
    path, not merely on the path having been seen. Without that condition the
    first version of this pool was actively wrong: an in-folder search of a
    gitignored `proj/dist` finds no `.gitignore` in its own corpus, answers
    "nothing ignored", and — pooled as fact — suppressed every later home
    search from ever asking, so the build directory leaked into search
    permanently. Same through the other door for a `q`- or `limit`-narrowed
    payload, and for a subset that can see only a NESTED marker while the
    outer one that overrides it is out of view.

An entry no oracle in this request can decide is neither filtered NOR pooled.
That is the under-filtering bias below, kept honest: not deciding is allowed,
recording a non-decision as a decision is not.

A completed scan no longer costs a full re-sweep. The pool carries its
verdicts across index generations — a generation stamp is not part of the key
at all — and queries only the paths it has not decided under the current
oracle, so the first search after a scan pays for what CHANGED rather than
~1s per 74k entries for everything. The staleness that buys is bounded by
VERDICT_MAX_AGE_S alone: an EDITED .gitignore (in either direction — a rule
added, or a rule removed and its files still invisible) is not visible to any
of the machinery here, so time is the only thing that can bound it, and the
sweep therefore happens on age whatever the index is doing. Newly-appeared
paths are never stale — they are exactly the ones that get queried.

The pool is also PERSISTED, one file per index root under the index state
dir. A sweep of a home-sized corpus is ~1.5s of check-ignore, and it used to
be re-bought on the first keystroke after every server start — a cost with no
cause, since a verdict is a fact about a path and an oracle, not about a
process. What does NOT change is the bound: `swept_at` is stored and the
loaded pool is discarded on age exactly as an in-memory one is, so
persistence alone only helps a restart within VERDICT_MAX_AGE_S. The other
half of the design is the startup warm (server/routers/index.py), which
re-sweeps at idle; together they mean no keystroke pays for a sweep.

Scope is an approximation of the walk's online discovery, biased to
under-filter (pruning is an optimization, never a hard dependency — the
walk takes the same posture when git is missing):

  * root inside a repo -> ONE oracle at the repo toplevel; `git -C toplevel
    check-ignore` cascades every nested .gitignore below it by itself.
  * root outside any repo -> an oracle at each corpus directory that holds a
    `.gitignore`, the OUTERMOST marker claiming each entry; the oracle's
    work-tree graft cascades the nested ones below it. A repo whose rules
    live only in .git/info/exclude (no .gitignore anywhere) goes unfiltered.

Mount-backed roots never get here: the index refuses to scan them, so they
are never `covered` — no check-ignore (kernel I/O) can be aimed at a mount.
"""
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from threading import Event as _Event
from threading import Lock

from fused_render.server.gitignore import _IgnoreOracle, _repo_toplevel

logger = logging.getLogger(__name__)

# Verdict pools kept per INDEX ROOT. Four is generous now that folders share
# their root's pool: a machine with more than four configured scan roots is
# already unusual, and a pool evicted here costs one re-sweep, not correctness.
_CACHE_ROOTS = 4
_cache: "OrderedDict[str, _Verdicts]" = OrderedDict()
_cache_lock = Lock()

# base -> an Event set when the sweep currently running for that base has
# folded its verdicts into the pool. One entry per base being swept, removed by
# the sweeper itself (in a `finally`, so a raising sweep releases its waiters).
# This is NOT the lock the docstring below refuses to hold across git: it
# serializes callers asking for the SAME base's verdicts, and leaves every
# other base free.
_inflight: dict = {}

# How long a caller may wait on someone else's sweep before doing its own. The
# floor under a sweeper that dies in a way its `finally` cannot see (a killed
# daemon thread at interpreter exit): the cost of being wrong is one duplicate
# sweep — what every caller did before this existed — so it is set generously
# against the ~1.5 s a home-sized sweep takes rather than tightly.
SWEEP_WAIT_MAX_S = 30.0

# How long a pool may live before it is discarded and git is re-asked about
# everything.
#
# This is the bound on the one staleness verdict reuse introduces, and it is
# the ONLY thing that can bound it: an edited .gitignore moves nothing this
# module or the index can observe, in either direction — a rule added leaves a
# build directory showing in search, a rule removed leaves its files invisible
# to search. So the sweep is on age alone. It used to be ANDed with an index
# generation change, which meant a settled index never swept at all and
# "bounded" was simply false.
#
# Five minutes is chosen against what it costs to be wrong (a few minutes of a
# wrong answer about files the user just re-scoped) versus what a sweep costs
# (seconds on a 315k corpus, paid by whichever search runs next). The pool
# absorbs everything else — scan completions, new files, folder browsing — so
# this is the only sweep there is.
VERDICT_MAX_AGE_S = 300.0

# The name of "no oracle in this request's scope can decide this entry". Not a
# verdict: such an entry is passed through unfiltered and left out of the pool.
_UNDECIDED = ""

# How often one root's pool may be written back. A sweep tops the pool up on
# every request that sees new paths, and the file is the whole pool each time
# (~200k rels on a home dir), so writing per top-up would put a multi-megabyte
# serialize on the search path. The FIRST save of a root is not debounced —
# that is the big one the warm buys, and it must reach disk before the process
# can exit.
_SAVE_MIN_INTERVAL_S = 60.0

# root -> when THIS process last wrote its pool. Only ever keyed on an actual
# index root (filter_corpus refuses to persist anything else, and that is the
# reason it refuses), so it is bounded by the number of configured scan roots —
# a handful — and needs no eviction. Entries for roots evicted from `_cache`
# are harmless: a stale stamp only delays one write.
_saved_at: dict = {}


class _Verdicts:
    """git's answers for one index root, accumulated across requests.

    `decider` maps a rel (relative to the index root) to the NAME of the oracle
    that answered for it; `ignored` is the subset git called ignored. A rel is
    a cache hit only when the current request would consult the same oracle —
    which is what makes a corpus that grew by 200 files cost 200 queries rather
    than 315,000, without letting a request that could not see the rules answer
    for one that can.
    """

    __slots__ = ("swept_at", "ignored", "decider")

    def __init__(self, swept_at: float):
        # When this pool was created. Never moved by an incremental top-up, or
        # the staleness bound would never be reached on a corpus that keeps
        # growing.
        self.swept_at = swept_at
        self.ignored: set = set()
        self.decider: dict = {}


def filter_corpus(out: dict, index_root: str | None = None) -> dict:
    """`search_under`'s response with gitignored entries removed.

    Only a covered response with entries is touched; `total` is recomputed and
    `truncated` preserved (the cap was applied to the unfiltered set, so "there
    was more" stays true).

    `index_root` is the configured scan root this request's folder lives under,
    and is what the verdict pool is keyed on. Omitted (or not actually an
    ancestor of the requested root) it falls back to the requested root, which
    is correct but gives that folder a pool of its own.
    """
    root = out.get("root") or ""
    entries = out.get("entries")
    if not out.get("covered") or not entries or not root:
        return out
    base = index_root or root
    prefix = _rel_prefix(base, root)
    # Only a base the CALLER identified as an index root is written to disk.
    # The fallbacks below are not roots and not bounded: a folder outside every
    # configured scan root gets a pool of its own, and persisting those would
    # grow `_saved_at` and the gitignore/ directory by one entry per such
    # folder ever searched, forever — the in-memory pool is LRU-capped at
    # _CACHE_ROOTS, but nothing reclaims either of those (prune_runs touches
    # run dirs only, and delete_store deliberately leaves gitignore/ alone).
    # In memory is all such a folder ever needed: the restart-warm case is
    # about the home page's root, which always arrives here as one.
    persist = index_root is not None and prefix is not None
    if prefix is None:
        base, prefix = root, ""
    # Indexes, not rels: a corpus may legitimately repeat a rel (it cannot
    # today, but nothing here should depend on that) and the decider is
    # per-entry.
    drop = _pooled_verdicts(base, prefix, root, entries, persist=persist)
    if not drop:
        return {**out, "total": len(entries)}
    kept = [e for i, e in enumerate(entries) if i not in drop]
    return {**out, "entries": kept, "total": len(kept)}


def _rel_prefix(base: str, root: str):
    """`root` as a path prefix relative to `base` ('' or 'a/b/'), or None when
    `root` does not live under `base`."""
    b = base if base == "/" else (base or "").rstrip("/")
    r = root if root == "/" else (root or "").rstrip("/")
    if b == r:
        return ""
    sep = "/" if b == "/" else b + "/"
    if r.startswith(sep):
        return r[len(sep):] + "/"
    return None


def _pooled_verdicts(base: str, prefix: str, root: str, entries: list,
                     persist: bool = True) -> set:
    """The INDEXES into `entries` that git calls ignored.

    Reads what the pool already decided UNDER THE SAME ORACLE, queries git for
    the rest, and folds the answers back in.

    Three kinds of work are kept OUT of `_cache_lock`, for one reason: it is a
    module-global, so anything held across it stops every search in the app —
    including ones for entirely different roots — behind this one. The git
    sweep is seconds; the disk load is a multi-megabyte `json.load` plus a few
    hundred thousand inserts; the snapshot is a full pass over up to 200k rels.
    The lock covers only the pool bookkeeping around them.

    What IS coordinated is duplicate work on the SAME base: `_inflight` makes a
    second caller wait for the sweep already running rather than start an
    identical one. That is the opposite of holding the lock — one base's
    callers queue for one sweep, and other bases are never touched. The race is
    not hypothetical: the startup warm sweeps on a detached thread for exactly
    the ~2.2 s in which the user's first keystroke arrives, and both used to
    build the same 200k-entry query set and shell out to git for it twice.
    """
    # Pure string work, no git: which oracle this request would consult for
    # each entry. Computed for the WHOLE corpus every time — a few tens of
    # milliseconds — because it is half of the cache key.
    top, deciders = _deciders(root, entries, prefix)
    rels = [prefix + e["rel"] for e in entries]
    # A process that has never held this root's pool asks disk before it asks
    # git: the file is a sweep of this same root, by this process before a
    # restart or by another one, and it is age-checked exactly as an in-memory
    # pool is. Speculative and outside the lock — if another thread has landed
    # a pool by the time we look, this one is simply dropped. An EXPIRED
    # in-memory pool deliberately does NOT come back through here: its own
    # saved copy is never newer than it, so reloading it would put the pool
    # back to the age it just aged out of and VERDICT_MAX_AGE_S would never be
    # reachable.
    with _cache_lock:
        known = base in _cache
    loaded = None if known else _load_verdicts(base)

    mine, waited = None, False
    while True:
        now = time.time()
        with _cache_lock:
            pool = _cache.get(base) or loaded
            if pool is None or (now - pool.swept_at) >= VERDICT_MAX_AGE_S:
                pool = _Verdicts(now)
            _cache[base] = pool
            _cache.move_to_end(base)
            while len(_cache) > _CACHE_ROOTS:
                _cache.popitem(last=False)
            drop, want = set(), set()
            for i, (name, _marker) in enumerate(deciders):
                # Nothing in this request's scope can decide this entry. Pass
                # it through (the under-filtering bias) and, crucially, do not
                # record that as a verdict: a wider request must still get to
                # ask.
                if name == _UNDECIDED:
                    continue
                if pool.decider.get(rels[i]) == name:
                    if rels[i] in pool.ignored:
                        drop.add(i)
                else:
                    want.add(i)
            if not want:
                return drop
            waiter = _inflight.get(base)
            if waiter is None or waited:
                # Ours to sweep. `waited` is the second time round:
                # whoever we waited for did not decide our entries (a narrower
                # corpus), so we sweep the remainder rather than queueing
                # behind sweep after sweep for a verdict that is never coming.
                mine = _inflight[base] = _Event()
                break
        # Someone else is asking git for this very base. Their answers land in
        # the pool we just read, so wait and read it again. The timeout is the
        # floor under a sweeper that somehow never finishes: worst case we do
        # what the old code always did and sweep concurrently.
        waiter.wait(timeout=SWEEP_WAIT_MAX_S)
        waited = True

    try:
        fresh = _ignored(root, entries, top, deciders, want)
    finally:
        with _cache_lock:
            if _inflight.get(base) is mine:
                del _inflight[base]
        mine.set()

    # The pool's parts to write back, or None for "nothing to write". One
    # value, not three: they are only ever meaningful together, and three
    # separate optionals let a later edit assign one without the others — a
    # half-built snapshot nothing in the signature would object to.
    to_save: tuple | None = None
    with _cache_lock:
        # A concurrent request may have swept the pool out from under us; its
        # verdicts are no less true, but they belong to the pool that asked for
        # them, so they are simply dropped rather than mixed into a new one.
        if _cache.get(base) is pool:
            for i in want:
                rel = rels[i]
                pool.decider[rel] = deciders[i][0]
                if i in fresh:
                    pool.ignored.add(rel)
                else:
                    # It may have been ignored under a DIFFERENT oracle before;
                    # this request's answer supersedes it, in both directions.
                    pool.ignored.discard(rel)
            if persist and _save_due(base, now):
                # Copied, not snapshotted, under the lock: `dict(...)` and
                # `set(...)` are one C-level pass each, while `_snapshot` is a
                # Python loop over every rel. The copy is also what makes the
                # snapshot safe to build outside — the pool itself keeps being
                # mutated, by this module, only under this lock.
                to_save = (pool.swept_at, dict(pool.decider),
                           set(pool.ignored))
    if to_save is not None:
        _save_verdicts(base, _snapshot(base, *to_save))
    return drop | fresh


# ------------------------------------------------------------ the pool on disk

def _verdicts_path(base: str) -> str:
    """Where one index root's pool lives, under the index state dir.

    A digest rather than the root path itself: a root is an absolute path with
    separators, spaces and any unicode the filesystem allows, and none of that
    belongs in a filename. The root is stored INSIDE the file and checked on
    load, so a truncated digest costs a re-sweep at worst, never another
    tree's verdicts."""
    from fused_render.index.config import load_config

    digest = hashlib.sha1(base.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(load_config().dir, "gitignore", digest[:16] + ".json")


def _snapshot(base: str, swept_at: float, decider: dict, ignored: set) -> dict:
    """The pool as a JSON-able document, grouped by deciding oracle.

    Takes the pool's parts rather than the pool, because it runs OUTSIDE
    `_cache_lock` (a Python pass over up to 200k rels) and the live pool keeps
    being mutated under that lock. The caller copies; this only reads.

    Grouping IS the compaction: `decider` maps every rel to an oracle name, and
    there are a handful of distinct names against up to 200k rels, so writing
    the name once per group instead of once per rel is most of the file. Both
    halves of a verdict round trip exactly — which rels the oracle decided, and
    which of them it called ignored — because reuse is conditional on the
    decider and a lossy `ignored`-only file would silently widen it."""
    groups: dict = {}
    for rel, name in decider.items():
        g = groups.get(name)
        if g is None:
            g = groups[name] = ([], [])
        g[0 if rel in ignored else 1].append(rel)
    return {"root": base, "swept_at": swept_at,
            "verdicts": [[name, ig, kept] for name, (ig, kept) in groups.items()]}


def _load_verdicts(base: str):
    """The persisted pool for `base`, or None — missing, too old, for another
    root, or unreadable in any way. Never raises: this runs on the path that
    answers a search box, and a pool is an optimization."""
    try:
        with open(_verdicts_path(base), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("root") != base:
            return None
        swept_at = float(data["swept_at"])
        # The same bound an in-memory pool lives under, anchored on the sweep
        # that produced these verdicts rather than on the process that read
        # them: an edited .gitignore is invisible to everything here, so age is
        # the only thing that can bound the staleness, restart or no restart.
        if (time.time() - swept_at) >= VERDICT_MAX_AGE_S:
            return None
        pool = _Verdicts(swept_at)
        for name, ignored, kept in data["verdicts"]:
            if not name:  # _UNDECIDED is not a verdict and is never written
                return None
            for rel in ignored:
                pool.decider[rel] = name
                pool.ignored.add(rel)
            for rel in kept:
                pool.decider[rel] = name
        return pool
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - corrupt, truncated, hand-edited: re-sweep
        logger.debug("unreadable gitignore verdict pool for %s", base,
                     exc_info=True)
        return None


def _save_due(base: str, now: float) -> bool:
    """Whether `base`'s pool may be written back now, stamping it when it may.

    Called under `_cache_lock`. The FIRST save of a root is never debounced —
    it is the full sweep the startup warm buys, and the whole point is that the
    next process finds it — while later top-ups are, because the file is the
    whole pool every time and a search must not serialize megabytes per
    keystroke."""
    last = _saved_at.get(base)
    if last is not None and (now - last) < _SAVE_MIN_INTERVAL_S:
        return False
    _saved_at[base] = now
    return True


def _save_verdicts(base: str, data: dict) -> None:
    """Write the pool atomically (tmp + rename, as `index/store._write_manifest`
    does) so a reader never sees half of one, and never raise.

    Inline on the thread that swept, not a background one: the sweep this
    follows is seconds of `git check-ignore` and the dump is a fraction of it,
    and in the normal case both are paid by the startup warm rather than by a
    request. The tmp name carries the pid so two processes writing the same
    root cannot land in each other's file."""
    path = _verdicts_path(base)
    tmp = f"{path}.{os.getpid()}.new"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _sweep_tmp(os.path.dirname(path), tmp)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - a cache that cannot be saved is still a cache
        logger.debug("could not save the gitignore verdict pool for %s", base,
                     exc_info=True)
    finally:
        # In a `finally`, not just on the error path: the write can also be
        # abandoned mid-`json.dump` — this runs on the warm's daemon thread,
        # which the interpreter kills outright at exit — and nothing else ever
        # reclaims this directory. A no-op after a successful `os.replace`.
        try:
            os.unlink(tmp)
        except OSError:
            pass


# A `.new` this old cannot belong to a write still in progress: the dump is a
# fraction of a second even for a home-sized pool, and the writer would have
# renamed it. Anything older is the residue of a process that died mid-write.
_TMP_STALE_S = 3600.0


def _sweep_tmp(dirname: str, keep: str) -> None:
    """Reclaim `.new` files left by processes that died mid-write.

    The `finally` above cannot cover a daemon thread killed at interpreter
    exit, and a pid is reused, so a stale tmp can even carry OUR pid. One
    listdir of a directory holding a handful of files, on the save path only.
    Never raises: a cache that cannot tidy up is still a cache."""
    now = time.time()
    try:
        names = os.listdir(dirname)
    except OSError:
        return
    for name in names:
        if not name.endswith(".new"):
            continue
        p = os.path.join(dirname, name)
        if p == keep:
            continue
        try:
            if (now - os.stat(p).st_mtime) > _TMP_STALE_S:
                os.unlink(p)
        except OSError:
            pass


def _deciders(root: str, entries: list, prefix: str):
    """`(repo_toplevel, [(name, marker), ...])` — the oracle for each entry.

    `name` identifies that oracle relative to the INDEX ROOT, so two requests
    scoped differently can be compared: an in-folder search of `~/proj` and a
    home search over `~` both name `~/proj`'s .gitignore "dir:proj" and may
    share its verdicts, while a request that could see no marker at all names
    `_UNDECIDED` and shares nothing. That comparison is the whole guard.

    `marker` is the same oracle as a rel under `root` ('' = root itself), which
    is what actually builds it; None when nothing in scope decides the entry.

    Discovery reads the WHOLE `entries` list, never the subset being queried:
    the `.gitignore` that decides a path is itself a corpus entry, and once its
    own verdict is pooled it drops out of the query set — narrowing scope with
    it would silently stop filtering everything it covers.
    """
    top = _repo_toplevel(root)
    if top is not None:
        # Inside a repo the oracle sits at the TOPLEVEL — an oracle at the
        # searched subfolder would take the no-repo graft path (no .git there)
        # and never see the toplevel's rules. It decides every entry whatever
        # the corpus happens to contain, so this branch has no undecidable
        # case, and its name is the toplevel rather than a corpus rel because
        # that is genuinely a different set of rules from the graft below.
        return top, [("repo:" + top, None)] * len(entries)
    oracle_rels = _oracle_roots(entries)
    if not oracle_rels:
        return None, [(_UNDECIDED, None)] * len(entries)
    out = []
    marker_for_dir: dict = {}
    for e in entries:
        rel = e["rel"]
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        marker = marker_for_dir.get(d, "?")
        if marker == "?":
            marker = _outermost(d, oracle_rels)
            marker_for_dir[d] = marker
        out.append((_UNDECIDED, None) if marker is None
                   else ("dir:" + (prefix + marker).rstrip("/"), marker))
    return None, out


def _ignored(root: str, entries: list, top, deciders: list, want: set) -> set:
    """The indexes in `want` that git calls ignored."""
    if top is not None:
        base = os.path.relpath(root, top).replace(os.sep, "/")
        prefix = "" if base in (".", "") else base + "/"
        # Queries are the entry rels re-based onto the toplevel; git cascades
        # nested .gitignore files from there by itself.
        idxs = sorted(want)
        queries = [prefix + entries[i]["rel"] for i in idxs]
        oracle = _IgnoreOracle(top)
        try:
            verdicts = oracle.ignored(queries)
        finally:
            oracle.close()
        if not verdicts:
            return set()
        return {i for i, q in zip(idxs, queries) if q in verdicts}
    # marker -> [(entry index, path rel to that oracle root)]
    by_oracle: dict = {}
    for i in sorted(want):
        marker = deciders[i][1]
        rel = entries[i]["rel"]
        by_oracle.setdefault(marker, []).append(
            (i, rel if marker == "" else rel[len(marker) + 1:]))
    drop = set()
    for marker, queries in by_oracle.items():
        oracle = _IgnoreOracle(os.path.join(root, marker) if marker else root)
        try:
            verdicts = oracle.ignored([sub for _, sub in queries])
        finally:
            oracle.close()
        if verdicts:
            drop.update(i for i, sub in queries if sub in verdicts)
    return drop


def _oracle_roots(entries: list):
    """Standalone oracle roots as rels ('' = the root itself), outermost
    semantics — the no-repo case only (the repo case is handled in _ignored
    with one oracle at the toplevel). Every corpus dir holding a .gitignore
    is a standalone ignore root, exactly as the walk treats one."""
    markers = set()
    for e in entries:
        rel = e["rel"]
        if rel == ".gitignore":
            return [""]  # the root itself is an ignore root: one oracle, all under it
        if rel.endswith("/.gitignore"):
            markers.add(rel[: -len("/.gitignore")])
    # Keep only outermost markers: a nested one is cascaded by its ancestor's
    # oracle anyway, and querying both would double the git work.
    out = []
    for m in sorted(markers):
        if not any(m.startswith(o + "/") for o in out):
            out.append(m)
    return out


def _outermost(d: str, oracle_rels: list):
    """The outermost oracle rel covering directory rel `d`, or None."""
    best = None
    for o in oracle_rels:
        if o == "" or d == o or d.startswith(o + "/"):
            if best is None or len(o) < len(best):
                best = o
    return best
