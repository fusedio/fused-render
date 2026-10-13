"""The .fused single-file app export/install (SPEC §43): export walks the
whole app folder into an opaque container, install unpacks it hardened into
the workspace's `app/` (AF-14, tests/test_appfile_install.py has the install
semantics), and the shared view-URL codec routes a double-clicked .fused to
its own embed URL, where the fusedapp template shows the install page."""

import io
import json
import os
import zipfile

import pytest

from fused_render import appfile
from fused_render._view_url_codec import canonical_fs_path, embed_url_path, view_url_path

MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Per-test shell home and workspace, the same way test_registered_apps.py
    does it: conftest allocates ONE FUSED_RENDER_HOME per process, and an
    install writes into the WORKSPACE."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))


def make_app(tmp_path, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "index.html").write_text(
        f"<html><head>{MARKER}<title>Demo</title></head><body>hi</body></html>"
    )
    (d / "data.py").write_text("def main():\n    return {'ok': True}\n")
    (d / "assets").mkdir()
    (d / "assets" / "logo.svg").write_text("<svg/>")
    return d


def test_export_then_install_round_trip(tmp_path):
    app = make_app(tmp_path)
    out = tmp_path / "demo.fused"
    manifest = appfile.export_app_file(str(app), str(out))
    assert manifest["fused_app_file"] == 2
    assert manifest["entry"] == "index.html"
    assert manifest["name"] == "demo"
    assert manifest["exported_at"].endswith("Z") and len(manifest["exported_at"]) == 20

    # v2 is an opaque container, not a zip: no PK magic anywhere a scanner
    # sniffs, and the member names live in the deflated index only.
    assert out.read_bytes()[:8] == b"FUSEDAPP"
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(out)
    names = {f["path"] for f in appfile.read_manifest(str(out))["files"]}
    assert names == {"index.html", "data.py", "assets/logo.svg"}

    result = appfile.install_app_file(str(out))
    assert result["action"] == "installed"
    assert os.path.isfile(result["entry"])
    dest = str(tmp_path / "workspace" / "app" / "demo")
    assert result["entry"] == os.path.join(dest, "index.html").replace(os.sep, "/")
    # The copy is the user's own: writable, never 0444.
    assert os.access(result["entry"], os.W_OK)
    assert os.access(os.path.join(dest, "data.py"), os.W_OK)

    # Same bytes again: the same version is installed, nothing to update.
    assert appfile.install_target(str(out))["state"] == "same"


def test_export_skips_machinery_and_hidden(tmp_path):
    app = make_app(tmp_path)
    (app / ".git").mkdir()
    (app / ".git" / "HEAD").write_text("ref")
    (app / ".env").write_text("SECRET=1")
    (app / "CLAUDE.md").write_text("authoring contract")
    (app / "node_modules").mkdir()
    (app / "node_modules" / "x.js").write_text("x")
    (app / "__pycache__").mkdir()
    (app / "__pycache__" / "d.pyc").write_bytes(b"\x00")
    out = tmp_path / "demo.fused"
    appfile.export_app_file(str(app), str(out))
    names = {f["path"] for f in appfile.read_manifest(str(out))["files"]}
    assert not any(
        ".git" in n or ".env" in n or "CLAUDE" in n or "node_modules" in n or "pycache" in n
        for n in names
    )


def test_export_fails_open_on_a_git_dir_git_rejects(tmp_path):
    """A `.git` whose HEAD git will not read (a stub, a half-copied checkout)
    is not a repository, and must not become the ignore oracle's root: every
    check-ignore against it answers 128, which read as "git stopped
    answering" and refused the whole export (main was red on this from
    #886). The marker fails open — the folder exports as a plain folder
    would, the stub itself dropped by the hidden-name rule — while a REAL
    ancestor .gitignore keeps being honored, because the parent's oracle
    stays in force below the stub."""
    root = tmp_path / "site"
    root.mkdir()
    (root / ".gitignore").write_text("*.log\n")
    app = make_app(root)
    (app / ".git").mkdir()
    (app / ".git" / "HEAD").write_text("ref")  # not a ref git accepts
    (app / "run.log").write_text("noise")
    (app / "keep.txt").write_text("data")
    out = tmp_path / "demo.fused"
    appfile.export_app_file(str(app), str(out))
    names = {f["path"] for f in appfile.read_manifest(str(out))["files"]}
    assert "keep.txt" in names
    assert "run.log" not in names
    assert not any(".git" in n for n in names)


def test_export_refuses_non_app_folder(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "index.html").write_text("<html><head></head></html>")  # no marker
    with pytest.raises(appfile.AppFileError, match="not a fused app"):
        appfile.export_app_file(str(d), str(tmp_path / "x.fused"))


def test_export_allows_fused_ai_pages(tmp_path):
    # Unlike the hosted exporter (RH-11), fused.ai.text() ships: an opened .fused
    # runs in the recipient's full local runtime, where /api/ai exists (D388).
    # A recipient without a claude CLI gets the graceful ai_unavailable state.
    app = make_app(tmp_path)
    (app / "chat.html").write_text("<html><script>fused.ai.text({prompt: 'hi'})</script></html>")
    out = tmp_path / "x.fused"
    appfile.export_app_file(str(app), str(out))
    assert "chat.html" in {f["path"] for f in appfile.read_manifest(str(out))["files"]}


def test_export_refuses_existing_out_path(tmp_path):
    app = make_app(tmp_path)
    out = tmp_path / "demo.fused"
    out.write_text("occupied")
    with pytest.raises(appfile.AppFileError, match="overwrite"):
        appfile.export_app_file(str(app), str(out))
    assert out.read_text() == "occupied"


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_install_rejects_zip_slip(tmp_path):
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "evil", "entry": "index.html"}
                ),
                "files/index.html": f"<html><head>{MARKER}</head></html>",
                "../escape.txt": "outside",
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="escape|rejected"):
        appfile.install_app_file(str(bad))
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "workspace" / "app" / "evil").exists()


def test_install_rejects_manifest_traversal_entry(tmp_path):
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "e", "entry": "../../x.html"}
                )
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="invalid entry"):
        appfile.read_manifest(str(bad))


def test_install_rejects_markerless_entry(tmp_path):
    bad = tmp_path / "nomarker.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "n", "entry": "index.html"}
                ),
                "files/index.html": "<html><head></head></html>",
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="fused-app"):
        appfile.install_app_file(str(bad))
    assert not (tmp_path / "workspace" / "app" / "n").exists()


def test_install_rejects_backslash_entry(tmp_path):
    # `..\..\x.html` passes a `/`-split ".." check but joins as traversal on
    # Windows; backslashes are rejected outright (the exporter never writes them).
    bad = tmp_path / "evil.fused"
    bad.write_bytes(
        _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {"fused_app_file": 1, "root": "files", "name": "e",
                     "entry": "..\\..\\x.html"}
                )
            }
        )
    )
    with pytest.raises(appfile.AppFileError, match="invalid entry"):
        appfile.read_manifest(str(bad))


def test_manifest_read_is_size_capped(tmp_path):
    # read_manifest runs before the capped extractor; a crafted zip declaring
    # a huge manifest must not be decompressed unbounded into memory.
    bomb = tmp_path / "bomb.fused"
    bomb.write_bytes(
        _zip_bytes({"manifest.json": " " * (appfile._MANIFEST_CAP_BYTES + 100)})
    )
    with pytest.raises(appfile.AppFileError, match="too large"):
        appfile.read_manifest(str(bomb))


def test_install_rejects_non_fused_zip(tmp_path):
    plain = tmp_path / "plain.fused"
    plain.write_bytes(_zip_bytes({"readme.txt": "hello"}))
    with pytest.raises(appfile.AppFileError, match="manifest"):
        appfile.read_manifest(str(plain))


def test_codec_routes_os_opened_fused_to_embed():
    # A Finder/Explorer double-click lands on the file's own EMBED url — the
    # fusedapp template renders the install page chrome-free (D390, AF-14).
    # In-explorer clicks use the ordinary view prefix and need no special case.
    assert view_url_path("/tmp/My App.fused") == "/explorer/embed/tmp/My%20App.fused"
    # Extension check is case-insensitive.
    assert view_url_path("/a/B.FUSED") == "/explorer/embed/a/B.FUSED"
    # And the embed path the install answers for the entry is the same codec.
    assert embed_url_path("/x/demo/index.html") == "/explorer/embed/x/demo/index.html"


def test_routes_export_and_install(tmp_path):
    from fastapi.testclient import TestClient

    from fused_render.server.app import create_app

    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)

    r = client.get("/api/appfile/export", params={"path": str(app_dir)})
    assert r.status_code == 200
    assert "demo.fused" in r.headers["content-disposition"]
    fused_path = tmp_path / "demo.fused"
    fused_path.write_bytes(r.content)

    # D390 / AF-14: no user-facing open route — the fusedapp template reads
    # the probe and POSTs the X-Fused-guarded install.
    r = client.post("/api/appfile/install", json={"file": str(fused_path)})
    assert r.status_code == 403
    r = client.post(
        "/api/appfile/install", json={"file": str(fused_path)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 200
    assert r.json()["entry_embed"].startswith("/explorer/embed/")
    assert appfile.install_target(str(fused_path))["state"] == "same"

    # A bad export target answers 400 with the reason.
    r = client.get("/api/appfile/export", params={"path": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "error" in r.json()

    # A junk .fused answers the error the template renders as its fail state.
    junk = tmp_path / "junk.fused"
    junk.write_bytes(b"not a zip")
    r = client.get("/api/appfile/install", params={"path": str(junk)})
    assert r.status_code == 400
    assert "error" in r.json()

    # The .fused extension resolves to the fusedapp template on stat, which is
    # what makes /explorer/view|embed/<path>.fused render the app at all.
    r = client.get("/api/fs/stat", params={"path": str(fused_path)}, headers={"X-Fused": "1"})
    assert r.status_code == 200
    modes = [t["mode"] for t in r.json().get("templates") or []]
    assert "fusedapp" in modes



def test_export_to_disk_writes_the_real_file_and_notes_the_mutation(tmp_path, monkeypatch):
    """The server-side export route (workstream B) writes the `.fused` to the
    resolved destination directory itself — not a temp dir behind a browser
    download — and reports the real absolute path, so the file is
    immediately locatable and immediately queued for reindexing."""
    from fastapi.testclient import TestClient

    from fused_render import appfile
    from fused_render.server.routers import appfile as appfile_router
    from fused_render.server.app import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    monkeypatch.setattr(
        appfile_router, "_export_destination_dir", lambda: str(dest_dir)
    )
    noted: list[str] = []
    monkeypatch.setattr(
        appfile_router, "note_index_mutation", lambda *paths: noted.extend(p for p in paths if p)
    )

    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)

    r = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir)},
        headers={"X-Fused": "1"},
    )
    assert r.status_code == 200
    body = r.json()
    from fused_render import appfile_container

    out_path = body["path"]
    assert os.path.isfile(out_path)
    # `out_path` is `canonical_fs_path(...)` (forward-slashed, per
    # `_view_url_codec.py`'s documented convention: "the form a path has
    # everywhere above the OS"); `dest_dir` is a raw `pathlib.Path`, which is
    # backslashed on Windows, so it needs the same canonicalization before
    # the comparison is separator-correct on every platform.
    assert os.path.dirname(out_path) == canonical_fs_path(str(dest_dir))
    assert appfile_container.is_container(out_path)
    # The FILE that was written, not the folder it landed in —
    # `note_index_mutation` scans the PARENT of whatever it is handed
    # (`index_touch._folder_of`), so passing the folder itself would queue a
    # scan of the folder's own parent (the user's home directory) instead of
    # Downloads.
    assert noted == [out_path]

    # A second export of the same app never clobbers the first — it picks a
    # sibling filename instead.
    r2 = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir)},
        headers={"X-Fused": "1"},
    )
    assert r2.status_code == 200
    out_path2 = r2.json()["path"]
    assert out_path2 != out_path
    assert os.path.isfile(out_path)
    assert os.path.isfile(out_path2)

    # Missing the X-Fused guard is refused, like every other mutating route.
    r3 = client.post("/api/appfile/export/save", data={"path": str(app_dir)})
    assert r3.status_code == 403


def test_export_to_disk_uses_the_callers_display_name(tmp_path, monkeypatch):
    """AppPage.tsx and EntryActionsMenu.tsx compute a version-suffixed name
    (`${name}-${versionLabel}`) specifically so a snapshot export never
    collides with a live export of the same app in Downloads. That name must
    become the written file's actual name, not just something the caller
    discards after computing it."""
    from fastapi.testclient import TestClient

    from fused_render import appfile, appfile_container
    from fused_render.server.routers import appfile as appfile_router
    from fused_render.server.app import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    monkeypatch.setattr(
        appfile_router, "_export_destination_dir", lambda: str(dest_dir)
    )
    monkeypatch.setattr(appfile_router, "note_index_mutation", lambda *paths: None)

    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)

    r = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir), "name": "myapp-v7"},
        headers={"X-Fused": "1"},
    )
    assert r.status_code == 200
    out_path = r.json()["path"]
    assert os.path.basename(out_path) == "myapp-v7.fused"
    assert appfile_container.is_container(out_path)

    # A second export under the SAME sha/name never collides into the plain
    # app-folder basename either — it gets a free sibling of the requested
    # name, not of the fallback.
    r2 = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir), "name": "myapp-v7"},
        headers={"X-Fused": "1"},
    )
    out_path2 = r2.json()["path"]
    assert os.path.basename(out_path2) == "myapp-v7 (2).fused"

    # No name at all still falls back to the app folder's own basename.
    r3 = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir)},
        headers={"X-Fused": "1"},
    )
    out_path3 = r3.json()["path"]
    assert os.path.basename(out_path3) == f"{os.path.basename(str(app_dir))}.fused"


def test_export_to_disk_job_row_is_silent(tmp_path, monkeypatch):
    """The route's own success job must not ALSO pop a card: the client
    already raises its own two-action notification ("Reveal folder" /
    "Open file") on the same export, so a non-silent job row here would show
    both at once. `popupJobs` (frontend/src/platform/lib/jobs.ts) already
    drops a `done` job whose stored tier is `silent`, so the job just needs
    to declare that tier."""
    from fastapi.testclient import TestClient

    from fused_render import appfile, jobs
    from fused_render.server.routers import appfile as appfile_router
    from fused_render.server.app import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    monkeypatch.setattr(
        appfile_router, "_export_destination_dir", lambda: str(dest_dir)
    )
    monkeypatch.setattr(appfile_router, "note_index_mutation", lambda *paths: None)

    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)

    r = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir)},
        headers={"X-Fused": "1"},
    )
    assert r.status_code == 200
    out_path = r.json()["path"]

    rows = [j for j in jobs.list_jobs() if j.get("page") == out_path]
    assert len(rows) == 1
    assert rows[0]["state"] == "done"
    assert rows[0]["tier"] == "silent"


def test_export_to_disk_bakes_a_caller_captured_preview(tmp_path, monkeypatch):
    """The disk-write export takes the same optional capture the browser
    download route does — a folder with no authored preview.png ships with
    whatever the caller photographed, not a blank thumbnail."""
    from fastapi.testclient import TestClient

    from fused_render import appfile
    from fused_render.server.app import create_app

    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    monkeypatch.setattr(
        "fused_render.server.routers.appfile._export_destination_dir",
        lambda: str(dest_dir),
    )

    client = TestClient(create_app(start_dir=str(tmp_path)))
    app_dir = make_app(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 16

    r = client.post(
        "/api/appfile/export/save",
        data={"path": str(app_dir)},
        files={"preview": ("preview.png", png, "image/png")},
        headers={"X-Fused": "1"},
    )
    assert r.status_code == 200
    assert appfile.read_preview(r.json()["path"]) == png
