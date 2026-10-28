"""Which buttons an app's Source panel shows (docs/CONTRIB_LOOP.md §16.9,
§17.2 for a GitHub-link copy).

THE ONE PLACE THAT DECIDES. `for_app(app_dir)` answers `{panel, actions}`:
the panel's state and sentence, and every button the page may draw. The
router's `POST /api/contrib/action` re-derives this list on every click and
refuses an id that is not in it — so the feature flags filter buttons here
and only here (P1), a click on a button the state already moved past is a
plain "not available", and the frontend never invents a button (§16.10).

TWO HALVES. `facts(app_dir)` gathers what the decision reads — the kind
(`classify.kind_of`), the record, the effective flags, the GitHub sign-in
snapshot, `engine.status` (every kind: it carries the merge job),
`ownsync.status` (an own app), `proposals.view`/`untracked` — all local: no
call here waits on GitHub (the page's GET must never block on it). Every
collaborator call is wrapped: a slice that fails reads as None, and a None
slice still yields a panel ("unknown", with Check now — P6: no state
without a fix button). `decide(facts)` is pure dict in, dict out; the
table tests drive it with literal facts, and
`test_every_called_function_exists` pins that the names wrapped here exist.

P6, IN THIS FILE: every automatic move the engine/ownsync makes on its own
also exists here as a button (Bring in, Publish now, Check now), and every
stuck state names the problem and offers at least one fix. Sentences and
labels are product words (§0.1: no git words) and name the author by login.
An own app's fixes per state are ownsync's (`status()["fixes"]`); this file
labels them and applies the flags.

`panel = {kind, repo, who, last_checked, state, sentence, files, unproposed,
unpublished, github_new, untracked, merge}` or None (nothing to show: the
sources phase off, an own app with no GitHub repo); `actions = [{id, label,
primary, confirm?, danger?, dialog?}]`, primary first. `dialog` marks a
button the page opens a dialog for (propose, remix) or a flow it runs
itself (sign-in) before anything is posted. `untracked`: the files the
Propose dialog lists unticked (only while Propose is offered); `merge`: the
Merge with Claude job the review shows (`engine.status`'s `merge_job`).
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

#: Every id the action route dispatches (§16.9).
IDS = ("check", "bring-in", "merge-claude", "match", "keep-working", "move-aside",
       "go-back-main", "propose", "update-proposal", "withdraw", "remix", "discard",
       "push-main", "publish-now", "use-github", "put-on-top", "publish-over",
       "save-edits", "sign-in", "retry", "choose-mine", "choose-theirs",
       # §18.4: the marketplace kind's (and a github copy's switch back).
       "switch-link", "switch-reviewed", "confirm-block", "remove-app")

#: The merge purposes whose jobs land in the folder, per kind — what a
#: "choice" job's per-file buttons (`merge_job.choose`) act on.
CHOICE_PURPOSES = {"github": ("update", "proposal"), "own": ("own-sync", "original"),
                   "marketplace": ("update", "proposal")}

SWITCH_LINK_CONFIRM = "Follow {who} directly? You'll get every change, unreviewed."
SWITCH_REVIEWED_CONFIRM = ("Switch to reviewed releases? Your copy goes to the version Fused "
                           "reviewed, and updates come only when {who} publishes a release.")

_OPEN_JOB = ("running", "landing", "ready")

USE_GITHUB_CONFIRM = ("Your copy becomes the version on GitHub. Your edits are kept in "
                      "Activity so you can bring them back.")


def _moved_message() -> str:
    """merge_job's own sentence for a Keep that found the copy moved since
    the job started (§17.4) — read from there, never copied."""
    mod = _safely("import merge_job", _mod, "merge_job")
    text = getattr(mod, "MOVED_MESSAGE", None)
    return text if isinstance(text, str) and text else (
        "Your copy changed meanwhile; run it again.")


def _publish_over_confirm() -> str:
    """ownsync's own sentence for Publish mine over it — it says what that
    module keeps, so it is read from there, never copied."""
    mod = _safely("import ownsync", _mod, "ownsync")
    text = getattr(mod, "PUBLISH_OVER_CONFIRM", None)
    return text if isinstance(text, str) and text else (
        "Replace the version on GitHub with this copy?")


def _mod(name: str):
    return importlib.import_module(f"fused_render.contrib.{name}")


def _safely(what: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — one failing slice must not blank the panel
        logger.warning("contrib actions: %s failed", what, exc_info=True)
        return None


def _call(what: str, module: str, fn: str, *args, **kwargs):
    """`<module>.<fn>(*args)`, or None when it fails."""
    mod = _safely(f"import {module}", _mod, module)
    func = getattr(mod, fn, None) if mod is not None else None
    if not callable(func):
        logger.warning("contrib actions: %s.%s is missing", module, fn)
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


def _own_checked(app_dir: str) -> float | None:
    from fused_render import git_upstream

    at = git_upstream.last_checked(app_dir)
    return at if isinstance(at, (int, float)) else None


def facts(app_dir: str, *, copy=_UNSET, proposal=_UNSET, github=_UNSET) -> dict:
    """What `decide` reads, from local state only. `copy`/`proposal`/
    `github` are the GET's own `engine.status`/`proposals.view`/sign-in
    answers when it already has them (one pass over the repo per read)."""
    from fused_render.contrib import refresh

    flags = _call("flags", "flags", "snapshot") or {}
    out = {"app_dir": app_dir, "market_ui": bool(_call("market ui", "flags", "marketplace_ui")),
           "kind": None, "repo": None, "remix_of": None, "owner_checked": True,
           "original_update": False, "flags": flags,
           "github": (_safely("identity", refresh.github_identity) if github is _UNSET
                      else github) or {},
           "copy": None, "own": None, "proposal": None, "untracked": [],
           "last_checked": None}
    record = _call("record", "store", "get", app_dir)
    kind = _call("kind", "classify", "kind_of", app_dir)
    if not isinstance(record, dict) or kind is None:
        return out
    out.update(kind=kind, repo=record.get("repo"), remix_of=_remix_of(record),
               owner_checked=record.get("owner_checked", True) is not False,
               original_update=bool(record.get("original_update")))
    if kind == "marketplace" and (isinstance(record.get("block"), dict)
                                  or _call("block gate", "blocks", "gate", app_dir)):
        # The block list is enforced whatever the switches say (§18.8), so a
        # blocked copy's panel — its way through — shows whatever they say
        # too, also before enforcement wrote the block (the gate already
        # refuses then). The router's `_market_ui_for` agrees.
        out["market_ui"] = True
    if kind not in ("github", "own", "marketplace"):
        return out
    out["copy"] = _call("status", "engine", "status", app_dir) if copy is _UNSET else copy
    out["proposal"] = (_call("proposal", "proposals", "view", app_dir)
                       if proposal is _UNSET else proposal)
    if kind in ("github", "marketplace"):
        out["last_checked"] = (out["copy"] or {}).get("last_checked")
        if flags.get("changes"):
            out["untracked"] = _call("untracked", "proposals", "untracked", app_dir) or []
        # §18.4: a github copy whose repo is in the Marketplace may switch
        # to reviewed releases; a marketplace copy's B5 credit line reads the
        # row's newest release. The catalog FILE only — never a build here.
        out["row"] = _call("market row", "market", "row_for_repo", record.get("repo"),
                           mode="local")
        out["record_proposal"] = record.get("proposal") if isinstance(
            record.get("proposal"), dict) else None
        out["block_warn"] = record.get("block_warn") if isinstance(
            record.get("block_warn"), dict) else None
    else:
        out["own"] = _call("own status", "ownsync", "status", app_dir)
        out["last_checked"] = _safely("checked", _own_checked, app_dir)
        # engine.status tracks a GitHub-link copy's merges only; an own
        # app's are ownsync's (own-sync) and a remix's (original). An inbox
        # proposal's merge in the same folder is the inbox item's, not here.
        out["own_merge"] = _call("own merge", "merge_job", "current", app_dir,
                                 purpose=CHOICE_PURPOSES["own"])
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

    def done(self) -> list[dict]:
        if self.items and not any(a["primary"] for a in self.items):
            # Exactly one primary when anything is offered: the first fix —
            # never one that drops, takes back or publishes work past a
            # confirm (then Check now).
            first = next((a for a in self.items
                          if a["id"] not in ("check", "withdraw", "remix")
                          and not a.get("danger") and not a.get("confirm")),
                         next((a for a in self.items if a["id"] == "check"), self.items[0]))
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


def _strs(v) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _panel(f: dict, **kw) -> dict:
    p = {"kind": f.get("kind"), "repo": f.get("repo"), "who": None,
         "last_checked": f.get("last_checked"), "state": "unknown",
         "sentence": "fused is still working out this app's state.", "files": [],
         "unproposed": 0, "unpublished": 0, "github_new": 0, "untracked": [],
         "merge": None}
    p.update(kw)
    return p


def _merge(copy) -> dict | None:
    job = (copy or {}).get("merge_job") if isinstance(copy, dict) else None
    return job if isinstance(job, dict) else None


def decide(f: dict) -> dict:
    """`{panel, actions}` for the facts `facts()` gathered. Pure."""
    flags = f.get("flags") or {}
    kind = f.get("kind")
    if not flags.get("sources") and not (kind == "marketplace" and f.get("market_ui")):
        # A marketplace copy's panel shows with the Marketplace switch too
        # (`flags.marketplace_ui`, §18.8); its proposal/remix buttons still
        # need their own phases (`_github` reads them from `flags`).
        return {"panel": None, "actions": []}
    if kind is None:
        return {"panel": _panel(f, kind="pending", state="pending",
                                sentence="Checking where this app came from…"),
                "actions": []}
    if kind == "github":
        out = _github(f, flags)
        _switch_reviewed(f, out)
        if isinstance(out.get("panel"), dict):
            # §18.4 B4: the BlockedStrip's warning on a GitHub-link copy.
            out["panel"]["block_warn"] = _block_view(f.get("block_warn"), warn=True)
        return out
    if kind == "marketplace":
        out = _marketplace(f, flags)
        if isinstance(out.get("panel"), dict):
            copy = f.get("copy") if isinstance(f.get("copy"), dict) else {}
            out["panel"]["block"] = copy.get("block")
        return out
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


def _github(f: dict, flags: dict) -> dict:
    copy = f.get("copy")
    who = (copy or {}).get("who") or _owner(f.get("repo")) or "the author"
    b = _Buttons()
    if not isinstance(copy, dict):
        b.add("check", "Check now", primary=True)
        return {"panel": _panel(f, who=who), "actions": b.done()}
    if copy.get("pending"):
        return {"panel": _panel(f, kind="pending", who=who, state="pending",
                                sentence="Checking where this app came from…"),
                "actions": []}
    # §16.12 gating: with the panel on (sources), every fix for a state the
    # engine recorded is offered; `changes` gates only the proposal buttons
    # (Propose, Update, Withdraw, Update <who>'s app directly); `remix` gates
    # Remix.
    changes_on = bool(flags.get("changes"))
    remix_on = bool(flags.get("remix"))
    signed_out = _signed_out(f)
    files = _strs(copy.get("changes_files"))
    dirty = bool(copy.get("dirty"))
    has_changes = bool(files) or dirty
    behind = int(copy.get("behind") or 0)
    unproposed = int(copy.get("unproposed") or 0)
    proposal = f.get("proposal") if isinstance(f.get("proposal"), dict) else None
    job = _merge(copy)
    blocked = _strs(copy.get("blocked_files"))
    error = copy.get("sync_error") if isinstance(copy.get("sync_error"), str) else None
    can_push = copy.get("can_push") is True
    panel = _panel(f, who=who, files=files, unproposed=unproposed, merge=job)
    extra: list[str] = []
    state = copy.get("state") or "following"
    lets_bring_in = True
    held_said = False  # the sentence already says Bring in is the user's move

    match_confirm = (f"Your copy becomes {who}'s version. Your changes are kept in "
                     "Activity so you can bring them back.")
    push_confirm = (f"Your changes go straight into {who}'s app, without a proposal for "
                    f"{who} to look at first.")

    def discard_button():
        b.add("discard", "Discard my changes", danger=True,
              confirm=(f"Your changes to this app go away and it goes back to {who}'s "
                       "version. fused keeps a copy you can bring back from Activity."))

    def remix_button():
        if remix_on and not signed_out:
            b.add("remix", "Publish as remix", dialog="remix")

    def match_button():
        b.add("match", f"Use {who}'s version", danger=True, confirm=match_confirm)

    def push_button():
        if changes_on and can_push and not signed_out:
            b.add("push-main", f"Update {who}'s app directly", confirm=push_confirm)

    def send_buttons(label: str):
        """Propose (or Sign in first), Remix, the collaborator's direct
        update, Discard — what a copy with changes of its own offers."""
        if changes_on:
            if signed_out:
                b.add("sign-in", "Sign in to GitHub", primary=True, dialog="sign-in")
                extra.append("Sign in to GitHub to send your proposal.")
            else:
                b.add("propose", label, primary=True, dialog="propose")
        remix_button()
        push_button()
        discard_button()

    if job is not None and job.get("status") in _OPEN_JOB:
        if job.get("status") == "ready":
            summary = (job.get("summary") or "").strip()
            sentence = "Claude combined both versions." + (f" {summary}" if summary else "")
        else:
            sentence = "Claude is combining both versions…"
        panel.update(state="merging", sentence=sentence, files=_strs(job.get("files")) or files)
        # Keep / Discard the merge live on the merge review (`panel.merge`,
        # merge_job's routes) — nothing else may move this copy meanwhile.
        return {"panel": panel, "actions": b.done()}

    if copy.get("off_default") or state == "other-branch":
        # §17.1 I4: HEAD is not the copy's own branch.
        lets_bring_in = False
        state = "other-branch"
        sentence = f"This copy is showing a different version of the app than {who}'s usual one"
        sentence += (f", which has {_plural(behind, 'newer change')}." if behind else ".")
        b.add("go-back-main", "Go back to the usual version of this app", primary=True)
    elif copy.get("needs_merge") or (job is not None and job.get("status") == "choice"):
        # §17.2 `needs-merge`: merging the author's latest into this copy
        # overlaps it (`conflict`, derived — nothing recorded). A "choice"
        # job is open (it holds syncs) while it waits for a pick.
        lets_bring_in = False
        nm_files = _strs(copy.get("needs_merge_files"))
        where = f": {_file_words(nm_files)}" if nm_files else ""
        state = "needs-merge"
        panel["files"] = nm_files or files
        if job is not None and job.get("status") == "choice":
            # A file removed on one side and changed on the other, or not
            # text: Claude can't combine it — the person picks a side, per
            # file first (the rest of the merge carries on).
            panel["files"] = _strs(job.get("files")) or panel["files"]
            fw = _file_words(panel["files"]) or "these files"
            sentence = (job.get("error") or "").strip() or (
                f"{who} and you changed {fw} in ways that can't be combined.")
            b.add("choose-mine", f"Keep mine for {fw}", primary=True)
            b.add("choose-theirs", f"Use {who}'s for {fw}")
        else:
            sentence = f"{who} changed the same parts you did{where}."
            failed = job is not None and job.get("status") == "failed"
            b.add("merge-claude", "Merge with Claude again" if failed else "Merge with Claude",
                  primary=True)
            if failed and (job.get("error") or "").strip() == _moved_message():
                # §17.4: Keep found the copy moved since the job started —
                # nothing was combined wrongly; it just has to run again.
                sentence += f" {_moved_message()}"
            elif failed:
                sentence += " Claude couldn't combine them last time."
        match_button()
    elif blocked:
        lets_bring_in = False
        state = "blocked"
        panel["files"] = blocked
        verb = "is" if len(blocked) == 1 else "are"
        sentence = f"{_file_words(blocked)} {verb} in the way of {who}'s update."
        b.add("move-aside", "Move my file aside and update", primary=True)
    elif state == "merged":
        if behind and (copy.get("auto_sync") is False or copy.get("kept_mine")):
            # Automatic updates off, or the user undid one (§17.3: buttons
            # only) — neither opening the app nor Check now moves anything;
            # Bring in (offered below) does.
            sentence = f"{who} merged your change. Bring it in when you're ready."
            held_said = True
        elif behind:
            # Not in yet (Claude was busy): the next sync or Bring in brings
            # it, then settles onto the author's version.
            sentence = (f"{who} merged your change. It comes into your copy the next time "
                        "you open the app, or when you press Check now.")
        else:
            sentence = f"{who} merged your change. You're on {who}'s latest version."
        if has_changes:
            # Edits made after the proposal stay; they can go in a new one.
            extra.append(f"Your newer changes: {_plural(len(files) or 1, 'file')}.")
            send_buttons(f"Propose to {who}")
    elif state == "declined" and changes_on:
        sentence = f"{who} declined your proposal. Your changes are still here."
        send_buttons("Propose again")
        # Leaves the closed proposal behind (nothing moves); the copy reads
        # as plain changes again. Proposal UI, so `changes`-gated with it.
        b.add("keep-working", "Keep working on it")
    elif state == "proposed" and changes_on and proposal is not None:
        comment = proposal.get("comment") if isinstance(proposal.get("comment"), dict) else None
        asked = bool(proposal.get("asked_update"))
        unfit = proposal.get("fits") is False
        if asked:
            sentence = f"{who} asked you to update your proposal."
        elif unfit:
            sentence = f"Your proposal no longer fits {who}'s latest."
        elif proposal.get("state") == "commented" and comment and comment.get("body"):
            sentence = f'{comment.get("author") or who} commented: "{comment["body"]}"'
        else:
            sentence = f"Waiting for {who}."
        if unproposed:
            extra.append(f"{_plural(unproposed, 'edit')} not in your proposal.")
        if signed_out:
            b.add("sign-in", "Sign in to GitHub", primary=True, dialog="sign-in")
        else:
            if proposal.get("can_update") and (unproposed or asked or unfit
                                               or proposal.get("head_differs")):
                b.add("update-proposal", "Update proposal", primary=True)
            elif proposal.get("can_update") and _strs(f.get("untracked")):
                # New files the proposal could carry (its sheet lists them
                # unticked) — offered, never the big button.
                b.add("update-proposal", "Add new files to your proposal")
                b.add("check", "Check now", primary=True)
            if proposal.get("can_withdraw"):
                b.add("withdraw", "Withdraw")
        remix_button()
        push_button()
        # The page offers "also withdraw my proposal" with it (args.withdraw).
        discard_button()
    elif has_changes:
        state = "changes"
        sentence = (f"Your changes: {_plural(len(files) or 1, 'file')}." if changes_on
                    else "You have your own changes in this app.")
        send_buttons(f"Propose to {who}")
    else:
        state = "following"
        sentence = (f"{who} has a newer version." if behind else
                    f"Following {who}'s latest.")

    if lets_bring_in and behind > 0:
        if copy.get("kept_mine") and not held_said:
            # §17.2 undo hold: the user undid an automatic bring-in (or ff,
            # or settle) and the author's tip has not moved since — nothing
            # comes in on its own until they ask.
            extra.append(f"You undid that; {who}'s latest is waiting.")
        b.add("bring-in", f"Bring in {who}'s latest")
    if copy.get("claude_busy"):
        extra.append("Claude is still working on this app.")
    if error:
        extra.append(error)
        if state == "following":
            state = "error"
        b.add("retry", "Try again", primary=not any(a["primary"] for a in b.items))
        if copy.get("sync_error_code") == "auth" and signed_out:
            b.add("sign-in", "Sign in to GitHub", dialog="sign-in")
    if not f.get("owner_checked", True) and signed_out:
        extra.append("Sign in so fused can tell whether this app is yours.")
        b.add("sign-in", "Sign in to GitHub", dialog="sign-in")
    # The foot's check names what it looks for, so nobody waits on a check
    # that cannot answer their question (§16.13).
    b.add("check", (f"Check for {who}'s reply" if state in ("proposed", "merged")
                    else f"Check for {who}'s latest"))
    if b.has("propose") or b.has("update-proposal"):
        panel["untracked"] = _strs(f.get("untracked"))
    panel.update(state=state, sentence=" ".join([sentence, *extra]).strip())
    return {"panel": panel, "actions": b.done()}


def _replace(panel: dict, old: str, new: str) -> None:
    panel["sentence"] = (panel.get("sentence") or "").replace(old, new, 1)


def _block_sentence(block: dict) -> str:
    reason = (block.get("reason") or "").strip().rstrip(".")
    return "Fused blocked the version you were running" + (f": {reason}." if reason else ".")


def _todo(block: dict) -> str:
    todo = (block.get("what_to_do") or "").strip()
    return f" {todo}" if todo else ""


def _marketplace(f: dict, flags: dict) -> dict:
    """A marketplace copy's panel (§18.4): `_github`'s — the same states,
    moves and buttons, the mirror holding the approved commit — plus the
    states it adds (`blocked-release`, `soft-blocked`, `delisted`), B5's
    words, the Check label "Check for a new release" (the route forces the
    catalog and the block list first, so it is never a no-op), and the
    explicit switch to following the author (`switch-link`, confirm)."""
    copy = f.get("copy") if isinstance(f.get("copy"), dict) else None
    who = (copy or {}).get("who") or _owner(f.get("repo")) or "the author"
    if copy is None or copy.get("pending"):
        out = _github(f, flags)
        if out["panel"]:
            out["panel"]["kind"] = "marketplace"
        return out
    block = copy.get("block") if isinstance(copy.get("block"), dict) else {}
    state = copy.get("state")
    job = _merge(copy)
    if state == "blocked-release":
        b = _Buttons()
        panel = _panel(f, kind="marketplace", who=who, state="blocked-release", merge=job)
        if job is not None and job.get("status") in _OPEN_JOB:
            panel.update(state="merging", sentence="Claude is rolling back the blocked version "
                                                   "and keeping your changes…")
            return {"panel": panel, "actions": b.done()}
        if block.get("no_target"):
            sentence = (_block_sentence(block) + " There's no earlier reviewed version to go back "
                        "to, so the app is stopped." + _todo(block))
            b.add("remove-app", "Remove this app", primary=True, danger=True,
                  dialog="remove-app")
        elif block.get("conflict"):
            sentence = (_block_sentence(block) + " Your changes overlap it, so the app is "
                        "stopped until Claude rolls it back with your changes kept."
                        + _todo(block))
            failed = job is not None and job.get("status") == "failed"
            b.add("merge-claude", "Merge with Claude again" if failed else "Merge with Claude",
                  primary=True)
        else:
            sentence = (_block_sentence(block) + " fused is rolling it back to the reviewed "
                        "version." + _todo(block))
        b.add("check", "Check for a new release")
        panel["sentence"] = sentence
        return {"panel": panel, "actions": b.done()}
    if state == "soft-blocked":
        b = _Buttons()
        reason = (block.get("reason") or "").strip().rstrip(".")
        sentence = ("Fused paused updates to this app" + (f": {reason}." if reason else ".")
                    + _todo(block) + " Open it only if you trust this version.")
        b.add("confirm-block", "Open it anyway", primary=True)
        b.add("switch-link", "Follow the author directly",
              confirm=SWITCH_LINK_CONFIRM.format(who=who))
        b.add("check", "Check for a new release")
        return {"panel": _panel(f, kind="marketplace", who=who, state="soft-blocked",
                                sentence=sentence), "actions": b.done()}
    out = _github(f, {**flags})
    panel, acts = out["panel"], out["actions"]
    if panel is None:
        return out
    panel["kind"] = "marketplace"
    approved = copy.get("approved") if isinstance(copy.get("approved"), dict) else {}
    when = _release_date(approved.get("at"))
    p = f.get("record_proposal") or {}
    following = f"Following {who}'s latest."
    if state == "delisted" and panel["state"] == "following":
        panel["state"] = "delisted"
        _replace(panel, following, _delisted())
    elif panel["state"] == "following":
        _replace(panel, f"{who} has a newer version.", f"{who} has a new release.")
        _replace(panel, following, f"On {who}'s release of {when}." if when
                 else f"On {who}'s reviewed version.")
    elif (copy.get("listing") or {}).get("status") == "delisted":
        panel["sentence"] = f"{_delisted()} {panel['sentence']}"
    if panel["state"] == "merged" and not copy.get("released"):
        # B5: Alice merged Bob's change; it reaches him with her next release
        # (nothing to bring in until then — `_github`'s merged sentences all
        # assume the author's branch moved).
        ships = f"{who} merged your change. It ships in {who}'s next release."
        for said in (f"{who} merged your change. Bring it in when you're ready.",
                     f"{who} merged your change. It comes into your copy the next time you "
                     "open the app, or when you press Check now.",
                     f"{who} merged your change. You're on {who}'s latest version."):
            _replace(panel, said, ships)
        acts = [a for a in acts if a["id"] != "bring-in"]
    if copy.get("live") or p.get("live"):
        login = ((f.get("github") or {}).get("login") or "").lower()
        rels = (f.get("row") or {}).get("releases") if isinstance(f.get("row"), dict) else None
        credits = [c.lower() for c in ((rels or [{}])[0].get("credits") or [])
                   if isinstance(c, str)] if rels and isinstance(rels[0], dict) else []
        panel["sentence"] += " Your change is live for everyone." + (
            " The release notes credit you." if login and login in credits else "")
    for a in acts:
        if a["id"] == "check" and panel["state"] != "proposed":
            a["label"] = "Check for a new release"
        elif a["id"] == "bring-in":
            a["label"] = f"Bring in {who}'s release"
    if panel["state"] not in ("merging",):
        acts.append({"id": "switch-link", "label": f"Follow {who} directly", "primary": False,
                     "confirm": SWITCH_LINK_CONFIRM.format(who=who)})
    return {"panel": panel, "actions": acts}


def _delisted() -> str:
    return ("No longer in the Community. Your copy keeps working on its last reviewed "
            "version.")


def _release_date(at) -> str:
    try:
        from fused_render.contrib import market

        return market.release_date(at)
    except Exception:  # noqa: BLE001
        return ""


def _block_view(block, *, warn=False) -> dict | None:
    if not isinstance(block, dict):
        return None
    try:
        from fused_render.contrib import blocks

        view = blocks.view(block)
    except Exception:  # noqa: BLE001
        return None
    if view is not None and warn:
        view["warn"] = True
    return view


def _switch_reviewed(f: dict, out: dict) -> None:
    """A github copy whose repo has a Marketplace row may switch to reviewed
    releases (§18.4 D13, confirm); and a block on a version it runs is a
    warning only (B4: it follows the author directly)."""
    panel = out.get("panel")
    if not isinstance(panel, dict):
        return
    who = panel.get("who") or _owner(f.get("repo")) or "the author"
    warn = (f.get("copy") or {}).get("block_warn") if isinstance(f.get("copy"), dict) else None
    warn = warn if isinstance(warn, dict) else f.get("block_warn")
    if isinstance(warn, dict):
        reason = (warn.get("reason") or "").strip().rstrip(".")
        panel["sentence"] = (f"{panel.get('sentence') or ''} Fused blocked a version of this app "
                             "that you're running" + (f": {reason}." if reason else ".")
                             + f" You follow {who} directly, so nothing was changed.").strip()
    if isinstance(f.get("row"), dict) and panel.get("state") not in ("merging", "pending"):
        out["actions"].append({"id": "switch-reviewed", "label": "Switch to reviewed releases",
                               "primary": False,
                               "confirm": SWITCH_REVIEWED_CONFIRM.format(who=who)})


#: Labels for the fixes ownsync names per state (`ownsync.FIXES`).
_OWN_FIXES = {
    "publish-now": dict(label="Publish now"),
    "check": dict(label="Check now"),
    "merge-claude": dict(label="Merge with Claude"),
    "use-github": dict(label="Use the version on GitHub", danger=True,
                       confirm=USE_GITHUB_CONFIRM),
    "put-on-top": dict(label="Put my edits on top"),
    "publish-over": dict(label="Publish mine over it", danger=True),  # + ownsync's confirm
    "save-edits": dict(label="Save my edits and continue"),
    "bring-in": dict(label="Bring in and publish"),  # `kept`: an undone combine
}


def _own(f: dict, flags: dict) -> dict:
    original = f.get("remix_of")
    b = _Buttons()
    kind = "remix" if original else "own"
    who = _owner(original) if original else None
    if not f.get("repo"):
        return {"panel": None, "actions": []}  # an app of their own, not on GitHub
    own = f.get("own")
    job = f.get("own_merge") if isinstance(f.get("own_merge"), dict) else None
    if not isinstance(own, dict):
        b.add("check", "Check now", primary=True)
        return {"panel": _panel(f, kind=kind, who=who, merge=job), "actions": b.done()}
    unpublished = int(own.get("unpublished") or 0)
    state = own.get("state") or "published"
    panel = _panel(f, kind=kind, who=who, files=_strs(own.get("files")),
                   unpublished=unpublished, github_new=int(own.get("github_new") or 0),
                   merge=job)
    sentence = own.get("message") if isinstance(own.get("message"), str) else ""
    extra: list[str] = []

    if job is not None and job.get("status") in _OPEN_JOB:
        # Keep / Discard live on the merge review (`panel.merge`).
        if job.get("status") == "ready":
            summary = (job.get("summary") or "").strip()
            sentence = "Claude combined both versions." + (f" {summary}" if summary else "")
        else:
            sentence = (sentence if state == "merging" else "") \
                or "Claude is combining both versions…"
        panel.update(state="merging", sentence=sentence, files=_strs(job.get("files")))
        return {"panel": panel, "actions": []}
    status = job.get("status") if job is not None else None
    why = ((job or {}).get("error") or "").strip()
    if status == "choice":
        # Removed on one side and changed on the other, or not text: Claude
        # isn't asked; `error` says which in plain words.
        panel["files"] = _strs(job.get("files")) or panel["files"]
        fw = _file_words(panel["files"]) or "these files"
        extra.append(why or "These versions can't be combined automatically.")
        theirs = "GitHub's" if job.get("purpose") == "own-sync" else f"{who}'s"
        b.add("choose-mine", f"Keep mine for {fw}", primary=True)
        b.add("choose-theirs", f"Use {theirs} for {fw}")
        if job.get("purpose") == "own-sync":
            b.add("use-github", **_OWN_FIXES["use-github"])
    elif status == "failed":
        # The overlap it failed on still stands (merge_job.current says so).
        extra.append("Claude couldn't combine them." + (f" {why}" if why else ""))
        if job.get("purpose") == "own-sync":
            b.add("merge-claude", "Merge with Claude again", primary=True)
            b.add("use-github", **_OWN_FIXES["use-github"])
        elif flags.get("remix"):
            b.add("bring-in", "Bring it in again", primary=True)
    for fix in _strs(own.get("fixes")):
        spec = _OWN_FIXES.get(fix)
        if spec is None or (fix == "put-on-top" and not unpublished):
            continue  # nothing of the author's to put on top
        if fix == "merge-claude" and status == "choice":
            continue  # Claude can't combine this one
        if fix == "publish-over":
            spec = {**spec, "confirm": _publish_over_confirm()}
        if fix == "publish-now" and state == "publish-hook":
            # Her own check stopped it: once she has dealt with it, it is a
            # retry of the same publish.
            spec = {**spec, "label": "Try again"}
        # With nothing unpublished, GitHub's version is the safe way out.
        b.add(fix, primary=fix == "use-github" and not unpublished and state == "github-changed",
              **spec)
    if state == "publish-auth" and _signed_out(f):
        b.add("sign-in", "Sign in to GitHub", primary=True, dialog="sign-in")
    if (state in ("published", "github-new") and original and f.get("original_update")
            and flags.get("remix") and status != "choice"):
        state = "original-moved"
        sentence = f"{who} updated the original. Bring it into your remix?"
        b.add("bring-in", "Bring it in", primary=True)
    proposal = f.get("proposal") if isinstance(f.get("proposal"), dict) else None
    if flags.get("changes") and proposal and proposal.get("can_withdraw"):
        # A remix keeps the proposal it sent as a GitHub-link copy: it may be
        # taken back, never updated (the remix carries its own credit line).
        extra.append(f"Your proposal to {who or 'the original'} is still open.")
        b.add("withdraw", "Withdraw")
    try:  # §18.6: Publish to marketplace & co. (WS-PUBLISH's module; never raises)
        from fused_render.contrib import actions_listing
        actions_listing.buttons(f, b)
        said = actions_listing.sentence(f)
        if isinstance(said, str) and said:
            extra.append(said)
    except ImportError:
        pass
    b.add("check", "Check now")
    panel.update(state=state, sentence=" ".join([s for s in (sentence, *extra) if s]).strip())
    return {"panel": panel, "actions": b.done()}


def for_app(app_dir: str, *, copy=_UNSET, proposal=_UNSET, github=_UNSET) -> dict:
    """`{panel, actions}` for the app folder `app_dir` (§16.9). Local only."""
    return decide(facts(app_dir, copy=copy, proposal=proposal, github=github))


def merge_open(app_dir: str) -> bool:
    """A Merge with Claude job that will land in this folder is running or
    waiting for Keep/Discard (an inbox proposal's merge never lands here).
    Undo would move the folder under it, so it is off meanwhile."""
    job = _call("open merge", "merge_job", "for_app", app_dir)
    return isinstance(job, dict) and job.get("land", True) is not False


def activity(app_dir: str) -> list:
    """The record's Activity lines, newest first (`store.activity`), or [] —
    with no Undo while a merge is open here (`merge_open`)."""
    lines = _call("activity", "store", "activity", app_dir)
    if not isinstance(lines, list):
        return []
    if any(isinstance(x, dict) and x.get("undo") for x in lines) and merge_open(app_dir):
        return [{**x, "undo": False} if isinstance(x, dict) else x for x in lines]
    blocked = _safely("blocked undo", _blocked_undo, app_dir, lines) or set()
    if blocked:
        return [{**x, "undo": False} if isinstance(x, dict) and x.get("id") in blocked else x
                for x in lines]
    return lines


def _blocked_undo(app_dir: str, lines: list) -> set:
    """M1 (§18.4): the ids of a marketplace copy's lines whose Undo would land
    on a blocked commit — not offered (`engine.restore` refuses them too).
    Here, not in `store.activity`: it asks git, which the store never does.
    Nothing to ask while the block list has nothing for this app."""
    from fused_render.contrib import blocks, store

    rec = store.get(app_dir)
    if not isinstance(rec, dict) or rec.get("kind") != "marketplace" \
            or not blocks.for_slug(blocks._slug(rec)):
        return set()
    out = set()
    for x in lines:
        if not isinstance(x, dict) or not x.get("undo") or x.get("kind") == store.SWITCH_KIND:
            continue
        entry = store.activity_entry(app_dir, x.get("id")) or {}
        backup = entry.get("backup")
        if isinstance(backup, list):
            backup = next((b for b in backup if isinstance(b, str)
                           and not b.endswith("-wip")), None)
        if backup and blocks.backup_blocked(store.key(app_dir), rec, backup):
            out.add(x.get("id"))
    return out
