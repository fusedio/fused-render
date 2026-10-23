"""The contribution loop under every way a collaboration can go (CONTRIB_LOOP.md
§16), end to end through the HTTP routes the page uses — on top of the
acceptance harness (`test_contrib_acceptance`: three machines, one FakeGitHub
of real bare repos, the same page invariants on every read).

Where the acceptance test tells one story, each test here is one permutation:
how the author accepts (merge commit, squash, rebase; in fused or on
github.com), what the sender has done since sending (nothing, a commit, an
unsaved edit beside or on top of the proposal, a new file), what happens to
every copy afterwards (the sender returns to the author's latest keeping
exactly the later edits, the author's own copy publishes what she had, a
plain follower fast-forwards), and the ways out of every stuck state (declined,
withdrawn, discarded, taken back, the author rewinding GitHub, two senders on
the same line, a collaborator publishing directly, flags turned off in the
middle, a folder moved on disk). Every step asserts nothing reached GitHub
without a click and no git word reached the page.
"""
from __future__ import annotations

import os
import time

import pytest

from _contrib_gh_world import APP_PY, PAGE, claude_commit, git
from test_contrib_acceptance import (  # noqa: F401 — `world` is the fixture
    ALL_OFF, ALL_ON, W, act, app_open, head, ids, install, louder, moved, no_pushes,
    page, post, read, refs, tip_of, turn_committed, use, wait_job, wait_merge, world, write,
    alice_publish, quiesce,
)

METHODS = ("merge", "squash", "rebase")
REPO = "alice/sine"


# -- helpers --------------------------------------------------------------------------


def setup_world():
    """Alice's own copy; Bob and Carol install from the link (acceptance step 1)."""
    use(W.alice)
    git(W.base, "clone", "-q", f"https://github.com/{REPO}", W.alice.app)
    out = page(W.alice)
    assert out["ok"] and out["panel"]["kind"] == "own", out
    for m in (W.bob, W.carol):
        install(m)
        out = page(m)
        assert out["panel"]["kind"] == "github" and out["panel"]["state"] == "following", out
        assert head(m.app) == tip_of()


def propose(m, files, message, *, title="A change", include=None):
    """A Claude turn then Propose: the PR on alice/sine, its number."""
    before = refs()
    turn_committed(m, files, message)
    no_pushes(before, f"{m.login}'s turn")
    out = page(m)
    assert "propose" in ids(out), (ids(out), out["panel"])
    r = act(m, "propose", title=title, description="Sent from a test.",
            include=list(include or []))
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    changed = moved(before, refs())
    assert changed and all(full == f"{m.login}/sine" for full, _ in changed), changed
    pr = next(p for p in W.fake.state()["pulls"][REPO]
              if p["state"] == "open" and p["user"] == m.login)
    out = page(m)
    assert out["panel"]["state"] == "proposed", out["panel"]
    return pr["number"]


def inbox_item(number, *, reviewed=True, timeout=60):
    """Alice's inbox item for `number`, once its review of the current head
    is ready (reviews run in the background as the page lists)."""
    deadline = time.time() + timeout
    item = None
    while time.time() < deadline:
        out = page(W.alice)
        items = ((out.get("inbox") or {}).get("items")) or []
        item = next((i for i in items if i.get("number") == number), None)
        if item and (not reviewed or (item.get("review_status") == "ready"
                     and (item.get("review") or {}).get("head_sha") == item.get("head_sha"))):
            return item
        time.sleep(0.2)
    raise AssertionError(f"no reviewed inbox item {number}: {item}")


def accept_in_fused(number, *, method):
    """Alice clicks Accept on her Proposals tab; the repo allows only `method`."""
    W.fake.set_merge_methods(REPO, merge=method == "merge", squash=method == "squash",
                             rebase=method == "rebase")
    inbox_item(number)
    done = None
    for _ in range(6):
        r = post(W.alice, "inbox/accept", number=number)
        assert r.get("ok") and r.get("job"), r
        done = wait_job(r["job"])
        if done.get("status") == "done" or done.get("reason") != "review-needed":
            break
        quiesce()
        time.sleep(0.3)
    assert done.get("status") == "done", done
    quiesce()
    pr = next(p for p in W.fake.state()["pulls"][REPO] if p["number"] == number)
    assert pr.get("merged"), pr
    return tip_of()


def accept_on_github(number, *, method):
    """Alice merges on github.com; fused only learns about it."""
    W.fake.merge(REPO, number, method)
    return tip_of()


def pr_row(number):
    return next(p for p in W.fake.state()["pulls"][REPO] if p["number"] == number)


def alice_clean():
    """Alice's copy is on GitHub's tip with no failure row and no stuck state."""
    from fused_render import git_upstream

    use(W.alice)
    assert head(W.alice.app) == tip_of(), (head(W.alice.app), tip_of())
    rows = [r for r in git_upstream.sync_failures()
            if os.path.realpath(str(r.get("root") or r.get("path") or "")) == W.alice.app]
    assert not rows, rows
    out = page(W.alice)
    assert out["panel"]["state"] not in ("github-changed", "needs-merge", "edits"), out["panel"]
    return out


def first_parent_subjects(root, since):
    return git(root, "log", "--first-parent", "--no-merges", "--format=%s", "HEAD",
               f"^{since}").splitlines()


def dirty_paths(root):
    return git(root, "diff", "--name-only", "HEAD").split()


def untracked_paths(root):
    return git(root, "ls-files", "--others", "--exclude-standard").split()


# == the accept matrix ==============================================================
#
# method × where Alice accepts × what Bob did after sending.


AFTER = ("none", "committed", "dirty-beside", "dirty-on-top", "new-file")


def _after_send(kind):
    """What Bob does between sending and the author's accept."""
    before = refs()
    if kind == "committed":
        turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    elif kind == "dirty-beside":        # an unsaved edit the proposal does not touch
        write(W.bob.app, "index.html", PAGE.replace("blue", "teal"))
    elif kind == "dirty-on-top":        # an unsaved edit on the proposed file itself
        write(W.bob.app, "app.py", louder(3).replace("return 2", "return 7"))
    elif kind == "new-file":
        write(W.bob.app, "scratch.txt", "not sent\n")
    no_pushes(before, "Bob's post-send work")


def _check_return(kind, tip, *, method):
    """Bob is on the author's latest with exactly his later work kept."""
    out = page(W.bob)
    p = out["panel"]
    assert p["state"] == "merged", p
    assert "merged your change" in p["sentence"], p
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    diff = git(W.bob.app, "diff", "--name-only", tip, "HEAD").split()
    # §17.10 Q7: a squash/rebase accept never puts Bob's own commit id into
    # main, so while later work keeps the branch off main his sent commit
    # stays in its first-parent history (the next PR's diff is still only
    # the later work). A merge-commit accept has his id in main already.
    sent = [] if method == "merge" else ["Claude: louder wave"]
    if kind == "committed":
        assert diff == ["later.txt"], diff
        assert first_parent_subjects(W.bob.app, tip) == ["Claude: later idea"] + sent
        assert "propose" in ids(out), ids(out)
    elif kind == "dirty-on-top":
        # An unsaved edit on the proposed file. Whatever the accept method,
        # the merged proposal's head is the merge base (§17.3), so the file
        # reads the same on both sides, the copy settles onto the tip and
        # the edit rides along, still unsaved and still Bob's to propose.
        assert "return 7" in read(W.bob.app, "app.py")
        assert "propose" in ids(out), ids(out)
        assert diff == [] and dirty_paths(W.bob.app) == ["app.py"], (diff, dirty_paths(W.bob.app))
        assert first_parent_subjects(W.bob.app, tip) == []
    else:
        assert diff == [], diff
        assert first_parent_subjects(W.bob.app, tip) == []
    if kind == "dirty-beside":
        assert dirty_paths(W.bob.app) == ["index.html"], dirty_paths(W.bob.app)
        assert "teal" in read(W.bob.app, "index.html")
    elif kind == "new-file":
        assert untracked_paths(W.bob.app) == ["scratch.txt"], untracked_paths(W.bob.app)
        assert W.fake.read(REPO, "scratch.txt") is None
    elif kind != "dirty-on-top":
        assert dirty_paths(W.bob.app) == [], dirty_paths(W.bob.app)
    # The proposal's content is in the author's latest, whatever the method.
    assert "return 3" in W.fake.read(REPO, "app.py")
    assert any(x.get("undo") and "merged your change" in x.get("text", "")
               for x in out["activity"]), out["activity"]
    return out


@pytest.mark.parametrize("after", AFTER)
@pytest.mark.parametrize("via", ("fused", "github"))
@pytest.mark.parametrize("method", METHODS)
def test_accept_matrix(world, method, via, after):
    setup_world()
    number = propose(W.bob, {"app.py": louder(3)}, "louder wave", title="Louder wave")
    _after_send(after)
    before = refs()
    tip = (accept_in_fused if via == "fused" else accept_on_github)(number, method=method)
    changed = {k[0] for k in moved(before, refs())}
    assert changed <= {REPO, "bob/sine"}, changed
    before = refs()
    app_open(W.bob)
    _check_return(after, tip, method=method)
    # The merge's shape on GitHub matches the method.
    parents = git(W.bob.app, "rev-list", "--parents", "-1", tip).split()[1:]
    assert len(parents) == (2 if method == "merge" else 1), (method, parents)
    if method == "squash":
        assert git(W.bob.app, "log", "-1", "--format=%an", tip).lower() == "bob"
    # Carol just follows.
    app_open(W.carol)
    assert head(W.carol.app) == tip
    assert page(W.carol)["panel"]["state"] == "following"
    # Alice's copy is on her latest too (via github.com: her next open).
    app_open(W.alice)
    alice_clean()
    no_pushes(before, "the return and the follow")


# == second loops ==================================================================


def test_second_proposal_after_a_return(world):
    """Propose → accept → return → propose again → accept with another
    method → return again: records, branches and credit all fresh."""
    setup_world()
    n1 = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    tip1 = accept_in_fused(n1, method="squash")
    app_open(W.bob)
    assert page(W.bob)["panel"]["state"] == "merged"
    n2 = propose(W.bob, {"index.html": PAGE.replace("blue", "gold")}, "gold", title="Gold")
    assert n2 != n1
    assert pr_row(n2)["head_branch"] != pr_row(n1)["head_branch"]
    from fused_render.contrib import store
    rec = store.get(W.bob.app)
    assert rec["proposal"]["number"] == n2 and not rec["proposal"].get("landed_at"), rec["proposal"]
    tip2 = accept_in_fused(n2, method="rebase")
    assert tip2 != tip1
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) == tip2, out["panel"]
    assert W.fake.read(REPO, "index.html") == PAGE.replace("blue", "gold")
    assert "return 3" in W.fake.read(REPO, "app.py")
    app_open(W.alice)
    alice_clean()


def test_declined_then_propose_again(world):
    setup_world()
    n1 = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    inbox_item(n1)
    r = post(W.alice, "inbox/decline", number=n1, note="Not now, thanks.")
    assert r.get("ok"), r
    assert pr_row(n1)["state"] == "closed" and not pr_row(n1)["merged"]
    bob_head = head(W.bob.app)
    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "declined", out["panel"]
    assert "declined" in out["panel"]["sentence"], out["panel"]
    assert "propose" in ids(out), ids(out)
    assert head(W.bob.app) == bob_head and "return 3" in read(W.bob.app, "app.py")
    no_pushes(before, "a declined proposal")
    # Propose again: a new proposal, the old one stays closed.
    r = act(W.bob, "propose", title="Louder, again", description="Second try.", include=[])
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    open_prs = [p for p in W.fake.state()["pulls"][REPO] if p["state"] == "open"]
    assert len(open_prs) == 1 and open_prs[0]["number"] != n1, open_prs
    n2 = open_prs[0]["number"]
    assert page(W.bob)["panel"]["state"] == "proposed"
    tip = accept_in_fused(n2, method="merge")
    app_open(W.bob)
    assert page(W.bob)["panel"]["state"] == "merged" and head(W.bob.app) == tip


def test_withdraw_then_propose_again(world):
    setup_world()
    n1 = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    branch = pr_row(n1)["head_branch"]
    out = page(W.bob)
    assert "withdraw" in ids(out), ids(out)
    r = act(W.bob, "withdraw")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert pr_row(n1)["state"] == "closed"
    assert W.fake.tip("bob/sine", branch) is None, "the copy on GitHub stayed"
    out = page(W.bob)
    assert out["panel"]["state"] == "changes" and "propose" in ids(out), out["panel"]
    assert "return 3" in read(W.bob.app, "app.py")
    n2 = propose(W.bob, {"index.html": PAGE.replace("blue", "gold")}, "gold", title="Gold")
    assert n2 != n1
    tip = accept_on_github(n2, method="squash")
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) == tip, out["panel"]
    assert "return 3" in W.fake.read(REPO, "app.py") and "gold" in W.fake.read(REPO, "index.html")


def test_discard_with_withdraw_and_undo(world):
    setup_world()
    n1 = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    write(W.bob.app, "index.html", PAGE.replace("blue", "teal"))  # unsaved too
    bob_head = head(W.bob.app)
    out = page(W.bob)
    assert "discard" in ids(out), ids(out)
    r = act(W.bob, "discard", confirm=True, withdraw=True)
    assert r.get("ok"), r
    if r.get("job"):
        assert wait_job(r["job"]).get("status") == "done", r
    quiesce()
    assert pr_row(n1)["state"] == "closed" and not pr_row(n1)["merged"]
    assert head(W.bob.app) == tip_of()
    assert read(W.bob.app, "app.py") == APP_PY and read(W.bob.app, "index.html") == PAGE
    out = page(W.bob)
    assert out["panel"]["state"] == "following", out["panel"]
    line = next(x for x in out["activity"] if x.get("undo") and "iscard" in x["text"])
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok"), r
    assert head(W.bob.app) == bob_head
    assert "return 3" in read(W.bob.app, "app.py")
    assert "teal" in read(W.bob.app, "index.html"), "the unsaved edit was not brought back"
    out = page(W.bob)
    assert out["panel"]["state"] == "changes" and "propose" in ids(out), out["panel"]


# == the author moves while a proposal is open =====================================


def test_update_after_maintainer_edit_then_accept(world):
    """Alice edits the proposal on GitHub (maintainer edits), Bob updates
    (her edit is merged in, never replayed), then she accepts by rebase: the
    return keeps Bob's later edit and her maintainer edit is in the tip."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    branch = pr_row(n)["head_branch"]
    sent = W.fake.tip("bob/sine", branch)
    W.fake.commit("bob/sine", branch, {"README.md": "Alice's note\n"}, "maintainer note",
                  author="alice")
    maint = W.fake.tip("bob/sine", branch)
    turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    out = page(W.bob)
    assert "update-proposal" in ids(out), (ids(out), out["panel"])
    before = refs()
    r = act(W.bob, "update-proposal")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert moved(before, refs()) == {("bob/sine", f"refs/heads/{branch}")}
    new = W.fake.tip("bob/sine", branch)
    assert new == head(W.bob.app)
    git(W.fake.bare_path("bob/sine"), "merge-base", "--is-ancestor", maint, new)
    git(W.fake.bare_path("bob/sine"), "merge-base", "--is-ancestor", sent, new)
    assert read(W.bob.app, "README.md") == "Alice's note\n"
    turn_committed(W.bob, {"even_later.txt": "x\n"}, "even later")
    tip = accept_in_fused(n, method="rebase")
    assert W.fake.read(REPO, "README.md") == "Alice's note\n"
    assert W.fake.read(REPO, "later.txt") == "after sending\n"
    assert W.fake.read(REPO, "even_later.txt") is None
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged", out["panel"]
    assert git(W.bob.app, "diff", "--name-only", tip, "HEAD").split() == ["even_later.txt"]
    assert first_parent_subjects(W.bob.app, tip) == [  # Q7: a rebase accept, later work kept
        "Claude: even later", "Claude: later idea", "Claude: louder"]
    app_open(W.alice)
    alice_clean()


def test_github_update_branch_then_accept_merge(world):
    """GitHub's "Update branch" merges main into Bob's proposal; Bob's copy
    merges that in on Update; a merge-commit accept returns cleanly."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    W.fake.update_branch(REPO, n)
    turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")  # row 11 on the turn
    out = page(W.bob)
    assert "update-proposal" in ids(out), (ids(out), out["panel"])
    r = act(W.bob, "update-proposal")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert W.fake.tip("bob/sine", pr_row(n)["head_branch"]) == head(W.bob.app)
    assert W.fake.read("bob/sine", "later.txt", f"refs/heads/{pr_row(n)['head_branch']}") \
        == "after sending\n"
    tip = accept_in_fused(n, method="merge")
    assert W.fake.read(REPO, "later.txt") == "after sending\n"
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) == tip, out["panel"]
    assert first_parent_subjects(W.bob.app, tip) == []


def test_accept_while_alice_has_an_unsaved_edit_beside(world):
    """Alice accepts with an unsaved edit in her own copy that the proposal
    does not touch: her copy comes up to date, the edit stays unsaved."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    use(W.alice)
    write(W.alice.app, "index.html", PAGE.replace("blue", "pink"))
    tip = accept_in_fused(n, method="merge")
    out = alice_clean()
    assert dirty_paths(W.alice.app) == ["index.html"], dirty_paths(W.alice.app)
    assert "pink" in read(W.alice.app, "index.html")
    assert W.fake.read(REPO, "index.html") == PAGE, "an unsaved edit was published"
    assert head(W.alice.app) == tip


def test_accept_while_alice_has_an_overlapping_unsaved_edit(world):
    """Alice accepts with an UNSAVED edit on the very file the proposal
    changes: her copy must not be left behind with today's "dirty" row.
    It is the `edits` state with "Save my edits and continue"; saving
    carries on into the overlap, which Merge with Claude settles and
    publishes."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    use(W.alice)
    write(W.alice.app, "app.py", louder(9))
    alice_head = head(W.alice.app)
    tip = accept_in_fused(n, method="merge")
    out = page(W.alice)
    p = out["panel"]
    assert p["state"] == "edits", (p, ids(out))
    assert "save-edits" in ids(out), ids(out)
    assert head(W.alice.app) == alice_head and "return 9" in read(W.alice.app, "app.py")
    assert W.fake.tip(REPO) == tip
    r = act(W.alice, "save-edits")
    assert r.get("ok") is not None, r
    quiesce()
    out = page(W.alice)
    assert out["panel"]["state"] == "needs-merge", (out["panel"], ids(out))
    r = act(W.alice, "merge-claude")
    assert r.get("ok") and r.get("job"), r
    j = wait_merge(r["job"])
    assert (j.get("result") or {}).get("status") == "ready", j
    r = post(W.alice, "merge/accept", job=r["job"])
    assert r.get("ok"), r
    quiesce()
    app_open(W.alice)
    alice_clean()
    published = W.fake.read(REPO, "app.py")
    assert "return 3" in published and "return 9" in published, published
    assert dirty_paths(W.alice.app) == []


def test_accept_while_alice_has_an_overlapping_unpublished_commit(world):
    """Alice committed to the same line locally and never published; the
    proposal merges on GitHub, her copy is stuck in plain words with Merge
    with Claude, and keeping Claude's merge publishes the combined version."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    use(W.alice)
    claude_commit(W.alice.app, {"app.py": louder(9)}, "nine")
    alice_edit = head(W.alice.app)
    tip = accept_in_fused(n, method="squash")
    out = page(W.alice)
    p = out["panel"]
    assert p["state"] == "needs-merge", (p, ids(out))
    assert "merge-claude" in ids(out), ids(out)
    assert head(W.alice.app) == alice_edit, "the stuck state moved her copy"
    assert W.fake.tip(REPO) == tip, "something was published from a stuck state"
    r = act(W.alice, "merge-claude")
    assert r.get("ok") and r.get("job"), r
    j = wait_merge(r["job"])
    assert (j.get("result") or {}).get("status") == "ready", j
    r = post(W.alice, "merge/accept", job=r["job"])
    assert r.get("ok"), r
    quiesce()
    app_open(W.alice)
    alice_clean()
    published = W.fake.read(REPO, "app.py")
    assert "return 3" in published and "return 9" in published, published
    git(W.alice.app, "merge-base", "--is-ancestor", alice_edit, "HEAD")
    git(W.alice.app, "merge-base", "--is-ancestor", tip, "HEAD")
    # Bob returns onto the combined version.
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) == tip_of(), out["panel"]


# == two senders ===================================================================


def test_two_senders_on_the_same_line(world):
    """Bob and Carol propose overlapping changes. Alice accepts Bob's; Carol's
    no longer fits, Alice asks her to update; Carol's copy brings in the new
    latest with Merge with Claude, updates, Alice accepts; both return."""
    setup_world()
    nb = propose(W.bob, {"app.py": louder(3)}, "louder", title="Bob louder")
    nc = propose(W.carol, {"app.py": louder(4)}, "louder four", title="Carol louder")
    tip1 = accept_in_fused(nb, method="merge")
    item = inbox_item(nc, reviewed=False)
    deadline = time.time() + 30
    while item.get("fits") is not False and time.time() < deadline:
        time.sleep(0.2)
        item = inbox_item(nc, reviewed=False)
    assert item.get("fits") is False, item
    r = post(W.alice, "inbox/ask-update", number=nc, note="Bob's change went in first.")
    assert r.get("ok"), r
    r = post(W.alice, "inbox/accept", number=nc)
    if r.get("ok") and r.get("job"):
        done = wait_job(r["job"])
        assert done.get("status") != "done", done  # cannot merge what no longer fits
    # Carol's copy: the new latest overlaps her change.
    carol_head = head(W.carol.app)
    before = refs()
    app_open(W.carol)
    assert head(W.carol.app) == carol_head
    out = page(W.carol)
    p = out["panel"]
    assert p["state"] == "needs-merge", p
    assert {"merge-claude", "match"} <= set(ids(out)), ids(out)
    r = act(W.carol, "merge-claude")
    assert r.get("ok") and r.get("job"), r
    j = wait_merge(r["job"])
    assert (j.get("result") or {}).get("status") == "ready", j
    r = post(W.carol, "merge/accept", job=r["job"])
    assert r.get("ok"), r
    quiesce()
    no_pushes(before, "Carol's merge")
    out = page(W.carol)
    assert out["panel"]["state"] == "proposed", out["panel"]
    assert "asked you to update" in out["panel"]["sentence"] or "update-proposal" in ids(out), out["panel"]
    assert "update-proposal" in ids(out), ids(out)
    r = act(W.carol, "update-proposal")
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert moved(before, refs()) == {("carol/sine", f"refs/heads/{pr_row(nc)['head_branch']}")}
    tip2 = accept_in_fused(nc, method="squash")
    assert tip2 != tip1
    combined = W.fake.read(REPO, "app.py")
    assert "return 3" in combined and "return 4" in combined, combined
    for m in (W.bob, W.carol):
        app_open(m)
        out = page(m)
        assert out["panel"]["state"] == "merged" and head(m.app) == tip2, (m.login, out["panel"])
        assert first_parent_subjects(m.app, tip2) == []
    app_open(W.alice)
    alice_clean()


def test_two_senders_on_different_files(world):
    setup_world()
    nb = propose(W.bob, {"app.py": louder(3)}, "louder", title="Bob louder")
    nc = propose(W.carol, {"index.html": PAGE.replace("blue", "gold")}, "gold", title="Carol gold")
    accept_in_fused(nb, method="rebase")
    item = inbox_item(nc)
    assert item.get("fits") is True, item
    tip = accept_in_fused(nc, method="merge")
    for m in (W.bob, W.carol):
        app_open(m)
        out = page(m)
        assert out["panel"]["state"] == "merged" and head(m.app) == tip, (m.login, out["panel"])
    assert "return 3" in W.fake.read(REPO, "app.py")
    assert "gold" in W.fake.read(REPO, "index.html")
    app_open(W.alice)
    alice_clean()


# == the author takes it back, rewinds, or a collaborator publishes ==================


def test_merged_then_taken_back_then_undo_and_propose_again(world):
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    accept_on_github(n, method="merge")
    # Alice changes her mind on github.com.
    W.fake.commit(REPO, "main", {"app.py": APP_PY}, "Revert louder", author="alice")
    tip = tip_of()
    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    p = out["panel"]
    # v3: no take-back detection; Alice's revert is just her latest, and the
    # merged line's Undo brings Bob's version back.
    assert p["state"] == "merged" and "merged your change" in p["sentence"], p
    assert head(W.bob.app) == tip and read(W.bob.app, "app.py") == APP_PY
    line = next(x for x in out["activity"] if x.get("undo") and "merged your change" in x["text"])
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok"), r
    assert "return 3" in read(W.bob.app, "app.py")
    out = page(W.bob)
    assert out["panel"]["state"] in ("changes", "merged"), out["panel"]
    assert "propose" in ids(out) or "bring-in" in ids(out), ids(out)
    no_pushes(before, "the take-back")


def test_author_rewinds_github_with_a_sender_following(world):
    """Alice force-pushes main back one commit from a terminal. Bob's copy
    (with changes) and Carol's (clean) never lose anything and never push;
    Alice's own copy is stuck in plain words with a way out."""
    setup_world()
    tip0 = tip_of()
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    app_open(W.carol)
    turn_committed(W.bob, {"app.py": louder(3)}, "louder")
    app_open(W.bob)
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    bob_head = head(W.bob.app)
    # The rewind.
    bare = W.fake.bare_path(REPO)
    git(bare, "update-ref", "refs/heads/main", tip0)
    W.fake.after_push(REPO, "main")
    assert tip_of() == tip0
    before = refs()
    for m in (W.bob, W.carol):
        res = app_open(m)
        assert res.get("status") in ("synced", "noop", "skipped", "needs-merge", "failed",
                                     "blocked", "deferred"), res
        out = page(m)
        assert out.get("actions"), out
    assert "return 3" in read(W.bob.app, "app.py")
    git(W.bob.app, "merge-base", "--is-ancestor", bob_head, "HEAD")
    assert read(W.carol.app, "index.html") == PAGE.replace("blue", "green"), \
        "a rewind on GitHub silently dropped a follower's files"
    no_pushes(before, "a rewind")
    # Alice's copy: the open itself names the rewind (nothing published).
    res = app_open(W.alice)
    out = page(W.alice)
    p = out["panel"]
    assert p["state"] == "github-changed", (p, res)
    assert tip_of() == tip0
    # Her green commit WAS published and then removed: "Put my edits on
    # top" would only re-publish it, so it is not offered; the two honest
    # ways out are.
    assert {"use-github", "publish-over"} <= set(ids(out)), ids(out)
    assert "put-on-top" not in ids(out), ids(out)
    no_pushes(before, "Alice's open after a rewind")
    r = act(W.alice, "publish-over", confirm=True)
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert tip_of() != tip0 and W.fake.read(REPO, "index.html") == PAGE.replace("blue", "green")
    alice_clean()
    for m in (W.bob, W.carol):
        app_open(m)
        out = page(m)
        assert out["panel"]["state"] in ("following", "changes"), (m.login, out["panel"])
    assert read(W.carol.app, "index.html") == PAGE.replace("blue", "green")


def test_collaborator_publishes_directly(world):
    setup_world()
    W.fake.add_collaborator(REPO, "carol")
    use(W.carol)
    from fused_render.contrib import classify, store
    classify._push_cache.clear()
    # Invited after installing: the record learns the rights on the next
    # ownership re-check (what a sign-in or a forced refresh runs).
    store.update(W.carol.app, lambda r: r.__setitem__("owner_checked", False))
    turn_committed(W.carol, {"app.py": louder(3)}, "louder")
    out = page(W.carol)
    assert out["panel"]["kind"] == "github", out["panel"]
    assert "push-main" in ids(out), (ids(out), out["panel"])
    assert "propose" in ids(out), ids(out)
    before = refs()
    r = act(W.carol, "push-main", confirm=True)
    assert r.get("ok") and (r.get("done") or {}).get("status") == "done", r
    assert moved(before, refs()) == {(REPO, "refs/heads/main")}
    assert tip_of() == head(W.carol.app)
    out = page(W.carol)
    assert out["panel"]["state"] == "following", out["panel"]
    app_open(W.bob)
    assert head(W.bob.app) == tip_of()
    app_open(W.alice)
    alice_clean()


# == flags, moves, and the sender's own stuck states =================================


def test_flags_off_while_merged_then_on_returns(world):
    """The loop is switched off while a proposal is open and the author
    merges it meanwhile: the copy still gets her latest (clean, local,
    undoable) and nothing is pushed; switching it back on returns the copy."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    W.bob.flags = dict(ALL_OFF)
    tip = accept_on_github(n, method="merge")
    before = refs()
    res = app_open(W.bob)
    assert res.get("status") in ("synced", "noop"), res
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    assert "return 3" in read(W.bob.app, "app.py")
    from fused_render.contrib import store
    assert not (store.get(W.bob.app) or {}).get("needs_merge")
    no_pushes(before, "flags off")
    W.bob.flags = dict(ALL_ON)
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged", out["panel"]
    assert head(W.bob.app) == tip, "the leftover combine commit stayed in history"
    no_pushes(before, "flags back on")


def test_folder_moved_on_disk_keeps_the_proposal(world):
    """Bob renames the app folder in Finder: the record (and its open
    proposal) follow the repo; the merge still returns it."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    use(W.bob)
    quiesce()
    new_app = os.path.join(os.path.dirname(W.bob.app), "sine-renamed")
    os.rename(W.bob.app, new_app)
    W.bob.app = new_app
    out = page(W.bob)
    assert out["ok"] is not False, out
    assert out["panel"]["kind"] == "github", out["panel"]
    assert out["panel"]["state"] == "proposed", out["panel"]
    tip = accept_in_fused(n, method="squash")
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) == tip, out["panel"]


def test_delete_versus_change_is_a_choice(world):
    """Alice removes a file Bob changed: no text to merge, so the page asks
    Keep mine / Use alice's for that file instead of going to Claude."""
    setup_world()
    turn_committed(W.bob, {"app.py": louder(3)}, "louder")
    use(W.alice)
    git(W.alice.app, "rm", "-q", "app.py")
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "drop app.py")
    assert W.fake.read(REPO, "app.py") is None
    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    p = out["panel"]
    assert p["state"] == "needs-merge", p
    assert "merge-claude" in ids(out) or "choose-mine" in ids(out), ids(out)
    if "merge-claude" in ids(out):
        r = act(W.bob, "merge-claude")
        assert r.get("ok") and r.get("job"), r
        j = wait_merge(r["job"])
        st = (j.get("result") or {}).get("status")
        assert st in ("choice", "ready"), j
        if st == "choice":
            out = page(W.bob)
            assert "choose-mine" in ids(out) and "choose-theirs" in ids(out), ids(out)
            assert any("app.py" in (a.get("label") or "") for a in out["actions"]), out["actions"]
            r = act(W.bob, "choose-mine")
            assert r.get("ok"), r
            if r.get("job"):
                wait_merge(r["job"])
        # Keep what Claude (or the choice) produced.
        from fused_render.contrib import merge_job
        job = merge_job.for_app(W.bob.app)
        assert job and job.get("status") == "ready", job
        r = post(W.bob, "merge/accept", job=job["id"])
        assert r.get("ok"), r
        quiesce()
    assert os.path.exists(os.path.join(W.bob.app, "app.py"))
    assert "return 3" in read(W.bob.app, "app.py")
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    git(W.bob.app, "merge-base", "--is-ancestor", tip_of(), "HEAD")
    no_pushes(before, "a delete/change overlap")
    out = page(W.bob)
    assert out["panel"]["state"] == "changes" and "propose" in ids(out), out["panel"]


def test_overlap_then_match_then_undo(world):
    """An overlap stands until Bob decides: nothing moves on its own; Use
    alice's version (match) lands him on her tip with his version saved;
    Undo brings his back."""
    setup_world()
    turn_committed(W.bob, {"app.py": louder(3)}, "louder")
    alice_publish({"app.py": louder(5)}, "five")
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "needs-merge", out["panel"]
    bob_head = head(W.bob.app)
    app_open(W.bob)
    assert head(W.bob.app) == bob_head, "an overlap moved the copy on its own"
    # A newer tip from Alice: still an overlap, still nothing moves.
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "needs-merge", out["panel"]
    assert "match" in ids(out), ids(out)
    r = act(W.bob, "match", confirm=True)
    assert r.get("ok"), r
    assert head(W.bob.app) == tip_of() and read(W.bob.app, "app.py") == louder(5)
    out = page(W.bob)
    line = next(x for x in out["activity"] if x.get("undo") and "alice" in x["text"])
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok") and head(W.bob.app) == bob_head, r


def test_undo_of_an_automatic_bring_in_holds_until_the_author_moves(world):
    """§17.2: Undo of an automatic bring-in is an answer; the next open does
    not redo it. Bring in (the button) does, and so does a newer tip."""
    setup_world()
    turn_committed(W.bob, {"app.py": louder(3)}, "louder")
    bob_mine = head(W.bob.app)
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    app_open(W.bob)
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    out = page(W.bob)
    line = next(x for x in out["activity"] if x.get("undo") and "alice" in x["text"])
    r = post(W.bob, "restore", id=line["id"])
    assert r.get("ok"), r
    assert head(W.bob.app) == bob_mine
    app_open(W.bob)
    assert head(W.bob.app) == bob_mine, "auto-sync re-merged right after Undo"
    out = page(W.bob)
    assert "bring-in" in ids(out) and "waiting" in out["panel"]["sentence"], out["panel"]
    # A newer tip from Alice lifts the hold.
    alice_publish({"README.md": "hi\n"}, "readme")
    app_open(W.bob)
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert read(W.bob.app, "README.md") == "hi\n"
    assert read(W.bob.app, "app.py") == louder(3)


# == merge commits on GitHub vs a copy whose history drifted ===========================
#
# The v1 engine rebased the sender's commits on every author push, so the ids
# GitHub merged (in a merge commit) were not the ids in his copy any more and
# the two never lined up again. v2 keeps ids, but a copy left in that shape by
# v1, or one whose commits were rewritten by hand, must still come back.


def test_merge_commit_when_the_senders_local_ids_were_rewritten(world):
    """Alice merges (merge commit) the proposal; meanwhile Bob's local copy
    of the same change has a different id (same content). The return lands
    him on her latest with nothing duplicated and nothing lost."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    # Rewrite Bob's commit in place (same tree, new id), as v1 did on every author push.
    use(W.bob)
    git(W.bob.app, "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
        "commit", "--amend", "-q", "--no-edit", "--reset-author")
    assert head(W.bob.app) != W.fake.tip("bob/sine", pr_row(n)["head_branch"])
    turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    tip = accept_on_github(n, method="merge")
    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged", out["panel"]
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    assert git(W.bob.app, "diff", "--name-only", tip, "HEAD").split() == ["later.txt"]
    # The amended commit is not the id GitHub merged, so (Q7) it stays in the
    # branch's history while later work keeps the branch off main.
    assert first_parent_subjects(W.bob.app, tip) == ["Claude: later idea", "Claude: louder"]
    assert "return 3" in read(W.bob.app, "app.py")
    no_pushes(before, "the return")
    app_open(W.alice)
    alice_clean()


def test_merge_commit_with_no_proposal_record_still_lines_up(world):
    """The same drift with the proposal record gone (a v1 copy, a lost
    store): the copy has the change under its own id, GitHub has it inside
    a merge commit. The ordinary update combines them cleanly and the copy
    reads as following, with Discard / Use alice's version as the one-click
    way back should anything look off."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    use(W.bob)
    git(W.bob.app, "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
        "commit", "--amend", "-q", "--no-edit", "--reset-author")
    from fused_render.contrib import store
    store.update(W.bob.app, lambda r: r.__setitem__("proposal", None))
    tip = accept_on_github(n, method="merge")
    before = refs()
    app_open(W.bob)
    out = page(W.bob)
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    assert git(W.bob.app, "diff", "--name-only", tip, "HEAD").split() == []
    assert out["panel"]["state"] == "following", out["panel"]
    assert "return 3" in read(W.bob.app, "app.py")
    no_pushes(before, "the update")


def test_author_keeps_merging_after_the_accept(world):
    """Alice accepts (merge commit) and then publishes two more changes
    before Bob opens: the return lands on her newest, keeping his later edit."""
    setup_world()
    n = propose(W.bob, {"app.py": louder(3)}, "louder", title="Louder")
    turn_committed(W.bob, {"later.txt": "after sending\n"}, "later idea")
    accept_on_github(n, method="merge")
    alice_publish({"index.html": PAGE.replace("blue", "green")}, "green")
    tip = alice_publish({"README.md": "hi\n"}, "readme")
    app_open(W.bob)
    out = page(W.bob)
    assert out["panel"]["state"] == "merged" and head(W.bob.app) != tip, out["panel"]
    git(W.bob.app, "merge-base", "--is-ancestor", tip, "HEAD")
    assert git(W.bob.app, "diff", "--name-only", tip, "HEAD").split() == ["later.txt"]
    assert read(W.bob.app, "index.html") == PAGE.replace("blue", "green")
    assert read(W.bob.app, "README.md") == "hi\n"
