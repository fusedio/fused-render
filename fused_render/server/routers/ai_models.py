"""GET /api/ai-models (+ /revisions) and POST /api/ai-models/delete — the two
routes the "AI Models" page's Local tab is made of.

**Three decorators and their argument handling, and nothing else.** Everything
these routes DO — walking the hub cache, reading what each repo is, the size
arithmetic, the deletions — is `fused_render.ai.hub_cache`, which is where it
belongs: `ai_runtime.py` and `hub_models.py` both read that walk too, and a
module three surfaces depend on should not be reachable only through one
page's router. See that module's docstring for the cache layout and the rules
the numbers obey.

What stays here is what is genuinely HTTP: the `X-Fused` guard on the mutating
POST, the shape of the request body, and turning a bad target into a 404 or a
400 rather than a traceback.
"""
import os

from fastapi import APIRouter, Body, Header

from fused_render.ai.hub_cache import (
    _TargetError,
    _blob_size,
    _delete_repo,
    _delete_revision,
    _listing,
    _refs_by_commit,
    _require_deletable,
    _require_not_in_use,
    _resolve_repo_dir,
    _scan_repo,
    _snapshot_blobs,
    _snapshot_dirs,
    hub_cache_dir,
)
from fused_render.server.common import _error, _require_fused

router = APIRouter()




# GET /api/ai-models/status was here: one isdir(), answering "does this machine
# have a hub cache at all" for the sidebar entry's gate. The entry is
# unconditional now (HF-8, D265) and nothing else ever asked, so the probe went
# with its only caller — `GET /api/ai-models` reports the same fact as `exists`
# for the page's empty state, which is the one reader left.


@router.get("/api/ai-models")
def api_ai_models():
    """Every repo in the hub cache, biggest first.

    Sync `def` on purpose: this walks a tree that can hold tens of thousands of
    blobs, so FastAPI runs it in the threadpool instead of stalling the event
    loop for every other request the page fires.
    """
    return _listing()


@router.get("/api/ai-models/revisions")
def api_ai_models_revisions(repo: str):
    """One repo's revisions, each with the bytes deleting it would actually
    free.

    `size` is the revision's EXCLUSIVE bytes — blobs no other revision
    references — because that is what a delete recovers; `shared` is what it
    holds in common with its siblings, and stays behind. Two revisions of a
    7GB model that differ in a config file are 7GB shared and a few KB each,
    and a row claiming 7GB apiece would be a lie in the one column this page
    exists for.

    Computed on demand rather than in the listing: it resolves every symlink in
    every snapshot, which the biggest-first overview does not need.
    """
    cache_dir = hub_cache_dir()
    try:
        repo_dir = _resolve_repo_dir(cache_dir, repo)
    except _TargetError as e:
        return _error(str(e), status=404)

    blobs_dir = os.path.join(repo_dir, "blobs")
    snapshots_dir = os.path.join(repo_dir, "snapshots")
    per_revision = {e.name: _snapshot_blobs(e.path, blobs_dir) for e in _snapshot_dirs(snapshots_dir)}
    refs_by_commit: dict[str, list[str]] = {}
    for ref, commit in _refs_by_commit(repo_dir).items():
        refs_by_commit.setdefault(commit, []).append(ref)

    revisions = []
    for commit, blobs in per_revision.items():
        others: set[str] = set()
        for other, other_blobs in per_revision.items():
            if other != commit:
                others |= other_blobs
        own = _scan_repo(os.path.join(snapshots_dir, commit))
        revisions.append(
            {
                "commit": commit,
                "refs": sorted(refs_by_commit.get(commit, [])),
                # own.size covers a snapshot dir that holds real files rather
                # than links (Windows), and is 0 in the ordinary symlink case.
                "size": sum(_blob_size(b) for b in blobs - others) + own.size,
                "shared": sum(_blob_size(b) for b in blobs & others),
                "files": len(blobs) + own.files,
                "mtime": own.mtime or None,
            }
        )
    revisions.sort(key=lambda r: (-r["size"], r["commit"]))
    return {"repo": repo, "revisions": revisions}


@router.post("/api/ai-models/delete")
def api_ai_models_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete named repos and/or revisions, then answer with the fresh listing.

    Body: `{"targets": [{"dir": "models--org--name", "revision": "<sha>"|null}]}`.
    A missing `revision` deletes the whole repo folder.

    The reply is the same shape `GET /api/ai-models` returns, plus `freed`
    and `failures`, so the page swaps in state it just re-read from disk rather
    than patching rows it hopes are still true. Guarded by `X-Fused` (D3) like
    every mutating POST: this one removes multi-GB directories, and a blind
    cross-origin POST must not reach it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    targets = body.get("targets")
    if not isinstance(targets, list) or not targets:
        return _error("'targets' must be a non-empty list")

    cache_dir = hub_cache_dir()
    freed = 0
    failures = []
    for target in targets:
        if not isinstance(target, dict):
            failures.append({"dir": None, "revision": None, "error": "target must be an object"})
            continue
        name, revision = target.get("dir"), target.get("revision")
        try:
            repo_dir = _resolve_repo_dir(cache_dir, name)
            _require_deletable(repo_dir)
            # Both kinds: a revision of a loaded model is the revision it is
            # holding open, so "just one revision" is not the safer request it
            # looks like.
            _require_not_in_use(os.path.basename(repo_dir))
            # `revision is None` is the whole repo; anything else is a revision
            # and must survive _segment. Testing truthiness instead would turn a
            # malformed revision ("", 0) into "delete the entire repo" — the
            # widest possible reading of the narrowest possible request.
            freed += (
                _delete_repo(repo_dir) if revision is None else _delete_revision(repo_dir, revision)
            )
        except _TargetError as e:
            failures.append({"dir": name, "revision": revision, "error": str(e)})
        except OSError as e:
            # Permission, a file held open, a disk error: this target failed and
            # the rest of the batch still runs.
            failures.append({"dir": name, "revision": revision, "error": str(e)})
    return {**_listing(), "freed": freed, "failures": failures}
