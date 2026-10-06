"""`/api/bots/*` and `/api/bot-apps/*` through the real server (the `client`
fixture): create/list/settings/flag/react/routines/skills/export/attach/
clone/delete, the image routes, the agent-engine delegations and the apps
gallery. No model call happens: `Bot.greet` and `Bot.start_task` are stubbed."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import base64
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile

import pytest

from fused_render.bots import apptools
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths
from fused_render.bots import registry

MARKED = '<!doctype html><html><head><meta name="fused-app" /><title>Price watch</title></head><body></body></html>'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A tmp fused workspace (apps root = <ws>/app), no greeting model call."""
    w = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(w))
    os.makedirs(bpaths.apps_root())
    monkeypatch.setattr(apptools, "ROOTS", [bpaths.apps_root()])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    return w


def j(resp):
    status, headers, body = resp
    return status, json.loads(body or b"{}")


def delete(client, path, headers=None):
    h = {"X-Fused": "1", **(headers or {})}
    return client._do(urllib.request.Request(client.base + path, headers=h, method="DELETE"))  # noqa: S310


def make(client, **body):
    st, out = j(client.post("/api/bots", {"name": "Scout", **body}))
    assert st == 200 and out["ok"], out
    return out["id"]


def status(client, **q):
    qs = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in q.items()})
    st, out = j(client.get("/api/bots" + (f"?{qs}" if qs else "")))
    assert st == 200, out
    return out


def one(out, bid):
    return next(b for b in out["bots"] if b["id"] == bid)


def test_create_list_and_cursor(client, ws):
    bid = make(client, model="haiku", effort="medium", instructions=" check the news ", approval="auto", build_access="full")
    out = status(client)
    assert set(out) == {"bots", "ts", "usage", "imessage", "channels"}
    assert out["imessage"] is None and out["channels"] is None  # the channels router is not started under tests
    assert isinstance(out["usage"], dict)
    s = one(out, bid)
    assert s["name"] == "Scout" and s["model"] == "haiku" and s["effort"] == "medium" and s["status"] == "idle"
    assert s["instructions"] == "check the news" and s["approval"] == "auto" and s["build_access"] == "full"
    assert s["seq"] == 1 and [e["text"] for e in s["events"]] == ["Scout created."]
    assert s["browser"]["running"] is False and s["shot"] is None and s["viewport"] == [1280, 800]
    assert s["memory"] is None and s["skills"] is None  # detail only
    # the cursor is the line count the page last saw
    assert one(status(client, cursors={bid: 1}), bid)["events"] == []
    # fast polls skip usage and iMessage
    fast = status(client, fast=1)
    assert fast["usage"] is None and fast["imessage"] is None
    # the selected bot reports detail
    d = one(status(client, shot_for=bid), bid)
    assert d["memory"] == "" and d["skills"] == [] and d["browser"]["files"] == [] and d["browser"]["artifacts"] == []
    assert d["browser"]["artifacts_dir"].endswith(os.path.join("bots", "scout"))


def test_create_rejects_unknown_model_quietly_and_settings_loudly(client, ws):
    bid = make(client, model="gpt-9")
    assert one(status(client), bid)["model"] == botmod.DEFAULT_MODEL
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"model": "gpt-9"}))
    assert st == 400 and "unknown model" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"effort": "max"}))
    assert st == 400 and "unknown effort" in out["error"]


def test_settings_flag_react(client, ws):
    bid = make(client)
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"name": "Ranger", "model": "opus", "effort": "high",
                                                          "instructions": "be brief", "memory": "- likes tables",
                                                          "approval": "auto", "build_access": "full",
                                                          "imessage_to": "Ali +15551234567", "engine": "steps"}))
    assert st == 200 and out == {"ok": True}
    d = one(status(client, shot_for=bid), bid)
    assert (d["name"], d["model"], d["effort"], d["instructions"]) == ("Ranger", "opus", "high", "be brief")
    assert d["memory"].strip() == "- likes tables" and d["approval"] == "auto" and d["engine"] == "steps"
    assert d["imessage_to"] == "Ali +15551234567"
    st, out = j(client.post(f"/api/bots/{bid}/flag", {"pinned": True, "hidden": False, "face": {"shape": "blob", "color": "#f00"}}))
    assert st == 200
    s = one(status(client), bid)
    assert s["pinned"] is True and s["hidden"] is False and s["face"] == {"shape": "blob", "color": "#f00", "icon": ""}
    st, out = j(client.post(f"/api/bots/{bid}/react", {"seq": 1, "emoji": "👍"}))
    assert st == 200 and out["reactions"] == {"1": "👍"}
    st, out = j(client.post(f"/api/bots/{bid}/react", {"seq": 1, "emoji": ""}))
    assert out["reactions"] == {}


def test_routines(client, ws):
    bid = make(client)
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "check prices", "kind": "interval", "minutes": 2}))
    assert st == 200
    r = out["routine"]
    assert r["minutes"] == 5 and r["enabled"] is True and r["next"] > r["created"]  # min 5
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "morning digest", "kind": "daily",
                                                          "time": "07:30", "weekdays": [0, 2, 4]}))
    daily = out["routine"]
    assert daily["time"] == "07:30" and daily["weekdays"] == [0, 2, 4]
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "x", "kind": "once", "at": 1}))
    assert st == 400 and "past" in out["error"]
    assert j(client.post(f"/api/bots/{bid}/routines", {"op": "disable", "rid": r["id"]}))[0] == 200
    assert next(x for x in one(status(client), bid)["routines"] if x["id"] == r["id"])["enabled"] is False
    assert j(client.post(f"/api/bots/{bid}/routines", {"op": "enable", "rid": r["id"]}))[0] == 200
    assert next(x for x in one(status(client), bid)["routines"] if x["id"] == r["id"])["enabled"] is True
    assert j(client.post(f"/api/bots/{bid}/routines", {"op": "delete", "rid": r["id"]}))[0] == 200
    assert [x["id"] for x in one(status(client), bid)["routines"]] == [daily["id"]]
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "delete", "rid": "nope"}))
    assert st == 400 and out["error"] == "no such routine"


def test_skills_save_and_delete(client, ws):
    bid = make(client)
    st, out = j(client.post(f"/api/bots/{bid}/skills", {"op": "save", "name": "LinkedIn feed", "trigger": "linkedin feed, linkedin posts",
                                                        "text": "1. goto https://www.linkedin.com/feed\n2. read"}))
    assert st == 200
    assert out["skills"] == [{"name": "linkedin-feed", "title": "LinkedIn feed", "trigger": "linkedin feed, linkedin posts",
                              "body": "1. goto https://www.linkedin.com/feed\n2. read"}]
    st, out = j(client.post(f"/api/bots/{bid}/skills", {"op": "save", "name": "x", "trigger": "", "text": "steps"}))
    assert st == 400 and "trigger" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/skills", {"op": "learn"}))
    assert st == 400 and "no finished task" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/skills", {"op": "delete", "rid": "linkedin-feed"}))
    assert st == 200 and out["skills"] == []


def test_send_starts_a_task(client, ws, monkeypatch):
    started = []
    monkeypatch.setattr(botmod.Bot, "start_task", lambda self, task, label=None, origin="manual", via=None: started.append((task, label)))
    bid = make(client)
    st, out = j(client.post(f"/api/bots/{bid}/send", {"text": "   "}))
    assert st == 400 and out["error"] == "empty message"
    st, out = j(client.post(f"/api/bots/{bid}/send", {"text": "find me a laptop", "reply_to": 1}))
    assert st == 200 and out == {"ok": True}
    assert started[0][1] == "find me a laptop" and started[0][0].startswith("Replying to your earlier message:\n> Scout created.")
    ev = one(status(client, cursors={bid: 1}), bid)["events"]
    assert ev[0]["role"] == "user" and ev[0]["reply"] == {"seq": 1, "role": "system", "text": "Scout created."}


def test_control_routes_on_an_idle_bot(client, ws):
    bid = make(client)
    for op in ("pause", "resume", "stop"):
        assert j(client.post(f"/api/bots/{bid}/{op}", {})) == (200, {"ok": True})
    st, out = j(client.post(f"/api/bots/{bid}/explode", {}))
    assert st == 404
    st, out = j(client.post(f"/api/bots/{bid}/nav", {"op": "sideways"}))
    assert st == 400 and "back|forward|reload" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/tab", {"tab": "flip"}))
    assert st == 400
    st, out = j(client.post("/api/bots/nobody/pause", {}))
    assert st == 404 and "no such bot" in out["error"]


def test_export(client, ws):
    bid = make(client)
    st, out = j(client.get(f"/api/bots/{bid}/export"))
    assert st == 200 and out["ok"] and out["name"] == "Scout-transcript.md"
    assert "Scout" in out["text"] and "Scout created." in out["text"]


def test_attach_caps_at_8_mb(client, ws):
    bid = make(client)
    st, out = j(client.post(f"/api/bots/{bid}/attach", {"name": "cv.pdf", "data": base64.b64encode(b"%PDF-1.4 hi").decode()}))
    assert st == 200 and out == {"ok": True, "name": "cv.pdf"}
    assert open(os.path.join(bpaths.bot_dir(bid), "files", "cv.pdf"), "rb").read() == b"%PDF-1.4 hi"
    st, out = j(client.post(f"/api/bots/{bid}/attach", {"name": "cv.pdf", "data": base64.b64encode(b"again").decode()}))
    assert out["name"] == "cv-2.pdf"  # never overwrites
    big = base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode()
    st, out = j(client.post(f"/api/bots/{bid}/attach", {"name": "big.bin", "data": big}))
    assert st == 400 and "too large" in out["error"]
    files = one(status(client, shot_for=bid), bid)["browser"]["files"]
    assert sorted(f["name"] for f in files) == ["cv-2.pdf", "cv.pdf"] and all(f["kind"] == "saved" for f in files)


def test_shot_and_step_thumbs(client, ws):
    bid = make(client)
    st, _, body = client.get(f"/api/bots/{bid}/shot")
    assert st == 404 and json.loads(body)["error"]
    b = registry.get(bid)
    os.makedirs(os.path.dirname(b.browser.shot_path), exist_ok=True)
    with open(b.browser.shot_path, "wb") as f:
        f.write(b"\x89PNG fake")
    st, headers, body = client.get(f"/api/bots/{bid}/shot?t=1")
    assert st == 200 and body == b"\x89PNG fake"
    assert headers["Content-Type"].startswith("image/png") and headers["Cache-Control"] == "no-cache"
    assert one(status(client), bid)["shot"] == f"/api/bots/{bid}/shot"
    os.makedirs(b.steps_dir, exist_ok=True)
    with open(os.path.join(b.steps_dir, "3.jpg"), "wb") as f:
        f.write(b"\xff\xd8jpeg")
    st, headers, body = client.get(f"/api/bots/{bid}/steps/3.jpg")
    assert st == 200 and body == b"\xff\xd8jpeg" and headers["Content-Type"].startswith("image/jpeg")
    assert client.get(f"/api/bots/{bid}/steps/4.jpg")[0] == 404
    assert client.get(f"/api/bots/{bid}/steps/..%2Fbot.json")[0] == 404


def test_agent_engine_routes_refuse_a_stale_token(client, ws):
    bid = make(client)
    st, out = j(client.get(f"/api/bots/{bid}/tools?token=nope"))
    assert st == 409 and out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/tool", {"name": "observe", "args": {}, "token": "nope"}))
    assert st == 409 and out["error"]


def test_clone_copies_settings_memory_and_skills(client, ws):
    bid = make(client, instructions="watch prices")
    client.post(f"/api/bots/{bid}/settings", {"memory": "- site X needs login"})
    client.post(f"/api/bots/{bid}/skills", {"op": "save", "name": "Prices", "trigger": "prices", "text": "1. goto x"})
    st, out = j(client.post(f"/api/bots/{bid}/clone", {"name": "Scout 2"}))
    assert st == 200 and out["ok"] and out["id"] != bid
    c = one(status(client, shot_for=out["id"]), out["id"])
    assert c["name"] == "Scout 2" and c["instructions"] == "watch prices"
    assert "site X needs login" in c["memory"] and [s["name"] for s in c["skills"]] == ["prices"]
    assert any("logins and cookies" in e["text"] for e in c["events"])


def test_delete_removes_the_folders(client, ws):
    bid = make(client)
    b = registry.get(bid)
    os.makedirs(b.cache_dir, exist_ok=True)
    assert os.path.isdir(bpaths.bot_dir(bid))
    st, out = j(delete(client, f"/api/bots/{bid}"))
    assert st == 200 and out == {"ok": True}
    assert not os.path.exists(bpaths.bot_dir(bid)) and not os.path.exists(bpaths.bot_cache_dir(bid))
    assert all(x["id"] != bid for x in status(client)["bots"])
    assert j(delete(client, f"/api/bots/{bid}"))[0] == 404


def test_builds_json(client, ws):
    assert j(client.get("/api/bots/builds")) == (200, {"builds": []})
    rows = [{"entryId": "e1", "name": "Price watch", "dir": "/x", "createdAt": 1}]
    assert j(client.post("/api/bots/builds", {"builds": rows})) == (200, {"ok": True})
    assert j(client.get("/api/bots/builds"))[1]["builds"] == rows
    assert j(client.post("/api/bots/builds", {"builds": "nope"}))[0] == 400


def test_profiles_and_usage(client, ws):
    st, out = j(client.get("/api/bots/profiles"))
    assert st == 200 and out == {"ok": True, "profiles": []}  # HOME is an empty tmp tree under tests
    st, out = j(client.get("/api/bots/usage"))
    assert st == 200 and "today" in out


def test_writes_need_x_fused(client, ws):
    no = {"X-Fused": ""}
    assert client.post("/api/bots", {"name": "x"}, headers=no)[0] == 403
    bid = make(client)
    for path, body in ((f"/api/bots/{bid}/send", {"text": "hi"}), (f"/api/bots/{bid}/pause", {}),
                       (f"/api/bots/{bid}/settings", {"name": "y"}), ("/api/bots/builds", {"builds": []}),
                       ("/api/bot-apps/mkdir", {"dir": "/tmp/x"}), ("/api/bot-apps/import", {"name": "a", "data": ""})):
        assert client.post(path, body, headers=no)[0] == 403, path
    assert delete(client, f"/api/bots/{bid}", headers=no)[0] == 403
    assert os.path.isdir(bpaths.bot_dir(bid))


def test_botsend_queues_a_task_the_scheduler_tick_runs(client, ws, monkeypatch, capsys):
    from fused_render.bots import botsend
    started = []
    monkeypatch.setattr(botmod.Bot, "start_task", lambda self, task, label=None, origin="manual", via=None: started.append(task))
    bid = make(client)
    assert botsend.main(["--list"]) == 0 and "Scout" in capsys.readouterr().out
    assert botsend.main(["nobody", "x"]) == 1
    assert botsend.main(["scout", "check", "the", "news"]) == 0
    inbox = os.path.join(bpaths.bot_dir(bid), "inbox")
    assert [n.endswith(".txt") for n in os.listdir(inbox)] == [True]
    registry.get(bid).tick_routines()  # what the 20 s scheduler does
    assert started == ["check the news"] and os.listdir(inbox) == []
    texts = [e["text"] for e in one(status(client), bid)["events"]]
    assert any(t.startswith("Task received from botsend") for t in texts) and "check the news" in texts


# ------------------------------------------------------------------- apps ---
def test_apps_list_icon_import_mkdir(client, ws):
    root = bpaths.apps_root()
    d = os.path.join(root, "price-watch")
    os.makedirs(d)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write(MARKED)
    with open(os.path.join(d, "icon.svg"), "w") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg"/>')
    os.makedirs(os.path.join(root, "not-an-app"))  # no marked index.html
    st, out = j(client.get("/api/bot-apps"))
    assert st == 200 and out["root"] == root
    assert [a["folder"] for a in out["apps"]] == ["price-watch"]
    a = out["apps"][0]
    assert a["name"] == "Price watch" and a["icon"] == "icon.svg" and a["mtime"] > 0
    st, headers, body = client.get("/api/bot-apps/icon?" + urllib.parse.urlencode({"dir": d}))
    assert st == 200 and headers["Content-Type"].startswith("image/svg+xml") and body.startswith(b"<svg")
    assert client.get("/api/bot-apps/icon?" + urllib.parse.urlencode({"dir": os.path.join(root, "not-an-app")}))[0] == 404
    assert client.get("/api/bot-apps/icon?" + urllib.parse.urlencode({"dir": "/etc"}))[0] == 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("budget/index.html", MARKED.replace("Price watch", "Budget"))
        zf.writestr("budget/calc.py", "def main():\n    return 1\n")
    st, out = j(client.post("/api/bot-apps/import", {"name": "budget.zip", "data": base64.b64encode(buf.getvalue()).decode()}))
    assert st == 200, out
    assert out["fusedApp"] is True and out["folder"] == "budget" and os.path.isfile(os.path.join(out["dir"], "calc.py"))
    st, out = j(client.post("/api/bot-apps/import", {"name": "x.zip", "data": base64.b64encode(b"not a zip").decode()}))
    assert st == 400

    st, out = j(client.post("/api/bot-apps/mkdir", {"dir": os.path.join(root, "new-build")}))
    assert st == 200 and out["existed"] is False and os.path.isdir(os.path.join(root, "new-build"))
    st, out = j(client.post("/api/bot-apps/mkdir", {"dir": os.path.join(str(ws), "elsewhere")}))
    assert st == 400 and "under" in out["error"]


def test_settings_trusted_apps_round_trip_and_clone(client, ws):
    bid = make(client)
    st, _ = j(client.post(f"/api/bots/{bid}/settings", {"name": "Ranger", "trusted_apps": ["Fake_Docs", "fake docs", "", "mail"]}))
    assert st == 200
    assert one(status(client), bid)["trusted_apps"] == ["fake-docs", "mail"]
    # a non-list leaves it alone; an empty list clears it
    client.post(f"/api/bots/{bid}/settings", {"name": "Ranger", "trusted_apps": "mail"})
    assert one(status(client), bid)["trusted_apps"] == ["fake-docs", "mail"]
    st, out = j(client.post(f"/api/bots/{bid}/clone", {"name": "Copy"}))
    assert st == 200
    assert one(status(client), out["id"])["trusted_apps"] == ["fake-docs", "mail"]
    client.post(f"/api/bots/{bid}/settings", {"name": "Ranger", "trusted_apps": []})
    assert one(status(client), bid)["trusted_apps"] == []
