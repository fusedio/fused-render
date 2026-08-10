"""Gitignore parity for the index-backed search corpus.

The live walk prunes gitignored entries as it descends (server/walk.py), and
the WalkEntry contract promises nothing ignored ever reaches search results.
The index scan deliberately knows nothing about git — its ignore rules are
name patterns, and gitignore is a server-layer concern — so an unfiltered
corpus would let the explorer's silent index-first swap flood search with
build junk (a gitignored dist/ of 100k generated files), with results
flipping depending on whether the index or the walk answered. The corpus is
therefore filtered HERE, with the same check-ignore oracle the walk uses.

Filtered once per compaction, not per keystroke: the verdict for a given
(root, index generation) cannot change, so it is cached against the
manifest's `updated` stamp. The first in-folder search after a scan pays the
git queries (~14µs each, so ~1s on a 74k-entry corpus — still far below the
walk it replaces); every search after that is a cache hit.

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
import os
from collections import OrderedDict
from threading import Lock

from fused_render.server.gitignore import _IgnoreOracle, _repo_toplevel

# Filtered corpora kept per root. Small: entries for a couple of hundred
# thousand files are tens of MB, and a stale generation's cache is dead
# weight the moment `updated` moves.
_CACHE_ROOTS = 4
_cache: "OrderedDict[str, tuple]" = OrderedDict()
_cache_lock = Lock()


def filter_corpus(out: dict, cacheable: bool = True) -> dict:
    """`search_under`'s response with gitignored entries removed.

    Only a covered response with entries is touched; `total` is recomputed
    and `truncated` preserved (the cap was applied to the unfiltered set, so
    "there was more" stays true). The caller passes `cacheable=False` for a
    server-side-filtered query (`q`) or a capped `limit`: either entry list is
    a SUBSET, and the cache holds only the whole-corpus answer the explorer
    asks for. A truncated payload is refused here on the same grounds, since
    the cap can also come from MAX_CORPUS rather than the caller — the cache
    is keyed on the generation alone, so a stored subset would be handed to
    the next full request under a `truncated` flag taken from its own fresh
    payload: a short list presented as the complete corpus."""
    root = out.get("root") or ""
    if not out.get("covered") or not out.get("entries") or not root:
        return out
    cacheable = cacheable and not out.get("truncated")
    updated = out.get("updated")
    if cacheable:
        with _cache_lock:
            hit = _cache.get(root)
            if hit is not None and hit[0] == updated:
                _cache.move_to_end(root)
                return {**out, "entries": hit[1], "total": len(hit[1])}
    entries = _apply(root, out["entries"])
    if cacheable:
        with _cache_lock:
            _cache[root] = (updated, entries)
            _cache.move_to_end(root)
            while len(_cache) > _CACHE_ROOTS:
                _cache.popitem(last=False)
    return {**out, "entries": entries, "total": len(entries)}


def _apply(root: str, entries: list) -> list:
    top = _repo_toplevel(root)
    if top is not None:
        # Inside a repo the oracle sits at the TOPLEVEL — an oracle at the
        # searched subfolder would take the no-repo graft path (no .git
        # there) and never see the toplevel's rules. Queries are the entry
        # rels re-based onto the toplevel; git cascades nested .gitignore
        # files from there by itself.
        base = os.path.relpath(root, top).replace(os.sep, "/")
        prefix = "" if base in (".", "") else base + "/"
        oracle = _IgnoreOracle(top)
        try:
            verdicts = oracle.ignored([prefix + e["rel"] for e in entries])
        finally:
            oracle.close()
        if not verdicts:
            return entries
        return [e for e in entries if prefix + e["rel"] not in verdicts]
    oracle_rels = _oracle_roots(entries)
    if not oracle_rels:
        return entries
    # entry index -> (oracle rel, path rel to that oracle root)
    by_oracle: dict = {rel: [] for rel in oracle_rels}
    marker_for_dir: dict = {}
    for i, e in enumerate(entries):
        rel = e["rel"]
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        marker = marker_for_dir.get(d, "?")
        if marker == "?":
            marker = _outermost(d, oracle_rels)
            marker_for_dir[d] = marker
        if marker is None:
            continue
        sub = rel if marker == "" else rel[len(marker) + 1:]
        by_oracle[marker].append((i, sub))
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
            drop.update(i for i, sub in queries if sub in verdicts)
    if not drop:
        return entries
    return [e for i, e in enumerate(entries) if i not in drop]


def _oracle_roots(entries: list):
    """Standalone oracle roots as rels ('' = the root itself), outermost
    semantics — the no-repo case only (the repo case is handled in _apply
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
