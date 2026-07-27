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
    structure = by_name["structure"]
    assert structure["source"] == "core"
    assert structure["editable"] is False
    assert structure["shadowsCore"] is False
    assert ".parquet" in structure["usedBy"]
    assert structure["path"] == os.path.join(os.path.abspath(server.TEMPLATES_DIR), "structure")
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


def test_inventory_reports_condition(ctx):
    # A folder with a condition.py is flagged hasCondition; a plain one is not.
    ctx.make_template("gated", extra={"condition.py": "def main(path):\n    return True\n"})
    ctx.make_template("plain")
    by_name = {t["name"]: t for t in ctx.client.get("/api/templates/inventory").json()["templates"]}
    assert by_name["gated"]["hasCondition"] is True
    assert by_name["plain"]["hasCondition"] is False
    # core templates ship no condition.py
    assert by_name["structure"]["hasCondition"] is False


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
        "name": "duckdb",
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


def test_registry_invalid_value_surfaces_error(ctx):
    # A shape-level bad value (not a list/string/null) must NOT render as a
    # silent empty row — its error surfaces on the entry.
    ctx.make_template("brandcard")
    ctx.registry({".csv": 5, ".brand": ["brandcard"]})
    by_key = {e["key"]: e for e in ctx.client.get("/api/templates/registry").json()["entries"]}
    bad = by_key[".csv"]
    assert bad["error"] is not None
    assert bad["templates"] == []
    # error (value invalid) is semantically distinct from disabled (value null).
    assert bad["disabled"] is False
    # A well-formed binding carries error=null.
    assert by_key[".brand"]["error"] is None


def test_registry_splice_token_is_dangling(ctx):
    # Splice is removed: a "..." entry is an ordinary name that resolves to no
    # folder — kept as a broken (exists:false) ref, not expanded, no error.
    ctx.registry({".csv": ["...", "csv"]})
    by_key = {e["key"]: e for e in ctx.client.get("/api/templates/registry").json()["entries"]}
    row = by_key[".csv"]
    assert row["error"] is None
    names = {t["name"]: t for t in row["templates"]}
    assert names["..."]["exists"] is False  # dangling, surfaced not dropped
    assert names["csv"]["exists"] is True


def test_registry_empty_list_disables_like_null(ctx):
    # An empty list disables previews exactly like null (no builtin fallback).
    ctx.registry({".csv": []})
    by_key = {e["key"]: e for e in ctx.client.get("/api/templates/registry").json()["entries"]}
    row = by_key[".csv"]
    assert row["disabled"] is True
    assert row["templates"] == []
    assert row["error"] is None


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


def test_put_allows_sentinel_and_dangling_names(ctx):
    # Sentinels are accepted...
    ok = ctx.client.put(
        "/api/templates/registry", json={"key": ".html", "value": ["code", "_render"]}, headers=FUSED
    )
    assert ok.status_code == 200
    assert _names(ok.json()) == ["code", "_render"]
    # ...and so is a dangling name (incl. the retired "..." token): PUT saves it
    # rather than blocking the whole binding — the UI surfaces it broken so the
    # user can fix it in their own time (they are not forced to remove it now).
    saved = ctx.client.put(
        "/api/templates/registry", json={"key": ".html", "value": ["code", "..."]}, headers=FUSED
    )
    assert saved.status_code == 200
    tmpl = {t["name"]: t for t in saved.json()["templates"]}
    assert tmpl["code"]["exists"] is True
    assert tmpl["..."]["exists"] is False  # saved, surfaced broken
    assert ctx.read_registry()[".html"] == ["code", "..."]


def test_put_empty_list_disables(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".png", "value": []}, headers=FUSED
    )
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["disabled"] is True
    assert entry["templates"] == []
    assert entry["userValue"] == []


def test_put_invalid_key_rejected(ctx):
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".geo*.json", "value": ["csv"]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "invalid registry key" in resp.json()["error"]


def test_put_unknown_names_saved_as_dangling(ctx):
    # Unknown names are no longer rejected — they save and surface broken, so a
    # user can bind a template they haven't created yet without being blocked.
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".csv", "value": ["nope", "also-nope"]}, headers=FUSED
    )
    assert resp.status_code == 200
    assert all(t["exists"] is False for t in resp.json()["templates"])
    assert ctx.read_registry()[".csv"] == ["nope", "also-nope"]


def test_put_rejects_non_string_entries(ctx):
    # Structurally invalid entries (not non-empty strings) are still rejected.
    resp = ctx.client.put(
        "/api/templates/registry", json={"key": ".csv", "value": ["csv", 5]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "non-empty string" in resp.json()["error"]


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
    assert _names(entry)[0] == "duckdb"  # the builtin default
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
    # Repeated `names=` params, not a comma-joined string.
    resp = ctx.client.get("/api/templates/export", params={"names": ["alpha", "beta"]})
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
        "recommendation.json",  # bindings sidecar at the zip root
    }
    assert not any("registry.json" in n for n in names)


def test_export_handles_comma_in_template_name(ctx):
    # A folder name containing a comma round-trips because names travel as
    # repeated params, not a comma-joined string.
    ctx.make_template("a,b")
    resp = ctx.client.get("/api/templates/export", params={"names": ["a,b"]})
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert set(zf.namelist()) == {"a,b/template.html", "recommendation.json"}


def test_delete_user_template(ctx):
    ctx.make_template("mine")
    assert (ctx.udir / "mine").is_dir()
    resp = ctx.client.post("/api/templates/delete", json={"name": "mine"}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine"}
    assert not (ctx.udir / "mine").exists()


def test_delete_core_template_refused(ctx):
    # 'code' is a CORE template (no user folder). Delete only touches
    # USER_TEMPLATES_DIR, so this 404s and the core folder is untouched.
    resp = ctx.client.post("/api/templates/delete", json={"name": "code"}, headers=FUSED)
    assert resp.status_code == 404
    # core 'code' still exports fine afterwards
    assert ctx.client.get("/api/templates/export", params={"names": ["code"]}).status_code == 200


def test_delete_user_shadow_leaves_core(ctx):
    # A user 'code' shadows the core one; deleting removes only the user folder.
    ctx.make_template("code", extra={"marker.txt": "USER"})
    resp = ctx.client.post("/api/templates/delete", json={"name": "code"}, headers=FUSED)
    assert resp.status_code == 200
    assert not (ctx.udir / "code").exists()
    # core 'code' is still there (export now resolves to core)
    assert ctx.client.get("/api/templates/export", params={"names": ["code"]}).status_code == 200


def test_delete_unknown_name(ctx):
    resp = ctx.client.post("/api/templates/delete", json={"name": "nope"}, headers=FUSED)
    assert resp.status_code == 404


def test_delete_rejects_traversal_name(ctx):
    resp = ctx.client.post("/api/templates/delete", json={"name": "../secrets"}, headers=FUSED)
    assert resp.status_code == 400


def test_delete_requires_fused_header(ctx):
    ctx.make_template("mine")
    resp = ctx.client.post("/api/templates/delete", json={"name": "mine"})
    assert resp.status_code == 403
    assert (ctx.udir / "mine").is_dir()  # untouched


def test_delete_without_flag_leaves_registry(ctx):
    # The pre-D109 default: no cleanRegistry -> bindings untouched, no
    # registryKeysCleaned in the response.
    ctx.make_template("mine")
    ctx.registry({".a": ["mine"]})
    resp = ctx.client.post("/api/templates/delete", json={"name": "mine"}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine"}
    assert ctx.read_registry() == {".a": ["mine"]}


def test_delete_clean_registry_prunes_lists(ctx):
    # cleanRegistry drops the name from every referencing user key; keys that
    # never referenced it are untouched (D109).
    ctx.make_template("mine")
    ctx.registry({".a": ["mine", "other"], ".b": ["other"]})
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": [".a"]}
    assert ctx.read_registry() == {".a": ["other"], ".b": ["other"]}


def test_delete_clean_registry_emptied_key_removed(ctx):
    # A key whose list the sweep empties is REMOVED (revert to core), never
    # left as [] — which would mean disabled per D95. A null (disabled) value
    # never references the name, so it survives byte-for-byte.
    ctx.make_template("mine")
    ctx.registry({".a": ["mine"], ".b": None})
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": [".a"]}
    assert ctx.read_registry() == {".b": None}


def test_delete_clean_registry_string_value_removes_key(ctx):
    # A bare-string value equal to the name counts as a reference; matching it
    # empties the value, so the key is removed entirely.
    ctx.make_template("mine")
    ctx.registry({".a": "mine"})
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": [".a"]}
    assert ctx.read_registry() == {}


def test_delete_clean_registry_name_match_is_exact(ctx):
    # Names are folder identities — unlike keys they are NOT lowercased, so a
    # differently-cased entry is a different name and stays bound.
    ctx.make_template("mine")
    ctx.registry({".a": ["Mine", "mine"]})
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": [".a"]}
    assert ctx.read_registry() == {".a": ["Mine"]}


def test_delete_clean_registry_no_references(ctx):
    # Nothing referenced the name: the sweep is a no-op, the registry file is
    # not rewritten, and registryKeysCleaned reports [].
    ctx.make_template("mine")
    ctx.registry({".a": ["other"]})
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": []}
    assert ctx.read_registry() == {".a": ["other"]}


def test_delete_clean_registry_corrupt_registry_refused(ctx):
    # A corrupt user registry is refused BEFORE the rmtree — the folder must
    # survive so the user can fix the registry and retry the whole gesture.
    ctx.make_template("mine")
    (ctx.udir / "registry.json").write_text("{not json", encoding="utf-8")
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "refusing to overwrite the user registry" in resp.json()["error"]
    assert (ctx.udir / "mine").is_dir()  # untouched


def test_delete_clean_registry_sweeps_fresh_snapshot(ctx, monkeypatch):
    # The sweep must rewrite the registry as it is AFTER the rmtree, not the
    # pre-check's snapshot — a binding edited concurrently while the delete is
    # in flight has to survive the write. Simulate the race by mutating the
    # registry file from inside rmtree.
    import shutil as _shutil

    ctx.make_template("mine")
    ctx.registry({".a": ["mine"]})
    real_rmtree = _shutil.rmtree

    def racing_rmtree(path, *args, **kwargs):
        ctx.registry({".a": ["mine"], ".b": ["late"]})  # concurrent edit
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("fused_render.templates_api.shutil.rmtree", racing_rmtree)
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "mine", "registryKeysCleaned": [".a"]}
    assert ctx.read_registry() == {".b": ["late"]}  # the late edit survived


def test_delete_clean_registry_corrupted_after_rmtree_skips_sweep(ctx, monkeypatch):
    # If the registry turns corrupt between the pre-check and the post-rmtree
    # re-read, the folder is already gone — report the delete, skip the sweep,
    # and surface the parse error instead of overwriting an unparseable file.
    import shutil as _shutil

    ctx.make_template("mine")
    ctx.registry({".a": ["mine"]})
    real_rmtree = _shutil.rmtree

    def corrupting_rmtree(path, *args, **kwargs):
        (ctx.udir / "registry.json").write_text("{not json", encoding="utf-8")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("fused_render.templates_api.shutil.rmtree", corrupting_rmtree)
    resp = ctx.client.post(
        "/api/templates/delete", json={"name": "mine", "cleanRegistry": True}, headers=FUSED
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == "mine"
    assert data["registryKeysCleaned"] == []
    assert data["registryError"]
    assert not (ctx.udir / "mine").exists()
    # unparseable file left byte-for-byte for the user to fix
    assert (ctx.udir / "registry.json").read_text(encoding="utf-8") == "{not json"


def test_export_allows_core_template(ctx):
    # 'code' is a core template — core templates are exportable too (SPEC
    # §2.5 update): resolve via core dir since there's no user shadow.
    resp = ctx.client.get("/api/templates/export", params={"names": "code"})
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
    assert names == {"code/template.html", "code/icon.svg", "recommendation.json"}


def test_export_user_shadow_wins_over_core(ctx):
    # 'code' also exists as a core template; the user folder should win.
    ctx.make_template("code", extra={"marker.txt": "USER"})
    resp = ctx.client.get("/api/templates/export", params={"names": "code"})
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        marker = zf.read("code/marker.txt")
    assert names == {"code/template.html", "code/marker.txt", "recommendation.json"}
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


def _export_recommendations(ctx, names):
    resp = ctx.client.get("/api/templates/export", params={"names": names})
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    return json.loads(zf.read("recommendation.json"))


def test_export_writes_recommendation_sidecar(ctx):
    ctx.make_template("alpha")
    ctx.make_template("beta")
    ctx.make_template("gamma")  # bound to nothing -> omitted
    ctx.registry({".foo": ["alpha"], ".bar": ["alpha", "beta"], ".baz/": ["alpha"]})
    body = _export_recommendations(ctx, ["gamma", "beta", "alpha"])
    assert body["version"] == 1
    # Keys sorted per template; unbound gamma omitted entirely.
    assert body["recommendations"] == {
        "alpha": [".bar", ".baz/", ".foo"],
        "beta": [".bar"],
    }


def test_export_recommendations_use_merged_registry(ctx):
    # A user override of ".csv" replaces core's list — 'duckdb' (in core's
    # ".csv") is no longer effectively bound to it, 'alpha' is.
    ctx.make_template("alpha")
    ctx.registry({".csv": ["alpha"]})
    recs = _export_recommendations(ctx, ["alpha", "duckdb"])["recommendations"]
    assert ".csv" in recs["alpha"]
    assert ".csv" not in recs.get("duckdb", [])
    # duckdb keeps its OTHER core bindings (e.g. ".parquet").
    assert ".parquet" in recs["duckdb"]


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


def test_import_rejects_zip_bomb_on_actual_bytes(ctx):
    # ~30 MB of zeros compresses to a few KB but expands past the 25 MB
    # per-entry cap. The cap is enforced on the bytes actually decompressed
    # DURING extraction (bounded/chunked), not on trusted file_size metadata.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb/big.bin", b"\x00" * (30 * 1024 * 1024))
    resp = _post_import(ctx, buf.getvalue())
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"]
    # Aborted mid-extraction and cleaned up: no leftover staging dir, and
    # nothing committed into the user templates dir.
    staging = ctx.home / ".import-staging"
    assert not staging.exists() or not any(staging.iterdir())
    assert not (ctx.udir / "bomb").exists()


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


def test_commit_rolls_back_on_midway_failure(ctx, monkeypatch):
    # If a later os.rename fails, earlier applied moves are undone — the commit
    # is all-or-nothing over USER_TEMPLATES_DIR, staging is cleaned, and the
    # handler returns 500 rather than leaving a half-applied import.
    iid = _stage(ctx, {"aaa/template.html": "<html>", "bbb/template.html": "<html>"})
    real_rename = os.rename

    def flaky_rename(src, dst):
        if str(dst).endswith(os.sep + "bbb"):
            raise OSError("simulated failure landing second template")
        return real_rename(src, dst)

    monkeypatch.setattr(templates_api.os, "rename", flaky_rename)
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"aaa": "overwrite", "bbb": "overwrite"}},
        headers=FUSED,
    )
    assert resp.status_code == 500
    assert "import failed" in resp.json()["error"]
    # 'aaa' landed first but was rolled back; 'bbb' never landed.
    assert not (ctx.udir / "aaa").exists()
    assert not (ctx.udir / "bbb").exists()
    # Staging swept despite the failure.
    assert not (ctx.home / ".import-staging" / iid).exists()


def test_commit_overwrite_restores_original_on_failure(ctx, monkeypatch):
    # An overwrite that fails after displacing the original restores it, so a
    # failed commit never destroys an existing template.
    ctx.make_template("keep", extra={"marker.txt": "ORIGINAL"})
    iid = _stage(
        ctx,
        {"aaa/template.html": "<html>", "keep/template.html": "<html>NEW</html>"},
    )
    real_rename = os.rename

    def flaky_rename(src, dst):
        # Fail the staged->target move for 'keep' (after its original is backed
        # up), forcing a restore.
        if str(dst).endswith(os.sep + "keep") and os.path.basename(os.path.dirname(src)) == iid:
            raise OSError("simulated failure landing overwrite")
        return real_rename(src, dst)

    monkeypatch.setattr(templates_api.os, "rename", flaky_rename)
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"aaa": "overwrite", "keep": "overwrite"}},
        headers=FUSED,
    )
    assert resp.status_code == 500
    # Original 'keep' is intact; nothing partially imported.
    assert (ctx.udir / "keep" / "marker.txt").read_text() == "ORIGINAL"
    assert not (ctx.udir / "aaa").exists()
    # No orphaned .bak folder left behind.
    assert not any(p.name.startswith("keep.bak.") for p in ctx.udir.iterdir())
    assert not (ctx.home / ".import-staging" / iid).exists()


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


# ------------------------------------- import: binding recommendations


def _rec_json(recs, version=1):
    return json.dumps({"version": version, "recommendations": recs})


def test_import_stage_parses_recommendations_with_statuses(ctx):
    ctx.registry({".abc": ["fresh"], ".dis": None})
    zb = _make_zip(
        {
            "fresh/template.html": "<html>",
            "recommendation.json": _rec_json({"fresh": [".abc", ".dis", ".csv", "nodot"]}),
        }
    )
    body = _post_import(ctx, zb).json()
    items = {i["name"]: i for i in body["items"]}
    # The sidecar is metadata, never warned as a stray top-level file; only
    # the ungrammatical key drew a warning and was dropped.
    assert body["warnings"] == ["recommendation.json: ignored invalid registry key 'nodot' for 'fresh'"]
    assert items["fresh"]["recommendedKeys"] == [
        {"key": ".abc", "status": "already-bound"},
        {"key": ".dis", "status": "disabled"},
        {"key": ".csv", "status": "new"},  # core-bound, but not to 'fresh'
    ]


def test_import_stage_recommendations_only_on_valid_items(ctx):
    zb = _make_zip(
        {
            "notemplate/readme.txt": "hi",
            "fresh/template.html": "<html>",
            "recommendation.json": _rec_json({"notemplate": [".foo"], "other": [".foo"]}),
        }
    )
    items = {i["name"]: i for i in _post_import(ctx, zb).json()["items"]}
    assert "recommendedKeys" not in items["notemplate"]  # invalid item
    assert "recommendedKeys" not in items["fresh"]  # no recs for it


def test_import_stage_recommendation_bad_json_warns_nonfatal(ctx):
    zb = _make_zip({"fresh/template.html": "<html>", "recommendation.json": "not json"})
    resp = _post_import(ctx, zb)
    assert resp.status_code == 200
    body = resp.json()
    assert any("not valid JSON" in w for w in body["warnings"])
    assert "recommendedKeys" not in body["items"][0]


def test_import_stage_recommendation_future_version_silently_ignored(ctx):
    zb = _make_zip(
        {
            "fresh/template.html": "<html>",
            "recommendation.json": _rec_json({"fresh": [".foo"]}, version=2),
        }
    )
    body = _post_import(ctx, zb).json()
    assert body["warnings"] == []
    assert "recommendedKeys" not in body["items"][0]


def test_commit_applies_bindings_appending_to_core_list(ctx):
    # Binding to a core-only key copies core's FULL list into the user entry
    # before appending — never a shorter shadow.
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"fresh": "overwrite"}, "bindings": {"fresh": [".csv", ".myext"]}},
        headers=FUSED,
    ).json()
    assert body["bindingsApplied"] == [
        {"key": ".csv", "template": "fresh"},
        {"key": ".myext", "template": "fresh"},
    ]
    reg = ctx.read_registry()
    assert reg[".csv"] == ["duckdb", "csv", "excel", "code", "reader", "annotate", "fresh"]
    assert reg[".myext"] == ["fresh"]


def test_commit_bindings_follow_keep_both_rename(ctx):
    ctx.make_template("brandcard")
    iid = _stage(ctx, {"brandcard/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"brandcard": "keep-both"}, "bindings": {"brandcard": [".xyz"]}},
        headers=FUSED,
    ).json()
    assert body["renamed"] == {"brandcard": "brandcard-2"}
    assert body["bindingsApplied"] == [{"key": ".xyz", "template": "brandcard-2"}]
    assert ctx.read_registry()[".xyz"] == ["brandcard-2"]


def test_commit_keep_both_binds_rename_even_when_original_already_bound(ctx):
    # Regression (owner 2026-07-15): 'test-template' is ALREADY bound to .pdf;
    # importing a zip that recommends .pdf for it with keep-both must still
    # bind the RENAMED copy — "already-bound" describes the original name, not
    # the keep-both rename, so the append must not be skipped as a no-op.
    ctx.make_template("test-template")
    ctx.registry({".pdf": ["pdf", "pdf_studio", "annotate", "test-template"]})
    iid = _stage(
        ctx,
        {
            "test-template/template.html": "<html>",
            "recommendation.json": _rec_json({"test-template": [".pdf"]}),
        },
    )
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={
            "resolutions": {"test-template": "keep-both"},
            "bindings": {"test-template": [".pdf"]},
        },
        headers=FUSED,
    ).json()
    assert body["renamed"] == {"test-template": "test-template-2"}
    assert body["bindingsApplied"] == [{"key": ".pdf", "template": "test-template-2"}]
    # Appended to the END of the existing list — core/user merge preserved.
    assert ctx.read_registry()[".pdf"] == [
        "pdf",
        "pdf_studio",
        "annotate",
        "test-template",
        "test-template-2",
    ]


def test_commit_bindings_for_skipped_template_ignored(ctx):
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {}, "bindings": {"fresh": [".xyz"]}},  # defaults to skip
        headers=FUSED,
    ).json()
    assert body["bindingsApplied"] == []
    assert not (ctx.udir / "registry.json").exists()  # nothing written


def test_commit_binding_reenables_disabled_key_with_core_list(ctx):
    ctx.registry({".csv": None})
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"fresh": "overwrite"}, "bindings": {"fresh": [".csv"]}},
        headers=FUSED,
    ).json()
    assert body["bindingsApplied"] == [{"key": ".csv", "template": "fresh"}]
    assert ctx.read_registry()[".csv"] == ["duckdb", "csv", "excel", "code", "reader", "annotate", "fresh"]


def test_commit_binding_already_bound_is_noop(ctx):
    ctx.registry({".foo": ["fresh"]})
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"fresh": "overwrite"}, "bindings": {"fresh": [".foo"]}},
        headers=FUSED,
    ).json()
    assert body["bindingsApplied"] == []
    assert ctx.read_registry() == {".foo": ["fresh"]}


def test_commit_rejects_invalid_binding_key_before_moving(ctx):
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    resp = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"fresh": "overwrite"}, "bindings": {"fresh": ["nodot"]}},
        headers=FUSED,
    )
    assert resp.status_code == 400
    assert "invalid registry key" in resp.json()["error"]
    # Nothing moved and the stage survives, so a corrected retry can commit.
    assert not (ctx.udir / "fresh").exists()
    assert (ctx.home / ".import-staging" / iid).is_dir()


def test_commit_without_bindings_reports_empty_applied(ctx):
    iid = _stage(ctx, {"fresh/template.html": "<html>"})
    body = ctx.client.post(
        f"/api/templates/import/{iid}/commit",
        json={"resolutions": {"fresh": "overwrite"}},
        headers=FUSED,
    ).json()
    assert body["bindingsApplied"] == []


# ----------------------------------------------------- new template (scaffold)


def test_new_template_scaffolds_and_binds(ctx):
    resp = ctx.client.post(
        "/api/templates/new",
        json={"name": "myview", "extensions": [".myext", ".foo.bar"]},
        headers=FUSED,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "myview"
    assert body["path"] == os.path.join(os.path.abspath(server.USER_TEMPLATES_DIR), "myview")
    assert body["bindings"] == [".myext", ".foo.bar"]
    # Starter kit was copied in — the required file plus the optional reader,
    # authoring guide, and the two canonical skills (resolved from the repo
    # skills/ dir here, since tests run from an editable/source checkout).
    folder = ctx.udir / "myview"
    assert (folder / "template.html").is_file()
    assert (folder / "reader.py").is_file()
    assert (folder / "CLAUDE.md").is_file()
    skills = folder / ".claude" / "skills"
    assert (skills / "fused-render-authoring" / "SKILL.md").is_file()
    assert (skills / "fused-render-custom-templates" / "SKILL.md").is_file()
    # Both extensions are brand new keys (no core or user binding yet), so the
    # new template is the whole list — appending onto nothing.
    reg = ctx.read_registry()
    assert reg[".myext"] == ["myview"]
    assert reg[".foo.bar"] == ["myview"]


def test_new_template_appends_to_existing_core_default(ctx):
    # Additive only (owner ask): binding a new template to a key that already
    # has a core default must append to that list, never replace it — an
    # existing multi-mode viewer (e.g. .csv -> duckdb/csv/code/annotate) keeps
    # every prior mode plus the new one at the end.
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "mycsv", "extensions": [".csv"]}, headers=FUSED
    )
    assert resp.status_code == 200
    reg = ctx.read_registry()
    assert reg[".csv"] == ["duckdb", "csv", "excel", "code", "reader", "annotate", "mycsv"]


def test_new_template_appends_to_existing_user_override(ctx):
    # Same additive rule when the key already has a user override (not just a
    # core default): the new template lands at the end, prior names untouched.
    ctx.registry({".myext": ["alpha", "beta"]})
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "gamma", "extensions": [".myext"]}, headers=FUSED
    )
    assert resp.status_code == 200
    reg = ctx.read_registry()
    assert reg[".myext"] == ["alpha", "beta", "gamma"]


def test_new_template_no_extensions_creates_no_registry(ctx):
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "draft", "extensions": []}, headers=FUSED
    )
    assert resp.status_code == 200
    assert resp.json()["bindings"] == []
    assert (ctx.udir / "draft" / "template.html").is_file()
    # No bindings requested -> the registry file is not created.
    assert not (ctx.udir / "registry.json").exists()


def test_new_template_binding_reachable_via_registry_view(ctx):
    ctx.client.post(
        "/api/templates/new", json={"name": "myview", "extensions": [".myext"]}, headers=FUSED
    )
    by_key = {e["key"]: e for e in ctx.client.get("/api/templates/registry").json()["entries"]}
    entry = by_key[".myext"]
    assert entry["resolvedSource"] == "user"
    assert _names(entry) == ["myview"]
    # The scaffolded template.html resolves, so the bound name is not broken.
    assert entry["templates"][0]["exists"] is True


def test_new_template_draft_ignores_corrupt_registry(ctx):
    # A no-extensions draft create never touches the registry, so a pre-existing
    # corrupt registry.json must not block it.
    ctx.udir.mkdir(parents=True, exist_ok=True)
    (ctx.udir / "registry.json").write_text("{not json")
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "draft", "extensions": []}, headers=FUSED
    )
    assert resp.status_code == 200
    assert (ctx.udir / "draft" / "template.html").is_file()


def test_new_template_with_extensions_rejects_corrupt_registry(ctx):
    # Once a binding is requested the registry must actually be read and
    # rewritten, so a corrupt file is refused up front and nothing is created.
    ctx.udir.mkdir(parents=True, exist_ok=True)
    (ctx.udir / "registry.json").write_text("{not json")
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "draft", "extensions": [".x"]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "refusing to overwrite the user registry" in resp.json()["error"]
    assert not (ctx.udir / "draft").exists()


def test_new_template_registry_write_failure_cleans_up(ctx, monkeypatch):
    # A folder that scaffolded fine but whose registry write blew up must not
    # linger — otherwise every retry 409s on a template that was never bound.
    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(templates_api.storage, "write_json", boom)
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "half", "extensions": [".x"]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "failed to bind template" in resp.json()["error"]
    assert not (ctx.udir / "half").exists()
    assert not (ctx.udir / "registry.json").exists()


def test_new_template_duplicate_conflicts_409(ctx):
    ctx.make_template("taken")
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "taken", "extensions": [".x"]}, headers=FUSED
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]


def test_new_template_copytree_failure_cleans_up(ctx, monkeypatch):
    # A mid-copy failure must not leave a half-created folder behind, and the
    # caller gets a clean error rather than an unhandled 500 traceback.
    def boom(src, dst, *args, **kwargs):
        os.makedirs(dst, exist_ok=True)  # partial folder, as a real copy would
        raise OSError("disk full")

    monkeypatch.setattr(templates_api.shutil, "copytree", boom)
    resp = ctx.client.post(
        "/api/templates/new", json={"name": "doomed", "extensions": [".x"]}, headers=FUSED
    )
    assert resp.status_code == 400
    assert "failed to create template" in resp.json()["error"]
    # The half-created folder was removed and no binding was written.
    assert not (ctx.udir / "doomed").exists()
    assert not (ctx.udir / "registry.json").exists()


def test_new_template_rejects_bad_name(ctx):
    for bad in ("has/slash", "has.dot", "_leading", ""):
        resp = ctx.client.post(
            "/api/templates/new", json={"name": bad, "extensions": []}, headers=FUSED
        )
        assert resp.status_code == 400, bad


def test_new_template_invalid_extension_creates_nothing(ctx):
    resp = ctx.client.post(
        "/api/templates/new",
        json={"name": "myview", "extensions": [".ok", ".geo*.json"]},
        headers=FUSED,
    )
    assert resp.status_code == 400
    assert "invalid registry key" in resp.json()["error"]
    # Rejected up front — no folder created, no registry written.
    assert not (ctx.udir / "myview").exists()
    assert not (ctx.udir / "registry.json").exists()


def test_new_template_requires_fused_header(ctx):
    resp = ctx.client.post("/api/templates/new", json={"name": "myview", "extensions": []})
    assert resp.status_code == 403
    assert not (ctx.udir / "myview").exists()


# --------------------------------------------------------- open in Claude (macOS)


def test_open_in_claude_success(ctx, monkeypatch):
    ctx.make_template("myview")
    monkeypatch.setattr(templates_api.sys, "platform", "darwin")
    monkeypatch.setattr(templates_api.shutil, "which", lambda _c: "/fake/bin/claude")
    calls = []
    monkeypatch.setattr(templates_api.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    resp = ctx.client.post("/api/templates/open-in-claude", json={"name": "myview"}, headers=FUSED)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # osascript invoked with a `do script` that cd's into the template folder
    # and runs the located claude binary.
    (argv,), _kw = calls[0]
    assert argv[0] == "osascript"
    joined = " ".join(argv)
    assert str(ctx.udir / "myview") in joined
    assert "/fake/bin/claude" in joined


def test_open_in_claude_non_darwin_errors(ctx, monkeypatch):
    ctx.make_template("myview")
    monkeypatch.setattr(templates_api.sys, "platform", "linux")
    resp = ctx.client.post("/api/templates/open-in-claude", json={"name": "myview"}, headers=FUSED)
    assert resp.status_code == 400
    assert "macOS" in resp.json()["error"]


def test_open_in_claude_missing_template_404(ctx, monkeypatch):
    monkeypatch.setattr(templates_api.sys, "platform", "darwin")
    resp = ctx.client.post("/api/templates/open-in-claude", json={"name": "nope"}, headers=FUSED)
    assert resp.status_code == 404


def test_open_in_claude_core_template_refused(ctx, monkeypatch):
    # 'code' is a core template (no user folder) -> 404, core folder untouched.
    monkeypatch.setattr(templates_api.sys, "platform", "darwin")
    resp = ctx.client.post("/api/templates/open-in-claude", json={"name": "code"}, headers=FUSED)
    assert resp.status_code == 404


def test_open_in_claude_requires_fused_header(ctx):
    ctx.make_template("myview")
    resp = ctx.client.post("/api/templates/open-in-claude", json={"name": "myview"})
    assert resp.status_code == 403
