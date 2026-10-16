"""Proposals (contract §8): Bob's changes to Alice's app, sent to her for
review as a pull request from his own copy of the repo on GitHub, in his
name, with Claude credited — plus Update, Withdraw, and the status refresh
that follows the proposal on GitHub and closes the loop when it is merged.

GITHUB IS THE TRUTH (§0.4). The record's `proposal` is the proposal's
IDENTITY on GitHub (repo + number) and a cache of what GitHub said last.
`refresh` re-derives every state from GitHub; nothing here infers "merged"
or "declined" from local history. A merge is recognised by the PR's own
`merged` flag and `merge_commit_sha`, never by matching Bob's commits — so
Alice squashing, rebasing or editing the proposal before accepting it
resolves exactly the same way. A record lost entirely (a reinstall, a store
reset) is recovered from GitHub too: Send adopts the open proposal from
Bob's account that carries fused's footer marker instead of opening a
second one.

WHAT IS SENT IS EXACTLY WHAT BOB RUNS (D16). Send/Update first commit any
uncommitted tracked edits as one "Your edits" commit, so the proposal is the
folder as it stands, not as of the last Claude turn.

CREDIT (§8 step 4). Claude's turns and app_git commit as `Fused
<apps@fused.io>` — no person. Before anything leaves the machine, every
commit in Bob's range authored by that identity is re-created with Bob as
author and committer (his GitHub noreply address) and ONE `Co-authored-by:
Claude` trailer; commits authored by anyone else (Alice's own edits brought
in below, Bob's hand-made commits) keep their authorship. Same trees, so
landing the rewritten tip (`replay.land`: backup ref first, `reset --keep`)
changes no file. Dates are carried over, which makes the rewrite a pure
function of content: re-running it over already-credited commits yields the
SAME commits, so an Update never re-sends what GitHub already has. Plumbing
only (`commit-tree`), never a rebase in the real folder (§0.3).

ALICE EDITING THE PROPOSAL (the decision the TL asked for). Proposals are
opened with maintainer edits allowed, so the head on GitHub can move without
Bob: Alice fixing a typo on github.com pushes onto Bob's branch. The record
keeps `pushed_head`, the exact commit WE last put there (the engine re-bases
`proposed_head` across auto-integrates; `pushed_head` is never rewritten).
On Update, a GitHub head that differs from `pushed_head` means someone else
added to the proposal: their commits (`pushed_head..<GitHub head>`) are
fetched and REPLAYED ON TOP OF Bob's current version in a scratch worktree.
Clean → Bob's copy lands that result (so he now runs, and sends, both), and
the push replaces the branch only if it still holds the head we just read
(`--force-with-lease`; a race is a plain "try again"). Overlap → Update
refuses with a plain sentence and nothing moves, locally or on GitHub — her
commits are never clobbered and his edits are never lost. There is no Merge
with Claude purpose for this corner in stage A; withdrawing and proposing
again is the way out. A title or description edited on github.com is simply
read back on the next refresh.

ONE OPEN PROPOSAL PER APP. Send refuses while one is open (that's Update);
after it is merged or declined, a new Send replaces the settled record.

Long operations (draft, send, update) are synchronous functions here that
raise `jobs.PlainError` (or `gh.GitHubError`, a subclass carrying `reason`);
the `start_*` helpers preflight on the request thread and wrap them in
`jobs.start`, answering the route's `{ok, job}` / `{ok: False, reason,
message}` directly.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from fused_render.contrib import gh
from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

CO_AUTHOR = "Co-authored-by: Claude <noreply@anthropic.com>"
FUSED_EMAIL = "apps@fused.io"
FOOTER = "\n\n---\nSent from Fused\n" + gh.PROPOSAL_MARKER

#: Proposal status from GitHub is re-read at most this often per app,
#: however many pages poll (contract §8: 60 s).
REFRESH_TTL_S = 60.0

#: Where another person's commits on Bob's proposal are fetched to (Update).
REMOTE_REF = "refs/fused/proposal-remote"

_TITLE_MAX = 200
_BODY_MAX = 20000
_NOTE_MAX = 2000

_DRAFT_DIFF_CHARS = 60000
_ASK_CHARS = 600
_ASKS_MAX = 12

_TRAILER_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*: .+$")
_CO_AUTHOR_RE = re.compile(r"^co-authored-by:\s*claude\s*<noreply@anthropic\.com>\s*$", re.I)

_refresh_lock = threading.Lock()
_last_refresh: dict[str, float] = {}


# ------------------------------------------------------------------ modules
#
# Imported lazily, by attribute on the package: engine imports git_upstream,
# which intercepts into engine, and tests replace any of these with
# monkeypatch.setattr on the module objects.

def _store():
    from fused_render.contrib import store
    return store


def _replay():
    from fused_render.contrib import replay
    return replay


def _flags():
    from fused_render.contrib import flags
    return flags


def _follow():
    from fused_render.contrib import follow
    return follow


def _gu():
    from fused_render import git_upstream
    return git_upstream


# ---------------------------------------------------------------- git bits


def _git(root, *args, timeout=None):
    gu = _gu()
    return gu._run(root, *args, timeout=timeout or gu.TIMEOUT_S)


def _ok(res) -> bool:
    return _gu()._ok(res)


def _out(res) -> str:
    return _gu()._out(res)


def _rev(root, spec) -> str | None:
    return _replay().rev(root, spec)


def _identity_cfg(ident: dict) -> list[str]:
    return ["-c", f"user.name={ident['name']}", "-c", f"user.email={ident['email']}",
            "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false"]


def _owner_name(record: dict) -> str:
    """The display name for the author (contract §13: the GitHub login)."""
    repo = (record or {}).get("repo") or ""
    return repo.split("/")[0] if "/" in repo else "the author"


def dirty(root) -> bool:
    """Uncommitted edits to tracked files, `.fused/` excluded (it is fused's
    own machine state, skip-worktree'd when committed)."""
    res = _git(root, "status", "--porcelain=v1", "--untracked-files=no", "--",
               ".", ":(exclude).fused")
    return _ok(res) and bool(_out(res))


def base_of(root, record) -> str | None:
    """The commit Bob's changes sit on: the engine's `refs/fused/base` when
    HEAD contains it, else where HEAD meets the author's latest."""
    replay = _replay()
    head = _rev(root, "HEAD")
    if not head:
        return None
    base = _rev(root, replay.BASE_REF)
    if base and replay.is_ancestor(root, base, head):
        return base
    tip = None
    try:
        follow = _follow()
        f = follow.for_record(record)
        tip = follow.tip_or_origin(root, f) if f is not None else None
    except Exception:  # noqa: BLE001 — no follow tip yet reads as "unknown"
        logger.debug("proposals: no follow tip for %s", root, exc_info=True)
    if not tip:
        return None
    mb = _out(_git(root, "merge-base", tip, head))
    return mb or None


def with_trailer(message: str) -> str:
    """`message` carrying exactly one Claude co-author trailer, at the end of
    its trailer block. Idempotent."""
    lines = [ln for ln in (message or "").rstrip().splitlines()
             if not _CO_AUTHOR_RE.match(ln.strip())]
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines).rstrip()
    paragraphs = text.split("\n\n")
    last = paragraphs[-1].splitlines() if text else []
    in_block = len(paragraphs) > 1 and last and all(_TRAILER_LINE_RE.match(ln) for ln in last)
    sep = "\n" if in_block else "\n\n"
    return (text + sep + CO_AUTHOR + "\n") if text else (CO_AUTHOR + "\n")


def commit_edits_locked(root, ident: dict, message: str = "Your edits") -> str | None:
    """Commit uncommitted tracked edits as one commit by `ident`; answers the
    new HEAD, or None when there was nothing to commit. Caller holds the
    slot (it writes the index and the branch)."""
    if not dirty(root):
        return None
    if not _ok(_git(root, "add", "-u", "--", ".", ":(exclude).fused")):
        raise PlainError("Your edits couldn't be saved for sending. Try again.")
    res = _git(root, *_identity_cfg(ident), "commit", "--no-verify", "-q", "-m", message)
    if not _ok(res):
        logger.warning("proposals: committing edits in %s failed: %s", root, _gu()._text(res))
        raise PlainError("Your edits couldn't be saved for sending. Try again.")
    return _rev(root, "HEAD")


_SEP = "\x1e"


def credit(root, base: str, head: str, ident: dict) -> str:
    """The tip of `base..head` re-created with Fused-authored commits credited
    to `ident` + Claude (module docstring). Writes objects only; moves no
    ref. Answers `head` itself when nothing needed changing."""
    fmt = "%H%x00%P%x00%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B" + "%x1e"
    res = _git(root, "log", "--reverse", "--topo-order", f"--format={fmt}", f"{base}..{head}")
    if not _ok(res):
        raise PlainError("Your changes couldn't be prepared for sending. Try again.")
    raw = res[1].decode("utf-8", "replace")
    commits = []
    for chunk in raw.split(_SEP):
        chunk = chunk.lstrip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x00", 9)
        if len(parts) != 10:
            continue
        commits.append(parts)
    if not commits:
        return head
    if any(len(c[1].split()) != 1 for c in commits):
        # A merge inside Bob's range never comes from the engine (it only
        # rebases); a hand-made one cannot be re-parented commit by commit.
        # Send the same content as ONE credited commit instead.
        tree = _out(_git(root, "rev-parse", f"{head}^{{tree}}"))
        return _commit_tree(root, tree, base, with_trailer("Your changes"),
                            author=(ident["name"], ident["email"], None),
                            committer=(ident["name"], ident["email"], None))
    parent_map = {}
    new = base
    for sha, parents, tree, an, ae, ad, cn, ce, cd, body in commits:
        old_parent = parents.split()[0]
        parent = parent_map.get(old_parent, old_parent)
        if ae.strip().lower() == FUSED_EMAIL:
            author = (ident["name"], ident["email"], ad)
            committer = (ident["name"], ident["email"], cd)
            message = with_trailer(body)
            changed = True
        else:
            author, committer, message = (an, ae, ad), (cn, ce, cd), body
            changed = parent != old_parent
        if not changed:
            new = sha
        else:
            new = _commit_tree(root, tree, parent, message, author=author, committer=committer)
        parent_map[sha] = new
    return new


def _commit_tree(root, tree, parent, message, *, author, committer) -> str:
    """`git commit-tree` with an explicit identity and dates (None = now)."""
    import subprocess

    env = {**os.environ, **_gu()._ENV,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    if author[2]:
        env["GIT_AUTHOR_DATE"] = author[2]
    if committer[2]:
        env["GIT_COMMITTER_DATE"] = committer[2]
    try:
        res = subprocess.run(
            [_gu()._git_bin(), "-C", root, "commit-tree", "--no-gpg-sign", tree,
             "-p", parent, "-F", "-"],
            input=message, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, close_fds=False, timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        res = None
    sha = (res.stdout or "").strip() if res is not None and res.returncode == 0 else ""
    if not re.match(r"^[0-9a-f]{40}$", sha):
        logger.warning("proposals: commit-tree failed in %s: %s", root,
                       (res.stderr if res is not None else "did not run"))
        raise PlainError("Your changes couldn't be prepared for sending. Try again.")
    return sha


def _pin_proposed_locked(root, sha: str) -> None:
    """Keep the proposed commit reachable (`refs/fused/proposed`): an
    auto-integrate rebases the branch past it, and the engine still asks
    questions about it afterwards (`is_ancestor`, the A4 return)."""
    ref = getattr(_replay(), "PROPOSED_REF", "refs/fused/proposed")
    _git(root, "update-ref", ref, sha)


def prepare(root, record, ident: dict) -> tuple[str, str]:
    """Make the folder ready to send: commit uncommitted edits, credit the
    range, land the credited tip. Answers `(tip, base)` — the exact commit to
    send and what it sits on. Retries once when a Claude turn commits in
    between (the land refuses a branch that moved)."""
    replay = _replay()
    gu = _gu()
    for _attempt in range(3):
        with replay.slot() as held:
            if not held:
                raise PlainError("Your app is busy updating. Try again in a moment.")
            if gu._operation_in_flight(root) is not None:
                raise PlainError("Your app is busy updating. Try again in a moment.")
            branch = gu._current_branch(root)
            if not branch:
                raise PlainError("This copy can't send proposals right now.")
            commit_edits_locked(root, ident)
            head = _rev(root, "HEAD")
        base = base_of(root, record)
        if not head or not base:
            raise PlainError(f"Couldn't find {_owner_name(record)}'s latest version. "
                             "Check your connection and try again.")
        if head == base:
            return head, base
        tip = credit(root, base, head, ident)
        if tip == head:
            return head, base
        if replay.land(root, branch=branch, old_tip=head, new_tip=tip, why="proposal-credit",
                       app_dir=root):
            return tip, base
    raise PlainError("Your app changed while sending. Try again.")


# ------------------------------------------------------------------ records


def _record(app_dir) -> tuple[str, dict]:
    store = _store()
    root = store.key(app_dir)
    record = store.get(root)
    if record is None:
        try:
            from fused_render.contrib import classify
            record = classify.ensure(root)
        except Exception:  # noqa: BLE001
            logger.exception("proposals: classify failed for %s", root)
            record = None
    if not isinstance(record, dict):
        raise PlainError("This app can't send proposals.")
    return root, record


def _contributor_record(app_dir) -> tuple[str, dict]:
    root, record = _record(app_dir)
    if record.get("kind") != "github" or not record.get("repo") or not record.get("branch"):
        raise PlainError("Proposals are for apps you installed from someone else.")
    return root, record


def _iso_to_epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_open(p: dict | None) -> bool:
    return bool(p) and (p.get("cache") or {}).get("state", "open") == "open"


def _branch_name(root) -> str:
    from fused_render import app_id
    slug = app_id.kebab(os.path.basename(root.rstrip("/")))[:40].strip("-") or "app"
    return f"fused-{slug}-{time.strftime('%Y%m%d-%H%M', time.gmtime())}"


def _body(description: str, note: str | None, login: str) -> str:
    body = (description or "").strip()
    if note and note.strip():
        body += f"\n\n**Note from {login}:** {note.strip()}"
    return body.strip() + FOOTER


def unproposed(root, record) -> int:
    """Edits Bob made since his open proposal was last sent — commits after
    `proposed_head`, plus one for uncommitted edits (Update sends those
    too). 0 without an open proposal."""
    p = (record or {}).get("proposal")
    if not _is_open(p):
        return 0
    n = 0
    proposed = p.get("proposed_head")
    head = _rev(root, "HEAD")
    if proposed and head and proposed != head:
        replay = _replay()
        if replay.is_ancestor(root, proposed, head):
            n = replay.count(root, f"{proposed}..{head}") or 0
        else:
            # Not carried by the engine (a hand-made reset, an old record):
            # count what HEAD has that the proposal lacks, patch-aware, minus
            # the author's own commits.
            base = base_of(root, record)
            rng = [f"{proposed}...{head}"] + ([f"^{base}"] if base else [])
            res = _git(root, "rev-list", "--count", "--right-only", "--cherry-pick", *rng)
            try:
                n = int(_out(res)) if _ok(res) else 0
            except ValueError:
                n = 0
    return n + (1 if dirty(root) else 0)


def view(app_dir) -> dict | None:
    """The page's `proposal` object (§12.1) from the cached record — no
    network. None when the app has no proposal on record."""
    try:
        store = _store()
        root = store.key(app_dir)
        record = store.get(root)
    except Exception:  # noqa: BLE001
        return None
    p = (record or {}).get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        return None
    cache = p.get("cache") or {}
    state = cache.get("state") or "open"
    if state == "merged":
        shown = "merged"
    elif state == "closed":
        shown = "declined"
    else:
        shown = "commented" if cache.get("comment") else "waiting"
    out = {"number": p["number"], "url": p.get("url") or "", "title": p.get("title") or "",
           "state": shown, "needs_merge": bool(record.get("needs_merge")),
           "unproposed": unproposed(root, record) if shown in ("waiting", "commented") else 0,
           "settled_seen": bool(p.get("settled_seen")),
           "can_update": record.get("kind") == "github"}
    if shown == "commented":
        out["comment"] = dict(cache["comment"])
    return out


# --------------------------------------------------------------- the draft


def _range_text(root, base: str) -> tuple[str, str, str]:
    log = _git(root, "log", "--reverse", "--format=%s%n%b", f"{base}..HEAD")
    stat = _git(root, "diff", "--stat", base, "--", ".", ":(exclude).fused")
    diff = _git(root, "diff", base, "--", ".", ":(exclude).fused")
    raw = diff[1].decode("utf-8", "replace") if _ok(diff) else ""
    # Strip Claude's per-turn subject prefix: the author reads the request,
    # not our bookkeeping.
    log_text = re.sub(r"(?m)^Claude: ", "", _out(log))
    return log_text, _out(stat), raw[:_DRAFT_DIFF_CHARS]


def _asks(root, since: float | None) -> list[str]:
    """What Bob asked Claude for in this app's recent chats (best-effort —
    the draft is still fine without them)."""
    try:
        from fused_render import claude_agent
        agent = claude_agent.agent_module()
        sessions = (agent._sessions(root) or {}).get("sessions") or []
    except Exception:  # noqa: BLE001
        logger.debug("proposals: no sessions for %s", root, exc_info=True)
        return []
    asks: list[str] = []
    for s in sessions[:4]:
        if since and (s.get("last_used") or 0) < since - 3600:
            continue
        try:
            turns = (agent._history(root, s["id"]) or {}).get("turns") or []
        except Exception:  # noqa: BLE001
            continue
        for t in turns:
            if t.get("role") == "user" and isinstance(t.get("text"), str):
                text = " ".join(t["text"].split())
                if text:
                    asks.append(text[:_ASK_CHARS])
    return asks[-_ASKS_MAX:]


def draft(app_dir) -> dict:
    """`{title, description}` for the Propose dialog, written by Claude from
    the change and Bob's own requests (contract §8). Never fails for a model
    problem — `ai.draft_proposal` falls back to a plain line."""
    root, record = _contributor_record(app_dir)
    base = base_of(root, record)
    if not base:
        raise PlainError(f"Couldn't find {_owner_name(record)}'s latest version. "
                         "Check your connection and try again.")
    log, stat, diff = _range_text(root, base)
    if not stat.strip():
        raise PlainError("There's nothing to send yet. Ask Claude for a change first.")
    first = _out(_git(root, "log", "--reverse", "--format=%ct", f"{base}..HEAD")).split()
    since = float(first[0]) if first else None
    me = gh.identity()
    from fused_render.contrib import ai
    return ai.draft_proposal(log=log, stat=stat, diff=diff, asks=_asks(root, since),
                             author=(me or {}).get("login") or "")


# --------------------------------------------------------------------- send


def _require_identity() -> dict:
    me = gh.identity(strict=True)
    if not me:
        raise gh.GitHubError("signed-out", "Sign in to GitHub to send your proposal.")
    return me


def send(app_dir, *, title: str, description: str, note: str | None = None) -> dict:
    """Send Bob's changes as a new proposal (contract §8 steps 1–6). Answers
    the page's proposal object."""
    title = " ".join((title or "").split())[:_TITLE_MAX]
    if not title:
        raise PlainError("Give your proposal a title.")
    description = (description or "")[:_BODY_MAX]
    note = (note or "")[:_NOTE_MAX]
    root, record = _contributor_record(app_dir)
    me = _require_identity()
    if _is_open(record.get("proposal")):
        refresh(root, force=True)
        root, record = _contributor_record(root)
        if _is_open(record.get("proposal")):
            raise PlainError("You already have a proposal waiting. "
                             "Use Update proposal to send your newer edits.")
    if record.get("needs_merge"):
        raise PlainError(f"{_owner_name(record)} changed the same parts you did. "
                         "Merge with Claude first, then send.")
    repo, base_branch = record["repo"], record["branch"]

    # Network first, before anything local moves: a refusal here (signed
    # out, no permission to copy, offline) leaves the folder untouched.
    fork = gh.ensure_fork(repo)
    fork_owner = gh.split_full(fork)[0]
    adopt = gh.find_open_pr(repo, fork_owner)
    if adopt and not (adopt.get("head_repo") or "").lower() == fork.lower():
        adopt = None

    tip, base = prepare(root, record, me)
    if tip == base:
        raise PlainError("There's nothing to send yet. Ask Claude for a change first.")

    if adopt:
        # The record was lost but GitHub still has Bob's open proposal from
        # this app: send to it rather than opening a second one. Commits
        # someone else added there are kept (same path as Update).
        head_branch = adopt["head_branch"]
        tip = _keep_their_edits(root, record, tip, pushed=None, remote=adopt.get("head_sha"),
                                fork=fork, head_branch=head_branch)
        _push(root, tip, fork, head_branch, lease=adopt.get("head_sha") or "")
        pr = adopt
    else:
        head_branch = _branch_name(root)
        # Bob's own fresh branch in his own copy: overwriting is safe (a
        # same-minute retry after a failed send reuses the name).
        try:
            gh.push(root, tip, fork, head_branch, force=True)
        except gh.GitHubError as exc:
            raise _plain_push_error(exc, record)
        try:
            pr = gh.open_pr(repo, title=title, body=_body(description, note, me["login"]),
                            head=f"{fork_owner}:{head_branch}", base=base_branch)
        except gh.GitHubError as exc:
            pr = gh.find_open_pr(repo, fork_owner, head_branch) if exc.reason == "conflict" else None
            if pr is None:
                if exc.reason == "conflict":
                    raise PlainError(f"{_owner_name(record)}'s app already has everything "
                                     "you'd send. There's nothing new to propose.")
                raise
    now = time.time()
    mb = _out(_git(root, "merge-base", base, tip)) or base
    proposal = {
        "repo": repo, "number": pr["number"], "url": pr.get("url") or "",
        "head_repo": fork, "head_branch": head_branch,
        "proposed_head": tip, "pushed_head": tip, "proposed_onto": mb,
        "title": pr.get("title") or title, "opened_at": now, "sent_at": now,
        "user": me["login"],
        "cache": {"state": "open", "merge_commit_sha": None, "comment": None,
                  "checked_at": now, "head_sha": tip, "error": None},
        "settled_seen": False, "returned_at": None,
    }

    def put(rec):
        rec["proposal"] = proposal

    with _replay().slot() as held:
        if held:
            _pin_proposed_locked(root, tip)
    _store().update(root, put)
    with _refresh_lock:
        _last_refresh[root] = time.monotonic()
    return view(root) or {}


def update(app_dir) -> dict:
    """Send Bob's newer edits to his open proposal (§8 Update): same
    preparation as Send, pushed to the proposal's own branch; no new
    proposal. Keeps commits someone else added there (module docstring)."""
    root, record = _record(app_dir)
    p = record.get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        raise PlainError("There's no proposal to update.")
    if record.get("kind") != "github":
        raise PlainError("Your remix is its own app now, so this proposal can't take "
                         "its newer edits.")
    me = _require_identity()
    if record.get("needs_merge"):
        raise PlainError(f"{_owner_name(record)} changed the same parts you did. "
                         "Merge with Claude first, then update.")
    pr = gh.get_pr(p["repo"], p["number"])
    _ingest(root, record, pr, None)
    if pr["state"] != "open":
        raise PlainError("This proposal is closed. Propose again to send your changes.")
    fork = pr.get("head_repo") or p.get("head_repo")
    head_branch = pr.get("head_branch") or p.get("head_branch")
    if not pr.get("head_repo"):
        raise PlainError("Your copy on GitHub is gone, so this proposal can't take new "
                         "edits. Withdraw it and propose again.")
    root, record = _contributor_record(root)
    tip, base = prepare(root, record, me)
    remote = pr.get("head_sha")
    pushed = p.get("pushed_head")
    if remote and remote != pushed:
        tip = _keep_their_edits(root, record, tip, pushed=pushed, remote=remote,
                                fork=fork, head_branch=head_branch)
    if tip == remote:
        return {**(view(root) or {}), "message": "Your proposal is already up to date."}
    _push(root, tip, fork, head_branch, lease=remote or "")
    now = time.time()
    mb = _out(_git(root, "merge-base", base, tip)) or base

    def put(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            q.update(proposed_head=tip, pushed_head=tip, proposed_onto=mb, sent_at=now)
            q.setdefault("cache", {})["head_sha"] = tip

    with _replay().slot() as held:
        if held:
            _pin_proposed_locked(root, tip)
    _store().update(root, put)
    return view(root) or {}


def _keep_their_edits(root, record, tip: str, *, pushed: str | None, remote: str | None,
                      fork: str, head_branch: str) -> str:
    """Bring commits someone else pushed onto Bob's proposal (`pushed..remote`)
    in on top of `tip`, landing the result in Bob's copy. Answers the new tip
    (== `tip` when there was nothing of theirs). Refuses plainly on overlap."""
    if not remote or remote == tip:
        return tip
    replay = _replay()
    if not gh.fetch(root, fork, f"+refs/heads/{head_branch}:{REMOTE_REF}"):
        raise PlainError("Couldn't reach GitHub to check your proposal. Try again.")
    fetched = _rev(root, REMOTE_REF)
    if fetched != remote:
        raise PlainError("Your proposal changed on GitHub while sending. Try again.")
    if replay.is_ancestor(root, remote, tip):
        return tip
    upstream = pushed if (pushed and _rev(root, pushed) and replay.is_ancestor(root, pushed, remote)) \
        else _out(_git(root, "merge-base", remote, tip))
    if not upstream:
        raise PlainError("Couldn't combine the proposal on GitHub with your copy. "
                         "Withdraw it and propose again.")
    res = replay.replay(root, onto=tip, upstream=upstream, branch=REMOTE_REF)
    who = _owner_name(record)
    if not res.get("ok") or not res.get("tip"):
        if res.get("conflict"):
            raise PlainError(
                f"{who} also edited this proposal on GitHub, in the same places as your "
                "newer edits. Your edits are safe here. Withdraw the proposal and send it "
                f"again, or ask {who} to accept it as it is.")
        raise PlainError("Couldn't combine the proposal on GitHub with your copy. Try again.")
    new = res["tip"]
    if new == tip:
        return tip
    gu = _gu()
    branch = gu._current_branch(root)
    if not branch or not replay.land(root, branch=branch, old_tip=tip, new_tip=new,
                                     why="proposal-their-edits", app_dir=root):
        raise PlainError("Your app changed while sending. Try again.")
    return new


def _push(root, tip, fork, head_branch, *, lease: str) -> None:
    try:
        gh.push(root, tip, fork, head_branch, lease=lease)
    except gh.GitHubError as exc:
        if exc.reason == "conflict":
            raise PlainError("Your proposal changed on GitHub while sending. Try again.")
        raise


def _plain_push_error(exc: gh.GitHubError, record: dict) -> Exception:
    if exc.reason == "forbidden":
        return gh.GitHubError("forbidden", "GitHub didn't let your account save a copy of "
                                           f"{_owner_name(record)}'s app.")
    return exc


def withdraw(app_dir) -> dict:
    """Close Bob's open proposal on GitHub (with a short note to the author)
    and forget it locally. Never touches Bob's changes."""
    store = _store()
    root = store.key(app_dir)
    record = store.get(root) or {}
    p = record.get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        return {"ok": True}
    if _is_open(p):
        me = gh.identity() or {}
        who = me.get("login") or p.get("user") or "The sender"
        try:
            gh.close_pr(p["repo"], p["number"], comment=f"{who} withdrew this proposal.")
        except gh.GitHubError as exc:
            if exc.reason != "not-found":
                raise

    def clear(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            rec["proposal"] = None

    store.update(root, clear)
    return {"ok": True}


# ------------------------------------------------------------------ refresh


def _newest_comment(comments: list, bob: str | None, since: float | None) -> dict | None:
    """The newest comment by someone other than Bob (and not a bot) posted
    after the last send — "alice commented"."""
    best = None
    for c in comments or []:
        who = c.get("author") or ""
        if not who or gh.same_login(who, bob) or who.lower().endswith("[bot]"):
            continue
        if not (c.get("body") or "").strip():
            continue
        at = _iso_to_epoch(c.get("at"))
        if since is not None and at is not None and at <= since:
            continue
        if best is None or (c.get("at") or "") >= (best.get("at") or ""):
            best = c
    if best is None:
        return None
    return {"author": best["author"], "body": best["body"][:4000], "at": best.get("at") or ""}


def _apply_pr(root, pr: dict, comments: list | None) -> tuple[dict | None, str | None]:
    """Write GitHub's answer into the record's cache. Answers `(proposal,
    previous state)`."""
    state = "merged" if pr.get("merged") else ("closed" if pr.get("state") == "closed" else "open")
    holder = {}

    def fn(rec):
        q = rec.get("proposal")
        if not isinstance(q, dict) or q.get("number") != pr.get("number"):
            return
        cache = q.setdefault("cache", {})
        holder["prev"] = cache.get("state", "open")
        cache.update(state=state, merge_commit_sha=pr.get("merge_commit_sha") if pr.get("merged") else None,
                     checked_at=time.time(), head_sha=pr.get("head_sha"), error=None)
        if comments is not None:
            cache["comment"] = _newest_comment(comments, pr.get("user") or q.get("user"),
                                               q.get("sent_at") or q.get("opened_at"))
        if state == "open":
            cache.setdefault("comment", None)
        if pr.get("title"):
            q["title"] = pr["title"]
        if pr.get("url"):
            q["url"] = pr["url"]
        if holder["prev"] != state and state != "open":
            q["settled_seen"] = False
        holder["p"] = dict(q)

    _store().update(root, fn)
    return holder.get("p"), holder.get("prev")


def _ingest(root, record, pr: dict, comments: list | None) -> None:
    """`_apply_pr`, plus the one-time "declined" event on the open → closed
    transition (a reopened-then-closed proposal says it again, as it should)."""
    q, prev = _apply_pr(root, pr, comments)
    if q is not None and prev == "open" and q["cache"]["state"] == "closed":
        try:
            _store().add_event(root, "declined", who=_owner_name(record))
        except Exception:  # noqa: BLE001
            logger.exception("proposals: declined event failed for %s", root)


def refresh(app_dir, *, force: bool = False) -> dict | None:
    """Re-read the proposal from GitHub (throttled to one read per
    REFRESH_TTL_S per app unless `force`) and act on what changed: closed
    without merging → declined (Bob's changes untouched); merged and the
    author's latest carries it → return to following (A4, `flags.loop()`).
    Answers the page's proposal object. Never raises for a GitHub problem —
    the cached state stands until the next read."""
    store = _store()
    root = store.key(app_dir)
    record = store.get(root)
    p = (record or {}).get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        return None
    now = time.monotonic()
    with _refresh_lock:
        last = _last_refresh.get(root)
        if not force and last is not None and now - last < REFRESH_TTL_S:
            due = False
        else:
            due = True
            _last_refresh[root] = now
    if due:
        try:
            pr = gh.get_pr(p["repo"], p["number"])
            comments = gh.pr_comments(p["repo"], p["number"]) if pr["state"] == "open" else None
        except gh.GitHubError as exc:
            reason = exc.reason

            def mark(rec):
                q = rec.get("proposal")
                if isinstance(q, dict) and q.get("number") == p["number"]:
                    q.setdefault("cache", {})["error"] = reason

            store.update(root, mark)
            return view(root)
        _ingest(root, record, pr, comments)
        record = store.get(root) or record
    p = (record or {}).get("proposal") or {}
    if ((p.get("cache") or {}).get("state") == "merged" and not p.get("returned_at")
            and record.get("kind") == "github"):
        _maybe_return(root, record)
    return view(root)


def _maybe_return(root, record) -> None:
    """A4: once the author's latest (after a fetch) contains the merge, hand
    the copy back to following. `engine.return_to_following` keeps Bob's
    unproposed edits and records `returned_at`."""
    try:
        if not _flags().loop():
            return
        p = record["proposal"]
        merged = (p.get("cache") or {}).get("merge_commit_sha")
        if not merged:
            return
        follow = _follow()
        f = follow.for_record(record)
        if f is None:
            return
        replay = _replay()
        tip = None
        # The follow ref is the engine's too, so this fetch holds the slot.
        with replay.slot(block=False) as held:
            if held:
                tip = follow.fetch(root, f)
        tip = tip or follow.tip(root, f)
        if not tip or not _rev(root, merged) or not replay.is_ancestor(root, merged, tip):
            return
        from fused_render.contrib import engine
        engine.return_to_following(root)
    except Exception:  # noqa: BLE001 — the next refresh or sync tries again
        logger.exception("proposals: returning %s to following failed", root)


def acknowledge(app_dir) -> None:
    """The page showed the Merged/Declined banner."""
    _store().ack(app_dir, "settled")


# ------------------------------------------------------------ job wrappers


def _refusal(reason: str, message: str) -> dict:
    return {"ok": False, "reason": reason, "message": message}


def preflight(app_dir, *, needs_identity: bool = True) -> dict | None:
    """The synchronous checks a route makes before starting a job: phase on,
    a GitHub-link copy, signed in. None = go ahead; else the refusal."""
    if not _flags().changes():
        return _refusal("disabled", "This isn't switched on yet.")
    try:
        _contributor_record(app_dir)
    except PlainError as exc:
        return _refusal("not-allowed", exc.message)
    if needs_identity:
        try:
            if not gh.identity(strict=True):
                return _refusal("signed-out", "Sign in to GitHub to send your proposal.")
        except gh.GitHubError as exc:
            return _refusal(exc.reason, exc.message)
    return None


def _start(kind: str, app_dir, fn, *, needs_identity: bool = True) -> dict:
    refusal = preflight(app_dir, needs_identity=needs_identity)
    if refusal:
        return refusal
    from fused_render.contrib import jobs
    root = _store().key(app_dir)
    return {"ok": True, "job": jobs.start(kind, root, fn)}


def start_draft(app_dir) -> dict:
    return _start("propose-draft", app_dir, lambda: draft(app_dir), needs_identity=False)


def start_send(app_dir, *, title: str, description: str, note: str | None = None) -> dict:
    return _start("propose-send", app_dir,
                  lambda: send(app_dir, title=title, description=description, note=note))


def start_update(app_dir) -> dict:
    return _start("propose-update", app_dir, lambda: update(app_dir))
