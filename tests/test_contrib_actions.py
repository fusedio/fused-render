"""Tests for contrib/actions.py — the ONE place that decides which Source
panel buttons exist (docs/CONTRIB_LOOP.md §16.9).

A table over `decide`: literal facts (kind × state × flags × sign-in) →
the exact action ids, primary first. `decide` is pure, so nothing here
touches git, GitHub or the store; the facts are shaped like the real
`engine.status` / `ownsync.status` answers (an own app's fixes come from
`ownsync.FIXES` itself). Pinned besides the ids: one primary at most, a fix
button in every stuck state (P6), confirm sentences on the buttons that
replace or publish, the author's login in labels, flags filtering buttons
(P1), no git words in anything a person reads (§0.1) — and that every
function actions.py and the router call exists with the keywords they pass
(the wrappers there turn a missing one into a plain refusal, which would
hide an integration bug).
"""
import inspect
import re

import pytest

from fused_render.contrib import actions, ownsync

ALL = {"sources": True, "changes": True, "inbox": True, "loop": True, "remix": True}
A1 = {"sources": True, "changes": False, "inbox": False, "loop": False, "remix": False}
NO_REMIX = {**ALL, "remix": False}
IN = {"signed_in": True, "login": "bob", "checking": False}
OUT = {"signed_in": False, "login": None, "checking": False}


def gh(state="following", *, flags=ALL, github=IN, proposal=None, owner_checked=True,
       untracked=(), **copy):
    """Facts for a GitHub-link copy; `copy` overrides engine.status fields."""
    c = {"kind": "github", "state": state, "repo": "alice/x", "who": "alice",
         "author": "alice", "changes_files": [], "changes": 0, "dirty": False,
         "unproposed": 0, "behind": 0, "needs_merge": False, "needs_merge_purpose": None,
         "needs_merge_files": [], "merge_job": None, "blocked_files": [],
         "kept_mine": False, "off_default": False, "claude_busy": False,
         "last_checked": 1000.0, "auto_sync": True, "can_push": None, "owner_checked": True,
         "sync_error": None, "sync_error_code": None, "pending": False}
    c.update(copy)
    if state == "needs-merge" and "needs_merge" not in copy:
        c["needs_merge"] = True
    if state == "merged" and "merged_in_tip" not in copy:
        c["merged_in_tip"] = True
    return {"kind": "github", "repo": "alice/x", "remix_of": None,
            "owner_checked": owner_checked, "original_update": False, "flags": flags,
            "github": github, "copy": c, "own": None, "proposal": proposal,
            "untracked": list(untracked), "last_checked": 1000.0}


def own(state="published", *, flags=ALL, github=IN, remix_of=None, original_update=False,
        repo="bob/mine", proposal=None, merge_job=None, **o):
    """Facts for an own app; `o` overrides ownsync.status fields;
    `merge_job` is merge_job.current(purpose own-sync/original)."""
    s = {"state": state, "unpublished": 0, "github_new": 0, "files": [],
         "message": ownsync._message(state, o.get("files") or ()),
         "fixes": list(ownsync.FIXES.get(state, []))}
    s.update(o)
    return {"kind": "own", "repo": repo, "remix_of": remix_of, "owner_checked": True,
            "original_update": original_update, "flags": flags, "github": github,
            "copy": {"kind": "own", "state": "remix" if remix_of else "own",
                     "merge_job": None},
            "own": s, "own_merge": merge_job, "proposal": proposal, "untracked": [],
            "last_checked": 2000.0}


def ids(f):
    return [a["id"] for a in actions.decide(f)["actions"]]


WAITING = {"number": 4, "state": "waiting", "can_update": True, "can_withdraw": True,
           "asked_update": False}
CH = {"changes_files": ["a.py"], "changes": 1}

TABLE = [
    # -- github copy, changes on ------------------------------------------------
    ("following", gh(), ["check"]),
    ("following, behind", gh(behind=2), ["bring-in", "check"]),
    ("changes", gh("changes", **CH), ["propose", "remix", "discard", "check"]),
    ("changes, remix off", gh("changes", flags=NO_REMIX, **CH), ["propose", "discard", "check"]),
    ("changes, signed out", gh("changes", github=OUT, **CH), ["sign-in", "discard", "check"]),
    ("changes, collaborator", gh("changes", can_push=True, **CH),
     ["propose", "remix", "push-main", "discard", "check"]),
    ("changes, behind", gh("changes", behind=1, **CH),
     ["propose", "remix", "discard", "bring-in", "check"]),
    ("changes, kept mine", gh("changes", behind=1, kept_mine=True, **CH),
     ["propose", "remix", "discard", "bring-in", "check"]),
    ("dirty only", gh("changes", dirty=True, changes_files=["a.py"], changes=1),
     ["propose", "remix", "discard", "check"]),
    ("needs-merge update", gh("needs-merge", needs_merge_purpose="update",
                              needs_merge_files=["a"], behind=3, **CH),
     ["merge-claude", "keep-mine", "use-theirs", "check"]),
    ("needs-merge return", gh("needs-merge", needs_merge_purpose="return"),
     ["merge-claude", "use-theirs", "check"]),
    ("needs-merge proposal", gh("needs-merge", needs_merge_purpose="proposal"),
     ["merge-claude", "check"]),
    ("blocked by a file", gh("changes", blocked_files=["data.csv"], behind=1, **CH),
     ["move-aside", "keep-mine", "check"]),
    ("other branch", gh("changes", off_default=True, behind=2, **CH), ["go-back-main", "check"]),
    ("merge job running", gh("needs-merge", merge_job={"id": "m", "status": "running"}), []),
    ("merge job landing", gh("needs-merge", merge_job={"id": "m", "status": "landing"}), []),
    ("merge job ready", gh("needs-merge", merge_job={"id": "m", "status": "ready",
                                                     "summary": "Both."}), []),
    ("merge job failed", gh("needs-merge", needs_merge_purpose="update",
                            merge_job={"id": "m", "status": "failed"}),
     ["merge-claude", "keep-mine", "use-theirs", "check"]),
    ("merge job choice", gh("needs-merge", needs_merge_purpose="update",
                            merge_job={"id": "m", "status": "choice", "files": ["logo.png"],
                                       "error": "logo.png isn't text."}),
     ["choose-mine", "choose-theirs", "keep-mine", "use-theirs", "check"]),
    ("merge job choice, nothing else recorded", gh(merge_job={"id": "m", "status": "choice",
                                                               "files": ["x.bin"]}),
     ["choose-mine", "choose-theirs", "keep-mine", "use-theirs", "check"]),
    ("merge job choice, return", gh("needs-merge", needs_merge_purpose="return",
                                    merge_job={"id": "m", "status": "choice"}),
     ["choose-mine", "choose-theirs", "keep-mine", "use-theirs", "check"]),
    ("merged", gh("merged"), ["check"]),
    # §16.12: edits kept after a merge can go in a new proposal.
    ("merged, kept edits", gh("merged", **CH), ["propose", "remix", "discard", "check"]),
    ("merged, kept edits, A1", gh("merged", flags=A1, **CH), ["discard", "check"]),
    ("declined", gh("declined", **CH), ["propose", "remix", "discard", "check"]),
    # §16.12: Remix and Discard stay offered while proposed (V6).
    ("proposed, nothing new", gh("proposed", proposal=WAITING, **CH),
     ["withdraw", "remix", "discard", "check"]),
    ("proposed, new edits", gh("proposed", unproposed=2, proposal=WAITING, **CH),
     ["update-proposal", "withdraw", "remix", "discard", "check"]),
    ("proposed, asked", gh("proposed", proposal={**WAITING, "asked_update": True}, **CH),
     ["update-proposal", "withdraw", "remix", "discard", "check"]),
    # §16.12 (contrib V1): Update when it no longer fits or GitHub's copy differs.
    ("proposed, no longer fits", gh("proposed", proposal={**WAITING, "fits": False}, **CH),
     ["update-proposal", "withdraw", "remix", "discard", "check"]),
    ("proposed, head differs", gh("proposed", proposal={**WAITING, "head_differs": True},
                                  **CH),
     ["update-proposal", "withdraw", "remix", "discard", "check"]),
    ("proposed, fits unknown", gh("proposed", proposal={**WAITING, "fits": None}, **CH),
     ["withdraw", "remix", "discard", "check"]),
    # Round 2 (a): new untracked files → Update offered as a secondary action.
    ("proposed, new files only", gh("proposed", proposal=WAITING, untracked=["new.csv"], **CH),
     ["update-proposal", "withdraw", "remix", "discard", "check"]),
    ("proposed, signed out", gh("proposed", unproposed=1, proposal=WAITING, github=OUT, **CH),
     ["sign-in", "discard", "check"]),
    ("proposed, collaborator", gh("proposed", can_push=True, proposal=WAITING, **CH),
     ["withdraw", "remix", "push-main", "discard", "check"]),
    ("proposed, remix off", gh("proposed", proposal=WAITING, flags=NO_REMIX, **CH),
     ["withdraw", "discard", "check"]),
    ("sync failed", gh(sync_error="Couldn't reach GitHub.", sync_error_code="offline"),
     ["retry", "check"]),
    ("sync failed, auth, signed out",
     gh(sync_error="Sign in to GitHub again.", sync_error_code="auth", github=OUT),
     ["retry", "sign-in", "check"]),
    ("owner unknown, signed out", gh(owner_checked=False, github=OUT), ["sign-in", "check"]),
    ("sign-in still checking", gh("changes", github={"signed_in": False, "checking": True},
                                  **CH),
     ["propose", "remix", "discard", "check"]),
    ("no status", {**gh(), "copy": None}, ["check"]),
    ("pending status", gh(pending=True), []),
    # -- github copy, changes OFF (§16.12: fixes under sources, no proposal UI) ----
    ("A1 following behind", gh(behind=2, flags=A1), ["bring-in", "check"]),
    ("A1 changes, author ahead (V3)", gh("changes", behind=2, flags=A1, **CH),
     ["bring-in", "discard", "check"]),
    ("A1 changes, collaborator", gh("changes", can_push=True, flags=A1, **CH),
     ["discard", "check"]),
    ("A1 needs-merge", gh("needs-merge", flags=A1, **CH),
     ["merge-claude", "keep-mine", "use-theirs", "check"]),
    ("A1 blocked (V4)", gh(blocked_files=["x"], behind=1, flags=A1),
     ["move-aside", "keep-mine", "check"]),
    ("A1 proposed", gh("proposed", unproposed=1, proposal=WAITING, flags=A1, **CH),
     ["discard", "check"]),
    ("A1 other branch", gh(off_default=True, behind=3, flags=A1), ["go-back-main", "check"]),
    # -- own app (fixes are ownsync.FIXES) ------------------------------------------
    ("own published", own(), ["check"]),
    ("own unpublished", own("unpublished", unpublished=2), ["publish-now", "check"]),
    ("own github-new", own("github-new", github_new=1), ["check"]),
    ("own needs-merge", own("needs-merge", files=["a.py"], unpublished=1),
     ["merge-claude", "use-github", "check"]),
    ("own github-changed", own("github-changed", unpublished=1),
     ["put-on-top", "use-github", "publish-over", "check"]),
    ("own github-changed, nothing of mine", own("github-changed"),
     ["use-github", "publish-over", "check"]),
    ("own edits", own("edits", files=["a.py"]), ["save-edits", "check"]),
    ("own publish failed", own("publish-failed", unpublished=1), ["publish-now", "check"]),
    ("own publish auth, signed out", own("publish-auth", unpublished=1, github=OUT),
     ["sign-in", "publish-now", "check"]),
    ("own merging", own("merging", merge_job={"id": "m", "status": "running",
                                              "purpose": "own-sync"}), []),
    ("own merge job ready", own("needs-merge", merge_job={"id": "m", "status": "ready",
                                                          "purpose": "own-sync"}), []),
    # rev-fe 2: a failed merge names it and offers the fixes again.
    ("own merge failed", own("needs-merge", merge_job={"id": "m", "status": "failed",
                                                       "purpose": "own-sync"}),
     ["merge-claude", "use-github", "check"]),
    # A "choice" job (removed vs changed, or not text): no Claude, pick a side.
    ("own merge choice", own("needs-merge", merge_job={"id": "m", "status": "choice",
                                                       "purpose": "own-sync"}),
     ["choose-mine", "choose-theirs", "use-github", "check"]),
    ("remix bring-in choice", own(remix_of="alice/x", original_update=True,
                                  merge_job={"id": "m", "status": "choice",
                                             "purpose": "original"}),
     ["choose-mine", "choose-theirs", "check"]),
    ("remix bring-in ready", own(remix_of="alice/x", original_update=True,
                                 merge_job={"id": "m", "status": "ready",
                                            "purpose": "original"}), []),
    ("remix bring-in failed", own(remix_of="alice/x", original_update=True,
                                  merge_job={"id": "m", "status": "failed",
                                             "purpose": "original"}),
     ["bring-in", "check"]),
    ("own no tracking yet", own("no-github"), ["check"]),
    # §16.12: held after a github→own upgrade; unknown rewind state.
    ("own held", own("held", unpublished=2), ["publish-now", "check"]),
    ("own kept (undone combine)", own("kept", github_new=1), ["bring-in", "check"]),
    ("remix kept, original moved too", own("kept", remix_of="alice/x", original_update=True),
     ["bring-in", "check"]),
    ("own unknown", own("unknown", unpublished=1), ["publish-now", "use-github", "check"]),
    ("own A1 still fixes", own("github-changed", unpublished=1, flags=A1),
     ["put-on-top", "use-github", "publish-over", "check"]),
    ("own no repo", own(repo=None), []),
    ("own no status", {**own(), "own": None}, ["check"]),
    ("remix original moved", own(remix_of="alice/x", original_update=True), ["bring-in", "check"]),
    ("remix original moved, remix off", own(remix_of="alice/x", original_update=True,
                                            flags=NO_REMIX), ["check"]),
    ("remix with an open proposal", own(remix_of="alice/x", repo="bob/x2",
                                        proposal={**WAITING, "can_update": False}),
     ["withdraw", "check"]),
    ("remix with an open proposal, changes off",
     own(remix_of="alice/x", repo="bob/x2", proposal=WAITING, flags=A1), ["check"]),
    ("remix with a merged proposal", own(remix_of="alice/x", repo="bob/x2",
                                         proposal={**WAITING, "state": "merged",
                                                   "can_withdraw": False}), ["check"]),
    # -- the rest -----------------------------------------------------------------
    ("scratch", {**gh(), "kind": "scratch"}, []),
    ("pending", {"kind": None, "flags": ALL}, []),
    ("sources off", {**gh("changes", **CH), "flags": {**ALL, "sources": False}}, []),
]


@pytest.mark.parametrize("name,f,want", TABLE, ids=[t[0] for t in TABLE])
def test_buttons(name, f, want):
    got = ids(f)
    assert sorted(got) == sorted(want), (name, got)
    out = actions.decide(f)["actions"]
    primaries = [a for a in out if a["primary"]]
    assert len(primaries) == (1 if out else 0), (name, out)
    assert out[0]["primary"] if out else True  # primary first
    assert set(got) <= set(actions.IDS)


@pytest.mark.parametrize("name,f,first", [
    ("changes", gh("changes", **CH), "propose"),
    ("signed out", gh("changes", github=OUT, **CH), "sign-in"),
    ("behind", gh(behind=1), "bring-in"),
    ("needs-merge", gh("needs-merge"), "merge-claude"),
    ("blocked", gh(blocked_files=["x"]), "move-aside"),
    ("own needs-merge", own("needs-merge", unpublished=1), "merge-claude"),
    ("github-changed", own("github-changed", unpublished=1), "put-on-top"),
    ("held", own("held", unpublished=2), "publish-now"),
    ("kept", own("kept", github_new=1), "bring-in"),
    ("choice", gh("needs-merge", merge_job={"id": "m", "status": "choice"}), "choose-mine"),
    ("own choice", own("needs-merge", merge_job={"id": "m", "status": "choice",
                                                 "purpose": "own-sync"}), "choose-mine"),
    ("proposed, new files only", gh("proposed", proposal=WAITING, untracked=["n.csv"], **CH),
     "check"),
    ("proposed, new edits and new files",
     gh("proposed", unproposed=1, proposal=WAITING, untracked=["n.csv"], **CH),
     "update-proposal"),
    ("unknown", own("unknown", unpublished=1), "publish-now"),
    ("github-changed, nothing of mine", own("github-changed"), "use-github"),
    ("sync failed", gh(sync_error="Couldn't reach GitHub."), "retry"),
    ("nothing to do", gh(), "check"),
    # Never a button that drops, takes back or publishes past a confirm.
    ("proposed, nothing new", gh("proposed", proposal=WAITING, **CH), "check"),
    ("proposed, collaborator", gh("proposed", can_push=True, proposal=WAITING, **CH), "check"),
    ("remix with an open proposal", own(remix_of="alice/x", proposal=WAITING), "check"),
])
def test_primary(name, f, first):
    assert actions.decide(f)["actions"][0]["id"] == first


STUCK = [
    gh("needs-merge", needs_merge_purpose="update"),
    gh("needs-merge", needs_merge_purpose="return"),
    gh("needs-merge", needs_merge_purpose="proposal"),
    gh(blocked_files=["x"]),
    gh(off_default=True),
    gh(sync_error="Couldn't reach GitHub."),
    own("needs-merge"), own("github-changed"), own("edits"),
    own("publish-failed", unpublished=1), own("publish-auth", unpublished=1),
    {**gh(), "copy": None}, {**own(), "own": None},
]


@pytest.mark.parametrize("f", STUCK)
def test_every_stuck_state_has_a_fix(f):
    """P6: a stuck or failed state names the problem and offers a fix —
    beyond Check now, unless the state itself is unknown (then Check now
    is the fix)."""
    out = actions.decide(f)
    assert out["panel"]["sentence"]
    unknown = out["panel"]["state"] == "unknown"
    assert [a for a in out["actions"] if unknown or a["id"] != "check"]


def test_every_own_fix_has_a_label():
    """ownsync names each state's fixes; every one it can name is a button."""
    named = {fix for fixes in ownsync.FIXES.values() for fix in fixes}
    assert named <= set(actions._OWN_FIXES) | {"check"}


def _button(f, id_):
    return next(a for a in actions.decide(f)["actions"] if a["id"] == id_)


def test_confirm_and_danger():
    nm = gh("needs-merge")
    assert _button(nm, "use-theirs")["confirm"] and _button(nm, "use-theirs")["danger"]
    ch = gh("changes", can_push=True, **CH)
    assert _button(ch, "discard")["confirm"] and _button(ch, "discard")["danger"]
    assert _button(ch, "push-main")["confirm"]
    assert "confirm" not in _button(ch, "propose")
    gc = own("github-changed", unpublished=1)
    assert _button(gc, "publish-over")["confirm"] == ownsync.PUBLISH_OVER_CONFIRM
    assert _button(gc, "publish-over")["danger"]
    assert _button(gc, "use-github")["confirm"]
    assert "confirm" not in _button(gc, "put-on-top")


def test_labels_name_the_author():
    ch = gh("changes", can_push=True, behind=1, **CH)
    assert _button(ch, "propose")["label"] == "Propose to alice"
    assert _button(ch, "bring-in")["label"] == "Bring in alice's latest"
    assert _button(ch, "push-main")["label"] == "Update alice's app directly"
    assert _button(gh("needs-merge"), "use-theirs")["label"] == "Use alice's version"
    assert _button(gh("declined", **CH), "propose")["label"] == "Propose again"
    assert _button(own(remix_of="alice/x", original_update=True), "bring-in")["label"] \
        == "Bring it in"


def test_dialog_buttons():
    ch = gh("changes", **CH)
    assert _button(ch, "propose")["dialog"] == "propose"
    assert _button(ch, "remix")["dialog"] == "remix"
    assert _button(gh("changes", github=OUT, **CH), "sign-in")["dialog"] == "sign-in"
    assert "dialog" not in _button(ch, "discard")


def test_panel_fields():
    f = gh("changes", behind=1, untracked=["new.csv"], changes_files=["a.py", "b.py"],
           changes=2)
    p = actions.decide(f)["panel"]
    assert set(p) == {"kind", "repo", "who", "last_checked", "state", "sentence", "files",
                      "unproposed", "unpublished", "github_new", "untracked", "merge"}
    assert p["kind"] == "github" and p["repo"] == "alice/x" and p["who"] == "alice"
    assert p["state"] == "changes" and p["files"] == ["a.py", "b.py"]
    assert p["last_checked"] == 1000.0 and p["untracked"] == ["new.csv"]
    assert p["sentence"].startswith("Your changes: 2 files.")
    # The untracked list rides only while Propose is offered.
    assert actions.decide(gh(untracked=["new.csv"]))["panel"]["untracked"] == []
    o = actions.decide(own("unpublished", unpublished=3, github_new=1))["panel"]
    assert o["state"] == "unpublished" and o["unpublished"] == 3 and o["github_new"] == 1
    assert o["kind"] == "own" and o["who"] is None and o["last_checked"] == 2000.0
    r = actions.decide(own(remix_of="alice/x", original_update=True))["panel"]
    assert r["kind"] == "remix" and r["who"] == "alice" and r["state"] == "original-moved"
    job = {"id": "m", "status": "ready", "summary": "Kept both.", "files": ["a"]}
    assert actions.decide(gh("needs-merge", merge_job=job))["panel"]["merge"] == job
    assert actions.decide(own("needs-merge", merge_job=job))["panel"]["merge"] == job


def test_nothing_to_show_is_null():
    assert actions.decide({**gh(), "flags": {**ALL, "sources": False}})["panel"] is None
    assert actions.decide(own(repo=None))["panel"] is None


@pytest.mark.parametrize("f,state,words", [
    (gh(), "following", "Following alice's latest."),
    (gh(behind=1), "following", "alice has a newer version."),
    (gh("merged"), "merged", "alice merged your change. You're on alice's latest version."),
    (gh("merged", merged_in_tip=False), "merged",
     "alice merged your change; fused will bring it in shortly."),
    (gh("merged", taken_back=True), "merged", "alice merged your change, then took it back."),
    (gh("needs-merge", merge_job={"id": "m", "status": "choice",
                                  "error": "logo.png isn't text, so it can't be combined."}),
     "needs-merge", "logo.png isn't text, so it can't be combined."),
    (own("needs-merge", merge_job={"id": "m", "status": "choice", "purpose": "own-sync",
                                   "error": "a.py was removed on GitHub."}),
     "needs-merge", "a.py was removed on GitHub."),
    (gh("merged", **CH), "merged", "Your newer changes: 1 file."),
    (gh(off_default=True, behind=3), "other-branch", "which has 3 newer changes."),
    (gh("proposed", proposal={**WAITING, "fits": False}, **CH), "proposed",
     "Your proposal no longer fits alice's latest."),
    (gh("declined", **CH), "declined", "alice declined your proposal."),
    (gh("proposed", proposal=WAITING, **CH), "proposed", "Waiting for alice."),
    (gh("proposed", unproposed=2, proposal=WAITING, **CH), "proposed",
     "2 edits not in your proposal."),
    (gh("proposed", proposal={**WAITING, "state": "commented",
                              "comment": {"author": "alice", "body": "Nice"}}, **CH),
     "proposed", 'alice commented: "Nice"'),
    (gh("proposed", proposal={**WAITING, "asked_update": True}), "proposed",
     "alice asked you to update your proposal."),
    (gh("needs-merge", needs_merge_purpose="update", needs_merge_files=["a.py"]),
     "needs-merge", "alice changed the same parts you did: a.py."),
    (gh(blocked_files=["data.csv"]), "blocked", "data.csv is in the way of alice's update."),
    (gh(off_default=True), "other-branch", "different version"),
    (gh(sync_error="Couldn't reach GitHub."), "error", "Couldn't reach GitHub."),
    (gh(merge_job={"id": "m", "status": "running"}), "merging", "Claude is combining"),
    (gh(merge_job={"id": "m", "status": "ready", "summary": "Kept both."}), "merging",
     "Claude combined both versions. Kept both."),
    (gh("changes", github=OUT, **CH), "changes", "Sign in to GitHub to send your proposal."),
    (gh(claude_busy=True), "following", "Claude is still working on this app."),
    (gh("changes", behind=1, kept_mine=True, **CH), "changes", "alice's latest is waiting"),
    (gh("changes", flags=A1, **CH), "changes", "You have your own changes in this app."),
    (own("needs-merge", files=["a.py"]), "needs-merge", "both change a.py"),
    (own("unpublished", unpublished=1), "unpublished", ownsync.MESSAGES["unpublished"]),
    (own(remix_of="alice/x", original_update=True), "original-moved",
     "alice updated the original. Bring it into your remix?"),
    (own(remix_of="alice/x", proposal=WAITING), "published",
     "Your proposal to alice is still open."),
])
def test_sentences(f, state, words):
    p = actions.decide(f)["panel"]
    assert p["state"] == state
    assert words in p["sentence"]


#: §0.1 plus the frontend's words.ts list (which also bans "main").
_GIT_WORDS = re.compile(r"\b(?:(?i:branch\w*|rebas\w*|fork\w*|commit\w*|push\w*|conflict\w*"
                        r"|origin|sha|main)|HEAD)\b")


def test_no_git_words():
    """§0.1 over every sentence, label and confirm the table produces."""
    for _name, f, _want in TABLE:
        out = actions.decide(f)
        texts = [(out["panel"] or {}).get("sentence") or ""] \
            + [a["label"] for a in out["actions"]] \
            + [a.get("confirm") or "" for a in out["actions"]]
        for t in texts:
            assert not _GIT_WORDS.search(t), (_name, t)


# -- the names actions.py and the router call -------------------------------------

#: (module, function, keywords passed by name) — every call actions.py and
#: server/routers/contrib.py make into another workstream's module.
CALLED = [
    ("store", "get", ()), ("store", "key", ()), ("store", "activity", ()),
    ("store", "ack", ()),
    ("classify", "kind_of", ()), ("classify", "ensure", ()),
    ("flags", "snapshot", ()),
    ("engine", "status", ()), ("engine", "bring_in", ()), ("engine", "keep_mine", ()),
    ("engine", "use_theirs", ()), ("engine", "move_aside", ()),
    ("engine", "go_back_main", ()), ("engine", "discard", ("withdraw",)),
    ("engine", "restore", ()), ("engine", "push_to_main", ()), ("engine", "merge_plan", ()),
    ("ownsync", "status", ()), ("ownsync", "check_now", ()), ("ownsync", "publish_now", ()),
    ("ownsync", "use_github", ()), ("ownsync", "put_on_top", ()),
    ("ownsync", "publish_over", ()), ("ownsync", "save_edits", ()),
    ("ownsync", "start_merge", ()), ("ownsync", "restore", ()), ("ownsync", "bring_in", ()),
    ("merge_job", "start", ("purpose", "ours", "theirs")), ("merge_job", "get", ()),
    ("merge_job", "accept", ()), ("merge_job", "discard", ()),
    ("merge_job", "current", ("purpose",)), ("merge_job", "for_app", ()),
    ("merge_job", "choose", ("side",)),
    ("proposals", "view", ()), ("proposals", "untracked", ()),
    ("proposals", "start_draft", ()),
    ("proposals", "start_send", ("title", "description", "note", "include")),
    ("proposals", "start_update", ("include",)), ("proposals", "withdraw", ()),
    ("inbox", "review", ()), ("inbox", "try_open", ("confirm",)), ("inbox", "try_remove", ()),
    ("inbox", "accept", ("confirm",)), ("inbox", "decline", ()), ("inbox", "ask_update", ()),
    ("inbox", "merge_start", ()), ("inbox", "merge_accept", ()), ("inbox", "owns_merge", ()),
    ("remix", "start_draft", ()), ("remix", "start_publish", ("name", "credit")),
    ("remix", "start_bring_in", ()),
    ("contributors", "list", ()),
]


@pytest.mark.parametrize("module,name,kw", CALLED, ids=[f"{m}.{n}" for m, n, _ in CALLED])
def test_every_called_function_exists(module, name, kw):
    import importlib

    fn = getattr(importlib.import_module(f"fused_render.contrib.{module}"), name, None)
    assert callable(fn), f"{module}.{name} is missing"
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    for k in kw:
        assert k in params, f"{module}.{name} takes no {k}="


def test_git_upstream_names_exist():
    from fused_render import git_upstream

    for name in ("sync_repo", "last_checked"):
        assert callable(getattr(git_upstream, name, None)), name
    assert {"action", "push"} <= set(inspect.signature(git_upstream.sync_repo).parameters)


def test_round2_new_files_and_taken_back():
    """Audit r2: an open proposal with new untracked files offers them via
    a secondary Update (sheet lists them); a merge taken back never claims
    the copy is on the author's latest."""
    f = gh("proposed", proposal=WAITING, untracked=["new.csv"], **CH)
    out = actions.decide(f)
    up = _button(f, "update-proposal")
    assert up["label"] == "Add new files to your proposal" and not up["primary"]
    assert out["panel"]["untracked"] == ["new.csv"]
    p = actions.decide(gh("merged", taken_back=True, merged_in_tip=True))["panel"]
    assert "You're on" not in p["sentence"]


def test_own_merge_reaches_the_panel_through_the_real_registry(tmp_path, monkeypatch):
    """rev-fe 1: engine.status carries a GitHub-link copy's merges only, so an
    own app's Merge with Claude must come from merge_job itself — through
    `facts`, the REAL registry (`merge_job.current`), not literal facts.
    Ready → the review (no other buttons, Undo off); failed → named, with
    Merge with Claude again; an inbox proposal's merge in the same folder
    stays out of the panel."""
    import time

    from fused_render.contrib import classify, flags, merge_job, refresh, store

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = tmp_path / "ws" / "tag" / "mine"
    app.mkdir(parents=True)
    monkeypatch.setattr(flags, "_raw", lambda: dict(ALL))
    rec = store.new_record("own", repo="bob/mine", branch="main", classified_by="migration")
    rec["own_state"] = {"state": "needs-merge"}  # the overlap still stands
    store.put(str(app), rec)
    monkeypatch.setattr(classify, "kind_of", lambda d, **_: "own")
    monkeypatch.setattr(ownsync, "status", lambda d: own("needs-merge")["own"])
    monkeypatch.setattr(refresh, "github_identity", lambda: IN)
    monkeypatch.setattr(actions, "_own_checked", lambda d: None)
    monkeypatch.setattr(store, "activity", lambda d: [
        {"id": "a1", "at": 1, "kind": "own-sync", "text": "Combined.", "undo": True}])
    now = time.time()

    def job(jid, purpose, status, land=True):
        j = {"id": jid, "root": merge_job._canon(str(app)), "purpose": purpose, "ours": "a",
             "theirs": "b", "land": land, "sides": None, "identity": None, "branch": None,
             "new_tip": None, "worktree": None, "status": status, "summary": "Kept both.",
             "files": ["a.py"], "error": None, "started_at": now, "ended_at": None,
             "cancel": False}
        monkeypatch.setitem(merge_job._jobs, jid, j)
        return j

    copy = {"kind": "own", "state": "own", "merge_job": None}
    # An inbox proposal's merge (never lands here) is not the panel's.
    job("inbox", "proposal", "ready", land=False)
    out = actions.for_app(str(app), copy=copy, proposal=None)
    assert out["panel"]["merge"] is None and out["panel"]["state"] == "needs-merge"
    assert actions.activity(str(app))[0]["undo"] is True
    mine = job("own", "own-sync", "ready")
    out = actions.for_app(str(app), copy=copy, proposal=None)
    assert out["panel"]["state"] == "merging" and out["panel"]["merge"]["id"] == "own"
    assert out["panel"]["sentence"] == "Claude combined both versions. Kept both."
    assert out["actions"] == []
    assert actions.activity(str(app))[0]["undo"] is False  # rev-fe 13
    mine.update(status="failed", error="Both sides rewrote the same lines.", ended_at=now)
    out = actions.for_app(str(app), copy=copy, proposal=None)
    assert out["actions"][0]["id"] == "merge-claude"
    assert out["actions"][0]["label"] == "Merge with Claude again"
    assert "Claude couldn't combine them. Both sides rewrote" in out["panel"]["sentence"]
    assert actions.activity(str(app))[0]["undo"] is True


def test_choice_buttons_name_the_files():
    f = gh("needs-merge", merge_job={"id": "m", "status": "choice", "files": ["logo.png"]})
    assert _button(f, "choose-mine")["label"] == "Keep mine for logo.png"
    assert _button(f, "choose-theirs")["label"] == "Use alice's for logo.png"
    assert not _button(f, "keep-mine")["primary"]
    o = own("needs-merge", merge_job={"id": "m", "status": "choice", "purpose": "own-sync",
                                      "files": ["a.py", "b.py"]})
    assert _button(o, "choose-theirs")["label"] == "Use GitHub's for a.py, b.py"
    assert not _button(o, "use-github")["primary"]
