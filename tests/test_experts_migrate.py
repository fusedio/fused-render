"""Specialists become experts (experts/migrate.py): the old root moves across whole, what sat in the new one is
deleted, and nothing in a home still names the old folder."""
import json
import os

import pytest

from _experts_conftest import *  # noqa: F401,F403 — the experts fixtures (workspace, flag)
from _experts_conftest import make_expert, needs_git


@pytest.fixture
def old_home(monkeypatch, tmp_path):
    """An expert made the ordinary way, then put back under the names it had as a specialist."""
    from fused_render import claude_session_move as csm
    from fused_render.experts import chat, migrate, paths
    from fused_render.shell import storage

    import pathlib

    # Where the agent keeps transcripts in tests (conftest's own Claude dir), so both modules agree.
    projects = pathlib.Path(chat.transcript(str(tmp_path), "x")).parent.parent
    monkeypatch.setattr(csm, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(csm, "SESSIONS_DIR", str(tmp_path / "claude-sessions"))
    monkeypatch.setattr(storage, "home_dir", lambda: str(tmp_path / "app-home"))
    os.makedirs(tmp_path / "app-home", exist_ok=True)

    s = make_expert()
    new_root = paths.root()
    old_root = migrate._old_root()
    name = os.path.basename(s.home)
    os.rename(new_root, old_root)
    home = os.path.join(old_root, name)
    os.rename(os.path.join(home, "expert.json"), os.path.join(home, "specialist.json"))
    os.rename(os.path.join(home, ".expert"), os.path.join(home, ".specialist"))
    with open(os.path.join(home, "memory.md"), "w", encoding="utf-8") as f:
        f.write("- [2 · 2026-11-20] a fact\n")
    bucket = projects / csm.munge(home)
    bucket.mkdir(parents=True)
    transcript = bucket / "sess-1.jsonl"
    transcript.write_text(json.dumps({"type": "user", "cwd": home, "sessionId": "sess-1",
                                      "message": {"content": "hi"}}) + "\n"
                          + json.dumps({"type": "assistant", "cwd": home, "sessionId": "sess-1",
                                        "message": {"content": [{"type": "text", "text": "hello"}]}}) + "\n")
    with open(os.path.join(home, ".specialist", "state.json"), "w", encoding="utf-8") as f:
        json.dump({"main": "sess-1", "folded": [{"session_id": "sess-gone", "at": 1}], "sources": {"sess-1": {"path": str(transcript), "line": 1, "file": ""}}}, f)
    with open(os.path.join(home, "inbox", "note.md"), "w", encoding="utf-8") as f:
        f.write(f"# Chat\n\nTranscript: {transcript}\nFrom line: 1\n")
    paths._reset()
    return {"id": s.id, "name": name, "old_root": old_root, "new_root": new_root, "chat": chat}


def test_the_old_root_moves_across_and_what_was_in_the_new_one_is_deleted(old_home):
    from fused_render.experts import migrate, paths, store

    os.makedirs(os.path.join(old_home["new_root"], "stray"))
    with open(os.path.join(old_home["new_root"], "stray", "expert.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "stray", "name": "Stray", "about": "x"}, f)

    migrate.run_once()

    import zipfile

    backup = os.path.join(os.path.dirname(migrate._stamp_path()), "backup")
    (name,) = os.listdir(backup)
    assert name.startswith("legacy-experts-") and name.endswith(".zip")
    with zipfile.ZipFile(os.path.join(backup, name)) as z:
        assert "stray/expert.json" in z.namelist()
    assert not os.path.exists(old_home["old_root"])
    assert sorted(n for n in os.listdir(old_home["new_root"]) if not n.startswith(".")) == [old_home["name"]]
    s = store.get(old_home["id"])
    assert s is not None and s.home == os.path.join(old_home["new_root"], old_home["name"])
    assert not os.path.exists(os.path.join(s.home, "specialist.json"))
    assert not os.path.exists(os.path.join(s.home, ".specialist"))
    with open(os.path.join(s.home, "memory.md"), encoding="utf-8") as f:
        assert f.read() == "- [2 · 2026-11-20] a fact\n"
    assert paths.root() == old_home["new_root"]


def test_nothing_in_a_moved_home_names_the_old_folder(old_home):
    from fused_render.experts import migrate, store

    migrate.run_once()

    s = store.get(old_home["id"])
    state = old_home["chat"].load(s.home)
    path = state["sources"]["sess-1"]["path"]
    assert state["main"] == "sess-1" and os.path.isfile(path)
    assert state["folded"] == []  # its transcript did not come across: no dead row for it
    assert path == old_home["chat"].transcript(s.home, "sess-1")
    with open(path, encoding="utf-8") as f:
        assert json.loads(f.readline())["cwd"] == s.home
    for rel in (".gitignore", "CLAUDE.md", os.path.join("inbox", "note.md"),
                os.path.join(".expert", "state.json")):
        with open(os.path.join(s.home, rel), encoding="utf-8") as f:
            text = f.read()
        assert "specialist" not in text.lower(), rel


def test_it_runs_once_so_a_later_old_root_never_deletes_experts(old_home):
    from fused_render.experts import migrate, store

    migrate.run_once()
    os.makedirs(os.path.join(old_home["old_root"], "again"))

    migrate.run_once()

    assert store.get(old_home["id"]) is not None
    assert os.path.isdir(os.path.join(old_home["old_root"], "again"))


def test_when_the_backup_cannot_be_written_nothing_is_deleted_and_it_retries(old_home, monkeypatch):
    from fused_render.experts import migrate

    stray = os.path.join(old_home["new_root"], "stray")
    os.makedirs(stray)
    open(os.path.join(stray, "memory.md"), "w").close()

    real = migrate.shutil.make_archive
    full = [True]

    def archive(*a, **k):
        if full[0]:
            raise OSError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(migrate.shutil, "make_archive", archive)
    migrate.run_once()
    assert os.path.isdir(stray) and os.path.isdir(old_home["old_root"])
    assert not os.path.exists(migrate._stamp_path())

    full[0] = False
    migrate.run_once()
    assert not os.path.exists(old_home["old_root"]) and not os.path.exists(stray)


def test_another_app_home_on_the_same_workspace_never_deletes_migrated_experts(old_home):
    from fused_render.experts import migrate, store

    migrate.run_once()
    os.remove(migrate._stamp_path())  # a second app home: no stamp of its own
    os.makedirs(os.path.join(old_home["old_root"], "made-by-an-old-build"))

    migrate.run_once()

    assert store.get(old_home["id"]) is not None
    assert os.path.isdir(os.path.join(old_home["old_root"], "made-by-an-old-build"))


def test_a_switch_left_by_the_removed_expert_bots_is_dropped(old_home, monkeypatch):
    from fused_render.experts import migrate
    from fused_render.shell import prefs

    monkeypatch.delenv("FUSED_RENDER_EXPERTS", raising=False)
    prefs.write_pref("experts_enabled", True)

    migrate.run_once()

    assert "experts_enabled" not in prefs.read_prefs()


def test_with_no_old_root_the_experts_are_left_alone_now_and_later(old_home):
    from fused_render.experts import migrate

    os.rename(old_home["old_root"], old_home["new_root"])  # a machine that only ever had experts
    migrate.run_once()
    assert os.path.isdir(os.path.join(old_home["new_root"], old_home["name"]))

    os.makedirs(old_home["old_root"])
    migrate.run_once()
    assert os.path.isdir(os.path.join(old_home["new_root"], old_home["name"]))


def test_the_stored_switch_moves_to_the_new_key(old_home, monkeypatch):
    from fused_render.experts import migrate
    from fused_render.shell import prefs

    monkeypatch.delenv("FUSED_RENDER_EXPERTS", raising=False)
    prefs.write_pref("specialists_enabled", True)
    prefs.write_pref("experts_enabled", False)

    migrate.run_once()

    data = prefs.read_prefs()
    assert data.get("experts_enabled") is True and "specialists_enabled" not in data


@needs_git
def test_the_renames_are_committed_so_changes_starts_clean(old_home):
    from fused_render.experts import history, migrate, store

    migrate.run_once()

    s = store.get(old_home["id"])
    assert history._git(s.home, "status", "--porcelain").stdout.strip() == ""
