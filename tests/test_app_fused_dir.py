"""The `.fused/` app state folder (fused_render/app_fused_dir.py, D548, SPEC §47).

`ensure` is called on the render path, so every test here is really asking one
of two questions: does it produce the documented layout, and does it stay quiet
and additive when the folder is not what it expected? The second is the one
worth the coverage — a helper that raises here fails the render that triggered
it, and a helper that overwrites here destroys the user's own data.
"""
import json
import os
import stat

import pytest

from fused_render import app_fused_dir


@pytest.fixture()
def app(tmp_path):
    d = tmp_path / "Fused" / "local" / "demo"
    d.mkdir(parents=True)
    (d / "index.html").write_text('<html><head><meta name="fused-app" /></head></html>')
    return d


# ------------------------------------------------------------------ the layout

def test_ensure_creates_data_cache_and_meta(app):
    assert app_fused_dir.ensure(str(app)) is True

    assert os.path.isdir(app / ".fused" / "data")
    assert os.path.isdir(app / ".fused" / "cache")

    meta = json.loads((app / ".fused" / "meta.json").read_text())
    assert meta["version"] == app_fused_dir.META_VERSION
    # The absolute path of the app dir — the whole point of the file.
    assert meta["app_dir"] == os.path.abspath(str(app))
    assert meta["created_at"]


def test_path_helpers_agree_with_what_ensure_built(app):
    app_fused_dir.ensure(str(app))
    assert os.path.isdir(app_fused_dir.data_dir(str(app)))
    assert os.path.isdir(app_fused_dir.cache_dir(str(app)))
    assert os.path.isfile(app_fused_dir.meta_path(str(app)))
    assert app_fused_dir.dot_fused(str(app)) == str(app / ".fused")


def test_ensure_is_idempotent_and_never_disturbs_existing_contents(app):
    app_fused_dir.ensure(str(app))
    kept = app / ".fused" / "data" / "notes.json"
    kept.write_text('{"n": 1}')
    first = (app / ".fused" / "meta.json").read_text()

    assert app_fused_dir.ensure(str(app)) is True

    assert kept.read_text() == '{"n": 1}'
    assert (app / ".fused" / "meta.json").read_text() == first


def test_ensure_completes_a_half_made_folder(app):
    """An app that hand-made `data/` and nothing else still ends up whole —
    creation is additive, not all-or-nothing."""
    (app / ".fused" / "data").mkdir(parents=True)
    (app / ".fused" / "data" / "state.json").write_text("{}")

    assert app_fused_dir.ensure(str(app)) is True

    assert (app / ".fused" / "data" / "state.json").read_text() == "{}"
    assert os.path.isdir(app / ".fused" / "cache")
    assert os.path.isfile(app / ".fused" / "meta.json")


# ------------------------------------------------------- meta.json as a witness

@pytest.fixture()
def claude_store(tmp_path, monkeypatch):
    """A private ~/.claude/{projects,sessions} — `ensure` on a moved app
    relocates transcripts, and the suite must never touch the real store."""
    from fused_render import claude_session_move as csm

    projects = tmp_path / "claude" / "projects"
    sessions = tmp_path / "claude" / "sessions"
    projects.mkdir(parents=True)
    sessions.mkdir(parents=True)
    monkeypatch.setattr(csm, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(csm, "SESSIONS_DIR", str(sessions))
    return projects, sessions


def _transcript(projects, cwd, sid, extra_line=None):
    from fused_render.claude_session_move import munge

    bucket = projects / munge(cwd)
    bucket.mkdir(exist_ok=True)
    lines = [
        json.dumps({"type": "permission-mode", "permissionMode": "auto"}),
        json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}),
        json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": "ok"}}),
    ]
    if extra_line is not None:
        # Mid-file, not last: a half-written LAST line on a fresh transcript is
        # session_liveness's own "turn in flight" signal, and would hold it.
        lines.insert(1, extra_line)
    (bucket / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    (bucket / sid / "tool-results").mkdir(parents=True)
    (bucket / sid / "tool-results" / "x.txt").write_text("r")
    return bucket


def test_a_copied_app_keeps_its_recorded_path(app, caplog, claude_store):
    """The witness half of D548: the recorded folder still exists, so this is
    a copy — the sessions belong to the original and the record is left as
    evidence for the app to act on."""
    app_fused_dir.ensure(str(app))
    meta_file = app / ".fused" / "meta.json"
    original = app.parent / "original"
    original.mkdir()
    stale = json.loads(meta_file.read_text())
    stale["app_dir"] = str(original)
    meta_file.write_text(json.dumps(stale))

    with caplog.at_level("INFO", logger="fused_render.app_fused_dir"):
        assert app_fused_dir.ensure(str(app)) is True

    assert json.loads(meta_file.read_text())["app_dir"] == str(original)
    assert "a copy, not a move" in caplog.text


def test_a_moved_app_carries_its_claude_sessions_and_repoints_meta(app, claude_store):
    """The move half: the recorded folder is gone, so every transcript whose
    own cwd was the old path or under it goes to the bucket for the new path,
    cwd repointed, side dir along — and only then is `app_dir` fixed, with
    the move kept in `migrations`."""
    from fused_render.claude_session_move import munge

    projects, _ = claude_store
    app_fused_dir.ensure(str(app))
    meta_file = app / ".fused" / "meta.json"
    old = os.path.join(os.sep, "somewhere", "else", "demo")
    meta = json.loads(meta_file.read_text())
    meta["app_dir"] = old
    meta["custom"] = "kept"
    meta_file.write_text(json.dumps(meta))

    _transcript(projects, old, "aaaa-1", extra_line="not json {")
    _transcript(projects, os.path.join(old, "sub", "deeper"), "bbbb-2")
    # A sibling whose bucket name shares the prefix — NOT under the old root.
    sibling = _transcript(projects, old + "-old", "cccc-3")
    # Another cwd colliding into the same bucket as `old` (munge is lossy).
    collide = old.replace(os.sep + "demo", ".demo")
    bucket = projects / munge(old)
    (bucket / "dddd-4.jsonl").write_text(json.dumps({"type": "user", "cwd": collide}) + "\n")
    (bucket / "memory").mkdir()
    (bucket / "memory" / "MEMORY.md").write_text("# m")

    assert app_fused_dir.ensure(str(app)) is True

    new_root = os.path.abspath(str(app))
    dst = projects / munge(new_root)
    moved = (dst / "aaaa-1.jsonl").read_text().splitlines()
    assert moved[0] == json.dumps({"type": "permission-mode", "permissionMode": "auto"})
    assert moved[1] == "not json {"
    assert json.loads(moved[2])["cwd"] == new_root
    assert (dst / "aaaa-1" / "tool-results" / "x.txt").read_text() == "r"
    sub = projects / munge(os.path.join(new_root, "sub", "deeper"))
    assert json.loads((sub / "bbbb-2.jsonl").read_text().splitlines()[1])["cwd"] == \
        os.path.join(new_root, "sub", "deeper")
    assert not (bucket / "aaaa-1.jsonl").exists()
    assert (sibling / "cccc-3.jsonl").exists()
    assert (bucket / "dddd-4.jsonl").exists()      # the collider stays
    assert (bucket / "memory" / "MEMORY.md").exists()  # bucket not emptied → memory stays

    fixed = json.loads(meta_file.read_text())
    assert fixed["app_dir"] == new_root
    assert fixed["custom"] == "kept"
    assert fixed["created_at"] == meta["created_at"]
    assert fixed["migrations"][0]["from"] == old
    assert fixed["migrations"][0]["to"] == new_root
    assert fixed["migrations"][0]["sessions"] == 2


def test_a_live_session_holds_the_move_back(app, claude_store):
    """A running session's transcript is being appended to; moving it would
    split the conversation. It stays, and so does the stale record — the next
    open retries."""
    projects, sessions = claude_store
    app_fused_dir.ensure(str(app))
    meta_file = app / ".fused" / "meta.json"
    old = os.path.join(os.sep, "somewhere", "else", "demo")
    meta = json.loads(meta_file.read_text())
    meta["app_dir"] = old
    meta_file.write_text(json.dumps(meta))
    _transcript(projects, old, "live-1")
    (sessions / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "live-1", "cwd": old}))

    assert app_fused_dir.ensure(str(app)) is True

    from fused_render.claude_session_move import munge
    assert (projects / munge(old) / "live-1.jsonl").exists()
    assert json.loads(meta_file.read_text())["app_dir"] == old
    assert "migrations" not in json.loads(meta_file.read_text())


def test_recorded_app_dir_matches_when_the_app_has_not_moved(app):
    app_fused_dir.ensure(str(app))
    recorded = app_fused_dir.recorded_app_dir(str(app))
    assert recorded is not None
    assert os.path.abspath(recorded) == os.path.abspath(str(app))


def test_recorded_app_dir_is_none_without_a_folder(app):
    assert app_fused_dir.recorded_app_dir(str(app)) is None
    assert app_fused_dir.read_meta(str(app)) is None


def test_an_unparseable_meta_is_left_alone(app):
    """It is a user-writable file in the user's folder. Overwriting it would
    destroy whatever they put there, and nothing in `ensure` needs it."""
    (app / ".fused").mkdir()
    (app / ".fused" / "meta.json").write_text("not json at all")

    assert app_fused_dir.ensure(str(app)) is True

    assert (app / ".fused" / "meta.json").read_text() == "not json at all"
    assert app_fused_dir.read_meta(str(app)) is None
    assert app_fused_dir.recorded_app_dir(str(app)) is None
    # …and the directories were still made.
    assert os.path.isdir(app / ".fused" / "cache")


def test_meta_holding_a_non_object_reads_as_absent(app):
    (app / ".fused").mkdir()
    (app / ".fused" / "meta.json").write_text("[1, 2, 3]")
    assert app_fused_dir.read_meta(str(app)) is None
    assert app_fused_dir.recorded_app_dir(str(app)) is None


def test_meta_with_a_blank_app_dir_reads_as_unrecorded(app):
    (app / ".fused").mkdir()
    (app / ".fused" / "meta.json").write_text(json.dumps({"version": 1, "app_dir": ""}))
    assert app_fused_dir.recorded_app_dir(str(app)) is None


# ------------------------------------------------------------------- refusals

def test_mount_backed_folders_are_refused(app, monkeypatch):
    """A remote mount is not the app's private disk, and this is the render
    path — no makedirs, no stat, nothing that can wedge a mount."""
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda _path: True)
    assert app_fused_dir.ensure(str(app)) is False
    assert not os.path.exists(app / ".fused")


def test_a_missing_folder_is_false_not_an_exception(tmp_path):
    assert app_fused_dir.ensure(str(tmp_path / "gone")) is False


def test_a_file_where_the_app_should_be_is_false(tmp_path):
    f = tmp_path / "notafolder"
    f.write_text("x")
    assert app_fused_dir.ensure(str(f)) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else False,
                    reason="root ignores the write bit")
def test_an_unwritable_app_folder_is_false_not_an_exception(app):
    """The failure a read-only medium produces. It must never reach the render
    that called it."""
    app.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert app_fused_dir.ensure(str(app)) is False
    finally:
        app.chmod(stat.S_IRWXU)
