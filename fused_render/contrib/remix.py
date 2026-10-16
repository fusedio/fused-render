"""Publish as remix, and bringing the original into a remix (contract §11,
handoff A5): Bob publishes his changed version of Alice's app as an app of
his own, credited as a remix of hers, and later takes in what she changed.

A NEW REPO, NOT A FORK (deliberate). The remix is a standalone repository in
Bob's account that carries Alice's whole history plus his. A GitHub fork
would be the wrong thing three ways: a user may hold only ONE fork of a repo,
and Bob's fork is already where his proposals live (handoff: remix and
propose are both available at any moment, neither withdrawing the other);
GitHub shows a fork as "forked from alice/x" with Alice's issues/PR
relationship rather than as Bob's own app; and a fork of a private repo can
never be made public. The credit is in the README, written by Bob, which is
what the handoff asks for.

A NEW APP IDENTITY. The remix is a different app, so the publish commit also
MINTS A NEW `fused-app-id` into the entry page. `app_id.stamp_entry` refuses
to overwrite an id on purpose ("an id is for life" — true of one app), so the
tag's content is rewritten here; everything else on the page is untouched.

ORDER, FOR FAILURE SAFETY: GitHub first (create the empty repo — or reuse the
one a failed earlier attempt created, recognised by owner + the description
this module writes), then the local commits, then the push, and only then
the remotes and the record. A failure before the push leaves Bob's copy a
GitHub-link copy with, at most, one extra local commit that a retry reuses;
nothing is published half-way under his name with the record pointing at it.

The remix's `origin` becomes Bob's repo (auto-sync pushes his later edits
there, as for any app of his own) and the original stays reachable as the
remote `original`. The record becomes `own` with `remix_of`; an open proposal
stays on the record and keeps being followed (`proposals.refresh`) until it
settles — a merged one on a remix never returns, so it settles at merged.

IDENTITY AND THE SWITCH (§16.6). Everything published is Bob's: tracked
edits are committed as him, and the same credit-once rewrite a first Send
runs (`proposals._prepare`) puts his name on Fused-authored commits first.
The switch to `own` happens under the slot in one stretch, RECORD FIRST:
the record's `repo`/`kind` change before `origin` does (§16.1: the record,
never `.git/config`, says what a copy is), then `origin` is re-pointed, then
`classify.ensure_push_guard(root, "own")` lifts the no-push guard — which
the rename carried to `original`, so the original stays never-push. No sync
can run in between and see a `github` record whose origin is Bob's, or an
`own` record still able to push to Alice.

BRING IN THE ORIGINAL is a MERGE, never a rebase: the remix's history is
published. `check_original` (background, on open) fetches the original's
branch into the private `refs/fused/original`; when HEAD lacks it the record
says `original_update`. `bring_in` hands the merge to `merge_job` (purpose
"original"), which already does exactly the two cases §11 names: a clean
merge comes back ready to confirm ("No overlap with your changes") without
Claude, an overlap goes to Claude. Accept lands the merge commit; auto-sync
pushes it to Bob's repo.
"""
from __future__ import annotations

import logging
import os
import re
import time

from fused_render.contrib import gh
from fused_render.contrib.jobs import PlainError

logger = logging.getLogger(__name__)

ORIGINAL_REF = "refs/fused/original"
_CREDIT_MAX = 500


def _p():
    from fused_render.contrib import proposals
    return proposals


def _store():
    from fused_render.contrib import store
    return store


def _replay():
    from fused_render.contrib import replay
    return replay


def _gu():
    from fused_render import git_upstream
    return git_upstream


def _flags():
    from fused_render.contrib import flags
    return flags


def description_for(original: str) -> str:
    """The new repo's GitHub description — also how a retry recognises the
    empty repo an earlier failed attempt created."""
    return f"Remix of {original}, made with Fused"


# ------------------------------------------------------------ the new id

_TAG_RE = re.compile(r"<meta\s[^>]*name\s*=\s*[\"']?fused-app-id[\"']?[^>]*>", re.I)
_CONTENT_RE = re.compile(r"(content\s*=\s*)([\"'])([^\"']*)\2", re.I)


def restamp(entry_html: str, new_id: str) -> bool:
    """Give the entry page `new_id`, replacing the id it declares (or adding
    one when it has none). True when the page reads back with `new_id`."""
    from fused_render import app_id

    if not app_id.is_valid(new_id):
        return False
    try:
        with open(entry_html, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeError):
        return False
    tag = _TAG_RE.search(text)
    if tag is None or not _CONTENT_RE.search(tag.group(0)):
        return app_id.stamp_entry(entry_html, new_id)
    new_tag = _CONTENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{new_id}{m.group(2)}",
                              tag.group(0), count=1)
    updated = text[:tag.start()] + new_tag + text[tag.end():]
    if app_id.id_from_text(updated) != new_id:
        return False
    try:
        with open(entry_html, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except OSError:
        return False
    return app_id.app_id(entry_html) == new_id


def _readme(root: str) -> str:
    for name in sorted(os.listdir(root)):
        if name.lower() in ("readme.md", "readme"):
            return os.path.join(root, name)
    return os.path.join(root, "README.md")


def _write_credit(root: str, name: str, credit: str) -> str:
    """Append the credit line to the README (creating one) — idempotent."""
    path = _readme(root)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = f"# {name}\n"
    if credit in text:
        return path
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n" + credit + "\n")
    return path


# -------------------------------------------------------------- the record


def _source_record(app_dir) -> tuple[str, dict]:
    store = _store()
    root = store.key(app_dir)
    record = store.get(root)
    if not isinstance(record, dict):
        raise PlainError("This app can't be published as a remix.")
    if record.get("kind") == "own" and record.get("remix_of"):
        raise PlainError("This is already your remix.")
    if record.get("kind") != "github" or not record.get("repo") or not record.get("branch"):
        raise PlainError("Only apps you installed from someone else can become a remix.")
    return root, record


def _remix_record(app_dir) -> tuple[str, dict]:
    store = _store()
    root = store.key(app_dir)
    record = store.get(root)
    if not isinstance(record, dict) or record.get("kind") != "own" \
            or not isinstance(record.get("remix_of"), dict):
        raise PlainError("This app isn't a remix.")
    return root, record


# ------------------------------------------------------------------- draft


def draft(app_dir) -> dict:
    """`{name, credit}` suggestions for the remix dialog (§11)."""
    root, record = _source_record(app_dir)
    p = _p()
    tip = p.tip_of(root, record)
    summary = ""
    if tip:
        summary = re.sub(r"(?m)^Claude: ", "", p._out(p._git(
            root, "log", "--reverse", "--no-merges", "--format=%s", "HEAD", f"^{tip}")))
    me = gh.identity() or {}
    from fused_render.contrib import ai
    return ai.draft_remix_credit(original=record["repo"], author=me.get("login") or "",
                                 summary=summary)


# ----------------------------------------------------------------- publish


def _target_repo(name: str, original: str, login: str, private: bool) -> str:
    """Create `login/name` — or reuse it when it is the empty repo a failed
    earlier publish of this same remix created."""
    full = f"{login}/{name}"
    try:
        info = gh.repo(full)
    except gh.GitHubError as exc:
        if exc.reason != "not-found":
            raise
        info = None
    if info is not None:
        if gh.same_login(info.get("owner"), login) and info.get("description") == description_for(original):
            return info.get("full_name") or full
        raise PlainError(f"You already have something called {name} on GitHub. "
                         "Pick another name.")
    try:
        return gh.create_repo(name, description=description_for(original), private=private)
    except gh.GitHubError as exc:
        if exc.reason == "conflict":
            raise PlainError(f"You already have something called {name} on GitHub. "
                             "Pick another name.")
        raise


def _remix_commit_locked(root, *, name: str, credit: str, original: str, me: dict) -> str:
    """The one commit, authored by Bob, that turns the copy into a new app:
    the README credit and a freshly minted app id. Reused when HEAD already
    is that commit (a retry). Caller holds the slot."""
    from fused_render import app_id, app_listing

    p = _p()
    subject = f"Remix of {original}"
    head_info = p._out(p._git(root, "log", "-1", "--format=%s%x00%ae", "HEAD")).split("\x00")
    if len(head_info) == 2 and head_info[0] == subject and head_info[1] == me["email"]:
        return p._rev(root, "HEAD")
    paths = [_write_credit(root, name, credit)]
    try:
        entry = app_listing.app_entry(root)
    except OSError:
        entry = None
    if entry and not restamp(entry, app_id.mint(name)):
        raise PlainError("Couldn't give your remix its own identity. Try again.")
    if entry:
        paths.append(entry)
    rels = [os.path.relpath(x, root) for x in paths]
    if not p._ok(p._git(root, "add", "--", *rels)):
        raise PlainError("Couldn't prepare your remix. Try again.")
    res = p._git(root, *p._identity_cfg(me), "commit", "--no-verify", "-q",
                 "-m", f"{subject}\n\n{credit}", "--", *rels)
    if not p._ok(res):
        logger.warning("remix: commit failed in %s: %s", root, _gu()._text(res))
        raise PlainError("Couldn't prepare your remix. Try again.")
    return p._rev(root, "HEAD")


def _repoint_locked(root, *, full: str, original: str, branch: str, follow_tip: str | None) -> None:
    """origin → Bob's repo (with the tracking refs auto-sync reads), the old
    origin kept as `original`, the original's tip pinned for
    `check_original`. Caller holds the slot."""
    p = _p()
    remotes = p._out(p._git(root, "remote")).split()
    if "original" in remotes:
        p._git(root, "remote", "remove", "original")
    if "origin" in remotes:
        p._git(root, "remote", "rename", "origin", "original")
    else:
        p._git(root, "remote", "add", "original", f"https://github.com/{original}.git")
    p._git(root, "remote", "add", "origin", f"https://github.com/{full}.git")
    head = p._rev(root, "HEAD")
    p._git(root, "update-ref", f"refs/remotes/origin/{branch}", head)
    p._git(root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    p._git(root, "config", f"branch.{branch}.remote", "origin")
    p._git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    if follow_tip:
        p._git(root, "update-ref", ORIGINAL_REF, follow_tip)


def _push_remix(root, tip: str, full: str, branch: str, name: str) -> None:
    """Push the remix NEVER over anything we did not put there. A reused repo
    (same owner + our description) is normally empty, or holds an earlier
    attempt's push that `tip` already contains. If its branch holds anything
    else — a later publish of the same name, proposals someone merged into
    an earlier remix — that is refused, never overwritten. The push is leased
    on the tip just read (or on "no branch yet"), so a write landing between
    the look and the push is refused too."""
    remote = gh.branch_tip(full, branch)
    if remote and remote != tip and not (_replay().rev(root, remote)
                                         and _replay().is_ancestor(root, remote, tip)):
        raise PlainError(f"Your {name} on GitHub already has other changes in it. "
                         "Pick another name.")
    if remote == tip:
        return
    try:
        gh.push(root, tip, full, branch, lease=remote or "")
    except gh.GitHubError as exc:
        if exc.reason == "conflict":
            raise PlainError(f"Your {name} on GitHub changed while publishing. Try again.")
        raise


def _push_guard(root) -> None:
    try:
        from fused_render.contrib import classify
        classify.ensure_push_guard(root, "own")
    except Exception:  # noqa: BLE001 — classification re-applies it
        logger.exception("remix: push guard for %s failed", root)


def publish(app_dir, *, name: str, credit: str, step=None) -> dict:
    """Publish Bob's version as his own app (§11). Answers `{repo, url,
    share_url}`; `step(key)` hears "create" → "send"."""
    name = (name or "").strip()
    credit = " ".join((credit or "").split())[:_CREDIT_MAX]
    if not credit:
        raise PlainError("Add a line crediting the original app.")
    root, record = _source_record(app_dir)
    p = _p()
    p._gate(root)
    me = p._require_identity()
    original, branch = record["repo"], record["branch"]
    try:
        private = bool(gh.repo(original).get("private"))
    except gh.GitHubError as exc:
        if exc.reason not in ("not-found", "forbidden"):
            raise
        # Can't tell: a remix of something we cannot see must not be made
        # public by default.
        private = True
    p._say(step, "create")
    full = _target_repo(name, original, me["login"], private)

    p._say(step, "send")
    follow_tip = p.tip_of(root, record, fetch=True)
    p._prepare(root, record, me, tip=follow_tip)
    replay = _replay()
    with replay.slot() as held:
        if not held:
            raise PlainError("Your app is busy updating. Try again in a moment.")
        p._gate(root)
        tip = _remix_commit_locked(root, name=name, credit=credit, original=original, me=me)
    if not tip:
        raise PlainError("Couldn't prepare your remix. Try again.")
    gu = _gu()
    local_branch = gu._current_branch(root) or branch
    _push_remix(root, tip, full, local_branch, name)

    now = time.time()

    def become_own(rec):
        rec.update(kind="own", repo=full, branch=local_branch, classified_by="remix",
                   classified_at=now, owner_checked=True,
                   remix_of={"repo": original, "branch": branch},
                   needs_merge=None, original_update=None, skip_tip=None)

    with replay.slot() as held:
        if not held:
            raise PlainError("Your app is busy updating. Try again in a moment.")
        # Record first, then origin, then the guard (module docstring).
        _store().update(root, become_own)
        _repoint_locked(root, full=full, original=original, branch=local_branch,
                        follow_tip=follow_tip)
        _push_guard(root)
    try:
        _store().add_event(root, "remixed", who=me["login"])
    except Exception:  # noqa: BLE001
        logger.exception("remix: event failed for %s", root)
    p._activity(root, "remixed", f"Published your version as your own app, {full}")
    url = f"https://github.com/{full}"
    # The GitHub link IS the share link (handoff A1/A5: people who install
    # it are GitHub-link users of Bob's repo).
    return {"repo": full, "url": url, "share_url": url}


# -------------------------------------------------------- the original


def check_original(app_dir) -> bool:
    """Fetch the original's branch and record whether the remix lacks it
    (`original_update`). Answers True when there is something to bring in.
    Background (on open); never raises."""
    try:
        if not _flags().remix():
            return False
        root, record = _remix_record(app_dir)
        src = record["remix_of"]
        if not src.get("repo") or not src.get("branch"):
            return False
        # A private ref only this module writes: safe without the slot
        # (gh.fetch's docstring).
        gh.fetch(root, src["repo"], f"+refs/heads/{src['branch']}:{ORIGINAL_REF}")
        replay = _replay()
        tip = replay.rev(root, ORIGINAL_REF)
        head = replay.rev(root, "HEAD")
        if not tip or not head:
            return False
        behind = not replay.is_ancestor(root, tip, head)
        now = time.time()

        def mark(rec):
            prev = rec.get("original_update") or {}
            if not behind:
                rec["original_update"] = None
            elif prev.get("sha") != tip:
                rec["original_update"] = {"sha": tip, "since": now}

        _store().update(root, mark)
        return behind
    except PlainError:
        return False
    except Exception:  # noqa: BLE001
        logger.exception("remix: checking the original failed for %s", app_dir)
        return False


def bring_in(app_dir) -> dict:
    """Start bringing the original's latest into the remix: a merge job
    (purpose "original") that comes back ready to confirm. `{ok, job}`, or
    `{ok, message}` when there is nothing to bring in."""
    root, record = _remix_record(app_dir)
    replay = _replay()
    tip = replay.rev(root, ORIGINAL_REF)
    head = replay.rev(root, "HEAD")
    if not tip:
        check_original(root)
        tip = replay.rev(root, ORIGINAL_REF)
    if not tip or not head:
        raise PlainError("Couldn't reach the original app on GitHub. Try again.")
    owner = record["remix_of"]["repo"].split("/")[0]
    if replay.is_ancestor(root, tip, head):
        _store().update(root, lambda rec: rec.update(original_update=None))
        return {"ok": True, "message": f"Your remix already has {owner}'s latest."}
    from fused_render.contrib import merge_job
    return {"ok": True, "job": merge_job.start(root, purpose="original", ours=head,
                                               theirs=tip)}


# ------------------------------------------------------------ job wrappers


def _preflight(app_dir, *, source: bool) -> dict | None:
    if not _flags().remix():
        return {"ok": False, "reason": "disabled", "message": "This isn't turned on."}
    try:
        (_source_record if source else _remix_record)(app_dir)
    except PlainError as exc:
        return {"ok": False, "reason": "not-allowed", "message": exc.message}
    return None


def start_draft(app_dir) -> dict:
    """Job result `{name, credit}`."""
    refusal = _preflight(app_dir, source=True)
    if refusal:
        return refusal
    return {"ok": True, "job": _p().start_job("remix-draft", app_dir,
                                              lambda step: draft(app_dir))}


def start_publish(app_dir, *, name: str, credit: str) -> dict:
    refusal = _preflight(app_dir, source=True)
    if refusal:
        return refusal
    refusal = _p().identity_refusal("Sign in to GitHub to publish your remix.")
    if refusal:
        return refusal
    return {"ok": True, "job": _p().start_job(
        "remix-publish", app_dir,
        lambda step: publish(app_dir, name=name, credit=credit, step=step))}


def start_bring_in(app_dir) -> dict:
    """Synchronous (the merge job is the long part and runs on its own
    thread): `{ok, job}` — a MERGE JOB id, polled like Merge with Claude."""
    refusal = _preflight(app_dir, source=False)
    if refusal:
        return refusal
    try:
        return bring_in(app_dir)
    except PlainError as exc:
        return {"ok": False, "reason": getattr(exc, "reason", "failed"), "message": exc.message}
