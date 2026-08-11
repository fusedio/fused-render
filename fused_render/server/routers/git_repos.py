"""GET /api/git-repos — git repositories on this machine, for the Explorer
homepage's "Repos" tab.

The candidate directory list comes from the app's own index (`dirs.parquet`),
never from a fresh filesystem walk. That is not an optimisation, it is the
safety property:

  * `dirs.parquet` is a complete directory tree for the scanned roots that has
    ALREADY had `node_modules`/`.venv`/`__pycache__`/… pruned, is confined to
    the root's `st_dev` (`scan.scan_dir_once`), and is `MountGuard`-screened —
    so sourcing candidates from it means this endpoint cannot walk into a
    network mount, and a wedged mount cannot hang it.
  * an `os.walk` of the home directory would re-cross all of that on every
    request. There is deliberately no fallback to one: "the index is not built
    yet" is a state the tab renders (`indexed: false`), not a cue to crawl.

`.git` itself is NOT in the index — it is in `SHARED_IGNORE_DIRS` and
`keep_subdirs` prunes it, so no row exists for any `.git` directory or anything
inside one. So the repos cannot be queried out of the index directly; instead
every indexed directory is confirmed with ONE `os.path.isdir(d + "/.git")`.
That is ~1 cheap stat per indexed directory (~71k on a real home, tens of ms),
and no subprocess: no `git rev-parse`, no `git` at all.

A `.git` DIRECTORY is the test. A linked worktree and a modern submodule mark
themselves with a `.git` FILE, so both are naturally excluded — normal repos
only, which is the intent.

Hidden and machine-managed paths are screened out with the explorer's one
standard for rows that did not come from its walk (`walk.junk_path`, shared with
/api/search/files): a dot-segment or a WALK_IGNORE_DIRS segment anywhere in the
path drops the row. Without it this tab is mostly other people's checkouts —
`~/.local/share/nvim/lazy/*`, `~/.oh-my-zsh/custom/plugins/*`,
`~/.claude/plugins/cache/temp_git_*` — which outnumbered the user's own repos
better than 2:1 on the first machine this ran on. The named cost: a repo you
deliberately keep inside a dotted directory is not listed here.

Order is the index's own row order, which is path order: the compaction writes
dirs.parquet `ORDER BY dir` (`store._compact_locked`). Free, deterministic, and
no extra stat or sort to get it. Nested repos therefore appear alongside their
parent rather than being collapsed away — a repo inside a repo is still a repo
you might want to open.
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
from fused_render.index.store import read_manifest
from fused_render.server.common import _error
from fused_render.server.walk import junk_path

logger = logging.getLogger(__name__)
router = APIRouter()


class IndexUnreadable(RuntimeError):
    """The index is on disk but its dirs table could not be queried. Distinct
    from "not built yet", which is a state the tab renders rather than an
    error."""


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


def _repos() -> dict:
    cfg = load_config()
    # BOTH are required, and neither implies the other: the manifest is written
    # by a compaction that had file shards, while dirs.parquet is the table this
    # endpoint actually reads. Missing either is "not indexed yet" — reported as
    # such rather than as an empty list, because "no repos on this machine" and
    # "we have not looked yet" are different answers.
    if read_manifest(cfg) is None or not os.path.exists(cfg.dirs_parquet):
        return {"indexed": False, "scanning": _scanning(cfg), "repos": []}
    import duckdb

    con = duckdb.connect()
    # No ORDER BY: the rows are already in `dir` order on disk (see the module
    # docstring), and sorting 70k+ strings again to reproduce that would be work
    # bought with nothing.
    try:
        rows = con.execute(f"SELECT dir FROM {dirs_src(cfg)}").fetchall()
    except Exception as e:  # noqa: BLE001 - duckdb's exception tree, flattened
        # An index that exists but cannot be read is a FAILURE, not "not indexed
        # yet": reporting it as the latter would have the tab promise a list that
        # is never coming. Same split /api/search/files draws (503 vs 502).
        logger.exception("the repo list query failed")
        raise IndexUnreadable(type(e).__name__) from e
    # Resolved once for the whole request; every `blocks()` call after that is
    # pure string comparison, no syscall. The index is already guarded during the
    # scan, so this is belt-and-braces — but it is the layer that keeps a stat
    # from ever reaching a mount tree if an older index on disk holds rows a
    # newer guard would have pruned.
    guard = MountGuard()
    repos = []
    for (d,) in rows:
        # `junk_path` before the stat, so the screened-out majority costs no
        # syscall at all. On a real home this is the difference between 69 rows
        # and 21: everything under ~/.local/share/nvim/lazy, ~/.oh-my-zsh and
        # ~/.claude/plugins/cache is a package manager's checkout, not a repo
        # anyone opens. It is the same standard /api/search/files and
        # /api/fs/walk hold their results to — a hidden path never surfaces in
        # the explorer, and a tab whose first cards are `temp_git_1786451303910`
        # is not a list of your repos.
        if not d or junk_path(d) or guard.blocks(d):
            continue
        # No try/except around the probe: os.path.isdir swallows OSError itself
        # and answers False, which is exactly right here — a directory deleted or
        # turned unreadable since the scan is dropped, the same way
        # /api/claude-sessions drops a folder no longer on disk. This list exists
        # to be opened.
        if os.path.isdir(os.path.join(d, ".git")):
            repos.append({"path": canonical_fs_path(d)})
    return {"indexed": True, "scanning": _scanning(cfg), "repos": repos}


@router.get("/api/git-repos")
async def api_git_repos():
    # duckdb plus tens of thousands of stats block, so it runs off the event
    # loop — a homepage tab must not stall the whole server while it lists.
    try:
        return await run_in_threadpool(_repos)
    except IndexUnreadable as e:
        return _error(f"the file index could not be read: {e}", status=502)
