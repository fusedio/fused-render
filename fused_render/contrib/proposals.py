"""Proposals (contract §8, v2 §16.5, v3 §17.5): Bob's changes to Alice's
app, sent to her for review as a pull request from his own copy of the repo
on GitHub, in his name — plus Update, Withdraw, the status refresh that
follows the proposal on GitHub, and `settle_direct` for a collaborator
publishing straight to the author's app.

GITHUB IS THE TRUTH (§0.4). The record's `proposal` is the proposal's
IDENTITY on GitHub (repo + number) and a cache of what GitHub said last.
`refresh` re-derives every state from GitHub; nothing here infers "merged"
or "declined" from local history. A record lost entirely (a reinstall, a
store reset) is recovered from GitHub too: Send adopts the open proposal
from Bob's account that carries fused's footer marker instead of opening a
second one.

ONE BRANCH OF BOB'S OWN, NO REWRITE EVER (§17). A `github` copy's HEAD is
`fused/mine`; `refs/heads/<b>` is a fetch-only mirror of the author's
branch. Send pushes `fused/mine` as it is to a NEW branch on Bob's copy on
GitHub — no commit is re-created, so every id Bob sees locally is the one
GitHub shows. The author's latest reaches `fused/mine` only by merge (the
engine's bring-in), so what was sent stays an ancestor of HEAD and an
Update is always a plain, non-force push. "Nothing new to send" is a count
(`unproposed`): Bob's own first-parent, non-merge commits after the
proposal's head — a bring-in merge, and the author's commits reached through
one, never count as his.

WHAT IS SENT IS WHAT BOB RUNS, AS HE CHOSE (D16, §16.5). Send/Update commit
the uncommitted edits to tracked files first, as one "Your edits" commit.
New files git does not track go only when Bob ticks them in the dialog
(`include`, offered by `untracked`) — a scratch file or a local secret
never leaves by default.

SOMEONE ELSE EDITED THE PROPOSAL. Proposals are opened with maintainer edits
allowed, so the head on GitHub can move without Bob: Alice fixing a typo on
github.com, GitHub's "Update branch", the author's Merge with Claude
(`inbox.merge_start`). Update fetches that head; when Bob's copy lacks it,
it is MERGED into his copy (clean → landed; overlap → Merge with Claude,
purpose "proposal", after which `merge_accepted` sends) — never replayed,
so Alice's commits keep their ids and nothing of hers becomes "his change".
The merge is built in memory (`replay.merge_tree` + `commit-tree`) and
landed by fast-forward (`replay.land_ff_locked`).

CLAUDE BUSY (§16.0 P8). Send, Update, Withdraw, Remix and settle_direct
refuse with one plain sentence while a Claude turn is open in the folder:
each may move the tree, and a turn's own commits do not take the slot.

ONE OPEN PROPOSAL PER APP. Send refuses while one is open (that's Update);
after it is merged or declined, a new Send replaces the settled record.

Long operations (draft, send, update) are synchronous functions here that
raise `jobs.PlainError` (or `gh.GitHubError`, a subclass carrying `reason`);
the `start_*` helpers preflight on the request thread and wrap them in
`jobs.start`, answering `{ok, job}` / `{ok: False, reason, message}`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime

from fused_render.contrib import gh
from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

FOOTER = "\n\n---\nSent from Fused\n" + gh.PROPOSAL_MARKER

#: Proposal status from GitHub is re-read at most this often per app,
#: however many pages poll (contract §8: 60 s).
REFRESH_TTL_S = 60.0

#: The proposal's head as this copy last saw it on GitHub: written by Send
#: and Update after their push, and by Update's fetch of edits made there;
#: deleted when the proposal ends (§17.5). `unproposed`/`head_differs`
#: count from it.
PR_HEAD_REF = "refs/fused/pr-head"


CLAUDE_BUSY = "Claude is still working on this app. Try again when it's done."
_MOVED = "Something changed on GitHub in the meantime. Try again."
_BUSY = "Your app is busy updating. Try again in a moment."

_TITLE_MAX = 200
_BODY_MAX = 20000
_NOTE_MAX = 2000

_DRAFT_DIFF_CHARS = 60000
_ASK_CHARS = 600
_ASKS_MAX = 12

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


def home_branch() -> str:
    """HEAD's branch on a `github` copy (§17.1 I4): `classify.HOME_BRANCH`,
    the one constant (the guard module's), read lazily."""
    from fused_render.contrib import classify
    return classify.HOME_BRANCH


# ---------------------------------------------------------------- git bits


def _git(root, *args, timeout=None):
    """Every git call here runs with no repo-configured hook or fsmonitor
    command (`git_upstream.SAFE_CONFIG`, §16.12): the folder is
    Claude-writable, and these are fused's own operations."""
    gu = _gu()
    return gu._run(root, *gu.SAFE_CONFIG, *args, timeout=timeout or gu.TIMEOUT_S)


def _ok(res) -> bool:
    return _gu()._ok(res)


def _out(res) -> str:
    return _gu()._out(res)


def _rev(root, spec) -> str | None:
    return _replay().rev(root, spec)


def _identity_cfg(ident: dict) -> list[str]:
    return ["-c", f"user.name={ident['name']}", "-c", f"user.email={ident['email']}",
            "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false"]


def commit_identity(root, ident: dict) -> dict:
    """Who commits as Bob here: the copy's repo-local `user.name`/`user.email`
    when both are set (§16.1: classification writes his GitHub identity
    there — one identity for app_git, Claude's turns and us), else `ident`
    (the signed-in account)."""
    name = _out(_git(root, "config", "--local", "--get", "user.name"))
    email = _out(_git(root, "config", "--local", "--get", "user.email"))
    return {**ident, "name": name, "email": email} if name and email else ident


def _owner_name(record: dict) -> str:
    """The display name for the author (contract §13: the GitHub login)."""
    repo = (record or {}).get("repo") or ""
    return repo.split("/")[0] if "/" in repo else "the author"


def _activity(root, kind: str, text: str, *, backup=None) -> None:
    """One Activity line per move (§16.9); never fails the move itself."""
    try:
        _store().add_activity(root, kind, text, backup=backup)
    except Exception:  # noqa: BLE001
        logger.exception("proposals: activity line failed for %s", root)


def _same_tree(root, a, b) -> bool:
    replay = _replay()
    ta, tb = replay.tree_of(root, a), replay.tree_of(root, b)
    return bool(ta) and ta == tb


def tip_of(root, record, *, fetch: bool = False) -> str | None:
    """The author's latest as this copy knows it (`refs/remotes/origin/<b>`,
    §16.0 P3) — fetched by the record's URL first when `fetch`. None when
    there is none at all."""
    follow = _follow()
    f = follow.for_record(record)
    if f is None:
        return None
    if fetch:
        try:
            got = follow.fetch(root, f)
        except Exception:  # noqa: BLE001 — offline reads the last fetch
            logger.debug("proposals: fetch failed for %s", root, exc_info=True)
            got = None
        if isinstance(got, tuple):
            got = got[0]
        if got:
            return got
    try:
        t = follow.tip(root, f)
    except Exception:  # noqa: BLE001
        t = None
    return t or _rev(root, f"refs/remotes/origin/{f.branch}")


# ------------------------------------------------------------------- busy


def claude_busy(root) -> bool:
    """A Claude turn is open in this folder right now (§16.0 P8) — the
    engine's one rule (`engine.claude_busy`: the transcript's last
    message). Unreadable reads as not busy."""
    try:
        from fused_render.contrib import engine
        return bool(engine.claude_busy(root))
    except Exception:  # noqa: BLE001
        logger.debug("proposals: claude busy check failed for %s", root, exc_info=True)
        return False


def _gate(root) -> None:
    if claude_busy(root):
        raise PlainError(CLAUDE_BUSY, reason="claude")


# ------------------------------------------------------------------ edits


def tracked_edits(root) -> list[str]:
    """Uncommitted edits to TRACKED files (staged or not, deletions and both
    sides of a rename included), minus fused's own state — what Send and
    Update always commit."""
    replay = _replay()
    res = _git(root, "diff", "--no-renames", "--name-only", "-z", "HEAD", "--",
               *replay.EDITS_PATHSPEC)
    if not _ok(res):
        return []
    return [p for p in res[1].decode("utf-8", "replace").split("\0") if p]


def untracked(app_dir) -> list[str]:
    """New files git does not track or ignore (app-relative paths), minus
    fused's own state — the Propose dialog lists them, unticked. No network."""
    root = _store().key(app_dir)
    return sorted(p for code, p in _replay().edits(root) if code == "??")


def dirty(root) -> bool:
    """Tracked edits Send/Update would commit (untracked files are opt-in)."""
    return bool(tracked_edits(root))


def _check_include(root, include) -> list[str]:
    """The untracked files Bob ticked, each one checked against what is
    really untracked here — the list comes from the page."""
    if not include:
        return []
    if not isinstance(include, (list, tuple)):
        raise PlainError("Pick the new files to send again.")
    allowed = set(untracked(root))
    picked = []
    for rel in include:
        norm = os.path.normpath(rel).replace(os.sep, "/") if isinstance(rel, str) else ""
        if not norm or norm not in allowed:
            raise PlainError("One of the new files you picked has changed. Open Propose again.")
        if norm not in picked:
            picked.append(norm)
    return picked


def commit_edits_locked(root, ident: dict, include=(), message: str = "Your edits") -> str | None:
    """Commit the tracked edits plus the untracked files in `include` as one
    commit by `ident`; answers the new HEAD, or None when there was nothing
    to commit. Only those paths (`commit --only`): anything else Bob staged
    by hand stays where it is. Caller holds the slot."""
    paths = list(dict.fromkeys(tracked_edits(root) + list(include or ())))
    if not paths:
        return None
    if not _ok(_git(root, "--literal-pathspecs", "add", "-A", "--", *paths)):
        raise PlainError("Your edits couldn't be saved for sending. Try again.")
    res = _git(root, "--literal-pathspecs", *_identity_cfg(ident), "commit", "--only",
               "--no-verify", "-q", "-m", message, "--", *paths)
    if not _ok(res):
        logger.warning("proposals: committing edits in %s failed: %s", root, _gu()._text(res))
        raise PlainError("Your edits couldn't be saved for sending. Try again.")
    return _rev(root, "HEAD")


# ------------------------------------------------------------- commit-tree


def _commit_tree(root, tree, parents, message, *, author, committer) -> str:
    """`git commit-tree` with an explicit identity and dates (None = now);
    `parents` is one sha or a list (a merge)."""
    parent_args = []
    for p in ([parents] if isinstance(parents, str) else parents):
        parent_args += ["-p", p]
    import subprocess

    env = {**os.environ, **_gu()._ENV,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
    for key, value in (("GIT_AUTHOR_DATE", author[2] if len(author) > 2 else None),
                       ("GIT_COMMITTER_DATE", committer[2] if len(committer) > 2 else None)):
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    try:
        res = subprocess.run(
            [_gu()._git_bin(), "-C", root, *_gu().SAFE_CONFIG, "commit-tree",
             "--no-gpg-sign", tree,
             *parent_args, "-F", "-"],
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


def _prepare(root, record, ident: dict, *, include=(), commit: bool = True) -> str:
    """Under the slot: commit the edits that go (`commit`: the tracked
    edits plus the ticked new files in `include`, as one "Your edits"
    commit in the copy's identity) on HEAD, and answer the HEAD to send.
    Nothing is re-created (§17.7). A `github` copy sends only from
    `fused/mine` (§17.1 I4): on any other branch ("a different version")
    it refuses. Send, Update and Remix publish all go through here."""
    gu = _gu()
    ident = commit_identity(root, ident)
    with _replay().slot() as held:
        if not held or gu._operation_in_flight(root) is not None:
            raise PlainError(_BUSY)
        branch = gu._current_branch(root)
        if not branch:
            raise PlainError("This copy can't send proposals right now.")
        if (record or {}).get("kind") == "github" and branch != home_branch():
            raise PlainError("You're looking at a different version of this app. "
                             "Go back to your usual version first, then send.")
        _gate(root)
        if commit:
            commit_edits_locked(root, ident, include)
        head = _rev(root, "HEAD")
    if not head:
        raise PlainError("This copy can't send proposals right now.")
    return head


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


def _settled(record, p: dict) -> bool:
    """Nothing left to learn from GitHub in the background: published
    directly; merged and landed (the engine's once-stamp `landed_at`,
    §17.2) — or merged on a remix, which never lands; closed unmerged once
    its closing note was read."""
    state = (p.get("cache") or {}).get("state")
    if p.get("direct"):
        return True
    if state == "merged":
        return bool(p.get("landed_at")) or (record or {}).get("kind") != "github"
    if state == "closed":
        return bool((p.get("cache") or {}).get("note_checked"))
    return False


def _slug(root) -> str:
    from fused_render import app_id
    return app_id.kebab(os.path.basename(root.rstrip("/")))[:40].strip("-") or "app"


def _branch_name(root) -> str:
    """`fused-<slug>-<yyyymmdd-hhmm>-<4 hex>`: the random tail keeps two
    copies with the same folder name sending in the same minute from ever
    sharing one."""
    import secrets
    return (f"fused-{_slug(root)}-{time.strftime('%Y%m%d-%H%M', time.gmtime())}"
            f"-{secrets.token_hex(2)}")


def _adoptable(root, record, repo: str, fork: str) -> dict | None:
    """The open proposal on GitHub that THIS copy may send to instead of
    opening a new one — never one that belongs to another copy of the same
    app on this machine (two folders, one GitHub account, one fork):
      * the record still names a head branch (its proposal settled, then was
        reopened on GitHub): exactly that branch's open proposal;
      * no proposal on record (lost): the newest open proposal from Bob's
        copy carrying fused's marker, sent from a folder of this name, that
        no OTHER record on this machine claims (same repo + number)."""
    p = record.get("proposal") if isinstance(record.get("proposal"), dict) else None
    owner = gh.split_full(fork)[0]
    if p and p.get("head_branch"):
        pr = gh.find_open_pr(repo, owner, p["head_branch"])
        return pr if pr and (pr.get("head_repo") or "").lower() == fork.lower() else None
    claimed = set()
    for key, rec in (_store().all() or {}).items():
        q = rec.get("proposal") if isinstance(rec, dict) else None
        if key != root and isinstance(q, dict) and (q.get("repo") or "").lower() == repo.lower():
            claimed.add(q.get("number"))
    prefix = f"fused-{_slug(root)}-"
    for pr in gh.list_open_prs(repo):          # newest first
        if (gh.same_login(pr.get("user"), owner)
                and (pr.get("head_repo") or "").lower() == fork.lower()
                and gh.PROPOSAL_MARKER in (pr.get("body") or "")
                and (pr.get("head_branch") or "").startswith(prefix)
                and pr.get("number") not in claimed):
            return pr
    return None


def _body(description: str, note: str | None, login: str) -> str:
    body = (description or "").strip()
    if note and note.strip():
        body += f"\n\n**Note from {login}:** {note.strip()}"
    return body.strip() + FOOTER


def mirror_of(root, record) -> str | None:
    """`refs/heads/<b>`: the author's latest as this copy holds it (§17.1
    I3 — equal to `origin/<b>` after every sync)."""
    branch = (record or {}).get("branch")
    return _rev(root, f"refs/heads/{branch}") if branch else None


def pr_head(root, record) -> str | None:
    """The proposal's head as this copy knows it (§17.5): `PR_HEAD_REF`
    when present, else the cache's `head_sha` when it is a local object,
    else the author's latest (`refs/heads/<b>`). No network."""
    p = (record or {}).get("proposal")
    got = _rev(root, PR_HEAD_REF)
    if got:
        return got
    cached = ((p or {}).get("cache") or {}).get("head_sha") if isinstance(p, dict) else None
    got = _rev(root, cached) if cached else None
    return got or mirror_of(root, record)


def unproposed(root, record) -> int:
    """Bob's commits an Update would add to his open proposal (§17.5):
    `rev-list --count --first-parent --no-merges <pr-head>..HEAD` — first
    parent and no merges, so a bring-in merge and the author's commits
    reached through it never count as his — +1 for uncommitted tracked
    edits (Update sends those too). 0 without an open proposal. No
    network."""
    p = (record or {}).get("proposal")
    if not _is_open(p):
        return 0
    base = pr_head(root, record)
    res = _git(root, "rev-list", "--count", "--first-parent", "--no-merges", "HEAD",
               *([f"^{base}"] if base else []))
    try:
        n = int(_out(res)) if _ok(res) else 0
    except ValueError:
        n = 0
    return n + (1 if dirty(root) else 0)


def head_differs(root, record) -> bool:
    """Update has something to do beyond what GitHub already has (§17.5):
    the proposal's head is NOT in HEAD (edits made to it on GitHub —
    maintainer edits, GitHub's "Update branch" — that Bob's copy lacks), or
    Bob has commits or edits after it (`unproposed > 0`). A bring-in merge
    of the author's latest alone never counts: the author has her own
    version already, and GitHub's `mergeable` (`fits`) says when the
    proposal stopped fitting it. False without an open proposal or when
    the head is unknown here. No network.

    "The proposal's head" is both what this copy last pushed or fetched
    (`PR_HEAD_REF`) and what GitHub last said (`cache.head_sha`): a cached
    head that is not even a local object is one HEAD lacks. Never the
    `main` mirror — the author's latest is not the proposal's."""
    p = (record or {}).get("proposal")
    if not _is_open(p):
        return False
    head = _rev(root, "HEAD")
    if not head:
        return False
    replay = _replay()
    cached = (p.get("cache") or {}).get("head_sha")
    for known in (_rev(root, PR_HEAD_REF), cached):
        if known and not (_rev(root, known) and replay.is_ancestor(root, known, head)):
            return True
    return unproposed(root, record) > 0


def conflict(root) -> bool:
    """The author's latest overlaps what Bob runs — the engine's one
    derived fact (`engine.conflict`, §17.2 `needs-merge`), so Send/Update
    refuse exactly when the page shows Merge with Claude. It merges on the
    merged proposal's head when there is one (round 3c): after a squash
    accept, the author's later edits of Bob's own lines are no overlap.
    Off `fused/mine` it is False: there Send/Update refuse with the
    "different version" sentence (`_prepare`), never pointing at a Merge
    with Claude the page does not offer. No network."""
    from fused_render.contrib import engine
    if _gu()._current_branch(root) != home_branch():
        return False
    got = engine.conflict(root)
    return bool(got and got.get("files"))


def _set_pr_head(root, sha: str | None) -> None:
    """Point `PR_HEAD_REF` at what GitHub now has (None deletes it)."""
    try:
        if sha:
            _git(root, "update-ref", PR_HEAD_REF, sha)
        else:
            _git(root, "update-ref", "-d", PR_HEAD_REF)
    except Exception:  # noqa: BLE001 — a cache; the next Update refetches
        logger.debug("proposals: %s not written in %s", PR_HEAD_REF, root, exc_info=True)


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
    waiting = shown in ("waiting", "commented")
    out = {"number": p["number"], "url": p.get("url") or "", "title": p.get("title") or "",
           "state": shown,
           # v2's recorded overlap, kept as a key for one release; nothing
           # reads it — v3 derives the overlap in `engine.status` (§17.2),
           # and Send/Update ask `conflict` themselves.
           "needs_merge": False,
           "unproposed": unproposed(root, record) if waiting else 0,
           "settled_seen": bool(p.get("settled_seen")),
           "direct": bool(p.get("direct")),
           # "<who> asked you to update your proposal" (inbox.ask_update).
           "asked_update": waiting and bool(cache.get("asked")),
           # After a remix (kind "own") the copy carries the credit line and
           # a new app id, which must never be sent into the author's app:
           # still followed, still withdrawable, never updated.
           "can_update": waiting and record.get("kind") == "github",
           "can_withdraw": waiting,
           # GitHub's own "does it still merge" (None = not known yet), and
           # whether HEAD lacks edits made on GitHub or holds new ones of
           # Bob's — either offers Update (§16.12, §17.5).
           "fits": cache.get("mergeable") if waiting else None,
           "head_differs": head_differs(root, record) if waiting else False}
    if shown in ("commented", "declined") and cache.get("comment"):
        # Declined: the author's closing note, if she left one.
        out["comment"] = dict(cache["comment"])
    if out["asked_update"]:
        out["asked"] = dict(cache["asked"])
    return out


# --------------------------------------------------------------- the draft


def _range_text(root, tip: str) -> tuple[str, str, str]:
    """Bob's commit messages, and the diff of what he runs (tracked edits
    included) against the author's latest — exactly what the proposal will
    show her once his copy has her latest."""
    log = _git(root, "log", "--reverse", "--no-merges", "--format=%s%n%b", "HEAD", f"^{tip}")
    stat = _git(root, "diff", "--stat", tip, "--", ".", ":(exclude).fused")
    diff = _git(root, "diff", tip, "--", ".", ":(exclude).fused")
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


def _no_tip(record) -> PlainError:
    return PlainError(f"Couldn't find {_owner_name(record)}'s latest version. "
                      "Check your connection and try again.")


def draft(app_dir) -> dict:
    """`{title, description}` for the Propose dialog, written by Claude from
    the change and Bob's own requests (contract §8). Never fails for a model
    problem — `ai.draft_proposal` falls back to a plain line."""
    root, record = _contributor_record(app_dir)
    tip = tip_of(root, record)
    if not tip:
        raise _no_tip(record)
    log, stat, diff = _range_text(root, tip)
    if not stat.strip() and not untracked(root):
        raise PlainError("There's nothing to send yet. Ask Claude for a change first.")
    first = _out(_git(root, "log", "--reverse", "--no-merges", "--format=%ct", "HEAD",
                      f"^{tip}")).split()
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


def _say(step, key: str) -> None:
    """Report progress to the job (`jobs.set_step`) when a caller wired one."""
    if step is not None:
        try:
            step(key)
        except Exception:  # noqa: BLE001 — progress is decoration
            logger.debug("proposals: step %s not reported", key, exc_info=True)


def _nothing_to_send(root, tip: str, include) -> bool:
    """Bob's copy holds nothing the author's latest lacks."""
    if include or dirty(root):
        return False
    head = _rev(root, "HEAD")
    return not head or _same_tree(root, head, tip) or _replay().is_ancestor(root, head, tip)


def send(app_dir, *, title: str, description: str, note: str | None = None,
         include=None, step=None) -> dict:
    """Send Bob's changes as a new proposal (§16.5). Answers the page's
    proposal object (`number`, `url`, …). `step(key)` hears "copy" → "send"
    → "open" as the job moves on."""
    title = " ".join((title or "").split())[:_TITLE_MAX]
    if not title:
        raise PlainError("Give your proposal a title.")
    description = (description or "")[:_BODY_MAX]
    note = (note or "")[:_NOTE_MAX]
    root, record = _contributor_record(app_dir)
    _gate(root)
    me = _require_identity()
    if _is_open(record.get("proposal")):
        refresh(root, force=True)
        root, record = _contributor_record(root)
        if _is_open(record.get("proposal")):
            raise PlainError("You already have a proposal waiting. "
                             "Use Update proposal to send your newer edits.")
    if conflict(root):
        raise PlainError(f"{_owner_name(record)} changed the same parts you did. "
                         "Merge with Claude first, then send.")
    include = _check_include(root, include)
    repo, base_branch = record["repo"], record["branch"]
    tip = tip_of(root, record, fetch=True)
    if not tip:
        raise _no_tip(record)
    if _nothing_to_send(root, tip, include):
        raise PlainError("There's nothing to send yet. Ask Claude for a change first.")

    # Network first, before anything local moves: a refusal here (signed
    # out, no permission to copy, offline) leaves the folder untouched.
    _say(step, "copy")
    fork = gh.ensure_fork(repo)
    fork_owner = gh.split_full(fork)[0]
    adopt = _adoptable(root, record, repo, fork)

    _say(step, "send")
    head = _prepare(root, record, me, include=include)
    if _same_tree(root, head, tip):
        raise PlainError("There's nothing to send yet. Ask Claude for a change first.")

    if adopt:
        # GitHub still has this copy's open proposal (record lost, or a
        # settled one reopened): send to it rather than opening a second
        # one. Whatever someone else added there is merged in first.
        head_branch = adopt["head_branch"]
        head = _take_in_pr_head(root, record, me, fork, head_branch, adopt.get("head_sha"))
        if head != adopt.get("head_sha"):
            _push(root, head, fork, head_branch)
        _say(step, "open")
        pr = adopt
    else:
        head_branch = _branch_name(root)
        try:
            gh.push(root, head, fork, head_branch)
        except gh.GitHubError as exc:
            raise _plain_push_error(exc, record)
        _say(step, "open")
        try:
            pr = gh.open_pr(repo, title=title, body=_body(description, note, me["login"]),
                            head=f"{fork_owner}:{head_branch}", base=base_branch)
        except gh.GitHubError as exc:
            pr = gh.find_open_pr(repo, fork_owner, head_branch) if exc.reason == "conflict" else None
            if pr is None:
                _delete_branch(fork, head_branch, repo)
                if exc.reason == "conflict":
                    raise PlainError(f"{_owner_name(record)}'s app already has everything "
                                     "you'd send. There's nothing new to propose.")
                raise
    now = time.time()
    proposal = {
        "repo": repo, "number": pr["number"], "url": pr.get("url") or "",
        "head_repo": fork, "head_branch": head_branch,
        "title": pr.get("title") or title, "opened_at": now, "sent_at": now,
        "user": me["login"],
        "cache": {"state": "open", "merge_commit_sha": None, "comment": None, "asked": None,
                  "checked_at": now, "head_sha": head, "error": None},
        "settled_seen": False, "landed_at": None,
    }
    _set_pr_head(root, head)

    def put(rec):
        rec["proposal"] = proposal

    _store().update(root, put)
    with _refresh_lock:
        _last_refresh[root] = time.monotonic()
    _activity(root, "proposal-sent",
              f"Sent your proposal to {_owner_name(record)}: {proposal['title']}")
    return view(root) or {}


class MergeStarted(PlainError):
    """Update stopped on an overlap with edits made to the proposal on
    GitHub; `job` is the Merge with Claude job combining them."""

    def __init__(self, message: str, job: str):
        super().__init__(message, reason="needs-merge")
        self.job = job


def _start_merge_job(root, *, purpose: str, ours: str, theirs: str, **extra) -> str:
    from fused_render.contrib import merge_job
    return merge_job.start(root, purpose=purpose, ours=ours, theirs=theirs, **extra)


def _take_in_pr_head(root, record, ident: dict, fork: str, head_branch: str,
                     remote: str | None) -> str:
    """Make sure Bob's copy holds whatever the proposal on GitHub holds
    (module docstring: someone else edited it). Answers HEAD, after merging
    the proposal's head in when it was missing. An overlap starts Merge with
    Claude and raises `MergeStarted`."""
    replay = _replay()
    head = _rev(root, "HEAD")
    if not remote or not head or remote == head:
        return head
    if _rev(root, remote) and replay.is_ancestor(root, remote, head):
        return head
    if not gh.fetch(root, fork, f"+refs/heads/{head_branch}:{PR_HEAD_REF}"):
        raise PlainError("Couldn't reach GitHub to check your proposal. Try again.")
    fetched = _rev(root, PR_HEAD_REF)
    if fetched != remote:
        raise PlainError(_MOVED)
    if replay.is_ancestor(root, fetched, head):
        return head
    mt = replay.merge_tree(root, head, fetched)
    if not mt.get("supported"):
        raise PlainError("Couldn't combine the proposal on GitHub with your copy. Try again.")
    if not mt.get("clean"):
        job = _start_merge_job(root, purpose="proposal", ours=head, theirs=fetched)
        raise MergeStarted(
            f"Your proposal was also changed on GitHub, in the same places as your newer "
            f"edits. Claude is combining them — check the result on this page, and it's "
            f"sent once you keep it.", job)
    ident = commit_identity(root, ident)
    me = (ident["name"], ident["email"])
    merged = _commit_tree(root, mt["tree"], [head, fetched],
                          "Bring in the edits made to your proposal on GitHub\n",
                          author=me, committer=me)
    gu = _gu()
    with replay.slot() as held:
        if not held:
            raise PlainError(_BUSY)
        _gate(root)   # a turn may have opened since `_prepare` let go of the slot
        # `head` is the merge's first parent: the fast-forward is the CAS.
        if gu._current_branch(root) != home_branch() or _rev(root, "HEAD") != head:
            why = "moved"
        else:
            why = replay.land_ff_locked(root, merged)[0]
    if why is not None:
        raise PlainError("Your app changed while sending. Try again.")
    _activity(root, "proposal-their-edits",
              "Brought in the edits made to your proposal on GitHub")
    return merged


def update(app_dir, *, include=None, step=None) -> dict:
    """Send Bob's newer edits to his open proposal (§16.5, §17.5): a plain
    push of HEAD — which already holds what was sent — to the proposal's
    own branch, after merging in edits made to it on GitHub.
    `include` = new files Bob ticked, as for Send (§16.12)."""
    root, record = _record(app_dir)
    p = record.get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        raise PlainError("There's no proposal to update.")
    if record.get("kind") != "github":
        raise PlainError("Your remix is its own app now, so this proposal can't take "
                         "its newer edits.")
    _gate(root)
    me = _require_identity()
    who = _owner_name(record)
    if conflict(root):
        raise PlainError(f"{who} changed the same parts you did. "
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
    p = record.get("proposal") or p
    include = _check_include(root, include)
    tip = tip_of(root, record, fetch=True)
    if not tip:
        raise _no_tip(record)
    head = _rev(root, "HEAD")
    edits = dirty(root) or bool(include)
    if not edits and _same_tree(root, head, tip):
        raise PlainError(f"Your copy matches {who}'s latest — there's nothing to send.")
    remote = pr.get("head_sha")
    asked = bool((p.get("cache") or {}).get("asked"))
    # Nothing new (§17.5): no edits, GitHub's head already in HEAD and no
    # commit of Bob's after it (`head_differs` — a bring-in of the author's
    # latest is never sent on its own), not asked, and it still fits.
    record = _store().get(root) or record      # `_ingest` refreshed the cache
    if (not edits and remote and not asked
            and pr.get("mergeable") is not False
            and not head_differs(root, record)):
        raise PlainError("Your proposal already has all your changes.", reason="nothing-new")
    _say(step, "send")
    head = _prepare(root, record, me, include=include)
    head = _take_in_pr_head(root, record, me, fork, head_branch, remote)
    if head == remote:
        raise PlainError("Your proposal already has all your changes.", reason="nothing-new")
    _push(root, head, fork, head_branch)
    _set_pr_head(root, head)
    caught_up = _github_shows(p["repo"], p["number"], head, step)
    now = time.time()

    def put(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            q.update(sent_at=now)
            # "alice commented" answers the version she commented on; a new
            # send starts the wait again (only comments after `sent_at`
            # count on the next refresh).
            q.setdefault("cache", {}).update(head_sha=head, comment=None, asked=None)

    _store().update(root, put)
    _activity(root, "proposal-updated", f"Sent your newer edits to your proposal for {who}")
    out = view(root) or {}
    if not caught_up:
        out["message"] = ("Sent. GitHub hasn't caught up yet; your newer edits show on the "
                          "proposal in a moment.")
    return out


#: After a push, GitHub shows the proposal's new version a few seconds later.
#: Update waits for that, briefly and visibly (step "confirm"), so the page
#: never reads the old version back as if it were current (§16.13).
CONFIRM_TRIES = 4
CONFIRM_WAIT_S = 1.5


def _github_shows(repo, number, head, step) -> bool:
    """Whether GitHub already reports `head` as the proposal's version."""
    _say(step, "confirm")
    for i in range(CONFIRM_TRIES):
        try:
            if gh.get_pr(repo, number).get("head_sha") == head:
                return True
        except gh.GitHubError:
            return False
        if i < CONFIRM_TRIES - 1:
            time.sleep(CONFIRM_WAIT_S)
    return False


def merge_accepted(app_dir) -> dict:
    """Merge with Claude (purpose "proposal") landed in Bob's copy: send the
    combined version now (`merge_job` calls this after its accept)."""
    return start_update(app_dir)


def _push(root, sha, fork, head_branch) -> None:
    """A plain push — never force (§16.5). Rejected = someone added to the
    proposal after we looked."""
    try:
        gh.push(root, sha, fork, head_branch)
    except gh.GitHubError as exc:
        if exc.reason == "conflict":
            raise PlainError(_MOVED)
        raise


def _plain_push_error(exc: gh.GitHubError, record: dict) -> Exception:
    if exc.reason == "forbidden":
        return gh.GitHubError("forbidden", "GitHub didn't let your account save a copy of "
                                           f"{_owner_name(record)}'s app.")
    if exc.reason == "conflict":
        return PlainError(_MOVED)
    return exc


def _delete_branch(head_repo: str | None, head_branch: str | None, repo: str | None) -> None:
    """Tidy Bob's copy on GitHub, best-effort — only ever a fused branch in
    HIS copy, never the author's repo."""
    if not head_repo or not head_branch or not head_branch.startswith("fused-"):
        return
    if repo and head_repo.lower() == repo.lower():
        return
    try:
        gh.delete_branch(head_repo, head_branch)
    except Exception:  # noqa: BLE001
        logger.info("proposals: could not delete %s:%s", head_repo, head_branch, exc_info=True)


def withdraw(app_dir) -> dict:
    """Close Bob's open proposal on GitHub (with a short note to the author),
    delete its copy on GitHub (best-effort), and forget it locally. Never
    touches Bob's changes.

    Refused (raises, reason "busy") while a send or update job for this app
    is still running: the send writes its proposal record only at the end,
    so a withdraw answered "ok" now would be followed by a fresh open
    proposal a moment later. Refused (reason "claude") while Claude works."""
    store = _store()
    root = store.key(app_dir)
    from fused_render.contrib import jobs
    for kind in ("propose-send", "propose-update"):
        if (jobs.latest(kind, root) or {}).get("status") == "running":
            raise PlainError("Your proposal is still being sent — try again in a moment.",
                             reason="busy")
    record = store.get(root) or {}
    p = record.get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        return {"ok": True}
    _gate(root)
    was_open = _is_open(p)
    if was_open:
        from fused_render.contrib import jobs

        me = gh.identity() or {}
        who = me.get("login") or p.get("user") or "The sender"
        jobs.set_step(jobs.current_id(), "close")
        try:
            gh.close_pr(p["repo"], p["number"], comment=f"{who} withdrew this proposal.")
        except gh.GitHubError as exc:
            if exc.reason != "not-found":
                raise
        jobs.set_step(jobs.current_id(), "tidy")
        _delete_branch(p.get("head_repo"), p.get("head_branch"), p.get("repo"))

    def clear(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            rec["proposal"] = None

    store.update(root, clear)
    _set_pr_head(root, None)
    if was_open:
        _activity(root, "proposal-withdrawn",
                  f"Withdrew your proposal to {_owner_name(record)}")
    return {"ok": True}


def settle_direct(app_dir, head: str | None = None) -> dict:
    """A collaborator published straight to the author's app (§16.5;
    `engine.push_to_main` pushed `head` and writes the Activity line): an
    open proposal is closed with "Published directly", its copy on GitHub
    deleted, and the record settled as merged at `head` — landed, seen,
    never polled again (GitHub may report it closed rather than merged; that
    must not read as declined). The one place outside the engine that
    stamps `landed_at` (§17.5: as v2 stamped `returned_at` here)."""
    store = _store()
    root = store.key(app_dir)
    record = store.get(root) or {}
    p = record.get("proposal") if isinstance(record.get("proposal"), dict) else None
    if not _is_open(p):
        return {"ok": True}
    try:
        gh.close_pr(p["repo"], p["number"], comment="Published directly")
    except gh.GitHubError:
        # GitHub may already count it merged (its commits are in now).
        logger.info("proposals: closing #%s after a direct publish failed", p.get("number"))
    _delete_branch(p.get("head_repo"), p.get("head_branch"), p.get("repo"))
    now = time.time()
    head = head or _rev(root, "HEAD")

    def put(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p.get("number") and _is_open(q):
            q.update(direct=True, landed_at=now, settled_seen=True)
            q.setdefault("cache", {}).update(state="merged", merge_commit_sha=head,
                                             checked_at=now)

    store.update(root, put)
    _set_pr_head(root, None)
    return {"ok": True}


# ------------------------------------------------------------------ refresh


def _plain_comment(body: str) -> str:
    return (body or "").replace(gh.ASK_UPDATE_MARKER, "").strip()


def _newest_comment(comments: list, bob: str | None, since: float | None, *,
                    asks: bool = False) -> dict | None:
    """The newest comment by someone other than Bob (and not a bot) posted
    after the last send — "alice commented". With `asks`, only the ones
    asking him to update (`inbox.ask_update`'s marker)."""
    best = None
    for c in comments or []:
        who = c.get("author") or ""
        if not who or gh.same_login(who, bob) or who.lower().endswith("[bot]"):
            continue
        body = c.get("body") or ""
        if asks and gh.ASK_UPDATE_MARKER not in body:
            continue
        if not asks and not _plain_comment(body):
            continue
        at = _iso_to_epoch(c.get("at"))
        if since is not None and at is not None and at <= since:
            continue
        if best is None or (c.get("at") or "") >= (best.get("at") or ""):
            best = c
    if best is None:
        return None
    return {"author": best["author"], "body": _plain_comment(best["body"])[:4000],
            "at": best.get("at") or ""}


def _apply_pr(root, pr: dict, comments: list | None) -> tuple[dict | None, str | None]:
    """Write GitHub's answer into the record's cache. Answers `(proposal,
    previous state)`."""
    state = "merged" if pr.get("merged") else ("closed" if pr.get("state") == "closed" else "open")
    holder = {}

    def fn(rec):
        q = rec.get("proposal")
        if not isinstance(q, dict) or q.get("number") != pr.get("number") or q.get("direct"):
            return
        cache = q.setdefault("cache", {})
        holder["prev"] = cache.get("state", "open")
        cache.update(state=state, merge_commit_sha=pr.get("merge_commit_sha") if pr.get("merged") else None,
                     checked_at=time.time(), head_sha=pr.get("head_sha"), error=None)
        if state == "open":
            cache["mergeable"] = pr.get("mergeable") if isinstance(pr.get("mergeable"), bool) \
                else cache.get("mergeable")
        if comments is not None:
            bob = pr.get("user") or q.get("user")
            since = q.get("sent_at") or q.get("opened_at")
            cache["comment"] = _newest_comment(comments, bob, since)
            cache["asked"] = _newest_comment(comments, bob, since, asks=True)
        if state == "closed" and comments is not None:
            cache["note_checked"] = True        # settled: refresh stops polling
        if state == "open":
            cache.setdefault("comment", None)
            cache["note_checked"] = False       # reopened: follow it again
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
    REFRESH_TTL_S per app unless `force`) and cache what it says: open
    (+ the author's newest comment, and whether she asked for an update),
    closed without merging (declined — Bob's changes untouched), merged.
    Answers the page's proposal object. Never raises for a GitHub problem —
    the cached state stands until the next read.

    Bringing a merged proposal's copy onto the author's latest is the
    engine's (§17.2 `merged` row: bring-in, then settle, stamping
    `landed_at` once).

    A SETTLED PROPOSAL IS NOT POLLED (`_settled`): published directly;
    merged and landed — or merged on a remix, which never lands; closed
    unmerged once its closing note has been read. Only an explicit `force`
    asks GitHub about a closed one again (Bob's "check now"). Merged but NOT
    yet landed keeps polling: the engine waits on the author's latest
    carrying the merge."""
    store = _store()
    root = store.key(app_dir)
    record = store.get(root)
    p = (record or {}).get("proposal")
    if not isinstance(p, dict) or not p.get("number"):
        return None
    cache = p.get("cache") or {}
    if p.get("direct") or (cache.get("state") == "merged" and _settled(record, p)):
        return view(root)
    if _settled(record, p) and not force:
        return view(root)
    now = time.monotonic()
    with _refresh_lock:
        last = _last_refresh.get(root)
        if not force and last is not None and now - last < REFRESH_TTL_S:
            return view(root)
        _last_refresh[root] = now
    try:
        pr = gh.get_pr(p["repo"], p["number"])
        # Comments while open ("alice commented", "please update"), and once
        # more when it is closed unmerged and the closing note was not read
        # yet: the decline note (inbox.decline comments, then closes) is
        # what Bob is shown, and reading it settles the proposal.
        wants = pr["state"] == "open" or (not pr.get("merged")
                                          and not cache.get("note_checked"))
        comments = gh.pr_comments(p["repo"], p["number"]) if wants else None
    except gh.GitHubError as exc:
        reason = exc.reason

        def mark(rec):
            q = rec.get("proposal")
            if isinstance(q, dict) and q.get("number") == p["number"]:
                q.setdefault("cache", {})["error"] = reason

        store.update(root, mark)
        return view(root)
    _ingest(root, record, pr, comments)
    return view(root)


def acknowledge(app_dir) -> None:
    """The page showed the Merged/Declined banner."""
    _store().ack(app_dir, "settled")


# ------------------------------------------------------------ job wrappers


def _refusal(reason: str, message: str) -> dict:
    return {"ok": False, "reason": reason, "message": message}


def preflight(app_dir, *, needs_identity: bool = True) -> dict | None:
    """The synchronous checks a route makes before starting a job: phase on,
    a GitHub-link copy, Claude not mid-turn, signed in. None = go ahead;
    else the refusal."""
    if not _flags().changes():
        return _refusal("disabled", "This isn't turned on.")
    try:
        root, _record_ = _contributor_record(app_dir)
    except PlainError as exc:
        return _refusal("not-allowed", exc.message)
    if needs_identity and claude_busy(root):
        return _refusal("claude", CLAUDE_BUSY)
    if needs_identity:
        return identity_refusal("Sign in to GitHub to send your proposal.")
    return None


def identity_refusal(signed_out_message: str) -> dict | None:
    """None when someone is signed in to GitHub, else the refusal: reason
    "signed-out" (the page runs sign-in) or "offline". From `gh.identity`'s
    cache, the unreachable answer included, so an offline machine does not
    re-ask GitHub on every gated POST; the job itself asks again, strictly."""
    if gh.identity():
        return None
    if gh.unreachable():
        return _refusal("offline", gh.GitHubError("offline").message)
    return _refusal("signed-out", signed_out_message)


def start_job(kind: str, app_dir, fn) -> str:
    """`jobs.start(kind, <key>, lambda: fn(step))` where `step(key)` reports
    progress through `jobs.set_step` on the job's own id (`jobs.current_id`,
    thread-local to the job thread)."""
    from fused_render.contrib import jobs

    def step(key: str) -> None:
        jobs.set_step(jobs.current_id(), key)

    return jobs.start(kind, _store().key(app_dir), lambda: fn(step))


def _start(kind: str, app_dir, fn, *, needs_identity: bool = True) -> dict:
    refusal = preflight(app_dir, needs_identity=needs_identity)
    if refusal:
        return refusal
    return {"ok": True, "job": start_job(kind, app_dir, fn)}


def start_draft(app_dir) -> dict:
    """Job result `{title, description}`."""
    return _start("propose-draft", app_dir, lambda step: draft(app_dir), needs_identity=False)


def start_send(app_dir, *, title: str, description: str, note: str | None = None,
               include=None) -> dict:
    """Job steps "copy" → "send" → "open"; result = the proposal object
    (`number`, `url`, `state`, …). `include` = the untracked files Bob
    ticked (`untracked`)."""
    return _start("propose-send", app_dir,
                  lambda step: send(app_dir, title=title, description=description,
                                    note=note, include=include, step=step))


def start_update(app_dir, *, include=None) -> dict:
    """Job result = the proposal object. Fails with reason "nothing-new"
    when GitHub already has everything, "needs-merge" when edits made on
    GitHub overlap (a Merge with Claude job started; `merge_accepted` sends
    once it is kept)."""
    return _start("propose-update", app_dir,
                  lambda step: update(app_dir, include=include, step=step))
