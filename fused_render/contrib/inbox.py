"""The author's inbox (contract §9.2): the open proposals on an app of her
own, each with Claude's safety review, plus Try, Accept and Decline.

GITHUB IS THE TRUTH. The list is `gh.list_open_prs`, cached per repo for
LIST_TTL_S; nothing here keeps a proposal state of its own. A proposal
opened on github.com shows up like one sent from fused (`from_fused` says
which — fused's footer marker in the body).

THE REVIEW reads the proposal ON THIS MACHINE before anything in it runs:
the proposal's head (`pull/<N>/head`) and the author's branch are fetched by
URL into private refs of her repo (`refs/fused/pr/<N>`, `…/<N>-base`) —
without the process-wide mutation slot, because it is a network call that
can take a minute and the slot gates every repo's auto-sync (`gh.fetch`
documents why a fetch into private refs cannot race auto-sync's) — and the diff
is `merge-base(base, head)..head`: exactly what the proposal brings, whatever
the author pushed since. The review is cached on
disk per head (`<home>/contrib/reviews/<owner>__<repo>/<N>-<head>.json`), so
it runs once per version of a proposal; a review whose model call failed is
not cached (it is a fact about the network, not the proposal) and is redone
once its job ages out. At most REVIEW_SLOTS reviews run at once; the rest
read "pending".

ACCEPT requires a review of the proposal's CURRENT head, merges with that
head as GitHub's `sha` guard (GitHub refuses if the contributor sent more
since), using the first method the repo allows (merge > squash > rebase),
then syncs the author's own copy right away through `git_upstream.sync_repo`
(the engine owns it: her unsent edits are replayed on top and sent), and
throws away the Try copy.
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import threading
import time

from fused_render.contrib import jobs
from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

LIST_TTL_S = 120.0
REVIEW_SLOTS = 2
FUSED_MARKER = "<!-- fused:proposal v1 -->"
_FETCH_TIMEOUT_S = 60.0
_MAX_DIFF_BYTES = 2_000_000

_lock = threading.Lock()
_lists: dict = {}          # repo -> (fetched_at, [pr])
_waiting: set = set()      # (app_dir, number) reviews queued behind the slots
_asked: dict = {}          # (app_dir, number) -> head the last review job read
_review_slots = threading.BoundedSemaphore(REVIEW_SLOTS)


def _mod(name):
    import importlib

    return importlib.import_module(f"fused_render.contrib.{name}")


def _gu():
    from fused_render import git_upstream

    return git_upstream


def _key(app_dir: str) -> str:
    """One spelling per folder (the store's key), so jobs, caches and the
    review bookkeeping all agree whichever path a caller holds."""
    return _mod("store").key(app_dir)


def _repo_of(app_dir: str) -> tuple[str | None, dict]:
    record = _mod("store").get(app_dir) or {}
    repo = record.get("repo")
    if record.get("kind") != "own" or not isinstance(repo, str) or "/" not in repo:
        return None, record
    return repo, record


def _gh_error(exc) -> bool:
    cls = getattr(_mod("gh"), "GitHubError", None)
    return cls is not None and isinstance(exc, cls)


def _plain_gh(exc, fallback: str = "Couldn't reach GitHub. Try again.") -> str:
    msg = getattr(exc, "message", None)
    return msg if isinstance(msg, str) and msg else fallback


# ------------------------------------------------------------- the listing


def _open_prs(repo: str, *, force: bool = False) -> builtins.list | None:
    now = time.time()
    with _lock:
        hit = _lists.get(repo)
        if hit and not force and now - hit[0] < LIST_TTL_S:
            return hit[1]
    try:
        prs = _mod("gh").list_open_prs(repo)
    except Exception as exc:  # noqa: BLE001 — offline keeps the last answer
        if not _gh_error(exc):
            logger.exception("inbox: listing proposals for %s failed", repo)
        return hit[1] if hit else None
    with _lock:
        _lists[repo] = (now, prs)
    return prs


def invalidate(app_dir: str) -> None:
    repo, _ = _repo_of(app_dir)
    if repo:
        with _lock:
            _lists.pop(repo, None)


def list(app_dir: str, *, review: bool = True, force: bool = False) -> builtins.list:
    """The inbox (InboxItem rows, oldest first). Talks to GitHub at most once
    per LIST_TTL_S per repo (`force` asks now). With `review`, starts a
    review for every item that has none for its current head. Also throws
    away Try copies whose proposal is no longer open. Never raises."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return []
    prs = _open_prs(repo, force=force)
    if prs is None:
        return []
    try:
        _mod("trycopy").sweep(app_dir, [p.get("number") for p in prs
                                        if isinstance(p.get("number"), int)])
    except Exception:  # noqa: BLE001
        logger.exception("inbox: try-copy sweep failed for %s", app_dir)
    return _items(app_dir, repo, prs, start_reviews=review)


def cached(app_dir: str) -> builtins.list | None:
    """The inbox from the last listing, with NO network and no reviews
    started — for GET /api/contrib/app. None when never listed."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return None
    with _lock:
        hit = _lists.get(repo)
    if hit is None:
        return None
    return _items(app_dir, repo, hit[1], start_reviews=False)


def _items(app_dir: str, repo: str, prs, *, start_reviews: bool) -> builtins.list:
    out = []
    for pr in sorted(prs, key=lambda p: p.get("number") or 0):
        number, head = pr.get("number"), pr.get("head_sha")
        if not isinstance(number, int) or not isinstance(head, str):
            continue
        review_obj = _review_for(app_dir, repo, number, head)
        status = "ready" if review_obj is not None else _review_status(app_dir, number, head)
        if status in ("none", "stale"):
            # No review of this head yet, and none on its way.
            if start_reviews:
                review(app_dir, number)
            status = "pending"
        body = pr.get("body") or ""
        from_fused = FUSED_MARKER in body
        if from_fused:
            summary = _description_of(body)
        else:
            summary = review_obj["summary"] if review_obj else ""
        try:
            try_state = _mod("trycopy").status(app_dir, number)
        except Exception:  # noqa: BLE001
            try_state = None
        out.append({
            "number": number, "url": pr.get("url") or "", "title": pr.get("title") or "",
            "author": pr.get("user") or "", "from_fused": from_fused,
            "summary": summary, "review": review_obj, "review_status": status,
            "try": ({"status": try_state["status"], "path": try_state["path"],
                     "url": try_state["url"]} if try_state else None),
            "head_sha": head, "created_at": pr.get("created_at") or "",
        })
    return out


def _description_of(body: str) -> str:
    """A fused-sent body minus its footer and the note line: Claude's
    description, which is the plain summary for these."""
    text = body.split("\n\n---\nSent from Fused")[0]
    text = re.sub(r"\n*\*\*Note from [^*]*:\*\*.*\Z", "", text, flags=re.S)
    return _mod("ai").plain(text)


# ------------------------------------------------------------- the review


def _reviews_dir(repo: str) -> str:
    from fused_render.shell import storage

    owner, _, name = repo.partition("/")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{owner}__{name}")
    return os.path.join(storage.home_dir(), "contrib", "reviews", safe)


def _review_path(repo: str, number: int, head: str) -> str:
    return os.path.join(_reviews_dir(repo), f"{int(number)}-{head}.json")


def _job_kind(number: int) -> str:
    return f"inbox-review-{int(number)}"


def _review_for(app_dir: str, repo: str, number: int, head: str) -> dict | None:
    """The finished review of exactly `head`: from disk, else from the last
    review job (an uncached, model-failed review) while that job is kept."""
    from fused_render.shell import storage

    data = storage.read_json(_review_path(repo, number, head))
    if isinstance(data, dict) and data.get("head_sha") == head:
        return data
    job = jobs.latest(_job_kind(number), app_dir)
    if job and job["status"] == "done" and isinstance(job["result"], dict) \
            and job["result"].get("head_sha") == head:
        return job["result"]
    return None


def _review_status(app_dir: str, number: int, head: str) -> str:
    """"running" | "pending" (queued) | "failed" (for this head) | "none"
    (never asked) | "stale" (asked about an older head)."""
    job = jobs.latest(_job_kind(number), app_dir)
    if job is None:
        return "none"
    if job["status"] == "running":
        with _lock:
            return "pending" if (app_dir, number) in _waiting else "running"
    if job["status"] == "failed":
        # A failure is about the version that was asked; a newer head
        # deserves its own attempt.
        with _lock:
            asked = _asked.get((app_dir, number))
        return "failed" if asked in (None, head) else "stale"
    return "stale"  # done, but for an older head


def review(app_dir: str, number: int) -> str:
    """Start (or join) the review of proposal `number`; returns a job id
    whose result is the Review."""
    app_dir = _key(app_dir)
    return jobs.start(_job_kind(number), app_dir,
                      lambda: _review_job(app_dir, int(number)))


def _review_job(app_dir: str, number: int) -> dict:
    from fused_render.shell import storage

    repo, record = _repo_of(app_dir)
    if repo is None:
        raise PlainError("This app has no proposals.")
    gh = _mod("gh")
    try:
        pr = gh.get_pr(repo, number)
    except Exception as exc:  # noqa: BLE001
        if _gh_error(exc):
            raise PlainError(_plain_gh(exc)) from None
        raise
    if pr.get("state") != "open" or pr.get("merged"):
        raise PlainError("This proposal is no longer open.")
    head = pr.get("head_sha") or ""
    with _lock:
        _asked[(app_dir, number)] = head
    done = _review_for(app_dir, repo, number, head)
    if done is not None:
        return done
    with _lock:
        _waiting.add((app_dir, number))
    try:
        _review_slots.acquire()
    finally:
        with _lock:
            _waiting.discard((app_dir, number))
    try:
        branch = pr.get("base_branch") or record.get("branch") or "main"
        head = _fetch_pr(app_dir, repo, number, branch)
        diff, files = _proposal_diff(app_dir, number)
        result = _mod("ai").safety_review(
            diff=diff, files=files, title=pr.get("title") or "",
            body=pr.get("body") or "", head_sha=head)
    finally:
        _review_slots.release()
    if result.get("model"):
        storage.write_json(_review_path(repo, number, head), result)
    return result


def _fetch_pr(app_dir: str, repo: str, number: int, branch: str) -> str:
    """Fetch the proposal's head and the author's branch into private refs
    (`gh.fetch`); returns the fetched head. A network call, so it does NOT
    hold the process-wide mutation slot (contract §0.9): both refspecs write
    refs under `refs/fused/pr/` that only this module writes, which is what
    `gh.fetch` documents as safe without it."""
    gu = _gu()
    n = int(number)
    fetched = _mod("gh").fetch(app_dir, repo, f"+refs/pull/{n}/head:refs/fused/pr/{n}",
                               f"+refs/heads/{branch}:refs/fused/pr/{n}-base",
                               timeout=_FETCH_TIMEOUT_S)
    head = gu._out(gu._run(app_dir, "rev-parse", "--verify", "-q",
                           f"refs/fused/pr/{n}^{{commit}}")) if fetched else ""
    if not head:
        raise PlainError("Couldn't download this proposal from GitHub. Try again.")
    return head


def _proposal_diff(app_dir: str, number: int) -> tuple[str, builtins.list]:
    gu = _gu()
    n = int(number)
    head, base = f"refs/fused/pr/{n}", f"refs/fused/pr/{n}-base"
    mb = gu._out(gu._run(app_dir, "merge-base", base, head))
    if not mb:
        raise PlainError("This proposal isn't based on your app, so it can't be "
                         "reviewed here.")
    common = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")
    diff_res = gu._run(app_dir, "diff", *common, mb, head, timeout=60.0)
    names = gu._run(app_dir, "diff", "--name-status", "--no-renames", mb, head,
                    timeout=60.0)
    if not gu._ok(diff_res) or not gu._ok(names):
        raise PlainError("Couldn't read this proposal. Try again.")
    # Cut far past ai.MAX_DIFF_CHARS, so a cut diff always reads as oversized.
    diff = diff_res[1][:_MAX_DIFF_BYTES].decode("utf-8", "replace")
    files = []
    for line in gu._out(names).splitlines():
        status, _, path = line.partition("\t")
        if path:
            files.append({"status": status.strip(), "path": path})
    return diff, files


# ------------------------------------------------------- try / accept / decline


def try_open(app_dir: str, number: int, *, confirm: bool = False) -> dict:
    """Make the Try copy. Refuses until the review of the current head is
    ready; on a "look" verdict, refuses with reason "confirm" unless
    `confirm`. `{ok, path, url}` or `{ok: False, reason, message}`."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return {"ok": False, "reason": "not-own", "message": "This app has no proposals."}
    head = _current_head(app_dir, repo, number)
    review_obj = _review_for(app_dir, repo, number, head) if head else None
    if review_obj is None:
        review(app_dir, number)
        return {"ok": False, "reason": "review-pending",
                "message": "Claude is still reading this proposal. You can try it "
                           "once that's done."}
    if review_obj.get("verdict") != "fine" and not confirm:
        return {"ok": False, "reason": "confirm",
                "message": "Claude thinks this proposal needs a look before it "
                           "runs. Try it anyway?"}
    try:
        made = _mod("trycopy").create(app_dir, number, head_sha=review_obj["head_sha"])
    except PlainError as exc:
        return {"ok": False, "reason": "failed", "message": exc.message}
    return {"ok": True, **made}


def try_remove(app_dir: str, number: int) -> dict:
    return _mod("trycopy").remove(_key(app_dir), number)


def _current_head(app_dir: str, repo: str, number: int) -> str | None:
    with _lock:
        hit = _lists.get(repo)
    for pr in (hit[1] if hit else []):
        if pr.get("number") == number:
            return pr.get("head_sha")
    try:
        return _mod("gh").get_pr(repo, number).get("head_sha")
    except Exception:  # noqa: BLE001
        return None


def _merge_method(repo: str) -> str:
    try:
        settings = _mod("gh").repo(repo) or {}
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            logger.exception("inbox: reading %s settings failed", repo)
        settings = {}
    for key, method in (("allow_merge_commit", "merge"),
                        ("allow_squash_merge", "squash"),
                        ("allow_rebase_merge", "rebase")):
        if settings.get(key):
            return method
    return "merge"


def accept(app_dir: str, number: int) -> dict:
    """Merge proposal `number` on GitHub and bring the author's copy up to
    date. Long (a sync runs): the route runs it as a job."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return {"ok": False, "reason": "not-own", "message": "This app has no proposals."}
    gh = _mod("gh")
    try:
        pr = gh.get_pr(repo, number)
    except Exception as exc:  # noqa: BLE001
        if _gh_error(exc):
            return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                    "message": _plain_gh(exc)}
        raise
    author = pr.get("user") or "the sender"
    if pr.get("merged"):
        return _after_accept(app_dir, number, "This proposal is already merged.")
    if pr.get("state") != "open":
        _cleanup(app_dir, number)
        return {"ok": False, "reason": "closed", "message": "This proposal is no longer open."}
    head = pr.get("head_sha") or ""
    review_obj = _review_for(app_dir, repo, number, head)
    if review_obj is None:
        review(app_dir, number)
        return {"ok": False, "reason": "review-needed",
                "message": f"{author} changed this proposal since Claude read it. "
                           "Claude is reading it again — accept it once that's done."}
    try:
        result = gh.merge_pr(repo, number, sha=head, method=_merge_method(repo))
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        reason = getattr(exc, "reason", "failed")
        if reason == "conflict" and getattr(exc, "status", None) == 409:
            # The sender added to it between our read and the merge (the
            # `sha` guard): read the new version first.
            review(app_dir, number)
            return {"ok": False, "reason": "review-needed",
                    "message": f"{author} just changed this proposal. Claude is "
                               "reading it again — accept it once that's done."}
        if reason == "conflict":
            return {"ok": False, "reason": "conflict",
                    "message": f"This proposal no longer fits with your latest "
                               f"version. Ask {author} to update it, or decline it."}
        return {"ok": False, "reason": reason, "message": _plain_gh(exc)}
    if not (result or {}).get("merged"):
        return {"ok": False, "reason": "failed",
                "message": "GitHub didn't merge this proposal. Try again."}
    return _after_accept(app_dir, number,
                         f"Merged. {author} will see it, and everyone who follows "
                         "this app gets it next time they open it.")


def _after_accept(app_dir: str, number: int, message: str) -> dict:
    _cleanup(app_dir, number)
    synced = False
    try:
        out = _gu().sync_repo(app_dir, action="Accept proposal", push=True)
        synced = (out or {}).get("status") == "synced"
    except Exception:  # noqa: BLE001 — the next open syncs it anyway
        logger.exception("inbox: syncing %s after accept failed", app_dir)
    return {"ok": True, "merged": True, "synced": synced, "message": message}


def decline(app_dir: str, number: int, note: str = "") -> dict:
    """Close proposal `number`, with `note` as a comment to its sender."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return {"ok": False, "reason": "not-own", "message": "This app has no proposals."}
    note = (note or "").strip()
    try:
        _mod("gh").close_pr(repo, number, comment=note or None)
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                "message": _plain_gh(exc)}
    _cleanup(app_dir, number)
    return {"ok": True, "message": "Declined."}


def _cleanup(app_dir: str, number: int) -> None:
    try:
        _mod("trycopy").remove(app_dir, number)
    except Exception:  # noqa: BLE001
        logger.exception("inbox: removing try copy failed")
    invalidate(app_dir)
