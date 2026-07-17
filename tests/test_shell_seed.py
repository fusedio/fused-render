"""Tests for first-run onboarding (fused_render/shell/seed.py, D81): the
~/Documents/Fused workspace, its seeded examples, and starter bookmarks.

FUSED_RENDER_DIR (the Fused dir) and FUSED_RENDER_HOME (~/.fused-render, holding
bookmarks.json) are both redirected to tmp dirs so no test touches a real dir.
"""

import json
import re
from urllib.parse import unquote

from fused_render.shell.seed import _sine_panel_url, ensure_fused_dir


def _setup(tmp_path, monkeypatch):
    fdir = tmp_path / "Documents" / "Fused"
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return fdir, home


def _bookmarks(home):
    return json.loads((home / "bookmarks.json").read_text(encoding="utf-8"))


def test_seeds_examples_into_empty_dir(tmp_path, monkeypatch):
    fdir, _ = _setup(tmp_path, monkeypatch)
    returned = ensure_fused_dir()

    assert returned == str(fdir)
    # All four packaged seed files land, each inside its own subfolder — nothing
    # loose at the workspace root.
    assert (fdir / "sine" / "sine.html").is_file()
    assert (fdir / "sine" / "sine.py").is_file()
    assert (fdir / "how_it_works" / "demo.py").is_file()
    assert (fdir / "how_it_works" / "explainer.html").is_file()
    # Nothing spilled to the root: only the two example subfolders exist.
    assert sorted(p.name for p in fdir.iterdir()) == ["how_it_works", "sine"]


def test_non_empty_dir_is_left_untouched(tmp_path, monkeypatch):
    fdir, _ = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "my_work.html").write_text("mine", encoding="utf-8")

    ensure_fused_dir()

    # Existing content preserved; no examples copied in over a user's own dir.
    assert (fdir / "my_work.html").read_text(encoding="utf-8") == "mine"
    assert not (fdir / "sine").exists()
    assert not (fdir / "how_it_works").exists()


def test_dir_with_only_ds_store_still_seeds(tmp_path, monkeypatch):
    # macOS drops .DS_Store into ~/Documents/Fused as soon as Finder looks at
    # it; hidden metadata must not count as user content blocking the seed.
    fdir, home = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / ".DS_Store").write_bytes(b"\x00")

    ensure_fused_dir()

    assert (fdir / "sine" / "sine.html").is_file()
    assert (fdir / "how_it_works" / "explainer.html").is_file()
    # The hidden file survives — seeding never deletes anything.
    assert (fdir / ".DS_Store").read_bytes() == b"\x00"
    # Bookmarks ride along with the fresh seed as usual.
    assert (home / "bookmarks.json").is_file()


def test_bookmarks_created_when_absent_with_view_urls(tmp_path, monkeypatch):
    fdir, home = _setup(tmp_path, monkeypatch)
    ensure_fused_dir()

    marks = _bookmarks(home)
    assert [m["name"] for m in marks] == ["Sine demo", "How it works"]
    # Sine demo opens a two-pane panel split (render | code) — parses back under
    # the layout codec to the SAME sine page in both panes, right in code mode.
    left, right = _parse_panel(marks[0]["url"])
    assert left == (str(fdir / "sine" / "sine.html"), "")
    assert right == (str(fdir / "sine" / "sine.html"), "?_mode=code")
    # How it works stays a plain /view/ + per-segment-encoded absolute path.
    assert marks[1]["url"] == "/view" + _encoded(str(fdir / "how_it_works" / "explainer.html"))
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


def _dec_seg(s: str) -> str:
    # layout-codec.ts decSeg: reverse only the structural escapes in one pass.
    return re.sub(r"%(25|2C|3B|28|29|3F)", lambda m: chr(int(m.group(1), 16)), s)


def _split_top(s: str, sep: str) -> list:
    # layout-codec.ts splitDepthAware: split on sep only at paren depth 0.
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _parse_panel(url: str) -> list:
    # Faithful mini-port of splitShellSearch + parseLayout for a flat row split:
    # balanced-paren extract the _layout span, one decodeURIComponent pass, then
    # depth-aware split on ';' (rows) and ',' (columns), un-escaping each leaf.
    assert url.startswith("/view/_panel?_layout=(") and url.endswith(")")
    inner = unquote(url[len("/view/_panel?_layout=(") : -1])
    rows = _split_top(inner, ";")
    assert len(rows) == 1  # single row, panes side by side
    leaves = []
    for cell in _split_top(rows[0], ","):
        q = cell.find("?")
        if q == -1:
            leaves.append((_dec_seg(cell), ""))
        else:
            leaves.append((_dec_seg(cell[:q]), "?" + _dec_seg(cell[q + 1 :])))
    return leaves


def test_sine_panel_url_exact_split_encoding():
    # Exact expected string for a fake home: the panel codec keeps `/ ? & =`
    # literal (it is NOT the /view/ per-segment encoding) and emits render|code.
    p = "/fake/Documents/Fused/sine/sine.html"
    assert _sine_panel_url(p) == (
        "/view/_panel?_layout=("
        "/fake/Documents/Fused/sine/sine.html,"
        "/fake/Documents/Fused/sine/sine.html?_mode=code)"
    )
    # A space in the path is escaped by urlSafeLayout (%20), not left literal,
    # and round-trips back through the codec to the original path.
    spaced = "/fake/My Dir/sine/sine.html"
    url = _sine_panel_url(spaced)
    assert " " not in url and "My%20Dir" in url
    left, right = _parse_panel(url)
    assert left == (spaced, "") and right == (spaced, "?_mode=code")


def test_bookmark_urls_encode_special_segments(tmp_path, monkeypatch):
    # A dir with a space proves the plain /view/ bookmark segments are
    # URL-encoded (not left literal) and decode back to the real fs path.
    fdir = tmp_path / "My Fused Dir"
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    ensure_fused_dir()

    url = _bookmarks(home)[1]["url"]  # "How it works" — a plain /view/ URL
    assert "My%20Fused%20Dir" in url  # space encoded, not literal
    assert " " not in url
    # Decoding the /view/ path yields the real absolute file path.
    decoded = "/" + "/".join(unquote(s) for s in url[len("/view/") :].split("/"))
    assert decoded == str(fdir / "how_it_works" / "explainer.html")


def test_existing_bookmarks_never_overwritten(tmp_path, monkeypatch):
    fdir, home = _setup(tmp_path, monkeypatch)
    home.mkdir(parents=True)
    existing = [{"id": "keep", "name": "mine", "url": "/view/x", "created_at": 1}]
    (home / "bookmarks.json").write_text(json.dumps(existing), encoding="utf-8")

    ensure_fused_dir()

    # Examples still seeded, but the pre-existing bookmarks file is untouched.
    assert (fdir / "sine" / "sine.html").is_file()
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
    assert (fdir / "how_it_works" / "explainer.html").is_file()
    assert sorted(p.name for p in fdir.iterdir()) == ["how_it_works", "sine"]
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
