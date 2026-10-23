"""What a GitHub-link copy follows, the one fetch of it, and its mirror
(docs/CONTRIB_LOOP.md §5, §16.2, §17.3).

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

Only `github` copies: an `own` copy's `origin` is its author's to change,
and git_upstream fetches it as it always has (§16.7).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from fused_render import git_upstream as gu
from fused_render.contrib import replay

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

REDIRECTED_MESSAGE = ("This app's settings point GitHub somewhere else, "
                      "so fused won't update it.")


@dataclass(frozen=True)
class Follow:
    kind: str          # "branch" (stage A); stage B adds "pinned"
    url: str           # https://github.com/O/R — from the RECORD
    branch: str        # e.g. "main"

    @property
    def ref(self) -> str:
        """Where the tip lives: git's own tracking ref (P3)."""
        return f"refs/remotes/origin/{self.branch}"


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _valid(repo, branch) -> bool:
    return (isinstance(repo, str) and bool(_REPO_RE.match(repo))
            and isinstance(branch, str) and bool(_BRANCH_RE.match(branch))
            and ".." not in branch and not branch.startswith(("-", "/")))


def for_record(record) -> Follow | None:
    """A `github` record → the author's branch, by URL; anything else
    (own, scratch, no repo, a malformed name) → None."""
    if not isinstance(record, dict) or record.get("kind") != "github":
        return None
    repo, branch = record.get("repo"), record.get("branch")
    if not _valid(repo, branch):
        return None
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
    `not-found` (gone, or private now), `forbidden`, or `failed`. The ref is
    shared with git's own fetches, so the CALLER holds the slot."""
    if github_redirected(root, fol.url):
        return None, "redirected"
    gh = _gh_module()
    refspec = f"+refs/heads/{fol.branch}:{fol.ref}"
    detail = getattr(gh, "fetch_detail", None) if gh else None
    if callable(detail):
        ok, reason = detail(root, fol.url, refspec)
        if not ok:
            return None, _REASONS.get(reason, "failed")
    else:
        fetcher = getattr(gh, "fetch", None) if gh else None
        if fetcher is None or not fetcher(root, fol.url, refspec):
            return None, "offline"
    t = tip(root, fol)
    if t and gu._current_branch(root) == replay.HOME_BRANCH:
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


def update_mirror(root, fol: Follow) -> dict:
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
    the ref's tip before."""
    res = {"moved": False, "backup": None, "ff": True, "old": None}
    new = tip(root, fol)
    ref = f"refs/heads/{fol.branch}"
    old = replay.rev(root, ref)
    res["old"] = old
    if not new or old == new or gu._current_branch(root) == fol.branch:
        return res
    if old and not replay.is_ancestor(root, old, new):
        res["ff"] = False
        pin = replay._new_backup_ref(root, "rewound")
        if not replay.ok(replay.git(root, "update-ref", pin, old)):
            return res
        res["backup"] = pin
    r = replay.git(root, "update-ref", "-m", "fused: mirror", ref, new, old or "")
    res["moved"] = replay.ok(r)
    if not res["moved"]:
        logger.info("contrib: mirror of %s in %s not moved: %s", ref, root, gu._text(r))
    elif res["backup"]:
        _rewound_line(root, res["backup"])
    return res


def _rewound_line(root, pin) -> None:
    """The author rewound their branch: one Activity line naming the pin."""
    try:
        from fused_render.contrib import store

        rec = store.get(root) or {}
        repo = rec.get("repo") if isinstance(rec.get("repo"), str) else ""
        who = repo.split("/", 1)[0] if "/" in repo else "the author"
        store.add_activity(root, "rewound", f"{who} went back to an earlier version of their "
                                            "app. Your copy and your changes are untouched.",
                           ref=pin)
    except Exception:  # noqa: BLE001 — the pin itself keeps the old version
        logger.exception("contrib: rewound line failed for %s", root)


def is_repo_root(app_dir) -> str | None:
    """`app_dir`'s realpath when it heads its own repository, else None."""
    root = gu.repo_root(app_dir)
    return root if root and root == os.path.realpath(app_dir) else None
