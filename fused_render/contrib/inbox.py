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
(her own-copy sync, §16.7: her unsent edits go on top and are published),
and throws away the Try copy.

NO LONGER FITS (§16.6). Each item carries `fits` — GitHub's own `mergeable`
for the proposal against her latest (None while GitHub has not worked it
out), read per proposal with the listing and cached alongside it. Two ways
out besides Decline: **Ask <sender> to update** (`ask_update`: a comment
carrying `gh.ASK_UPDATE_MARKER`, which the sender's next refresh shows with
an Update button) and **Merge with Claude** (`merge_start`): her latest is
merged into the proposal in a scratch worktree of HER repo (merge_job
purpose "proposal"), and keeping the result (`merge_accepted`) pushes that
merge onto the proposal's own branch through maintainer edits — never
force, never into her folder — after which the new head is reviewed again
like any other change to a proposal. Her copy only takes the work when she
accepts the proposal.
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

from fused_render.contrib import jobs
from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

LIST_TTL_S = 120.0
REVIEW_SLOTS = 2
#: Review files kept per repo (newest first); a closed proposal's go at once.
REVIEWS_KEEP = 50
FUSED_MARKER = "<!-- fused:proposal v1 -->"
ALREADY_ACCEPTED = "Already accepted."
_FETCH_TIMEOUT_S = 60.0
_MAX_DIFF_BYTES = 2_000_000

_lock = threading.Lock()
_lists: dict = {}          # repo -> (fetched_at, [pr])
_gens: dict = {}           # repo -> generation, bumped by `invalidate`
_stale: set = set()        # repos whose cached list predates the last invalidate
_waiting: set = set()      # (app_dir, number) reviews queued behind the slots
_asked: dict = {}          # (app_dir, number) -> head the last review job read
_fits: dict = {}           # (repo, number) -> (read_at, head, mergeable)
_merges: dict = {}         # merge job id -> the proposal it combines (merge_start)
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
    """GitHub's open proposals, cached LIST_TTL_S. A listing that was asked
    for BEFORE an `invalidate` (an accept or decline that just closed one)
    is answered to its caller but never cached: it may still hold the
    proposal that was just closed, and caching it would bring that row back
    for two minutes."""
    now = time.time()
    with _lock:
        hit = _lists.get(repo)
        if hit and not force and now - hit[0] < LIST_TTL_S:
            return hit[1]
        gen = _gens.get(repo, 0)
    try:
        prs = _mod("gh").list_open_prs(repo)
    except Exception as exc:  # noqa: BLE001 — offline keeps the last answer
        if not _gh_error(exc):
            logger.exception("inbox: listing proposals for %s failed", repo)
        return hit[1] if hit else None
    with _lock:
        if _gens.get(repo, 0) == gen:
            _lists[repo] = (now, prs)
            _stale.discard(repo)
    return prs


def invalidate(app_dir: str, number: int | None = None) -> None:
    """The cached list is out of date (`number` was just accepted or
    declined here). The list is KEPT — minus `number` — so the page keeps
    every other card (and whatever was typed into it) while a fresh listing
    runs; it is marked stale (`refreshing`) and the next `list` asks GitHub.
    The generation bump makes a listing already in flight, which may still
    hold `number`, answer its caller without being cached."""
    repo, _ = _repo_of(app_dir)
    if repo:
        with _lock:
            hit = _lists.get(repo)
            if hit is not None:
                _lists[repo] = (0.0, [p for p in hit[1] if number is None
                                      or p.get("number") != number])
            _gens[repo] = _gens.get(repo, 0) + 1
            _stale.add(repo)
            # An accept moved the author's branch: whether every OTHER
            # proposal still fits is GitHub's to say again (its `mergeable`
            # is per head AND base), so the next listing asks afresh.
            for key in [k for k in _fits if k[0] == repo]:
                _fits.pop(key, None)


def refreshing(app_dir: str) -> bool:
    """Whether the cached list predates the last `invalidate` (a fresh one
    is due) — the page shows its cards with a loading mark meanwhile."""
    repo, _ = _repo_of(_key(app_dir))
    with _lock:
        return bool(repo) and repo in _stale


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
    open_numbers = [p.get("number") for p in prs if isinstance(p.get("number"), int)]
    try:
        _mod("trycopy").sweep(app_dir, open_numbers)
    except Exception:  # noqa: BLE001
        logger.exception("inbox: try-copy sweep failed for %s", app_dir)
    _prune_reviews(repo, open_numbers)
    for pr in prs:
        if isinstance(pr.get("number"), int):
            if _fits_for(repo, pr["number"], pr.get("head_sha"), network=True) is None:
                _ask_fits_again(repo, pr["number"], pr.get("head_sha"))
    return _items(app_dir, repo, prs, start_reviews=review)


#: GitHub computes `mergeable` lazily: the first read after either side
#: moved answers null and starts the computation, and the real answer comes
#: a few seconds later. Asked again, off-thread, a few times — otherwise "No
#: longer fits" would wait for the next listing, up to LIST_TTL_S away.
FITS_RETRY_S = 5.0
FITS_RETRIES = 6
_fits_pending: set = set()


def _ask_fits_again(repo: str, number: int, head) -> None:
    key = (repo, int(number), head)
    with _lock:
        if key in _fits_pending:
            return
        _fits_pending.add(key)

    def run():
        try:
            for _ in range(FITS_RETRIES):
                time.sleep(FITS_RETRY_S)
                if _fits_for(repo, number, head, network=True) is not None:
                    return
        finally:
            with _lock:
                _fits_pending.discard(key)

    threading.Thread(target=run, name=f"contrib-bg-fits-{number}", daemon=True).start()


def _fits_for(repo: str, number: int, head, *, network: bool) -> bool | None:
    """GitHub's `mergeable` for proposal `number` at `head`, cached
    LIST_TTL_S (it changes when either side moves). Without `network`, the
    cached answer for this head only. None = not known (yet)."""
    key = (repo, int(number))
    with _lock:
        hit = _fits.get(key)
    if hit and hit[1] == head and (not network or time.time() - hit[0] < LIST_TTL_S):
        return hit[2]
    if not network:
        return None
    try:
        pr = _mod("gh").get_pr(repo, number)
    except Exception as exc:  # noqa: BLE001 — unknown, not "doesn't fit"
        if not _gh_error(exc):
            logger.exception("inbox: reading proposal %s of %s failed", number, repo)
        return hit[2] if hit and hit[1] == head else None
    value = pr.get("mergeable") if isinstance(pr.get("mergeable"), bool) else None
    if value is not None:   # GitHub computes it lazily: ask again next time
        with _lock:
            _fits[key] = (time.time(), pr.get("head_sha"), value)
    return value if pr.get("head_sha") == head else None


def cached(app_dir: str, *, start_reviews: bool = False) -> builtins.list | None:
    """The inbox from the last listing, with NO network — for GET
    /api/contrib/app. None when never listed. With `start_reviews` (the
    page is open), a review starts for every item that has none (they queue
    behind REVIEW_SLOTS); without it such an item reads "none", so the page
    never shows "reading" for a review nobody started."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return None
    with _lock:
        hit = _lists.get(repo)
    if hit is None:
        return None
    return _items(app_dir, repo, hit[1], start_reviews=start_reviews)


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
            else:
                status = "none"
        body = pr.get("body") or ""
        from_fused = FUSED_MARKER in body
        # The summary is Claude's own reading (the review) whenever there is
        # one; the sender's words are only the stand-in until then, and are
        # always shown as theirs (`description`), never as Claude's.
        description = _description_of(body) if from_fused else _plain_body(body)
        if review_obj is not None:
            summary, source = review_obj.get("summary") or "", "review"
        else:
            summary, source = description, "sender"
        try:
            try_state = _mod("trycopy").status(app_dir, number)
        except Exception:  # noqa: BLE001
            try_state = None
        out.append({
            "number": number, "url": pr.get("url") or "", "title": pr.get("title") or "",
            "author": pr.get("user") or "", "from_fused": from_fused,
            "fits": _fits_for(repo, number, head, network=False),
            "merge_job": _merge_for(app_dir, number),
            "summary": summary, "summary_source": source, "description": description,
            "review": review_obj, "review_status": status,
            "try": ({"status": try_state["status"], "path": try_state["path"],
                     "url": try_state["url"]} if try_state else None),
            "head_sha": head, "created_at": pr.get("created_at") or "",
        })
    return out


def _description_of(body: str) -> str:
    """A fused-sent body minus its footer and the note line: the sender's
    description (drafted by their Claude, edited by them)."""
    text = body.split("\n\n---\nSent from Fused")[0]
    text = re.sub(r"\n*\*\*Note from [^*]*:\*\*.*\Z", "", text, flags=re.S)
    return _mod("ai").plain(text)


def _plain_body(body: str) -> str:
    return _mod("ai").plain(body, 1500)


# ------------------------------------------------------------- the review


def _reviews_dir(repo: str) -> str:
    from fused_render.shell import storage

    owner, _, name = repo.partition("/")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{owner}__{name}")
    return os.path.join(storage.home_dir(), "contrib", "reviews", safe)


def _review_path(repo: str, number: int, head: str, base: str) -> str:
    """One file per (proposal, head, merge base): the review read exactly
    `base..head`, so a different base is a different review."""
    return os.path.join(_reviews_dir(repo), f"{int(number)}-{head}-{base}.json")


def _job_kind(number: int) -> str:
    return f"inbox-review-{int(number)}"


def _review_for(app_dir: str, repo: str, number: int, head: str,
                base: str | None = None) -> dict | None:
    """The finished review of exactly `head` — and, when `base` is given,
    of exactly `base..head` (accept and Try pass the merge base they see
    now; the listing, which runs no git, takes the newest review of the
    head). From disk, else from the last review job (an uncached,
    model-failed review) while that job is kept."""
    import glob

    from fused_render.shell import storage

    def matches(data):
        return (isinstance(data, dict) and data.get("head_sha") == head
                and (base is None or data.get("base_sha") == base))

    if base is not None:
        candidates = [_review_path(repo, number, head, base)]
    else:
        pattern = os.path.join(glob.escape(_reviews_dir(repo)), f"{int(number)}-{head}-*.json")
        candidates = sorted(glob.glob(pattern), key=_mtime, reverse=True)
    for path in candidates:
        data = storage.read_json(path)
        if matches(data):
            return data
    job = jobs.latest(_job_kind(number), app_dir)
    if job and job["status"] == "done" and matches(job["result"]):
        return job["result"]
    return None


def _review_files(repo: str) -> builtins.list:
    """`[(number, head, path)]` of every review file kept for `repo`."""
    try:
        names = os.listdir(_reviews_dir(repo))
    except OSError:
        return []
    out = []
    for name in names:
        m = re.fullmatch(r"(\d+)-([0-9a-f]{40,64})-([0-9a-f]{40,64})\.json", name)
        if m:
            out.append((int(m.group(1)), m.group(2),
                        os.path.join(_reviews_dir(repo), name)))
    return out


def _prune_reviews(repo: str, open_numbers=None, *, drop: int | None = None) -> None:
    """Delete the review files of proposals no longer open (or of `drop`),
    then keep the newest REVIEWS_KEEP. Never raises."""
    files = _review_files(repo)
    keep_open = None if open_numbers is None else {int(n) for n in open_numbers}
    left = []
    for number, _head, path in files:
        gone = (drop is not None and number == drop) or \
            (keep_open is not None and number not in keep_open)
        if gone:
            _unlink(path)
        else:
            left.append(path)
    for path in sorted(left, key=_mtime, reverse=True)[REVIEWS_KEEP:]:
        _unlink(path)


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _local_base(app_dir: str, number: int) -> str | None:
    """The merge base of the last-fetched proposal and author's branch —
    local refs only, no network."""
    gu = _gu()
    n = int(number)
    return gu._out(gu._run_safe(app_dir, "merge-base", f"refs/fused/pr/{n}-base",
                           f"refs/fused/pr/{n}")) or None


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
        base = _local_base(app_dir, number)
        if base is None:
            raise PlainError("This proposal isn't based on your app, so it can't be "
                             "reviewed here.")
        done = _review_for(app_dir, repo, number, head, base)
        if done is not None:
            return done
        diff, files = _proposal_diff(app_dir, number, base)
        result = _mod("ai").safety_review(
            diff=diff, files=files, title=pr.get("title") or "",
            body=pr.get("body") or "", head_sha=head)
        result["base_sha"] = base
    finally:
        _review_slots.release()
    if result.get("model"):
        storage.write_json(_review_path(repo, number, head, base), result)
        _prune_reviews(repo)
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
    head = gu._out(gu._run_safe(app_dir, "rev-parse", "--verify", "-q",
                           f"refs/fused/pr/{n}^{{commit}}")) if fetched else ""
    if not head:
        raise PlainError("Couldn't download this proposal from GitHub. Try again.")
    return head


# The diff a review reads must show EVERY byte the proposal brings, whatever
# the repo says about how to show it. Git would otherwise:
#  - print "Binary files differ" for any file it guesses is binary (a NUL in
#    an HTML comment, UTF-16, a .pyc) — code the review never sees;
#  - honour `.gitattributes` (`*.js binary`, `-diff`, a textconv driver) —
#    from the WORK TREE and the INDEX, i.e. whatever the author's copy holds;
#  - honour diff config (`diff.noprefix` would break our own parsing).
# So: `-a` (text, always), `--no-textconv`, `--no-ext-diff`, an EMPTY work
# tree and a fresh index (no `.gitattributes` to read), no global attributes
# file, and the prefix/quoting config pinned. `$GIT_DIR/info/attributes`
# (this machine's, not the proposal's) can still mark a file `-diff`; `-a`
# overrides that too. The git dir itself is the author's, so a partial
# clone still fetches a missing blob on demand.
_NEUTRAL_CONFIG = ("-c", "core.attributesFile=" + os.devnull,
                   "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
                   "-c", "diff.relative=false", "-c", "core.quotePath=false",
                   "-c", "diff.renames=true", "-c", "color.ui=false")


def _neutral_git(app_dir: str, *args, timeout: float = 120.0):
    """`(rc, stdout_bytes)` of `git <args>` in the author's repo with no
    attributes and pinned diff config (see above), or None."""
    gu = _gu()
    git_dir = gu._out(gu._run_safe(app_dir, "rev-parse", "--absolute-git-dir"))
    if not git_dir:
        return None
    with tempfile.TemporaryDirectory(prefix="fused-review-") as tmp:
        tree = os.path.join(tmp, "tree")
        os.mkdir(tree)
        env = {**os.environ, **gu._ENV, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        try:
            proc = subprocess.run(
                [gu._git_bin(), "--git-dir", git_dir, "--work-tree", tree,
                 *gu.SAFE_CONFIG, *_NEUTRAL_CONFIG, *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                env=env, timeout=timeout, close_fds=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
    if proc.returncode != 0:
        logger.warning("inbox: git %s failed: %s", args[0],
                       proc.stderr.decode("utf-8", "replace")[-500:])
    return proc.returncode, proc.stdout


def _raw_entries(raw: bytes) -> builtins.list:
    """`diff --raw -z --no-abbrev` → `[{"status", "path", "old_path", "mode",
    "blob"}]` (`blob` the new content's id)."""
    parts = raw.split(b"\0")
    out, i = [], 0
    while i < len(parts):
        meta = parts[i].decode("utf-8", "replace")
        if not meta.startswith(":"):
            i += 1
            continue
        fields = meta[1:].split()
        status = fields[4] if len(fields) >= 5 else ""
        new_mode = fields[1] if len(fields) >= 2 else ""
        new_blob = fields[3] if len(fields) >= 4 else ""
        if status[:1] in ("R", "C") and i + 2 < len(parts):
            old, path, i = parts[i + 1], parts[i + 2], i + 3
        else:
            old, path, i = None, parts[i + 1] if i + 1 < len(parts) else b"", i + 2
        if path:
            out.append({"status": status, "path": path.decode("utf-8", "replace"),
                        "old_path": old.decode("utf-8", "replace") if old else None,
                        "mode": new_mode, "blob": new_blob})
    return out


_TEXT_CHECK_MAX = 8 * 1024 * 1024


def _not_text(app_dir: str, entries) -> set:
    """Paths whose NEW content is not UTF-8 text (a NUL anywhere, or bytes
    that do not decode) — binary files and other encodings alike. Read from
    the blobs the raw entries name (`git cat-file --batch`), never by
    matching names in the patch: git writes `+++ b/my page.html<TAB>` for a
    name with a space and C-quotes others, and a name that fails to match is
    a file nobody checks. A blob past _TEXT_CHECK_MAX counts as not text
    (nobody reviews 8 MB by reading it); a missing one too."""
    gu = _gu()
    want = {}
    for e in entries:
        blob = e.get("blob") or ""
        if e["status"][:1] == "D" or e.get("mode") == "160000" \
                or not re.fullmatch(r"[0-9a-f]{40,64}", blob) or set(blob) == {"0"}:
            continue
        want.setdefault(blob, []).append(e["path"])
    if not want:
        return set()
    blobs = builtins.list(want)

    def batch(flag: str):
        try:
            return subprocess.run(
                [gu._git_bin(), "-C", app_dir, *gu.SAFE_CONFIG, "cat-file", flag],
                input=("\n".join(blobs) + "\n").encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=120.0, close_fds=False,
                env={**os.environ, **gu._ENV}).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None

    bad = set()
    sizes = batch("--batch-check")
    small = []
    for line, blob in zip((sizes or b"").decode("utf-8", "replace").splitlines(), blobs):
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit() \
                and int(parts[2]) <= _TEXT_CHECK_MAX:
            small.append(blob)
        else:
            bad.add(blob)
    if sizes is None:
        bad.update(blobs)
    blobs = small
    data = batch("--batch") if blobs else b""
    pos = 0
    for blob in blobs:
        nl = (data or b"").find(b"\n", pos)
        if nl < 0:
            bad.add(blob)
            continue
        parts = data[pos:nl].split()
        if len(parts) != 3 or not parts[2].isdigit():
            bad.add(blob)
            pos = nl + 1
            continue
        size = int(parts[2])
        content = data[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
        if b"\0" in content:
            bad.add(blob)
            continue
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            bad.add(blob)
    return {p for blob in bad for p in want.get(blob, [])}


def _proposal_diff(app_dir: str, number: int, base: str | None = None,
                   ) -> tuple[str, builtins.list]:
    """`(diff text, file entries)` of `base..refs/fused/pr/<N>` — `base` the
    merge base (computed when omitted). Every entry: `status`, `path`,
    `old_path` (a rename's), `mode` (new mode; "120000" = a link) and
    `binary` (its new lines are not UTF-8 text)."""
    n = int(number)
    head = f"refs/fused/pr/{n}"
    mb = base or _local_base(app_dir, n)
    if not mb:
        raise PlainError("This proposal isn't based on your app, so it can't be "
                         "reviewed here.")
    patch = _neutral_git(app_dir, "diff", "-a", "--no-textconv", "--no-ext-diff",
                         "--no-color", "-M", "--src-prefix=a/", "--dst-prefix=b/",
                         mb, head)
    raw = _neutral_git(app_dir, "diff", "--raw", "-z", "-M", "--no-abbrev", mb, head)
    if not patch or patch[0] != 0 or not raw or raw[0] != 0:
        raise PlainError("Couldn't read this proposal. Try again.")
    files = _raw_entries(raw[1])
    not_text = _not_text(app_dir, files)
    for e in files:
        e["binary"] = e["path"] in not_text
        e.pop("blob", None)
    # Cut far past ai.MAX_DIFF_CHARS, so a cut diff always reads as oversized.
    diff = patch[1][:_MAX_DIFF_BYTES].decode("utf-8", "replace").replace("\0", "\\0")
    return diff, files


# ------------------------------------------------------- try / accept / decline


MERGING_MESSAGE = ("Claude is still combining this proposal — keep or discard that "
                   "first.")


def _merging(app_dir: str, number: int) -> dict | None:
    """The refusal while this item's Merge with Claude is open (running,
    waiting for Keep/Discard, or kept but not yet sent): accepting,
    declining or trying it then would race the push Keep makes."""
    job = _merge_for(app_dir, number)
    if job and job["status"] in ("running", "ready", "landing"):
        return {"ok": False, "reason": "merging", "message": MERGING_MESSAGE}
    return None


def try_open(app_dir: str, number: int, *, confirm: bool = False) -> dict:
    """Make the Try copy. Refuses until the review of the current head is
    ready; on a "look" verdict, refuses with reason "confirm" unless
    `confirm`. `{ok, path, url}` or `{ok: False, reason, message}`."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return {"ok": False, "reason": "not-own", "message": "This app has no proposals."}
    busy = _merging(app_dir, number)
    if busy:
        return busy
    head = _current_head(app_dir, repo, number)
    base = _local_base(app_dir, number) if head else None
    review_obj = _review_for(app_dir, repo, number, head, base) if base else None
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


def accept(app_dir: str, number: int, *, confirm: bool = False) -> dict:
    """Merge proposal `number` on GitHub and bring the author's copy up to
    date. Needs a review of exactly what would be merged (the head GitHub
    reports now, against the merge base a fresh download gives); a "look"
    verdict refuses with reason "confirm" unless `confirm`, like Try. Long
    (a download and a sync run): the route runs it as a job."""
    app_dir = _key(app_dir)
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return {"ok": False, "reason": "not-own", "message": "This app has no proposals."}
    busy = _merging(app_dir, number)
    if busy:
        return busy
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
        return _after_accept(app_dir, number, ALREADY_ACCEPTED)
    if pr.get("state") != "open":
        _cleanup(app_dir, number)
        return {"ok": False, "reason": "closed", "message": "This proposal is no longer open."}
    head = pr.get("head_sha") or ""
    try:
        fetched = _fetch_pr(app_dir, repo, number,
                            pr.get("base_branch") or _repo_of(app_dir)[1].get("branch")
                            or "main")
    except PlainError as exc:
        return {"ok": False, "reason": "offline", "message": exc.message}
    base = _local_base(app_dir, number) if fetched == head else None
    review_obj = _review_for(app_dir, repo, number, head, base) if base else None
    if review_obj is None:
        review(app_dir, number)
        return {"ok": False, "reason": "review-needed",
                "message": _why_unreviewed(app_dir, repo, number, head, author,
                                           moved=fetched != head)}
    if review_obj.get("verdict") != "fine" and not confirm:
        return {"ok": False, "reason": "confirm",
                "message": "Claude thinks this proposal needs a look. Accept it anyway?"}
    try:
        result = gh.merge_pr(repo, number, sha=head, method=_merge_method(repo))
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        if _merged_now(repo, number):
            # Merged meanwhile — an earlier click, a second page, or on
            # github.com. That is what was asked for.
            return _after_accept(app_dir, number, ALREADY_ACCEPTED)
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
                         "this app gets it next time they open it.", author)


def _why_unreviewed(app_dir: str, repo: str, number: int, head: str,
                    author: str, *, moved: bool = False) -> str:
    """Why there is no review of exactly what would be merged, in words: the
    sender changed it, the author's app moved under it, or Claude simply has
    not read it (never, or the review was lost — a restart drops an uncached
    one). Only the first is the sender's doing."""
    heads = {h for n, h, _p in _review_files(repo) if n == int(number)}
    job = jobs.latest(_job_kind(number), app_dir)
    if job and job["status"] == "done" and isinstance(job["result"], dict):
        heads.add(job["result"].get("head_sha"))
    then = "Claude is reading it again — accept it once that's done."
    if moved:  # it changed between GitHub's answer and the download
        return f"{author} just changed this proposal. " + then
    if head in heads:
        return "Your app changed since Claude read this proposal. " + then
    if heads:
        return f"{author} changed this proposal since Claude read it. " + then
    return "Claude needs to read this proposal first. Accept it once that's done."


def _merged_now(repo: str, number: int) -> bool:
    try:
        return bool(_mod("gh").get_pr(repo, number).get("merged"))
    except Exception:  # noqa: BLE001 — unknown is "not merged": report the refusal
        return False


def _activity(app_dir: str, kind: str, text: str) -> None:
    try:
        _mod("store").add_activity(app_dir, kind, text)
    except Exception:  # noqa: BLE001 — the line is decoration of a move that happened
        logger.exception("inbox: activity line failed for %s", app_dir)


def _after_accept(app_dir: str, number: int, message: str, author: str | None = None) -> dict:
    if author:
        _activity(app_dir, "inbox-accepted", f"Accepted {author}'s proposal")
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
    busy = _merging(app_dir, number)
    if busy:
        return busy
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


# ------------------------------------------------- no longer fits (§16.6)


def _open_pr(app_dir: str, number: int) -> tuple[str | None, dict | None, dict | None]:
    """`(repo, pr, refusal)` for an open proposal on the author's app."""
    repo, _ = _repo_of(app_dir)
    if repo is None:
        return None, None, {"ok": False, "reason": "not-own",
                            "message": "This app has no proposals."}
    try:
        pr = _mod("gh").get_pr(repo, number)
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        return repo, None, {"ok": False, "reason": getattr(exc, "reason", "failed"),
                            "message": _plain_gh(exc)}
    if pr.get("state") != "open" or pr.get("merged"):
        return repo, None, {"ok": False, "reason": "closed",
                            "message": "This proposal is no longer open."}
    return repo, pr, None


ASK_DEFAULT = ("Could you update your proposal? It no longer fits with my latest "
               "version.")


def ask_update(app_dir: str, number: int, note: str = "") -> dict:
    """Ask the sender to update proposal `number`: one comment carrying
    `gh.ASK_UPDATE_MARKER` (their refresh offers Update)."""
    app_dir = _key(app_dir)
    repo, pr, refusal = _open_pr(app_dir, number)
    if refusal:
        return refusal
    gh = _mod("gh")
    author = pr.get("user") or "the sender"
    text = " ".join((note or "").split())[:2000] or ASK_DEFAULT
    try:
        gh.comment(repo, number, f"{text}\n\n{gh.ASK_UPDATE_MARKER}")
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                "message": _plain_gh(exc)}
    _activity(app_dir, "inbox-asked", f"Asked {author} to update their proposal")
    return {"ok": True, "message": f"Asked {author} to update it."}


def merge_start(app_dir: str, number: int) -> dict:
    """Merge with Claude for a proposal that no longer fits: her latest on
    GitHub merged into the proposal's head, in a scratch worktree of her
    repo. `{ok, job}` (a merge job, polled like any Merge with Claude) or
    `{ok, message}` when it already fits."""
    app_dir = _key(app_dir)
    repo, pr, refusal = _open_pr(app_dir, number)
    if refusal:
        return refusal
    author = pr.get("user") or "the sender"
    # A proposal from a branch of the author's own repo (a collaborator's):
    # GitHub reports `maintainer_can_modify` false there, since the flag is
    # about forks, but the author can push to her own branches.
    same_repo = str(pr.get("head_repo") or "").lower() == repo.lower()
    if not pr.get("head_repo") or not (pr.get("maintainer_can_modify") or same_repo):
        return {"ok": False, "reason": "forbidden",
                "message": f"{author} didn't allow changes to this proposal. "
                           f"Ask {author} to update it instead."}
    head = pr.get("head_sha") or ""
    try:
        fetched = _fetch_pr(app_dir, repo, number, pr.get("base_branch")
                            or _repo_of(app_dir)[1].get("branch") or "main")
    except PlainError as exc:
        return {"ok": False, "reason": "offline", "message": exc.message}
    if fetched != head:
        return {"ok": False, "reason": "moved",
                "message": f"{author} just changed this proposal. Try again."}
    gu = _gu()
    n = int(number)
    base = gu._out(gu._run_safe(app_dir, "rev-parse", "--verify", "-q",
                           f"refs/fused/pr/{n}-base^{{commit}}"))
    if not base:
        return {"ok": False, "reason": "offline",
                "message": "Couldn't download your latest version from GitHub. Try again."}
    if _mod("replay").is_ancestor(app_dir, base, head):
        return {"ok": True, "message": "This proposal already has your latest version."}
    mj = _mod("merge_job")
    # One merge job per app folder (merge_job answers the open one): never
    # let this proposal's Keep land on another's combination.
    open_job = mj.for_app(app_dir)
    if open_job:
        with _lock:
            mine = _merges.get(open_job.get("id"))
        if mine and mine["number"] == n and mine["head"] == head:
            return {"ok": True, "job": open_job["id"]}
        return {"ok": False, "reason": "busy",
                "message": "Claude is still combining another version of this app. "
                           "Finish that first."}
    job = mj.start(app_dir, purpose="proposal", ours=head, theirs=base, land=False)
    with _lock:
        _merges[job] = {"app": app_dir, "repo": repo, "number": n, "author": author,
                        "head": head, "head_repo": pr["head_repo"],
                        "head_branch": pr["head_branch"]}
    return {"ok": True, "job": job}


def owns_merge(job_id: str) -> bool:
    """Whether merge job `job_id` is an inbox Merge with Claude — its Keep
    must go through `merge_accept` (which sends the result), never straight
    to `merge_job.accept`."""
    with _lock:
        return job_id in _merges


def _merge_for(app_dir: str, number: int) -> dict | None:
    """The item's Merge with Claude (`merge_start`), for the page to show
    and Keep/Discard on the item: `{id, status, summary, files, error}`.
    "accepted" with a result not yet sent reads as "ready" (Keep retries)."""
    with _lock:
        mine = [(jid, info) for jid, info in _merges.items()
                if info.get("app") == app_dir and info.get("number") == int(number)]
    for jid, info in reversed(mine):
        job = _mod("merge_job").get(jid)
        status = (job or {}).get("status")
        if info.get("tip"):
            status = "ready"
        elif status not in ("running", "ready", "landing", "failed"):
            continue
        return {"id": jid, "status": status, "summary": (job or {}).get("summary") or "",
                "files": builtins.list((job or {}).get("files") or []),
                "error": (job or {}).get("error")}
    return None


def merge_accept(app_dir: str, job_id: str) -> dict:
    """Keep the combination `merge_start` made (the page's Keep): the merge
    job hands back the merged commit without touching her folder
    (`land=False`), and `merge_accepted` sends it."""
    with _lock:
        info = _merges.get(job_id)
    if not info:
        return {"ok": False, "reason": "stale",
                "message": "This merge is no longer waiting for you."}
    tip = info.get("tip")   # kept, sending it failed last time: send again
    if not tip:
        out = _mod("merge_job").accept(job_id) or {}
        if not out.get("ok"):
            return out
        tip = out.get("tip")
        with _lock:
            info["tip"] = tip
    return merge_accepted(app_dir, {"id": job_id, "tip": tip})


def merge_accepted(app_dir: str, job: dict) -> dict:
    """The author kept Claude's combination (`merge_job.accept` of a job
    `merge_start` made): send it onto the proposal's own branch — a plain
    push, never force — and read the new version again."""
    app_dir = _key(app_dir)
    with _lock:
        info = _merges.get((job or {}).get("id"))
    tip = (job or {}).get("new_tip") or (job or {}).get("tip")
    if not info or not tip:
        return {"ok": False, "reason": "stale",
                "message": "This merge is no longer waiting for you."}
    gh = _mod("gh")
    author = info["author"]
    try:
        gh.push(app_dir, tip, info["head_repo"], info["head_branch"])
    except Exception as exc:  # noqa: BLE001
        if not _gh_error(exc):
            raise
        if getattr(exc, "reason", None) == "conflict":
            with _lock:   # combined with a version that is gone: start over
                _merges.pop(job.get("id"), None)
            return {"ok": False, "reason": "moved",
                    "message": f"{author} changed this proposal meanwhile. "
                               "Merge with Claude again."}
        return {"ok": False, "reason": getattr(exc, "reason", "failed"),
                "message": _plain_gh(exc)}
    with _lock:
        _merges.pop(job.get("id"), None)
        _fits.pop((info["repo"], info["number"]), None)
    _activity(app_dir, "inbox-merged",
              f"Combined {author}'s proposal with your latest version")
    invalidate(app_dir)
    try:
        review(app_dir, info["number"])
    except Exception:  # noqa: BLE001 — the next listing starts it anyway
        logger.exception("inbox: re-review after merge failed for %s", app_dir)
    return {"ok": True, "message": f"Done. {author}'s proposal now fits your latest "
                                   "version. Claude is reading it again."}


def _cleanup(app_dir: str, number: int) -> None:
    """A proposal was merged or closed: its Try copy and review files go,
    and the next listing asks GitHub afresh."""
    try:
        _mod("trycopy").remove(app_dir, number)
    except Exception:  # noqa: BLE001
        logger.exception("inbox: removing try copy failed")
    repo, _ = _repo_of(app_dir)
    if repo:
        _prune_reviews(repo, drop=int(number))
    invalidate(app_dir, int(number))
