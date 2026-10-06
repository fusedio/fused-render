"""Starter apps (port of OpenBot tests/test_starters.py): the catalog under
fused_render/bots/starters/<key>/, copying one into the apps folder
(bots/starters.py), the preset.json `apps` hook that installs them when a bot
is created, and the `/api/bot-apps/starters*` routes. App tools are monkeypatched:
no starter's Python ever runs here."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import time

import pytest

from fused_render.bots import apptools
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths
from fused_render.bots import presets as presets_mod
from fused_render.bots import registry, store
from fused_render.bots import starters


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A tmp fused workspace (apps root = <ws>/app), no greeting model call."""
    w = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(w))
    os.makedirs(bpaths.apps_root())
    monkeypatch.setattr(apptools, "ROOTS", [bpaths.apps_root()])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    monkeypatch.setattr(apptools, "_cache_at", 0.0)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    return w


@pytest.fixture
def bot(ws):
    registry.reset_for_tests()
    bid = "s1"
    os.makedirs(bpaths.bot_dir(bid))
    store.write_meta(bid, {"id": bid, "name": "Tester", "model": "sonnet", "effort": "low", "status": "idle",
                           "instructions": "", "created": time.time(), "task": "", "step": 0, "url": None, "title": None})
    b = registry.get(bid)
    yield b
    registry.reset_for_tests()


def _docs(root):
    return next(s for s in starters.list_state(root)["starters"] if s["key"] == "google-docs-tabs")


def test_catalog_lists_the_shipped_starters():
    cat = {s["key"]: s for s in starters.starters()}
    for key, min_tools, ready_key in (("google-docs-tabs", 10, "connected"), ("google-sheets-tabs", 10, "connected"),
                                      ("apple-notes", 5, "ok"), ("mac-automation", 15, "ok")):
        assert key in cat, key
        s = cat[key]
        assert s["name"] and s["desc"] and s["version"], key
        assert s["tools"] >= min_tools, key
        assert s["icon"], key
        assert s["setup_tool"].endswith("_status") and s["ready_key"] == ready_key, key
        # the copy is clean: nothing machine-local rides along
        for bad in (".venv", ".fused", "__pycache__", ".DS_Store"):
            assert not os.path.exists(os.path.join(s["src"], bad)), f"{key}/{bad}"
        assert os.path.isfile(os.path.join(s["src"], "mcp.toml")) and os.path.isfile(os.path.join(s["src"], "pyproject.toml"))
        assert os.path.dirname(s["src"]) == starters.STARTERS_DIR


def test_google_starters_ship_their_lockfiles():
    for key in ("google-docs-tabs", "google-sheets-tabs"):
        assert os.path.isfile(os.path.join(starters.STARTERS_DIR, key, "uv.lock")), key


def test_list_reports_install_state(tmp_path):
    root = str(tmp_path / "app")
    docs = _docs(root)
    assert docs["installed"] is False and docs["dir"] == "" and docs["update"] is False
    assert "src" not in docs
    starters.install("google-docs-tabs", root)
    docs = _docs(root)
    assert docs["installed"] is True and docs["dir"] == os.path.join(root, "google-docs-tabs")
    assert docs["installed_version"] == docs["version"] and docs["update"] is False


def test_install_copies_the_app_and_records_the_version(tmp_path):
    root = str(tmp_path / "app")
    r = starters.install("google-docs-tabs", root)
    assert r["installed"] is True and r["existed"] is False and r["name"] == "Google Docs Tabs"
    d = r["dir"]
    for f in ("index.html", "mcp.toml", "pyproject.toml", "docs.py", "icon.svg", "uv.lock"):
        assert os.path.isfile(os.path.join(d, f)), f
    with open(os.path.join(d, ".fused", "starter.json")) as f:
        rec = json.load(f)
    assert rec["starter"] == "google-docs-tabs" and rec["version"] and rec["installed_at"]


def test_install_never_overwrites_an_existing_folder(tmp_path):
    root = str(tmp_path / "app")
    d = os.path.join(root, "google-docs-tabs")
    os.makedirs(d)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write("<!doctype html><title>mine</title>")
    r = starters.install("google-docs-tabs", root)
    assert r["installed"] is False and r["existed"] is True
    with open(os.path.join(d, "index.html")) as f:
        assert f.read() == "<!doctype html><title>mine</title>"
    assert not os.path.exists(os.path.join(d, "mcp.toml"))


def test_update_replaces_shipped_files_but_keeps_state(tmp_path):
    root = str(tmp_path / "app")
    d = starters.install("google-docs-tabs", root)["dir"]
    with open(os.path.join(d, "docs.py"), "a") as f:
        f.write("\n# edited by a build\n")
    os.makedirs(os.path.join(d, ".fused", "data"))
    with open(os.path.join(d, ".fused", "data", "docs.json"), "w") as f:
        f.write("[]")
    with open(os.path.join(d, "notes.txt"), "w") as f:
        f.write("mine")
    with open(os.path.join(d, ".fused", "starter.json"), "w") as f:
        json.dump({"starter": "google-docs-tabs", "version": "0.0.0"}, f)
    assert _docs(root)["update"] is True
    r = starters.install("google-docs-tabs", root, update=True)
    assert r["installed"] is True and r["existed"] is True
    with open(os.path.join(d, "docs.py")) as f:
        assert "edited by a build" not in f.read()
    assert os.path.isfile(os.path.join(d, ".fused", "data", "docs.json"))
    assert os.path.isfile(os.path.join(d, "notes.txt"))
    assert _docs(root)["update"] is False


def test_unknown_starter_and_relative_root_raise(tmp_path):
    with pytest.raises(ValueError):
        starters.install("myspace-widget", str(tmp_path))
    with pytest.raises(ValueError):
        starters.install("google-docs-tabs", "app")
    with pytest.raises(ValueError):
        starters.list_state("app")
    with pytest.raises(ValueError):
        starters.status("app")


def test_ensure_installs_missing_and_skips_unknown(tmp_path):
    root = str(tmp_path / "app")
    assert starters.ensure(["google-sheets-tabs", "nope"], root) == ["Google Sheets Tabs"]
    assert starters.ensure(["google-sheets-tabs"], root) == []


def test_status_without_app_tools_reports_unknown_not_error(tmp_path, monkeypatch):
    root = str(tmp_path / "app")
    starters.install("google-docs-tabs", root)
    monkeypatch.setattr(apptools, "available", lambda: False)
    ready, why = starters.status(root)
    assert ready == {"google-docs-tabs": None}
    assert "unavailable" in why["google-docs-tabs"]


def _fake_tools(monkeypatch, result, seen=None):
    rec = apptools.ToolRec(app="google-docs-tabs", app_dir="", name="docs_status", description="")
    monkeypatch.setattr(apptools, "available", lambda: True)
    monkeypatch.setattr(apptools, "registry", lambda force=False: [rec])
    monkeypatch.setattr(apptools, "find", lambda recs, app, name: rec if (app, name) == ("google-docs-tabs", "docs_status") else None)

    def run(r, args, timeout_s=apptools.TIMEOUT_S):
        if seen is not None:
            seen.append(timeout_s)
        return result
    monkeypatch.setattr(apptools, "run_tool", run)


def test_status_runs_the_setup_tool_with_a_cap(tmp_path, monkeypatch):
    root = str(tmp_path / "app")
    starters.install("google-docs-tabs", root)
    seen = []
    _fake_tools(monkeypatch, apptools.RunResult(True, json.dumps({"connected": True})), seen)
    assert starters.status(root) == ({"google-docs-tabs": True}, {})
    assert seen == [starters.STATUS_TIMEOUT_S] and starters.STATUS_TIMEOUT_S <= 8
    _fake_tools(monkeypatch, apptools.RunResult(True, json.dumps({"connected": False})))
    assert starters.status(root) == ({"google-docs-tabs": False}, {})
    _fake_tools(monkeypatch, apptools.RunResult(False, "error: timed out"))
    ready, why = starters.status(root)
    assert ready == {"google-docs-tabs": None} and why["google-docs-tabs"] == "error: timed out"


# ---- presets that rely on a starter ----
def test_google_presets_name_their_starter_apps():
    cat = {p["key"]: p for p in presets_mod.presets()}
    assert cat["gdocs"]["apps"] == ["google-docs-tabs"]
    assert cat["gsheets"]["apps"] == ["google-sheets-tabs"]
    assert cat["applenotes"]["apps"] == ["apple-notes"]
    assert cat["mac"]["apps"] == ["mac-automation"]
    assert cat["linkedin"]["apps"] == []
    keys = {s["key"] for s in starters.starters()}
    for p in presets_mod.presets():
        for a in p["apps"]:
            assert a in keys, f"{p['key']} names a starter that does not ship: {a}"


def test_apply_preset_installs_its_starter_app(bot, tmp_path):
    root = str(tmp_path / "app")
    presets_mod.apply_preset(bot, "gdocs", apps_root=root)
    assert os.path.isfile(os.path.join(root, "google-docs-tabs", "mcp.toml"))
    assert bot.meta["preset"] == "gdocs" and bot.meta["face"]["icon"] == "gdocs"
    notes = [ev for _, ev in store.iter_events(bot.events_path)]
    assert any("Installed the Google Docs Tabs app" in n.get("text", "") for n in notes)
    # a second bot from the same preset finds the app already there and says nothing about it
    bot.meta["instructions"] = ""
    before = os.path.getsize(bot.events_path)
    presets_mod.apply_preset(bot, "gdocs", apps_root=root)
    assert os.path.getsize(bot.events_path) == before


def test_apply_preset_defaults_to_the_apps_root(bot):
    presets_mod.apply_preset(bot, "gsheets")
    assert os.path.isfile(os.path.join(bpaths.apps_root(), "google-sheets-tabs", "mcp.toml"))


def test_google_playbooks_lean_on_the_tools():
    cat = {p["key"]: p for p in presets_mod.presets()}
    for key, prefix in (("gdocs", "docs_"), ("gsheets", "sheets_")):
        for s in cat[key]["skills"]:
            assert prefix in s["body"], f"{key}/{s['name']} never names a {prefix}* tool"
        assert f"{prefix}status" in cat[key]["instructions"]


# ---- HTTP -------------------------------------------------------------------------
def j(resp):
    status, headers, body = resp
    return status, json.loads(body or b"{}")


STARTER_FIELDS = {"key", "name", "desc", "version", "tools", "icon", "setup_tool", "ready_key",
                  "installed", "dir", "installed_version", "update"}


def test_starters_route_lists_with_install_state(client, ws):
    st, out = j(client.get("/api/bot-apps/starters"))
    assert st == 200 and out["root"] == bpaths.apps_root()
    cat = {s["key"]: s for s in out["starters"]}
    assert {"google-docs-tabs", "google-sheets-tabs", "apple-notes"} <= set(cat)
    assert all(set(s) == STARTER_FIELDS for s in out["starters"])
    assert cat["google-docs-tabs"]["installed"] is False


def test_install_and_update_routes(client, ws):
    # POSTs need the X-Fused header
    st, _, _ = client.post("/api/bot-apps/starters/google-docs-tabs/install", {}, headers={"X-Fused": ""})
    assert st == 403
    assert not os.path.exists(os.path.join(bpaths.apps_root(), "google-docs-tabs"))
    st, out = j(client.post("/api/bot-apps/starters/google-docs-tabs/install", {}))
    d = os.path.join(bpaths.apps_root(), "google-docs-tabs")
    assert st == 200 and out == {"ok": True, "key": "google-docs-tabs", "dir": d, "installed": True,
                                 "existed": False, "name": "Google Docs Tabs"}
    assert os.path.isfile(os.path.join(d, "mcp.toml"))
    # a second install leaves it alone
    st, out = j(client.post("/api/bot-apps/starters/google-docs-tabs/install", {}))
    assert st == 200 and out["installed"] is False and out["existed"] is True
    with open(os.path.join(d, ".fused", "starter.json"), "w") as f:
        json.dump({"starter": "google-docs-tabs", "version": "0.0.0"}, f)
    st, out = j(client.get("/api/bot-apps/starters"))
    assert next(s for s in out["starters"] if s["key"] == "google-docs-tabs")["update"] is True
    st, out = j(client.post("/api/bot-apps/starters/google-docs-tabs/update", {}))
    assert st == 200 and out["installed"] is True and out["existed"] is True
    st, out = j(client.get("/api/bot-apps/starters"))
    docs = next(s for s in out["starters"] if s["key"] == "google-docs-tabs")
    assert docs["installed"] is True and docs["update"] is False and docs["dir"] == d


def test_install_route_unknown_key_is_400(client, ws):
    for key in ("myspace", "..", "%2E%2E"):
        st, out = j(client.post(f"/api/bot-apps/starters/{key}/install", {}))
        assert st in (400, 404), (key, st)
        if st == 400:
            assert "unknown starter" in out["error"]
    st, out = j(client.post("/api/bot-apps/starters/myspace/update", {}))
    assert st == 400 and "unknown starter" in out["error"]


def test_status_route(client, ws, monkeypatch):
    st, out = j(client.get("/api/bot-apps/starters/status"))
    assert st == 200 and out == {"ok": True, "ready": {}, "why": {}}  # nothing installed yet
    j(client.post("/api/bot-apps/starters/google-docs-tabs/install", {}))
    _fake_tools(monkeypatch, apptools.RunResult(True, json.dumps({"connected": True})))
    st, out = j(client.get("/api/bot-apps/starters/status"))
    assert st == 200 and out == {"ok": True, "ready": {"google-docs-tabs": True}, "why": {}}
    monkeypatch.setattr(apptools, "available", lambda: False)
    st, out = j(client.get("/api/bot-apps/starters/status"))
    assert out["ready"] == {"google-docs-tabs": None} and "unavailable" in out["why"]["google-docs-tabs"]


def test_icon_route(client, ws):
    status, headers, body = client.get("/api/bot-apps/starters/google-docs-tabs/icon")
    assert status == 200 and headers.get("Content-Type", "").startswith("image/svg+xml")
    with open(os.path.join(starters.STARTERS_DIR, "google-docs-tabs", "icon.svg"), "rb") as f:
        assert body == f.read()
    status, _, _ = client.get("/api/bot-apps/starters/myspace/icon")
    assert status == 404


def test_icon_route_refuses_path_traversal(client, ws):
    for key in ("..", "%2E%2E", "..%2F..%2Fpyproject.toml", "..%2Fpresets", "google-docs-tabs%2F..%2F..%2F..%2Fpyproject.toml"):
        status, _, body = client.get(f"/api/bot-apps/starters/{key}/icon")
        assert status == 404, (key, status, body[:80])
    status, _, _ = client.get("/api/bot-apps/starters/../presets/icon")
    assert status == 404
