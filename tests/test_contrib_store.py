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
    assert engine.owns(str(d)) is False


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


def test_github_clone_unchecked_without_identity(home, tmp_path):
    d = _clone(tmp_path, "x", "git@github.com:Alice/X.git")
    rec = classify.ensure(d)
    assert (rec["kind"], rec["repo"], rec["owner_checked"]) == ("github", "Alice/X", False)
    assert rec["classified_by"] == "migration"
    # A migration copy with unknown ownership is NOT owned yet: today's sync.
    _prefs(home, contrib_sources_enabled=True, contrib_changes_enabled=True)
    assert engine.owns(d) is False


def test_recheck_upgrades_to_own_once(home, tmp_path, monkeypatch):
    d = _clone(tmp_path, "x", "https://github.com/alice/x")
    classify.ensure(d)
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
    store.update(d, lambda r: r.__setitem__("proposal", {"number": 9}))
    # A Finder move the server never saw.
    moved = str(tmp_path / "ws" / "app" / "x-moved")
    os.rename(d, moved)
    rec = classify.ensure(moved)
    assert rec["proposal"] == {"number": 9} and rec["classified_by"] == "install"
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
    assert engine._event(store.get(str(d)))["kind"] == "updated-kept"


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
