"""GitHub, for the contribution loop: THE ONLY module that runs `gh` or talks
to github.com (contract §7). Everything else in `contrib/` asks this module,
so there is one place that knows GitHub's error shapes, one credential story
and one switch to a fake.

**Two backends, one surface.** The module-level functions below delegate to
`_backend()`, re-read on EVERY call (never at import) so a test that sets the
env var after importing still gets the fake:

  * the real one runs `gh api` — the binary `github_setup.resolve()` finds,
    signed in through `github_login` — so this app never holds a token;
  * `FakeGitHub`, selected by `FUSED_RENDER_CONTRIB_FAKE_GH=<dir>`: users,
    forks, pull requests and comments in `<dir>/state.json`, each repo a REAL
    bare repo under `<dir>/repos/`, so a push is a real `git push` to a file
    path and a merge writes real commits. Tests, and the TL's local smoke
    (`python -m fused_render.contrib.gh <dir> help`), drive the other side of
    the conversation through its mutators: Alice merging (merge/squash/rebase),
    closing, commenting, pushing onto Bob's proposal branch.

**Errors are plain.** Every failure raises `GitHubError(reason, message)`:
`reason` is one of `REASONS` for the code to branch on, `message` is a
sentence the page may show as-is (contract §0.1: no git words, never gh's or
git's own stderr — that goes to the server log only). It subclasses
`jobs.PlainError`, so a job function can let one escape and the job fails
with that sentence rather than "Something went wrong".

**Pushing without `gh auth setup-git`.** `push`/`fetch` pass git an explicit
credential helper (`gh auth git-credential`) for https://github.com and reset
every other helper first, so they work whether or not setup-git ever ran and
whether `gh`'s own git protocol is ssh (it is, on the owner's machine). They
also run with the user's GLOBAL and SYSTEM git config switched off
(`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`): a developer's
`url."git@github.com:".insteadOf https://github.com/` would otherwise rewrite
our https URL to ssh and skip the helper, and Apple's system `osxkeychain`
helper can answer with a stale token before ours is asked. The price is that a
proxy configured ONLY in ~/.gitconfig is not seen (env proxies still are);
accepted, and noted in the risks. Hooks are off too (`--no-verify` plus an
empty `core.hooksPath`): the app folder is Claude-writable, and a pre-push hook
there would run with Bob's GitHub token in reach.

**Never prompts.** `GIT_TERMINAL_PROMPT=0`, empty askpass, `GH_PROMPT_DISABLED`
— a signed-out machine fails fast with reason `signed-out`, it does not hang a
job thread on a prompt nobody can see.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

FAKE_ENV = "FUSED_RENDER_CONTRIB_FAKE_GH"

#: One API round trip. Generous for a slow link, short enough that a wedged
#: `gh` cannot pin a job thread (contract §7: 20 s).
API_TIMEOUT_S = 20.0
#: GitHub creates a fork asynchronously: the POST answers 202 at once and the
#: copy becomes readable some seconds later (contract §7: poll up to 60 s).
FORK_POLL_S = 60.0
_FORK_POLL_STEP_S = 1.5
#: A push is proportional to the repo's size, unlike the API calls — same
#: headroom `github_setup._PUBLISH_TIMEOUT_S` gives `gh repo create --push`.
PUSH_TIMEOUT_S = 300.0
FETCH_TIMEOUT_S = 120.0
#: Who is signed in changes rarely and costs a round trip to ask (§7: 10 min).
#: A signed-out answer is trusted for much less: the person is probably in the
#: middle of signing in right now.
IDENTITY_TTL_S = 600.0
_SIGNED_OUT_TTL_S = 15.0
#: GitHub could not be asked (offline, rate-limited): non-strict callers are
#: answered None from cache this long (see `identity`).
_UNREACHABLE_TTL_S = 30.0
_UNREACHABLE = object()

REASONS = ("signed-out", "not-found", "forbidden", "offline", "conflict",
           "rate-limited", "failed")

_MESSAGES = {
    "signed-out": "Sign in to GitHub to continue.",
    "not-found": "GitHub could not find that app. It may be private, or it no longer exists.",
    "forbidden": "GitHub did not allow that for your account.",
    "offline": "GitHub could not be reached. Check your connection and try again.",
    "conflict": "Something changed on GitHub in the meantime. Try again.",
    "rate-limited": "GitHub is busy right now. Try again in a few minutes.",
    "failed": "GitHub could not finish that. Try again.",
}

# owner/name as GitHub allows them. Validated before a value is ever spliced
# into an API path, so a hostile string ("../../user/repos") cannot address
# a different endpoint than the one the caller meant.
_FULL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: The marker every proposal body sent from fused ends with (contract §8), so
#: a proposal whose local record was lost can still be recognised on GitHub.
PROPOSAL_MARKER = "<!-- fused:proposal v1 -->"


class GitHubError(PlainError):
    """A GitHub refusal: `reason` for the code, `message` for the person.
    `status` (the HTTP status, when there was one) and `detail` (gh's or git's
    raw words) are for the log and for callers that must tell two 422s apart —
    never for the page."""

    def __init__(self, reason: str, message: str | None = None, *,
                 status: int | None = None, detail: str = ""):
        assert reason in REASONS, reason
        super().__init__(message or _MESSAGES[reason])
        self.reason = reason
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:  # the log line, with the raw part
        return f"GitHubError({self.reason!r}, status={self.status}, detail={self.detail[:300]!r})"


# --------------------------------------------------------------- small helpers


def split_full(full_or_url: str) -> tuple[str, str]:
    """`(owner, name)` from "owner/name" or any GitHub URL spelling
    `appfile_git.normalize_url` folds (https, ssh, `.git`, `www.`). Raises
    `GitHubError("not-found")` for anything else — an unaddressable repo IS
    one GitHub cannot find."""
    s = (full_or_url or "").strip()
    if "github.com" in s.lower():
        from fused_render.appfile_git import normalize_url
        s = normalize_url(s).split("github.com/", 1)[-1]
    s = s.strip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    if not _FULL_RE.match(s):
        raise GitHubError("not-found", detail=f"not a repo: {full_or_url!r}")
    owner, name = s.split("/", 1)
    return owner, name


def _full(full: str) -> str:
    owner, name = split_full(full)
    return f"{owner}/{name}"


def same_login(a: str | None, b: str | None) -> bool:
    """GitHub logins are case-insensitive; every comparison goes through here."""
    return bool(a) and bool(b) and a.lower() == b.lower()


def noreply_email(user_id, login: str) -> str:
    """The address GitHub attributes to an account without exposing a real
    one, and maps back to the profile on every commit it appears on."""
    return f"{user_id}+{login}@users.noreply.github.com"


def _git_bin():
    # One resolver for the whole process — see app_git._git_bin.
    from fused_render.shell import git_tools
    return git_tools.git_bin()


def _git(repo: str, *args: str, env: dict | None = None, input: str | None = None,
         timeout: float = 60.0) -> subprocess.CompletedProcess | None:
    """One bounded git call in `repo`; None when git could not run at all.
    Absolute git, `-C` never `cwd=`, close_fds=False (app_git's docstring has
    the SIGSEGV story)."""
    try:
        return subprocess.run(
            [_git_bin(), "-C", repo, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            close_fds=False, env=env if env is not None else _local_env(),
            input=input, timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.info("git %s in %s did not run: %s", args[:1], repo, exc)
        return None


def _local_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "",
            "SSH_ASKPASS": "", "GIT_EDITOR": "false"}


def git_net_env() -> dict:
    """The environment for a git process that talks to GitHub — see the module
    docstring for why global and system config are switched off."""
    return {**_local_env(), "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_LFS_SKIP_SMUDGE": "1"}


def git_net_args() -> list[str]:
    """`-c` options (placed before the git subcommand) that make https to
    github.com authenticate through `gh`, whatever the user's git config
    says. Empty for the fake: its remotes are file paths."""
    if _fake() is not None:
        return []
    from fused_render import github_setup
    path, _source = github_setup.resolve()
    hooks = ["-c", "core.hooksPath=" + os.devnull]
    if not path:
        return ["-c", "credential.helper="] + hooks
    return ["-c", "credential.helper=",
            "-c", f"credential.https://github.com.helper=!{shlex.quote(path)} auth git-credential",
            *hooks]


def git_auth_args() -> list[str]:
    """inbox.py's spelling of `git_net_args`."""
    return git_net_args()


def clone_url(full_or_url: str) -> str:
    """The URL git should fetch from / push to for a repo: real GitHub's https
    URL, or the fake's bare repo path."""
    owner, name = split_full(full_or_url)
    fake = _fake()
    if fake is not None:
        return fake.bare_path(f"{owner}/{name}")
    return f"https://github.com/{owner}/{name}.git"


def _git_failure(res: subprocess.CompletedProcess | None, *, pushing: bool) -> GitHubError:
    """A plain `GitHubError` for a failed push/fetch; git's words go to the log."""
    if res is None:
        return GitHubError("offline")
    raw = ((res.stderr or "") + "\n" + (res.stdout or "")).strip()
    low = raw.lower()
    logger.info("GitHub git transfer failed (rc %s): %s", res.returncode, raw[-2000:])
    if any(m in low for m in _OFFLINE_MARKERS):
        return GitHubError("offline", detail=raw)
    if "permission to" in low and "denied" in low:
        return GitHubError("forbidden", detail=raw)
    if any(m in low for m in ("authentication failed", "could not read username",
                              "terminal prompts disabled", "invalid username or password")):
        return GitHubError("signed-out", detail=raw)
    if "repository not found" in low or "does not appear to be a git repository" in low:
        return GitHubError("not-found", detail=raw)
    if pushing and any(m in low for m in ("stale info", "rejected", "non-fast-forward",
                                          "fetch first")):
        return GitHubError("conflict", detail=raw)
    return GitHubError("failed", detail=raw)


_OFFLINE_MARKERS = (
    "could not resolve host", "no such host", "error connecting", "dial tcp",
    "connection refused", "connection timed out", "network is unreachable",
    "operation timed out", "timeout awaiting", "i/o timeout", "tls handshake",
    "failed to connect", "unable to access", "connection reset",
)


def url_rewrites(root: str, url: str | None = None) -> list[str]:
    """The repo's OWN `url.<x>.insteadOf` / `pushInsteadOf` rules that could
    re-point a GitHub transfer from `root` — `[]` when there are none.

    THE GUARD (r05). Global/system config is already off for GitHub
    transfers (`git_net_env`), but `.git/config` is not ours: anything
    running in the app folder (the app, a Claude turn, a README's
    instructions) can write `url.https://evil/.insteadOf=https://github.com/`
    and every push of Bob's work — with his GitHub credential helper attached
    — would go there. A rule counts when its prefix mentions github.com, or
    is a prefix of `url` (the fake's file paths included). Read with
    `--local --includes`, so a local `[include]` cannot hide one; the
    command-scope rules tests use (GIT_CONFIG_COUNT) are not local and do not
    count. `push`/`fetch` refuse when this is non-empty; follow.fetch's
    fallback should ask it too."""
    res = _git(root, "config", "--local", "--includes", "--get-regexp",
               r"^url\..*\.(insteadof|pushinsteadof)$", env=_local_env(), timeout=10)
    if res is None or res.returncode not in (0, 1):
        return []
    hits = []
    for line in (res.stdout or "").splitlines():
        key, _, value = line.partition(" ")
        prefix = value.strip()
        if not prefix:
            continue
        if "github.com" in prefix.lower() or (url and url.startswith(prefix)):
            hits.append(f"{key} = {prefix}")
    return hits


def _guard(root: str, url: str) -> None:
    hits = url_rewrites(root, url)
    if hits:
        logger.warning("refusing a GitHub transfer from %s: its .git/config rewrites "
                       "GitHub URLs: %s", root, "; ".join(hits))
        raise GitHubError("forbidden", "This app's settings would send your changes "
                                       "somewhere other than GitHub, so nothing was sent.")


def push(root: str, sha: str, full: str, branch: str, *, force: bool = False,
         lease: str | None = None) -> None:
    """Push commit `sha` from the repo at `root` to `refs/heads/<branch>` of
    GitHub repo `full`. Raises `GitHubError`.

    `lease` (DELIBERATE TIGHTENING of §7's bare `force`): the sha the caller
    last saw at that branch on GitHub, or "" for "must not exist yet". The
    push then replaces the branch only if it is still exactly that — the
    atomic form of "did someone else (Alice, via maintainer edits) push onto
    Bob's proposal since we looked?" — and a refusal is reason `conflict`.
    `force` without a lease overwrites unconditionally; only Remix's first
    push to a fresh repo and tests should want that."""
    if not _SHA_RE.match(sha or "") or not _BRANCH_RE.match(branch or ""):
        raise GitHubError("failed", detail=f"bad push args {sha!r} {branch!r}")
    url = clone_url(full)
    _guard(root, url)
    target = f"refs/heads/{branch}"
    args = ["push", "--no-verify", "--quiet"]
    if lease is not None:
        if lease and not _SHA_RE.match(lease):
            raise GitHubError("failed", detail=f"bad lease {lease!r}")
        args.append(f"--force-with-lease={target}:{lease}")
        spec = f"{sha}:{target}"
    else:
        spec = f"{'+' if force else ''}{sha}:{target}"
    res = _git(root, *git_net_args(), *args, url, spec, env=git_net_env(),
               timeout=PUSH_TIMEOUT_S)
    if res is None or res.returncode != 0:
        raise _git_failure(res, pushing=True)
    fake = _fake()
    if fake is not None:
        fake.after_push(full, branch)


def fetch(root: str, full_or_url: str, *refspecs: str, timeout: float = FETCH_TIMEOUT_S) -> bool:
    """`git fetch <github url> <refspecs…>` into the repo at `root`, with the
    same credentials and hygiene as `push`. Never raises: False means nothing
    was fetched (offline, gone, no access) and the caller keeps what it has.

    SAFE WITHOUT THE MUTATION SLOT when every refspec writes a private ref
    only its caller writes (`refs/fused/…`) — inbox.py's `_FETCH_QUIET`
    argument, adopted here: objects tolerate concurrent writers, FETCH_HEAD
    (auto-sync's) is not written, and the after-fetch housekeeping that
    rewrites shared files (gc and its packed-refs, maintenance, the
    commit-graph) is switched off. A refspec into a ref someone else also
    moves (`refs/fused/follow`) still wants the caller to hold the slot."""
    try:
        url = clone_url(full_or_url)
        _guard(root, url)
    except GitHubError:
        return False
    res = _git(root, *git_net_args(), "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "fetch.writeCommitGraph=false", "fetch", "--quiet", "--no-tags",
               "--no-write-fetch-head", url, *refspecs, env=git_net_env(),
               timeout=timeout)
    if res is None or res.returncode != 0:
        _git_failure(res, pushing=False)  # logs the raw words
        return False
    return True


# ------------------------------------------------------------- backend switch

_fakes: dict[str, "FakeGitHub"] = {}
_fakes_lock = threading.Lock()


def _fake() -> "FakeGitHub | None":
    root = os.environ.get(FAKE_ENV)
    if not root:
        return None
    root = os.path.realpath(root)
    with _fakes_lock:
        inst = _fakes.get(root)
        if inst is None:
            inst = _fakes[root] = FakeGitHub(root)
        return inst


def _backend():
    return _fake() or _REAL


def fake() -> "FakeGitHub":
    """The active fake (tests' handle on the other side). Raises when the
    real backend is selected, so a test can never mutate real GitHub."""
    inst = _fake()
    if inst is None:
        raise RuntimeError(f"{FAKE_ENV} is not set")
    return inst


# ------------------------------------------------------------- the public API

_identity_cache: dict[str, tuple[float, dict | None]] = {}
_identity_lock = threading.Lock()


def identity(*, strict: bool = False, refresh: bool = False) -> dict | None:
    """`{login, id, name, email}` of the signed-in GitHub account, or None
    when nobody is signed in. `email` is the account's noreply address — what
    every commit fused writes in this person's name carries.

    Cached 10 minutes (contract §7). Not signed in → None. GitHub unreachable
    → None too by default, which is what classification wants ("could not
    tell, check again later"); `strict=True` raises `GitHubError("offline")`
    instead, for a caller that must not mistake a dropped connection for
    "please sign in".

    "Could not tell" (offline, rate-limited, …) is cached too, for
    _UNREACHABLE_TTL_S, but ONLY for non-strict callers: offline, every gated
    POST's preflight would otherwise re-run `gh api user` on the request
    thread. A strict caller is about to act on the answer, so it skips that
    entry and asks again."""
    key = os.environ.get(FAKE_ENV) or "real"
    now = time.time()
    if not refresh:
        with _identity_lock:
            hit = _identity_cache.get(key)
        if hit is not None:
            at, value = hit
            if value is _UNREACHABLE:
                if not strict and 0 <= now - at <= _UNREACHABLE_TTL_S:
                    return None
            else:
                ttl = IDENTITY_TTL_S if value is not None else _SIGNED_OUT_TTL_S
                if 0 <= now - at <= ttl:
                    return dict(value) if value else None
    try:
        user = _backend().user()
    except GitHubError as exc:
        if exc.reason == "signed-out":
            user = None
        else:
            with _identity_lock:
                _identity_cache[key] = (now, _UNREACHABLE)
            if strict:
                raise
            return None
    value = None
    if user and user.get("login"):
        value = {"login": user["login"], "id": user.get("id"),
                 "name": user.get("name") or user["login"],
                 "email": noreply_email(user.get("id"), user["login"])}
    with _identity_lock:
        _identity_cache[key] = (now, value)
    return dict(value) if value else None


def unreachable() -> bool:
    """Whether the last `identity` answer of None meant "GitHub could not be
    asked" (cached, still fresh) rather than "nobody is signed in" — so a
    preflight can say "offline" instead of offering sign-in."""
    key = os.environ.get(FAKE_ENV) or "real"
    with _identity_lock:
        hit = _identity_cache.get(key)
    return bool(hit) and hit[1] is _UNREACHABLE and 0 <= time.time() - hit[0] <= _UNREACHABLE_TTL_S


def forget_identity() -> None:
    """Drop the cached identity — after a sign-in/out, and in tests."""
    with _identity_lock:
        _identity_cache.clear()


def _require_login() -> str:
    me = identity(strict=True)
    if not me:
        raise GitHubError("signed-out")
    return me["login"]


def org_role(org: str) -> str | None:
    """"admin" when the signed-in user is an OWNER of org `org`, "member" when
    an ordinary member, None otherwise (not a member, an invitation not yet
    accepted, a user rather than an org, or no permission to ask)."""
    if not _LOGIN_RE.match(org or ""):
        return None
    try:
        return _backend().org_role(org)
    except GitHubError as exc:
        if exc.reason in ("not-found", "forbidden"):
            return None
        raise


def repo(full: str) -> dict:
    """`{full_name, default_branch, owner, permissions:{push, admin}, private,
    allow_merge_commit, allow_squash_merge, allow_rebase_merge, fork_of}`."""
    return _backend().repo(_full(full))


def ensure_fork(full: str) -> str:
    """Bob's copy of `full` on GitHub ("bob/x" — or "bob/x-1" when he already
    had an unrelated x: GitHub picks the name, and the RETURNED full name is
    the one to use). Creates it when he has none, then waits (≤ 60 s) until
    GitHub can actually serve it. An existing fork is returned as it is. When
    the signed-in user owns `full` there is nothing to copy: `full` itself."""
    full = _full(full)
    login = _require_login()
    if same_login(split_full(full)[0], login):
        return full
    fork_full = _backend().create_fork(full)
    deadline = time.time() + FORK_POLL_S
    while True:
        try:
            info = _backend().repo(fork_full)
            if _backend().has_branch(fork_full, info.get("default_branch") or ""):
                return info.get("full_name") or fork_full
        except GitHubError as exc:
            if exc.reason not in ("not-found",):
                raise
        if time.time() >= deadline:
            raise GitHubError(
                "failed", "GitHub is still setting up your copy. Try again in a minute.")
        time.sleep(_FORK_POLL_STEP_S)


def branch_tip(full: str, branch: str) -> str | None:
    """The commit at `branch` of `full` on GitHub; None when the branch does
    not exist (an empty repo has none)."""
    if not _BRANCH_RE.match(branch or ""):
        return None
    return _backend().branch_tip(_full(full), branch)


def find_open_pr(full: str, head_owner: str, head_branch: str | None = None) -> dict | None:
    """The open pull request on `full` from `head_owner`'s `head_branch`, or
    None. With no `head_branch` (a proposal whose local record was lost): the
    newest open one from `head_owner` whose body carries `PROPOSAL_MARKER`."""
    full = _full(full)
    if head_branch is not None:
        if not _BRANCH_RE.match(head_branch):
            return None
        for pr in _backend().list_pulls(full, head=f"{head_owner}:{head_branch}"):
            if pr["state"] == "open":
                return pr
        return None
    mine = [pr for pr in _backend().list_pulls(full)
            if pr["state"] == "open" and same_login(pr.get("user"), head_owner)
            and PROPOSAL_MARKER in (pr.get("body") or "")]
    mine.sort(key=lambda pr: pr.get("created_at") or "", reverse=True)
    return mine[0] if mine else None


def open_pr(full: str, *, title: str, body: str, head: str, base: str) -> dict:
    """Open a pull request on `full` from `head` ("bob:branch") into `base`,
    with maintainer edits allowed (Alice may edit the proposal — handoff A2).
    An already-open one for the same head is reason `conflict`."""
    return _backend().open_pull(_full(full), title=title, body=body, head=head, base=base)


def get_pr(full: str, number: int) -> dict:
    """`{number, url, state, merged, merge_commit_sha, head_sha, head_repo,
    head_branch, base_branch, user, title, body, created_at, closed_at,
    maintainer_can_modify}`. `state` is "open" | "closed"; `merged` says which
    kind of closed. `merge_commit_sha` is None unless merged (GitHub fills it
    with a throwaway test-merge commit while a PR is open — never trust that)."""
    return _backend().get_pull(_full(full), int(number))


def pr_comments(full: str, number: int) -> list[dict]:
    """Every comment on a pull request, `{author, body, at}`, oldest first:
    conversation comments, review bodies and line comments alike — Alice
    writing on one line of the change is still Alice commenting."""
    rows = _backend().comments(_full(full), int(number))
    return sorted(rows, key=lambda c: c.get("at") or "")


def list_open_prs(full: str) -> list[dict]:
    """Every open pull request on `full`, newest first."""
    rows = [pr for pr in _backend().list_pulls(_full(full)) if pr["state"] == "open"]
    return sorted(rows, key=lambda pr: pr.get("created_at") or "", reverse=True)


def merge_pr(full: str, number: int, *, sha: str, method: str) -> dict:
    """Merge a pull request on GitHub — only if its head is still `sha` (the
    version that was reviewed: GitHub refuses, reason `conflict`, if Bob
    pushed since). `method` is "merge" | "squash" | "rebase".
    → `{"merged": bool, "sha": <the commit now on the base branch>}`."""
    if method not in ("merge", "squash", "rebase"):
        raise GitHubError("failed", detail=f"bad merge method {method!r}")
    if not _SHA_RE.match(sha or ""):
        raise GitHubError("failed", detail=f"bad sha {sha!r}")
    return _backend().merge_pull(_full(full), int(number), sha=sha, method=method)


def close_pr(full: str, number: int, *, comment: str | None = None) -> None:
    """Close a pull request, after posting `comment` on it when given."""
    full = _full(full)
    if comment:
        _backend().add_comment(full, int(number), comment)
    _backend().close_pull(full, int(number))


def comment(full: str, number: int, body: str) -> None:
    _backend().add_comment(_full(full), int(number), body)


def create_repo(name: str, *, description: str, private: bool = False) -> str:
    """A new, EMPTY repo in the signed-in user's account → its full name. A
    name already taken there is reason `conflict`."""
    if not re.match(r"^[A-Za-z0-9._-]{1,100}$", name or "") or name in (".", ".."):
        raise GitHubError("failed", "That name can't be used on GitHub. Use letters, "
                                    "numbers, dashes, dots or underscores.")
    return _backend().create_repo(name, description=description, private=private)


# ------------------------------------------------------------ the real backend


def _gh_path() -> str:
    from fused_render import github_setup
    path, _source = github_setup.resolve()
    if not path or not github_setup.executable(path):
        # No `gh` is the same next step as "signed out": the sign-in flow is
        # what installs it (GitHubSignIn over /api/github/install + login).
        raise GitHubError("signed-out", detail="no gh binary")
    return path


def _spawn_gh(cmd: list, stdin_text: str | None, timeout: float):
    """The one `gh` spawn. Broken out, like
    `github_setup._spawn_gh_repo_create`, so a test can fake exactly this."""
    # posix-spawn-exempt: cmd is `[path, "api", ...]` assembled by `_Real._api`
    # alone, where `path` comes from `github_setup.resolve()` — an absolute
    # file, so it carries a directory and CPython posix_spawns it. It is `gh`,
    # never git; close_fds=False is passed literally below and no cwd= exists
    # anywhere in this module (git gets `-C`).
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout, input=stdin_text,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
             "NO_COLOR": "1", "GH_HOST": "github.com", "GIT_TERMINAL_PROMPT": "0"},
        close_fds=False, text=True, encoding="utf-8", errors="replace",
    )


_HTTP_RE = re.compile(r"\(HTTP (\d{3})\)")


def _api_failure(rc: int, out: str, err: str) -> GitHubError:
    """Map one failed `gh api` call to a plain `GitHubError`. gh prints the
    response body (JSON) on stdout and "gh: Not Found (HTTP 404)" on stderr,
    exit 1; signed out is exit 4 with a "gh auth login" hint and no status."""
    m = _HTTP_RE.search(err or "")
    status = int(m.group(1)) if m else None
    body_msg = ""
    try:
        parsed = json.loads(out) if out and out.strip().startswith("{") else None
        if isinstance(parsed, dict):
            body_msg = " ".join(
                [str(parsed.get("message") or "")]
                + [str(e.get("message") or e.get("code") or "")
                   for e in parsed.get("errors") or [] if isinstance(e, dict)])
    except ValueError:
        pass
    detail = f"{err.strip()} {body_msg}".strip()
    low = detail.lower()
    if status is None:
        if any(m in low for m in ("gh auth login", "not logged", "authentication required",
                                  "no oauth token")):
            return GitHubError("signed-out", detail=detail)
        if any(m in low for m in _OFFLINE_MARKERS):
            return GitHubError("offline", detail=detail)
        return GitHubError("failed", detail=detail)
    if status == 401:
        return GitHubError("signed-out", status=status, detail=detail)
    if status == 429 or (status == 403 and "rate limit" in low):
        return GitHubError("rate-limited", status=status, detail=detail)
    if status == 403:
        return GitHubError("forbidden", status=status, detail=detail)
    if status == 404:
        return GitHubError("not-found", status=status, detail=detail)
    if status in (405, 409, 422):
        return GitHubError("conflict", status=status, detail=detail)
    if status >= 500:
        return GitHubError("failed", "GitHub is having trouble right now. Try again in a few minutes.",
                           status=status, detail=detail)
    return GitHubError("failed", status=status, detail=detail)


def _pr_out(raw: dict) -> dict:
    """GitHub's pull request object, narrowed to the fields §7 promises."""
    head = raw.get("head") or {}
    base = raw.get("base") or {}
    merged = bool(raw.get("merged_at")) or raw.get("merged") is True
    return {
        "number": raw.get("number"),
        "url": raw.get("html_url") or "",
        "state": "open" if raw.get("state") == "open" else "closed",
        "merged": merged,
        "merge_commit_sha": raw.get("merge_commit_sha") if merged else None,
        "head_sha": head.get("sha"),
        "head_repo": (head.get("repo") or {}).get("full_name"),
        "head_branch": head.get("ref"),
        "base_branch": base.get("ref"),
        "user": (raw.get("user") or {}).get("login"),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "created_at": raw.get("created_at"),
        "closed_at": raw.get("closed_at"),
        "maintainer_can_modify": bool(raw.get("maintainer_can_modify")),
    }


def _repo_out(raw: dict) -> dict:
    perms = raw.get("permissions") or {}
    parent = raw.get("parent") or {}
    return {
        "full_name": raw.get("full_name"),
        "default_branch": raw.get("default_branch") or "main",
        "owner": (raw.get("owner") or {}).get("login"),
        "permissions": {"push": bool(perms.get("push")), "admin": bool(perms.get("admin"))},
        "private": bool(raw.get("private")),
        "allow_merge_commit": raw.get("allow_merge_commit", True) is not False,
        "allow_squash_merge": raw.get("allow_squash_merge", True) is not False,
        "allow_rebase_merge": raw.get("allow_rebase_merge", True) is not False,
        "fork_of": parent.get("full_name") if raw.get("fork") else None,
        "description": raw.get("description") or "",
    }


class _Real:
    """`gh api`, one call per method."""

    def _api(self, method: str, path: str, body: dict | None = None, *,
             paginate: bool = False, timeout: float = API_TIMEOUT_S):
        cmd = [_gh_path(), "api", "--method", method,
               "-H", "Accept: application/vnd.github+json",
               "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if paginate:
            # --slurp wraps every page in one outer array, so the pages parse
            # as ONE JSON document instead of gh's concatenated `[…][…]`.
            cmd += ["--paginate", "--slurp"]
        if body is not None:
            cmd += ["--input", "-"]
        cmd.append(path)
        try:
            res = _spawn_gh(cmd, json.dumps(body) if body is not None else None, timeout)
        except subprocess.TimeoutExpired:
            raise GitHubError("offline", detail=f"gh api {path} timed out")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise GitHubError("failed", detail=f"gh did not run: {exc}")
        if res.returncode != 0:
            exc = _api_failure(res.returncode, res.stdout or "", res.stderr or "")
            logger.info("gh api %s %s failed: %r", method, path, exc)
            if exc.reason == "signed-out":
                forget_identity()
            raise exc
        text = (res.stdout or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            raise GitHubError("failed", detail=f"gh api {path}: unparseable answer")
        if paginate and isinstance(data, list):
            flat = []
            for page in data:
                flat.extend(page if isinstance(page, list) else [page])
            return flat
        return data

    def user(self) -> dict:
        return self._api("GET", "user") or {}

    def org_role(self, org: str) -> str | None:
        data = self._api("GET", f"user/memberships/orgs/{org}") or {}
        if data.get("state") != "active":
            return None
        return data.get("role") if data.get("role") in ("admin", "member") else None

    def repo(self, full: str) -> dict:
        return _repo_out(self._api("GET", f"repos/{full}") or {})

    def has_branch(self, full: str, branch: str) -> bool:
        return self.branch_tip(full, branch) is not None

    def branch_tip(self, full: str, branch: str) -> str | None:
        if not branch:
            return None
        try:
            data = self._api("GET", f"repos/{full}/branches/{branch}") or {}
        except GitHubError as exc:
            if exc.reason == "not-found":
                return None
            raise
        sha = (data.get("commit") or {}).get("sha")
        return sha if isinstance(sha, str) and _SHA_RE.match(sha) else None

    def create_fork(self, full: str) -> str:
        data = self._api("POST", f"repos/{full}/forks", {"default_branch_only": True}) or {}
        name = data.get("full_name")
        if not name:
            raise GitHubError("failed", detail="fork answer had no full_name")
        return name

    def list_pulls(self, full: str, head: str | None = None) -> list[dict]:
        q = "state=open&per_page=100" + (f"&head={head}" if head else "")
        return [_pr_out(r) for r in self._api("GET", f"repos/{full}/pulls?{q}", paginate=True) or []]

    def get_pull(self, full: str, number: int) -> dict:
        return _pr_out(self._api("GET", f"repos/{full}/pulls/{number}") or {})

    def open_pull(self, full: str, *, title: str, body: str, head: str, base: str) -> dict:
        return _pr_out(self._api("POST", f"repos/{full}/pulls", {
            "title": title, "body": body, "head": head, "base": base,
            "maintainer_can_modify": True}) or {})

    def comments(self, full: str, number: int) -> list[dict]:
        out = []
        for c in self._api("GET", f"repos/{full}/issues/{number}/comments?per_page=100",
                           paginate=True) or []:
            out.append({"author": (c.get("user") or {}).get("login"),
                        "body": c.get("body") or "", "at": c.get("created_at")})
        for r in self._api("GET", f"repos/{full}/pulls/{number}/reviews?per_page=100",
                           paginate=True) or []:
            if (r.get("body") or "").strip():
                out.append({"author": (r.get("user") or {}).get("login"),
                            "body": r.get("body") or "", "at": r.get("submitted_at")})
        for c in self._api("GET", f"repos/{full}/pulls/{number}/comments?per_page=100",
                           paginate=True) or []:
            out.append({"author": (c.get("user") or {}).get("login"),
                        "body": c.get("body") or "", "at": c.get("created_at")})
        return out

    def merge_pull(self, full: str, number: int, *, sha: str, method: str) -> dict:
        data = self._api("PUT", f"repos/{full}/pulls/{number}/merge",
                         {"sha": sha, "merge_method": method}) or {}
        return {"merged": bool(data.get("merged")), "sha": data.get("sha")}

    def close_pull(self, full: str, number: int) -> None:
        self._api("PATCH", f"repos/{full}/pulls/{number}", {"state": "closed"})

    def add_comment(self, full: str, number: int, body: str) -> None:
        self._api("POST", f"repos/{full}/issues/{number}/comments", {"body": body})

    def create_repo(self, name: str, *, description: str, private: bool) -> str:
        data = self._api("POST", "user/repos", {
            "name": name, "description": description, "private": bool(private),
            "auto_init": False}) or {}
        if not data.get("full_name"):
            raise GitHubError("failed", detail="create answer had no full_name")
        return data["full_name"]


_REAL = _Real()


# ------------------------------------------------------------------ the fake


class FakeGitHub:
    """GitHub in a folder, for tests and local smoke. Same backend methods as
    `_Real`, plus mutators for the people on the other side.

    State in `<root>/state.json`, re-read and re-written per operation under a
    lock (+ an flock, so a smoke CLI in another process can mutate while the
    server runs). Repos are bare repos at `<root>/repos/<owner>/<name>.git`
    (lowercased — GitHub names are case-insensitive). A pull request keeps its
    head LIVE (read from the head repo's branch while open), mirrors it into
    the base repo's `refs/pull/<n>/head` the way GitHub does, and freezes it
    when merged or closed."""

    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        self._local = threading.local()
        os.makedirs(os.path.join(root, "repos"), exist_ok=True)

    # ---- state

    def _path(self) -> str:
        return os.path.join(self.root, "state.json")

    @contextlib.contextmanager
    def _txn(self):
        """One read-modify-write of state.json. REENTRANT within a thread: an
        inner `_txn` shares the outer one's dict and only the outermost saves
        — a second `flock` on a fresh fd from the same process would block
        on the first (flock locks belong to the open file description)."""
        with self._lock:
            if getattr(self._local, "depth", 0):
                self._local.depth += 1
                try:
                    yield self._local.state
                finally:
                    self._local.depth -= 1
                return
            lockf = open(os.path.join(self.root, ".lock"), "a+")
            try:
                try:
                    import fcntl
                    fcntl.flock(lockf, fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover - non-posix
                    pass
                try:
                    with open(self._path(), encoding="utf-8") as fh:
                        state = json.load(fh)
                except (OSError, ValueError):
                    state = {}
                for key, empty in (("me", None), ("users", {}), ("orgs", {}),
                                   ("repos", {}), ("pulls", {}), ("calls", []),
                                   # fork full -> reads left before it answers
                                   ("hidden", {}),
                                   # backend method -> [reason, times left]
                                   ("fail", {})):
                    state.setdefault(key, empty)
                before = json.dumps(state, sort_keys=True)
                self._local.depth, self._local.state = 1, state
                try:
                    yield state
                finally:
                    self._local.depth, self._local.state = 0, None
                    # Saved even when the body raised: a `fail_next` counter
                    # the raising call just consumed must stay consumed.
                    if json.dumps(state, sort_keys=True) != before:
                        tmp = self._path() + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as fh:
                            json.dump(state, fh, indent=1, sort_keys=True)
                        os.replace(tmp, self._path())
            finally:
                lockf.close()

    def state(self) -> dict:
        with self._txn() as st:
            return json.loads(json.dumps(st))

    def bare_path(self, full: str) -> str:
        owner, name = split_full(full)
        return os.path.join(self.root, "repos", owner.lower(), name.lower() + ".git")

    def git_env(self) -> dict:
        """Env vars that make EVERY git process (follow.fetch, a clone, …)
        resolve https://github.com/<o>/<r> to this fake's bare repos. Git's
        local transport tries `<path>.git` too, so the `.git`-less URL a record
        stores works as well."""
        return {"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{os.path.join(self.root, 'repos')}/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/"}

    # ---- the people (test / smoke side)

    def add_user(self, login: str, name: str | None = None) -> dict:
        with self._txn() as st:
            u = st["users"].get(login.lower())
            if u is None:
                u = st["users"][login.lower()] = {
                    "login": login, "id": 1000 + len(st["users"]), "name": name or login}
            return dict(u)

    def sign_in(self, login: str) -> None:
        self.add_user(login)
        with self._txn() as st:
            st["me"] = login
        forget_identity()

    def sign_out(self) -> None:
        with self._txn() as st:
            st["me"] = None
        forget_identity()

    def set_org(self, org: str, roles: dict) -> None:
        with self._txn() as st:
            st["orgs"][org.lower()] = {k.lower(): v for k, v in roles.items()}

    def fail_next(self, method: str, reason: str, times: int = 1) -> None:
        """Make the next `times` calls of backend `method` raise `reason`."""
        with self._txn() as st:
            st["fail"][method] = [reason, times]

    def delay_fork(self, polls: int) -> None:
        """The next fork created answers not-found for `polls` reads first —
        GitHub's asynchronous fork creation."""
        with self._txn() as st:
            st["fork_delay"] = polls

    def user_info(self, login: str) -> dict:
        with self._txn() as st:
            return dict(st["users"][login.lower()])

    def email_of(self, login: str) -> str:
        u = self.user_info(login)
        return noreply_email(u["id"], u["login"])

    def create_repo_as(self, owner: str, name: str, files: dict | None = None, *,
                       branch: str = "main", private: bool = False,
                       message: str = "First version", fork_of: str | None = None) -> str:
        """A repo owned by `owner` (a user, or an org), seeded with `files`."""
        full = f"{owner}/{name}"
        bare = self.bare_path(full)
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        self._g(self.root, "init", "-q", "--bare", "-b", branch, bare)
        with self._txn() as st:
            st["repos"][full.lower()] = {
                "full_name": full, "owner": owner, "name": name, "default_branch": branch,
                "private": private, "fork_of": fork_of, "collaborators": [],
                "allow_merge_commit": True, "allow_squash_merge": True,
                "allow_rebase_merge": True, "description": "", "next_number": 1}
        if files:
            self.commit(full, branch, files, message, author=owner)
        return full

    def add_collaborator(self, full: str, login: str) -> None:
        with self._txn() as st:
            st["repos"][full.lower()]["collaborators"].append(login.lower())

    def set_merge_methods(self, full: str, *, merge=True, squash=True, rebase=True) -> None:
        with self._txn() as st:
            r = st["repos"][full.lower()]
            r.update(allow_merge_commit=merge, allow_squash_merge=squash,
                     allow_rebase_merge=rebase)

    def tip(self, full: str, branch: str | None = None) -> str | None:
        branch = branch or self._repo_row(full)["default_branch"]
        res = self._g(self.bare_path(full), "rev-parse", "-q", "--verify",
                      f"refs/heads/{branch}^{{commit}}", check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def read(self, full: str, path: str, ref: str | None = None) -> str | None:
        ref = ref or f"refs/heads/{self._repo_row(full)['default_branch']}"
        res = self._g(self.bare_path(full), "show", f"{ref}:{path}", check=False)
        return res.stdout if res.returncode == 0 else None

    def commit(self, full: str, branch: str, files: dict, message: str, *,
               author: str) -> str:
        """Commit `files` ({path: text, or None to delete}) onto `branch` of
        `full` as GitHub user `author` — Alice pushing to main, or Alice pushing
        onto Bob's proposal branch through maintainer edits."""
        bare = self.bare_path(full)
        parent = self.tip(full, branch)
        email = self.email_of(self.add_user(author)["login"])
        fd, index = tempfile.mkstemp(prefix="fakegh-index-", dir=self.root)
        os.close(fd)
        os.unlink(index)
        env = {**_local_env(), "GIT_INDEX_FILE": index}
        try:
            if parent:
                self._g(bare, "read-tree", parent, env=env)
            for path, text in files.items():
                if text is None:
                    self._g(bare, "update-index", "--force-remove", "--", path, env=env)
                    continue
                blob = self._g(bare, "hash-object", "-w", "--stdin", input=text).stdout.strip()
                self._g(bare, "update-index", "--add", "--cacheinfo",
                        f"100644,{blob},{path}", env=env)
            tree = self._g(bare, "write-tree", env=env).stdout.strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(index)
        sha = self._commit_tree(bare, tree, [parent] if parent else [], message,
                                author=(author, email), committer=(author, email))
        self._g(bare, "update-ref", f"refs/heads/{branch}", sha, parent or "0" * 40)
        self.after_push(full, branch)
        return sha

    def comment_as(self, login: str, full: str, number: int, body: str, *,
                   at: str | None = None) -> None:
        self.add_user(login)
        with self._txn() as st:
            pr = self._pr_row(st, full, number)
            pr["comments"].append({"author": login, "body": body, "at": at or _iso_now()})

    def edit_pr(self, full: str, number: int, *, title: str | None = None,
                body: str | None = None) -> None:
        with self._txn() as st:
            pr = self._pr_row(st, full, number)
            if title is not None:
                pr["title"] = title
            if body is not None:
                pr["body"] = body

    def close(self, full: str, number: int) -> None:
        with self._txn() as st:
            pr = self._pr_row(st, full, number)
            self._freeze(pr)
            pr["state"] = "closed"
            pr["closed_at"] = _iso_now()

    def reopen(self, full: str, number: int) -> None:
        with self._txn() as st:
            pr = self._pr_row(st, full, number)
            if pr["merged"]:
                raise GitHubError("conflict")
            pr["state"] = "open"
            pr["closed_at"] = None

    def merge(self, full: str, number: int, method: str = "merge", *,
              as_login: str | None = None) -> str:
        """Alice accepting on github.com: realistic commits on the base repo.
        merge = a two-parent merge commit; squash = ONE new commit (Bob
        authored, the commit messages folded into its body); rebase = each
        commit re-created on top of the base, authors kept. Returns
        `merge_commit_sha` as GitHub reports it for each method."""
        with self._txn() as st:
            pr = dict(self._pr_row(st, full, number))
            row = self._repo_row_st(st, full)
        merger = as_login or row["owner"]
        self.add_user(merger)
        bare = self.bare_path(full)
        self._sync_pull_ref(full, pr)
        base_ref = f"refs/heads/{pr['base']}"
        base_tip = self._g(bare, "rev-parse", base_ref).stdout.strip()
        head = self._g(bare, "rev-parse", f"refs/pull/{number}/head").stdout.strip()
        merger_id = (merger, self.email_of(merger))
        github = ("GitHub", "noreply@github.com")
        if method == "rebase":
            mb = self._g(bare, "merge-base", base_tip, head).stdout.strip()
            commits = self._g(bare, "rev-list", "--reverse", "--topo-order",
                              f"{mb}..{head}").stdout.split()
            cur = base_tip
            for c in commits:
                tree = self._merge_tree(bare, cur, c, merge_base=f"{c}^")
                info = self._g(bare, "log", "-1", "--format=%an%x00%ae%x00%aI%x00%B", c).stdout
                an, ae, ad, msg = info.split("\x00", 3)
                cur = self._commit_tree(bare, tree, [cur], msg.rstrip("\n") + "\n",
                                        author=(an, ae), committer=github, author_date=ad)
            new = cur
        else:
            tree = self._merge_tree(bare, base_tip, head)
            if method == "merge":
                msg = f"Merge pull request #{number} from {pr['head_repo'].split('/')[0]}/{pr['head_branch']}\n\n{pr['title']}\n"
                new = self._commit_tree(bare, tree, [base_tip, head], msg,
                                        author=merger_id, committer=github)
            else:
                mb = self._g(bare, "merge-base", base_tip, head).stdout.strip()
                subjects = self._g(bare, "log", "--reverse", "--format=* %s",
                                   f"{mb}..{head}").stdout.strip()
                coauthors = sorted(set(re.findall(
                    r"^Co-authored-by: .+$",
                    self._g(bare, "log", "--format=%B", f"{mb}..{head}").stdout, re.M)))
                msg = f"{pr['title']} (#{number})\n\n{subjects}\n"
                if coauthors:
                    msg += "\n" + "\n".join(coauthors) + "\n"
                bob = (pr["user"], self.email_of(pr["user"]))
                new = self._commit_tree(bare, tree, [base_tip], msg, author=bob,
                                        committer=github)
        self._g(bare, "update-ref", base_ref, new, base_tip)
        with self._txn() as st:
            row_pr = self._pr_row(st, full, number)
            self._freeze(row_pr)
            row_pr.update(state="closed", merged=True, merge_commit_sha=new,
                          closed_at=_iso_now())
        return new

    # ---- backend surface (what the module-level functions call)

    def _call(self, st: dict, method: str) -> None:
        st["calls"].append(method)
        del st["calls"][:-200]
        fail = st["fail"].get(method)
        if fail and fail[1] > 0:
            fail[1] -= 1
            raise GitHubError(fail[0], detail=f"fake: {method} told to fail")

    def _me(self, st: dict) -> str:
        if not st["me"]:
            raise GitHubError("signed-out")
        return st["me"]

    def _visible(self, st: dict, full: str) -> dict:
        row = st["repos"].get(full.lower())
        if row is None:
            raise GitHubError("not-found", status=404)
        if row["private"]:
            me = (st["me"] or "").lower()
            org = st["orgs"].get(row["owner"].lower(), {})
            if me != row["owner"].lower() and me not in row["collaborators"] and me not in org:
                raise GitHubError("not-found", status=404)
        return row

    def _can_push(self, st: dict, row: dict, login: str) -> bool:
        login = (login or "").lower()
        org = st["orgs"].get(row["owner"].lower(), {})
        return (login == row["owner"].lower() or login in row["collaborators"]
                or org.get(login) == "admin")

    def user(self) -> dict:
        with self._txn() as st:
            self._call(st, "user")
            me = self._me(st)
            return dict(st["users"][me.lower()])

    def org_role(self, org: str) -> str | None:
        with self._txn() as st:
            self._call(st, "org_role")
            me = self._me(st).lower()
            if org.lower() not in st["orgs"]:
                raise GitHubError("not-found", status=404)
            return st["orgs"][org.lower()].get(me)

    def repo(self, full: str) -> dict:
        with self._txn() as st:
            self._call(st, "repo")
            hidden = st["hidden"].get(full.lower())
            if hidden:
                st["hidden"][full.lower()] = hidden - 1
                raise GitHubError("not-found", status=404)
            row = self._visible(st, full)
            me = st["me"] or ""
            return {"full_name": row["full_name"], "default_branch": row["default_branch"],
                    "owner": row["owner"],
                    "permissions": {"push": self._can_push(st, row, me),
                                    "admin": me.lower() == row["owner"].lower()},
                    "private": row["private"],
                    "allow_merge_commit": row["allow_merge_commit"],
                    "allow_squash_merge": row["allow_squash_merge"],
                    "allow_rebase_merge": row["allow_rebase_merge"],
                    "fork_of": row["fork_of"], "description": row.get("description", "")}

    def has_branch(self, full: str, branch: str) -> bool:
        return bool(branch) and self.tip(full, branch) is not None

    def branch_tip(self, full: str, branch: str) -> str | None:
        with self._txn() as st:
            self._call(st, "branch_tip")
            self._visible(st, full)
        return self.tip(full, branch) if branch else None

    def create_fork(self, full: str) -> str:
        with self._txn() as st:
            self._call(st, "create_fork")
            me = self._me(st)
            src = self._visible(st, full)
            for row in st["repos"].values():
                if same_login(row["owner"], me) and (row["fork_of"] or "").lower() == full.lower():
                    return row["full_name"]
            name, n = src["name"], 0
            while f"{me}/{name}".lower() in st["repos"]:
                n += 1
                name = f"{src['name']}-{n}"
            fork_full = f"{me}/{name}"
            st["repos"][fork_full.lower()] = {
                **src, "full_name": fork_full, "owner": me, "name": name,
                "fork_of": src["full_name"], "collaborators": [], "next_number": 1,
                "private": src["private"]}
            delay = st.pop("fork_delay", 0)
            if delay:
                st["hidden"][fork_full.lower()] = delay
        dst = self.bare_path(fork_full)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        self._g(self.root, "clone", "-q", "--bare", "--single-branch", "--branch",
                src["default_branch"], self.bare_path(full), dst)
        return fork_full

    def list_pulls(self, full: str, head: str | None = None) -> list[dict]:
        with self._txn() as st:
            self._call(st, "list_pulls")
            self._visible(st, full)
            rows = [dict(p) for p in st["pulls"].get(full.lower(), [])]
        out = []
        for pr in rows:
            if head and f"{pr['head_repo'].split('/')[0]}:{pr['head_branch']}".lower() != head.lower():
                continue
            out.append(self._pr_public(full, pr))
        return out

    def get_pull(self, full: str, number: int) -> dict:
        with self._txn() as st:
            self._call(st, "get_pull")
            self._visible(st, full)
            pr = dict(self._pr_row(st, full, number))
        return self._pr_public(full, pr)

    def open_pull(self, full: str, *, title: str, body: str, head: str, base: str) -> dict:
        head_owner, head_branch = head.split(":", 1)
        with self._txn() as st:
            self._call(st, "open_pull")
            me = self._me(st)
            row = self._visible(st, full)
            head_repo = None
            for r in st["repos"].values():
                if same_login(r["owner"], head_owner) and (
                        r["full_name"].lower() == full.lower()
                        or (r["fork_of"] or "").lower() == full.lower()):
                    head_repo = r["full_name"]
            if head_repo is None:
                raise GitHubError("conflict", status=422, detail="head repo is not a fork")
            for p in st["pulls"].get(full.lower(), []):
                if (p["state"] == "open" and p["head_repo"].lower() == head_repo.lower()
                        and p["head_branch"] == head_branch):
                    raise GitHubError("conflict", status=422,
                                      detail="A pull request already exists")
        head_sha = self.tip(head_repo, head_branch)
        if head_sha is None:
            raise GitHubError("conflict", status=422, detail="head branch missing")
        base_sha = self.tip(full, base)
        if base_sha is None:
            raise GitHubError("conflict", status=422, detail="base branch missing")
        self._g(self.bare_path(full), "fetch", "-q", self.bare_path(head_repo),
                f"+refs/heads/{head_branch}:refs/pull/tmp", check=False)
        mb = self._g(self.bare_path(full), "merge-base", base_sha, "refs/pull/tmp", check=False)
        if mb.returncode == 0 and mb.stdout.strip() == head_sha:
            raise GitHubError("conflict", status=422, detail="No commits between")
        with self._txn() as st:
            row = st["repos"][full.lower()]
            number = row["next_number"]
            row["next_number"] = number + 1
            pr = {"number": number, "title": title, "body": body, "state": "open",
                  "merged": False, "merge_commit_sha": None, "head_repo": head_repo,
                  "head_branch": head_branch, "base": base, "user": me,
                  "created_at": _iso_now(), "closed_at": None, "comments": [],
                  "maintainer_can_modify": True, "frozen_head": None}
            st["pulls"].setdefault(full.lower(), []).append(pr)
        self._g(self.bare_path(full), "update-ref", "-d", "refs/pull/tmp", check=False)
        self._sync_pull_ref(full, pr)
        return self._pr_public(full, pr)

    def comments(self, full: str, number: int) -> list[dict]:
        with self._txn() as st:
            self._call(st, "comments")
            self._visible(st, full)
            return [dict(c) for c in self._pr_row(st, full, number)["comments"]]

    def merge_pull(self, full: str, number: int, *, sha: str, method: str) -> dict:
        with self._txn() as st:
            self._call(st, "merge_pull")
            me = self._me(st)
            row = self._visible(st, full)
            if not self._can_push(st, row, me):
                raise GitHubError("forbidden", status=403)
            allowed = {"merge": row["allow_merge_commit"], "squash": row["allow_squash_merge"],
                       "rebase": row["allow_rebase_merge"]}
            if not allowed[method]:
                raise GitHubError("conflict", status=405, detail="method not allowed")
            pr = dict(self._pr_row(st, full, number))
        if pr["state"] != "open":
            raise GitHubError("conflict", status=405, detail="not open")
        live = self._pr_public(full, pr)["head_sha"]
        if live != sha:
            raise GitHubError("conflict", status=409, detail="Head branch was modified")
        try:
            new = self.merge(full, number, method, as_login=me)
        except subprocess.CalledProcessError as exc:
            raise GitHubError("conflict", status=405, detail=f"not mergeable: {exc}")
        return {"merged": True, "sha": new}

    def close_pull(self, full: str, number: int) -> None:
        with self._txn() as st:
            self._call(st, "close_pull")
            self._me(st)
            self._visible(st, full)
        self.close(full, number)

    def add_comment(self, full: str, number: int, body: str) -> None:
        with self._txn() as st:
            self._call(st, "add_comment")
            me = self._me(st)
            self._visible(st, full)
            self._pr_row(st, full, number)["comments"].append(
                {"author": me, "body": body, "at": _iso_now()})

    def create_repo(self, name: str, *, description: str, private: bool) -> str:
        with self._txn() as st:
            self._call(st, "create_repo")
            me = self._me(st)
            if f"{me}/{name}".lower() in st["repos"]:
                raise GitHubError("conflict", status=422, detail="name already exists")
        full = self.create_repo_as(me, name, None, private=private)
        with self._txn() as st:
            st["repos"][full.lower()]["description"] = description
            # GitHub makes the first branch pushed to an empty repo its default.
            st["repos"][full.lower()]["empty"] = True
        return full

    def after_push(self, full: str, branch: str) -> None:
        """Bookkeeping a real push gets from GitHub for free: an empty repo's
        first pushed branch becomes its default; open PRs' refs follow."""
        with self._txn() as st:
            row = st["repos"].get(full.lower())
            if row and row.pop("empty", False):
                row["default_branch"] = branch
                self._g(self.bare_path(full), "symbolic-ref", "HEAD",
                        f"refs/heads/{branch}", check=False)

    # ---- internals

    def _repo_row(self, full: str) -> dict:
        with self._txn() as st:
            return dict(self._repo_row_st(st, full))

    @staticmethod
    def _repo_row_st(st: dict, full: str) -> dict:
        row = st["repos"].get(full.lower())
        if row is None:
            raise GitHubError("not-found", status=404)
        return row

    @staticmethod
    def _pr_row(st: dict, full: str, number: int) -> dict:
        for pr in st["pulls"].get(full.lower(), []):
            if pr["number"] == int(number):
                return pr
        raise GitHubError("not-found", status=404)

    def _freeze(self, pr: dict) -> None:
        if pr["state"] == "open":
            pr["frozen_head"] = self.tip(pr["head_repo"], pr["head_branch"])

    def _pr_public(self, full: str, pr: dict) -> dict:
        if pr["state"] == "open":
            head_sha = self.tip(pr["head_repo"], pr["head_branch"])
            self._sync_pull_ref(full, pr)
        else:
            head_sha = pr["frozen_head"]
        return {
            "number": pr["number"],
            "url": f"https://github.com/{full}/pull/{pr['number']}",
            "state": pr["state"], "merged": pr["merged"],
            "merge_commit_sha": pr["merge_commit_sha"] if pr["merged"] else None,
            "head_sha": head_sha, "head_repo": pr["head_repo"],
            "head_branch": pr["head_branch"], "base_branch": pr["base"],
            "user": pr["user"], "title": pr["title"], "body": pr["body"],
            "created_at": pr["created_at"], "closed_at": pr["closed_at"],
            "maintainer_can_modify": pr["maintainer_can_modify"],
        }

    def _sync_pull_ref(self, full: str, pr: dict) -> None:
        if pr["state"] != "open":
            return
        self._g(self.bare_path(full), "fetch", "-q", self.bare_path(pr["head_repo"]),
                f"+refs/heads/{pr['head_branch']}:refs/pull/{pr['number']}/head", check=False)

    def _merge_tree(self, bare: str, ours: str, theirs: str, *, merge_base: str | None = None) -> str:
        args = ["merge-tree", "--write-tree"]
        if merge_base:
            args.append(f"--merge-base={merge_base}")
        res = self._g(bare, *args, ours, theirs, check=False)
        if res.returncode != 0:
            raise GitHubError("conflict", status=405, detail="Pull Request is not mergeable")
        return res.stdout.split("\n", 1)[0].strip()

    def _commit_tree(self, bare: str, tree: str, parents: list, message: str, *,
                     author: tuple, committer: tuple, author_date: str | None = None) -> str:
        env = {**_local_env(), "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
        if author_date:
            env["GIT_AUTHOR_DATE"] = author_date
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        return self._g(bare, *args, env=env, input=message).stdout.strip()

    def _g(self, repo: str, *args: str, env: dict | None = None, input: str | None = None,
           check: bool = True) -> subprocess.CompletedProcess:
        res = _git(repo, *args, env=env, input=input, timeout=60)
        if res is None:
            raise RuntimeError(f"fake github: git {args[:1]} did not run")
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, args, res.stdout, res.stderr)
        return res


def _iso_now() -> str:
    # Microseconds: two comments a test writes in the same second must still
    # order, and GitHub's own ISO strings compare correctly against these.
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1e6) % 1000000:06d}Z"


# ---------------------------------------------------------- smoke-test CLI

_HELP = """usage: python -m fused_render.contrib.gh <fake dir> <command> [args]
  env                               print `export` lines (fake on, git URLs mapped)
  user <login> [name]               add a GitHub user
  signin <login> | signout          who this machine is signed in as
  seed <owner> <name> <folder>      create owner/name from a folder's files
  push <login> <full> <branch> <path>=<text>...   commit files onto a branch
  comment <login> <full> <n> <text>
  merge <full> <n> [merge|squash|rebase]
  close <full> <n>
  show                              dump state.json"""


def _cli(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("help", "-h", "--help"):
        print(_HELP)
        return 0
    f = FakeGitHub(os.path.realpath(argv[0]))
    cmd, a = argv[1], argv[2:]
    if cmd == "env":
        print(f"export {FAKE_ENV}={shlex.quote(f.root)}")
        for k, v in f.git_env().items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "user":
        print(f.add_user(a[0], a[1] if len(a) > 1 else None))
    elif cmd == "signin":
        f.sign_in(a[0])
    elif cmd == "signout":
        f.sign_out()
    elif cmd == "seed":
        files = {}
        for dirpath, dirnames, names in os.walk(a[2]):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".fused", ".venv")]
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, encoding="utf-8", errors="replace") as fh:
                    files[os.path.relpath(p, a[2])] = fh.read()
        f.add_user(a[0])
        print(f.create_repo_as(a[0], a[1], files))
    elif cmd == "push":
        files = dict(kv.split("=", 1) for kv in a[3:])
        print(f.commit(a[1], a[2], files, "Edit from GitHub", author=a[0]))
    elif cmd == "comment":
        f.comment_as(a[0], a[1], int(a[2]), " ".join(a[3:]))
    elif cmd == "merge":
        print(f.merge(a[0], int(a[1]), a[2] if len(a) > 2 else "merge"))
    elif cmd == "close":
        f.close(a[0], int(a[1]))
    elif cmd == "show":
        print(json.dumps(f.state(), indent=1))
    else:
        print(_HELP)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))
