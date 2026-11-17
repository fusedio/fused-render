"""The `expert` bot kind (docs/experts.md §5): create through the registry and POST /api/bots, no browser,
clone and browser ops refused, the summary's expert fields, the shipped-expert seed, export/import keeps
the kind. No model, no network, no Chrome."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os

import pytest

from fused_render.bots import bot as botmod
from fused_render.bots import browser as browser_mod
from fused_render.bots import browsers
from fused_render.bots import registry
from fused_render.bots import store as bstore
from fused_render.experts import model as xmodel
from fused_render.experts import seed as xseed
from fused_render.experts import store as xstore

MANIFEST = {"ask_me_about": "Canvases", "reads": [], "offered_to": {"targets": ["canvas"]}, "shipped": ""}


@pytest.fixture(autouse=True)
def _no_chrome(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an expert must never start Chrome")
    monkeypatch.setattr(browser_mod.Browser, "start", boom)
    monkeypatch.setattr(browser_mod.BrowserProcess, "start", boom)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    monkeypatch.setattr(xmodel, "available", lambda bot: (True, ""))


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


def _events(b):
    return [ev for _, ev in bstore.iter_events(b.events_path)]


def test_registry_create_makes_an_expert_without_a_browser():
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST), browser="whatever", preset="linkedin")
    assert b.meta["kind"] == "expert" and botmod.is_expert(b.meta)
    assert "browser_id" not in b.meta and "preset" not in b.meta
    assert not browsers.exists(b.id)  # no browser folder made
    assert xstore.manifest(b)["ask_me_about"] == "Canvases"
    pub = json.load(open(os.path.join(b.public_dir, "bot.json")))
    assert pub["kind"] == "expert" and pub["expert"]["ask_me_about"] == "Canvases"
    evs = _events(b)
    greet = [e for e in evs if e["role"] == "done"]
    assert len(greet) == 1 and greet[0]["source"] == "seed" and "Canvases".lower() in greet[0]["text"].lower()
    # reload from disk keeps the kind and still no browser
    registry.reset_for_tests()
    b2 = registry.get(b.id)
    assert botmod.is_expert(b2.meta) and "browser_id" not in b2.meta and not browsers.exists(b.id)


def test_unknown_kind_still_coerces_to_a_bot():
    b = botmod.create("Plain", kind="robot", greet=False)
    assert "kind" not in b.meta or b.meta["kind"] == "bot"
    assert botmod.KINDS == ("bot", "super", "expert")


def test_clone_refused():
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST))
    with pytest.raises(ValueError, match="expert cannot be cloned"):
        registry.clone(b.id)


def test_browser_entry_points_and_idle_sleep():
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST))
    for fn in (b.takeover, b.popout, b.wake_browser):
        with pytest.raises(ValueError, match="no browser"):
            fn()
    assert b.idle_sleep_due(False) is False
    b.idle_sleep()  # a no-op
    assert botmod._engine_for(b.meta) == "expert"


def test_summary_carries_expert_fields(monkeypatch):
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST))
    s = b.summary(detail=True)
    assert s["kind"] == "expert" and s["expert"]["ask_me_about"] == "Canvases"
    assert s["expert"]["memory"]["propose_at"] == 3  # defaults filled
    assert s["expert_available"] is True and s["expert_reason"] == ""
    assert s["browser"]["running"] is False and s["memory"] == "" and s["skills"] == []
    monkeypatch.setattr(xmodel, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    s = b.summary(light=True)
    assert s["expert_available"] is False and s["expert_reason"] == "needs Claude, Codex or Copilot"


def test_super_bot_handoffs_and_bots_section_skip_experts():
    sb = botmod.create(kind="super", greet=False)
    registry.create("Ref", kind="expert", expert=dict(MANIFEST))
    botmod.create("Scout", greet=False)
    sec = botmod.bots_section(sb)
    assert "Scout" in sec and "Ref" not in sec
    t, err = sb._handoff_target("Ref")
    assert t is None and "Ref" not in err.split("BOTS:")[-1]


def test_delete_an_expert():
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST))
    registry.delete(b.id)
    assert b.id not in bstore.list_ids()


# ------------------------------------------------------------------ routes ---
def test_create_and_browser_ops_through_routes(client):
    st, out = j(client.post("/api/bots", {"name": "Ref", "kind": "expert", "expert": MANIFEST, "engine": "steps",
                                          "encrypt": True, "browser_id": "nope"}))
    assert st == 200, out
    bid = out["id"]
    st, rows = j(client.get("/api/bots"))
    row = next(r for r in rows["bots"] if r["id"] == bid)
    assert row["kind"] == "expert" and row["expert"]["ask_me_about"] == "Canvases" and "engine" not in row
    for op, body in (("takeover", {}), ("popout", {}), ("wake", {}), ("goto", {"url": "https://x.test"}),
                     ("nav", {"op": "back"}), ("tab", {"tab": "new"}), ("viewport", {"w": 800, "h": 600})):
        st, out = j(client.post(f"/api/bots/{bid}/{op}", body))
        assert st == 400 and "expert" in out["error"] and "no browser" in out["error"], (op, out)
    st, out = j(client.post(f"/api/bots/{bid}/clone", {}))
    assert st == 400 and "cannot be cloned" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"name": "Ref 2", "engine": "steps", "encrypt": True,
                                                          "browser_id": "other"}))
    assert st == 200, out
    b = registry.get(bid)
    assert b.meta["name"] == "Ref 2" and "engine" not in b.meta and "browser_id" not in b.meta
    st, out = j(client.post("/api/bots", {"name": "x", "kind": "robot"}))
    assert st == 400 and "kind must be" in out["error"]
    st, roster = j(client.get("/api/bots/roster"))
    assert next(r for r in roster["bots"] if r["id"] == bid)["kind"] == "expert"


# ------------------------------------------------------------------ seed ---
def _shipped(tmp_path, monkeypatch):
    d = tmp_path / "shipped" / "demo"
    (d / "skills").mkdir(parents=True)
    (d / "expert.json").write_text(json.dumps({"name": "Demo Expert", "manifest": {"ask_me_about": "Demos"},
                                               "face": {"shape": "hexagon", "color": "#2a9a86", "icon": ""}}))
    (d / "instructions.md").write_text("Be precise.\n")
    (d / "skills" / "layout.md").write_text("# Folder layout\ntrigger: layout, folder\n\nThe body.\n")
    (d / "questions.md").write_text("## Q?\n- must: a point\n")
    monkeypatch.setattr(xseed, "SHIPPED_DIR", str(tmp_path / "shipped"))


def test_seed_creates_once_and_not_after_delete(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch)
    ids = xseed.seed()
    assert len(ids) == 1
    b = registry.get(ids[0])
    assert botmod.is_expert(b.meta) and b.meta["name"] == "Demo Expert"
    assert b.meta["instructions"] == "Be precise."
    assert xstore.manifest(b)["shipped"] == "demo" and xstore.manifest(b)["ask_me_about"] == "Demos"
    assert b.meta["face"]["shape"] == "hexagon"
    sk = b.skills()
    assert [s["name"] for s in sk] == ["layout"] and sk[0]["title"] == "Folder layout" and sk[0]["trigger"] == "layout, folder"
    assert not os.path.exists(os.path.join(b.public_dir, "questions.md"))  # no longer seeded
    assert xseed.seed() == []
    registry.delete(b.id)
    assert xseed.seed() == []  # the witness keeps a deleted expert deleted
    assert json.load(open(xseed.witness_path()))["keys"]["demo"]["id"] == b.id


def test_seed_tolerates_a_missing_shipped_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(xseed, "SHIPPED_DIR", str(tmp_path / "nope"))
    assert xseed.seed() == []


def test_registry_seed_step_never_raises(monkeypatch):
    monkeypatch.setattr(xseed, "seed", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    registry._seed_experts()  # logs, does not raise


def test_real_shipped_expert_seeds():
    ids = xseed.seed()
    keys = xseed.shipped_keys()
    assert len(ids) == len(keys)
    for bid in ids:
        assert botmod.is_expert(registry.get(bid).meta)


# ------------------------------------------------------------------ export / import ---
def test_export_import_keeps_the_kind(tmp_path, monkeypatch):
    from fused_render.bots import botfile, package
    b = registry.create("Ref", kind="expert", expert=dict(MANIFEST), instructions="Answer briefly.")
    b.skill_save("Layout", "layout", "body text", name="layout")
    path = package.write(b.id, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    ws2 = tmp_path / "ws2"
    (ws2 / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws2))
    reset_bots_caches()  # noqa: F405
    out = botfile.install(path, apps=[], memory=False)
    nb = registry.get(out["bot_id"])
    assert botmod.is_expert(nb.meta) and "browser_id" not in nb.meta and not browsers.exists(nb.id)
    assert xstore.manifest(nb)["ask_me_about"] == "Canvases"
    assert [s["name"] for s in nb.skills()] == ["layout"]
    assert [e for e in _events(nb) if e["role"] == "done" and e.get("source") == "seed"]
