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
3. An index whose applied ignore signature does not match the current one is
   reported as NOT READY, not as an empty list. This is the migration case and it
   is the one way this endpoint could ship a silent lie: `.git` moving out of the
   ignore list and into the leaf rules changed `IgnoreRules.sig()`, which forces
   a full rescan — but until that rescan finishes, an index already on disk has
   no `.git` rows whatsoever. A pure query would then answer
   `{indexed: true, repos: []}` and tell the user, with total confidence, that
   they have no repositories. So the signature is checked and a stale index is
   reported exactly as a missing one.

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


def _usable(cfg) -> bool:
    """Whether the index on disk can answer this question at all.

    Three ways it cannot, all reported identically to the caller:
      * no manifest — nothing has ever compacted;
      * no dirs.parquet — the table this endpoint reads is the one that matters,
        and the manifest does not imply it;
      * an applied ignore signature that is not the current one — the index was
        built under different rules, so it predates `.git` being recorded and has
        no `.git` rows to find. See the module docstring's point 3.

    `applied_ignore_sig(cfg)` with no root answers "do ALL recorded roots match
    the current rules", which is the right question: a machine indexing two roots
    where only one has been rescanned can still be missing every repo under the
    other.
    """
    if read_manifest(cfg) is None or not os.path.exists(cfg.dirs_parquet):
        return False
    # None means "an index predating the applied-ignore file", which cannot be
    # assumed to have been built under the current rules either.
    return applied_ignore_sig(cfg) == cfg.rules.sig()


def _repos() -> dict:
    cfg = load_config()
    if not _usable(cfg):
        return {"indexed": False, "scanning": _scanning(cfg), "repos": []}
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
    guard = MountGuard()
    repos = []
    for (git_dir,) in rows:
        root = git_dir.rpartition("/")[0]
        # A `.git` at the filesystem root has no parent to offer; nothing else
        # can produce an empty root here.
        if not root or junk_path(root) or guard.blocks(root):
            continue
        repos.append({"path": canonical_fs_path(root)})
    return {"indexed": True, "scanning": _scanning(cfg), "repos": repos}


@router.get("/api/git-repos")
async def api_git_repos():
    # duckdb blocks, so it runs off the event loop — a homepage tab must not
    # stall the whole server while it lists.
    try:
        return await run_in_threadpool(_repos)
    except IndexUnreadable as e:
        return _error(f"the file index could not be read: {e}", status=502)
