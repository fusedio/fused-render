"""Shared browsers (docs/bots.md §5): a bot's logins are a browser folder that
several bots may name. Through the real server: create with `browser_id`,
the Logins switch in settings, clone share/copy, `/api/bots/browsers` and its
ops, delete keeping a browser another bot still names. No Chrome is started
(nothing navigates)."""
from _bots_conftest import *  # noqa: F401,F403
import json
import os
import urllib.parse
import urllib.request

import pytest

from fused_render.bots import apptools, browsers, registry
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A tmp fused workspace (apps root = <ws>/app), no greeting model call (as test_bots_routes)."""
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


def delete(client, path):
    return client._do(urllib.request.Request(client.base + path, headers={"X-Fused": "1"}, method="DELETE"))  # noqa: S310


def make(client, **body):
    st, out = j(client.post("/api/bots", {"name": "Scout", **body}))
    assert st == 200 and out["ok"], out
    return out["id"]


def row(client, bid):
    st, out = j(client.get("/api/bots"))
    assert st == 200
    return next(b for b in out["bots"] if b["id"] == bid)


def test_new_bot_gets_its_own_browser_and_can_share_another(client, ws):
    a = make(client, name="Alpha")
    ra = row(client, a)
    assert ra["browser_id"] == a and ra["shared_with"] == [] and ra["browser_name"] == "Alpha"
    assert os.path.isfile(browsers.meta_path(a)) and not os.path.exists(os.path.join(bpaths.bot_dir(a), "profile"))
    b = make(client, name="Beta", browser_id=a)
    rb, ra = row(client, b), row(client, a)
    assert rb["browser_id"] == a and rb["shared_with"] == [{"id": a, "name": "Alpha"}]
    assert ra["shared_with"] == [{"id": b, "name": "Beta"}] and ra["browser"]["shared"] is True
    assert not os.path.exists(browsers.meta_path(b))  # no folder of its own
    st, out = j(client.get("/api/bots/browsers"))
    assert st == 200 and sorted(x["name"] for x in [r for r in out["browsers"] if r["id"] == a][0]["bots"]) == ["Alpha", "Beta"]
    assert j(client.post("/api/bots", {"name": "X", "browser_id": "nope"}))[0] == 400


def test_settings_logins_switch_both_ways(client, ws):
    a, b = make(client, name="Alpha"), make(client, name="Beta")
    assert j(client.post(f"/api/bots/{b}/settings", {"name": "Beta", "browser_id": a}))[0] == 200
    assert row(client, b)["browser_id"] == a and not browsers.exists(b)  # its old, unused browser is gone
    # "this bot only" again: a fresh browser of its own; Alpha keeps the shared one
    assert j(client.post(f"/api/bots/{b}/settings", {"name": "Beta", "browser_id": ""}))[0] == 200
    assert row(client, b)["browser_id"] == b and browsers.exists(b) and browsers.exists(a)
    assert row(client, a)["shared_with"] == []
    # the OWNER of a browser others joined asks for its own: it gets a NEW one, the others keep the old
    c = make(client, name="Gamma", browser_id=a)
    assert j(client.post(f"/api/bots/{a}/settings", {"name": "Alpha", "browser_id": ""}))[0] == 200
    ra = row(client, a)
    assert ra["browser_id"] not in (a, c) and ra["shared_with"] == [] and row(client, c)["browser_id"] == a
    # a switch and an encrypt flag in the same save: the flag the dialog showed was the old browser's, so it is not applied
    assert j(client.post(f"/api/bots/{c}/settings", {"name": "Gamma", "browser_id": ra["browser_id"], "encrypt": True}))[0] == 200
    assert row(client, c)["encrypt"] is False


def test_clone_shares_by_default_and_copies_on_request(client, ws):
    a = make(client, name="Alpha")
    st, out = j(client.post(f"/api/bots/{a}/clone", {"name": "Twin"}))
    assert st == 200 and row(client, out["id"])["browser_id"] == a
    st, out = j(client.post(f"/api/bots/{a}/clone", {"name": "Copy", "share": False}))
    assert st == 200 and row(client, out["id"])["browser_id"] == out["id"] and browsers.exists(out["id"])


def test_delete_keeps_a_browser_another_bot_names(client, ws):
    a = make(client, name="Alpha")
    b = make(client, name="Beta", browser_id=a)
    assert j(delete(client, f"/api/bots/{a}"))[0] == 200
    assert browsers.exists(a) and row(client, b)["browser_id"] == a and row(client, b)["shared_with"] == []
    assert j(delete(client, f"/api/bots/{b}"))[0] == 200
    assert not browsers.exists(a)


def test_browser_ops(client, ws):
    a = make(client, name="Alpha")
    b = make(client, name="Beta", browser_id=a)
    st, out = j(client.post(f"/api/bots/browsers/{a}", {"op": "rename", "name": "  Work  "}))
    assert (st, out) == (200, {"ok": True, "name": "Work"})
    assert row(client, b)["browser_name"] == "Work"
    assert [r for r in j(client.get("/api/bots/browsers"))[1]["browsers"] if r["id"] == a][0]["name"] == "Work"
    assert j(client.post(f"/api/bots/browsers/{a}", {"op": "rename", "name": ""}))[0] == 400
    assert j(client.post(f"/api/bots/browsers/{a}", {"op": "encrypt", "on": True}))[0] == 200
    assert row(client, a)["encrypt"] is True and row(client, b)["encrypt"] is True and browsers.read_meta(a)["encrypt"]
    assert j(client.post(f"/api/bots/browsers/{a}", {"op": "encrypt", "on": False}))[0] == 200
    assert j(client.post(f"/api/bots/browsers/{a}", {"op": "bogus"}))[0] == 400
    assert j(client.post("/api/bots/browsers/nope", {"op": "rename", "name": "x"}))[0] == 400
    # delete: both bots get a fresh browser of their own, the old folder goes
    assert j(client.post(f"/api/bots/browsers/{a}", {"op": "delete"}))[0] == 200
    ra, rb = row(client, a), row(client, b)
    assert a != ra["browser_id"] != rb["browser_id"] != a and ra["shared_with"] == [] and rb["shared_with"] == []
    assert browsers.exists(ra["browser_id"]) and browsers.exists(rb["browser_id"]) and not browsers.exists(a)


def test_old_profile_folder_is_adopted(client, ws):
    a = make(client, name="Old")
    registry.forget(a)
    # pretend the bot predates browsers: profile under the bot folder, no browser_id
    meta_p = os.path.join(bpaths.bot_dir(a), "bot.json")
    with open(meta_p) as f:
        meta = json.load(f)
    meta.pop("browser_id", None)
    with open(meta_p, "w") as f:
        json.dump(meta, f)
    browsers.remove(a)
    os.makedirs(os.path.join(bpaths.bot_dir(a), "profile", "Default"))
    with open(os.path.join(bpaths.bot_dir(a), "profile", "Default", "Cookies"), "w") as f:
        f.write("x")
    r = row(client, a)
    assert r["browser_id"] == a
    assert os.path.isfile(os.path.join(browsers.browser_dir(a), "profile", "Default", "Cookies"))
    assert not os.path.exists(os.path.join(bpaths.bot_dir(a), "profile"))
