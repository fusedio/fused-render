"""Which buttons an app's Source panel shows (docs/CONTRIB_LOOP.md §16.9).

THE ONE PLACE THAT DECIDES. `for_app(app_dir)` answers `{panel, actions}`:
the panel's state and sentence, and every button the page may draw. The
router's `POST /api/contrib/action` re-derives this list on every click and
refuses an id that is not in it — so the feature flags filter buttons here
and only here (P1), a click on a button the state already moved past is a
plain "not available", and the frontend never invents a button (§16.10).

TWO HALVES. `facts(app_dir)` gathers what the decision reads — the record,
the effective flags, the GitHub sign-in snapshot, `engine.status` (a
`github` copy), `ownsync.status` (an own app), `proposals.view` — all local:
no call here waits on GitHub (the page's GET must never block on it). Every
collaborator call is wrapped: a slice that fails or is missing reads as
None, and a None slice still yields a panel ("unknown", with Check now —
P6: no state without a fix button). `decide(facts)` is pure dict in, dict
out; the table tests drive it with literal facts.

P6, IN THIS FILE: every automatic move the engine/ownsync makes on its own
also exists here as a button (Bring in, Publish now, Check now), and every
stuck state names the problem and offers at least one fix. Sentences and
labels are product words (§0.1: no git words) and name the author by login.

`panel = {kind, repo, who, last_checked, state, sentence, files, unproposed,
unpublished, github_new}`; `actions = [{id, label, primary, confirm?,
danger?, dialog?}]`, primary first. `dialog` marks a button the page opens
a dialog for (propose, remix) or a flow it runs itself (sign-in) before
anything is posted.
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

#: Every id the action route dispatches (§16.9).
IDS = ("check", "bring-in", "merge-claude", "keep-mine", "use-theirs", "move-aside",
       "go-back-main", "propose", "update-proposal", "withdraw", "remix", "discard",
       "push-main", "publish-now", "use-github", "put-on-top", "publish-over",
       "save-edits", "sign-in", "retry")

_OPEN_JOB = ("running", "landing", "ready")


def _mod(name: str):
    return importlib.import_module(f"fused_render.contrib.{name}")


def _safely(what: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — one missing slice must not blank the panel
        logger.warning("contrib actions: %s failed", what, exc_info=True)
        return None


def _call(what: str, module: str, fn: str, *args, **kwargs):
    """`<module>.<fn>(*args)`, or None when it fails or does not exist (yet)."""
    mod = _safely(f"import {module}", _mod, module)
    func = getattr(mod, fn, None) if mod is not None else None
    if not callable(func):
        return None
    return _safely(what, func, *args, **kwargs)


# -- facts ----------------------------------------------------------------------


_UNSET = object()


def _owner(repo) -> str | None:
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else None


def _remix_of(record: dict) -> str | None:
    original = record.get("remix_of")
    if isinstance(original, dict):
        original = original.get("repo")
    return original if isinstance(original, str) and original else None


def _own_failures(app_dir: str) -> list:
    from fused_render import git_upstream

    return [f for f in (git_upstream.sync_failures() or []) if f.get("root") == app_dir]


def _own_checked(app_dir: str) -> float | None:
    from fused_render import git_upstream

    state = git_upstream.repo_state_for(app_dir) or {}
    at = state.get("checked_at")
    return at if isinstance(at, (int, float)) else None


def facts(app_dir: str, *, copy=_UNSET, proposal=_UNSET) -> dict:
    """What `decide` reads, from local state only. `copy`/`proposal` are
    the GET's own `engine.status`/`proposals.view` answers when it already
    has them (one git pass per read)."""
    from fused_render.contrib import refresh

    record = _call("record", "store", "get", app_dir)
    flags = _call("flags", "flags", "snapshot") or {}
    out = {"kind": None, "repo": None, "remix_of": None, "owner_checked": True,
           "flags": flags, "github": _safely("identity", refresh.github_identity) or {},
           "copy": None, "own": None, "proposal": None, "failures": [],
           "last_checked": None}
    if not isinstance(record, dict):
        return out
    kind = record.get("kind")
    out.update(kind=kind, repo=record.get("repo"), remix_of=_remix_of(record),
               owner_checked=record.get("owner_checked", True) is not False,
               original_update=bool(record.get("original_update")))
    if kind == "github":
        out["copy"] = (_call("status", "engine", "status", app_dir)
                       if copy is _UNSET else copy)
        out["proposal"] = (_call("proposal", "proposals", "view", app_dir)
                           if proposal is _UNSET else proposal)
        out["last_checked"] = (out["copy"] or {}).get("last_checked")
    elif kind == "own":
        own = _call("own status", "ownsync", "status", app_dir)
        out["own"] = own
        out["failures"] = _safely("sync failures", _own_failures, app_dir) or []
        at = (own or {}).get("last_checked")
        out["last_checked"] = at if isinstance(at, (int, float)) else _safely(
            "checked", _own_checked, app_dir)
        # A remix may still have the proposal it sent as a GitHub-link copy.
        out["proposal"] = (_call("proposal", "proposals", "view", app_dir)
                           if proposal is _UNSET else proposal)
    return out


# -- decide ---------------------------------------------------------------------


class _Buttons:
    """The action list being built: one entry per id, primary first."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, id_: str, label: str, *, primary=False, confirm=None, danger=False,
            dialog=None):
        if id_ in {a["id"] for a in self.items}:
            return
        a = {"id": id_, "label": label, "primary": bool(primary)}
        if confirm:
            a["confirm"] = confirm
        if danger:
            a["danger"] = True
        if dialog:
            a["dialog"] = dialog
        self.items.append(a)

    def has(self, id_: str) -> bool:
        return any(a["id"] == id_ for a in self.items)

    def drop(self, *ids):
        self.items = [a for a in self.items if a["id"] not in ids]

    def done(self) -> list[dict]:
        if self.items and not any(a["primary"] for a in self.items):
            # Exactly one primary when anything is offered: the first fix.
            first = next((a for a in self.items if a["id"] != "check"), self.items[0])
            first["primary"] = True
        seen_primary = False
        for a in self.items:  # at most one primary
            if a["primary"] and seen_primary:
                a["primary"] = False
            seen_primary = seen_primary or a["primary"]
        return sorted(self.items, key=lambda a: not a["primary"])


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _file_words(files: list) -> str:
    files = [f for f in files if isinstance(f, str)]
    if not files:
        return ""
    if len(files) <= 3:
        return ", ".join(files)
    return f"{', '.join(files[:2])} and {len(files) - 2} more"


def _panel(f: dict, **kw) -> dict:
    p = {"kind": f.get("kind"), "repo": f.get("repo"), "who": None,
         "last_checked": f.get("last_checked"), "state": "unknown",
         "sentence": "fused is still working out this app's state.", "files": [],
         "unproposed": 0, "unpublished": 0, "github_new": 0}
    p.update(kw)
    return p


def decide(f: dict) -> dict:
    """`{panel, actions}` for the facts `facts()` gathered. Pure."""
    flags = f.get("flags") or {}
    kind = f.get("kind")
    if not flags.get("sources"):
        return {"panel": _panel(f, state="off", sentence=""), "actions": []}
    if kind is None:
        return {"panel": _panel(f, kind="pending", state="pending",
                                sentence="Checking where this app came from…"),
                "actions": []}
    if kind == "github":
        return _github(f, flags)
    if kind == "own":
        return _own(f, flags)
    if kind == "scratch":
        return {"panel": _panel(f, state="scratch",
                                sentence="A trial copy of a proposal, next to your own app."),
                "actions": []}
    return {"panel": _panel(f), "actions": []}


def _signed_out(f: dict) -> bool:
    gh = f.get("github") or {}
    # `checking`: this machine was never measured — not yet an answer.
    return not gh.get("signed_in") and not gh.get("checking")


def _needs_merge(copy: dict) -> dict | None:
    nm = copy.get("needs_merge")
    if isinstance(nm, dict):
        return nm
    if nm:  # a v1-shaped status: a bool beside its files/message
        return {"purpose": copy.get("needs_merge_purpose") or "update",
                "files": (copy.get("merge_job") or {}).get("files") or []}
    return None


def _github(f: dict, flags: dict) -> dict:
    copy = f.get("copy")
    who = (copy or {}).get("author") or _owner(f.get("repo")) or "the author"
    b = _Buttons()
    if not isinstance(copy, dict):
        b.add("check", "Check now", primary=True)
        return {"panel": _panel(f, who=who), "actions": b.done()}
    if copy.get("pending"):
        return {"panel": _panel(f, kind="pending", who=who, state="pending",
                                sentence="Checking where this app came from…"),
                "actions": []}
    changes_on = bool(flags.get("changes"))
    remix_on = bool(flags.get("remix"))
    signed_out = _signed_out(f)
    files = [x for x in (copy.get("files") or []) if isinstance(x, str)]
    dirty = bool(copy.get("dirty"))
    has_changes = bool(files) or dirty or bool(copy.get("changes"))
    behind = int(copy.get("behind") or 0)
    unproposed = int(copy.get("unproposed") or 0)
    proposal = f.get("proposal") if isinstance(f.get("proposal"), dict) else None
    job = copy.get("merge_job") if isinstance(copy.get("merge_job"), dict) else None
    nm = _needs_merge(copy)
    blocked = copy.get("blocked") if isinstance(copy.get("blocked"), dict) else None
    fetch_error = copy.get("fetch_error") if isinstance(copy.get("fetch_error"), dict) else None
    can_push = copy.get("can_push") is True
    panel = _panel(f, who=who, files=files, unproposed=unproposed)
    extra: list[str] = []
    state = copy.get("state") or "following"
    lets_bring_in = True

    def theirs_confirm():
        return (f"Your copy becomes {who}'s version. Your changes are kept in Activity "
                "so you can bring them back.")

    def discard_button():
        b.add("discard", "Discard my changes", danger=True,
              confirm=(f"Your changes to this app go away and it goes back to {who}'s "
                       "version. fused keeps a copy you can bring back from Activity."))

    def send_buttons(label: str):
        if signed_out:
            b.add("sign-in", "Sign in to GitHub", primary=True, dialog="sign-in")
            extra.append("Sign in to GitHub to send your proposal.")
        else:
            b.add("propose", label, primary=True, dialog="propose")
        if remix_on and not signed_out:
            b.add("remix", "Publish as remix", dialog="remix")
        if can_push and not signed_out:
            b.add("push-main", f"Update {who}'s app directly",
                  confirm=(f"Your changes go straight into {who}'s app, without a "
                           f"proposal for {who} to look at first."))

    if job is not None and job.get("status") in _OPEN_JOB:
        lets_bring_in = False
        if job.get("status") == "ready":
            summary = (job.get("summary") or "").strip()
            sentence = "Claude combined both versions." + (f" {summary}" if summary else "")
        else:
            sentence = "Claude is combining both versions…"
        panel.update(state="merging", sentence=sentence,
                     files=job.get("files") or files)
        # Keep / Discard the merge live on the merge review (merge_job's
        # routes) — nothing else may move this copy while it is open.
        return {"panel": panel, "actions": b.done()}

    if copy.get("other_branch"):
        lets_bring_in = False
        state, sentence = "other-branch", (
            f"This copy is showing a different version than {who}'s main one.")
        b.add("go-back-main", "Go back to the main version", primary=True)
    elif nm is not None and changes_on:
        lets_bring_in = False
        nm_files = [x for x in (nm.get("files") or []) if isinstance(x, str)]
        purpose = nm.get("purpose") or "update"
        where = f": {_file_words(nm_files)}" if nm_files else ""
        state = "needs-merge"
        panel["files"] = nm_files or files
        if purpose == "return":
            sentence = f"{who} merged your change, and your newer edits overlap it{where}."
            b.add("merge-claude", "Merge with Claude", primary=True)
            b.add("use-theirs", f"Use {who}'s version", danger=True, confirm=theirs_confirm())
        elif purpose == "proposal":
            sentence = f"Your proposal no longer fits {who}'s latest{where}."
            b.add("merge-claude", "Merge with Claude", primary=True)
        else:
            sentence = f"{who} changed the same parts you did{where}."
            b.add("merge-claude", "Merge with Claude", primary=True)
            b.add("keep-mine", "Keep mine")
            b.add("use-theirs", f"Use {who}'s version", danger=True, confirm=theirs_confirm())
        if job is not None and job.get("status") == "failed":
            sentence += " Claude couldn't combine them last time."
    elif blocked is not None and changes_on:
        lets_bring_in = False
        in_way = [x for x in (blocked.get("files") or []) if isinstance(x, str)]
        state = "blocked"
        panel["files"] = in_way
        what = _file_words(in_way) or "A file of yours"
        verb = "is" if len(in_way) <= 1 else "are"
        sentence = f"{what} {verb} in the way of {who}'s update."
        b.add("move-aside", "Move my file aside and update", primary=True)
        b.add("keep-mine", "Keep mine")
    elif state == "merged":
        sentence = f"{who} merged your change. You're on {who}'s latest version."
    elif state == "declined" and changes_on:
        sentence = f"{who} declined your proposal. Your changes are still here."
        send_buttons("Propose again")
        discard_button()
    elif state == "proposed" and changes_on and proposal is not None:
        pstate = proposal.get("state")
        comment = proposal.get("comment") if isinstance(proposal.get("comment"), dict) else None
        if proposal.get("asked"):
            sentence = f"{who} asked you to update your proposal."
        elif pstate == "commented" and comment and comment.get("body"):
            sentence = f'{comment.get("author") or who} commented: "{comment["body"]}"'
        else:
            sentence = f"Waiting for {who}."
        if unproposed or dirty:
            n = unproposed + (1 if dirty and not unproposed else 0)
            extra.append(f"{_plural(n, 'edit')} not in your proposal.")
        if signed_out:
            b.add("sign-in", "Sign in to GitHub", primary=True, dialog="sign-in")
        else:
            if proposal.get("can_update", True) and (unproposed or dirty
                                                     or proposal.get("asked")):
                b.add("update-proposal", "Update proposal", primary=True)
            if proposal.get("can_withdraw", True):
                b.add("withdraw", "Withdraw")
            if can_push:
                b.add("push-main", f"Update {who}'s app directly",
                      confirm=(f"Your changes go straight into {who}'s app, without a "
                               f"proposal for {who} to look at first."))
    elif has_changes:
        state = "changes"
        n = len(files) or (1 if has_changes else 0)
        if changes_on:
            sentence = f"Your changes: {_plural(n, 'file')}."
            send_buttons(f"Propose to {who}")
            discard_button()
        else:
            # Changes off: the engine only fetches and catches up (P1) —
            # nothing here may offer to send or drop them.
            sentence = "You have your own changes in this app."
            lets_bring_in = False
    else:
        state = "following"
        sentence = (f"{who} has a newer version." if behind else
                    f"Following {who}'s latest.")

    if lets_bring_in and behind > 0:
        if copy.get("skip_tip"):
            extra.append(f"You kept your version; {who}'s latest is waiting.")
        b.add("bring-in", f"Bring in {who}'s latest")
    if copy.get("busy"):
        extra.append("Claude is still working on this app.")
    if fetch_error is not None:
        msg = fetch_error.get("message") or f"Couldn't reach {who}'s app on GitHub."
        extra.append(msg)
        if state == "following":
            state = "error"
        b.add("retry", "Try again", primary=not any(a["primary"] for a in b.items))
        if fetch_error.get("reason") in ("auth", "signed-out") and signed_out:
            b.add("sign-in", "Sign in to GitHub", dialog="sign-in")
    if not f.get("owner_checked", True) and signed_out:
        extra.append("Sign in so fused can tell whether this app is yours.")
        b.add("sign-in", "Sign in to GitHub", dialog="sign-in")
    b.add("check", "Check now")
    panel.update(state=state, sentence=" ".join([sentence, *extra]).strip())
    return {"panel": panel, "actions": b.done()}


def _own(f: dict, flags: dict) -> dict:
    original = f.get("remix_of")
    repo = f.get("repo")
    b = _Buttons()
    kind = "remix" if original else "own"
    who = _owner(original) if original else None
    if not repo:
        return {"panel": _panel(f, kind=kind, who=who, state="local", sentence="Your app."),
                "actions": []}
    own = f.get("own")
    if not isinstance(own, dict):
        b.add("check", "Check now", primary=True)
        return {"panel": _panel(f, kind=kind, who=who), "actions": b.done()}
    files = [x for x in (own.get("files") or []) if isinstance(x, str)]
    unpublished = int(own.get("unpublished") or 0)
    github_new = int(own.get("github_new") or 0)
    message = own.get("message") if isinstance(own.get("message"), str) else None
    state = own.get("state") or "ok"
    panel = _panel(f, kind=kind, who=who, files=files, unpublished=unpublished,
                   github_new=github_new)
    extra: list[str] = []
    failures = [x for x in (f.get("failures") or []) if isinstance(x, dict)]

    if state == "needs-merge":
        sentence = message or "Your edits and the version on GitHub changed the same parts."
        b.add("merge-claude", "Merge with Claude", primary=True)
    elif state == "github-changed":
        sentence = message or ("The version on GitHub changed in a way fused can't "
                               "combine on its own.")
        b.add("use-github", "Use the version on GitHub", danger=True,
              confirm=("Your copy becomes the version on GitHub. Your changes are kept in "
                       "Activity so you can bring them back."))
        if own.get("can_put_on_top", True) is not False and unpublished:
            b.add("put-on-top", "Put my edits on top", primary=True)
        b.add("publish-over", "Publish mine over it", danger=True,
              confirm=("The version on GitHub is replaced with yours. What changed on "
                       "GitHub since you last saw it will be gone from there."))
    elif state == "edits":
        where = _file_words(files)
        sentence = message or (f"Your unsaved edits to {where} are in the way of the update."
                               if where else "Your unsaved edits are in the way of the update.")
        b.add("save-edits", "Save my edits and continue", primary=True)
    elif failures or state == "error":
        state = "error"
        sentence = message or "Your latest changes couldn't be sent to GitHub yet."
        b.add("retry", "Try again", primary=True)
        if _signed_out(f) and any(x.get("reason") == "auth" for x in failures):
            b.add("sign-in", "Sign in to GitHub", dialog="sign-in")
    elif unpublished:
        state = "unpublished"
        sentence = message or f"{_plural(unpublished, 'change')} not on GitHub yet."
        b.add("publish-now", "Publish now", primary=True)
    elif original and f.get("original_update") and flags.get("remix"):
        state = "original-moved"
        sentence = f"{who} updated the original. Bring it into your remix?"
        b.add("bring-in", "Bring it in", primary=True)
    else:
        state = "ok"
        sentence = message or (f"Remix of {original}." if original else "Your app.")
        if github_new:
            extra.append(f"GitHub has {_plural(github_new, 'newer change')}.")
    if unpublished and state in ("error",):
        b.add("publish-now", "Publish now")
    b.add("check", "Check now")
    panel.update(state=state, sentence=" ".join([sentence, *extra]).strip())
    return {"panel": panel, "actions": b.done()}


def for_app(app_dir: str, *, copy=_UNSET, proposal=_UNSET) -> dict:
    """`{panel, actions}` for the app folder `app_dir` (§16.9). Local only."""
    return decide(facts(app_dir, copy=copy, proposal=proposal))


def activity(app_dir: str) -> list:
    """The record's Activity lines, newest first (`store.activity`), or []."""
    lines = _call("activity", "store", "activity", app_dir)
    return lines if isinstance(lines, list) else []
