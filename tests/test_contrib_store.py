"""contrib/store.py, classify.py and flags.py (docs/CONTRIB_LOOP.md §1, §3,
§4): the record outside the folder, its move/prune/adoption self-healing,
the ownership rule, and the prerequisite chain. Real folders and repos."""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _git_repo import git, git_available, write  # noqa: E402

from fused_render import app_doctor, app_state_move, app_state_remove  # noqa: E402
from fused_render.contrib import classify, engine, flags, store  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.setattr(classify, "_identity", lambda: None)
    monkeypatch.setattr(classify, "ensure_in_background", lambda app_dir: False)
    return h


def _prefs(home, **values):
    with open(home / "prefs.json", "w") as f:
        json.dump(values, f)


def _clone(tmp_path, name, url):
    d = tmp_path / "ws" / "app" / name
    d.mkdir(parents=True)
    git(str(d), "init", "-q", "-b", "main")
    write(str(d), "index.html", "<p>x</p>\n")
    git(str(d), "add", "-A")
    git(str(d), "commit", "-q", "-m", "c1")
    git(str(d), "remote", "add", "origin", url)
    return str(d)


# ------------------------------------------------------------------ flags


def test_flags_default_off_and_prerequisites(home):
    assert flags.snapshot() == {"sources": False, "changes": False, "inbox": False,
                                "loop": False, "remix": False}
    _prefs(home, contrib_changes_enabled=True, contrib_loop_enabled=True,
           contrib_inbox_enabled=True, contrib_remix_enabled=True)
    assert not any(flags.snapshot().values())  # sources is everyone's prerequisite
    _prefs(home, contrib_sources_enabled=True, contrib_changes_enabled=True,
           contrib_loop_enabled=True)
    assert flags.snapshot() == {"sources": True, "changes": True, "inbox": False,
                                "loop": False, "remix": False}
    _prefs(home, contrib_sources_enabled=True, contrib_changes_enabled=True,
           contrib_inbox_enabled=True, contrib_loop_enabled=True, contrib_remix_enabled="yes")
    assert flags.loop() is True and flags.remix() is False


def test_prefs_response_reports_raw_switches(home):
    from fused_render.shell import prefs

    _prefs(home, contrib_loop_enabled=True)
    assert prefs._prefs_response()["contrib"] == {
        "sources": False, "changes": False, "inbox": False, "loop": True, "remix": False}


# ------------------------------------------------------------------ store


def test_store_keys_by_realpath_and_hides_missing_folders(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    store.put(str(link), store.new_record("github", repo="alice/x", branch="main"))
    assert store.get(str(real))["repo"] == "alice/x"
    os.rmdir(real)
    assert store.get(str(link)) is None
    assert store.find_by_repo("ALICE/X")  # the orphan stays for adoption


def test_store_unknown_keys_survive(home, tmp_path):
    d = tmp_path / "try"
    d.mkdir()
    rec = store.new_record("scratch", classified_by="try")
    rec["try_of"] = {"app": "/a", "number": 3, "head_sha": "abc"}
    store.put(str(d), rec)
    store.add_event(str(d), "updated")
    assert store.get(str(d))["try_of"]["number"] == 3
    assert classify.ensure(str(d))["kind"] == "scratch"
    _prefs(home, contrib_sources_enabled=True, contrib_changes_enabled=True,
           contrib_inbox_enabled=True)
    assert engine.status(str(d))["kind"] == "scratch"
    assert classify.kind_of(str(d)) == "scratch"  # never synced (git_upstream)


def test_events_cap_and_ack(home, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    store.put(str(d), store.new_record("github", repo="alice/x", branch="main"))
    for _ in range(store.EVENTS_KEEP + 3):
        store.add_event(str(d), "updated-kept", who="alice")
    events = store.get(str(d))["events"]
    assert len(events) == store.EVENTS_KEEP and not any(e["seen"] for e in events)
    store.ack(str(d), "event")
    assert all(e["seen"] for e in store.get(str(d))["events"])
    with pytest.raises(ValueError):
        store.add_event(str(d), "not-a-kind")


def test_move_and_prune_hooks(home, tmp_path):
    old = tmp_path / "ws" / "app" / "old"
    new = tmp_path / "ws" / "app" / "new"
    old.mkdir(parents=True)
    store.put(str(old), store.new_record("github", repo="alice/x", branch="main"))
    # A move while the old folder still exists is a COPY: nothing moves.
    new.mkdir()
    assert store.move(str(old), str(new)) == 0
    os.rmdir(old)
    app_state_move.rewrite_stores(str(old), str(new))
    assert store.get(str(new))["repo"] == "alice/x"
    out = app_state_remove.prune_stores(str(new))
    assert out["contrib"] == 1
    assert store.get(str(new)) is None


def test_move_never_overwrites_an_existing_record(home, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    new.mkdir()
    store.put(str(old), store.new_record("own"))  # folder already gone
    store.put(str(new), store.new_record("github", repo="alice/x", branch="main"))
    assert store.move(str(old), str(new)) == 0
    assert store.get(str(new))["kind"] == "github"


# --------------------------------------------------------------- classify

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def test_non_github_and_nested_folders_are_own(home, tmp_path):
    d = _clone(tmp_path, "gl", "https://gitlab.com/alice/x.git")
    assert classify.ensure(d)["kind"] == "own" and classify.ensure(d)["repo"] is None
    plain = tmp_path / "ws" / "app" / "plain"
    plain.mkdir(parents=True)
    assert classify.ensure(str(plain))["kind"] == "own"


def test_pre_loop_clone_is_not_classified_while_signed_out(home, tmp_path, monkeypatch):
    """Audit V4 (§16.12): a pre-loop clone with no GitHub identity to ask is
    NOT recorded — it may be the author's own app, auto-pushing as ever —
    until an identity exists."""
    d = _clone(tmp_path, "x", "git@github.com:Alice/X.git")
    assert classify.ensure(d) is None and store.get(d) is None
    assert classify.kind_of(d) is None
    monkeypatch.setattr(classify, "_identity", lambda: {"login": "bob"})
    monkeypatch.setattr(classify, "_role_cache", {})
    monkeypatch.setattr(classify, "_gh", lambda: type("G", (), {
        "org_role": staticmethod(lambda org: "member")}))
    rec = classify.ensure(d)
    assert (rec["kind"], rec["repo"], rec["owner_checked"]) == ("github", "Alice/X", True)


def test_recheck_upgrades_to_own_once(home, tmp_path, monkeypatch):
    d = _clone(tmp_path, "x", "https://github.com/alice/x")
    classify.record_install(d, "https://github.com/alice/x")  # unchecked
    monkeypatch.setattr(classify, "_identity", lambda: {"login": "Alice"})
    rec = classify.ensure(d)
    assert rec["kind"] == "own" and rec["owner_checked"] is True


def test_org_admin_is_own_collaborator_is_github(home, tmp_path, monkeypatch):
    class FakeGh:
        @staticmethod
        def identity():
            return {"login": "bob"}

        @staticmethod
        def org_role(org):
            return {"acme": "admin", "friends": "member"}.get(org)

    monkeypatch.setattr(classify, "_identity", lambda: {"login": "bob"})
    monkeypatch.setattr(classify, "_gh", lambda: FakeGh)
    assert classify.ensure(_clone(tmp_path, "a", "https://github.com/acme/a"))["kind"] == "own"
    rec = classify.ensure(_clone(tmp_path, "b", "https://github.com/friends/b"))
    assert rec["kind"] == "github" and rec["owner_checked"] is True


def test_record_install_and_finder_move_adoption(home, tmp_path):
    d = _clone(tmp_path, "x", "https://github.com/alice/x")
    rec = classify.record_install(d, "https://github.com/alice/x")
    assert rec["classified_by"] == "install" and rec["kind"] == "github"
    def dirty_state(r):
        r["proposal"] = {"number": 9}
        r["needs_merge"] = {"onto": "x"}
        r["backups"] = [{"ref": "refs/fused/backup/1", "at": 0, "why": "t"}]
        r["protected"] = True
    store.update(d, dirty_state)
    # A Finder move the server never saw.
    moved = str(tmp_path / "ws" / "app" / "x-moved")
    os.rename(d, moved)
    rec = classify.ensure(moved)
    assert rec["proposal"]["number"] == 9 and rec["classified_by"] == "install"
    # The old folder's git state does not travel (it names its refs).
    assert rec["needs_merge"] is None and rec["activity"] == [] and "protected" not in rec
    assert "backups" not in rec and rec["root_commit"]
    assert len(store.find_by_repo("alice/x")) == 1


# ----------------------------------------------------------------- doctor


def test_fused_dir_row_is_gated_and_reports_tracked_paths(home, tmp_path):
    d = _clone(tmp_path, "x", "https://github.com/alice/x")
    write(d, ".fused/meta.json", "{}\n")
    git(d, "add", "-f", "-A")
    git(d, "commit", "-q", "-m", "fused")
    ids = [c["id"] for c in app_doctor.report(d)["checks"]]
    assert "fused-dir-committed" not in ids
    _prefs(home, contrib_sources_enabled=True)
    row = next(c for c in app_doctor.report(d)["checks"] if c["id"] == "fused-dir-committed")
    assert row["state"] == "fail" and row["severity"] == "warning"
    assert [f["path"] for f in row["findings"]] == [".fused/meta.json"]
    assert app_doctor.report_one(d, "fused-dir-committed")["state"] == "fail"


# ----------------------------------------------------- lifecycle housekeeping


def test_unrendered_event_never_hides_a_banner(home, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    store.put(str(d), store.new_record("github", repo="alice/x", branch="main"))
    store.add_event(str(d), "updated-kept", who="alice")
    store.add_event(str(d), "discarded", who="alice")  # newer, never drawn
    assert store.get(str(d))["events"][-1]["seen"] is True
    assert store.get(str(d))["events"][0]["seen"] is False


def test_orphan_rows_drop_after_thirty_days(home, tmp_path):
    gone, here = tmp_path / "gone", tmp_path / "here"
    here.mkdir()
    store.put(str(gone), store.new_record("own"))
    store.put(str(here), store.new_record("own"))
    assert store.sweep_orphans(now=1000.0) == 0
    assert store.get_raw(store.key(str(gone)))["orphaned_at"] == 1000.0
    assert "orphaned_at" not in store.get(str(here))
    assert store.sweep_orphans(now=1000.0 + store.ORPHAN_TTL_S - 1) == 0
    assert store.sweep_orphans(now=1000.0 + store.ORPHAN_TTL_S + 1) == 1
    assert store.get_raw(store.key(str(gone))) is None
    assert store.get(str(here)) is not None


def test_flags_memo_follows_the_file(home):
    assert flags.sources() is False
    _prefs(home, contrib_sources_enabled=True)
    assert flags.sources() is True
    from fused_render.shell import prefs

    prefs.write_pref("contrib_sources_enabled", False)
    assert flags.sources() is False


def test_org_role_is_asked_once_per_owner(home, tmp_path, monkeypatch):
    calls = []

    class FakeGh:
        @staticmethod
        def org_role(org):
            calls.append(org)
            return "member"

    monkeypatch.setattr(classify, "_role_cache", {})
    monkeypatch.setattr(classify, "_identity", lambda: {"login": "bob"})
    monkeypatch.setattr(classify, "_gh", lambda: FakeGh)
    for n in ("a", "b", "c"):
        classify.ensure(_clone(tmp_path, n, f"https://github.com/acme/{n}"))
    assert calls == ["acme"]


# ===================================================== v2 guards (§16.1, §16.8)
#
# Ported from the 2026-10-16 audit (scratchpad audit-user/test_audit.py): each
# test below reproduced a v1 bug and now asserts the FIXED behaviour. Bob's
# copy is a real clone of a bare "GitHub" repo (insteadOf maps the URL), so a
# push that should never happen would really land.

import shutil  # noqa: E402
import subprocess  # noqa: E402

from fused_render import app_git, appfile_git, git_upstream  # noqa: E402

REPO = "alice/sine"
URL = "https://github.com/" + REPO


@pytest.fixture
def world(tmp_path, monkeypatch, home):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name, value in (("_checked", {}), ("_state", {}), ("_sync_failures", {}),
                        ("_pulled_events", []), ("_pending", {}), ("_last_fetch", {})):
        monkeypatch.setattr(git_upstream, name, value)
    monkeypatch.setattr(git_upstream, "auto_sync_enabled", lambda: True)
    monkeypatch.setattr(classify, "_kind_memo", {})
    monkeypatch.setattr(classify, "_guarded", {})
    monkeypatch.setattr(store, "_bind_memo", {})
    remote = str(tmp_path / "remote.git")
    alice = str(tmp_path / "alice")
    git(str(tmp_path), "init", "-q", "--bare", "-b", "main", remote)
    os.makedirs(alice)
    git(alice, "init", "-q", "-b", "main")
    write(alice, "index.html", "<p>hello</p>\n")
    git(alice, "add", "-A")
    git(alice, "commit", "-q", "-m", "c1")
    git(alice, "remote", "add", "origin", remote)
    git(alice, "push", "-q", "-u", "origin", "main")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL + ".git")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", URL)

    class W:
        pass

    w = W()
    w.tmp, w.home, w.remote, w.alice, w.mp = tmp_path, home, remote, alice, monkeypatch
    w.ws = tmp_path / "ws"
    return w


def _install(w, name="sine", **kw):
    """Bob's copy as a GitHub-link install leaves it (record_install)."""
    bob = os.path.join(str(w.ws), "app", name)
    os.makedirs(os.path.dirname(bob), exist_ok=True)
    git(str(w.tmp), "clone", "-q", URL, bob)
    classify.record_install(bob, URL)
    if kw:
        store.update(bob, lambda r: r.update(kw))
    return os.path.realpath(bob)


def _alice_push(w, name, text, msg="alice change"):
    write(w.alice, name, text)
    git(w.alice, "add", "-A")
    git(w.alice, "commit", "-q", "-m", msg)
    git(w.alice, "push", "-q", "origin", "main")
    return git(w.alice, "rev-parse", "HEAD").strip()


def _commit(repo, name, text, msg):
    write(repo, name, text)
    git(repo, "add", "-A", "--", ".")
    git(repo, "commit", "-q", "-m", msg, "--", ".")
    return git(repo, "rev-parse", "HEAD").strip()


def _remote_main(w):
    return git(w.remote, "rev-parse", "refs/heads/main").strip()


def _raw_push(repo):
    """`git push origin main` exactly as a terminal or a Claude session runs it."""
    return subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"],
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _sync(root, push=False):
    return git_upstream.sync_repo(root, action="test", push=push)


def _all_on(home):
    _prefs(home, contrib_sources_enabled=True, contrib_changes_enabled=True,
           contrib_inbox_enabled=True, contrib_loop_enabled=True)


def _ident():
    return {"login": "bob", "id": 7, "name": "Bob B", "email": "7+bob@users.noreply.github.com"}


def test_install_records_root_commit_and_push_guard(world):
    bob = _install(world)
    rec = store.get(bob)
    assert rec["v"] == 2 and rec["kind"] == "github"
    assert rec["root_commit"] == git(bob, "rev-list", "--max-parents=0", "HEAD").strip()
    assert git(bob, "config", "--get", "remote.origin.pushurl").strip() == classify.NO_PUSH_URL
    assert git(bob, "config", "--local", "--get", "fused.kind").strip() == "github"
    assert classify.kind_of(bob) == "github"


@pytest.mark.parametrize("flags_on", [{}, {"contrib_sources_enabled": True}])
def test_flags_off_github_copy_never_pushes_to_the_author(world, flags_on):
    """P0-1/P0-2 (audit F1, F1d, L1): a GitHub-link copy's Claude turn with
    the loop off (or only A1 on) used to `git push origin main` straight to
    the author. v2: the kind alone routes it to the engine, which never
    pushes; and no standing failure row quoting git."""
    _prefs(world.home, **flags_on)
    bob = _install(world)
    before = _remote_main(world)
    _commit(bob, "b.txt", "bob\n", "Claude: tweak")
    r = _sync(bob, push=True)
    assert r["status"] != "failed" and r.get("pushed") is not True
    assert _remote_main(world) == before
    assert git_upstream.sync_failures() == []


def test_unchecked_migration_record_never_pushes(world):
    """Audit F2: a pre-existing clone classified signed out (unknown owner)
    was synced plainly and pushed. v2: unknown owner counts as github."""
    _all_on(world.home)
    bob = _install(world, classified_by="migration", owner_checked=False)
    before = _remote_main(world)
    _commit(bob, "b.txt", "bob\n", "Claude: b")
    r = _sync(bob, push=True)
    assert r.get("pushed") is not True and _remote_main(world) == before


def test_engine_error_is_skipped_never_a_plain_push(world, monkeypatch):
    """P2's first lock: an engine exception is a silent `skipped`, never a
    fall-through to `_sync_plain`."""
    bob = _install(world)
    _commit(bob, "b.txt", "bob\n", "mine")
    from fused_render.contrib import engine as eng

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(eng, "sync", boom)
    monkeypatch.setattr(git_upstream, "_sync_plain",
                        lambda *a, **k: pytest.fail("plain sync of a GitHub-link copy"))
    before = _remote_main(world)
    r = _sync(bob, push=True)
    assert r == {"ok": True, "status": "skipped", "why": "contrib-error"}
    assert _remote_main(world) == before


def test_raw_git_push_is_refused_by_the_push_guard(world):
    """P1-5 (audit O2): a Claude session's own `git push origin main` reached
    the author (pushurl unset). v2: `no-push://fused` stops every push by
    remote name."""
    bob = _install(world)
    _commit(bob, "b.txt", "bob\n", "mine")
    before = _remote_main(world)
    assert _raw_push(bob).returncode != 0
    assert _remote_main(world) == before


def test_sign_in_rechecks_unknown_owners_whatever_the_flags(world, monkeypatch):
    """TL ruling 2026-10-19: the author's own repo installed while signed
    out (unknown owner → github, guarded) gets its auto-push back at
    sign-in, every flag off; someone else's copy gets the account's commit
    identity instead."""
    mine = _install(world, name="mine")  # alice/sine, owner unknown
    assert store.get(mine)["owner_checked"] is False and classify.is_push_guarded(mine)
    out = classify.after_sign_in({"login": "alice", "id": 1, "email": "1+alice@users.noreply.github.com"})
    assert out["rechecked"] == 1
    rec = store.get(mine)
    assert rec["kind"] == "own" and rec["owner_checked"] is True
    assert not classify.is_push_guarded(mine) and classify.kind_of(mine) == "own"
    theirs = _install(world, name="theirs")
    out = classify.after_sign_in(_ident())  # bob: not the owner
    assert out == {"rechecked": 1, "identity": 1}
    assert store.get(theirs)["kind"] == "github" and classify.is_push_guarded(theirs)
    assert git(theirs, "config", "--local", "--get", "user.email").strip() == _ident()["email"]


def test_push_guard_removed_when_kind_becomes_own(world):
    bob = _install(world)
    store.update(bob, lambda r: r.__setitem__("kind", "own"))
    assert classify.ensure_push_guard(bob, "own")
    assert git(bob, "config", "--get-all", "remote.origin.pushurl", check=False).strip() == ""
    assert git(bob, "config", "--local", "--get", "fused.kind", check=False).strip() == ""
    _commit(bob, "b.txt", "bob\n", "mine")
    assert _raw_push(bob).returncode == 0  # the author's own copy pushes as ever


def test_repointed_origin_is_reset_to_the_record(world):
    """P1-4 (audit O5): a `git remote set-url` (an app, a README, Claude)
    re-pointed the copy, and the plain path followed it. v2: the record's
    repo wins, with an activity line; nothing reaches the other repo."""
    evil = str(world.tmp / "evil.git")
    git(str(world.tmp), "init", "-q", "--bare", "-b", "main", evil)
    bob = _install(world)
    git(bob, "remote", "set-url", "origin", evil)
    _commit(bob, "b.txt", "bob\n", "mine")
    _sync(bob, push=True)
    assert git(bob, "config", "--get", "remote.origin.url").strip() == URL
    assert git(evil, "for-each-ref", "refs/heads/").strip() == ""
    lines = [a for a in store.activity(bob) if a["kind"] == "repointed"]
    assert len(lines) == 1 and REPO in lines[0]["text"]


def test_fused_dir_never_unprotected_and_never_committed(world, monkeypatch):
    """P0-3 (audit F4): A1 off cleared skip-worktree on the author's committed
    `.fused/`, and the next Claude-turn sweep committed this machine's state
    (then pushed it). v2: protected for any recorded copy whatever the flags,
    never undone, and every commit fused makes excludes `.fused` anyway."""
    os.makedirs(os.path.join(world.alice, ".fused"))
    _alice_push(world, ".fused/meta.json", '{"author": true}\n', "alice commits .fused")
    _all_on(world.home)
    bob = _install(world)
    write(bob, ".fused/meta.json", '{"machine": "bob-laptop"}\n')
    assert git(bob, "status", "--porcelain").strip() == ""
    _prefs(world.home)  # every flag off
    _sync(bob)
    assert git(bob, "status", "--porcelain").strip() == ""
    # Even unprotected by hand, fused's own commits leave it out.
    git(bob, "update-index", "--no-skip-worktree", "--", ".fused/meta.json")
    write(bob, "index.html", "<p>bob</p>\n")
    monkeypatch.setattr(git_upstream, "schedule_sync", lambda *a, **k: None)
    assert app_git.commit(os.path.join(bob, "index.html"), "Edit")
    assert git(bob, "show", "--name-only", "--format=", "HEAD").split() == ["index.html"]


def test_app_git_commits_in_a_github_copy_use_the_users_identity(world, monkeypatch):
    bob = _install(world)
    assert classify.apply_identity(bob, _ident())
    monkeypatch.setattr(git_upstream, "schedule_sync", lambda *a, **k: None)
    write(bob, "index.html", "<p>bob</p>\n")
    assert app_git.commit(os.path.join(bob, "index.html"), "Edit")
    out = subprocess.run(["git", "-C", bob, "log", "-1", "--format=%an <%ae>"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "Bob B <7+bob@users.noreply.github.com>"


def test_claude_turn_in_a_github_copy_credits_claude(world, monkeypatch):
    import importlib.util

    from fused_render.claude_agent import AGENT_PATH

    bob = _install(world)
    classify.apply_identity(bob, _ident())
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(world.ws))
    spec = importlib.util.spec_from_file_location("test_contrib_agent", AGENT_PATH)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "_request_sync", lambda app_dir: None)
    write(bob, "index.html", "<p>by claude</p>\n")
    agent._commit_turn(os.path.join(bob, "index.html"), "make it pretty")
    out = subprocess.run(["git", "-C", bob, "log", "-1", "--format=%an <%ae>%n%B"],
                         capture_output=True, text=True).stdout
    assert out.startswith("Bob B <7+bob@users.noreply.github.com>\nClaude: make it pretty")
    assert "Co-authored-by: Claude <noreply@anthropic.com>" in out


def test_switch_card_hidden_for_a_github_copy_while_the_panel_exists(world):
    """P2-8 (audit O1): with A1 on (the panel's Go back), a GitHub-link copy
    on another branch gets no old Switch card; Update is never offered for
    one (round 2 keeps the Switch row only with A1 off — next test)."""
    _prefs(world.home, contrib_sources_enabled=True)
    bob = _install(world)
    git(bob, "checkout", "-q", "-b", "feature")
    _alice_push(world, "c.txt", "alice\n")
    git_upstream.note_app_opened(bob, _runner=lambda fn: fn())
    assert git_upstream.known_repos() == []
    assert git_upstream.update_repo(bob)["reason"] == "contrib"
    assert git_upstream.switch_repo(bob)["reason"] == "contrib"


# ------------------------------------------------------------- binding


def test_a_different_repo_at_the_path_drops_the_record(world):
    """P1-6 (audit R3, R3b): the folder replaced by a clone of another repo
    kept the old record — and the engine reset HEAD onto the old author's
    tip, deleting the new project's files. v2: bound to the root commit."""
    _all_on(world.home)
    a = _install(world)
    other = str(world.tmp / "other.git")
    git(str(world.tmp), "init", "-q", "--bare", "-b", "main", other)
    work = str(world.tmp / "otherwork")
    git(str(world.tmp), "clone", "-q", other, work)
    _commit(work, "x.txt", "x\n", "other project")
    git(work, "push", "-q", "origin", "main")
    shutil.rmtree(a)
    git(str(world.tmp), "clone", "-q", other, a)
    before = git(a, "rev-parse", "HEAD").strip()
    assert classify.kind_of(a) is None and store.get(a) is None
    assert store.get_raw(store.key(a)) is None  # dropped, not orphaned
    _sync(a)
    assert git(a, "rev-parse", "HEAD").strip() == before
    assert os.path.exists(os.path.join(a, "x.txt"))


def test_a_new_local_app_with_the_same_name_is_not_a_copy(world):
    """Audit R4: a brand-new app made where a deleted copy was got the old
    `github` record by folder key."""
    a = _install(world)
    shutil.rmtree(a)
    os.makedirs(a)
    git(a, "init", "-q", "-b", "main")
    _commit(a, "index.html", "<p>mine</p>\n", "New app from starter")
    assert classify.kind_of(a) is None


def test_v1_record_gets_its_root_commit_filled_on_first_read(world):
    bob = _install(world)
    store.update(bob, lambda r: r.pop("root_commit"))
    assert store.get_raw(store.key(bob)).get("root_commit") is None
    assert store.get(bob)["root_commit"]
    assert store.get_raw(store.key(bob))["root_commit"]


def test_fresh_reinstall_at_the_same_path_starts_clean(world):
    """Audit R2: deleting the copy in Finder and installing the link again
    inherited the old proposal and overlap. v2: a fresh place drops the
    folder's record before recording the install."""
    a = _install(world, proposal={"repo": REPO, "number": 7, "url": "u",
                                  "proposed_head": "d" * 40, "cache": {"state": "open"}},
                  needs_merge={"onto": "1" * 40, "purpose": "update"})
    shutil.rmtree(a)
    git(str(world.tmp), "clone", "-q", URL, a)
    appfile_git._drop_record(a)  # what install() does right after `_place`
    classify.record_install(a, URL)
    rec = store.get(a)
    assert rec["proposal"] is None and rec["needs_merge"] is None


def test_adoption_needs_the_same_root_commit_and_prefers_the_moved_copy(world):
    """Audit R1: of two orphans of one repo, adoption took the newest — the
    deleted sibling — not the moved copy holding the proposal."""
    import time as _t

    a = _install(world, name="sine")
    sent = _commit(a, "b.txt", "bob\n", "proposed")
    store.update(a, lambda r: r.__setitem__("proposal", {"repo": REPO, "number": 7,
                                                        "sent": sent, "cache": {"state": "open"}}))
    _t.sleep(0.01)
    b = _install(world, name="sine-2")
    c = os.path.join(str(world.ws), "app", "renamed")
    os.rename(a, c)
    shutil.rmtree(b)
    assert classify.ensure(c)["proposal"]["number"] == 7
    # An orphan with another root commit is never adopted.
    d = os.path.join(str(world.ws), "app", "unrelated")
    os.makedirs(d)
    git(d, "init", "-q", "-b", "main")
    _commit(d, "y.txt", "y\n", "y")
    git(d, "remote", "add", "origin", URL)
    store.put(os.path.join(str(world.ws), "app", "gone"),
              {**store.new_record("github", repo=REPO, branch="main"),
               "root_commit": "f" * 40, "proposal": {"number": 9}})
    assert classify.ensure(d) is None  # signed out: neither adopted nor classified


# ---------------------------------------------------------- v1 -> v2 + activity


def test_first_v2_sync_migrates_a_v1_record_and_deletes_v1_refs(world):
    bob = _install(world)
    head = git(bob, "rev-parse", "HEAD").strip()
    for ref in store.V1_REFS:
        git(bob, "update-ref", ref, head)
    git(bob, "update-ref", "refs/fused/backup/1-update", head)

    def v1(r):
        r.pop("v")
        r.update({"needs_merge": {"onto": head, "purpose": "needs-merge"},
                  "backups": [{"ref": "refs/fused/backup/1-update", "at": 5.0, "why": "update"}],
                  "proposal": {"number": 3, "pushed_head": head, "proposed_head": "x"}})

    store.update(bob, v1)
    _sync(bob)
    rec = store.get(bob)
    assert rec["v"] == 2 and rec["needs_merge"] is None and "backups" not in rec
    assert rec["proposal"]["sent"] == head and "pushed_head" not in rec["proposal"]
    assert store.referenced_backups(bob) == {"refs/fused/backup/1-update"}
    assert git(bob, "for-each-ref", "--format=%(refname)", "refs/fused/").split() \
        == ["refs/fused/backup/1-update"]


def test_v1_proposal_reads_as_sent(home, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    store.put(str(d), {"kind": "github", "repo": "alice/x",
                       "proposal": {"number": 1, "proposed_head": "abc"}})
    assert store.get(str(d))["proposal"]["sent"] == "abc"
    assert store.get(str(d))["proposal"]["checked_tip"] is None


def test_activity_api(home, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    store.put(str(d), store.new_record("github", repo="alice/x", branch="main"))
    first = store.add_activity(str(d), "update", "Got alice's latest")
    undo = store.add_activity(str(d), "moved", "Got alice's latest",
                              backup=["refs/fused/backup/9-x", "refs/fused/backup/9-x-wip"])
    lines = store.activity(str(d))
    assert [(a["id"], a["undo"]) for a in lines] == [(undo, True), (first, False)]
    assert set(lines[0]) == {"id", "at", "kind", "text", "undo"}
    assert store.activity_entry(str(d), undo)["backup"][1] == "refs/fused/backup/9-x-wip"
    assert store.referenced_backups(str(d)) == {"refs/fused/backup/9-x",
                                                "refs/fused/backup/9-x-wip"}
    assert store.mark_restored(str(d), undo)
    assert store.activity(str(d))[0]["undo"] is False
    for i in range(store.ACTIVITY_KEEP + 5):
        store.add_activity(str(d), "update", f"line {i}")
    lines = store.activity(str(d))
    assert len(lines) == store.ACTIVITY_KEEP
    assert lines[0]["text"] == f"line {store.ACTIVITY_KEEP + 4}"
    assert store.add_activity(str(tmp_path / "none"), "x", "y") is None


# ------------------------------------------------------------ own copies


def test_own_copy_divergence_goes_to_ownsync(world, monkeypatch):
    """§16.7: an own copy's diverged branch is `ownsync.resolve`'s; its None
    ("not handled") keeps today's diverged failure row."""
    import types

    import fused_render.contrib as pkg

    a = _install(world)
    store.update(a, lambda r: r.__setitem__("kind", "own"))
    _commit(a, "b.txt", "mine\n", "mine")
    _alice_push(world, "c.txt", "alice\n")
    calls = []
    fake = types.ModuleType("fused_render.contrib.ownsync")
    fake.resolve = lambda root, action, push: calls.append(push) or {
        "ok": True, "status": "synced", "pulled": 1, "pushed": push}
    monkeypatch.setitem(sys.modules, "fused_render.contrib.ownsync", fake)
    monkeypatch.setattr(pkg, "ownsync", fake, raising=False)
    assert _sync(a, push=True)["status"] == "synced" and calls == [True]
    fake.resolve = lambda root, action, push: None
    r = _sync(a)
    assert r["status"] == "failed" and r["failure"]["reason"] == "diverged"
    assert git_upstream.last_checked(a)


def test_own_copy_ahead_only_push_asks_ownsync_first(world, monkeypatch):
    """TL ruling (§16.7 rewind): only ahead → `ownsync.resolve` before the
    plain push (a GitHub rewind must not re-publish removed commits), and
    before an open's no-push sync too, so the rewind is named on the page
    at once rather than by a Publish now that then refuses; None falls
    through to today's sync/push."""
    import types

    import fused_render.contrib as pkg

    a = _install(world)
    store.update(a, lambda r: r.__setitem__("kind", "own"))
    _commit(a, "b.txt", "mine\n", "mine")
    before = _remote_main(world)
    fake = types.ModuleType("fused_render.contrib.ownsync")
    fake.resolve = lambda root, action, push: {"ok": True, "status": "skipped",
                                               "why": "github-changed"}
    monkeypatch.setitem(sys.modules, "fused_render.contrib.ownsync", fake)
    monkeypatch.setattr(pkg, "ownsync", fake, raising=False)
    assert _sync(a, push=True)["why"] == "github-changed"
    assert _remote_main(world) == before
    assert _sync(a)["why"] == "github-changed"  # an open asks the same question
    assert _remote_main(world) == before
    seen = []
    fake.note_seen = lambda root: seen.append(root)
    fake.resolve = lambda root, action, push: None
    assert _sync(a)["status"] == "synced"  # not handled: today's sync
    assert seen == [a]  # a plain own sync notes the GitHub tip it saw
    r = _sync(a, push=True)
    assert r["status"] == "synced" and r["pushed"] is True


def test_own_copy_divergence_through_the_real_ownsync(world):
    """The hook end to end: the author's own copy, behind AND ahead of GitHub
    (a commit made elsewhere), is settled by `ownsync.resolve` — never
    today's diverged row — and its push guard (left from a mistaken
    `github` classification) is gone."""
    _prefs(world.home, contrib_sources_enabled=True)
    a = _install(world)
    store.update(a, lambda r: r.__setitem__("kind", "own"))
    mine = _commit(a, "b.txt", "mine\n", "mine")
    tip = _alice_push(world, "c.txt", "alice\n")
    r = _sync(a, push=True)
    assert r["status"] == "synced" and r.get("pushed") is True, r
    assert not classify.is_push_guarded(a)
    assert subprocess.run(["git", "-C", a, "merge-base", "--is-ancestor", tip, "HEAD"]).returncode == 0
    assert git(world.remote, "log", "-1", "--format=%s", "main").strip() == "mine"
    assert mine  # picked onto the tip: a new id, same change


# ---------------------------------------------------------- install page


def test_install_page_get_the_latest_brings_it_in(world, monkeypatch):
    """§16.8's headline: "Get the latest" on a GitHub-link copy with nothing
    of its own is the engine's Bring in."""
    from fused_render import app_fused_dir, appfile

    bob = _install(world)
    tip = _alice_push(world, "c.txt", "alice\n")
    src = {"url": URL, "commit": tip}
    git(bob, "fetch", "-q", "origin")  # what the engine's own fetch leaves
    assert appfile_git.probe(bob, src) == "update"
    monkeypatch.setattr(appfile, "write_stamp", lambda *a, **k: None)
    monkeypatch.setattr(app_fused_dir, "ensure", lambda *a, **k: None)
    target = {"git": src, "state": "update", "path": bob, "found_by": "git"}
    done = appfile_git.install("x.fused", target, {"entry": "index.html"}, force=False)
    assert done["action"] == "updated"
    assert git(bob, "rev-parse", "HEAD").strip() == tip


def test_install_page_for_a_github_copy_with_changes_is_plain(world):
    bob = _install(world)
    _commit(bob, "b.txt", "bob\n", "mine")
    tip = _alice_push(world, "c.txt", "alice\n")
    src = {"url": URL, "commit": tip}
    assert appfile_git.probe(bob, src) == "local"
    assert appfile_git.copy_view(bob) == {"who": "alice", "changes": True, "collision": [], "app_page": False}
    target = {"git": src, "state": "local", "path": bob, "found_by": "git"}
    with pytest.raises(appfile_git.GitInstallError) as exc:
        appfile_git.install("x.fused", target, {}, force=False)
    # Audit V1/V13 (§16.12): with A1 off the app page has no panel — plain
    # words, no pointer there.
    assert str(exc.value) == "This app has changes of its own, so it isn't updated from here."
    _prefs(world.home, contrib_sources_enabled=True)
    with pytest.raises(appfile_git.GitInstallError) as exc:
        appfile_git.install("x.fused", target, {}, force=False)
    msg = str(exc.value)
    assert msg.startswith("You have your own changes in this app. Open it to bring in")
    for word in ("git", "commit", "pull", "branch", "push"):
        assert word not in msg.lower()


def test_install_page_names_a_colliding_file_instead_of_offering_get_the_latest(world):
    """Audit contrib V3: Bob's own untracked NOTES.md, Alice adds NOTES.md.
    The copy has not taken the update in, so the engine sees no collision —
    but the link's commit does: the page says `local` naming the file, never
    a "Get the latest" that refuses."""
    bob = _install(world)
    write(bob, "NOTES.md", "bob's notes\n")
    tip = _alice_push(world, "NOTES.md", "alice\n", "notes")
    git(bob, "fetch", "-q", "origin")  # the copy knows the commit (no staged clone here)
    src = {"url": URL, "commit": tip}
    assert appfile_git.probe(bob, src) == "local"
    assert appfile_git.copy_view(bob, src)["collision"] == ["NOTES.md"]
    target = {"git": src, "state": "local", "path": bob, "found_by": "git"}
    with pytest.raises(appfile_git.GitInstallError) as exc:
        appfile_git.install("x.fused", target, {}, force=False)
    assert "NOTES.md" in str(exc.value)
    with open(os.path.join(bob, "NOTES.md")) as f:
        assert f.read() == "bob's notes\n"


# ------------------------------------------------------ review round 1 (§16.12)


def test_pre_loop_author_app_keeps_auto_pushing_while_signed_out(world, monkeypatch):
    """Audit author V4: A1 on, signed out, a pre-loop app of Alice's (no
    record) was classified github/unchecked and stopped reaching GitHub.
    Now: no record until an identity exists; today's sync pushes."""
    _prefs(world.home, contrib_sources_enabled=True)
    monkeypatch.setattr(classify, "ensure_in_background", lambda d: classify.ensure(d))
    lap = os.path.join(str(world.ws), "app", "sine")
    os.makedirs(os.path.dirname(lap), exist_ok=True)
    git(str(world.tmp), "clone", "-q", URL, lap)
    mine = _commit(lap, "c.txt", "c\n", "Claude: c")
    r = _sync(os.path.realpath(lap), push=True)
    assert store.get(lap) is None and r["pushed"] is True and _remote_main(world) == mine


def test_kind_lookup_failure_is_skipped_not_a_plain_push(world, monkeypatch):
    """Audit V7: an exception deciding the kind fell through to `_sync_plain`
    (a push, and a failure row quoting git). Now: skipped, nothing moves."""
    bob = _install(world)
    _commit(bob, "b.txt", "bob\n", "bob")

    def boom(*a, **k):
        raise RuntimeError("store broke")

    monkeypatch.setattr(classify, "kind_of", boom)
    before = _remote_main(world)
    r = _sync(bob, push=True)
    assert r == {"ok": True, "status": "skipped", "why": "contrib-error"}
    assert _remote_main(world) == before and git_upstream.sync_failures() == []


def test_push_by_url_is_refused_by_the_pre_push_hook(world):
    """Audit V6: the pushurl guard only covers pushes by remote NAME; a push
    by URL went straight to the author. Now a pre-push hook refuses every
    push; fused's own pushes skip hooks; the person's own hook comes back
    when the copy becomes theirs."""
    bob = _install(world)
    _commit(bob, "b.txt", "bob\n", "bob")
    before = _remote_main(world)
    by_url = subprocess.run(["git", "-C", bob, "push", "-q", URL, "HEAD:main"],
                            capture_output=True, text=True)
    assert by_url.returncode != 0 and "can't be sent from here" in by_url.stderr
    assert _remote_main(world) == before
    fused = subprocess.run(["git", "-C", bob, "-c", "core.hooksPath=/dev/null", "push", "-q",
                            URL, "HEAD:refs/heads/fused-test"], capture_output=True, text=True)
    assert fused.returncode == 0, fused.stderr
    hook = os.path.join(bob, ".git", "hooks", "pre-push")
    # The person's own hook is kept aside and restored on `own`.
    assert classify.ensure_push_guard(bob, "own") and not os.path.exists(hook)
    with open(hook, "w") as f:
        f.write("#!/bin/sh\n# mine\nexit 0\n")
    assert classify.ensure_push_guard(bob, "github")
    assert classify.HOOK_MARK in open(hook).read()
    assert classify.ensure_push_guard(bob, "own")
    assert open(hook).read() == "#!/bin/sh\n# mine\nexit 0\n"


def test_sign_in_upgrade_holds_unpublished_work_until_publish_now(world, monkeypatch):
    """Audit V8: a signed-out install upgraded to own at sign-in auto-pushed
    every commit the page had said stays local. Now: held (+ activity line),
    no automatic push until Publish now — with A1 on, where that button is
    (round 2). ownsync is stubbed out ("not handled") so this pins
    git_upstream's own guard; ownsync's handling of `hold` is its suite's."""
    import types

    import fused_render.contrib as pkg

    _prefs(world.home, contrib_sources_enabled=True)
    fake = types.ModuleType("fused_render.contrib.ownsync")
    fake.resolve = lambda root, action, push: None
    fake.note_seen = lambda root: None
    monkeypatch.setitem(sys.modules, "fused_render.contrib.ownsync", fake)
    monkeypatch.setattr(pkg, "ownsync", fake, raising=False)
    bob = _install(world)
    _commit(bob, "b1.txt", "x\n", "local 1")
    _commit(bob, "b2.txt", "x\n", "local 2")
    before = _remote_main(world)
    monkeypatch.setattr(classify, "_owns", lambda owner, ident: True)
    assert classify.after_sign_in({"login": "alice", "id": 1})["rechecked"] == 1
    assert classify.kind_of(bob) == "own" and classify.is_held(bob)
    assert store.get(bob)["hold"]["count"] == 2 and store.get(bob)["own_state"] is None
    assert "2 edits are ready to publish" in store.activity(bob)[0]["text"]
    from fused_render import app_doctor

    assert app_doctor._contrib_git_row(bob)["detail"] == app_doctor._HELD_DETAIL
    _commit(bob, "b3.txt", "x\n", "Claude: b3")
    r = _sync(bob, push=True)
    assert r["status"] == "synced" and r["pushed"] is False and r["held"] is True
    assert _remote_main(world) == before
    r = git_upstream.sync_repo(bob, action=git_upstream.PUBLISH_NOW, push=True)
    assert r["pushed"] is True and _remote_main(world) == git(bob, "rev-parse", "HEAD").strip()
    assert not classify.is_held(bob)


def test_v1_record_over_a_different_repo_is_dropped_not_bound(world):
    """Audit V11: a v1 record (no root commit) whose folder now holds a
    different repo got bound to it and pointed its origin at Alice."""
    a = os.path.join(str(world.ws), "app", "sine")
    rec = store.new_record("github", repo=REPO, branch="main", classified_by="install")
    for v2_only in ("root_commit", "v", "copy_id"):
        rec.pop(v2_only)
    other = str(world.tmp / "other.git")
    git(str(world.tmp), "init", "-q", "--bare", "-b", "main", other)
    w = str(world.tmp / "ow")
    git(str(world.tmp), "clone", "-q", other, w)
    _commit(w, "x.txt", "x\n", "other")
    git(w, "push", "-q", "origin", "main")
    os.makedirs(os.path.dirname(a), exist_ok=True)
    git(str(world.tmp), "clone", "-q", other, a)
    store.put(a, rec)
    _sync(os.path.realpath(a))
    assert classify.kind_of(a) is None and store.get_raw(store.key(a)) is None
    assert git(a, "remote", "get-url", "origin").strip() == other


def test_record_sync_failure_helper_writes_todays_row(world):
    r = git_upstream.record_sync_failure("/x/app", "diverged", action="Sync", push=False,
                                         message="Your copy and the author's both changed.")
    assert r["status"] == "failed" and r["failure"]["reason"] == "diverged"
    assert git_upstream.sync_failures()[0]["output"] == "Your copy and the author's both changed."


def test_an_own_copys_auto_push_still_runs_its_authors_hooks(world):
    """Round 2 (author R3, §16.0 P5): an own copy's plain fetch/pull/push is
    today's — the author's pre-push hook (a secret scanner) still runs on
    auto-push; only fused's private reads are hook-free."""
    a = _install(world)
    store.update(a, lambda r: r.__setitem__("kind", "own"))
    classify.ensure_push_guard(a, "own")
    hook = os.path.join(a, ".git", "hooks", "pre-push")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho secret-scanner-said-no >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    _commit(a, "b.txt", "mine\n", "mine")
    before = _remote_main(world)
    r = _sync(a, push=True)
    assert r["status"] == "failed" and "secret-scanner-said-no" in r["failure"]["output"]
    assert _remote_main(world) == before


def test_commits_in_a_github_copy_never_run_its_fsmonitor_or_hooks(world, monkeypatch):
    """rev-sec r2: a GitHub-link copy's `.git/config` is the side its app
    (or a Claude session in it) can write. A planted `core.fsmonitor`
    command and a pre-commit hook must not run when fused commits there —
    app_git or the Claude-turn sweep. (An own copy keeps its author's.)"""
    import importlib.util

    from fused_render.claude_agent import AGENT_PATH

    bob = _install(world)
    marker = str(world.tmp / "ran.txt")
    script = str(world.tmp / "evil.sh")
    with open(script, "w") as f:
        f.write(f"#!/bin/sh\necho ran >> {marker}\nexit 1\n")
    os.chmod(script, 0o755)
    git(bob, "config", "core.fsmonitor", script)
    hook = os.path.join(bob, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write(f"#!/bin/sh\necho hook >> {marker}\nexit 1\n")
    os.chmod(hook, 0o755)
    monkeypatch.setattr(git_upstream, "schedule_sync", lambda *a, **k: None)
    write(bob, "index.html", "<p>bob</p>\n")
    assert app_git.commit(os.path.join(bob, "index.html"), "Edit")
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(world.ws))
    spec = importlib.util.spec_from_file_location("test_contrib_agent_safe", AGENT_PATH)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "_request_sync", lambda app_dir: None)
    write(bob, "index.html", "<p>by claude</p>\n")
    agent._commit_turn(os.path.join(bob, "index.html"), "tweak")
    assert git(bob, "log", "-1", "--format=%s").strip() == "Claude: tweak"
    assert not os.path.exists(marker)


def test_sign_in_upgrade_with_flags_off_is_their_repo_auto_pushing(world, monkeypatch):
    """Round 2 R7: with A1 off there is no Publish now anywhere — the
    upgrade holds nothing; it is their repo and auto-pushes as today."""
    bob = _install(world)
    _commit(bob, "b1.txt", "x\n", "l1")
    monkeypatch.setattr(classify, "_owns", lambda owner, ident: True)
    classify.after_sign_in({"login": "alice", "id": 1})
    assert classify.kind_of(bob) == "own" and not classify.is_held(bob)
    assert store.get(bob)["hold"] is None
    assert store.activity(bob)[0]["text"] == classify.PUBLISHED_AS_USUAL
    r = _sync(bob, push=True)
    assert r["pushed"] is True and _remote_main(world) == git(bob, "rev-parse", "HEAD").strip()


def test_unknown_owner_wording_never_promises_the_edits_stay(world):
    """Final gate (R7): a GitHub-link copy whose owner fused couldn't ask
    about may turn out to be yours (then it publishes) — Doctor and the git
    panel say "until fused knows whether this app is yours"."""
    import importlib.util

    from fused_render import app_doctor

    bob = _install(world)  # signed out: owner unchecked
    _sync(bob)
    row = app_doctor._contrib_git_row(bob)
    assert row["detail"] == ("Your changes stay on this computer until fused knows "
                             "whether this app is yours.")
    ops_path = os.path.join(os.path.dirname(app_git.__file__), "templates", "git", "ops.py")
    spec = importlib.util.spec_from_file_location("ops_for_contrib_test", ops_path)
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    assert ops._push_blocked(bob).endswith("until fused knows whether this app is yours.")
    store.update(bob, lambda r: r.__setitem__("owner_checked", True))
    _sync(bob)
    assert "until fused knows" not in ops._push_blocked(bob)
    assert "until you propose them" in app_doctor._contrib_git_row(bob)["detail"]


def test_a1_off_while_held_says_so_before_publishing(world, monkeypatch):
    """Final gate: A1 switched off while held — the next push publishes the
    held edits; one activity line says so and the hold is dropped."""
    _prefs(world.home, contrib_sources_enabled=True)
    bob = _install(world)
    _commit(bob, "b1.txt", "x\n", "l1")
    monkeypatch.setattr(classify, "_owns", lambda owner, ident: True)
    classify.after_sign_in({"login": "alice", "id": 1})
    assert classify.is_held(bob)
    _prefs(world.home)  # A1 off
    r = _sync(bob, push=True)
    assert r["pushed"] is True and store.get(bob)["hold"] is None
    assert store.activity(bob)[0]["text"] == classify.PUBLISHED_AS_USUAL


def test_a_finder_duplicate_of_a_github_copy_is_a_github_copy(world):
    """Round 2 R4: a Finder duplicate carries fused.kind=github, the guard
    and the copy id, but no record — it got the plain sync (a push attempt
    and a git-worded failure row every turn). Now it gets its own github
    record (cloned from the live copy, fresh copy id), whatever the
    identity state; a marked folder that can't be recorded is skipped."""
    bob = _install(world)
    store.update(bob, lambda r: r.__setitem__("proposal", {"repo": REPO, "number": 3,
                                                          "sent": "a" * 40}))
    dup = os.path.join(str(world.ws), "app", "sine copy")
    shutil.copytree(bob, dup, symlinks=True)
    dup = os.path.realpath(dup)
    _commit(dup, "c.txt", "x\n", "Claude: c")
    before = _remote_main(world)
    r = _sync(dup, push=True)
    assert r["status"] != "failed" and git_upstream.sync_failures() == []
    assert _remote_main(world) == before
    rec = store.get(dup)
    assert rec["kind"] == "github" and rec["repo"] == REPO and rec["proposal"] is None
    assert rec["classified_by"] == "duplicate" and rec["copy_id"] != store.get(bob)["copy_id"]
    assert git(dup, "config", "--get", classify.COPY_ID_CONFIG).strip() == rec["copy_id"]
    # A marked folder whose origin names no GitHub repo: skipped, no push.
    odd = os.path.join(str(world.ws), "app", "odd")
    shutil.copytree(dup, odd, symlinks=True)
    git(odd, "remote", "set-url", "origin", str(world.remote))
    git(odd, "config", "--unset", classify.COPY_ID_CONFIG)
    assert _sync(os.path.realpath(odd), push=True)["why"] == "contrib-unrecorded"
    assert _remote_main(world) == before


def test_flags_off_switch_card_stays_for_a_github_copy_on_another_branch(world):
    """Round 2 R5: with A1 off (no app-page Go back) a GitHub-link copy on
    another branch keeps today's Switch card; with A1 on it is hidden."""
    bob = _install(world)
    git(bob, "checkout", "-q", "-b", "feature")
    _alice_push(world, "c.txt", "alice\n")
    git_upstream.note_app_opened(bob, _runner=lambda fn: fn())
    rows = git_upstream.known_repos()
    assert [(r["branch"], r["on_default"], r["behind"]) for r in rows] == [("feature", False, 1)]
    assert git_upstream.switch_repo(bob)["ok"] is True
    _prefs(world.home, contrib_sources_enabled=True)
    assert git_upstream.known_repos() == []


def test_a_github_copys_failure_row_never_invites_a_push(world):
    """Round 2 R6: the flags-off rows the engine records for a GitHub-link
    copy quoted `git pull` and their Fix prompt ended "ask me whether to
    push". Now: plain title, no command, push False, and a prompt saying
    the copy follows the repo and must never be pushed."""
    bob = _install(world)
    r = git_upstream._record_failure(bob, "diverged", action="Auto-update on app open",
                                     command="git pull --ff-only -- origin main",
                                     output="Your changes and alice's latest overlap.",
                                     push=False)
    assert r["command"] == "" and r["push"] is False and r["follows"] == REPO
    assert "git" not in r["title"].lower()
    assert "NEVER be pushed" in r["prompt"] and f"https://github.com/{REPO}" in r["prompt"]
    plain = git_upstream._record_failure("/x/other", "diverged", action="a", command="git pull",
                                         output="o", push=True)
    assert plain["command"] == "git pull" and "prompt" not in plain


def test_a_github_record_without_root_commit_over_an_unrelated_repo_is_dropped(world):
    """Round 2 R8: the V11 fix only covered v1 rows; a github row made
    without a root commit still bound to whatever repo was there."""
    a = os.path.join(str(world.ws), "app", "sine")
    rec = store.new_record("github", repo=REPO, branch="main", classified_by="install")
    for k in ("root_commit", "copy_id", "last_head"):
        rec.pop(k, None)
    other = str(world.tmp / "other.git")
    git(str(world.tmp), "init", "-q", "--bare", "-b", "main", other)
    w = str(world.tmp / "ow")
    git(str(world.tmp), "clone", "-q", other, w)
    _commit(w, "x.txt", "x\n", "other")
    git(w, "push", "-q", "origin", "main")
    os.makedirs(os.path.dirname(a), exist_ok=True)
    git(str(world.tmp), "clone", "-q", other, a)
    store.put(a, rec)
    _sync(os.path.realpath(a))
    assert classify.kind_of(a) is None
    assert git(a, "remote", "get-url", "origin").strip() == other


def test_binding_never_drops_a_v2_record_over_its_origin(world):
    """§16.1: binding is on the root commit ONLY. An own copy's origin is its
    author's to change (a rename, a transfer); a v2 row missing its root
    commit is filled whatever the origin says. Only a v1 row is judged by
    origin (round-1 item 5)."""
    a = _install(world)
    store.update(a, lambda r: r.update({"kind": "own"}))
    git(a, "remote", "set-url", "origin", "https://github.com/alice/renamed")
    store.update(a, lambda r: r.__setitem__("root_commit", None))
    rec = store.get(a)
    assert rec is not None and rec["kind"] == "own" and rec["root_commit"]
    _sync(a)
    assert store.get(a)["repo"] == REPO
    assert git(a, "remote", "get-url", "origin").strip() == "https://github.com/alice/renamed"


def test_a_fresh_clone_never_adopts_a_deleted_copys_record(world, monkeypatch):
    """Audit V12: a copy with an open proposal deleted in Finder, the same
    repo `git clone`d into a NEW folder: the orphan (proposal and all) was
    adopted. A fresh clone carries no `fused.copyid`: never adopts."""
    monkeypatch.setattr(classify, "_identity", lambda: {"login": "bob"})
    monkeypatch.setattr(classify, "_role_cache", {})
    monkeypatch.setattr(classify, "_gh", lambda: type("G", (), {
        "org_role": staticmethod(lambda org: "member")}))
    a = _install(world)
    head = git(a, "rev-parse", "HEAD").strip()
    store.update(a, lambda r: r.__setitem__("proposal", {"repo": REPO, "number": 7,
                                                        "sent": head, "cache": {"state": "open"}}))
    shutil.rmtree(a)
    n = os.path.join(str(world.ws), "app", "sine-fresh")
    git(str(world.tmp), "clone", "-q", URL, n)
    rec = classify.ensure(n)
    assert rec["proposal"] is None and rec["classified_by"] == "migration"
    assert store.get_raw(store.key(a))["proposal"]["number"] == 7  # still an orphan


def test_two_copies_of_one_repo_the_moved_one_is_adopted(world, monkeypatch):
    """Audit V10: two copies share one root commit. Finder: move A to C,
    delete B (newer). C adopted the NEWEST orphan, B's. Now: the one whose
    copy id travelled with the folder."""
    import time as _t

    a = _install(world, name="sine")
    store.update(a, lambda r: r.__setitem__("skip_tip", "a" * 40))
    _t.sleep(0.01)
    b = _install(world, name="sine-2")
    store.update(b, lambda r: r.__setitem__("skip_tip", "b" * 40))
    c = os.path.join(str(world.ws), "app", "renamed")
    os.rename(a, c)
    shutil.rmtree(b)
    rec = classify.ensure(c)
    assert rec is not None and rec["classified_by"] == "install"
    assert store.get_raw(store.key(b)) is not None and store.get_raw(store.key(a)) is None


def test_an_orphan_from_before_copy_ids_needs_its_last_head(world):
    """Orphans without a copy id: adopted only when the folder's HEAD is or
    contains their `last_head` (and a sent proposal), best match first."""
    a = _install(world, name="sine")
    mine = _commit(a, "b.txt", "bob\n", "mine")
    git(a, "config", "--local", "--unset", classify.COPY_ID_CONFIG)

    def legacy(r):
        r.pop("copy_id")
        r["last_head"] = mine
    store.update(a, legacy)
    c = os.path.join(str(world.ws), "app", "moved")
    os.rename(a, c)
    store.put(os.path.join(str(world.ws), "app", "other"),
              {**store.new_record("github", repo=REPO, branch="main"),
               "copy_id": None, "root_commit": store.get_raw(store.key(a))["root_commit"],
               "last_head": "e" * 40, "classified_at": 9e12})
    rec = classify.ensure(c)
    assert rec is not None and rec["last_head"] == mine


def test_link_install_place_drops_the_old_record_and_guards_the_clone(world, monkeypatch):
    """The real `_place` path of a link install: a deleted copy's stale
    record at the same folder is gone, the clone is recorded `github`,
    push-guarded, hooked and stamped."""
    from fused_render import app_fused_dir, appfile

    dest = os.path.join(str(world.ws), "app", "sine")
    os.makedirs(dest)
    store.put(dest, {**store.new_record("github", repo=REPO, branch="main"),
                     "root_commit": None, "proposal": {"number": 7, "sent": "d" * 40}})
    shutil.rmtree(dest)
    tip = git(world.alice, "rev-parse", "HEAD").strip()
    stage = os.path.join(appfile_git._staging_root(URL), "sine")
    os.makedirs(os.path.dirname(stage), exist_ok=True)
    git(str(world.tmp), "clone", "-q", URL, stage)
    name, app_id = appfile_git.expected_identity(stage, URL)
    monkeypatch.setattr(appfile, "write_stamp", lambda *a, **k: None)
    monkeypatch.setattr(app_fused_dir, "ensure", lambda *a, **k: None)
    target = {"git": {"url": URL, "commit": tip}, "state": "new", "path": dest, "found_by": None}
    manifest = {"name": name, "app_id": app_id, "entry": "index.html"}
    done = appfile_git.install("x.fused", target, manifest, force=False)
    assert done["action"] == "installed"
    rec = store.get(dest)
    assert rec["kind"] == "github" and rec["proposal"] is None and rec["classified_by"] == "install"
    assert classify.is_push_guarded(dest)
    assert classify.HOOK_MARK in open(os.path.join(dest, ".git", "hooks", "pre-push")).read()
    assert git(dest, "config", "--local", "--get", classify.COPY_ID_CONFIG).strip() == rec["copy_id"]
