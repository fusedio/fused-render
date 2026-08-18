"""Tests for the one-shot ~/Documents/Fused -> ~/Fused workspace migration
(fused_render/workspace_migration.py, D336).

Every test redirects HOME and FUSED_RENDER_HOME at a tmp dir and CLEARS
FUSED_RENDER_DIR (conftest sets it for the whole run), so the migration sees a
throwaway machine and never a real workspace.
"""
import json
import os

import pytest

from fused_render import workspace_migration as wm
from fused_render.shell import storage


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake home: (legacy dir, new dir, shell home dir). Nothing exists yet."""
    monkeypatch.delenv("FUSED_RENDER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser on Windows
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / ".fused-render"))
    legacy = tmp_path / "Documents" / "Fused"
    new = tmp_path / "Fused"
    return legacy, new, tmp_path / ".fused-render"


def _sidecar_dir(home, abs_path):
    parts = [p for p in storage._sidecar_subpath(str(abs_path)).split("/") if p]
    return os.path.join(str(home), "sidecar", *parts)


# ------------------------------------------------------------- the folder move

def test_moves_the_workspace_when_only_the_legacy_dir_exists(machine):
    legacy, new, _ = machine
    legacy.mkdir(parents=True)
    (legacy / "app" / ".git").mkdir(parents=True)
    (legacy / "app" / "index.html").write_text("mine", encoding="utf-8")

    wm.run()

    assert not legacy.exists()
    assert (new / "app" / "index.html").read_text(encoding="utf-8") == "mine"
    assert (new / "app" / ".git").is_dir()


def test_refuses_when_the_destination_already_exists(machine):
    legacy, new, _ = machine
    legacy.mkdir(parents=True)
    (legacy / "old.html").write_text("old", encoding="utf-8")
    new.mkdir(parents=True)
    (new / "kept.html").write_text("kept", encoding="utf-8")

    wm.run()

    # Neither side loses anything, and nothing is merged.
    assert (legacy / "old.html").read_text(encoding="utf-8") == "old"
    assert (new / "kept.html").read_text(encoding="utf-8") == "kept"
    assert not (new / "old.html").exists()


def test_no_op_when_the_legacy_dir_is_missing(machine):
    legacy, new, _ = machine

    wm.run()

    assert not legacy.exists()
    assert not new.exists()


def test_skipped_entirely_when_fused_render_dir_is_set(machine, tmp_path, monkeypatch):
    legacy, new, _ = machine
    custom = tmp_path / "chosen"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(custom))
    legacy.mkdir(parents=True)
    (legacy / "x.html").write_text("x", encoding="utf-8")

    wm.run()

    assert (legacy / "x.html").is_file()
    assert not new.exists()
    assert not custom.exists()


def test_second_run_after_a_successful_migration_is_a_no_op(machine):
    legacy, new, _ = machine
    legacy.mkdir(parents=True)
    (legacy / "a.html").write_text("a", encoding="utf-8")

    wm.run()
    (new / "b.html").write_text("b", encoding="utf-8")
    wm.run()

    assert (new / "a.html").read_text(encoding="utf-8") == "a"
    assert (new / "b.html").read_text(encoding="utf-8") == "b"
    assert not legacy.exists()


def test_a_move_failure_does_not_raise(machine, monkeypatch):
    legacy, new, _ = machine
    legacy.mkdir(parents=True)

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(wm.os, "rename", boom)

    wm.run()  # must not raise: startup continues even when migration cannot

    assert legacy.is_dir()


# ---------------------------------------------------------------- the sidecars

def test_renames_the_sidecar_subtree(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    (legacy / "a.parquet").write_text("", encoding="utf-8")
    old_side = _sidecar_dir(home, legacy)
    os.makedirs(os.path.join(old_side, "sub"), exist_ok=True)
    with open(os.path.join(old_side, "a.parquet.json"), "w", encoding="utf-8") as f:
        json.dump({"comments": [{"id": "c1", "text": "keep me"}]}, f)

    wm.run()

    new_side = _sidecar_dir(home, new)
    assert not os.path.exists(old_side)
    with open(os.path.join(new_side, "a.parquet.json"), encoding="utf-8") as f:
        assert json.load(f)["comments"][0]["text"] == "keep me"
    assert os.path.isdir(os.path.join(new_side, "sub"))


def test_sidecars_outside_the_workspace_are_untouched(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    other = _sidecar_dir(home, legacy.parent / "Elsewhere")
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "z.json"), "w", encoding="utf-8") as f:
        json.dump({"k": 1}, f)

    wm.run()

    assert os.path.isfile(os.path.join(other, "z.json"))


def test_an_existing_new_sidecar_subtree_is_never_clobbered(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    old_side = _sidecar_dir(home, legacy)
    new_side = _sidecar_dir(home, new)
    os.makedirs(old_side, exist_ok=True)
    os.makedirs(new_side, exist_ok=True)
    with open(os.path.join(old_side, "old.json"), "w", encoding="utf-8") as f:
        json.dump({"old": 1}, f)
    with open(os.path.join(new_side, "new.json"), "w", encoding="utf-8") as f:
        json.dump({"new": 1}, f)

    wm.run()

    assert os.path.isfile(os.path.join(old_side, "old.json"))
    assert os.path.isfile(os.path.join(new_side, "new.json"))


# ------------------------------------------------------------- the state files

def test_rewrites_community_install_paths(machine, monkeypatch):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    from fused_render import community

    installs = home / "community" / "installs.json"
    monkeypatch.setattr(community, "INSTALLS_JSON", str(installs))
    storage.write_json(str(installs), {"schema": 1, "installs": {
        "sine": {"path": str(legacy / "local" / "sine"), "commit": "abc"},
        "other": {"path": str(legacy.parent / "Elsewhere" / "app")},
    }})

    wm.run()

    data = storage.read_json(str(installs))
    assert data["installs"]["sine"]["path"] == str(new / "local" / "sine")
    assert data["installs"]["sine"]["commit"] == "abc"
    assert data["installs"]["other"]["path"] == str(legacy.parent / "Elsewhere" / "app")


def test_rewrites_bookmark_urls_including_nested_folders(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    bookmarks = os.path.join(str(home), "bookmarks.json")
    storage.write_json(bookmarks, [
        {"id": "1", "name": "app", "url": "/explorer/view"
         + str(legacy / "my app" / "index.html").replace(" ", "%20")
         + "?sort=name"},
        {"id": "2", "type": "folder", "name": "f", "children": [
            {"id": "3", "name": "deep", "url": "/explorer/view" + str(legacy / "d")},
        ]},
        {"id": "4", "name": "outside", "url": "/explorer/view/Users/x/notes"},
        {"id": "5", "name": "sentinel", "url": "/explorer/view/_prefs"},
    ])

    wm.run()

    items = storage.read_json(bookmarks)
    assert items[0]["url"] == ("/explorer/view"
                               + str(new / "my app" / "index.html").replace(" ", "%20")
                               + "?sort=name")
    assert items[1]["children"][0]["url"] == "/explorer/view" + str(new / "d")
    assert items[2]["url"] == "/explorer/view/Users/x/notes"
    assert items[3]["url"] == "/explorer/view/_prefs"


def test_rewrites_the_bookmark_file_sentinel_query(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    from urllib.parse import quote

    bookmarks = os.path.join(str(home), "bookmarks.json")
    url = ("/explorer/view/_bookmark?file="
           + quote(str(legacy / "saved.bookmark"), safe="") + "&x=1")
    storage.write_json(bookmarks, [{"id": "1", "name": "b", "url": url}])

    wm.run()

    got = storage.read_json(bookmarks)[0]["url"]
    assert got == ("/explorer/view/_bookmark?file="
                   + quote(str(new / "saved.bookmark"), safe="") + "&x=1")


def test_rewrites_recent_urls_and_keeps_their_params(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    recents = os.path.join(str(home), "recents.json")
    storage.write_json(recents, {"collapsed": False, "entries": [
        {"url": "/explorer/view" + str(legacy / "a.html") + "?_side=claude&run=",
         "openedAt": "2026-01-01T00:00:00+00:00", "title": "A"},
        {"url": "/explorer/view/Users/x/b.html", "openedAt": "x"},
    ]})

    wm.run()

    entries = storage.read_json(recents)["entries"]
    assert entries[0]["url"] == ("/explorer/view" + str(new / "a.html")
                                 + "?_side=claude&run=")
    assert entries[0]["title"] == "A"
    assert entries[1]["url"] == "/explorer/view/Users/x/b.html"


def test_rewrites_scheduled_message_targets(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    store = os.path.join(str(home), "scheduled_messages.json")
    storage.write_json(store, {"entries": [
        {"id": "a", "target": str(legacy / "proj"), "message": "hi"},
        {"id": "b", "target": str(legacy.parent / "Elsewhere"), "message": "yo"},
    ]})

    wm.run()

    entries = storage.read_json(store)["entries"]
    assert entries[0]["target"] == str(new / "proj")
    assert entries[0]["message"] == "hi"
    assert entries[1]["target"] == str(legacy.parent / "Elsewhere")


def test_state_rewrites_run_even_when_an_earlier_run_moved_the_folder(machine):
    """Re-entry after a crash between the folder move and the bookkeeping: the
    legacy dir is already gone, but the stale absolute paths must still heal."""
    legacy, new, home = machine
    new.mkdir(parents=True)
    recents = os.path.join(str(home), "recents.json")
    storage.write_json(recents, {"collapsed": False, "entries": [
        {"url": "/explorer/view" + str(legacy / "a.html"), "openedAt": "x"},
    ]})

    wm.run()

    entries = storage.read_json(recents)["entries"]
    assert entries[0]["url"] == "/explorer/view" + str(new / "a.html")


def test_missing_state_files_are_a_no_op(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)

    wm.run()

    assert not os.path.exists(os.path.join(str(home), "recents.json"))
    assert not os.path.exists(os.path.join(str(home), "bookmarks.json"))


def _pin_json(home, ref=""):
    """Where the menu-bar pin lives for `home` — under Application Support, NOT
    under the shell home dir, branch-nested exactly like it."""
    base = os.path.join(str(home), "Library", "Application Support", "fused-render")
    if ref:
        base = os.path.join(base, "branches", ref)
    return os.path.join(base, "pin.json")


def test_rewrites_the_menu_bar_pin(machine, tmp_path):
    """The pin (D97, SPEC 25 PV-7) is the one state path that does NOT live
    under home_dir(); a stale one leaves the pinned view unopenable."""
    legacy, new, _ = machine
    legacy.mkdir(parents=True)
    pin = _pin_json(tmp_path)
    storage.write_json(pin, {"path": str(legacy / "app" / "index.html"),
                             "size": [520, 640]})

    wm.run()

    data = storage.read_json(pin)
    assert data["path"] == str(new / "app" / "index.html")
    # The popover size is a window preference that survives re-pins
    # (pin_store.save_pin merges); the rewrite must not drop it either.
    assert data["size"] == [520, 640]


def test_a_pin_outside_the_workspace_is_left_alone(machine, tmp_path):
    legacy, _, _ = machine
    legacy.mkdir(parents=True)
    outside = str(tmp_path / "Elsewhere" / "note.html")
    pin = _pin_json(tmp_path)
    storage.write_json(pin, {"path": outside})

    wm.run()

    assert storage.read_json(pin) == {"path": outside}


def test_a_missing_pin_file_is_a_no_op(machine, tmp_path):
    legacy, _, _ = machine
    legacy.mkdir(parents=True)

    wm.run()

    assert not os.path.exists(_pin_json(tmp_path))


def test_a_corrupt_pin_file_is_a_no_op_and_never_raises(machine, tmp_path):
    """pin_store itself treats a bad pin.json as "no pin, never raise"; this
    matches that, and leaves the bytes exactly as they were."""
    legacy, _, _ = machine
    legacy.mkdir(parents=True)
    pin = _pin_json(tmp_path)
    os.makedirs(os.path.dirname(pin), exist_ok=True)
    with open(pin, "w", encoding="utf-8") as f:
        f.write("{not json at all")

    wm.run()

    assert open(pin, encoding="utf-8").read() == "{not json at all"

    with open(pin, "w", encoding="utf-8") as f:
        f.write('["a list, not the pin object"]')

    wm.run()

    assert storage.read_json(pin) == ["a list, not the pin object"]


def test_the_pin_of_a_branch_install_is_the_nested_one(machine, tmp_path, monkeypatch):
    """APP_SUPPORT_DIR is branch_dir()-nested like home_dir(); a
    FUSED_RENDER_BRANCH install must rewrite its OWN pin and not the
    baseline's."""
    from fused_render import _branch

    legacy, new, _ = machine
    legacy.mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "feat/pin")
    monkeypatch.setattr(_branch, "_CACHED_REF", None)
    baseline = _pin_json(tmp_path)
    nested = _pin_json(tmp_path, _branch.branch_ref())
    assert nested != baseline
    storage.write_json(baseline, {"path": str(legacy / "a.html")})
    storage.write_json(nested, {"path": str(legacy / "b.html")})

    wm.run()

    assert storage.read_json(nested)["path"] == str(new / "b.html")
    assert storage.read_json(baseline)["path"] == str(legacy / "a.html")


# ------------------------------------------------------------- the entry points

def _source(module):
    """The module's source with comment lines dropped — these are ORDER
    assertions, and a comment that merely names a call is not a call."""
    return "\n".join(line for line in open(module, encoding="utf-8").read().splitlines()
                      if not line.lstrip().startswith("#"))


def _func_body(src, name):
    start = src.index(f"def {name}(")
    rest = src[start:]
    end = rest.find("\ndef ", 1)  # main() is the last one: -1 -> to end of file
    return rest if end < 0 else rest[:end]


def test_the_cli_runs_before_onboarding():
    """Ordering is load-bearing: ensure_fused_dir CREATES the destination, and a
    destination that exists is what the migration refuses to move into."""
    src = _source("fused_render/cli.py")
    assert "workspace_migration.run()" in src
    assert src.index("workspace_migration.run()") < src.index("ensure_fused_dir()")


def test_the_app_migrates_before_anything_can_read_the_state_it_rewrites():
    """The migration is a STARTUP PRECONDITION, not a server concern: it
    rewrites on-disk state, so every component that reads that state must come
    after it. The menu-bar pin is the one that reads early — PinController's
    constructor caches the pinned (workspace-absolute) path, and the boot timer
    builds it before the server thread exists — so a migration inside
    _start_server_thread left the popover on the pre-move path for the whole
    upgrade session.

    Source order, not behaviour: reaching main() means starting the AppKit run
    loop, which a test cannot do. It still catches the real failure mode, which
    is entirely "A read before B wrote"."""
    src = _source("fused_render/app.py")
    body = _func_body(src, "main")
    migrate = body.index("workspace_migration.run()")
    assert migrate < body.index("PinController("), "the pin would cache a stale path"
    assert migrate < body.index("target=_bootstrap_server"), "the server reads state too"
    # ...and still strictly before onboarding, which creates the destination the
    # migration refuses to move into. Expressed as reachability rather than text
    # order: the only ensure_fused_dir() call sits in _start_server_thread, which
    # only _bootstrap_server reaches, on the thread main starts after migrating.
    assert src.count("ensure_fused_dir()") == 1
    assert "ensure_fused_dir()" in _func_body(src, "_start_server_thread")
    assert src.count("_start_server_thread(port)") == 1
    assert "_start_server_thread(port)" in body[body.index("def _bootstrap_server("):]


def test_create_app_never_migrates():
    """Importing/creating the server in a test must not touch real user dirs —
    the same property meta_migration has."""
    src = open("fused_render/server/app.py", encoding="utf-8").read()
    assert "workspace_migration" not in src


def test_a_stray_file_at_the_legacy_path_changes_nothing(machine):
    legacy, new, home = machine
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("not a workspace", encoding="utf-8")
    recents = os.path.join(str(home), "recents.json")
    url = "/explorer/view" + str(legacy / "a.html")
    storage.write_json(recents, {"collapsed": False, "entries": [{"url": url}]})

    wm.run()

    assert legacy.read_text(encoding="utf-8") == "not a workspace"
    assert storage.read_json(recents)["entries"][0]["url"] == url


# ---------------------------------------------------- path shapes (no real fs)
# These drive the pure helpers directly with Windows-shaped and
# backslash-bearing POSIX input, so they assert the cross-platform behaviour on
# any host — the same discipline storage._sidecar_subpath (ntpath) and
# _view_url_codec are written for.

def test_a_windows_view_url_is_remapped_despite_the_backslashed_source():
    """The url decodes to a FORWARD-slashed drive path while the legacy dir
    comes back from os.path.abspath backslashed; comparing them literally
    matched nothing, so a Windows user kept every bookmark pointing at the
    folder that just moved."""
    src = "C:\\Users\\v\\Documents\\Fused"
    dst = "C:\\Users\\v\\Fused"

    assert (wm._remap_url("/explorer/view/C%3A/Users/v/Documents/Fused/x/index.html",
                          src, dst)
            == "/explorer/view/C%3A/Users/v/Fused/x/index.html")
    # The workspace root itself, and a path outside it.
    assert (wm._remap_url("/explorer/view/C%3A/Users/v/Documents/Fused", src, dst)
            == "/explorer/view/C%3A/Users/v/Fused")
    assert wm._remap_url("/explorer/view/C%3A/Users/v/Elsewhere/x", src, dst) is None


def test_a_windows_bookmark_sentinel_query_is_remapped():
    from urllib.parse import quote

    src = "C:\\Users\\v\\Documents\\Fused"
    dst = "C:\\Users\\v\\Fused"
    url = ("/explorer/view/_bookmark?file="
           + quote("C:/Users/v/Documents/Fused/saved.bookmark", safe="") + "&x=1")

    assert wm._remap_url(url, src, dst) == (
        "/explorer/view/_bookmark?file="
        + quote("C:/Users/v/Fused/saved.bookmark", safe="") + "&x=1")


def test_a_posix_backslash_in_a_filename_round_trips():
    """On POSIX a backslash is a legal filename character (storage and the
    frontend codec both normalize it ONLY for drive paths). Splitting on it
    turned one bookmark into a path that does not exist."""
    src = "/Users/x/Documents/Fused"
    dst = "/Users/x/Fused"

    assert (wm._remap_url("/explorer/view/Users/x/Documents/Fused/weird%5Cname.html",
                          src, dst)
            == "/explorer/view/Users/x/Fused/weird%5Cname.html")


def test_a_posix_sibling_is_not_matched_through_a_backslash():
    src = "/Users/x/Documents/Fused"
    dst = "/Users/x/Fused"

    assert wm._remap("/Users/x/Documents/Fused\\evil", src, dst) is None


# ------------------------------------------ the workspace's own sidecar (#3)

def test_moves_the_sidecar_of_the_workspace_directory_itself(machine):
    """A directory is bookmarkable (bookmarks._fs_path_from_url takes "a file
    OR a directory listing"), so the workspace dir has its own sidecar — and
    it is a SIBLING file of the subtree, `<...>/Documents/Fused.json`, which
    the subtree rename walks straight past."""
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    old_file = _sidecar_dir(home, legacy) + ".json"
    os.makedirs(os.path.dirname(old_file), exist_ok=True)
    with open(old_file, "w", encoding="utf-8") as f:
        json.dump({"bookmarkHistory": [{"at": "2026-01-01"}]}, f)

    wm.run()

    new_file = _sidecar_dir(home, new) + ".json"
    assert not os.path.exists(old_file)
    with open(new_file, encoding="utf-8") as f:
        assert json.load(f)["bookmarkHistory"][0]["at"] == "2026-01-01"


def test_an_existing_new_workspace_sidecar_file_is_never_clobbered(machine):
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    old_file = _sidecar_dir(home, legacy) + ".json"
    new_file = _sidecar_dir(home, new) + ".json"
    for p, payload in ((old_file, {"old": 1}), (new_file, {"new": 1})):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    wm.run()

    with open(old_file, encoding="utf-8") as f:
        assert json.load(f) == {"old": 1}
    with open(new_file, encoding="utf-8") as f:
        assert json.load(f) == {"new": 1}


# --------------------------------------------- untrusted values in state JSON

def test_a_corrupt_value_does_not_abort_the_rewrite(machine, monkeypatch):
    """Every path/url read out of these files is whatever JSON happens to hold.
    A non-string one must be SKIPPED, not raise: run() swallows exceptions, so
    a raise here would silently abandon the migration part-way and leave the
    user's state half-rewritten. This pins the isinstance guards in _remap /
    _remap_url — the reason their parameters are typed `object`."""
    legacy, new, home = machine
    legacy.mkdir(parents=True)
    from fused_render import community

    installs = home / "community" / "installs.json"
    monkeypatch.setattr(community, "INSTALLS_JSON", str(installs))
    storage.write_json(str(installs), {"installs": {
        "bad": {"path": None},
        "worse": {"path": 17},
        "good": {"path": str(legacy / "app")},
    }})
    bookmarks = os.path.join(str(home), "bookmarks.json")
    storage.write_json(bookmarks, [
        {"id": "1", "name": "bad", "url": None},
        {"id": "2", "name": "worse", "url": {"nested": 1}},
        {"id": "3", "name": "good", "url": "/explorer/view" + str(legacy / "a.html")},
    ])
    recents = os.path.join(str(home), "recents.json")
    storage.write_json(recents, {"entries": [
        {"url": 42},
        {"url": "/explorer/view" + str(legacy / "b.html")},
    ]})
    store = os.path.join(str(home), "scheduled_messages.json")
    storage.write_json(store, {"entries": [
        {"id": "a", "target": ["not", "a", "path"]},
        {"id": "b", "target": str(legacy / "proj")},
    ]})

    wm.run()

    # The neighbours of every corrupt value are still rewritten...
    assert storage.read_json(str(installs))["installs"]["good"]["path"] \
        == str(new / "app")
    assert storage.read_json(bookmarks)[2]["url"] \
        == "/explorer/view" + str(new / "a.html")
    assert storage.read_json(recents)["entries"][1]["url"] \
        == "/explorer/view" + str(new / "b.html")
    assert storage.read_json(store)["entries"][1]["target"] == str(new / "proj")
    # ...and the corrupt ones are left exactly as they were.
    assert storage.read_json(str(installs))["installs"]["bad"]["path"] is None
    assert storage.read_json(bookmarks)[0]["url"] is None
    assert storage.read_json(recents)["entries"][0]["url"] == 42
