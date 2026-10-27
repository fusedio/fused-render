"""What a GitHub-link copy follows, the one fetch of it, and its mirror
(docs/CONTRIB_LOOP.md §16.2, §17.3).

ONE UPSTREAM (§16.0 P3). The author's latest lives in git's own
`refs/remotes/origin/<branch>` — the ref every git tool, the Explorer's ↑N
marks and the git panel already read — not in a private ref. v1's
`refs/fused/follow` is deleted on the first v2 sync (`engine`).

FETCHED BY THE RECORD'S URL, NOT BY `origin`. The repo's `.git/config` is
writable by anything running in the app folder (the app's own Python, a
Claude turn, a README's instructions), and a copy re-pointed at some other
repo must not start pulling that repo's code under Bob's changes. So the
tip is fetched by URL —
`git fetch --no-tags <record url> +refs/heads/<b>:refs/remotes/origin/<b>`
— through `gh.fetch` (gh as the credential helper so private repos read,
TLS/proxy pinned, the FakeGitHub's bare repo in tests), and only after
`github_redirected` found no repo-local rule re-pointing or re-credentialing
a GitHub transfer (fail closed).

Only `github` and `marketplace` copies: an `own` copy's `origin` is its
author's to change, and git_upstream fetches it as it always has (§16.7).

PINNED (§18.4). A marketplace copy follows the same branch, by the same URL,
but its mirror holds the APPROVED commit of its index row — the author's tip
is fetched into `origin/<b>` for its objects and for one question only: is
the approved commit on the author's branch (a commit only a fork has is
never run, even though GitHub serves it by sha).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from fused_render import git_upstream as gu
from fused_render.contrib import replay

logger = logging.getLogger(__name__)

#: `owner/name`; a name of `.` or `..` is refused (`marketplace.is_repo`
#: does the same): it is spliced into `repos/{full}/…` API paths.
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?$)[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

REDIRECTED_MESSAGE = ("This app's settings point GitHub somewhere else, "
                      "so fused won't update it.")


@dataclass(frozen=True)
class Follow:
    kind: str          # "branch" (a GitHub-link copy) | "pinned" (a marketplace copy, §18.4)
    url: str           # https://github.com/O/R — from the RECORD
    branch: str        # e.g. "main"
    # "pinned" only: the approved commit of the copy's index row
    # (`record.approved.commit`) — what the mirror holds (M1).
    commit: str | None = None

    @property
    def ref(self) -> str:
        """Where the tip lives: git's own tracking ref (P3). For a pinned
        follow it is the author's tip, fetched for objects and the
        ancestor check only — never what the copy runs."""
        return f"refs/remotes/origin/{self.branch}"

    @property
    def pinned(self) -> bool:
        return self.kind == "pinned"


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: `update_mirror`'s refusal for a pinned follow whose approved commit is
#: not on the author's branch (any more): fused never moves the mirror
#: there (a commit only a fork has, or one the author rewound away).
NOT_ON_BRANCH = "not-on-branch"

#: `update_mirror`'s refusal for a pinned follow whose approved commit is,
#: or is built on, a hard-blocked commit (§18.4 B4): the mirror never holds
#: it.
BLOCKED = "blocked"


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _valid(repo, branch) -> bool:
    """A repo and an author's branch fused can use in argv and refs. A
    branch named `fused` or `fused/…` is refused: `fused/mine` is the copy's
    own branch, and git cannot hold both `fused` and `fused/mine` (the same
    rule as `classify._valid_branch`; `appfile_git.clone` refuses to install
    such a repo)."""
    return (isinstance(repo, str) and bool(_REPO_RE.match(repo))
            and isinstance(branch, str) and bool(_BRANCH_RE.match(branch))
            and ".." not in branch and not branch.startswith(("-", "/"))
            and branch != "fused" and not branch.startswith("fused/"))


def for_record(record) -> Follow | None:
    """A `github` record → the author's branch, by URL; a `marketplace`
    record → the same branch PINNED at its approved commit (§18.4), None
    while it has no readable approved commit (M3: nothing to follow, no
    move); anything else (own, scratch, no repo, a malformed name) → None."""
    if not isinstance(record, dict) or record.get("kind") not in ("github", "marketplace"):
        return None
    repo, branch = record.get("repo"), record.get("branch")
    if not _valid(repo, branch):
        return None
    if record.get("kind") == "marketplace":
        approved = record.get("approved") if isinstance(record.get("approved"), dict) else {}
        commit = approved.get("commit")
        if not isinstance(commit, str) or not _SHA_RE.match(commit):
            return None
        return Follow("pinned", github_url(repo), branch, commit=commit)
    return Follow("branch", github_url(repo), branch)


def _gh_module():
    try:
        from fused_render.contrib import gh
    except Exception:  # noqa: BLE001
        return None
    return gh


def github_redirected(root, url=None) -> bool:
    """Whether this repo's OWN config re-points or re-credentials a GitHub
    transfer — `.git/config`, what it includes, `.git/config.worktree` —
    gh.py's `url_rewrites`, the one test every caller uses. FAILS CLOSED:
    gh.py missing, or the check itself failing, reads as redirected."""
    gh = _gh_module()
    rewrites = getattr(gh, "url_rewrites", None) if gh else None
    if rewrites is None:
        logger.warning("contrib: no GitHub config guard available for %s; refusing", root)
        return True
    try:
        hits = rewrites(root, url)
    except Exception:  # noqa: BLE001
        logger.exception("contrib: GitHub config guard failed for %s", root)
        return True
    if hits:
        logger.warning("contrib: %s's own git config redirects GitHub: %s", root, hits)
    return bool(hits)


#: gh.py's failure reasons, in the engine's `sync_error` codes.
_REASONS = {"signed-out": "auth", "auth": "auth", "not-found": "not-found",
            "forbidden": "forbidden", "offline": "offline", "redirected": "redirected"}


def fetch(root, fol: Follow) -> tuple[str | None, str | None]:
    """`(tip, reason)`: the author's tip, fetched by the record's URL into
    `refs/remotes/origin/<branch>`, or `(None, reason)` — `redirected`
    (`github_redirected`), `offline` (silent, retried), `auth`,
    `not-found` (gone, or private now), `forbidden`, or `failed` (`busy`:
    the slot stayed taken). The ref is shared with git's own fetches, so
    the fetch holds the slot — the caller's when it has it already (the
    engine), else its own (Send, Update proposal, remix publish).

    A PINNED follow's mirror is never moved here: the engine moves it once,
    after `blocks.vet_approved` (§18.4 M1 — not even briefly onto a release
    that contains a blocked commit)."""
    with replay.slot() as held:
        if not held:
            return None, "busy"
        return _fetch_locked(root, fol)


def _fetch_locked(root, fol: Follow) -> tuple[str | None, str | None]:
    if github_redirected(root, fol.url):
        return None, "redirected"
    gh = _gh_module()
    refspec = f"+refs/heads/{fol.branch}:{fol.ref}"
    detail = getattr(gh, "fetch_detail", None) if gh else None
    if fol.pinned:
        # §18.4: the author's branch (objects + the ancestor check) AND the
        # approved commit by sha — GitHub serves any reachable sha; when the
        # combined form is refused, the branch alone, then the bare sha.
        if not callable(detail):
            return None, "offline"
        ok, reason = detail(root, fol.url, refspec, fol.commit)
        if not ok:
            ok, reason = detail(root, fol.url, refspec)
            if ok and not replay.rev(root, fol.commit):
                detail(root, fol.url, fol.commit)
        if not ok:
            return None, _REASONS.get(reason, "failed")
    elif callable(detail):
        ok, reason = detail(root, fol.url, refspec)
        if not ok:
            return None, _REASONS.get(reason, "failed")
    else:
        fetcher = getattr(gh, "fetch", None) if gh else None
        if fetcher is None or not fetcher(root, fol.url, refspec):
            return None, "offline"
    t = tip(root, fol)
    if t and not fol.pinned and gu._current_branch(root) == replay.HOME_BRANCH:
        # Every fetch keeps I3 (review P1 #4): the engine's, and the ones
        # Send/Update/remix make (`proposals.tip_of(fetch=True)`).
        update_mirror(root, fol)
    return (t, None) if t else (None, "failed")


#: v1 name; same answer.
fetch_detail = fetch


def tip(root, fol: Follow) -> str | None:
    """The last fetched tip, no network."""
    return replay.rev(root, fol.ref)


#: v1 name; one upstream now, so the same answer as `tip`.
tip_or_origin = tip


def update_mirror(root, fol: Follow, *, why: str = "rewound") -> dict:
    """The second half of §17.3's fetch row (I3): `refs/heads/<b>` follows
    `refs/remotes/origin/<b>` as a REF update — never a worktree move —
    with git's compare-and-set (`update-ref <ref> <new> <old>`). A non-fast-
    forward (the author rewound, or commits made on the mirror while it was
    checked out) pins the old tip under `refs/fused/backup/<ms>-rewound`
    first and moves the ref anyway. While HEAD IS `<b>` (the `other-branch`
    state) the local ref is left alone: a checked-out branch is never moved
    under its worktree. A non-fast-forward here IS the author rewinding (a
    mirror fused moved off its own commits elsewhere — Go back, a new home
    branch — is reset there, not here), so it writes the one Activity line
    "<who> went back to an earlier version…", naming the pin as `ref` (kept,
    never offered as Undo: restoring it would put the author's old version
    over Bob's own). The caller holds the slot. Answers `{"moved",
    "backup", "ff", "old"}` — `ff` False for a pinned non-fast-forward, `old`
    the ref's tip before.

    PINNED (§18.4, a marketplace copy): `new` is the approved commit
    (`fol.commit`), never origin's tip, and only when it is in the
    author's branch's history (`merge-base --is-ancestor <commit>
    origin/<b>`) — else nothing moves and `reason` is `NOT_ON_BRANCH`
    (`missing` when the commit was never fetched), and never onto a commit
    that is or contains a HARD-blocked commit of the copy's listing
    (`blocks.contains_blocked`) — `reason` `BLOCKED` then, also when the
    block list cannot be read (M1, fail closed). `why` names the pin of a
    non-fast-forward move: `rewound` (the row went back: the one Activity
    line, no Undo), or a caller's own (`rollback`, `switch` — the caller
    writes its line)."""
    res = {"moved": False, "backup": None, "ff": True, "old": None, "reason": None}
    ref = f"refs/heads/{fol.branch}"
    old = replay.rev(root, ref)
    res["old"] = old
    if fol.pinned:
        new = replay.rev(root, fol.commit)
        if not new:
            res["reason"] = "missing"
            return res
        author = tip(root, fol)
        if not author or not replay.is_ancestor(root, new, author):
            res["reason"] = NOT_ON_BRANCH
            return res
        if _hard_blocked(root, new):
            res["reason"] = BLOCKED
            return res
    else:
        new = tip(root, fol)
    if not new or old == new or gu._current_branch(root) == fol.branch:
        return res
    if old and not replay.is_ancestor(root, old, new):
        res["ff"] = False
        pin = replay._new_backup_ref(root, why)
        if not replay.ok(replay.git(root, "update-ref", pin, old)):
            return res
        res["backup"] = pin
    r = replay.git(root, "update-ref", "-m", "fused: mirror", ref, new, old or "")
    res["moved"] = replay.ok(r)
    if not res["moved"]:
        logger.info("contrib: mirror of %s in %s not moved: %s", ref, root, gu._text(r))
    elif res["backup"] and why == "rewound":
        _rewound_line(root, res["backup"], pinned=fol.pinned)
    return res


def _hard_blocked(root, sha) -> bool:
    """Whether `sha` is, or contains, a hard-blocked commit of the copy's
    listing — every pinned mirror move asks it (Discard, Use <who>'s
    version, Go back, a sync, a switch all land on the mirror). Unknown
    reads as blocked."""
    try:
        from fused_render.contrib import blocks, store

        rec = store.get(root) or {}
        return blocks.contains_blocked(root, blocks._slug(rec), sha, None,
                                       levels=("hard",)) is not None
    except Exception:  # noqa: BLE001
        logger.exception("contrib: block check failed for %s in %s", sha, root)
        return True


def _rewound_line(root, pin, *, pinned=False) -> None:
    """The author rewound their branch (or, pinned, the index row went back
    to an earlier release): one Activity line naming the pin."""
    try:
        from fused_render.contrib import store

        rec = store.get(root) or {}
        repo = rec.get("repo") if isinstance(rec.get("repo"), str) else ""
        who = repo.split("/", 1)[0] if "/" in repo else "the author"
        text = (f"{who}'s reviewed version went back to an earlier release. Your copy and "
                "your changes are untouched." if pinned else
                f"{who} went back to an earlier version of their app. Your copy and your "
                "changes are untouched.")
        store.add_activity(root, "rewound", text, ref=pin)
    except Exception:  # noqa: BLE001 — the pin itself keeps the old version
        logger.exception("contrib: rewound line failed for %s", root)


def is_repo_root(app_dir) -> str | None:
    """`app_dir`'s realpath when it heads its own repository, else None."""
    root = gu.repo_root(app_dir)
    return root if root and root == os.path.realpath(app_dir) else None
