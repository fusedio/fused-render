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

def test_a_moved_app_keeps_its_recorded_path(app, caplog):
    """THE decision (D548): the recorded `app_dir` is evidence that the folder
    moved, so `ensure` reports it and leaves it. Rewriting on sight would erase
    the divergence in the same moment an app could act on it."""
    app_fused_dir.ensure(str(app))
    meta_file = app / ".fused" / "meta.json"
    stale = json.loads(meta_file.read_text())
    stale["app_dir"] = os.path.join(os.sep, "somewhere", "else", "demo")
    meta_file.write_text(json.dumps(stale))

    with caplog.at_level("INFO", logger="fused_render.app_fused_dir"):
        assert app_fused_dir.ensure(str(app)) is True

    assert json.loads(meta_file.read_text())["app_dir"] == stale["app_dir"]
    assert app_fused_dir.recorded_app_dir(str(app)) == stale["app_dir"]
    assert "moved or copied" in caplog.text
    assert stale["app_dir"] in caplog.text


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
