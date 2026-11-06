"""The workspace claim gates bot engines (registry module docstring, PR #2057 review round 2): a home whose owner
does not hold `<bots_root>/.routines.lock` refuses every door that starts an engine (WorkspaceBusy -> 409), never
runs the takeover walk, and the Super Bot seed waits while legacy bots remain."""
from _bots_conftest import *  # noqa: F401,F403 — app_home (autouse), client, …
import json
import os
import shutil

import pytest

from fused_render.bots import apptools, handoffs, migrate_v3, registry, store
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths

fcntl = pytest.importorskip("fcntl")


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


@pytest.fixture
def ws(monkeypatch):
    """No greeting model call and no engine: start_task only records."""
    os.makedirs(bpaths.apps_root(), exist_ok=True)
    monkeypatch.setattr(apptools, "ROOTS", [bpaths.apps_root()])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    started = []
    monkeypatch.setattr(botmod.Bot, "start_task",
                        lambda self, task, label=None, origin="manual", via=None, images=None: started.append(task))
    return started


@pytest.fixture
def walks(monkeypatch):
    """_become_owner / _pass with every step quiet except the takeover walk, which is counted."""
    n = []
    for name in ("_start_router", "_import_fusedbot", "sync_wake", "_migrate_v3", "_seed_super", "_sweep_browsers",
                 "_sweep_handoffs"):
        monkeypatch.setattr(registry, name, lambda: None)
    monkeypatch.setattr(bpaths, "migrate_layout", lambda: None)
    monkeypatch.setattr(registry, "_reconcile", lambda: n.append(1))
    monkeypatch.setattr(botmod.Bot, "tick_routines", lambda self: None)
    return n


class OtherHome:
    """Another home's owner on the same workspace: it holds `<bots_root>/.routines.lock`."""

    def __enter__(self):
        registry._drop_workspace_claim()  # a lazy claim this process won on an earlier touch
        self.fh = open(os.path.join(bpaths.bots_root(), registry.WS_LOCK), "a+", encoding="utf-8")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def release(self):
        if self.fh is not None:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None

    def __exit__(self, *exc):
        self.release()


def _make(client):
    st, out = j(client.post("/api/bots", {"name": "Scout"}))
    assert st == 200, out
    return out["id"]


def test_require_workspace_raises_while_another_home_holds_the_claim():
    registry.require_owner()
    with OtherHome():
        assert registry.workspace_claimed() is False
        with pytest.raises(registry.WorkspaceBusy) as e:
            registry.require_workspace()
        assert isinstance(e.value, ValueError)
        assert str(e.value) == (f"another fused-render instance owns the bots in this workspace ({bpaths.bots_root()}); "
                                "use that window, quit it, or take them over here")
    assert registry.workspace_claimed() is True  # retried and won once the other home let go
    registry.require_workspace()


def test_doors_refuse_without_the_claim_and_open_after_the_pass(client, ws, walks):
    started = ws
    bid = _make(client)
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "check", "kind": "interval",
                                                          "minutes": 60}))
    assert st == 200
    rid = out["routine"]["id"]
    msg = f"another fused-render instance owns the bots in this workspace ({bpaths.bots_root()}); use that window, quit it, or take them over here"
    other = OtherHome().__enter__()
    try:
        registry._pass()
        registry._pass()
        assert walks == []  # no takeover walk without the claim: the claimant's running tasks are not ours
        st, out = j(client.post(f"/api/bots/{bid}/send", {"text": "find me a laptop"}))
        assert (st, out["error"], out["reason"]) == (409, msg, "workspace_busy")  # + `holder` for the page's Override
        st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "check", "kind": "interval",
                                                              "minutes": 60}))
        assert (st, out["error"]) == (409, msg)  # read-only for bot state: the claimant's next save would revert it
        assert j(client.post(f"/api/bots/{bid}/routines", {"op": "run", "rid": rid}))[0] == 409
        assert j(client.post("/api/bots", {"name": "Second"}))[0] == 409
        assert j(client.post(f"/api/bots/{bid}/open", {})) == (200, {"ok": True, "setup": "pending"})
        assert j(client.get("/api/bots"))[0] == 200  # reads are never gated
        assert started == []
        other.release()
        # The claim is won by the next request, but the doors stay shut until a pass ran the workspace steps.
        assert j(client.post(f"/api/bots/{bid}/send", {"text": "find me a laptop"}))[0] == 409
        registry._pass()
        assert walks == [1]
        assert j(client.post(f"/api/bots/{bid}/send", {"text": "find me a laptop"})) == (200, {"ok": True})
        assert started == ["find me a laptop"]
        registry._pass()
        assert walks == [1]  # once per claim
    finally:
        other.release()


def test_first_ownership_without_the_claim_skips_the_walk_then_runs_it_once(walks):
    registry.require_owner()
    with OtherHome():
        registry._become_owner()
        assert walks == []
    registry._become_owner()
    assert walks == [1]
    registry._become_owner()
    assert walks == [1]


def test_handoff_sweep_does_nothing_without_the_claim(monkeypatch):
    seen = []

    def probe():
        seen.append(1)
        raise RuntimeError("stop here")  # sweep never raises

    monkeypatch.setattr(handoffs, "_botmod", probe)
    registry.require_owner()
    with OtherHome():
        handoffs.sweep(registry)
        assert seen == []
    handoffs.sweep(registry)
    assert seen == [1]


def _supers():
    return [b for b in store.list_ids() if botmod.is_super(store.read_meta(b))]


def _stamp():
    os.makedirs(os.path.dirname(migrate_v3.stamp_path()), exist_ok=True)
    with open(migrate_v3.stamp_path(), "w", encoding="utf-8") as f:
        json.dump({"at": 1.0}, f)


def test_seed_waits_while_a_legacy_bot_remains():
    _stamp()
    legacy = os.path.join(bpaths.legacy_data_dir(), "a1a2a3a4")
    os.makedirs(legacy)
    with open(os.path.join(legacy, store.BOT_FILE), "w", encoding="utf-8") as f:
        json.dump({"id": "a1a2a3a4", "name": "Super Bot", "kind": "super", "created": 1.0}, f)
    registry._seed_super()
    assert not os.path.exists(registry.seed_stamp_path()) and _supers() == []
    shutil.rmtree(legacy)
    registry._seed_super()
    assert os.path.exists(registry.seed_stamp_path()) and len(_supers()) == 1
    registry._seed_super()
    assert len(_supers()) == 1


def test_seed_waits_for_a_parked_bot_but_never_for_strays():
    _stamp()
    um = os.path.join(bpaths.bots_root(), migrate_v3.UNMIGRATED)
    # Strays (every real machine parks cache/bots/browsers and the like) and an empty dir never block the seed.
    os.makedirs(os.path.join(um, "stray-20261101-120000", "cache", "browsers"))
    with open(os.path.join(um, "stray-20261101-120000", "cache", "note.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    os.makedirs(os.path.join(um, "c1c2c3c4"))  # empty: nothing parked in it
    parked = os.path.join(um, "b1b2b3b4")
    os.makedirs(parked)
    with open(os.path.join(parked, store.BOT_FILE), "w", encoding="utf-8") as f:
        f.write("{not json")  # an unplaceable bot is parked as-is
    assert registry._legacy_bots_remain() is True
    registry._seed_super()
    assert not os.path.exists(registry.seed_stamp_path()) and _supers() == []
    shutil.rmtree(parked)
    assert registry._legacy_bots_remain() is False
    registry._seed_super()
    assert len(_supers()) == 1


class _Chan:
    kind = "fake"

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def door(self):
        return ("s1s2s3s4", "+1")

    def send(self, addr, text, event=None):
        if self.fail:
            raise RuntimeError("no send")
        self.sent.append((addr, text))


class _Bot:
    id, meta = "s1s2s3s4", {}

    def __init__(self):
        self.received = []

    def receive(self, text, via=None, **kw):
        self.received.append(text)


class _Reg:
    def __init__(self, bot, claimed):
        self.bot, self.claimed = bot, claimed

    def get(self, bid):
        return self.bot

    def workspace_claimed(self):
        return self.claimed[0]


def test_channel_inbound_is_refused_and_answered_without_the_claim(caplog):
    from fused_render.bots.channels import router as rmod
    from fused_render.bots.channels.base import Inbound
    b, claimed = _Bot(), [False]
    r = rmod.Router(_Reg(b, claimed))
    ch = _Chan()
    caplog.set_level("INFO", logger=rmod.__name__)
    r.dispatch(ch, Inbound(addr="+1", text="find me a laptop"))
    r.dispatch(ch, Inbound(addr="+1", text="again"))
    assert b.received == []
    assert ch.sent == [("+1", rmod.BUSY_REPLY)] * 2
    assert rmod.BUSY_REPLY == "another fused-render instance owns the bots in this workspace"
    assert len([x for x in caplog.records if "refused" in x.getMessage()]) == 1  # logged once per streak
    mute = _Chan(fail=True)  # a channel that cannot reply: the refusal still holds, nothing raises
    r.dispatch(mute, Inbound(addr="+1", text="hi"))
    assert b.received == []
    r.dispatch(ch, Inbound(addr="+2", text="not the door"))  # a stranger is dropped before any reply
    assert len(ch.sent) == 2
    claimed[0] = True
    r.dispatch(ch, Inbound(addr="+1", text="find me a laptop"))
    assert b.received == ["find me a laptop"] and len(ch.sent) == 2


def test_router_uses_the_real_registry_gate():
    from fused_render.bots.channels import router as rmod
    from fused_render.bots.channels.base import Inbound
    b = _Bot()

    class Reg:
        get = staticmethod(lambda bid: b)
        workspace_claimed = staticmethod(registry.workspace_claimed)

    r, ch = rmod.Router(Reg()), _Chan()
    registry.require_owner()
    with OtherHome():
        r.dispatch(ch, Inbound(addr="+1", text="hi"))
        assert b.received == [] and ch.sent == [("+1", rmod.BUSY_REPLY)]
    r.dispatch(ch, Inbound(addr="+1", text="hi"))
    assert b.received == ["hi"]


# ------------------------------------------------ read-only without the claim (CodeRabbit round 3) ---
def _files(bid):
    """Every byte of the bot's state a read-only home must never touch."""
    out = {}
    for name, path in (("bot.json", store.meta_path(bid)), ("state.json", store.state_path(bid)),
                       ("events.jsonl", store.events_path(bid))):
        try:
            with open(path, "rb") as f:
                out[name] = f.read()
        except FileNotFoundError:
            out[name] = None
    return out


def _stale(client):
    """A bot as the claimant leaves it mid-task: running, an open hand-off, a hand-over with a held message, an
    id-less routine — everything the load-time reconcile would rewrite."""
    bid = _make(client)
    meta = store.read_meta(bid)
    meta.update(status="running", task="find a laptop", control=True, control_by="user", control_since=1.0,
                held=[{"seq": 1, "text": "and a mouse"}],
                handoffs=[{"id": "h1h1h1h1", "target": "t1t1t1t1", "target_name": "Other", "task": "look",
                           "state": "running", "created_at": 1.0}],
                routines=[{"task": "check", "kind": "interval", "minutes": 60, "enabled": True, "created": 1.0,
                           "last": None, "last_result": ""}])
    store.write_meta(bid, meta)
    registry.forget(bid)  # the next get() builds it from disk
    return bid


def test_unclaimed_load_leaves_the_bot_files_byte_identical(client, ws, walks):
    bid = _stale(client)
    before = _files(bid)
    assert before["bot.json"] and before["state.json"]
    with OtherHome():
        b = registry.get(bid)
        assert b.read_only is True
        assert b.meta["status"] == "running" and b.meta["control"] is True and b.meta.get("held")
        assert b.meta["handoffs"][0]["state"] == "running" and not b.meta["handoffs"][0].get("done_at")
        rid = b.meta["routines"][0]["id"]
        assert rid  # a transient id, in memory only
        assert registry.get(bid) is b and registry.all() == [b]
        st, out = j(client.get("/api/bots"))
        assert st == 200 and [x["id"] for x in out["bots"]] == [bid]
        assert j(client.get("/api/bots/pulse"))[0] == 200
        assert j(client.get(f"/api/bots/{bid}/package"))[0] == 200  # the export plan reads
        assert _files(bid) == before
        assert "id" not in store.read_meta(bid)["routines"][0]
    # Claim won: the read-only Bot is rebuilt from disk and reconciles once, as a fresh load would.
    b2 = registry.get(bid)
    assert b2 is not b and b2.read_only is False
    assert b2.meta["status"] == "idle" and b2.meta["handoffs"][0]["state"] == "failed"
    assert b not in b2.browser.proc.views
    disk = store.read_meta(bid)
    assert disk["status"] == "idle" and disk["routines"][0]["id"]


def _gated(bid, rid, brid):
    b64 = "aGk="
    p = f"/api/bots/{bid}"
    return [
        ("post", f"{p}/routines", {"op": "add", "text": "x", "kind": "interval", "minutes": 60}),
        ("post", f"{p}/routines", {"op": "edit", "rid": rid, "text": "y", "kind": "interval", "minutes": 30}),
        ("post", f"{p}/routines", {"op": "enable", "rid": rid}),
        ("post", f"{p}/routines", {"op": "disable", "rid": rid}),
        ("post", f"{p}/routines", {"op": "delete", "rid": rid}),
        ("post", f"{p}/routines", {"op": "run", "rid": rid}),
        ("post", f"{p}/settings", {"name": "Renamed"}),
        ("post", f"{p}/profile", {"profile": "Default"}),
        ("post", f"{p}/flag", {"pinned": True}),
        ("post", f"{p}/react", {"seq": 1, "emoji": "+1"}),
        ("post", f"{p}/seen", {"seq": 99}),
        ("post", f"{p}/attach", {"name": "a.txt", "data": b64}),
        ("post", f"{p}/skills", {"op": "save", "name": "S", "trigger": "t", "text": "do it"}),
        ("post", f"{p}/skills", {"op": "delete", "rid": "s"}),
        ("post", f"{p}/skills", {"op": "learn"}),
        ("post", f"{p}/goto", {"url": "https://example.com"}),
        ("post", f"{p}/nav", {"op": "reload"}),
        ("post", f"{p}/tab", {"tab": "new"}),
        ("post", f"{p}/viewport", {"w": 800, "h": 600}),
        ("post", f"{p}/takeover", {}),
        ("post", f"{p}/giveback", {}),
        ("post", f"{p}/popout", {}),
        ("post", f"{p}/wake", {}),
        ("post", f"{p}/clear", {}),
        ("post", f"{p}/clone", {"name": "Copy"}),
        ("post", f"{p}/send", {"text": "hi"}),
        ("post", f"/api/bots/browsers/{brid}", {"op": "encrypt", "on": True}),
        ("post", f"/api/bots/browsers/{brid}", {"op": "delete"}),
        ("delete", p, None),
        ("post", "/api/botfile/install", {"path": "/nowhere/x.fusedbot", "apps": [], "bot_id": bid}),
    ]


def test_every_bot_write_refuses_without_the_claim_and_works_after(client, ws, walks):
    bid = _stale(client)
    registry.get(bid)  # reconciled under the claim: the routine has its id on disk now
    rid = store.read_meta(bid)["routines"][0]["id"]
    brid = store.read_meta(bid)["browser_id"]
    registry.forget(bid)
    before = _files(bid)
    ids_before = store.list_ids()
    msg = f"another fused-render instance owns the bots in this workspace ({bpaths.bots_root()}); use that window, quit it, or take them over here"
    other = OtherHome().__enter__()
    try:
        registry._pass()  # a started owner without the claim
        for method, path, body in _gated(bid, rid, brid):
            resp = client.delete(path) if method == "delete" else client.post(path, body)
            st, out = j(resp)
            assert (st, out.get("error")) == (409, msg), (path, body, st, out)
            assert _files(bid) == before, (path, body)
        assert store.list_ids() == ids_before
        # Reads, the dock and the export plan stay open.
        assert j(client.get("/api/bots"))[0] == 200
        assert j(client.get(f"/api/bots/{bid}/package"))[0] == 200
        assert j(client.get(f"/api/bots/{bid}/export"))[0] == 200
        assert j(client.post(f"/api/bots/{bid}/open", {})) == (200, {"ok": True, "setup": "pending"})
        assert _files(bid) == before
        other.release()
        registry._pass()  # wins the claim and runs the workspace steps
        assert j(client.post(f"/api/bots/{bid}/flag", {"pinned": True})) == (200, {"ok": True})
        assert store.read_meta(bid)["pinned"] is True
        st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "disable", "rid": rid}))
        assert st == 200, out
        assert store.read_meta(bid)["routines"][0]["enabled"] is False
    finally:
        other.release()
