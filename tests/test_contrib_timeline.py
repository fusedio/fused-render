"""The collaboration timeline (docs/CONTRIB_LOOP.md §16.16): Activity lines
for what the other people did — dated when it happened, written once —
from the reads fused already makes. The contributor's side runs against
FakeGitHub (`make_world`: Bob's copy of alice/sine); the author's inbox,
the Community listing and the install lines are driven directly."""
from __future__ import annotations

import pytest

from _contrib_gh_world import PAGE, claude_commit, make_world


@pytest.fixture
def w(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def _tl():
    from fused_render.contrib import timeline
    return timeline


def _p():
    from fused_render.contrib import proposals
    return proposals


def _lines(w, root=None):
    return w.store.activity(root or w.app)


def _kinds(w, root=None):
    return [a["kind"] for a in _lines(w, root)]


def _send(w):
    claude_commit(w.app, {"index.html": PAGE.replace("blue", "red")}, "red")
    return _p().send(w.app, title="Make the wave red", description="Red is nicer.", note="")


# ------------------------------------------------------------------ the store


def test_store_dates_keys_actor_and_link(w):
    s = w.store
    now = s.activity(w.app)[0]["at"] if s.activity(w.app) else None
    first = s.add_activity(w.app, "a", "now")
    past = s.add_activity(w.app, "b", "an hour ago", at=1_000.0, key="k:1", actor="alice",
                          link="https://github.com/alice/sine/pull/1")
    assert first and past
    assert s.add_activity(w.app, "b", "again", key="k:1") is None   # once per key
    lines = s.activity(w.app)
    assert [a["text"] for a in lines][:1] == ["now"] and lines[-1]["text"] == "an hour ago"
    assert lines[-1]["actor"] == "alice" and lines[-1]["link"].endswith("/pull/1")
    assert "actor" not in lines[0] and "link" not in lines[0]
    # Only github.com pages are linked.
    s.add_activity(w.app, "c", "odd", link="javascript:alert(1)")
    assert "link" not in s.activity(w.app)[0]
    assert now is None or now <= lines[0]["at"]


# ------------------------------------------------ the contributor's proposal


def test_contributor_sees_comments_asks_and_acceptance_when_it_happens(w):
    from fused_render.contrib import gh
    _send(w)
    assert _kinds(w)[0] == "proposal-sent"
    assert _lines(w)[0]["link"] == "https://github.com/alice/sine/pull/1"
    w.fake.comment_as("alice", "alice/sine", 1, "Can you make it darker?")
    w.fake.comment_as("bob", "alice/sine", 1, "sure")            # his own: no line
    w.fake.comment_as("alice", "alice/sine", 1, f"Please update.\n\n{gh.ASK_UPDATE_MARKER}")
    _p().refresh(w.app, force=True)
    lines = _lines(w)
    assert lines[0]["text"] == "alice asked you to update your proposal: “Please update.”"
    assert lines[1]["text"] == "alice commented on your proposal: “Can you make it darker?”"
    assert lines[0]["actor"] == lines[1]["actor"] == "alice"
    _p().refresh(w.app, force=True)
    assert _kinds(w).count("proposal-comment") == 1            # never twice

    w.fake.merge("alice/sine", 1, as_login="alice")
    pr = gh.get_pr("alice/sine", 1)
    _p().refresh(w.app, force=True)
    acc = next(a for a in _lines(w) if a["kind"] == "proposal-accepted")
    assert acc["text"] == ("alice accepted your proposal “Make the wave red”. "
                           "It comes into your copy with the next update.")
    assert acc["at"] == pytest.approx(_tl().iso(pr["merged_at"]))
    assert "undo" not in acc and acc["actor"] == "alice"
    _p().refresh(w.app, force=True)
    assert _kinds(w).count("proposal-accepted") == 1


def test_contributor_sees_a_close_and_a_reopen(w):
    _send(w)
    w.fake.close("alice/sine", 1)
    _p().refresh(w.app, force=True)
    assert _lines(w)[0]["text"] == ("alice closed your proposal “Make the wave red” "
                                    "without taking it.")
    w.fake.reopen("alice/sine", 1)
    _p().refresh(w.app, force=True)
    assert _lines(w)[0]["text"] == "Your proposal “Make the wave red” is open again."


def test_marketplace_copy_hears_the_release_comes_later():
    tl = _tl()
    before = {"number": 3, "title": "T", "cache": {"state": "open"}}
    after = {"number": 3, "title": "T", "url": "https://github.com/alice/x/pull/3",
             "cache": {"state": "merged"}}
    out = tl.proposal_lines({"kind": "marketplace", "repo": "alice/x"}, before, after,
                            {"merged_by": "carol", "merged_at": "2026-10-28T09:00:00Z"}, None,
                            me="bob", local_head=None)
    assert [x["text"] for x in out] == [
        "carol accepted your proposal “T”. It reaches you with alice's next Community release."]
    assert out[0]["key"] == "pr:3:merged"


def test_edits_from_elsewhere_and_no_longer_fits():
    tl = _tl()
    rec = {"kind": "github", "repo": "alice/x"}
    before = {"number": 3, "cache": {"state": "open", "head_sha": "a", "mergeable": True}}
    after = {"number": 3, "cache": {"state": "open", "head_sha": "b", "mergeable": False}}
    kinds = [x["kind"] for x in tl.proposal_lines(rec, before, after, {}, None, me="bob",
                                                   local_head="a")]
    assert kinds == ["proposal-no-fit", "proposal-edited"]
    # Bob's own Update moved the head: no "edited" line.
    assert [x["kind"] for x in tl.proposal_lines(rec, before, after, {}, None, me="bob",
                                                  local_head="b")] == ["proposal-no-fit"]


# ---------------------------------------------------------- the author's inbox


@pytest.fixture
def own(w):
    """Alice's own copy's record (a folder; the inbox calls are driven)."""
    import os
    root = os.path.join(os.path.dirname(w.app), "mine")
    os.makedirs(root)
    w.store.put(root, w.store.new_record("own", repo="alice/sine", branch="main"))
    return w.store.key(root)


def _item(n, head, *, author="bob", title="Make it red"):
    return {"number": n, "head_sha": head, "author": author, "title": title,
            "url": f"https://github.com/alice/sine/pull/{n}",
            "created_at": "2026-10-20T10:00:00Z"}


def test_author_sees_who_proposed_what_and_newer_edits(w, own):
    tl = _tl()
    assert tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine") == 1
    line = _lines(w, own)[0]
    assert line["text"] == "bob proposed “Make it red”."
    assert line["at"] == pytest.approx(tl.iso("2026-10-20T10:00:00Z"))
    assert line["actor"] == "bob" and line["link"].endswith("/pull/1")
    assert tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine") == 0
    assert tl.inbox_listed(own, [_item(1, "b")], repo="alice/sine") == 1
    assert _lines(w, own)[0]["text"] == "bob sent newer edits to “Make it red”."
    # Her own Merge with Claude, pushed onto the proposal: not his.
    tl.inbox_event(own, 1, "combined", author="bob", head="c")
    assert tl.inbox_listed(own, [_item(1, "c")], repo="alice/sine") == 0


def test_author_sees_what_became_of_a_proposal_that_left(w, own, monkeypatch):
    from fused_render.contrib import gh
    tl = _tl()
    tl.inbox_listed(own, [_item(1, "a"), _item(2, "b", author="carol", title="Blue")],
                    repo="alice/sine")
    answers = {
        1: {"state": "closed", "merged": True, "merged_by": "dave", "user": "bob",
            "title": "Make it red", "merged_at": "2026-10-21T08:00:00Z"},
        2: {"state": "closed", "merged": False, "user": "carol", "title": "Blue",
            "closed_at": "2026-10-22T08:00:00Z"},
    }
    monkeypatch.setattr(gh, "get_pr", lambda repo, n: answers[n])
    monkeypatch.setattr(gh, "pr_comments", lambda repo, n: [
        {"author": "carol", "body": "carol withdrew this proposal.", "at": ""}])
    assert tl.inbox_listed(own, [], repo="alice/sine") == 2
    texts = [a["text"] for a in _lines(w, own)]
    assert texts[:2] == ["carol withdrew their proposal “Blue”.",
                         "dave merged bob's proposal “Make it red” on GitHub."]


def test_accept_here_is_not_said_again_by_the_listing(w, own, monkeypatch):
    from fused_render.contrib import gh
    tl = _tl()
    tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine")
    tl.inbox_event(own, 1, "accepted", author="bob")
    monkeypatch.setattr(gh, "get_pr", lambda *a: pytest.fail("no need to ask GitHub"))
    assert tl.inbox_listed(own, [], repo="alice/sine") == 0
    assert _lines(w, own)[0]["text"] == "Accepted bob's proposal “Make it red”."


def test_unreachable_github_asks_again_next_listing(w, own, monkeypatch):
    from fused_render.contrib import gh
    tl = _tl()
    tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine")

    def offline(*a):
        raise gh.GitHubError("offline")
    monkeypatch.setattr(gh, "get_pr", offline)
    assert tl.inbox_listed(own, [], repo="alice/sine") == 0
    assert "1" in w.store.get(own)["inbox_seen"]


def test_a_listing_racing_merge_with_claude_keeps_ours(w, own, monkeypatch):
    """Bugbot: a listing that read the record before Merge with Claude
    recorded `ours` must neither drop it nor credit the sender with that
    head."""
    tl = _tl()
    tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine")
    stale = w.store.get(own)                       # what the in-flight listing read
    tl.inbox_event(own, 1, "combined", author="bob", head="c")
    assert w.store.get(own)["inbox_seen"]["1"]["ours"] == "c"
    real = w.store.get
    with monkeypatch.context() as m:
        m.setattr(w.store, "get", lambda d: stale if d == own else real(d))
        assert tl.inbox_listed(own, [_item(1, "c")], repo="alice/sine") == 0
    assert w.store.get(own)["inbox_seen"]["1"]["ours"] == "c"
    assert "inbox-updated" not in _kinds(w, own)


def test_the_authors_own_steps_carry_no_actor(w, own, monkeypatch):
    """Bugbot: only someone else's step has an `actor` (the page marks it)."""
    from fused_render.contrib import gh
    tl = _tl()
    tl.inbox_listed(own, [_item(1, "a"), _item(3, "x", title="Blue")], repo="alice/sine")
    monkeypatch.setattr(gh, "identity", lambda *a, **k: {"login": "alice"})
    monkeypatch.setattr(gh, "get_pr", lambda repo, n: {
        "state": "closed", "merged": True, "merged_by": "alice", "user": "bob",
        "title": "Blue", "merged_at": "2026-10-21T08:00:00Z"})
    tl.inbox_listed(own, [_item(1, "a")], repo="alice/sine")
    mine = next(a for a in _lines(w, own) if a["text"].startswith("You merged"))
    assert "actor" not in mine
    for what in ("tried", "asked", "combined", "declined", "accepted"):
        tl.inbox_event(own, 1, what, author="bob", head="h")
    tl.reviewed(own, 1, {"user": "bob", "title": "Make it red",
                         "url": "https://github.com/alice/sine/pull/1"},
                {"verdict": "fine", "summary": "Fine.", "head_sha": "h"})
    own_kinds = {"inbox-tried", "inbox-asked", "inbox-merged", "inbox-declined",
                 "inbox-accepted", "inbox-reviewed"}
    steps = [a for a in _lines(w, own) if a["kind"] in own_kinds]
    assert {a["kind"] for a in steps} == own_kinds
    assert not [a for a in steps if "actor" in a], steps
    # Someone else's step still names them.
    opened = next(a for a in _lines(w, own) if a["kind"] == "inbox-opened")
    assert opened["actor"] == "bob"
    # The contributor's side: "You merged your proposal" is theirs too.
    out = tl.proposal_lines({"kind": "github", "repo": "alice/x"},
                            {"number": 5, "cache": {"state": "open"}},
                            {"number": 5, "title": "T", "cache": {"state": "merged"}},
                            {"merged_by": "bob"}, None, me="bob", local_head=None)
    assert out[0]["text"].startswith("You merged") and "actor" not in out[0]


def test_review_line(w, own):
    _tl().reviewed(own, 4, {"user": "bob", "title": "Red", "url": "https://github.com/a/b/pull/4"},
                   {"verdict": "look", "summary": "It reads your files.", "head_sha": "h"})
    assert _lines(w, own)[0]["text"] == ("Claude read bob's proposal “Red” and thinks it "
                                         "needs a look. “It reads your files.”")


# ----------------------------------------------------- the Community listing


def test_listing_lines_follow_the_request_through_review():
    tl = _tl()
    sent = {"name": "Sine", "pr": {"number": 7, "state": "open", "kind": "list",
                                   "head_sha": "h1", "url": "https://github.com/i/x/pull/7"},
            "review": {"status": "in-review", "sha": "h1"}, "row": None, "checked_at": 1}
    assert [x["text"] for x in tl.listing_lines({}, sent)] == [
        "Sent Sine to the Community for review."]
    failed = {**sent, "review": {"status": "failed", "sha": "h1",
                                 "reasons": ["It sends data to a server."]}}
    assert [x["text"] for x in tl.listing_lines(sent, failed)] == [
        "The Community review turned down Sine. It sends data to a server."]
    again = {**sent, "pr": {**sent["pr"], "head_sha": "h2"},
             "review": {"status": "needs-confirmation", "sha": "h2",
                        "new_powers": [{"kind": "net", "detail": "Talks to api.example.com"}]}}
    assert [x["text"] for x in tl.listing_lines(failed, again)] == [
        "Sent a newer version of Sine for review.",
        "The Community review needs your OK: Sine asks for new powers "
        "(Talks to api.example.com)."]
    live = {"name": "Sine", "pr": None, "review": None, "row": {"commit": "c1"},
            "approved": {"commit": "c1", "at": "2026-10-25T12:00:00Z", "whats_new": "Red"},
            "checked_at": 2}
    got = tl.listing_lines(again, live)
    assert [x["text"] for x in got] == ["Sine is live in the Community. What's new: “Red”"]
    assert got[0]["at"] == pytest.approx(tl.iso("2026-10-25T12:00:00Z"))
    assert tl.listing_lines(live, {**live, "checked_at": 3}) == []
    out = {**live, "row": None, "approved": None}
    assert [x["kind"] for x in tl.listing_lines(live, out)] == ["listing-out"]
    # A read that failed says nothing.
    assert tl.listing_lines(live, {**out, "error": "offline"}) == []


# ------------------------------------------------- installs and the catalog


def test_install_lines():
    tl = _tl()
    gh_line = tl.install_line({"kind": "github", "repo": "alice/sine"})
    assert gh_line["text"] == ("Installed alice's app from GitHub (alice/sine). You'll get "
                               "alice's changes as they publish them.")
    mk = tl.install_line({"kind": "marketplace", "repo": "alice/sine",
                          "approved": {"at": "2026-10-26T00:00:00Z", "version": "1.2"}})
    assert mk["text"] == "Installed alice's app from the Community, version 1.2 (released Oct 26)."
    assert tl.install_line({"kind": "own", "repo": None}) is None


def test_record_install_writes_the_first_line(w, tmp_path):
    from fused_render.contrib import classify
    import subprocess
    d = tmp_path / "fresh"
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin",
                    "https://github.com/alice/sine"], check=True)
    classify.record_install(str(d), "https://github.com/alice/sine", kind="marketplace",
                            market={"slug": "sine", "approved": {"commit": "a" * 40}})
    assert _kinds(w, w.store.key(str(d))) == ["installed"]


def test_delisted_and_back(w, monkeypatch):
    from fused_render.contrib import market
    w.store.update(w.app, lambda r: r.update(kind="marketplace", slug="sine",
                                             approved={"commit": "a" * 40}))
    monkeypatch.setattr(market, "approved_for", lambda rec, **k: market.Approved(
        commit=None, at=None, whats_new=None, version=None, status="delisted"))
    market.refresh_approved(w.app)
    market.refresh_approved(w.app)
    assert _kinds(w).count("delisted") == 1
    monkeypatch.setattr(market, "approved_for", lambda rec, **k: market.Approved(
        commit="a" * 40, at=None, whats_new=None, version=None, status="live", slug="sine"))
    market.refresh_approved(w.app)
    assert _lines(w)[0]["text"] == "alice put this app back in the Community."
