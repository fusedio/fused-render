"""Tests for POST /api/bookmarks/history (fused_render/shell/bookmarks.py) —
the bookmarkHistory sidecar mirror.

The sidecar now lives under home_dir()/sidecar/<mapped path>.json (D83-
reversal), never next to the TARGET file — see shell/storage.py's
sidecar_path. FUSED_RENDER_HOME is pinned to an isolated tmp dir for every
test here so a real sidecar under the developer's actual ~/.fused-render is
never touched. Calling the handlers as plain functions (rather than via
TestClient) keeps the module importable in venvs where starlette's TestClient
is missing its httpx dependency.
"""
import json
import os

import pytest

from fused_render.shell import bookmarks


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _post(payload, x_fused="1"):
    return bookmarks.post_bookmark_history(payload=payload, x_fused=x_fused)


def _url_for(path) -> str:
    # An absolute fs path already begins with "/", so "/view" + path yields
    # "/view/private/tmp/.../sample.html".
    return "/view" + str(path)


def _sidecar(path) -> dict:
    return json.loads(open(bookmarks.storage.sidecar_path(str(path)), encoding="utf-8").read())


def _sidecar_exists(path) -> bool:
    return os.path.exists(bookmarks.storage.sidecar_path(str(path)))


def test_create_writes_sidecar(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "name": "sample.html",
                  "url": _url_for(f) + "?a=1", "created_at": 1720000000000})
    assert resp == {"recorded": True}

    data = _sidecar(f)
    assert data["claudeSessions"] == []
    hist = data["bookmarkHistory"]
    assert len(hist) == 1
    e = hist[0]
    assert e["id"] == "bk-1"
    # Portable: the entry stores only the query string, never the absolute
    # /view/<abs-path> url — the target file's own path deterministically
    # derives its sidecar's location (storage.sidecar_path).
    assert e["search"] == "a=1"
    assert "url" not in e
    assert e["recorded_at"] == e["updated_at"]


def test_bare_url_stores_empty_search(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "n", "url": _url_for(f)})  # no query
    e = _sidecar(f)["bookmarkHistory"][0]
    assert e["search"] == ""
    assert "url" not in e


def test_preserves_existing_sessions(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1, "cwd": "/x"}]
    sidecar_path = bookmarks.storage.sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"claudeSessions": sess}, fh)

    _post({"id": "bk-1", "name": "n", "url": _url_for(f)})
    data = _sidecar(f)
    assert data["claudeSessions"] == sess
    assert len(data["bookmarkHistory"]) == 1


def test_update_by_id(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "n", "url": _url_for(f) + "?a=1"})
    first = _sidecar(f)["bookmarkHistory"][0]

    _post({"id": "bk-1", "name": "n", "url": _url_for(f) + "?a=2"})
    hist = _sidecar(f)["bookmarkHistory"]
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
    hist = _sidecar(f)["bookmarkHistory"]
    assert [e["id"] for e in hist] == ["never-created"]


def test_none_field_does_not_clobber(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    _post({"id": "bk-1", "name": "keep-me", "url": _url_for(f)})
    _post({"id": "bk-1", "name": None, "url": _url_for(f)})  # update carries no name
    e = _sidecar(f)["bookmarkHistory"][0]
    assert e["name"] == "keep-me"


def test_directory_target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    resp = _post({"id": "bk-1", "name": "proj", "url": _url_for(d)})
    assert resp == {"recorded": True}
    assert _sidecar_exists(d)  # sidecar under home_dir()/sidecar/, not a sibling


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
    assert _sidecar_exists(f)


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
    assert not _sidecar_exists(f)


def test_bad_payload_rejected(tmp_path):
    assert _post({"url": "/view/x"}).status_code == 400        # no id
    assert _post({"id": "bk-1"}).status_code == 400            # no url


def test_embed_prefix_handled(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    resp = _post({"id": "bk-1", "url": "/embed" + str(f)})
    assert resp == {"recorded": True}
    assert _sidecar_exists(f)


# --------------------------------------------------- read-only remote mounts
# D83-reversal: the sidecar now lives under home_dir()/sidecar/, never on the
# target's own mount, so a read-only remote mount no longer has any bearing on
# whether the bookmarkHistory mirror can be written — the old sidecar-write
# incident (CacheMode=full 403-looping a doomed PutObject) structurally can't
# happen anymore. This used to be a skip case; now it's a plain success case.

@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "cog.tif")
    with open(f, "w") as fh:
        fh.write("x")
    return f


def test_history_recorded_under_read_only_mount(ro_mount):
    resp = _post({"id": "bk-1", "name": "cog.tif",
                  "url": "/view" + ro_mount + "?stretch=2,1471"})
    assert resp == {"recorded": True}
    assert _sidecar_exists(ro_mount)
