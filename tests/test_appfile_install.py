"""Installing a `.fused` app file into the workspace (SPEC §43 AF-14): a
`.fused` is an installer, the one working copy of the app lives at
`<workspace>/app/<slug>`, and the install stamp (`.fused/appfile.json`)
tells a later file apart as an update, a downgrade, the same version, or an
update over a copy the user edited since (diverged).

`install_target` is the read-only probe the install page reads;
`install_app_file` writes. `.fused/` inside the copy is never touched.
"""

import json
import os
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

from fused_render import app_fused_dir, appfile
from fused_render.server import create_app

MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'
FUSED = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Per-test shell home and workspace: an install writes into the
    WORKSPACE — a shared one would drop `app/demo` into the developer's real
    ~/Fused while the suite runs."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))


def make_app(tmp_path, name="demo", app_id=None, extra=None):
    """A fresh source folder each time: a second export of the same name
    (v2 of an app) starts from scratch, so files v1 had and v2 lists in
    `extra` no more are really gone from the new file."""
    d = tmp_path / "src" / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    tag = f'<meta name="fused-app-id" content="{app_id}" />' if app_id else ""
    (d / "index.html").write_text(
        f"<html><head>{MARKER}{tag}<title>Demo</title></head><body>hi</body></html>"
    )
    (d / "data.py").write_text("def main():\n    return {'ok': True}\n")
    (d / "assets").mkdir()
    (d / "assets" / "logo.svg").write_text("<svg/>")
    (d / "README.md").write_text("# demo\n\nA **demo** app.\n")
    for rel, content in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def export(tmp_path, name="demo", out_name=None, app_id=None, extra=None, stamp=None):
    """An exported file; `stamp` overrides `exported_at` so two exports of
    one app can be ordered deterministically."""
    app = make_app(tmp_path, name, app_id=app_id, extra=extra)
    out = tmp_path / (out_name or f"{name}.fused")
    if out.exists():
        out.unlink()
    appfile.export_app_file(str(app), str(out))
    if stamp is not None:
        _rewrite_index(out, exported_at=stamp)
    return out


def _rewrite_index(fused_path, **fields):
    """Re-write the container with index fields replaced — how a file from
    another day (or a hostile one) is produced without hand-building it."""
    from fused_render import appfile_container as container

    index = appfile.read_manifest(str(fused_path))
    members = [
        (f["path"], container.read_member(str(fused_path), index, f["path"], f["size"]))
        for f in index["files"]
    ]
    meta = {k: v for k, v in index.items()
            if k not in ("files", "fused_app_file", "_data_start")}
    meta.update(fields)
    os.unlink(fused_path)
    container.write(str(fused_path), meta, members)


def app_root(tmp_path):
    return tmp_path / "workspace" / "app"


# -- the probe ----------------------------------------------------------------


def test_target_is_app_slug_and_reports_new_first(tmp_path):
    fused = export(tmp_path)
    t = appfile.install_target(str(fused))
    assert t["slug"] == "demo"
    assert t["path"] == str(app_root(tmp_path) / "demo").replace(os.sep, "/")
    assert t["installed"] is False
    assert t["state"] == "new"
    assert t["needs_force"] is False
    assert t["readme"].startswith("# demo")
    assert {f["path"] for f in t["files"]} == {"index.html", "data.py", "assets/logo.svg", "README.md"}
    assert t["entry"] == "index.html"


def test_a_missing_file_is_an_appfile_error(tmp_path):
    with pytest.raises(appfile.AppFileError, match="no such file"):
        appfile.install_target(str(tmp_path / "nope.fused"))


# -- install ------------------------------------------------------------------


def test_install_lands_an_editable_copy_with_stamp_and_repo(tmp_path):
    fused = export(tmp_path, app_id="demo-abc12345")
    r = appfile.install_app_file(str(fused))
    dest = app_root(tmp_path) / "demo"
    assert r["action"] == "installed"
    assert r["entry"] == str(dest / "index.html").replace(os.sep, "/")
    assert (dest / "index.html").is_file() and (dest / "assets" / "logo.svg").is_file()
    assert os.access(dest / "index.html", os.W_OK)  # never 0444
    # The stamp records the file's date and every member's hash.
    stamp = appfile.read_stamp(str(dest))
    assert stamp["exported_at"] == appfile.read_manifest(str(fused))["exported_at"]
    assert stamp["app_id"] == "demo-abc12345"
    assert set(stamp["files"]) == {"index.html", "data.py", "assets/logo.svg", "README.md"}
    # `.fused/` is scaffolded (data, cache, meta) beside the stamp.
    assert (dest / ".fused" / "data").is_dir()
    assert app_fused_dir.recorded_app_dir(str(dest)) == str(dest)
    # The copy heads its own repo, like a newly created app/ app.
    assert (dest / ".git").is_dir()
    # Staging left nothing behind.
    assert not [n for n in os.listdir(app_root(tmp_path)) if n.startswith(".install-")]


def test_the_probe_reads_same_after_install(tmp_path):
    fused = export(tmp_path)
    appfile.install_app_file(str(fused))
    t = appfile.install_target(str(fused))
    assert t["installed"] is True
    assert t["state"] == "same"
    assert t["needs_force"] is True
    assert t["entry_path"].endswith("/app/demo/index.html")


def test_install_finds_the_copy_by_id_wherever_it_sits(tmp_path):
    fused = export(tmp_path, app_id="demo-abc12345")
    appfile.install_app_file(str(fused))
    # The user renamed the copy: still this app, by its id.
    os.rename(app_root(tmp_path) / "demo", app_root(tmp_path) / "renamed")
    t = appfile.install_target(str(fused))
    assert t["found_by"] == "id"
    assert t["path"].endswith("/app/renamed")
    assert t["state"] == "same"


# -- update, divergence, downgrade -------------------------------------------


def test_a_newer_file_over_an_untouched_copy_is_a_plain_update(tmp_path):
    v1 = export(tmp_path, app_id="demo-abc12345", stamp="2026-01-01T00:00:00Z")
    appfile.install_app_file(str(v1))
    dest = app_root(tmp_path) / "demo"
    # The app saved state; the user never touched the files.
    (dest / ".fused" / "data" / "notes.json").write_text("{}")
    v2 = export(tmp_path, out_name="demo-v2.fused", app_id="demo-abc12345",
                extra={"new.py": "x = 1\n"}, stamp="2026-02-01T00:00:00Z")
    t = appfile.install_target(str(v2))
    assert t["state"] == "update" and t["needs_force"] is False and t["changed"] == []
    r = appfile.install_app_file(str(v2))
    assert r["action"] == "updated"
    assert (dest / "new.py").is_file()
    assert (dest / ".fused" / "data" / "notes.json").read_text() == "{}"  # never touched
    assert appfile.read_stamp(str(dest))["exported_at"] == "2026-02-01T00:00:00Z"


def test_a_file_the_author_deleted_is_pruned_only_when_unchanged(tmp_path):
    v1 = export(tmp_path, app_id="demo-abc12345",
                extra={"gone.py": "old\n", "kept.py": "old\n"}, stamp="2026-01-01T00:00:00Z")
    appfile.install_app_file(str(v1))
    dest = app_root(tmp_path) / "demo"
    (dest / "kept.py").write_text("my edit\n")  # the user's work
    (dest / "mine.txt").write_text("not from any export\n")
    # v2 drops both gone.py and kept.py.
    v2 = export(tmp_path, out_name="demo-v2.fused", app_id="demo-abc12345",
                stamp="2026-02-01T00:00:00Z")
    r = appfile.install_app_file(str(v2), force=True)
    assert r["pruned"] == ["gone.py"]
    assert not (dest / "gone.py").exists()
    assert (dest / "kept.py").read_text() == "my edit\n"   # the user's edit stays
    assert (dest / "mine.txt").is_file()                   # never stamped, never touched


def test_an_edited_copy_reads_as_diverged_and_needs_force(tmp_path):
    v1 = export(tmp_path, app_id="demo-abc12345", stamp="2026-01-01T00:00:00Z")
    appfile.install_app_file(str(v1))
    dest = app_root(tmp_path) / "demo"
    (dest / "data.py").write_text("def main():\n    return 'edited'\n")
    (dest / "assets" / "logo.svg").unlink()
    v2 = export(tmp_path, out_name="demo-v2.fused", app_id="demo-abc12345",
                stamp="2026-02-01T00:00:00Z")
    t = appfile.install_target(str(v2))
    assert t["state"] == "diverged"
    assert sorted(t["changed"]) == ["assets/logo.svg", "data.py"]
    with pytest.raises(appfile.AppFileError, match="confirmation"):
        appfile.install_app_file(str(v2))
    r = appfile.install_app_file(str(v2), force=True)
    assert r["action"] == "updated"
    assert "edited" not in (dest / "data.py").read_text()


def test_an_older_file_is_a_downgrade(tmp_path):
    v2 = export(tmp_path, app_id="demo-abc12345", stamp="2026-02-01T00:00:00Z")
    appfile.install_app_file(str(v2))
    v1 = export(tmp_path, out_name="demo-v1.fused", app_id="demo-abc12345",
                stamp="2026-01-01T00:00:00Z")
    t = appfile.install_target(str(v1))
    assert t["state"] == "downgrade" and t["needs_force"] is True


def test_a_copy_without_a_stamp_is_unknown(tmp_path):
    fused = export(tmp_path)
    dest = app_root(tmp_path) / "demo"
    dest.mkdir(parents=True)
    (dest / "index.html").write_text(f"<html><head>{MARKER}</head></html>")
    t = appfile.install_target(str(fused))
    assert t["installed"] is True and t["state"] == "unknown"
    with pytest.raises(appfile.AppFileError, match="confirmation"):
        appfile.install_app_file(str(fused))
    appfile.install_app_file(str(fused), force=True)
    assert appfile.read_stamp(str(dest)) is not None


def test_a_slug_held_by_a_different_app_is_a_clash(tmp_path):
    other = export(tmp_path, app_id="other-11111111")
    appfile.install_app_file(str(other))
    mine = export(tmp_path, out_name="mine.fused", app_id="mine-22222222")
    t = appfile.install_target(str(mine))
    assert t["state"] == "clash"
    with pytest.raises(appfile.AppFileError, match="different app"):
        appfile.install_app_file(str(mine), force=True)
    assert "mine" not in (app_root(tmp_path) / "demo" / "index.html").read_text()


def test_an_id_less_file_over_an_identified_app_is_a_clash_too(tmp_path):
    """A file with no id cannot prove it is the same app as the folder that
    declares one, so it never reads as that app's update."""
    other = export(tmp_path, app_id="other-11111111")
    appfile.install_app_file(str(other))
    old = export(tmp_path, out_name="old.fused")  # same slug, no id
    assert appfile.install_target(str(old))["state"] == "clash"
    with pytest.raises(appfile.AppFileError, match="different app"):
        appfile.install_app_file(str(old), force=True)


# -- the run-in-place era's state ----------------------------------------------


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the run-in-place era's symlink needs Developer Mode on the Windows runner")
def test_install_adopts_the_shared_state_dir_and_retires_extracts(tmp_path):
    import pathlib

    fused = export(tmp_path, app_id="demo-abc12345")
    # Built from the same helpers the install reads, so a branch-nested home
    # (`storage.home_dir()` under `branches/<ref>/`) lands them where the
    # adoption looks.
    shared = pathlib.Path(appfile.shared_dot_fused_dir("demo-abc12345"))
    (shared / "data").mkdir(parents=True)
    (shared / "data" / "saved.json").write_text('{"k": 1}')
    (shared / "meta.json").write_text(json.dumps(
        {"version": 1, "app_dir": "/old/extract/demo-deadbeef", "created_at": "x"}))
    extract = pathlib.Path(appfile.appfiles_root()) / "demo-0123456789abcdef"
    extract.mkdir(parents=True)
    (extract / "index.html").write_text(
        f'<html><head>{MARKER}<meta name="fused-app-id" content="demo-abc12345" /></head></html>')
    os.symlink(str(shared), str(extract / ".fused"), target_is_directory=True)

    r = appfile.install_app_file(str(fused))
    dest = app_root(tmp_path) / "demo"
    assert r["adopted_state"] == str(shared)
    assert (dest / ".fused" / "data" / "saved.json").read_text() == '{"k": 1}'
    assert not shared.exists()
    # The witness is settled on the copy, the origin kept as evidence.
    meta = app_fused_dir.read_meta(str(dest))
    assert meta["app_dir"] == str(dest)
    assert meta["migrations"][-1]["installed"] is True
    # The extract that pointed at the shared dir is gone, the link unlinked
    # rather than followed.
    assert not extract.exists()


# -- hostile names ------------------------------------------------------------


@pytest.mark.parametrize("hostile", ["../evil", "/etc/evil", "C:\\evil", "..", "a/../../b"])
def test_a_hostile_manifest_name_cannot_escape_the_tag_dir(tmp_path, hostile):
    fused = export(tmp_path)
    _rewrite_index(fused, name=hostile)
    t = appfile.install_target(str(fused))
    dest = os.path.normpath(t["path"])
    assert os.path.dirname(dest) == str(app_root(tmp_path))
    assert os.sep not in os.path.basename(dest) and "/" not in os.path.basename(dest)


def test_an_empty_manifest_name_falls_back_to_the_file_stem(tmp_path):
    fused = export(tmp_path, out_name="My Sine Wave.fused")
    _rewrite_index(fused, name="")
    t = appfile.install_target(str(fused))
    assert t["slug"] == "my-sine-wave"
    assert t["name"] == "My Sine Wave"


# -- routes -------------------------------------------------------------------


def test_routes_probe_unguarded_install_guarded_and_409_until_forced(tmp_path):
    fused = export(tmp_path, app_id="demo-abc12345", stamp="2026-01-01T00:00:00Z")
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.get("/api/appfile/install", params={"path": str(fused)})
    assert r.status_code == 200 and r.json()["state"] == "new"
    assert client.get("/api/appfile/install", params={"path": "relative.fused"}).status_code == 400
    assert client.post("/api/appfile/install", json={"file": str(fused)}).status_code == 403
    r = client.post("/api/appfile/install", json={"file": str(fused)}, headers=FUSED)
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "installed"
    assert body["entry_view"].startswith("/explorer/view/")
    assert body["entry_embed"].startswith("/explorer/embed/")
    # Same version again: 409 with the fresh probe, 200 with force.
    r = client.post("/api/appfile/install", json={"file": str(fused)}, headers=FUSED)
    assert r.status_code == 409 and r.json()["target"]["state"] == "same"
    r = client.post("/api/appfile/install", json={"file": str(fused), "force": True}, headers=FUSED)
    assert r.status_code == 200 and r.json()["action"] == "updated"
    # The icon route: 404 for a file without one, 400 for a relative path.
    assert client.get("/api/appfile/icon", params={"path": str(fused)}).status_code == 404
    assert client.get("/api/appfile/icon", params={"path": "x.fused"}).status_code == 400
    # No run-in-place routes any more.
    assert client.post("/api/appfile/open", json={"file": str(fused)}, headers=FUSED).status_code in (404, 405)
    assert client.get("/api/appfile/clone", params={"path": str(fused)}).status_code in (404, 405)


def test_a_fused_is_not_a_hub_row(tmp_path):
    """An app file is an installer (AF-14): the hub lists the installed copy,
    never the file."""
    fused = export(tmp_path)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/apps").json()["apps"] == []
    client.post("/api/appfile/install", json={"file": str(fused)}, headers=FUSED)
    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["demo"]
    assert apps[0]["tag"] == "app"
