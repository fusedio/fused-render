"""Tests for first-run onboarding (fused_render/shell/seed.py, D81): the
~/Documents/Fused workspace, its seeded examples, starter bookmarks, and the
first-launch landing URL.

FUSED_RENDER_DIR (the Fused dir) and FUSED_RENDER_HOME (~/.fused-render, holding
bookmarks.json) are both redirected to tmp dirs so no test touches a real dir.
"""

import json
from urllib.parse import unquote

from fused_render.shell.seed import ensure_fused_dir, ensure_fused_dir_and_landing


def _setup(tmp_path, monkeypatch):
    fdir = tmp_path / "Documents" / "Fused"
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return fdir, home


def _bookmarks(home):
    return json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))


# Every project folder the packaged seed ships (dot-metadata like .DS_Store on
# a dev machine is skipped by the seeder and must never land).
SEED_DIRS = ["how_it_works", "showcase", "sine", "tutorial"]


def test_seeds_examples_into_empty_dir(tmp_path, monkeypatch):
    fdir, _ = _setup(tmp_path, monkeypatch)
    returned = ensure_fused_dir()

    assert returned == str(fdir)
    # The packaged seed files land, each inside its own subfolder — nothing
    # loose at the workspace root.
    assert (fdir / "sine" / "sine.html").is_file()
    assert (fdir / "sine" / "sine.py").is_file()
    assert (fdir / "how_it_works" / "demo.py").is_file()
    assert (fdir / "how_it_works" / "explainer.html").is_file()
    assert (fdir / "showcase" / "index.html").is_file()
    assert (fdir / "tutorial" / "index.html").is_file()
    assert (fdir / "tutorial" / "hello.py").is_file()
    # Nothing spilled to the root: only the example subfolders exist.
    assert sorted(p.name for p in fdir.iterdir()) == SEED_DIRS


def test_non_empty_dir_is_left_untouched(tmp_path, monkeypatch):
    fdir, _ = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "my_work.html").write_text("mine", encoding="utf-8")

    ensure_fused_dir()

    # Existing content preserved; no examples copied in over a user's own dir.
    assert (fdir / "my_work.html").read_text(encoding="utf-8") == "mine"
    assert not (fdir / "sine").exists()
    assert not (fdir / "showcase").exists()


def test_dir_with_only_ds_store_still_seeds(tmp_path, monkeypatch):
    # macOS drops .DS_Store into ~/Documents/Fused as soon as Finder looks at
    # it; hidden metadata must not count as user content blocking the seed.
    fdir, home = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / ".DS_Store").write_bytes(b"\x00")

    ensure_fused_dir()

    assert (fdir / "showcase" / "index.html").is_file()
    assert (fdir / "how_it_works" / "explainer.html").is_file()
    # The hidden file survives — seeding never deletes anything.
    assert (fdir / ".DS_Store").read_bytes() == b"\x00"
    # Bookmarks ride along with the fresh seed as usual.
    assert (home / "bookmarks.json").is_file()


def test_bookmarks_created_when_absent_with_view_urls(tmp_path, monkeypatch):
    fdir, home = _setup(tmp_path, monkeypatch)
    ensure_fused_dir()

    marks = _bookmarks(home)
    assert [m["name"] for m in marks] == [
        "Tutorial",
        "Showcase",
        "Sine demo",
        "How it works",
    ]
    # Tutorial/Showcase/How-it-works are plain /view/ + per-segment-encoded
    # absolute paths; the Sine demo is a two-pane _panel split (page | code).
    assert marks[0]["url"] == "/view" + _encoded(str(fdir / "tutorial" / "index.html"))
    assert marks[1]["url"] == "/view" + _encoded(str(fdir / "showcase" / "index.html"))
    sine = str(fdir / "sine" / "sine.html")
    assert marks[2]["url"] == f"/view/_panel?_layout=({sine},{sine}?_mode=code)"
    assert marks[3]["url"] == "/view" + _encoded(str(fdir / "how_it_works" / "explainer.html"))
    # UUIDv4 ids + a numeric created_at, matching the store's shape.
    for m in marks:
        assert len(m["id"]) == 36 and m["id"].count("-") == 4
        assert isinstance(m["created_at"], int)


def _encoded(abs_path: str) -> str:
    # Mirror seed._view_url without importing it: leading slash kept as the
    # /view join, each segment percent-encoded (round-trip check below covers
    # the encoding rule itself).
    from urllib.parse import quote

    return "/" + "/".join(quote(s, safe="!*'()") for s in abs_path.lstrip("/").split("/") if s)


def test_bookmark_urls_encode_special_segments(tmp_path, monkeypatch):
    # A dir with a space proves the plain /view/ bookmark segments are
    # URL-encoded (not left literal) and decode back to the real fs path.
    fdir = tmp_path / "My Fused Dir"
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    ensure_fused_dir()

    url = _bookmarks(home)[0]["url"]  # "Tutorial" — a plain /view/ URL
    assert "My%20Fused%20Dir" in url  # space encoded, not literal
    assert " " not in url
    # Decoding the /view/ path yields the real absolute file path.
    decoded = "/" + "/".join(unquote(s) for s in url[len("/view/") :].split("/"))
    assert decoded == str(fdir / "tutorial" / "index.html")


def test_existing_bookmarks_never_overwritten(tmp_path, monkeypatch):
    fdir, home = _setup(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    existing = [{"id": "keep", "name": "mine", "url": "/view/x", "created_at": 1}]
    (home / "bookmarks.json").write_text(json.dumps(existing), encoding="utf-8")

    ensure_fused_dir()

    # Examples still seeded, but the pre-existing bookmarks file is untouched.
    assert (fdir / "showcase" / "index.html").is_file()
    assert _bookmarks(home) == existing


def test_bookmarks_not_seeded_without_examples(tmp_path, monkeypatch):
    # A non-empty dir with no example pages: examples aren't present, so starter
    # bookmarks must not be written onto the user's unrelated workspace.
    fdir, home = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "notes.txt").write_text("hi", encoding="utf-8")

    ensure_fused_dir()

    assert not (home / "bookmarks.json").exists()


def test_partial_seed_leftover_is_cleaned_and_reseeded(tmp_path, monkeypatch):
    # An interrupted first run can strand a hidden ".<name>.partial" temp dir and
    # leave the real examples missing. The next start must clear the leftover and
    # complete seeding (the partial must not wedge seeding off forever).
    fdir, home = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    partial = fdir / ".sine.partial"
    partial.mkdir()
    (partial / "sine.html").write_text("half-copied", encoding="utf-8")

    ensure_fused_dir()

    # Leftover gone; both examples fully seeded; nothing else at the root.
    assert not partial.exists()
    assert (fdir / "sine" / "sine.html").is_file()
    assert (fdir / "showcase" / "index.html").is_file()
    assert sorted(p.name for p in fdir.iterdir()) == SEED_DIRS
    # Bookmarks ride along with the completed seed.
    assert (home / "bookmarks.json").is_file()


def test_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    fdir, home = _setup(tmp_path, monkeypatch)
    ensure_fused_dir()
    first = _bookmarks(home)

    # User edits a seeded example; a second startup must not re-seed or reset it.
    (fdir / "sine" / "sine.html").write_text("edited", encoding="utf-8")
    ensure_fused_dir()

    assert (fdir / "sine" / "sine.html").read_text(encoding="utf-8") == "edited"
    assert _bookmarks(home) == first


def test_first_launch_landing_is_showcase(tmp_path, monkeypatch):
    # The one run that seeds the examples also reports the showcase /view/ URL
    # so the entry points open the browser there on a brand-new install.
    fdir, _ = _setup(tmp_path, monkeypatch)
    returned, landing = ensure_fused_dir_and_landing()

    assert returned == str(fdir)
    assert landing == "/view" + _encoded(str(fdir / "showcase" / "index.html"))


def test_no_landing_on_subsequent_runs(tmp_path, monkeypatch):
    # Only the first (seeding) run lands on showcase; every later launch opens
    # the root URL as before — existing installs see no behavior change.
    _setup(tmp_path, monkeypatch)
    ensure_fused_dir()

    _, landing = ensure_fused_dir_and_landing()
    assert landing is None


def test_bookmarks_skip_missing_targets_on_legacy_workspace(tmp_path, monkeypatch):
    # A legacy workspace (older seed set, bookmarks.json deleted): bookmark
    # seeding re-runs but must only bookmark pages that actually exist —
    # never a dangling bookmark onto a file that isn't there.
    fdir, home = _setup(tmp_path, monkeypatch)
    (fdir / "tutorial").mkdir(parents=True)
    (fdir / "tutorial" / "index.html").write_text("old seed", encoding="utf-8")
    # No showcase/ — the older seed never shipped it.

    ensure_fused_dir()

    marks = _bookmarks(home)
    assert [m["name"] for m in marks] == ["Tutorial"]


def test_no_bookmarks_when_no_targets_exist(tmp_path, monkeypatch):
    # No bookmark target present in a non-empty dir: nothing to point at,
    # so no bookmarks.json is written at all.
    fdir, home = _setup(tmp_path, monkeypatch)
    (fdir / "my_stuff").mkdir(parents=True)
    (fdir / "my_stuff" / "notes.html").write_text("user content", encoding="utf-8")

    ensure_fused_dir()

    assert not (home / "bookmarks.json").exists()


def test_no_landing_when_dir_has_user_content(tmp_path, monkeypatch):
    # A non-empty dir never seeds, so it never redirects the first tab either.
    fdir, _ = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "notes.txt").write_text("hi", encoding="utf-8")

    _, landing = ensure_fused_dir_and_landing()
    assert landing is None
