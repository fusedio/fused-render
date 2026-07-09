"""Tests for the template-management API (fused_render/templates_api.py;
TEMPLATE_MGMT_SPEC §2): inventory, extended registry view, binding upsert/reset,
export zip, and the two-step import (stage -> commit) with its security guards.

FUSED_RENDER_HOME is redirected to a tmp home so staging lands there, and
server.USER_TEMPLATES_DIR / server.USER_REGISTRY are pointed under it (the same
module-constant seam test_templates.py / test_shell_prefs.py patch). Core
templates keep resolving from the conftest-staged .core-templates dir.
"""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from fused_render import server, templates_api
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    udir = home / "templates"
    udir.mkdir()
    monkeypatch.setattr(server, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server, "USER_REGISTRY", str(udir / "registry.json"))
    client = TestClient(create_app(start_dir=str(tmp_path)))

    class Ctx:
        def __init__(self):
            self.client = client
            self.home = home
            self.udir = udir

        def make_template(self, name, *, icon=False, extra=None):
            folder = udir / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "template.html").write_text("<html></html>", encoding="utf-8")
            if icon:
                (folder / "icon.svg").write_text("<svg/>", encoding="utf-8")
            for fname, content in (extra or {}).items():
                (folder / fname).write_text(content, encoding="utf-8")
            return folder

        def registry(self, mapping):
            (udir / "registry.json").write_text(json.dumps(mapping), encoding="utf-8")

        def read_registry(self):
            return json.loads((udir / "registry.json").read_text(encoding="utf-8"))

    return Ctx()


def _names(entry):
    return [t["name"] for t in entry["templates"]]


def _make_zip(entries):
    """entries: dict arcname -> bytes|str. Returns raw zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for arc, content in entries.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(arc, content)
    return buf.getvalue()


def _post_import(ctx, zip_bytes, headers=FUSED):
    return ctx.client.post(
        "/api/templates/import",
        files={"file": ("upload.zip", zip_bytes, "application/zip")},
        headers=headers,
    )


# ------------------------------------------------------------- inventory (2.1)


def test_inventory_sources_and_core_templates(ctx):
    body = ctx.client.get("/api/templates/inventory").json()
    assert [s["id"] for s in body["sources"]] == ["core", "user"]
    sources_by_id = {s["id"]: s for s in body["sources"]}
    assert sources_by_id["core"]["dir"] == os.path.abspath(server.TEMPLATES_DIR)
    assert sources_by_id["user"]["dir"] == os.path.abspath(server.USER_TEMPLATES_DIR)
    by_name = {t["name"]: t for t in body["templates"]}
    # Core templates are present, locked, and carry usedBy from the registry.
    table = by_name["table"]
    assert table["source"] == "core"
    assert table["editable"] is False
    assert table["shadowsCore"] is False
    assert ".parquet" in table["usedBy"]
    assert table["path"] == os.path.join(os.path.abspath(server.TEMPLATES_DIR), "table")
    # vendor/ and shared/ have no template.html -> never listed as templates.
    assert "vendor" not in by_name and "shared" not in by_name


def test_inventory_user_template_and_used_by(ctx):
    ctx.make_template("brandcard", icon=True)
    ctx.registry({".brand": ["brandcard"]})
    by_name = {t["name"]: t for t in ctx.client.get("/api/templates/inventory").json()["templates"]}
    bc = by_name["brandcard"]
    assert bc["source"] == "user"
    assert bc["editable"] is True
    assert bc["hasIcon"] is True
    assert bc["usedBy"] == [".brand"]
    assert bc["shadowsCore"] is False
    assert bc["path"] == os.path.join(os.path.abspath(server.USER_TEMPLATES_DIR), "brandcard")


def test_inventory_user_shadows_core_single_entry(ctx):
    ctx.make_template("code")  # same name as a core template -> shadow
    templates = ctx.client.get("/api/templates/inventory").json()["templates"]
    code_rows = [t for t in templates if t["name"] == "code"]
    assert len(code_rows) == 1  # one entry, not two
    assert code_rows[0]["source"] == "user"
    assert code_rows[0]["shadowsCore"] is True
    # shadowed -> the USER folder's path, not the hidden core one.
    assert code_rows[0]["path"] == os.path.join(os.path.abspath(server.USER_TEMPLATES_DIR), "code")


# -------------------------------------------------------- registry read (2.2)


def test_registry_rich_shape(ctx):
    body = ctx.client.get("/api/templates/registry").json()
    assert {s["id"] for s in body["sources"]} == {"core", "user"}
    sources_by_id = {s["id"]: s for s in body["sources"]}
    assert sources_by_id["core"]["dir"] == os.path.abspath(server.TEMPLATES_DIR)
    assert sources_by_id["user"]["dir"] == os.path.abspath(server.USER_TEMPLATES_DIR)
    assert body["builtin_registry"] == server.BUILTIN_REGISTRY
    assert body["user_registry"] == server.USER_REGISTRY
    by_key = {e["key"]: e for e in body["entries"]}
    csv = by_key[".csv"]
    assert csv["keyKind"] == "simple"
    assert csv["resolvedSource"] == "core"
    assert csv["overridesCore"] is False
    assert "userValue" not in csv  # omitted when no user key
    assert csv["templates"][0] == {
        "name": "csv",
        "source": "core",
        "exists": True,
        "hasIcon": True,
    }


def test_registry_broken_name_marked_not_exists(ctx):
    ctx.registry({".csv": ["no-such-template", "csv"]})
    by_key = {e["key"]: e for e in ctx.client.get("/api/templates/registry").json()["entries"]}
    templates = by_key[".csv"]["templates"]
    assert templates[0] == {"name": "no-such-template", "source": None, "exists": False, "hasIcon": False}
    assert templates[1]["name"] == "csv" and templates[1]["exists"] is True


# ----------------------------------------------------------- put binding (2.3)


def test_put_upsert_returns_recomputed_entry(ctx):
    ctx.make_template("mytable", icon=True)
    resp = ctx.client.put(
        "/api/templates/registry",
        json={"key": ".csv", "value": ["mytable", "csv", "code"]},
        headers=FUSED,
    )
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["key"] == ".csv"
    assert entry["resolvedSource"] == "user"
    assert entry["overridesCore"] is True
    assert _names(entry) == ["mytable", "csv", "code"]
    assert entry["userValue"] == ["mytable", "csv", "code"]
    # mytable resolves to the user source with an icon.
    mt = entry["templates"][0]
    assert mt["source"] == "user" and mt["hasIcon"] is True
    # Persisted to the user registry file.
    assert ctx.read_registry()[".csv"] == ["mytable", "csv", "code"]


def test_put_null_disables(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".png", "value": None}, headers=FUSED
    )
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["disabled"] is True
    assert entry["templates"] == []
    assert entry["userValue"] is None


def test_put_new_directory_key(ctx):
    ctx.make_template("bundle")
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".obt/", "value": ["bundle"]}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json()["keyKind"] == "directory"


def test_put_allows_sentinel_and_splice(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".html", "value": ["code", "..."]}, headers=FUSED
    )
    assert resp.status_code == 200
    # splice expands the builtin .html list after `code`
    assert _names(resp.json())[0] == "code"


def test_put_invalid_key_rejected(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".geo*.json", "value": ["csv"]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "invalid registry key" in resp.json()["error"]


def test_put_unknown_name_rejected(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".csv", "value": ["nope", "also-nope"]}, headers=FUSED
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert "unknown template name" in err and "nope" in err
    # Nothing was written on rejection.
    assert not (ctx.udir / "registry.json").exists()


def test_put_bad_value_type_rejected(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".csv", "value": "csv"}, headers=FUSED
    )
    assert resp.status_code == 400


def test_put_requires_fused_header(ctx):
    resp = ctx.client.put("/api/templates/registry", json={"key": ".csv", "value": ["csv"]})
    assert resp.status_code == 403
    assert not (ctx.udir / "registry.json").exists()


def test_put_case_collision_replaced(ctx):
    ctx.registry({".CSV": ["code"]})
    ctx.client.put(
        "/api/templates/registry", json={"key": ".csv", "value": ["csv"]}, headers=FUSED
    )
    reg = ctx.read_registry()
    assert ".CSV" not in reg  # the case-colliding key was dropped
    assert reg[".csv"] == ["csv"]


# --------------------------------------------------------- reset binding (2.4)


def test_reset_removes_user_override_reverts_to_core(ctx):
    ctx.registry({".csv": ["code"]})
    resp = ctx.client.post(
        "/api/templates/registry/reset", json={"key": ".csv"}, headers=FUSED
    )
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["resolvedSource"] == "core"
    assert entry["overridesCore"] is False
    assert _names(entry)[0] == "csv"  # the builtin default
    assert ".csv" not in ctx.read_registry()


def test_reset_user_only_key_reports_removed(ctx):
    ctx.make_template("brandcard")
    ctx.registry({".brand": ["brandcard"]})
    resp = ctx.client.post(
        "/api/templates/registry/reset", json={"key": ".brand"}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"key": ".brand", "removed": True}
    assert ".brand" not in ctx.read_registry()


def test_reset_absent_key_is_noop(ctx):
    resp = ctx.client.post(
        "/api/templates/registry/reset", json={"key": ".csv"}, headers=FUSED
    )
    # .csv is a builtin key with no user override -> reverts to core cleanly.
    assert resp.status_code == 200
    assert resp.json()["resolvedSource"] == "core"


def test_reset_requires_fused_header(ctx):
    resp = ctx.client.post("/api/templates/registry/reset", json={"key": ".csv"})
    assert resp.status_code == 403


# --------------------------------------------------------------- export (2.5)


def test_export_zip_contains_folders_only(ctx):
    ctx.make_template("alpha", icon=True, extra={"reader.py": "x=1"})
    ctx.make_template("beta")
    ctx.registry({".a": ["alpha"]})  # registry.json must NOT be in the zip
    resp = ctx.client.get("/api/templates/export", params={"names": "alpha,beta"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "fused-render-templates.zip" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
    assert names == {
        "alpha/template.html",
        "alpha/icon.svg",
        "alpha/reader.py",
        "beta/template.html",
    }
    assert not any("registry.json" in n for n in names)


def test_export_allows_core_template(ctx):
    # 'code' is a core template — core templates are exportable too (SPEC
    # §2.5 update): resolve via core dir since there's no user shadow.
    resp = ctx.client.get("/api/templates/export", params={"names": "code"})
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
    assert names == {"code/template.html", "code/icon.svg"}


def test_export_user_shadow_wins_over_core(ctx):
    # 'code' also exists as a core template; the user folder should win.
    ctx.make_template("code", extra={"marker.txt": "USER"})
    resp = ctx.client.get("/api/templates/export", params={"names": "code"})
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        marker = zf.read("code/marker.txt")
    assert names == {"code/template.html", "code/marker.txt"}
    assert marker == b"USER"


def test_export_rejects_unknown_name(ctx):
    resp = ctx.client.get("/api/templates/export", params={"names": "no-such-template"})
    assert resp.status_code == 400
    assert "no such template" in resp.json()["error"]


def test_export_rejects_traversal_name(ctx):
    resp = ctx.client.get("/api/templates/export", params={"names": "../secrets"})
    assert resp.status_code == 400


def test_export_requires_names(ctx):
    assert ctx.client.get("/api/templates/export").status_code == 400


# --------------------------------------------------- import: security (2.6)


def test_import_rejects_parent_escape(ctx):
    zb = _make_zip({"good/template.html": "<html>", "../evil.txt": "pwn"})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 400
    assert "rejected zip" in resp.json()["error"]
    # No staging dir left behind (rejected before extraction).
    staging = ctx.home / ".import-staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_import_rejects_absolute_path(ctx):
    zb = _make_zip({"/etc/evil": "pwn"})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 400


def test_import_rejects_symlink_entry(ctx):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("card/link")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    resp = _post_import(ctx, buf.getvalue())
    assert resp.status_code == 400
    assert "symlink" in resp.json()["error"]


def test_import_entry_count_guard(ctx, monkeypatch):
    monkeypatch.setattr(templates_api, "MAX_ENTRIES", 3)
    zb = _make_zip({f"f{i}.txt": "x" for i in range(4)})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 400
    assert "too many entries" in resp.json()["error"]


def test_import_per_entry_size_guard(ctx, monkeypatch):
    monkeypatch.setattr(templates_api, "MAX_ENTRY_UNCOMPRESSED", 10)
    zb = _make_zip({"card/template.html": "x" * 50})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]


def test_import_total_size_guard(ctx, monkeypatch):
    monkeypatch.setattr(templates_api, "MAX_TOTAL_UNCOMPRESSED", 10)
    zb = _make_zip({"a.txt": "x" * 8, "b.txt": "y" * 8})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 400
    assert "uncompressed size too large" in resp.json()["error"]


def test_import_requires_fused_header(ctx):
    zb = _make_zip({"card/template.html": "<html>"})
    resp = _post_import(ctx, zb, headers={})
    assert resp.status_code == 403


def test_import_bad_zip(ctx):
    resp = _post_import(ctx, b"not a zip at all")
    assert resp.status_code == 400
    assert "not a valid .zip" in resp.json()["error"]


# --------------------------------------------- import: stage manifest (2.6)


def test_import_stage_manifest(ctx):
    ctx.make_template("brandcard")  # pre-existing -> conflict
    zb = _make_zip(
        {
            "brandcard/template.html": "<html>",
            "brandcard/reader.py": "x=1",
            "fresh/template.html": "<html>",
            "notemplate/readme.txt": "hi",
            "loose.txt": "ignore me",
        }
    )
    resp = _post_import(ctx, zb)
    assert resp.status_code == 200
    body = resp.json()
    assert body["expiresInSec"] == templates_api.IMPORT_TTL_SEC
    items = {i["name"]: i for i in body["items"]}
    assert items["brandcard"]["valid"] is True
    assert items["brandcard"]["conflictsExisting"] is True
    assert items["brandcard"]["fileCount"] == 2
    assert items["fresh"]["valid"] is True
    assert items["fresh"]["conflictsExisting"] is False
    assert items["notemplate"]["valid"] is False
    assert items["notemplate"]["hasTemplateHtml"] is False
    assert any("loose.txt" in w for w in body["warnings"])
    # Staging dir exists under the tmp home.
    assert (ctx.home / ".import-staging" / body["importId"]).is_dir()


# ------------------------------------------- import: commit resolve (2.7)


def _stage(ctx, entries):
    return _post_import(ctx, _make_zip(entries)).json()["importId"]


def test_commit_keep_both_and_overwrite_and_invalid(ctx):
    ctx.make_template("brandcard", extra={"marker.txt": "OLD"})
    iid = _stage(
        ctx,
        {
            "brandcard/template.html": "<html>NEW</html>",
            "fresh/template.html": "<html>",
            "notemplate/readme.txt": "hi",
        },
    )
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"brandcard": "keep-both", "fresh": "overwrite"}},
        headers=FUSED,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["imported"]) == {"brandcard-2", "fresh"}
    assert body["renamed"] == {"brandcard": "brandcard-2"}
    assert body["overwritten"] == []  # fresh did not exist beforehand
    # Original brandcard is untouched; the keep-both copy landed as brandcard-2.
    assert (ctx.udir / "brandcard" / "marker.txt").read_text() == "OLD"
    assert (ctx.udir / "brandcard-2" / "template.html").read_text() == "<html>NEW</html>"
    assert (ctx.udir / "fresh" / "template.html").exists()
    # Invalid item was dropped; staging swept.
    assert not (ctx.udir / "notemplate").exists()
    assert not (ctx.home / ".import-staging" / iid).exists()


def test_commit_overwrite_replaces_existing(ctx):
    ctx.make_template("brandcard", extra={"old.txt": "OLD"})
    iid = _stage(ctx, {"brandcard/template.html": "<html>NEW</html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"brandcard": "overwrite"}},
        headers=FUSED,
    ).json()
    assert body["overwritten"] == ["brandcard"]
    assert body["imported"] == ["brandcard"]
    # The old file is gone; the new content is present.
    assert not (ctx.udir / "brandcard" / "old.txt").exists()
    assert (ctx.udir / "brandcard" / "template.html").read_text() == "<html>NEW</html>"


def test_commit_default_and_explicit_skip(ctx):
    iid = _stage(ctx, {"fresh/template.html": "<html>", "other/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"other": "skip"}},  # fresh omitted -> defaults to skip
        headers=FUSED,
    ).json()
    assert set(body["skipped"]) == {"fresh", "other"}
    assert body["imported"] == []
    assert not (ctx.udir / "fresh").exists()
    assert not (ctx.udir / "other").exists()


def test_commit_unknown_import_id(ctx):
    resp = ctx.client.post(
        "/api/templates/import/deadbeefdeadbeef/commit", json={"resolutions": {}}, headers=FUSED
    )
    assert resp.status_code == 404


def test_commit_malformed_import_id(ctx):
    resp = ctx.client.post(
        "/api/templates/import/..%2f..%2fetc/commit", json={"resolutions": {}}, headers=FUSED
    )
    assert resp.status_code == 404


def test_commit_expired_import_id(ctx, monkeypatch):
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    staging = ctx.home / ".import-staging" / iid
    # Age the staging dir past the TTL.
    old = os.path.getmtime(staging) - templates_api.IMPORT_TTL_SEC - 10
    os.utime(staging, (old, old))
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit", json={"resolutions": {"fresh": "overwrite"}}, headers=FUSED
    )
    assert resp.status_code == 410
    assert not staging.exists()  # expired stage is swept


def test_commit_requires_fused_header(ctx):
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit", json={"resolutions": {"fresh": "overwrite"}}
    )
    assert resp.status_code == 403


def test_import_sweeps_stale_staging(ctx, monkeypatch):
    # A pre-existing stale staging dir is swept on the next import call.
    staging_root = ctx.home / ".import-staging"
    stale = staging_root / "0000000000000000"
    stale.mkdir(parents=True)
    old = os.path.getmtime(stale) - templates_api.IMPORT_TTL_SEC - 10
    os.utime(stale, (old, old))
    _post_import(ctx, _make_zip({"fresh/template.html": "<html>"}))
    assert not stale.exists()
