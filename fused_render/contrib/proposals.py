"""Proposals (contract §8, v2 §16.5): Bob's changes to Alice's app, sent to
her for review as a pull request from his own copy of the repo on GitHub, in
his name, with Claude credited — plus Update, Withdraw, the status refresh
that follows the proposal on GitHub, and `settle_direct` for a collaborator
publishing straight to the author's app.

GITHUB IS THE TRUTH (§0.4). The record's `proposal` is the proposal's
IDENTITY on GitHub (repo + number) and a cache of what GitHub said last.
`refresh` re-derives every state from GitHub; nothing here infers "merged"
or "declined" from local history. A record lost entirely (a reinstall, a
store reset) is recovered from GitHub too: Send adopts the open proposal
from Bob's account that carries fused's footer marker instead of opening a
second one.

MERGE, NEVER REBASE (§16.0 P4). Bob's commits keep their ids forever. What
GitHub has from him is `proposal.sent` — the exact commit last sent — and it
stays an ancestor of his HEAD however often the author's latest is brought
in (the engine merges it). So an Update is always a plain, non-force push
that GitHub sees as "added commits", and "nothing new to send" is a count:
`unproposed` = Bob's own first-parent commits after `sent` that the author's
latest lacks.

CREDIT, ONCE (§16.5). Commits made before this machine knew Bob's GitHub
identity carry `Fused <apps@fused.io>`. Before the FIRST thing leaves the
machine (Send, Remix publish, a collaborator's direct publish), every
Fused-authored commit Bob has that the author's latest and any earlier send
lack is re-created with Bob as author and committer and ONE `Co-authored-by:
Claude` trailer — merges too, every parent mapped. Same trees, so landing
the result (backup ref first, `reset --keep`) changes no file. Never again
after that: a commit that was sent is never rewritten.

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

CO_AUTHOR = "Co-authored-by: Claude <noreply@anthropic.com>"
FUSED_EMAIL = "apps@fused.io"
FOOTER = "\n\n---\nSent from Fused\n" + gh.PROPOSAL_MARKER

#: Proposal status from GitHub is re-read at most this often per app,
#: however many pages poll (contract §8: 60 s).
REFRESH_TTL_S = 60.0

#: Where the proposal's head on GitHub is fetched to (Update, adoption).
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


# ----------------------------------------------------------------- credit


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


_SEP = "\x1e"


def credit(root, base: str, head: str, ident: dict, *, exclude=()) -> str:
    """The tip of `head ^base ^exclude…` re-created with Fused-authored
    commits credited to `ident` + Claude (module docstring). Writes objects
    only; moves no ref. Answers `head` itself when nothing needed changing."""
    fmt = "%H%x00%P%x00%T%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B" + "%x1e"
    nots = [f"^{x}" for x in (base, *exclude) if x]
    res = _git(root, "log", "--reverse", "--topo-order", f"--format={fmt}", head, *nots)
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
    # Merges are re-created too, every parent mapped (a "Bring in" merge the
    # engine made before Bob signed in is Fused-authored). A parent outside
    # the range (the author's side, an earlier send) maps to itself;
    # `--topo-order --reverse` visits every parent before its child.
    parent_map: dict[str, str] = {}
    for sha, parents, tree, an, ae, ad, cn, ce, cd, body in commits:
        old_parents = parents.split()
        new_parents = [parent_map.get(x, x) for x in old_parents]
        if ae.strip().lower() == FUSED_EMAIL:
            author = (ident["name"], ident["email"], ad)
            committer = (ident["name"], ident["email"], cd)
            message = with_trailer(body)
            changed = True
        else:
            author, committer, message = (an, ae, ad), (cn, ce, cd), body
            changed = new_parents != old_parents
        parent_map[sha] = (_commit_tree(root, tree, new_parents, message, author=author,
                                        committer=committer) if changed else sha)
    tip = _rev(root, head)
    return parent_map.get(tip, tip) if tip else head


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
            [_gu()._git_bin(), "-C", root, "commit-tree", "--no-gpg-sign", tree,
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


def _land_locked(root, *, branch, old_tip, new_tip, why) -> tuple[str | None, str | None]:
    """`replay.land_locked`: `(None, backup ref|None)` when the branch moved,
    else `(reason, None)`. Caller holds the slot."""
    return _replay().land_locked(root, branch=branch, old_tip=old_tip, new_tip=new_tip,
                                 why=why)


def _sent_shas(root, record) -> list[str]:
    """Commits that already left this machine and are never rewritten: the
    open or settled proposal's `sent`, when this repo still has it."""
    p = (record or {}).get("proposal")
    sent = p.get("sent") if isinstance(p, dict) else None
    return [sent] if sent and _rev(root, sent) else []


def _credit_once_locked(root, record, ident: dict, tip: str, branch: str) -> None:
    """The credit rewrite (module docstring), landed. Caller holds the slot."""
    p = record.get("proposal") if isinstance(record.get("proposal"), dict) else None
    if p and p.get("sent") and _is_open(p):
        return  # never after the first send
    head = _rev(root, "HEAD")
    if not head:
        return
    new = credit(root, tip, head, ident, exclude=_sent_shas(root, record))
    if new == head:
        return
    why, backup = _land_locked(root, branch=branch, old_tip=head, new_tip=new,
                               why="proposal-credit")
    if why is not None:
        raise PlainError("Your app changed while sending. Try again.")
    _activity(root, "proposal-credit",
              "Put your name on your changes, with Claude credited, before sending them",
              backup=backup)


def _prepare(root, record, ident: dict, *, tip: str | None, include=(),
             do_credit: bool = True, commit: bool = True) -> str:
    """Under the slot: commit the edits that go (`commit`), credit once
    (`do_credit`), and answer the HEAD to send."""
    gu = _gu()
    ident = commit_identity(root, ident)
    with _replay().slot() as held:
        if not held or gu._operation_in_flight(root) is not None:
            raise PlainError(_BUSY)
        branch = gu._current_branch(root)
        if not branch:
            raise PlainError("This copy can't send proposals right now.")
        _gate(root)
        if commit:
            commit_edits_locked(root, ident, include)
        if do_credit and tip:
            _credit_once_locked(root, record, ident, tip, branch)
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
    directly; merged and returned (or merged on a remix, which never
    returns); closed unmerged once its closing note was read."""
    state = (p.get("cache") or {}).get("state")
    if p.get("direct"):
        return True
    if state == "merged":
        return bool(p.get("returned_at")) or (record or {}).get("kind") != "github"
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


def count_unproposed(root, sent: str | None, tip: str | None) -> int:
    """Bob's own commits (first parent, merges not counted) that neither the
    last send nor the author's latest has (§16.3)."""
    nots = [f"^{x}" for x in (sent, tip) if x and _rev(root, x)]
    res = _git(root, "rev-list", "--count", "--first-parent", "--no-merges", "HEAD", *nots)
    try:
        return int(_out(res)) if _ok(res) else 0
    except ValueError:
        return 0


def unproposed(root, record) -> int:
    """`count_unproposed` for an open proposal (+1 for uncommitted tracked
    edits — Update sends those too); 0 without one. No network."""
    p = (record or {}).get("proposal")
    if not _is_open(p):
        return 0
    n = count_unproposed(root, p.get("sent"), tip_of(root, record))
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
    waiting = shown in ("waiting", "commented")
    out = {"number": p["number"], "url": p.get("url") or "", "title": p.get("title") or "",
           "state": shown, "needs_merge": bool(record.get("needs_merge")),
           "unproposed": unproposed(root, record) if waiting else 0,
           "settled_seen": bool(p.get("settled_seen")),
           "direct": bool(p.get("direct")),
           # "<who> asked you to update your proposal" (inbox.ask_update).
           "asked_update": waiting and bool(cache.get("asked")),
           # After a remix (kind "own") the copy carries the credit line and
           # a new app id, which must never be sent into the author's app:
           # still followed, still withdrawable, never updated.
           "can_update": waiting and record.get("kind") == "github",
           "can_withdraw": waiting}
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
    if record.get("needs_merge"):
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
    head = _prepare(root, record, me, tip=tip, include=include)
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
        "sent": head, "checked_tip": tip,
        "title": pr.get("title") or title, "opened_at": now, "sent_at": now,
        "user": me["login"],
        "cache": {"state": "open", "merge_commit_sha": None, "comment": None, "asked": None,
                  "checked_at": now, "head_sha": head, "error": None},
        "settled_seen": False, "returned_at": None,
    }

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
        branch = gu._current_branch(root)
        why = _land_locked(root, branch=branch, old_tip=head, new_tip=merged,
                           why="proposal-their-edits")[0] if branch else "moved"
    if why is not None:
        raise PlainError("Your app changed while sending. Try again.")
    _activity(root, "proposal-their-edits",
              "Brought in the edits made to your proposal on GitHub")
    return merged


def update(app_dir, *, step=None) -> dict:
    """Send Bob's newer edits to his open proposal (§16.5): a plain push of
    HEAD — which already holds `sent` — to the proposal's own branch."""
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
    if record.get("needs_merge"):
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
    tip = tip_of(root, record, fetch=True)
    if not tip:
        raise _no_tip(record)
    head = _rev(root, "HEAD")
    edits = dirty(root)
    if not edits and _same_tree(root, head, tip):
        raise PlainError(f"Your copy matches {who}'s latest — there's nothing to send.")
    remote = pr.get("head_sha")
    sent = p.get("sent")
    asked = bool((p.get("cache") or {}).get("asked"))
    if (not edits and remote and remote == sent and not asked
            and pr.get("mergeable") is not False
            and count_unproposed(root, sent, tip) == 0):
        raise PlainError("Your proposal already has all your changes.", reason="nothing-new")
    _say(step, "send")
    head = _prepare(root, record, me, tip=tip, do_credit=False)
    head = _take_in_pr_head(root, record, me, fork, head_branch, remote)
    if head == remote:
        raise PlainError("Your proposal already has all your changes.", reason="nothing-new")
    _push(root, head, fork, head_branch)
    now = time.time()

    def put(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            q.update(sent=head, checked_tip=tip, sent_at=now)
            # "alice commented" answers the version she commented on; a new
            # send starts the wait again (only comments after `sent_at`
            # count on the next refresh).
            q.setdefault("cache", {}).update(head_sha=head, comment=None, asked=None)

    _store().update(root, put)
    _activity(root, "proposal-updated", f"Sent your newer edits to your proposal for {who}")
    return view(root) or {}


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
        me = gh.identity() or {}
        who = me.get("login") or p.get("user") or "The sender"
        try:
            gh.close_pr(p["repo"], p["number"], comment=f"{who} withdrew this proposal.")
        except gh.GitHubError as exc:
            if exc.reason != "not-found":
                raise
        _delete_branch(p.get("head_repo"), p.get("head_branch"), p.get("repo"))

    def clear(rec):
        q = rec.get("proposal")
        if isinstance(q, dict) and q.get("number") == p["number"]:
            rec["proposal"] = None

    store.update(root, clear)
    if was_open:
        _activity(root, "proposal-withdrawn",
                  f"Withdrew your proposal to {_owner_name(record)}")
    return {"ok": True}


def credit_once(app_dir) -> dict:
    """The credit rewrite on its own (module docstring), for a collaborator's
    direct publish: `engine.push_to_main` calls it before bringing the
    author's latest in and pushing. `{ok}` or `{ok: False, reason, message}`."""
    try:
        root, record = _contributor_record(app_dir)
        _gate(root)
        me = _require_identity()
        tip = tip_of(root, record, fetch=True)
        if not tip:
            raise _no_tip(record)
        _prepare(root, record, me, tip=tip, commit=False)
    except PlainError as exc:
        return {"ok": False, "reason": exc.reason or "failed", "message": exc.message}
    return {"ok": True}


def settle_direct(app_dir, head: str | None = None) -> dict:
    """A collaborator published straight to the author's app (§16.5;
    `engine.push_to_main` pushed `head` and writes the Activity line): an
    open proposal is closed with "Published directly", its copy on GitHub
    deleted, and the record settled as merged at `head` — returned, seen,
    never polled again (GitHub may report it closed rather than merged; that
    must not read as declined)."""
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
            q.update(direct=True, returned_at=now, settled_seen=True)
            q.setdefault("cache", {}).update(state="merged", merge_commit_sha=head,
                                             checked_at=now)

    store.update(root, put)
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


def _apply_pr(root, pr: dict, comments: list | None,
              tip: str | None = None) -> tuple[dict | None, str | None]:
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
        if tip:
            q["checked_tip"] = tip
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


def _ingest(root, record, pr: dict, comments: list | None, tip: str | None = None) -> None:
    """`_apply_pr`, plus the one-time "declined" event on the open → closed
    transition (a reopened-then-closed proposal says it again, as it should)."""
    q, prev = _apply_pr(root, pr, comments, tip)
    if q is not None and prev == "open" and q["cache"]["state"] == "closed":
        try:
            _store().add_event(root, "declined", who=_owner_name(record))
        except Exception:  # noqa: BLE001
            logger.exception("proposals: declined event failed for %s", root)


def refresh(app_dir, *, force: bool = False, tip: str | None = None) -> dict | None:
    """Re-read the proposal from GitHub (throttled to one read per
    REFRESH_TTL_S per app unless `force`) and cache what it says: open
    (+ the author's newest comment, and whether she asked for an update),
    closed without merging (declined — Bob's changes untouched), merged.
    `tip` (the engine's fetch, §16.3) is recorded as `checked_tip` on a
    successful read. Answers the page's proposal object. Never raises for a
    GitHub problem — the cached state stands until the next read.

    Returning a merged proposal's copy to the author's latest is the
    engine's (§16.3 row 7), which calls this first whenever the author's
    latest moved.

    A SETTLED PROPOSAL IS NOT POLLED (`_settled`): published directly;
    merged and returned — or merged on a remix, which never returns; closed
    unmerged once its closing note has been read. Only an explicit `force`
    asks GitHub about a closed one again (Bob's "check now"). Merged but NOT
    yet returned keeps polling: the engine waits on the author's latest
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
    _ingest(root, record, pr, comments, tip)
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


def start_update(app_dir) -> dict:
    """Job result = the proposal object. Fails with reason "nothing-new"
    when GitHub already has everything, "needs-merge" when edits made on
    GitHub overlap (a Merge with Claude job started; `merge_accepted` sends
    once it is kept)."""
    return _start("propose-update", app_dir, lambda step: update(app_dir, step=step))
