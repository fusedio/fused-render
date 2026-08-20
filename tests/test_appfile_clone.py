"""Cloning a `.fused` app file into the workspace (D397): the way out of a
read-only artifact.

`clone_target` is the read-only probe the preview header's button reads to pick
its label; `clone_app_file` does the copy. The rule under both is that the
DESTINATION FOLDER EXISTING is what "already cloned" means — there is no
records file — so most of what is worth pinning here is about that presence
test and about the copy landing writable.
"""

import json
import os
import zipfile

import pytest

from fused_render import appfile

MARKER = '<meta charset="utf-8" />\n<meta name="fused-app" />'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Per-test shell home and workspace. Both matter: the extract cache lives
    under the home, and a clone writes into the WORKSPACE — a shared one would
    drop `local/demo` into the developer's real ~/Fused while the suite runs."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(appfile, "appfiles_root", lambda: str(tmp_path / "cache"))


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


def export(tmp_path, name="demo", out_name=None):
    app = make_app(tmp_path, name)
    out = tmp_path / (out_name or f"{name}.fused")
    appfile.export_app_file(str(app), str(out))
    return out


def rewrite_manifest_name(fused_path, new_name):
    """Re-zip the file with `manifest.json`'s `name` replaced — how a hostile
    or merely odd app file is produced without hand-building a whole zip."""
    with zipfile.ZipFile(fused_path) as zf:
        items = [(i, zf.read(i.filename)) for i in zf.infolist()]
    with zipfile.ZipFile(fused_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, raw in items:
            if info.filename == "manifest.json":
                man = json.loads(raw)
                man["name"] = new_name
                raw = json.dumps(man).encode()
            zf.writestr(info.filename, raw)


# -- the probe ----------------------------------------------------------------


def test_target_is_local_slug_and_reports_not_cloned_first(tmp_path):
    out = export(tmp_path)
    t = appfile.clone_target(str(out))
    assert t["name"] == "demo"
    assert t["slug"] == "demo"
    assert t["path"] == os.path.join(appfile.clone_dir(), "demo").replace(os.sep, "/")
    assert t["cloned"] is False
    # A probe must not create the tag dir, extract, or otherwise leave a trace:
    # it is read on every preview of a .fused.
    assert not os.path.isdir(appfile.clone_dir())
    assert not os.path.isdir(str(tmp_path / "cache"))


def test_an_existing_folder_at_the_destination_reads_as_cloned(tmp_path):
    """The owner's rule, and the named cost of it: presence is the whole test,
    so a folder the user put there themselves reads as this app's clone."""
    out = export(tmp_path)
    unrelated = os.path.join(appfile.clone_dir(), "demo")
    os.makedirs(unrelated)
    (tmp_path / "workspace" / "local" / "demo" / "mine.html").write_text("mine")
    t = appfile.clone_target(str(out))
    assert t["cloned"] is True
    assert t["path"] == unrelated.replace(os.sep, "/")


def test_a_missing_file_is_an_appfile_error(tmp_path):
    with pytest.raises(appfile.AppFileError):
        appfile.clone_target(str(tmp_path / "nope.fused"))


# -- the copy -----------------------------------------------------------------


def test_clone_lands_the_payload_writable_in_the_workspace(tmp_path):
    out = export(tmp_path)
    r = appfile.clone_app_file(str(out))
    assert r["cloned"] is False
    dest = r["path"]
    assert os.path.isdir(dest)
    # Every payload member, at the same relative paths.
    assert sorted(
        os.path.relpath(os.path.join(dp, f), dest).replace(os.sep, "/")
        for dp, _dn, fs in os.walk(dest)
        for f in fs
    ) == ["assets/logo.svg", "data.py", "index.html"]
    assert "fused-app" in open(os.path.join(dest, "index.html")).read()
    # THE point of the feature: the extract is 0o444 and copytree carries mode
    # across, so a clone that skipped the lift would be an uneditable
    # "development copy". Checked by writing, not by stat'ing a bit — the
    # question is whether an editor can save.
    for rel in ("index.html", "data.py", "assets/logo.svg"):
        p = os.path.join(dest, rel)
        assert os.access(p, os.W_OK), rel
        with open(p, "a") as fh:
            fh.write("\n<!-- edited -->\n")


def test_plain_files_no_git_init(tmp_path):
    """Owner's call, and the difference from the showcase Clone: a .fused is a
    snapshot with no upstream to diff against, so nothing inits a repo."""
    r = appfile.clone_app_file(str(export(tmp_path)))
    assert not os.path.exists(os.path.join(r["path"], ".git"))
    assert not os.path.exists(os.path.join(r["path"], ".gitignore"))


def test_a_second_clone_is_a_no_op_that_reports_the_existing_copy(tmp_path):
    out = export(tmp_path)
    first = appfile.clone_app_file(str(out))
    edited = os.path.join(first["path"], "index.html")
    with open(edited, "w") as fh:
        fh.write("MY EDIT")

    second = appfile.clone_app_file(str(out))
    assert second["cloned"] is True
    assert second["path"] == first["path"]
    # The user's edit survives: a re-clone never overwrites and never makes a
    # second folder.
    assert open(edited).read() == "MY EDIT"
    assert sorted(os.listdir(appfile.clone_dir())) == ["demo"]


def test_the_probe_flips_after_a_clone(tmp_path):
    """What the header button actually reads: "Clone" before, "Go to local
    version" after, with no state store between them."""
    out = export(tmp_path)
    assert appfile.clone_target(str(out))["cloned"] is False
    appfile.clone_app_file(str(out))
    assert appfile.clone_target(str(out))["cloned"] is True


def test_clone_extracts_a_file_never_opened(tmp_path):
    """No open is required first — the clone rides open_app_file, so the one
    hardened extractor runs on the way through rather than a second unzip."""
    out = export(tmp_path)
    assert not os.path.isdir(str(tmp_path / "cache"))
    r = appfile.clone_app_file(str(out))
    assert os.path.isfile(os.path.join(r["path"], "index.html"))


def test_staging_leaves_nothing_behind(tmp_path):
    appfile.clone_app_file(str(export(tmp_path)))
    assert sorted(os.listdir(appfile.clone_dir())) == ["demo"]


def test_the_clone_is_an_ordinary_workspace_app(tmp_path):
    """The payoff, and the reason the destination is a workspace TAG DIR rather
    than anywhere writable: the clone needs no registration to become a real
    app. `open_app_file` refuses a payload whose entry lost the fused-app
    marker, so the copy carries it, and the ordinary workspace walk is what
    finds it — tagged `local`, like anything else in that dir."""
    from fused_render import app_listing
    from fused_render.shell.seed import fused_dir

    r = appfile.clone_app_file(str(export(tmp_path)))
    rows = {a["name"]: a for a in app_listing.workspace_apps(fused_dir())}
    assert "demo" in rows, sorted(rows)
    assert rows["demo"]["tag"] == "local"
    assert os.path.realpath(rows["demo"]["path"]) == os.path.realpath(r["path"])


# -- the manifest name is attacker-controlled ---------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../evil",
        "../../evil",
        "/etc/evil",
        "C:\\evil",
        "..",
        ".",
        "foo/bar",
        "\\\\server\\share",
    ],
)
def test_a_hostile_manifest_name_cannot_escape_the_tag_dir(tmp_path, hostile):
    """`name` is zip data. It reaches the filesystem only through `_slug`,
    whose character class admits no separator, dot or drive letter — so these
    collapse to ordinary segments instead of climbing out of local/."""
    out = export(tmp_path)
    rewrite_manifest_name(out, hostile)
    t = appfile.clone_target(str(out))
    local = os.path.realpath(appfile.clone_dir())
    assert os.sep not in t["slug"] and "/" not in t["slug"]
    r = appfile.clone_app_file(str(out))
    landed = os.path.realpath(r["path"])
    assert landed != local
    assert os.path.dirname(landed) == local
    assert os.path.isfile(os.path.join(landed, "index.html"))


def test_an_empty_manifest_name_falls_back_to_the_file_stem(tmp_path):
    """Not a shared "app" literal: two unnamed app files would then collide on
    one local/app folder and the second would read as the first's clone."""
    a = export(tmp_path, name="one", out_name="alpha.fused")
    b = export(tmp_path, name="two", out_name="beta.fused")
    rewrite_manifest_name(a, "")
    rewrite_manifest_name(b, "   ")
    assert appfile.clone_target(str(a))["slug"] == "alpha"
    assert appfile.clone_target(str(b))["slug"] == "beta"
    appfile.clone_app_file(str(a))
    assert appfile.clone_target(str(b))["cloned"] is False


def test_a_unicode_name_still_yields_a_usable_folder(tmp_path):
    out = export(tmp_path, out_name="ünïcødé.fused")
    rewrite_manifest_name(out, "Ünïcødé Åpp")
    r = appfile.clone_app_file(str(out))
    assert r["slug"]
    assert os.path.isdir(r["path"])
    assert os.path.dirname(os.path.realpath(r["path"])) == os.path.realpath(
        appfile.clone_dir()
    )


# -- the routes ---------------------------------------------------------------


def test_routes_probe_unguarded_and_clone_guarded(tmp_path):
    from fastapi.testclient import TestClient

    from fused_render.server.app import create_app

    client = TestClient(create_app(start_dir=str(tmp_path)))
    out = export(tmp_path)

    # The GET is a read-only probe — unguarded, like /api/appfile/preview.
    r = client.get("/api/appfile/clone", params={"path": str(out)})
    assert r.status_code == 200
    assert r.json()["cloned"] is False
    dest = r.json()["path"]

    # The POST writes, so it carries the D3 guard the open route does.
    r = client.post("/api/appfile/clone", json={"file": str(out)})
    assert r.status_code == 403

    r = client.post(
        "/api/appfile/clone", json={"file": str(out)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 200
    assert r.json() == {**r.json(), "cloned": False, "path": dest}
    assert os.path.isfile(os.path.join(dest, "index.html"))

    # The probe now answers what the button reads as "Go to local version",
    # and a second POST is the same no-op the function-level test pins.
    assert client.get("/api/appfile/clone", params={"path": str(out)}).json()["cloned"]
    r = client.post(
        "/api/appfile/clone", json={"file": str(out)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 200 and r.json()["cloned"] is True

    # Bad input on both halves answers 400 with the reason the toast shows.
    assert client.get("/api/appfile/clone", params={"path": "relative.fused"}).status_code == 400
    r = client.get("/api/appfile/clone", params={"path": str(tmp_path / "nope.fused")})
    assert r.status_code == 400 and "error" in r.json()

    junk = tmp_path / "junk.fused"
    junk.write_bytes(b"not a zip")
    r = client.post(
        "/api/appfile/clone", json={"file": str(junk)}, headers={"X-Fused": "1"}
    )
    assert r.status_code == 400 and "error" in r.json()
