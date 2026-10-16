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
ref no remote config names. The fetch goes through `gh.fetch` (gh as the
credential helper, so private repos read; the FakeGitHub's bare repo in
tests); without gh.py, a private repo the https URL cannot read is retried
over the ssh spelling of the SAME owner/name, never anything `.git/config`
says.

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


def _gh():
    try:
        from fused_render.contrib import gh
    except ImportError:
        return None
    return gh if callable(getattr(gh, "fetch", None)) else None


def _ssh_url(url: str) -> str:
    return "git@github.com:" + url.split("github.com/", 1)[1] + ".git"


_GITHUB_PREFIXES = ("https://github.com", "http://github.com", "git@github.com",
                    "ssh://git@github.com", "github.com", "https://www.github.com")


def github_redirected(root) -> bool:
    """Whether this repo's OWN config (`.git/config`, which anything running
    in the app folder can write) re-points GitHub: a `url.<x>.insteadOf` or
    `pushInsteadOf` whose value is a github.com prefix sends every fetch and
    push "to GitHub" somewhere else, the record's URL notwithstanding. The
    one test for both callers — this module's fetch and gh.py's fetch/push
    (review r05): gh.py's `url_rewrites` (reads `--includes` too) when it is
    there, else the same rule here. Unreadable config reads as not
    redirected."""
    gh = _gh()
    rewrites = getattr(gh, "url_rewrites", None) if gh else None
    if rewrites is not None:
        try:
            return bool(rewrites(root))
        except Exception:  # noqa: BLE001 — fall back to the local rule
            pass
    # `--local --includes` AND `--worktree` (`.git/config.worktree`, which git
    # applies once `extensions.worktreeConfig` is on — review r2_02).
    for scope in (("--local", "--includes"), ("--worktree",)):
        r = gu._run(root, "config", *scope, "--get-regexp", r"^url\..*\.(push)?insteadof$")
        if not gu._ok(r):
            continue
        for line in gu._out(r).splitlines():
            _key, _, value = line.partition(" ")
            if value.strip().lower().startswith(_GITHUB_PREFIXES):
                return True
    return False


REDIRECTED_MESSAGE = ("This app's settings point GitHub somewhere else, "
                      "so fused won't update it.")


def fetch_detail(root, follow: Follow) -> tuple[str | None, str | None]:
    """`(tip, reason)`: the fetched tip, or None with git_upstream's failure
    class (`offline` | `auth` | `git-failed`) or `redirected`
    (`github_redirected`). Writes only the follow ref (github kind) or
    origin's tracking ref (own); the engine serializes its own fetches per
    copy."""
    gh = _gh()
    rewrites = getattr(gh, "url_rewrites", None) if gh else None
    if github_redirected(root) or (rewrites is not None and rewrites(root, follow.url)):
        import logging

        logging.getLogger(__name__).warning(
            "contrib: %s re-points github.com in its own git config; not fetching", root)
        return None, "redirected"
    if follow.via_origin:
        r = gu._run(root, "fetch", "--", "origin", follow.branch)
    elif _gh() is not None:
        # gh.py's fetch: gh as the credential helper (private repos fetch
        # without `gh auth setup-git`), the user's global/system git config
        # switched off, and the FakeGitHub's bare repo in fake mode.
        refspec = f"+refs/heads/{follow.branch}:{follow.ref}"
        if not _gh().fetch(root, follow.url, refspec):
            return None, "offline"
        return tip(root, follow), None
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
