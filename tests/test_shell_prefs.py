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


def test_registry_view_lists_builtin_bindings(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    _point_user_registry_at(tmp_path, monkeypatch)
    body = client.get("/api/templates/registry").json()
    by_pattern = {e["pattern"]: e for e in body["entries"]}
    html = by_pattern[".html"]
    # Pin the contract, not the whole shipped list: rendered-first default.
    assert html["templates"][:2] == ["_render", "code"]
    assert html["source"] == "builtin"
    assert by_pattern[".parquet"]["templates"][0] == "table"
    # Directory keys sort after file keys and keep their trailing slash.
    assert body["entries"][-1]["pattern"].endswith("/")
    assert body["error"] is None


def test_registry_view_shows_user_bindings_and_overrides(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(
        json.dumps(
            {
                ".html": ["code"],  # override: drop the rendered mode
                ".xyz": ["tree", "..."],  # new binding; splice expands to nothing
                ".log": None,  # disabled: no preview at all
            }
        ),
        encoding="utf-8",
    )
    body = client.get("/api/templates/registry").json()
    by_pattern = {e["pattern"]: e for e in body["entries"]}
    assert by_pattern[".html"]["source"] == "user-override"
    assert by_pattern[".html"]["templates"] == ["code"]
    assert by_pattern[".xyz"]["source"] == "user"
    assert by_pattern[".xyz"]["templates"] == ["tree"]
    assert by_pattern[".log"]["disabled"] is True
    assert by_pattern[".log"]["templates"] == []
    # One row per pattern: the built-in .html entry is replaced, not doubled.
    assert sum(1 for e in body["entries"] if e["pattern"] == ".html") == 1


def test_registry_view_splice_expands_builtin_list(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    # Capture the shipped built-in list first, then splice onto it — the test
    # holds whatever modes the built-in registry grows.
    builtin_html = {
        e["pattern"]: e for e in client.get("/api/templates/registry").json()["entries"]
    }[".html"]["templates"]
    (udir / "registry.json").write_text(
        json.dumps({".html": ["...", "tree"]}), encoding="utf-8"
    )
    body = client.get("/api/templates/registry").json()
    by_pattern = {e["pattern"]: e for e in body["entries"]}
    assert by_pattern[".html"]["templates"] == builtin_html + ["tree"]


def test_registry_view_override_is_case_insensitive(tmp_path, monkeypatch):
    # Resolution matches keys case-insensitively (_key_segments lowercases), so
    # a user ".CSV" key OVERRIDES the built-in ".csv" — the view must show ONE
    # row sourced user-override, not two rows (a case-sensitive `in` check
    # would double-list the pattern and mis-source both).
    client, _ = _client(tmp_path, monkeypatch)
    udir = _point_user_registry_at(tmp_path, monkeypatch)
    (udir / "registry.json").write_text(json.dumps({".CSV": ["code"]}), encoding="utf-8")
    entries = client.get("/api/templates/registry").json()["entries"]
    csv_rows = [e for e in entries if e["pattern"].lower() == ".csv"]
    assert len(csv_rows) == 1
    assert csv_rows[0]["pattern"] == ".CSV"
    assert csv_rows[0]["source"] == "user-override"
    assert csv_rows[0]["templates"] == ["code"]

    # And a "..." splice against a case-differing builtin expands to the
    # builtin's list (found case-insensitively), not to nothing.
    (udir / "registry.json").write_text(json.dumps({".CSV": ["...", "code"]}), encoding="utf-8")
    entries = client.get("/api/templates/registry").json()["entries"]
    row = next(e for e in entries if e["pattern"] == ".CSV")
    assert row["templates"][0] == "csv"  # the builtin .csv default, spliced in
