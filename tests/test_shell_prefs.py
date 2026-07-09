"""Tests for the Preferences backend (SPEC §20): GET/PUT /api/prefs
(shell/prefs.py — the persisted engine preference + log location), the
per-request engine dispatch it drives in /api/run, and the merged
extension→templates registry view (GET /api/templates/registry).

FUSED_RENDER_HOME is redirected to a tmp dir and FUSED_RENDER_ENGINE cleared
so no test reads the real prefs or a developer's env override.
"""
import json

from fastapi.testclient import TestClient

import fused_render.shell.prefs as prefs_mod
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


def _client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app), home


# -- /api/prefs -----------------------------------------------------------------


def test_defaults_builtin_unforced(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    body = client.get("/api/prefs").json()
    assert body["engine"]["selected"] == "builtin"
    assert body["engine"]["effective"] == "builtin"
    assert body["engine"]["forced_by"] is None
    assert isinstance(body["engine"]["fused_available"], bool)
    assert body["log"]["path"].endswith(".log")


def test_put_persists_and_degrades_while_fused_unavailable(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: False)
    body = client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED).json()
    # Persisted...
    saved = json.loads((home / "prefs.json").read_text(encoding="utf-8"))
    assert saved["engine"] == "fused"
    assert body["engine"]["selected"] == "fused"
    # ...but effective degrades to builtin until the package is importable.
    assert body["engine"]["effective"] == "builtin"

    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    assert client.get("/api/prefs").json()["engine"]["effective"] == "fused"


def test_put_rejects_unknown_engine_and_missing_header(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    assert client.put("/api/prefs", json={"engine": "warp"}, headers=FUSED).status_code == 400
    assert client.put("/api/prefs", json={"engine": "fused"}).status_code == 403
    assert not (home / "prefs.json").exists()


def test_deploy_enabled_defaults_off_and_toggles(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Default off (opt-in), so the preview-header Deploy button stays hidden.
    assert client.get("/api/prefs").json()["deploy"]["enabled"] is False
    # Turn it on — persisted and reflected in the response and a fresh GET.
    body = client.put("/api/prefs", json={"deploy_enabled": True}, headers=FUSED).json()
    assert body["deploy"]["enabled"] is True
    assert json.loads((home / "prefs.json").read_text(encoding="utf-8"))["deploy_enabled"] is True
    assert client.get("/api/prefs").json()["deploy"]["enabled"] is True
    # And back off.
    assert client.put("/api/prefs", json={"deploy_enabled": False}, headers=FUSED).json()[
        "deploy"
    ]["enabled"] is False


def test_deploy_enabled_toggle_is_independent_of_engine(tmp_path, monkeypatch):
    # A partial PUT touching only deploy_enabled must not disturb the engine pref.
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    body = client.put("/api/prefs", json={"deploy_enabled": True}, headers=FUSED).json()
    assert body["engine"]["selected"] == "fused"
    assert body["deploy"]["enabled"] is True


def test_put_rejects_bad_deploy_enabled_and_empty_body(tmp_path, monkeypatch):
    client, home = _client(tmp_path, monkeypatch)
    # Non-boolean deploy_enabled …
    assert (
        client.put("/api/prefs", json={"deploy_enabled": "yes"}, headers=FUSED).status_code == 400
    )
    # … and a PUT naming no known preference are both rejected without a write.
    assert client.put("/api/prefs", json={"nope": 1}, headers=FUSED).status_code == 400
    assert not (home / "prefs.json").exists()


def test_env_var_reports_as_forcing(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    # The pref still persists (applies once the override is removed), but the
    # reported state says who is in charge right now.
    body = client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED).json()
    assert body["engine"]["forced_by"] == "builtin"
    assert body["engine"]["effective"] == "builtin"


def test_forced_auto_reports_match_dispatch_after_midsession_install(tmp_path, monkeypatch):
    # FUSED_RENDER_ENGINE=auto with fused absent at startup. The engine must be
    # resolved LIVE, so a mid-session install (which /api/deploy/install
    # supports) flips BOTH the reported state and actual dispatch together —
    # the page never claims a different running engine than /api/run uses.
    # (Built here, not via _client, since _client clears FUSED_RENDER_ENGINE
    # and create_app must see =auto at startup.)
    available = {"v": False}
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "auto")
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: available["v"])
    client = TestClient(create_app(start_dir=str(tmp_path)))  # validates =auto (no raise)

    assert client.get("/api/config").json()["engine"] == "builtin"
    assert client.get("/api/prefs").json()["engine"]["effective"] == "builtin"

    available["v"] = True  # installed mid-session
    assert client.get("/api/config").json()["engine"] == "fused"
    assert client.get("/api/prefs").json()["engine"]["effective"] == "fused"


# -- per-request engine dispatch (server /api/run + /api/config) -----------------


async def _fake_fused_run(path, params):
    return {"ok": True, "result": {"engine": "fused-stub"}, "stdout": ""}


def test_engine_switch_applies_without_restart(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    (tmp_path / "one.py").write_text("def main():\n    return {'engine': 'builtin-real'}\n", encoding="utf-8")

    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    monkeypatch.setattr("fused_render.engine.run_python", _fake_fused_run, raising=False)

    # Default pref: the built-in executor really runs the file.
    assert client.get("/api/config").json()["engine"] == "builtin"
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "builtin-real"}

    # Flip the pref — the SAME app instance dispatches the next run to the
    # fused engine (no restart), and /api/config reports it.
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    assert client.get("/api/config").json()["engine"] == "fused"
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "fused-stub"}

    # And back.
    client.put("/api/prefs", json={"engine": "builtin"}, headers=FUSED)
    run = client.post(
        "/api/run", json={"py": str(tmp_path / "one.py"), "params": {}}, headers=FUSED
    ).json()
    assert run["result"] == {"engine": "builtin-real"}


def test_forced_env_var_beats_the_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_ENGINE", "builtin")
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    client.put("/api/prefs", json={"engine": "fused"}, headers=FUSED)
    # The process override pins the engine regardless of the pref.
    assert client.get("/api/config").json()["engine"] == "builtin"


# -- /api/templates/registry ------------------------------------------------------


def _point_user_registry_at(tmp_path, monkeypatch):
    # USER_REGISTRY/USER_TEMPLATES_DIR are module constants resolved at import
    # (same seam test_templates.py patches).
    from fused_render import server

    udir = tmp_path / "user-templates"
    udir.mkdir()
    monkeypatch.setattr(server, "USER_TEMPLATES_DIR", str(udir))
    monkeypatch.setattr(server, "USER_REGISTRY", str(udir / "registry.json"))
    return udir


def _names(entry):
    # The effective ordered template names for an entry (§2.2 templates are now
    # {name, source, exists, hasIcon} objects, not bare strings).
    return [t["name"] for t in entry["templates"]]


def test_registry_view_lists_builtin_bindings(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    _point_user_registry_at(tmp_path, monkeypatch)
    body = client.get("/api/templates/registry").json()
    # The sources block is modelled for extensibility (§1) — core + user today.
    assert {s["id"] for s in body["sources"]} == {"core", "user"}
    by_key = {e["key"]: e for e in body["entries"]}
    html = by_key[".html"]
    # Pin the contract, not the whole shipped list: rendered-first default.
    assert _names(html)[:2] == ["_render", "code"]
    assert html["resolvedSource"] == "core"
    assert html["overridesCore"] is False
    assert html["keyKind"] == "simple"
    parquet = by_key[".parquet"]
    assert _names(parquet)[0] == "table"
    # Per-template objects carry the resolved source + icon presence.
    table = parquet["templates"][0]
    assert table["source"] == "core" and table["exists"] is True and table["hasIcon"] is True
    # Directory keys sort after file keys and keep their trailing slash.
    assert body["entries"][-1]["key"].endswith("/")
    assert by_key[".zarr/"]["keyKind"] == "directory"
    assert body["error"] is None


def test_registry_view_shows_user_bindings_and_overrides(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(
        json.dumps(
            {
                ".html": ["code"],  # override: drop the rendered mode
                ".xyz": ["tree"],  # new user-only binding
                ".log": None,  # disabled: no preview at all
            }
        ),
        encoding="utf-8",
    )
    body = client.get("/api/templates/registry").json()
    by_key = {e["key"]: e for e in body["entries"]}

    html = by_key[".html"]
    assert html["resolvedSource"] == "user"
    assert html["overridesCore"] is True
    assert _names(html) == ["code"]
    assert html["userValue"] == ["code"]
    assert html["coreTemplates"][:2] == ["_render", "code"]  # what a reset restores

    xyz = by_key[".xyz"]
    assert xyz["resolvedSource"] == "user"
    assert xyz["overridesCore"] is True  # the user registry defines this key
    assert _names(xyz) == ["tree"]
    assert xyz["coreTemplates"] is None  # builtin has no .xyz key

    log = by_key[".log"]
    assert log["disabled"] is True
    assert log["templates"] == []
    assert log["userValue"] is None
    # One row per key: the built-in .html entry is replaced, not doubled.
    assert sum(1 for e in body["entries"] if e["key"] == ".html") == 1


def test_registry_view_splice_token_is_dangling(tmp_path, monkeypatch):
    # Splice removed: "..." is an ordinary name kept as broken (exists:false)
    # in the row, not expanded to the built-in list.
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(
        json.dumps({".html": ["...", "tree"]}), encoding="utf-8"
    )
    body = client.get("/api/templates/registry").json()
    by_key = {e["key"]: e for e in body["entries"]}
    tmpl = {t["name"]: t for t in by_key[".html"]["templates"]}
    assert "..." in tmpl and tmpl["..."]["exists"] is False
    assert _names(by_key[".html"]) == ["...", "tree"]  # verbatim, unexpanded
    assert by_key[".html"]["error"] is None  # dangling name is not a shape error


def test_registry_view_override_is_case_insensitive(tmp_path, monkeypatch):
    # Resolution matches keys case-insensitively (_key_segments lowercases), so
    # a user ".CSV" key OVERRIDES the built-in ".csv" — the view must show ONE
    # row sourced user, not two rows (a case-sensitive `in` check would
    # double-list the key and mis-source both).
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(json.dumps({".CSV": ["code"]}), encoding="utf-8")
    entries = client.get("/api/templates/registry").json()["entries"]
    csv_rows = [e for e in entries if e["key"].lower() == ".csv"]
    assert len(csv_rows) == 1
    assert csv_rows[0]["key"] == ".CSV"
    assert csv_rows[0]["resolvedSource"] == "user"
    assert csv_rows[0]["overridesCore"] is True
    assert _names(csv_rows[0]) == ["code"]
