"""What a copy follows (docs/CONTRIB_LOOP.md §5).

The engine is written against "the tip this copy follows", never `main`
specifically: stage A follows the author's default branch; stage B adds a
marketplace copy that follows an approved version (`kind="pinned"`), and
nothing above this module should have to change for it.

A GITHUB-LINK COPY FOLLOWS THE URL IN ITS RECORD, NOT ITS `origin`. The
repo's `.git/config` is writable by anything running in the app folder (the
app's own Python, a Claude turn, the author's README instructions), and a
copy re-pointed at some other repo must not start pulling that repo's code
under Bob's changes. So the follow tip is fetched BY URL —
`git fetch <url> +refs/heads/<branch>:refs/fused/follow` — into a private
ref no remote config names. A private repo the https URL cannot read
(appfile_git's clone falls back to ssh for those) is retried over the ssh
spelling of the SAME owner/name, never anything `.git/config` says.

AN OWN COPY FOLLOWS ITS `origin`, as auto-sync always has: the author's
remote is theirs to change, and `refs/remotes/origin/<branch>` is the tip.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from fused_render import git_upstream as gu
from fused_render.contrib import replay

FOLLOW_REF = replay.FOLLOW_REF

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: stderr fragments meaning "https could not authenticate" — the cue for the
#: ssh retry (appfile_git._AUTH_FAILURE_MARKERS' twin).
_AUTH_MARKERS = ("could not read username", "authentication failed",
                 "repository not found", "terminal prompts disabled")


@dataclass(frozen=True)
class Follow:
    kind: str          # "branch" (stage A); stage B adds "pinned"
    url: str           # https://github.com/O/R — from the RECORD
    branch: str        # e.g. "main"
    ref: str = FOLLOW_REF
    #: An own copy: fetch `origin` as today, tip = its tracking ref.
    via_origin: bool = False


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _valid(repo, branch) -> bool:
    return (isinstance(repo, str) and bool(_REPO_RE.match(repo))
            and isinstance(branch, str) and bool(_BRANCH_RE.match(branch))
            and ".." not in branch and not branch.startswith(("-", "/")))


def for_record(record) -> Follow | None:
    """github → the author's branch, by URL; own with a repo → its own
    branch via `origin`; anything else (no repo, scratch) → None."""
    if not isinstance(record, dict):
        return None
    repo, branch = record.get("repo"), record.get("branch")
    if not _valid(repo, branch):
        return None
    if record.get("kind") == "github":
        return Follow("branch", github_url(repo), branch)
    if record.get("kind") == "own":
        return Follow("branch", github_url(repo), branch,
                      ref=f"refs/remotes/origin/{branch}", via_origin=True)
    return None


def _ssh_url(url: str) -> str:
    return "git@github.com:" + url.split("github.com/", 1)[1] + ".git"


def fetch_detail(root, follow: Follow) -> tuple[str | None, str | None]:
    """`(tip, reason)`: the fetched tip, or None with git_upstream's failure
    class (`offline` | `auth` | `git-failed`). The caller holds the slot (a
    fetch writes refs)."""
    if follow.via_origin:
        r = gu._run(root, "fetch", "--", "origin", follow.branch)
    else:
        refspec = f"+refs/heads/{follow.branch}:{follow.ref}"
        args = ("fetch", "--no-tags", "--no-write-fetch-head", "--")
        r = gu._run(root, *args, follow.url, refspec)
        if not gu._ok(r) and r is not None and any(
                m in gu._text(r).lower() for m in _AUTH_MARKERS):
            r = gu._run(root, "-c", "core.sshCommand=ssh -oBatchMode=yes",
                        *args, _ssh_url(follow.url), refspec)
    if not gu._ok(r):
        return None, gu._classify(r)
    return tip(root, follow), None


def fetch(root, follow: Follow) -> str | None:
    """Fetch the follow tip (§5); None offline/unreachable/refused."""
    return fetch_detail(root, follow)[0]


def tip(root, follow: Follow) -> str | None:
    """The last fetched follow tip, no network."""
    return replay.rev(root, follow.ref)


def tip_or_origin(root, follow: Follow) -> str | None:
    """`tip`, else (a copy never fetched by the engine yet — a fresh install)
    the clone's own `origin/<branch>`, which the install's clone wrote from
    the same URL. Read-only; for status and the first sync."""
    return tip(root, follow) or replay.rev(root, f"refs/remotes/origin/{follow.branch}")


def is_repo_root(app_dir) -> str | None:
    """`app_dir`'s realpath when it heads its own repository, else None."""
    root = gu.repo_root(app_dir)
    return root if root and root == os.path.realpath(app_dir) else None
