"""The launcher: hotkey.py spec handling, launcher.py search/settings/registry,
GET /api/launcher and the launcher keys of PUT /api/prefs.

The AppKit half (launcher_panel.py, the Carbon half of hotkey.py) never
imports in CI; everything it acts on is pinned here.
"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from fused_render import hotkey, launcher
from fused_render.server import create_app

# A macOS-only feature: the registry answers canonical (forward-slash) paths
# and the expectations below compare them with `str(tmp_path)` — backslashed
# on Windows, where the panel never exists anyway.
pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="macOS-only launcher; POSIX path expectations")

FUSED = {"X-Fused": "1"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    (tmp_path / "Fused").mkdir()
    monkeypatch.setattr(launcher, "native_hooks", {})
    launcher.invalidate()
    return home


def _app(tmp_path, name: str, title: str | None = None) -> str:
    folder = tmp_path / "Fused" / "local" / name
    folder.mkdir(parents=True)
    head = f"<title>{title}</title>" if title else ""
    (folder / "index.html").write_text(
        f'<!doctype html><html><head><meta name="fused-app">{head}</head><body></body></html>')
    return str(folder)


# ---- hotkey specs ------------------------------------------------------------


def test_parse_spec_default():
    keycode, flags, names, key = hotkey.parse_spec("alt+space")
    assert keycode == 0x31 and flags == hotkey.ALT and names == {"alt"} and key == "space"


def test_parse_spec_accepts_browser_codes_and_aliases():
    keycode, flags, names, key = hotkey.parse_spec("Option+Shift+KeyA")
    assert keycode == 0x00 and flags == hotkey.ALT | hotkey.SHIFT and key == "a"
    assert hotkey.parse_spec("cmd+Digit1")[3] == "1"
    assert hotkey.parse_spec("ctrl+F5")[0] == 0x60


@pytest.mark.parametrize("bad", ["", "space", "alt", "alt+", "hyper+space", "alt+nosuchkey", "+"])
def test_parse_spec_rejects(bad):
    with pytest.raises(hotkey.SpecError):
        hotkey.parse_spec(bad)


def test_canonical_orders_modifiers():
    assert hotkey.canonical("shift+cmd+alt+ctrl+KeyK") == "ctrl+alt+shift+cmd+k"
    assert hotkey.canonical("ALT+SPACE") == "alt+space"


def test_display():
    assert hotkey.display("alt+space") == "⌥Space"
    assert hotkey.display("cmd+shift+k") == "⇧⌘K"
    assert hotkey.display("ctrl+slash") == "⌃/"
    assert hotkey.display("garbage") == "garbage"


# ---- settings (prefs.json) ---------------------------------------------------


def test_settings_defaults(home):
    s = launcher.settings()
    assert s["hotkey"] == "alt+space" and s["display"] == "⌥Space"
    assert s["row_modifier"] == "alt" and s["row_modifier_display"] == "⌥"
    assert s["bound"] is None and s["pinned_bound"] is None


def test_settings_corrupt_values_read_as_defaults(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "prefs.json").write_text(json.dumps(
        {"launcher_hotkey": "nonsense", "launcher_row_modifier": "space"}))
    assert launcher.get_hotkey() == hotkey.DEFAULT_SPEC
    assert launcher.get_row_modifier() == launcher.DEFAULT_ROW_MODIFIER


def test_canonical_modifiers():
    assert launcher.canonical_modifiers("Command") == "cmd"
    assert launcher.canonical_modifiers("cmd+alt") == "alt+cmd"
    for bad in ("", "space", "alt+space"):
        with pytest.raises(hotkey.SpecError):
            launcher.canonical_modifiers(bad)


def test_pinned_specs_and_home_spec():
    assert launcher.home_spec("alt") == "alt+0" and launcher.home_spec("") is None
    assert hotkey.parse_spec(launcher.home_spec("alt"))[0] == 0x1D
    assert launcher.pinned_specs("") == []
    assert launcher.pinned_specs("alt") == [f"alt+{n}" for n in range(1, 10)]
    assert all(hotkey.parse_spec(s) for s in launcher.pinned_specs("ctrl+alt"))


# ---- search ------------------------------------------------------------------


def rows(*names, pinned=(), recent=()):
    return [{"path": f"/x/{n}", "name": n, "title": n, "pinned": n in pinned,
             "recent": n in recent} for n in names]


def test_empty_query_lists_recent_then_pinned_in_order():
    r = rows("A", "B", "C", "D", "E", pinned=("C", "A"), recent=("D", "A"))
    assert [x["name"] for x in launcher.search("", r)] == ["A", "C", "D"]
    assert [x["name"] for x in launcher.search("   ", r)] == ["A", "C", "D"]


def test_search_ranks_prefix_then_word_then_substring_then_subsequence():
    r = rows("Photos", "Screen Shot", "Amphetamine", "OpenSVG", "Nothing")
    got = [x["name"] for x in launcher.search("s", r)]
    assert got[0] == "Screen Shot"
    assert set(got) == {"Screen Shot", "Photos", "OpenSVG"}
    got = [x["name"] for x in launcher.search("os", r)]
    assert got == ["Photos", "OpenSVG"]  # substring beats subsequence
    assert launcher.search("zz", r) == []


def test_search_is_case_insensitive_and_uses_title():
    r = [{"path": "/x/a", "name": "05_OpenWhisper", "title": "Open Whisper", "pinned": False}]
    assert launcher.search("WHISPER", r) == r
    assert launcher.search("open w", r) == r


def test_search_ties_keep_registry_order_and_limit():
    r = rows(*[f"App{i}" for i in range(12)])
    got = launcher.search("app", r)
    assert [x["name"] for x in got] == [f"App{i}" for i in range(launcher.MAX_RESULTS)]


# ---- registry ----------------------------------------------------------------


def test_registry_desk_first_newest_first_then_workspace(home, tmp_path):
    from fused_render import current_apps

    a = _app(tmp_path, "alpha", "Alpha One")
    b = _app(tmp_path, "beta")
    _app(tmp_path, "gamma", "Gamma")
    current_apps.add(a)
    current_apps.add(b)
    launcher.invalidate()
    reg = launcher.registry(running={b})
    paths = [r["path"] for r in reg]
    assert paths[:2] == [b, a]  # nothing opened yet: desk, newest-added first
    assert not reg[0]["recent"] and not reg[1]["recent"]
    assert reg[0]["pinned"] and reg[0]["running"] and reg[1]["pinned"] and not reg[1]["running"]
    assert reg[1]["title"] == "Alpha One" and reg[1]["url"].startswith("/apps/")
    assert len(paths) == len(set(paths))
    gamma = [r for r in reg if r["name"] == "gamma"]
    assert gamma and not gamma[0]["pinned"] and gamma[0]["title"] == "Gamma"
    assert launcher.nth_pinned(1)["path"] == b and launcher.nth_pinned(2)["path"] == a
    assert launcher.nth_pinned(1)["kind"] == "app"
    assert launcher.nth_pinned(3) is None and launcher.nth_pinned(0) is None


# ---- routes ------------------------------------------------------------------


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path / "Fused")))


def test_launcher_search_route(home, tmp_path):
    from fused_render import current_apps

    a = _app(tmp_path, "zeta", "Zeta")
    current_apps.add(a)
    launcher.invalidate()
    client = _client(tmp_path)
    body = client.get("/api/launcher").json()
    assert body["query"] == "" and [x["path"] for x in body["apps"]] == [a, ""]
    assert body["apps"][-1]["home"] is True and body["apps"][-1]["url"] == "/"
    body = client.get("/api/launcher?q=zet").json()
    assert [x["name"] for x in body["apps"]] == ["zeta", "Fused Render"]
    body = client.get("/api/launcher?q=qqqq").json()
    assert [x.get("home") for x in body["apps"]] == [True]
    # The page is a Vite entry (shell-dist/launcher.html): served when built,
    # an explicit 503 when the frontend has not been built in this checkout.
    page = client.get("/launcher")
    assert page.status_code in (200, 503)
    if page.status_code == 503:
        assert "launcher page not built" in page.text


def test_prefs_launcher_keys(home, tmp_path):
    calls = []
    launcher.native_hooks.update({"rebind": calls.append, "hotkey_bound": lambda: True})
    client = _client(tmp_path)
    got = client.get("/api/prefs").json()["launcher"]
    assert got["hotkey"] == "alt+space" and got["bound"] is True and got["pinned_bound"] is None
    r = client.put("/api/prefs", json={"launcher_hotkey": "cmd+shift+KeyL"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["launcher"]["hotkey"] == "shift+cmd+l" and calls == ["shift+cmd+l"]
    r = client.put("/api/prefs", json={"launcher_hotkey": "KeyL"}, headers=FUSED)
    assert r.status_code == 400 and "modifier" in r.json()["error"]
    assert launcher.get_hotkey() == "shift+cmd+l"
    # row modifier: stored canonically; the hook is told with None (no rebind)
    r = client.put("/api/prefs", json={"launcher_row_modifier": "cmd+alt"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["launcher"]["row_modifier"] == "alt+cmd"
    assert r.json()["launcher"]["row_modifier_display"] == "⌥⌘"
    assert calls == ["shift+cmd+l", None]
    r = client.put("/api/prefs", json={"launcher_row_modifier": "space"}, headers=FUSED)
    assert r.status_code == 400 and launcher.get_row_modifier() == "alt+cmd"
    # unguarded (no X-Fused header) is refused
    assert client.put("/api/prefs", json={"launcher_hotkey": "alt+space"}).status_code == 403
    stored = json.loads((home / "prefs.json").read_text())
    assert stored["launcher_hotkey"] == "shift+cmd+l" and stored["launcher_row_modifier"] == "alt+cmd"
    assert os.path.isfile(home / "prefs.json")


# ---- Fused Bot flavor --------------------------------------------------------


@pytest.fixture
def bot(monkeypatch):
    from fused_render import _flavor

    monkeypatch.setattr(_flavor, "_CACHED", "bot")


def _prefs_file(home):
    return home / "prefs.json"


def _bot(bid, name, *, pinned=False, updated=0.0, status="idle"):
    return {"kind": "bot", "id": bid, "name": name, "face": {"emoji": "x"}, "status": status,
            "running": status != "idle", "updated": updated, "pinned": pinned}


def test_bot_flavor_keys_and_defaults(home, bot):
    assert launcher.hotkey_key() == "bot_launcher_hotkey"
    assert launcher.row_modifier_key() == "bot_launcher_row_modifier"
    assert launcher.default_hotkey() == "alt+shift+space" and launcher.default_row_modifier() == "alt+shift"
    home.mkdir(parents=True, exist_ok=True)
    _prefs_file(home).write_text(json.dumps({"launcher_hotkey": "cmd+shift+KeyL",
                                             "launcher_row_modifier": "cmd"}))
    s = launcher.settings()
    assert s["kind"] == "bots" and s["hotkey"] == "alt+shift+space" and s["row_modifier"] == "alt+shift"
    _prefs_file(home).write_text(json.dumps({"bot_launcher_hotkey": "cmd+shift+KeyL",
                                             "bot_launcher_row_modifier": "cmd"}))
    s = launcher.settings()
    assert s["hotkey"] == "shift+cmd+l" and s["row_modifier"] == "cmd"


def test_render_flavor_ignores_bot_keys(home):
    assert launcher.hotkey_key() == "launcher_hotkey" and launcher.row_modifier_key() == "launcher_row_modifier"
    home.mkdir(parents=True, exist_ok=True)
    _prefs_file(home).write_text(json.dumps({"bot_launcher_hotkey": "cmd+shift+KeyL",
                                             "bot_launcher_row_modifier": "cmd"}))
    s = launcher.settings()
    assert s["kind"] == "apps" and s["hotkey"] == hotkey.DEFAULT_SPEC and s["row_modifier"] == "alt"


def test_bot_registry_orders_pinned_then_recent(home, bot, monkeypatch):
    from fused_render import dock

    monkeypatch.setattr(dock, "_bot_rows", lambda: [
        _bot("old", "Old", updated=1), _bot("zed", "zed", pinned=True),
        _bot("new", "New", updated=9, status="busy"), _bot("alp", "Alpha", pinned=True)])
    launcher.invalidate()
    reg = launcher.registry()
    assert [r["id"] for r in reg] == ["alp", "zed", "new", "old"]  # pinned by name casefold, then updated desc
    assert [r["pinned"] for r in reg] == [True, True, False, False]
    assert [r["recent"] for r in reg] == [False, False, True, True]
    new = reg[2]
    assert new["kind"] == "bot" and new["path"] == "new" and new["url"] == "/bots?bot=new"
    assert new["name"] == new["title"] == "New" and new["icon"] is None
    assert new["face"] == {"emoji": "x"} and new["status"] == "busy" and new["running"] is True
    assert [r["id"] for r in launcher.search("", reg)] == ["alp", "zed", "new", "old"]
    assert launcher.nth_pinned(1)["id"] == "alp" and launcher.nth_pinned(3)["id"] == "new"
    assert launcher.nth_pinned(5) is None


def test_prefs_launcher_keys_store_per_flavor(home, tmp_path, bot):
    home.mkdir(parents=True, exist_ok=True)
    _prefs_file(home).write_text(json.dumps({"launcher_hotkey": "alt+space"}))
    client = _client(tmp_path)
    r = client.put("/api/prefs", json={"launcher_hotkey": "cmd+shift+KeyL",
                                       "launcher_row_modifier": "cmd"}, headers=FUSED)
    assert r.status_code == 200
    stored = json.loads(_prefs_file(home).read_text())
    assert stored["bot_launcher_hotkey"] == "shift+cmd+l" and stored["bot_launcher_row_modifier"] == "cmd"
    assert stored["launcher_hotkey"] == "alt+space" and "launcher_row_modifier" not in stored
    assert r.json()["launcher"]["hotkey"] == "shift+cmd+l"


def test_api_launcher_bot_has_no_files(home, tmp_path, bot, monkeypatch):
    from fused_render import dock

    monkeypatch.setattr(dock, "_bot_rows", lambda: [_bot("a", "Alpha", pinned=True)])
    launcher.invalidate()
    body = _client(tmp_path).get("/api/launcher?q=alp").json()
    assert body["files"] == [] and body["files_reason"] == ""
    assert [x["id"] for x in body["apps"] if x.get("kind") == "bot"] == ["a"]
