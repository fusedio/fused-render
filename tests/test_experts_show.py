"""An expert showing its work in its main chat (docs/experts.md §14): the canvas server, its two tools,
and the card line a `show` answers with."""
from _experts_conftest import *  # noqa: F401,F403
import json
import threading

import pytest

from fused_render.experts import chat, launch, show, store

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
    watched = set()  # the canvases a watcher runs on
    monkeypatch.setattr(canvases, "_sync_manager", lambda env, name, create: (env, name) in watched or None)
    monkeypatch.setattr(canvases, "_sync_start_response",
                        lambda env, name: (calls.append(("start", env, name)), watched.add((env, name))) and {"ok": 1})
    return root, calls


def _text(res):
    return res["content"][0]["text"]


def test_only_the_main_chat_gets_the_canvas_server(base, monkeypatch):
    from fused_render.shell import prefs
    s = make_expert()
    main = launch.spec(f"{s.id}:main", s.home)
    assert set(main["mcp_servers"]) == {"expert_services", show.SERVER}
    assert main["mcp_servers"][show.SERVER]["args"][4:] == [f"api/experts/{s.id}/{show.SERVER}", show.SERVER]
    assert "mcp__expert_canvas__show" in main["append"] and show.SKILL in main["append"]
    assert main["ask"] == ["mcp__expert_canvas__canvas_create"]  # each new canvas is the user's to approve
    for kind in ("breakout", "update"):
        sp = launch.spec(f"{s.id}:{kind}", s.home)
        assert set(sp["mcp_servers"]) == {"expert_services"} and "canvas" not in sp["append"]
        assert "ask" not in sp
    monkeypatch.setattr(prefs, "canvases_enabled", lambda: False)
    off = launch.spec(f"{s.id}:main", s.home)
    assert set(off["mcp_servers"]) == {"expert_services"} and off["append"] == launch.APPEND["main"]
    assert "ask" not in off
    assert _text(show.call(s, "show", {"path": "/x"})).startswith("error: canvases are not available")


def test_a_main_chat_is_handed_the_workbench_skills(base, monkeypatch):
    from fused_render import skill_plugin
    s = make_expert()
    fetched = []
    monkeypatch.setattr(skill_plugin, "workbench_plugin_root", lambda: None)
    monkeypatch.setattr(skill_plugin, "sync_workbench_plugin", lambda: fetched.append(1) and None)
    off = launch.spec(f"{s.id}:main", s.home)  # never fetched and the fetch fails: the tools still come
    assert fetched == [1] and "workbench_skills" not in off and "workbench:" not in off["append"]
    assert show.SERVER in off["mcp_servers"]
    monkeypatch.setattr(skill_plugin, "workbench_plugin_root", lambda: "/plugins/workbench")
    on = launch.spec(f"{s.id}:main", s.home)
    assert on["workbench_skills"] is True and fetched == [1]  # on disk already: no fetch on the start path
    assert "`workbench:json-ui-schemas` for any `.json` widget" in on["append"]
    assert "workbench_skills" not in launch.spec(f"{s.id}:breakout", s.home)


def test_the_skill_names_the_tools_as_the_session_sees_them():
    text = open(show.SKILL, encoding="utf-8").read()
    for name in show.TOOLS:
        assert f"mcp__{show.SERVER}__{name}" in text


def test_create_then_show_a_node_and_the_canvas(base):
    base, calls = base
    s = make_expert()
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


def test_showing_a_canvas_made_without_the_tool_starts_its_sync_and_pushes(base, monkeypatch):
    from fused_render import canvases
    base, calls = base
    pushed = threading.Event()
    monkeypatch.setattr(canvases, "api_canvases_sync_push",
                        lambda body, x_fused=None: (calls.append(("push", body["env"], body["name"])), pushed.set()))
    folder = base / "prod" / "user" / "By_hand"  # as the `fused` CLI and its own writes leave it: no watcher
    folder.mkdir()
    (folder / "canvas.toml").write_text(TOML.format(name="By_hand"))
    (folder / "udf.py").write_text("@fused.udf\ndef udf():\n    return 1\n")
    s = make_expert()
    assert not show.call(s, "show", {"path": str(folder / "udf.py")})["isError"]
    assert pushed.wait(10) and calls == [("start", "prod", "By_hand"), ("push", "prod", "By_hand")]
    assert not show.call(s, "show", {"path": str(folder)})["isError"]  # now watched: nothing more
    assert len(calls) == 2


def test_what_is_refused(base, tmp_path):
    base, calls = base
    s = make_expert()
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
    s = make_expert()
    monkeypatch.setattr(fusedcli, "workbench_env", lambda: "unstable")  # the Preferences choice
    assert show.envs()[1] == "unstable"
    create = show.mcp_tools()[0]  # the card the user approves shows the environment: the expert must give it
    assert create["inputSchema"]["required"] == ["name", "env"]
    assert "`unstable`, the user's default" in create["inputSchema"]["properties"]["env"]["description"]
    assert not show.call(s, "canvas_create", {"name": "A"})["isError"]
    assert calls[0] == ("create", "unstable", "A")
    assert not show.call(s, "canvas_create", {"name": "B", "env": "prod"})["isError"]  # the user named one
    assert ("create", "prod", "B") in calls
    res = show.call(s, "canvas_create", {"name": "C", "env": "staging"})
    assert res["isError"] and "one of" in _text(res)


def test_the_session_door_serves_the_canvas_server(client, base):
    s = make_expert()
    token = launch.new_token(s.id)
    door = f"/api/experts/{s.id}/{show.SERVER}"
    status, _, raw = client.get(f"{door}/tools?token={token}")
    assert status == 200 and [t["name"] for t in json.loads(raw)["tools"]] == ["canvas_create", "show"]
    call = {"name": "show", "args": {"path": "/nowhere/x.py"}, "token": token}
    status, _, raw = client.post(f"{door}/tool", call)
    assert status == 200 and json.loads(raw)["isError"]
    assert client.post(f"{door}/tool", {**call, "token": "x"})[0] == 403
    assert client.get(f"/api/experts/{s.id}/other/tools?token={token}")[0] == 400
