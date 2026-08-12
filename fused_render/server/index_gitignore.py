"""Gitignore parity for the index-backed search corpus.

The live walk prunes gitignored entries as it descends (server/walk.py), and
the WalkEntry contract promises nothing ignored ever reaches search results.
The index scan deliberately knows nothing about git — its ignore rules are
name patterns, and gitignore is a server-layer concern — so an unfiltered
corpus would let the explorer's silent index-first swap flood search with
build junk (a gitignored dist/ of 100k generated files), with results
flipping depending on whether the index or the walk answered. The corpus is
therefore filtered HERE, with the same check-ignore oracle the walk uses.

What is cached is the VERDICTS — a set of paths git called ignored — not the
filtered entry list. That is the whole design, and it is what makes the two
things this module used to get wrong impossible rather than guarded:

  * PER INDEX ROOT, NOT PER REQUEST. The cache used to be an LRU of four
    filtered lists keyed on the REQUESTED root. The home page asks for one
    root and kept its slot; the in-folder search asks with the CURRENT FOLDER,
    so every folder was a distinct key and browsing five folders evicted the
    first — re-paying a full check-ignore sweep over that folder's entire
    recursive subtree. Verdicts are keyed on the enclosing INDEX ROOT and the
    entry rels are re-based onto it, so a folder's corpus is answered out of
    its root's accumulated verdicts and browsing costs nothing.
  * SUBSETS CANNOT MASQUERADE AS THE CORPUS. A stored entry LIST is only
    correct for the request that produced it, which is why a `q`-narrowed, a
    `limit`-capped and a `truncated` payload all had to be refused. A verdict
    is a fact about ONE path and is equally true whichever request asked, so a
    partial payload can safely both read and contribute — and a short list can
    never be handed to a later full request under a `truncated` flag taken
    from that request's own payload. `total` is recomputed after filtering.

A completed scan no longer costs a full re-sweep. The cache carries its
verdicts across index generations and queries only the paths it has not seen
before, so the first search after a scan pays for what CHANGED rather than
~1s per 74k entries for everything. The staleness that buys is bounded by
VERDICT_MAX_AGE_S: a file that became gitignored keeps being served until the
next full sweep, and after that long the next generation change throws the
whole set away and re-asks git. Newly-appeared paths are never stale — they
are exactly the ones that get queried.

Scope is an approximation of the walk's online discovery, biased to
under-filter (pruning is an optimization, never a hard dependency — the
walk takes the same posture when git is missing):

  * root inside a repo -> ONE oracle at the repo toplevel; `git -C toplevel
    check-ignore` cascades every nested .gitignore below it by itself.
  * root outside any repo -> an oracle at each corpus directory that holds a
    `.gitignore`, the OUTERMOST marker claiming each entry; the oracle's
    work-tree graft cascades the nested ones below it. A repo whose rules
    live only in .git/info/exclude (no .gitignore anywhere) goes unfiltered.

Scope is per REQUEST, so verdicts pooled under one index root can come from
different scopes — a whole-root request sees .gitignore markers above a
subfolder that a request for that subfolder alone would miss. That only ever
filters MORE, in the direction of what the walk itself would do, so the pool
stays on the honest side of the approximation.

Mount-backed roots never get here: the index refuses to scan them, so they
are never `covered` — no check-ignore (kernel I/O) can be aimed at a mount.
"""
import os
import time
from collections import OrderedDict
from threading import Lock

from fused_render.server.gitignore import _IgnoreOracle, _repo_toplevel

# Verdict pools kept per INDEX ROOT. Four is generous now that folders share
# their root's pool: a machine with more than four configured scan roots is
# already unusual, and a pool evicted here costs one re-sweep, not correctness.
_CACHE_ROOTS = 4
_cache: "OrderedDict[str, _Verdicts]" = OrderedDict()
_cache_lock = Lock()

# How long a pool may be carried across index generations before the next
# generation change discards it and re-asks git about everything.
#
# This is the bound on the one staleness verdict reuse introduces: a path that
# BECAME gitignored (someone edited a .gitignore) keeps being served until the
# sweep. Five minutes is chosen against what it costs to be wrong — a few
# minutes of a build directory still showing in search — versus what a full
# sweep costs on every scan completion, which is the multi-second stall this
# module was rebuilt to remove. Scans complete far more often than .gitignore
# files change.
VERDICT_MAX_AGE_S = 300.0


class _Verdicts:
    """git's answers for one index root, accumulated across requests.

    `seen` is every rel asked about (relative to the index root); `ignored` is
    the subset git called ignored. A rel absent from `seen` has no verdict yet
    and must be queried — which is what makes a corpus that grew by 200 files
    cost 200 queries rather than 315,000.
    """

    __slots__ = ("updated", "swept_at", "ignored", "seen")

    def __init__(self, updated, swept_at: float):
        self.updated = updated
        # When this pool was last started from EMPTY. Not moved by an
        # incremental top-up, or the staleness bound would never be reached on
        # a corpus that keeps growing.
        self.swept_at = swept_at
        self.ignored: set = set()
        self.seen: set = set()


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
    if prefix is None:
        base, prefix = root, ""
    ignored = _pooled_verdicts(base, prefix, root, entries, out.get("updated"))
    if not ignored:
        return {**out, "total": len(entries)}
    kept = [e for e in entries if prefix + e["rel"] not in ignored]
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


def _pooled_verdicts(base: str, prefix: str, root: str, entries: list, updated) -> set:
    """The index-root-relative rels of `entries` that git calls ignored.

    Reads what the pool already knows, queries git for the rest, and folds the
    answers back in. The git work happens OUTSIDE the lock: it is the second
    (or the ten-second) part, and holding a lock across it would serialize
    every search in the app behind one folder's first sweep.
    """
    now = time.time()
    with _cache_lock:
        pool = _cache.get(base)
        if pool is None or (
            pool.updated != updated and (now - pool.swept_at) >= VERDICT_MAX_AGE_S
        ):
            pool = _Verdicts(updated, now)
            _cache[base] = pool
        pool.updated = updated
        _cache.move_to_end(base)
        while len(_cache) > _CACHE_ROOTS:
            _cache.popitem(last=False)
        rels = [prefix + e["rel"] for e in entries]
        known = {r for r in rels if r in pool.ignored}
        unknown = [e for e, r in zip(entries, rels) if r not in pool.seen]
    if not unknown:
        return known
    fresh = _ignored(root, entries, {e["rel"] for e in unknown})
    with _cache_lock:
        # A concurrent request may have swept the pool out from under us; its
        # verdicts are no less true, but they belong to the pool that asked for
        # them, so they are simply dropped rather than mixed into a new one.
        if _cache.get(base) is pool:
            pool.seen.update(prefix + e["rel"] for e in unknown)
            pool.ignored.update(prefix + r for r in fresh)
    return known | {prefix + r for r in fresh}


def _ignored(root: str, entries: list, only: set) -> set:
    """The rels in `only` that git calls ignored, relative to `root`.

    Marker discovery reads the WHOLE `entries` list even though only a subset
    is queried: the `.gitignore` that decides a path is itself a corpus entry,
    and once its verdict is known it drops out of `only` — narrowing scope with
    it would silently stop filtering everything it covers.
    """
    top = _repo_toplevel(root)
    if top is not None:
        # Inside a repo the oracle sits at the TOPLEVEL — an oracle at the
        # searched subfolder would take the no-repo graft path (no .git
        # there) and never see the toplevel's rules. Queries are the entry
        # rels re-based onto the toplevel; git cascades nested .gitignore
        # files from there by itself.
        base = os.path.relpath(root, top).replace(os.sep, "/")
        prefix = "" if base in (".", "") else base + "/"
        rels = [e["rel"] for e in entries if e["rel"] in only]
        if not rels:
            return set()
        oracle = _IgnoreOracle(top)
        try:
            verdicts = oracle.ignored([prefix + r for r in rels])
        finally:
            oracle.close()
        if not verdicts:
            return set()
        return {r for r in rels if prefix + r in verdicts}
    oracle_rels = _oracle_roots(entries)
    if not oracle_rels:
        return set()
    # oracle rel -> [(entry rel, path rel to that oracle root)]
    by_oracle: dict = {rel: [] for rel in oracle_rels}
    marker_for_dir: dict = {}
    for e in entries:
        rel = e["rel"]
        if rel not in only:
            continue
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        marker = marker_for_dir.get(d, "?")
        if marker == "?":
            marker = _outermost(d, oracle_rels)
            marker_for_dir[d] = marker
        if marker is None:
            continue
        sub = rel if marker == "" else rel[len(marker) + 1:]
        by_oracle[marker].append((rel, sub))
    drop = set()
    for marker, queries in by_oracle.items():
        if not queries:
            continue
        oracle = _IgnoreOracle(os.path.join(root, marker) if marker else root)
        try:
            verdicts = oracle.ignored([sub for _, sub in queries])
        finally:
            oracle.close()
        if verdicts:
            drop.update(rel for rel, sub in queries if sub in verdicts)
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
