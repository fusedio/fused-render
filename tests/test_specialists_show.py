"""A specialist showing its work in its main chat (docs/specialists.md §15): the canvas server, its two tools,
and the card line a `show` answers with."""
from _specialists_conftest import *  # noqa: F401,F403
import json

import pytest

from fused_render.specialists import chat, launch, show, store

TOML = ('type = "canvas"\nversion = 2\nname = "{name}"\n\n[canvas]\nedges = []\n\n[[canvas.nodes]]\n'
        'udfName = "udf"\nx = 0.0\ny = 0.0\n')


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(store, "live_runs", lambda home: [])


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A canvases base with one environment, the `fused` wrapper and the Canvases feature on, and the three
    canvases calls `canvas_create` makes answered without a Fused (the clone writes the folder a pull would)."""
    from fused_render import canvases, fusedcli
    from fused_render.shell import prefs
    root = tmp_path / "canvas"
    (root / "prod" / "user").mkdir(parents=True)
    monkeypatch.setattr(canvases, "canvases_base", lambda: str(root))
    monkeypatch.setenv(fusedcli.CLI_DIR_ENV, str(tmp_path / "fused-bin"))
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: True)
    calls = []

    def clone(body, x_fused=None):
        calls.append(("clone", body["env"], body["name"]))
        d = root / body["env"] / "user" / body["name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "canvas.toml").write_text(TOML.format(name=body["name"]))
        (d / "udf.py").write_text("@fused.udf\ndef udf():\n    return 1\n")
        return {"ok": True, "dir": str(d)}
    monkeypatch.setattr(canvases, "api_canvases_create",
                        lambda body, x_fused=None: calls.append(("create", body["env"], body["name"])) or {"ok": True})
    monkeypatch.setattr(canvases, "api_canvases_clone", clone)
    monkeypatch.setattr(canvases, "_sync_start_response",
                        lambda env, name: calls.append(("start", env, name)) or {"ok": True})
    return root, calls


def _text(res):
    return res["content"][0]["text"]


def test_only_the_main_chat_gets_the_canvas_server(base, monkeypatch):
    from fused_render.shell import prefs
    s = make_specialist()
    main = launch.spec(f"{s.id}:main", s.home)
    assert set(main["mcp_servers"]) == {"specialist_services", show.SERVER}
    assert main["mcp_servers"][show.SERVER]["args"][4:] == [f"api/specialists/{s.id}/{show.SERVER}", show.SERVER]
    assert "mcp__specialist_canvas__show" in main["append"] and show.SKILL in main["append"]
    assert main["ask"] == ["mcp__specialist_canvas__canvas_create"]  # each new canvas is the user's to approve
    for kind in ("breakout", "update"):
        sp = launch.spec(f"{s.id}:{kind}", s.home)
        assert set(sp["mcp_servers"]) == {"specialist_services"} and "canvas" not in sp["append"]
        assert "ask" not in sp
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: False)
    off = launch.spec(f"{s.id}:main", s.home)
    assert set(off["mcp_servers"]) == {"specialist_services"} and off["append"] == launch.APPEND["main"]
    assert "ask" not in off
    assert _text(show.call(s, "show", {"path": "/x"})).startswith("error: canvases are not available")


def test_the_skill_names_the_tools_as_the_session_sees_them():
    text = open(show.SKILL, encoding="utf-8").read()
    for name in show.TOOLS:
        assert f"mcp__{show.SERVER}__{name}" in text


def test_create_then_show_a_node_and_the_canvas(base):
    base, calls = base
    s = make_specialist()
    made = show.call(s, "canvas_create", {"name": "Parcels"})
    folder = base / "prod" / "user" / "Parcels"
    assert not made["isError"] and str(folder) in _text(made)
    assert calls == [("create", "prod", "Parcels"), ("clone", "prod", "Parcels"), ("start", "prod", "Parcels")]
    assert [(r["env"], r["name"]) for r in show.made(s.home)] == [("prod", "Parcels")]
    assert chat.load(s.home)["main"] is None  # the chat's own state is untouched

    again = show.call(s, "canvas_create", {"name": "Parcels"})  # its own canvas: the folder back, nothing made
    assert not again["isError"] and len(show.made(s.home)) == 1
    assert [c[0] for c in calls] == ["create", "clone", "start", "start"]  # only its sync is started again

    node = show.call(s, "show", {"path": str(folder / "udf.py"), "note": "  Parcels\nby county "})
    assert not node["isError"]
    line = _text(node).splitlines()[-1]
    assert line.startswith(show.CARD)
    assert json.loads(line[len(show.CARD):]) == {"kind": "udf", "path": str(folder / "udf.py"), "title": "udf",
                                                 "canvas": "Parcels", "env": "prod", "note": "Parcels by county"}
    whole = show.call(s, "show", {"path": str(folder / "canvas.toml")})
    card = json.loads(_text(whole).splitlines()[-1][len(show.CARD):])
    assert card["kind"] == "canvas" and card["path"] == str(folder) and card["note"] == ""


def test_what_is_refused(base, tmp_path):
    base, calls = base
    s = make_specialist()
    (base / "prod" / "user" / "Theirs").mkdir()
    taken = show.call(s, "canvas_create", {"name": "Theirs"})  # the user's own canvas: never taken over
    assert taken["isError"] and "already has a canvas" in _text(taken) and calls == []
    assert show.call(s, "canvas_create", {"name": "bad name"})["isError"]
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n")
    for path in (str(loose), str(tmp_path / "missing.py"), str(tmp_path), ""):
        res = show.call(s, "show", {"path": path})
        assert res["isError"] and show.CARD not in _text(res)
    assert show.call(s, "nope", {})["isError"]


def test_the_environment_is_the_user_s_default_unless_named(base, monkeypatch):
    from fused_render import fusedcli
    base, calls = base
    s = make_specialist()
    monkeypatch.setattr(fusedcli, "workbench_env", lambda: "unstable")  # the Preferences choice
    assert show.envs()[1] == "unstable"
    assert not show.call(s, "canvas_create", {"name": "A"})["isError"]
    assert calls[0] == ("create", "unstable", "A")
    assert not show.call(s, "canvas_create", {"name": "B", "env": "prod"})["isError"]  # the user named one
    assert ("create", "prod", "B") in calls
    res = show.call(s, "canvas_create", {"name": "C", "env": "staging"})
    assert res["isError"] and "one of" in _text(res)


def test_the_session_door_serves_the_canvas_server(client, base):
    s = make_specialist()
    token = launch.new_token(s.id)
    door = f"/api/specialists/{s.id}/{show.SERVER}"
    status, _, raw = client.get(f"{door}/tools?token={token}")
    assert status == 200 and [t["name"] for t in json.loads(raw)["tools"]] == ["canvas_create", "show"]
    call = {"name": "show", "args": {"path": "/nowhere/x.py"}, "token": token}
    status, _, raw = client.post(f"{door}/tool", call)
    assert status == 200 and json.loads(raw)["isError"]
    assert client.post(f"{door}/tool", {**call, "token": "x"})[0] == 403
    assert client.get(f"/api/specialists/{s.id}/other/tools?token={token}")[0] == 400
