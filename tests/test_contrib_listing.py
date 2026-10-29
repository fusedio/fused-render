"""Publish to the Marketplace from fused (docs/CONTRIB_LOOP.md §18.6, B3) —
`contrib/listing.py`, `contrib/actions_listing.py`, `server/routers/listing.py`
against a FakeGitHub holding the index repo and Alice's app repo (real bare
repos; every git process maps https://github.com/ to them).

What a wrong answer would cost: a row naming a commit GitHub doesn't have, a
forged comment read as a pass, a publish that skipped App Doctor, a delist
that deleted someone else's row. Each is pinned here.
"""
from __future__ import annotations

import json
import os

import pytest

from tests._contrib_gh_world import git, write_prefs

INDEX = "fusedio/apps-index"
REPO = "alice/sine-starter"
#: The listing slug IS the entry page's fused-app-id (PAGE below).
SLUG = "sine-starter-0a1b2c3d"
BOT = "github-actions[bot]"

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="fused-app" />
<meta name="fused-app-id" content="sine-starter-0a1b2c3d" />
<meta name="description" content="A sine wave to start from." />
<title>Sine Starter</title>
</head>
<body><p>sine</p></body>
</html>
"""

OTHER_ROW = {"slug": "other", "repo": "bob/other", "branch": "main", "commit": "a" * 40,
             "name": "Other", "description": "", "category": "starters", "tags": [],
             "icon": None, "preview": None, "remix_of": None, "version": "1.0.0"}

ROW_KEYS = ["slug", "repo", "branch", "commit", "name", "description", "category", "tags",
            "icon", "preview", "remix_of", "version"]


@pytest.fixture
def w(tmp_path, monkeypatch):
    from fused_render.contrib import gh, store

    home, ws = tmp_path / "home", tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_INDEX", INDEX)
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gh, "_FORK_POLL_STEP_S", 0.01)
    write_prefs(marketplace_enabled=True)
    gh.forget_identity()

    from fused_render import github_setup
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"signed_in": True, "account": "alice", "stale": False})

    fake.add_user("alice", "Alice")
    fake.add_user("fusedio")
    fake.create_repo_as("fusedio", "apps-index", {
        "README.md": "The Fused Marketplace index.\n",
        "apps/other.json": json.dumps(OTHER_ROW, indent=2) + "\n",
    })
    fake.create_repo_as("alice", "sine-starter", {
        "index.html": PAGE, "app.py": "def f():\n    return 1\n",
        "README.md": "# Sine Starter\n", "preview.png": "not really a png\n",
        "icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n",
        "metadata.json": json.dumps({"version": "1.2.0", "tags": ["starter"]}) + "\n",
    })
    fake.sign_in("alice")

    (ws / "app").mkdir(parents=True)
    app = str(ws / "app" / "sine-starter")
    git(ws / "app", "clone", "-q", f"https://github.com/{REPO}", app)
    git(app, "config", "user.name", "Alice")
    git(app, "config", "user.email", "alice@example.com")
    rec = store.new_record("own", repo=REPO, branch="main", classified_by="install")
    store.put(app, rec)
    git(app, "config", "--local", "fused.copyid", rec["copy_id"])
    from fused_render.contrib import listing
    listing._index_branch_memo.clear()
    return type("W", (), {"fake": fake, "app": store.key(app), "gh": gh, "store": store})


def _commit_push(app, rel, text, msg="Change"):
    with open(os.path.join(app, rel), "w", encoding="utf-8") as fh:
        fh.write(text)
    git(app, "add", "-A")
    git(app, "commit", "-q", "-m", msg)
    git(app, "push", "-q", "origin", "HEAD:main")
    return git(app, "rev-parse", "HEAD")


def _row_at_head(w, number):
    pr = w.gh.get_pr(INDEX, number)
    text = w.fake.read(pr["head_repo"], f"apps/{SLUG}.json", ref=pr["head_sha"])
    return pr, (json.loads(text) if text is not None else None)


def _details(**kw):
    d = {"slug": "ignored-by-publish", "name": "Sine Starter", "description": "A sine wave.",
         "category": "starters", "tags": ["starter", "Demo"], "icon": "icon.svg",
         "preview": "preview.png"}
    d.update(kw)
    return d


def _block(sha, status, **kw):
    data = {"sha": sha, "status": status, "reasons": [], "whats_new": "First release.",
            "new_powers": []}
    data.update(kw)
    return f"<!-- fused-review {json.dumps(data)} -->\nReview result for {sha[:7]}."


def _publish(w):
    from fused_render.contrib import listing
    out = listing.publish(w.app, _details())
    return out["pr"]["number"]


# -- publish ---------------------------------------------------------------------


def test_publish_opens_the_index_pr_with_the_exact_row(w):
    from fused_render.contrib import listing

    tip = w.fake.tip(REPO)
    out = listing.publish(w.app, _details())
    assert out["ok"]
    pr, row = _row_at_head(w, out["pr"]["number"])
    assert pr["title"] == "List Sine Starter"
    assert pr["state"] == "open" and pr["head_branch"] == "fused-" + SLUG
    assert pr["head_repo"].lower().startswith("alice/")  # her fork, in her name
    assert "Opened from fused" in pr["body"]
    assert list(row) == ROW_KEYS
    assert row == {"slug": SLUG, "repo": REPO, "branch": "main", "commit": tip,
                   "name": "Sine Starter", "description": "A sine wave.",
                   "category": "starters", "tags": ["starter", "demo"], "icon": "icon.svg",
                   "preview": "preview.png", "remix_of": None, "version": "1.2.0"}
    # The PR changes only the row: other rows untouched on the branch.
    assert w.fake.read(pr["head_repo"], "apps/other.json", ref=pr["head_sha"]) is not None
    lst = listing.view(w.app)
    assert lst["listed"] is False and lst["pr"]["kind"] == "list"
    assert lst["review"]["status"] == "in-review"
    from fused_render.contrib import actions_listing
    assert actions_listing.decide(lst)["state"] == "in-review"


def test_publish_again_reuses_the_one_pr(w):
    from fused_render.contrib import listing

    n = _publish(w)
    w.fake.comment_as(BOT, INDEX, n, _block(w.gh.get_pr(INDEX, n)["head_sha"], "fail",
                                            reasons=["Reads ~/.ssh/id_rsa."]))
    listing.status(w.app, fresh=True)
    out = listing.publish(w.app, _details(name="Sine Wave"))
    assert out["pr"]["number"] == n
    assert len(w.gh.list_open_prs(INDEX)) == 1
    pr, row = _row_at_head(w, n)
    assert pr["title"] == "List Sine Wave" and row["name"] == "Sine Wave"


def test_slug_taken_by_another_repo_is_refused(w):
    from fused_render.contrib import listing
    from fused_render.contrib.jobs import PlainError

    # Someone copied the folder (same fused-app-id) and listed it first.
    w.fake.commit(INDEX, "main", {f"apps/{SLUG}.json": json.dumps(
        {**OTHER_ROW, "slug": SLUG}) + "\n"}, "List the copy", author="fusedio")
    with pytest.raises(PlainError) as e:
        listing.publish(w.app, _details())
    assert e.value.reason == "slug-taken" and "remix" in e.value.message
    assert w.gh.list_open_prs(INDEX) == []


def test_the_slug_is_the_app_id_and_publish_refuses_without_one(w):
    """Owner 2026-10-27: the Community indexes apps by `fused-app-id`, never
    a name-based slug. The dialog's slug is ignored; a page with no valid id
    is refused (409 reason `app-id`) before anything is sent, pointing at
    App Doctor, whose `app-id` row then blocks the listing too."""
    from fused_render.contrib import listing
    from fused_render.contrib.jobs import PlainError

    assert listing.draft(w.app)["app_id"] == SLUG
    page = os.path.join(w.app, "index.html")
    with open(page, encoding="utf-8") as fh:
        text = fh.read()
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(text.replace(f'<meta name="fused-app-id" content="{SLUG}" />\n', ""))
    d = listing.draft(w.app)
    assert d["slug"] is None and d["app_id"] is None
    with pytest.raises(PlainError) as e:
        listing.publish(w.app, _details(slug="sine-starter"))
    assert e.value.reason == "app-id"
    assert e.value.message.endswith("Open App Doctor to add one.")
    # Through the route: the listing shape (200, ok false), before any job.
    from fused_render.server.routers import listing as r

    got = r.api_contrib_listing(path=w.app)
    if got.get("job"):
        from fused_render.contrib import jobs
        jobs.wait(got["job"], 30)
    got = r.api_contrib_listing(path=w.app)
    # The Doctor gate already takes Publish off the strip (App Doctor's
    # `app-id` row); a button pressed anyway gets the listing shape, 200.
    assert "publish-market" not in [a["id"] for a in got["actions"]]
    out = r.api_contrib_listing_action({"path": w.app, "action": "publish-market",
                                        "args": _details()}, x_fused="1")
    assert isinstance(out, dict) and out["ok"] is False
    assert r._no_app_id(r._Target(w.app, {})) == {"ok": False, "reason": "app-id",
                                              "message": listing.APP_ID_MISSING}
    assert w.gh.list_open_prs(INDEX) == []
    assert any(b["id"] == "app-id" for b in listing.doctor_blockers(w.app))


@pytest.mark.parametrize("bad", [
    {"name": "x" * 61}, {"description": ""}, {"description": "y" * 401},
    {"icon": "app.py"}, {"preview": "../preview.png"}, {"category": "games"},
    {"version": "v1"},
])
def test_rows_the_review_check_would_refuse_are_refused_here(w, bad):
    """WS-IDX's shape rules (scripts/review/check.py `shape_problems`), in
    words before a PR exists."""
    from fused_render.contrib import listing
    from fused_render.contrib.jobs import PlainError

    with pytest.raises(PlainError) as e:
        listing.publish(w.app, _details(**bad))
    assert e.value.reason == "invalid"
    assert w.gh.list_open_prs(INDEX) == []


def test_review_commit_status_on_the_head_is_the_gate(w):
    """WS-IDX's check sets commit status `review` on the PR HEAD sha
    (pending → success | failure); that, not the check run, is the gate."""
    from fused_render.contrib import listing

    n = _publish(w)
    head = w.gh.get_pr(INDEX, n)["head_sha"]
    w.fake.set_commit_status(INDEX, head, "review", "pending")
    assert listing.status(w.app, fresh=True)["review"]["status"] == "in-review"
    # A status someone else set is not the gate.
    w.fake.set_commit_status(INDEX, head, "review", "failure", creator="mallory")
    assert listing.status(w.app, fresh=True)["review"]["status"] == "in-review"
    # The gate failed with no block (a crash): failed, fail closed.
    w.fake.set_commit_status(INDEX, head, "review", "error")
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["status"] == "failed"
    assert lst["review"]["reasons"] == [listing.COULDNT_FINISH]
    # A re-run passed with powers: the block decides, the gate agrees.
    w.fake.set_commit_status(INDEX, head, "review", "success")
    w.fake.comment_as(BOT, INDEX, n, _block(head, "needs-confirmation", new_powers=[
        {"kind": "files", "detail": "writes to ~/Documents"}]))
    # An old failed check run on the base no longer matters once a status exists.
    w.fake.set_check_run(INDEX, head, "review", conclusion="failure")
    assert listing.status(w.app, fresh=True)["review"]["status"] == "needs-confirmation"
    listing.confirm_powers(w.app)
    # The bot accepts the confirm and edits its block to "pass".
    w.fake.comment_as(BOT, INDEX, n, _block(head, "pass", new_powers=[
        {"kind": "files", "detail": "writes to ~/Documents"}]))
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["status"] == "in-review" and lst["review"]["confirmed"]


def test_review_without_claude_says_so(w):
    """No Anthropic key on the index: the block carries "claude": "skipped"
    and What's new comes from the PR body's "## What changed" line."""
    from fused_render.contrib import actions_listing, listing

    n = _publish(w)
    pr = w.gh.get_pr(INDEX, n)
    assert "\n## What changed\nFirst release: A sine wave.\n" in pr["body"]
    w.fake.comment_as(BOT, INDEX, n, _block(pr["head_sha"], "pass", claude="skipped"))
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["claude_skipped"] is True
    assert actions_listing.CLAUDE_OFF in actions_listing.decide(lst)["sentence"]


def test_only_the_workflow_bot_writes_the_verdict(w):
    from fused_render.contrib import listing

    n = _publish(w)
    head = w.gh.get_pr(INDEX, n)["head_sha"]
    w.fake.comment_as("evil[bot]", INDEX, n, _block(head, "fail", reasons=["spoof"]))
    assert listing.status(w.app, fresh=True)["review"]["status"] == "in-review"


def test_unpushed_tip_is_refused_and_publish_now_offered(w):
    from fused_render.contrib import actions_listing, listing
    from fused_render.contrib.jobs import PlainError

    listing.status(w.app, fresh=True)
    with open(os.path.join(w.app, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("# local only\n")
    git(w.app, "commit", "-q", "-am", "Local only")
    with pytest.raises(PlainError) as e:
        listing.publish(w.app, _details())
    assert e.value.reason == "unpushed"
    assert w.gh.list_open_prs(INDEX) == []
    decided = actions_listing.decide(listing.view(w.app))
    ids = [a["id"] for a in decided["actions"]]
    assert "publish-now" in ids and "publish-market" not in ids
    assert decided["actions"][0]["id"] == "publish-now"


def test_doctor_gate_refuses_a_publish(w):
    from fused_render.contrib import actions_listing, listing
    from fused_render.contrib.jobs import PlainError

    os.remove(os.path.join(w.app, "preview.png"))
    git(w.app, "commit", "-q", "-am", "No preview")
    git(w.app, "push", "-q", "origin", "HEAD:main")
    with pytest.raises(PlainError) as e:
        listing.publish(w.app, _details(preview=None))
    assert e.value.reason == "doctor"
    assert "preview" in e.value.message.lower()
    assert w.gh.list_open_prs(INDEX) == []
    decided = actions_listing.decide(listing.view(w.app))
    assert [a["id"] for a in decided["actions"]][0] == "open-doctor"


# -- the review ------------------------------------------------------------------


def test_status_maps_the_review_block_and_ignores_forgeries(w):
    from fused_render.contrib import actions_listing, listing

    n = _publish(w)
    head = w.gh.get_pr(INDEX, n)["head_sha"]
    # Anyone can comment on a public PR: a non-bot "pass" is not a verdict.
    w.fake.add_user("mallory")
    w.fake.comment_as("mallory", INDEX, n, _block(head, "pass"))
    assert listing.status(w.app, fresh=True)["review"]["status"] == "in-review"
    # A bot block for an OLD head does not describe this one.
    w.fake.comment_as(BOT, INDEX, n, _block("b" * 40, "fail", reasons=["old"]))
    assert listing.status(w.app, fresh=True)["review"]["status"] == "in-review"

    w.fake.comment_as(BOT, INDEX, n, _block(head, "needs-confirmation", new_powers=[
        {"kind": "network", "detail": "talks to api.example.com"}]))
    w.fake.set_check_run(INDEX, head, "review", conclusion="success")
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["status"] == "needs-confirmation"
    assert lst["review"]["new_powers"] == [{"kind": "network",
                                            "detail": "talks to api.example.com"}]
    decided = actions_listing.decide(lst)
    assert decided["actions"][0]["id"] == "confirm-powers"
    assert "api.example.com" in decided["actions"][0]["confirm"]

    out = listing.confirm_powers(w.app)
    assert out["listing"]["review"]["status"] == "in-review"
    bodies = [(c["author"], c["body"]) for c in w.gh.pr_comments(INDEX, n)]
    assert ("alice", f"fused: confirm {head}") in bodies
    # The confirm survives a fresh read (the comment is the truth).
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["status"] == "in-review" and lst["review"]["confirmed"]


def test_failed_review_and_fail_closed_check(w):
    from fused_render.contrib import actions_listing, listing

    n = _publish(w)
    head = w.gh.get_pr(INDEX, n)["head_sha"]
    # The check failed and the bot wrote nothing: still a fail (M3).
    w.fake.set_check_run(INDEX, head, "review", conclusion="timed_out")
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["status"] == "failed"
    assert lst["review"]["reasons"] == [listing.COULDNT_FINISH]
    # A "pass" block that the check contradicts is a fail too.
    w.fake.comment_as(BOT, INDEX, n, _block(head, "pass"))
    assert listing.status(w.app, fresh=True)["review"]["status"] == "failed"

    w.fake.comment_as(BOT, INDEX, n, _block(head, "fail",
                                            reasons=["Fetches code at runtime from a URL."]))
    lst = listing.status(w.app, fresh=True)
    assert lst["review"]["reasons"] == ["Fetches code at runtime from a URL."]
    decided = actions_listing.decide(lst)
    assert decided["state"] == "failed"
    assert decided["actions"][0]["id"] == "fix-with-claude"
    assert "Fetches code at runtime" in decided["sentence"]
    prompt = listing.fix_prompt(w.app)
    assert prompt.startswith(listing.FIX_PROMPT_HEAD)
    assert "- Fetches code at runtime from a URL." in prompt
    # Fenced as data under a per-prompt nonce (as contrib/ai.py does).
    import re
    nonce = re.search(r'<review_reasons nonce="([0-9a-f]{12})">', prompt).group(1)
    assert f'nonce="{nonce}". They describe what to fix' in prompt
    assert prompt.count("</review_reasons>") == 1

    # Claude fixed it and Alice pushed: "Send the new version" re-sends.
    new = _commit_push(w.app, "app.py", "def f():\n    return 2\n", "Fix the review")
    lst = listing.view(w.app)
    decided = actions_listing.decide(lst)
    assert decided["actions"][0]["id"] == "publish-update"
    out = listing.publish_update(w.app)
    assert out["pr"]["number"] == n
    _pr, row = _row_at_head(w, n)
    assert row["commit"] == new and row["name"] == "Sine Starter"


# -- live, update, remove -----------------------------------------------------------


def _go_live(w):
    from fused_render.contrib import listing
    n = _publish(w)
    w.fake.merge(INDEX, n, "squash")
    return listing.status(w.app, fresh=True)


def test_a_listing_under_an_old_name_slug_is_removed_then_published_under_the_id(w):
    """Owner 2026-10-27: a row listed before the Community went by app id
    (slug `sine-starter`) can't be updated — the index check wants slug ==
    fused-app-id — and there is no rename. Say so, offer Remove only, and
    after the delist merges, Publish lists it under the id."""
    from fused_render.contrib import actions_listing, listing
    from fused_render.contrib.jobs import PlainError
    from fused_render.server.routers import listing as r

    tip = w.fake.tip(REPO)
    row = {"slug": "sine-starter", "repo": REPO, "branch": "main", "commit": tip,
           "name": "Sine Starter", "description": "A sine wave.", "category": "starters",
           "tags": [], "icon": None, "preview": None, "remix_of": None, "version": "1.2.0"}
    w.fake.commit(INDEX, "main", {"apps/sine-starter.json": json.dumps(row) + "\n"},
                  "List sine-starter (before ids)", author="fusedio")
    lst = listing.status(w.app, fresh=True)
    assert lst["listed"] and lst["slug"] == "sine-starter"
    assert lst["app_id"] == SLUG and lst["slug_is_app_id"] is False
    old = (f"Sine Starter is listed under the old id sine-starter. Remove it from the "
           f"Community, then publish it again under its app id {SLUG}.")
    _commit_push(w.app, "app.py", "def f():\n    return 3\n", "Louder sine")
    lst = listing.view(w.app)
    assert lst["update_available"] is True
    decided = actions_listing.decide(lst)
    ids = [a["id"] for a in decided["actions"]]
    assert decided["state"] == "live" and old in decided["sentence"]
    assert "publish-update" not in ids and "remove-market" in ids
    # Pressed anyway: the module refuses, and the route in the listing shape.
    with pytest.raises(PlainError) as e:
        listing.publish_update(w.app)
    assert (e.value.reason, e.value.message) == ("old-slug", old)
    assert r._publish_update(r._Target(w.app, {}), {}) == {
        "ok": False, "reason": "old-slug", "message": old}
    assert w.gh.list_open_prs(INDEX) == []
    # Remove works on the old slug; once merged, Publish lists under the id.
    out = listing.remove(w.app)
    pr = w.gh.get_pr(INDEX, out["pr"]["number"])
    assert pr["head_branch"] == "fused-sine-starter"
    w.fake.merge(INDEX, out["pr"]["number"], "squash")
    lst = listing.status(w.app, fresh=True)
    assert lst["listed"] is False
    assert listing.draft(w.app)["slug"] == SLUG
    n = listing.publish(w.app, _details())["pr"]["number"]
    pr, row = _row_at_head(w, n)
    assert pr["head_branch"] == "fused-" + SLUG and row["slug"] == SLUG
    assert listing.view(w.app)["slug_is_app_id"] is True


def test_a_failed_request_under_an_old_slug_offers_a_fresh_publish():
    """Not listed yet, but the request used a name slug: Send again would be
    refused by the index, so Publish (under the id) is offered instead."""
    from fused_render.contrib import actions_listing

    lst = {"name": "Sine Starter", "checked_at": 1.0, "listed": False,
           "slug": "sine-starter", "app_id": SLUG, "doctor_clear": True,
           "pr": {"kind": "list", "state": "open", "commit": "a" * 40},
           "review": {"status": "failed", "reasons": ["Reads saved passwords."]}}
    decided = actions_listing.decide(lst)
    ids = [a["id"] for a in decided["actions"]]
    assert "publish-update" not in ids and "publish-market" in ids
    assert f"was sent under the old id sine-starter. Publish it again under its app id {SLUG}." \
        in decided["sentence"]


def test_merged_listing_is_live_then_update(w):
    from fused_render.contrib import actions_listing, listing

    tip = w.fake.tip(REPO)
    lst = _go_live(w)
    assert lst["listed"] and lst["approved"]["commit"] == tip
    assert lst["pr"] is None and lst["review"]["status"] == "live"
    assert lst["update_available"] is False
    decided = actions_listing.decide(lst)
    assert decided["state"] == "live"
    assert "publish-update" not in [a["id"] for a in decided["actions"]]

    new = _commit_push(w.app, "app.py", "def f():\n    return 3\n", "Louder sine")
    lst = listing.view(w.app)
    assert lst["update_available"] is True
    decided = actions_listing.decide(lst)
    assert decided["actions"][0]["id"] == "publish-update"
    out = listing.publish_update(w.app)
    pr, row = _row_at_head(w, out["pr"]["number"])
    assert pr["title"] == "Update Sine Starter"
    assert "Louder sine" in pr["body"]
    assert row["commit"] == new and row["tags"] == ["starter", "demo"]
    assert listing.view(w.app)["pr"]["kind"] == "update"


def test_remove_opens_a_delete_pr(w):
    from fused_render.contrib import actions_listing, listing

    _go_live(w)
    out = listing.remove(w.app)
    pr, row = _row_at_head(w, out["pr"]["number"])
    assert pr["title"] == "Remove Sine Starter from the Community"
    assert row is None  # the row is deleted on the branch
    assert w.fake.read(pr["head_repo"], "apps/other.json", ref=pr["head_sha"]) is not None
    lst = listing.view(w.app)
    assert lst["listed"] is True and lst["pr"]["kind"] == "remove"
    assert actions_listing.decide(lst)["state"] == "removing"
    w.fake.merge(INDEX, out["pr"]["number"], "squash")
    lst = listing.status(w.app, fresh=True)
    assert lst["listed"] is False and lst["pr"] is None


def test_listing_made_elsewhere_is_found_by_repo(w):
    """No cached slug and no catalog: the index's rows name the repo."""
    from fused_render.contrib import listing

    _go_live(w)
    w.store.update(w.app, lambda r: r.pop("listing_own", None))
    lst = listing.status(w.app, fresh=True)
    assert lst["listed"] and lst["slug"] == SLUG


def test_draft_prefills_from_the_app(w):
    from fused_render.contrib import listing

    d = listing.draft(w.app)
    assert d["slug"] == SLUG and d["name"] == "Sine Starter"
    assert d["description"] == "A sine wave to start from."
    assert d["category"] == "productivity" and d["tags"] == ["starter"]
    assert d["icon"] == "icon.svg" and d["preview"] == "preview.png"
    assert [c["id"] for c in d["categories"]] == ["starters", "local-ai", "productivity",
                                                  "geospatial"]


# -- buttons + routes ------------------------------------------------------------------


def test_buttons_feed_the_own_panel(w):
    from fused_render.contrib import actions, actions_listing, listing

    listing.status(w.app, fresh=True)
    b = actions._Buttons()
    actions_listing.buttons({"kind": "own", "repo": REPO, "app_dir": w.app,
                             "github": {"signed_in": True}}, b)
    assert [a["id"] for a in b.items] == ["publish-market", "check-listing"]
    assert b.items[0]["dialog"] == "listing"
    # Signed out, or a github copy: nothing, never a raise.
    for f in ({"kind": "own", "repo": REPO, "app_dir": w.app, "github": {}},
              {"kind": "github", "repo": REPO, "app_dir": w.app,
               "github": {"signed_in": True}},
              None):
        b = actions._Buttons()
        actions_listing.buttons(f, b)
        assert b.items == []


def test_routes_gate_offer_and_run_jobs(w):
    from fused_render.contrib import jobs
    from fused_render.server.routers import listing as r

    body = {"path": w.app}
    assert r.api_contrib_listing_action(dict(body, action="publish-market"),
                                        x_fused=None).status_code == 403
    got = r.api_contrib_listing(path=w.app)
    assert got["ok"] and got["job"]  # never checked: a refresh job, named
    assert jobs.wait(got["job"], 30)["status"] == "done"
    got = r.api_contrib_listing(path=w.app)
    assert got["state"] == "not-listed" and got["job"] is None
    assert [a["id"] for a in got["actions"]] == ["publish-market", "check-listing"]

    refused = r.api_contrib_listing_action(dict(body, action="remove-market"), x_fused="1")
    assert refused == {"ok": False, "reason": "not-offered",
                       "message": "That isn't available right now."}
    draft = r.api_contrib_listing_draft(path=w.app)
    assert draft["ok"] and draft["slug"] == SLUG

    started = r.api_contrib_listing_action(
        dict(body, action="publish-market", args=_details()), x_fused="1")
    assert started["ok"] and started["job"]
    job = jobs.wait(started["job"], 60)
    assert job["status"] == "done", job
    assert job["result"]["pr"]["number"] >= 1
    got = r.api_contrib_listing(path=w.app)
    assert got["state"] == "in-review"

    w.fake.merge(INDEX, job["result"]["pr"]["number"], "squash")
    done = jobs.wait(r.api_contrib_listing_action(dict(body, action="check-listing"),
                                                  x_fused="1")["job"], 30)
    assert done["status"] == "done" and done["result"]["listing"]["listed"]
    refused = r.api_contrib_listing_action(dict(body, action="remove-market"), x_fused="1")
    assert refused["reason"] == "confirm" and "keep their copy" in refused["message"]


def test_an_unread_pr_head_is_never_a_remove(w, monkeypatch):
    """The PR head could not be fetched: its missing row is not a delete —
    "Removing…" (and Remove beside it) would be a lie one click from real."""
    from fused_render.contrib import actions_listing, gh, listing

    n = _publish(w)
    real = listing._fetch

    def fetch(scratch, index, *refspecs):
        if any("refs/pull/" in r for r in refspecs):
            raise gh.GitHubError("offline")
        return real(scratch, index, *refspecs)
    monkeypatch.setattr(listing, "_fetch", fetch)
    lst = listing.status(w.app, fresh=True)
    assert lst["pr"]["number"] == n and lst["pr"]["kind"] == "list"
    assert actions_listing.decide(lst)["state"] == "in-review"


def test_fix_with_claude_route_opens_the_session(w, monkeypatch):
    from urllib.parse import quote

    from fused_render.server.routers import apps as apps_router
    from fused_render.server.routers import listing as r

    n = _publish(w)
    head = w.gh.get_pr(INDEX, n)["head_sha"]
    w.fake.comment_as(BOT, INDEX, n, _block(head, "fail", reasons=["Reads saved passwords."]))
    from fused_render.contrib import listing
    listing.status(w.app, fresh=True)
    made = []

    def create(entry, prompt, *a, **k):
        made.append((entry, prompt))
        return {"id": "t1", "run_id": "r1"}, None
    monkeypatch.setattr(apps_router, "_live_app_task", lambda entry, is_ours: None)
    monkeypatch.setattr(apps_router, "_create_app_task", create)
    out = r.api_contrib_listing_action({"path": w.app, "action": "fix-with-claude"},
                                       x_fused="1")
    entry = os.path.join(w.app, "index.html")
    assert out["ok"] and out["entry_html"] == entry
    assert out["open"] == ("/explorer/view/" + "/".join(
        quote(s, safe="") for s in entry.split("/") if s) + "?_side=claude&run=r1")
    assert made and made[0][1].startswith(listing.FIX_PROMPT_HEAD)
    assert "- Reads saved passwords." in made[0][1]
    # A fix already running is opened, never a second one.
    monkeypatch.setattr(apps_router, "_live_app_task",
                        lambda entry, is_ours: {"id": "t1", "state": "sent", "run_id": "r1"})
    again = r.api_contrib_listing_action({"path": w.app, "action": "fix-with-claude"},
                                         x_fused="1")
    assert again["already"] is True and len(made) == 1


def test_routes_refuse_a_github_copy(w):
    from fused_render.server.routers import listing as r

    w.store.update(w.app, lambda rec: rec.update(kind="github"))
    got = r.api_contrib_listing(path=w.app)
    assert got["ok"] is False and got["reason"] == "not-allowed"


def test_a_listed_app_whose_request_was_closed_can_still_be_removed():
    # The owner closed the publish request on GitHub while an earlier
    # listing is live: "Remove from Community" is still there (owner,
    # 2026-10-27, stuck on this state), and the sentence says the old
    # version stays up.
    from fused_render.contrib import actions_listing

    lst = {"name": "Sine Starter", "checked_at": "2026-10-27T00:00:00Z", "listed": True,
           "doctor_clear": True,
           "review": {"status": "failed", "reasons": ["closed"], "new_powers": []},
           "pr": {"number": 9, "kind": "publish", "state": "closed", "commit": "a" * 40}}
    decided = actions_listing.decide(lst)
    assert decided["state"] == "failed"
    ids = [a["id"] for a in decided["actions"]]
    assert "remove-market" in ids and "publish-update" in ids
    assert "still in the Community" in decided["sentence"]
    # A closed REMOVE request on a listed app: Remove, no Send again.
    lst["pr"]["kind"] = "remove"
    ids = [a["id"] for a in actions_listing.decide(lst)["actions"]]
    assert "remove-market" in ids and "publish-update" not in ids
