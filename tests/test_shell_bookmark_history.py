"""Tests for POST /api/bookmarks/history (fused_render/shell/bookmarks.py) —
the bookmarkHistory sidecar mirror.

The sidecar lives next to the TARGET file (`<file>.json`), not under
FUSED_RENDER_HOME, so these drive the route functions directly with a tmp_path
target. Calling the handlers as plain functions (rather than via TestClient)
keeps the module importable in venvs where starlette's TestClient is missing
its httpx dependency.
"""
import json

from fused_render.shell import bookmarks


def _post(payload, x_fused="1"):
    return bookmarks.post_bookmark_history(payload=payload, x_fused=x_fused)


def _url_for(path) -> str:
    # An absolute fs path already begins with "/", so "/view" + path yields
    # "/view/private/tmp/.../sample.html".
    return "/view" + str(path)


def test_create_writes_sidecar(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "name": "sample.html",
                  "url": _url_for(f) + "?a=1", "created_at": 1720000000000})
    assert resp == {"recorded": True}

    data = json.loads((tmp_path / "sample.html.json").read_text())
    assert data["claudeSessions"] == []
    hist = data["bookmarkHistory"]
    assert len(hist) == 1
    e = hist[0]
    assert e["id"] == "bk-1"
    # Portable: the entry stores only the query string, never the absolute
    # /view/<abs-path> url — the target file is the sidecar's owner.
    assert e["search"] == "a=1"
    assert "url" not in e
    assert e["recorded_at"] == e["updated_at"]


def test_bare_url_stores_empty_search(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "n", "url": _url_for(f)})  # no query
    e = json.loads((tmp_path / "sample.html.json").read_text())["bookmarkHistory"][0]
    assert e["search"] == ""
    assert "url" not in e


def test_preserves_existing_sessions(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1, "cwd": "/x"}]
    (tmp_path / "sample.html.json").write_text(json.dumps({"claudeSessions": sess}))

    _post({"id": "bk-1", "name": "n", "url": _url_for(f)})
    data = json.loads((tmp_path / "sample.html.json").read_text())
    assert data["claudeSessions"] == sess
    assert len(data["bookmarkHistory"]) == 1


def test_update_by_id(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "n", "url": _url_for(f) + "?a=1"})
    first = json.loads((tmp_path / "sample.html.json").read_text())["bookmarkHistory"][0]

    _post({"id": "bk-1", "name": "n", "url": _url_for(f) + "?a=2"})
    hist = json.loads((tmp_path / "sample.html.json").read_text())["bookmarkHistory"]
    assert len(hist) == 1
    e = hist[0]
    assert e["search"] == "a=2"
    assert e["recorded_at"] == first["recorded_at"]  # unchanged
    assert e["updated_at"] >= first["updated_at"]


def test_update_only_upsert(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    # No prior create for this id -> appended.
    _post({"id": "never-created", "url": _url_for(f)})
    hist = json.loads((tmp_path / "sample.html.json").read_text())["bookmarkHistory"]
    assert [e["id"] for e in hist] == ["never-created"]


def test_none_field_does_not_clobber(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "keep-me", "url": _url_for(f)})
    _post({"id": "bk-1", "name": None, "url": _url_for(f)})  # update carries no name
    e = json.loads((tmp_path / "sample.html.json").read_text())["bookmarkHistory"][0]
    assert e["name"] == "keep-me"


def test_directory_target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    resp = _post({"id": "bk-1", "name": "proj", "url": _url_for(d)})
    assert resp == {"recorded": True}
    assert (tmp_path / "proj.json").exists()  # sibling sidecar


def test_sentinel_no_op(tmp_path):
    resp = _post({"id": "bk-1", "name": "layout", "url": "/view/_panel?_layout=abc"})
    assert resp == {"recorded": False}


def test_nested_file_named_like_sentinel_records(tmp_path):
    # Only the exact top-level `/view/_panel`|`/view/_tab` is a sentinel; a real
    # file that merely happens to be named `_panel` deeper in the tree is a
    # normal target and must get a sidecar.
    f = tmp_path / "_panel"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "url": _url_for(f)})
    assert resp == {"recorded": True}
    assert (tmp_path / "_panel.json").exists()


def test_nonexistent_path_no_op(tmp_path):
    resp = _post({"id": "bk-1", "url": _url_for(tmp_path / "nope.html")})
    assert resp == {"recorded": False}


def test_windows_drive_letter_path(monkeypatch):
    # A Windows bookmark url carries a drive-letter path (rootedFsPath keeps
    # `C:/...` as-is). It must resolve to `C:/...`, NOT `/C:/...` — otherwise the
    # extra leading slash misses on disk and history is silently skipped.
    monkeypatch.setattr("os.path.exists", lambda p: True)
    assert bookmarks._fs_path_from_url("/view/C:/Users/me/sample.html") == "C:/Users/me/sample.html"


def test_windows_bare_drive_gets_trailing_slash(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: True)
    assert bookmarks._fs_path_from_url("/view/C:") == "C:/"


def test_posix_path_gets_leading_slash(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: True)
    assert bookmarks._fs_path_from_url("/view/Users/me/x.html") == "/Users/me/x.html"


def test_missing_fused_header_forbidden(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "url": _url_for(f)}, x_fused=None)
    assert resp.status_code == 403
    assert not (tmp_path / "sample.html.json").exists()


def test_bad_payload_rejected(tmp_path):
    assert _post({"url": "/view/x"}).status_code == 400        # no id
    assert _post({"id": "bk-1"}).status_code == 400            # no url


def test_embed_prefix_handled(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "url": "/embed" + str(f)})
    assert resp == {"recorded": True}
    assert (tmp_path / "sample.html.json").exists()
