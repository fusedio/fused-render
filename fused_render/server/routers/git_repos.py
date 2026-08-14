"""GET /api/git-repos — git repositories on this machine, for the Explorer
homepage's "Repos" tab.

Repo-ness is an INDEX FACT, not a filesystem probe. `.git` is a leaf directory
(`ignore.LEAF_DIR_NAMES`): the scan records one dirs row for it and never lists
its contents, so "which directories are repositories" is exactly "which dirs rows
are named `.git`", and each repo root is that row's parent. One SQL query over
`dirs.parquet`, zero stats, zero subprocesses — no `git rev-parse`, no `git` at
all.

That replaces the first cut of this endpoint, which read every indexed directory
out of the same table and asked `os.path.isdir(d + "/.git")` about each: ~71k
stats per request on a real home. It also inherits the index's safety
properties rather than re-earning them — the table is `st_dev`-confined
(`scan.scan_dir_once`), `MountGuard`-screened and node_modules/.venv-pruned, so
this endpoint cannot touch a network mount at all. There is deliberately no walk
or stat fallback: "the index cannot answer yet" is a state the tab renders, not a
cue to crawl.

A `.git` DIRECTORY is what gets a row, so linked worktrees and modern submodules
— which mark themselves with a `.git` FILE — are absent from the dirs table and
therefore naturally excluded. Normal repos only, which is the intent.

THREE THINGS THAT LOOK WRONG AND ARE NOT
----------------------------------------
1. `junk_path` is applied to the PARENT, never to the row. `.git` is itself a
   dot-segment, so screening the raw row would reject every repository on the
   machine. The parent is the path the user is offered, so the parent is what
   gets held to the explorer's standard for rows that did not come from its walk
   (`walk.junk_path`, shared with /api/search/files). Without it the tab is
   mostly other people's checkouts — `~/.local/share/nvim/lazy/*`,
   `~/.oh-my-zsh/custom/plugins/*`, `~/.claude/plugins/cache/temp_git_*`
   outnumbered the user's own repos better than 2:1 on the first machine this ran
   on. Named cost: a repo you deliberately keep inside a dotted directory is not
   listed here.
2. `MountGuard` still screens the parent, even though the index is already
   guarded at scan time. It is the layer that holds if an index written by an
   older build carries rows a newer guard would have pruned, and it costs nothing
   — the roots resolve once per request and every check is string comparison.
3. A STALE index still answers. If the dirs table yields `.git` rows they are
   served — while a scan is in flight, while the applied ignore signature is a
   generation behind, whatever has changed on disk since. An index is ALWAYS
   slightly behind the filesystem, so treating "behind" as "unusable" would refuse
   to answer approximately always; the response carries `stale: true` instead and
   the tab shows the list with a quiet note. Staleness is the normal condition, not
   an error.

   The rules signature keeps exactly ONE job, and it is not a veto on results. It
   separates two kinds of zero:

     * zero rows because the RULE NEVER RAN — an index predating `.git` becoming a
       leaf dir has no `.git` rows to find, and answering "no repositories on this
       machine" from it is a confident falsehood. This is the original silent lie
       and the reason any of this exists; it reports not-ready (`reason:
       "outdated"`) so the tab can say a rebuild is coming.
     * zero rows because the machine GENUINELY HAS NO REPOS — the rule ran and
       found nothing. A real answer, served as `{indexed: true, repos: []}`.

   The test is on RAW row count, before screening. Screening can legitimately take
   real rows down to zero (every repo on the machine inside a dotted directory),
   and that is a rule that DID run — so it is an answer, not a migration.

   Signatures are still checked per configured ROOT (see `_fresh` on why the
   rootless form is not enough); the consequence of a mismatch is now `stale`
   rather than silence.

   Serving a stale list does not mean ignoring staleness: the request also fires
   the ordinary background freshness check (`_note_tab_opened`), so a tab open is
   a chance for the index to catch up, never a wait for it to.

Order is the index's own row order, which is path order: the compaction writes
dirs.parquet `ORDER BY dir` (`store._compact_locked`), and stripping the trailing
`/.git` preserves that order. Free, deterministic, no sort. Nested repos appear
alongside their parent rather than being collapsed away — a repo inside a repo is
still a repo you might want to open.
"""
import logging
import os

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from fused_render._view_url_codec import canonical_fs_path
from fused_render.index import runner
from fused_render.index.config import load_config
from fused_render.index.ignore import MountGuard
from fused_render.index.query import dirs_src
from fused_render.index.store import applied_ignore_sig, read_manifest
from fused_render.server.common import _error
from fused_render.server.walk import junk_path

logger = logging.getLogger(__name__)
router = APIRouter()

# The leaf directory whose presence marks a repository. Deliberately spelled out
# here rather than derived from LEAF_DIR_NAMES: that tuple is "directories the
# scan records opaquely" and may grow to hold names that have nothing to do with
# git, at which point iterating it here would start listing non-repositories.
GIT_DIR = ".git"


class IndexUnreadable(RuntimeError):
    """The index is on disk but its dirs table could not be queried. Distinct
    from "not built yet"/"stale", which are states the tab renders rather than
    errors."""


def _scanning(cfg) -> bool:
    """Whether any scan is in flight — the same fact /api/index/status reports
    under this name, so the tab can say "still building" while the first scan
    runs. Best-effort: a run directory that cannot be read must not turn a
    perfectly answerable repo list into an error."""
    try:
        return any(r["running"] for r in runner.list_runs(cfg, limit=20)["runs"])
    except Exception:  # noqa: BLE001 - telemetry only, never the answer
        logger.debug("could not read index run state", exc_info=True)
        return False


def _fresh(cfg) -> bool:
    """Whether every configured root's slice of the index was built under today's
    rules. NOT a gate on serving results (module docstring point 3) — it decides
    `stale`, and it separates "no rows because the leaf rule never ran" from "no
    rows because there are no repos".

    Every root is checked INDIVIDUALLY, and the rootless `applied_ignore_sig(cfg)`
    is deliberately not used for this. That form compares only the values in the
    file's `roots` map and never consults `legacy_sig` — so on a store migrated
    from the pre-per-root format, the moment the FIRST root is rescanned and
    stamped, `{"roots": {"/a": current}, "legacy_sig": old}` reads as "everything
    matches" while `/b` is still described by nothing but the stale legacy sig and
    still has no repo rows. The per-root form returns `legacy_sig` for exactly
    those roots (`roots.get(root, data.get("legacy_sig"))`), which is the answer
    that makes them visible as stale.

    A root that has never been stamped at all answers None, which is not the
    current signature either — "predates the applied-ignore file" cannot be
    assumed to have been built under today's rules.
    """
    sig = cfg.rules.sig()
    # scan_roots is the definition of "what this index is supposed to cover"
    # (configured roots, else home) and lives with the scan scheduler that acts on
    # it; duplicating the fallback here is how the two would drift.
    from fused_render.server.routers.index import scan_roots

    return all(applied_ignore_sig(cfg, r) == sig for r in scan_roots(cfg))


def _not_ready(cfg, reason: str) -> dict:
    """The index cannot answer. `stale` is False rather than True: there is no list,
    so "the list may be out of date" would be a claim about nothing."""
    return {"indexed": False, "reason": reason, "scanning": _scanning(cfg),
            "stale": False, "repos": []}


def _note_tab_opened(cfg) -> None:
    """Ask the index whether it is behind, in the background, exactly as
    /api/fs/list does for the folder the explorer just opened.

    This tab reads nothing but the index, so without a nudge of its own it is
    the one surface in the app that can never notice a stale one — it would
    happily serve a repo list from a scan hours old, marked `stale: false`,
    because as far as the index knows it IS as fresh as it gets.

    The paths checked are the configured scan ROOTS, because the tab is
    machine-wide: there is no open folder to name, and note_folder_opened only
    acts on a path inside a root anyway. Named limitation, and it is a real one:
    a root's own mtime moves only when its DIRECT entries change, so a repo
    cloned three levels down does not make the root look stale and is not
    detected here. This is a cheap check that occasionally helps, not a
    guarantee that the tab is current — the honest answer to "is this list
    complete" remains `stale` plus the next scheduled scan.

    Every root is offered, but at most ONE check actually runs: the checker is
    serialized by a single non-blocking slot (routers/index._freshness_slot), so
    the second root is simply skipped whenever the first took the slot. That is
    accepted rather than worked around — the roots take turns across requests,
    and a queue of checks behind a tab render is exactly what the slot exists to
    prevent.

    note_folder_opened itself never raises and never blocks (it spawns a
    thread), so this costs the request a lock acquire; the try/except is for the
    config read and for the import, not for the check."""
    try:
        # Function-local, like `_fresh`'s: routers/index.py is the module that
        # owns both the scan roots and the freshness hook, and importing it at
        # module scope would close the cycle between the two routers.
        from fused_render.server.routers.index import note_folder_opened, scan_roots

        for root in scan_roots(cfg):
            note_folder_opened(root)
    except Exception:  # noqa: BLE001 - housekeeping must never become the answer
        logger.debug("could not check index freshness for the repos tab",
                     exc_info=True)


def _repos() -> dict:
    cfg = load_config()
    # Off the event loop already (run_in_threadpool below), and fire-and-forget:
    # the repo list is served from whatever the index holds right now, whether or
    # not this starts a scan.
    _note_tab_opened(cfg)
    # The only genuinely unanswerable state: no store to read. Everything past here
    # queries first and judges freshness second — rows win over signatures.
    if read_manifest(cfg) is None or not os.path.exists(cfg.dirs_parquet):
        return _not_ready(cfg, "no-index")
    import duckdb

    con = duckdb.connect()
    # `dir LIKE '%/.git'` would also match a directory literally named `x/.git`
    # on a platform where that is possible, and would not match a `.git` at the
    # filesystem root — neither matters, but comparing the final component is
    # what is actually meant, and DuckDB evaluates it just as cheaply.
    #
    # No ORDER BY: rows are already in `dir` order on disk (see the module
    # docstring) and dropping a constant-length suffix keeps that order.
    try:
        rows = con.execute(
            f"SELECT dir FROM {dirs_src(cfg)} "
            f"WHERE regexp_extract(dir, '[^/]*$') = '{GIT_DIR}'").fetchall()
    except Exception as e:  # noqa: BLE001 - duckdb's exception tree, flattened
        # An index that exists but cannot be read is a FAILURE, not "not ready":
        # reporting it as the latter would have the tab promise a list that is
        # never coming. Same split /api/search/files draws (503 vs 502).
        logger.exception("the repo list query failed")
        raise IndexUnreadable(type(e).__name__) from e
    fresh = _fresh(cfg)
    # RAW row count, before screening — the one thing the signature still decides.
    # No rows under old rules means the leaf rule never ran and the data does not
    # exist yet; saying "no repositories" from that is the original silent lie.
    # Rows screened down to zero is a different thing entirely: the rule DID run, so
    # the empty answer is real (module docstring point 3).
    if not rows and not fresh:
        return _not_ready(cfg, "outdated")
    guard = MountGuard()
    repos = []
    for (git_dir,) in rows:
        root = git_dir.rpartition("/")[0]
        # A `.git` at the filesystem root has no parent to offer; nothing else
        # can produce an empty root here.
        if not root or junk_path(root) or guard.blocks(root):
            continue
        repos.append({"path": canonical_fs_path(root)})
    scanning = _scanning(cfg)
    # ONE flag for the user-facing question "might this list be out of date?", over
    # two causes that have the same answer and the same remedy (wait). A caller that
    # needs to tell them apart still has `scanning` on its own. Note what is NOT in
    # here: files changed on disk since the scan. Nothing can know that without
    # re-walking, which is the cost this endpoint exists to avoid — so `stale: false`
    # means "as fresh as the index gets", never "identical to the filesystem".
    return {"indexed": True, "reason": None, "scanning": scanning,
            "stale": (not fresh) or scanning, "repos": repos}


@router.get("/api/git-repos")
async def api_git_repos():
    # duckdb blocks, so it runs off the event loop — a homepage tab must not
    # stall the whole server while it lists.
    try:
        return await run_in_threadpool(_repos)
    except IndexUnreadable as e:
        return _error(f"the file index could not be read: {e}", status=502)
