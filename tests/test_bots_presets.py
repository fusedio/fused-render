"""Bot presets (port of OpenBot tests/test_presets.py): the catalog under
fused_render/bots/presets/<key>/ (preset.json + playbook .md files),
applying one to a fresh bot, the content-quality rules every shipped playbook
must keep, and the HTTP side (`GET /api/bots/presets`, `POST /api/bots` with
`preset`). No model call happens: `Bot.greet` is stubbed."""
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

ALL_KEYS = ("linkedin", "youtube", "x", "reddit", "instagram", "facebook", "tiktok",
            "gmail", "calendar", "slack", "github", "hackernews", "amazon", "news", "indeed", "maps", "notion",
            "linkme", "shopmy", "ltk", "associates", "twitch", "applenotes", "mac")


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


@pytest.fixture
def bot(ws):
    registry.reset_for_tests()
    bid = "p1"
    os.makedirs(bpaths.bot_dir(bid))
    store.write_meta(bid, {"id": bid, "name": "Tester", "model": "sonnet", "effort": "low", "status": "idle",
                           "instructions": "", "created": time.time(), "task": "", "step": 0, "url": None, "title": None})
    b = registry.get(bid)
    yield b
    registry.reset_for_tests()


def _events(b):
    return [ev for _, ev in store.iter_events(b.events_path)]


def test_catalog_lists_every_preset_folder_with_its_playbooks():
    cat = {p["key"]: p for p in presets_mod.presets()}
    for key in ALL_KEYS:
        assert key in cat, key
        p = cat[key]
        assert p["name"] and p["color"].startswith("#") and p["instructions"].strip()
        assert p["model"] in botmod.MODELS
        assert len(p["skills"]) >= 3, key
        assert all(s["title"] and s["trigger"] for s in p["skills"]), key
        assert isinstance(p["apps"], list)


def test_catalog_is_sorted_by_order_then_name():
    cat = presets_mod.presets()
    keys = [p["key"] for p in cat]
    assert keys[0] == "linkedin"
    assert [(p["order"], p["name"]) for p in cat] == sorted((p["order"], p["name"]) for p in cat)


def test_presets_dir_ships_inside_the_package():
    assert presets_mod.PRESETS_DIR == os.path.join(os.path.dirname(botmod.__file__), "presets")
    assert os.path.isfile(os.path.join(presets_mod.PRESETS_DIR, "linkedin", "preset.json"))


def test_apply_copies_playbooks_and_sets_icon_face(bot):
    presets_mod.apply_preset(bot, "linkedin")
    titles = {s["title"] for s in bot.skills()}
    assert len(titles) >= 3 and any("feed" in t.lower() for t in titles)
    assert bot.meta["face"]["icon"] == "linkedin"
    assert bot.meta["face"]["color"].startswith("#")
    assert bot.meta["preset"] == "linkedin"
    # the preset's standing rules land in Instructions when the bot has none
    assert "never" in bot.meta["instructions"].lower()
    # and it is on disk, not only in memory
    assert store.read_meta(bot.id)["face"]["icon"] == "linkedin"


def test_apply_keeps_instructions_the_user_typed(bot):
    bot.meta["instructions"] = "Only the jobs tab."
    presets_mod.apply_preset(bot, "youtube")
    assert bot.meta["instructions"] == "Only the jobs tab."


def test_apply_unknown_preset_raises(bot):
    with pytest.raises(ValueError):
        presets_mod.apply_preset(bot, "myspace")
    assert not bot.skills()


def test_playbooks_mount_on_their_trigger_words(bot):
    presets_mod.apply_preset(bot, "linkedin")
    assert bot.skills_for("summarize my linkedin feed this morning")
    assert not bot.skills_for("what is the weather")


# ---- content quality: every shipped playbook must be one the bot can actually follow ----
def _all_playbooks():
    for p in presets_mod.presets():
        for s in p["skills"]:
            yield p, s


GENERIC_TASKS = [
    "what is the weather in Paris tomorrow",
    "open the fused docs and tell me how to deploy a udf",
    "build me a small expense tracker app",
    "summarize this pdf I attached",
    "find a good italian recipe for dinner",
]


def test_every_playbook_has_5_to_12_numbered_steps():
    for p, s in _all_playbooks():
        steps = [ln for ln in s["body"].splitlines() if ln.strip() and ln.lstrip()[0].isdigit()]
        assert 5 <= len(steps) <= 12, f"{p['key']}/{s['name']}: {len(steps)} steps"


def test_every_playbook_body_fits_the_skill_cap_with_room():
    for p, s in _all_playbooks():
        assert len(s["body"]) <= botmod.Bot.SKILL_BODY_CAP - 500, f"{p['key']}/{s['name']} is {len(s['body'])} chars"


def test_playbooks_use_login_not_take_over():
    for p, s in _all_playbooks():
        assert "take over" not in s["body"].lower(), f"{p['key']}/{s['name']}"
    for p in presets_mod.presets():
        assert "take over" not in p["instructions"].lower(), p["key"]
        assert "login" in p["instructions"].lower(), f"{p['key']} instructions never mention `login`"


def test_triggers_are_lowercase_and_several():
    for p, s in _all_playbooks():
        phrases = [t.strip() for t in s["trigger"].split(",") if t.strip()]
        assert len(phrases) >= 3, f"{p['key']}/{s['name']}: {phrases}"
        assert s["trigger"] == s["trigger"].lower(), f"{p['key']}/{s['name']}"


def test_no_playbook_fires_on_generic_tasks(bot):
    """Mount every preset's playbooks on one bot and make sure unrelated tasks mount nothing."""
    bot.SKILL_MAX = 10_000  # the per-bot cap is not what this test is about
    for p in presets_mod.presets():
        for s in p["skills"]:
            bot.skill_save(s["title"], s["trigger"], s["body"], name=f"{p['key']}-{s['name']}")
    for task in GENERIC_TASKS:
        hits = [h["title"] for h in bot.skills_for(task)]
        assert not hits, f"{task!r} mounted {hits}"


def test_each_preset_has_4_to_6_playbooks_and_short_instructions():
    for p in presets_mod.presets():
        assert 4 <= len(p["skills"]) <= 6, f"{p['key']}: {len(p['skills'])}"
        assert len(p["instructions"]) <= 700, f"{p['key']} instructions are {len(p['instructions'])} chars"


# ---- create() with a preset -------------------------------------------------------
def test_create_with_preset_applies_before_the_created_line(ws):
    b = botmod.create("Scout", preset="linkedin")
    n = len(b.skills())
    assert n >= 4 and b.meta["preset"] == "linkedin" and b.meta["face"]["icon"] == "linkedin"
    texts = [e["text"] for e in _events(b)]
    assert texts[-1] == f"Scout created. Comes with {n} linkedin playbooks."


def test_create_with_unknown_preset_leaves_no_bot(ws):
    before = registry.ids()
    with pytest.raises(ValueError, match="unknown preset"):
        botmod.create("Ghost", preset="myspace")
    assert registry.ids() == before


def test_create_without_preset_keeps_the_plain_line(ws):
    b = botmod.create("Plain")
    assert [e["text"] for e in _events(b)] == ["Plain created."]
    assert "preset" not in b.meta


# ---- HTTP -------------------------------------------------------------------------
def j(resp):
    status, headers, body = resp
    return status, json.loads(body or b"{}")


def test_presets_route_lists_titles_only(client, ws):
    st, out = j(client.get("/api/bots/presets"))
    assert st == 200 and out["ok"] is True
    cat = {p["key"]: p for p in out["presets"]}
    assert set(ALL_KEYS) <= set(cat)
    li = cat["linkedin"]
    assert set(li) == {"key", "name", "color", "order", "model", "instructions", "apps", "skills"}
    assert li["skills"] and all(isinstance(t, str) for t in li["skills"])
    assert cat["gdocs"]["apps"] == ["google-docs-tabs"]


def test_create_route_with_preset_copies_skills_and_sets_face(client, ws):
    st, out = j(client.post("/api/bots", {"name": "Scout", "preset": "youtube", "instructions": ""}))
    assert st == 200 and out["ok"], out
    bid = out["id"]
    st, status = j(client.get(f"/api/bots?shot_for={bid}"))
    s = next(b for b in status["bots"] if b["id"] == bid)
    assert s["face"]["icon"] == "youtube" and s["face"]["color"].startswith("#")
    assert len(s["skills"]) >= 4
    assert "login" in s["instructions"].lower()
    assert s["events"][-1]["text"] == f"Scout created. Comes with {len(s['skills'])} youtube playbooks."


def test_create_route_with_preset_installs_its_starter(client, ws):
    st, out = j(client.post("/api/bots", {"name": "Docs", "preset": "gdocs"}))
    assert st == 200, out
    assert os.path.isfile(os.path.join(bpaths.apps_root(), "google-docs-tabs", "mcp.toml"))
    st, status = j(client.get("/api/bots"))
    s = next(b for b in status["bots"] if b["id"] == out["id"])
    assert any("Installed the Google Docs Tabs app" in e["text"] for e in s["events"])


def test_create_route_unknown_preset_is_400_and_no_bot(client, ws):
    st, out = j(client.post("/api/bots", {"name": "Ghost", "preset": "myspace"}))
    assert st == 400 and "unknown preset" in out["error"]
    st, status = j(client.get("/api/bots"))
    assert status["bots"] == []
    st, out = j(client.post("/api/bots", {"name": "Ghost", "preset": ["x"]}))
    assert st == 400


def test_flag_keeps_a_brand_icon(client, ws):
    st, out = j(client.post("/api/bots", {"name": "Scout"}))
    bid = out["id"]
    st, _ = j(client.post(f"/api/bots/{bid}/flag", {"face": {"shape": "", "color": "#0a66c2", "icon": "linkedin"}}))
    assert st == 200
    st, status = j(client.get("/api/bots"))
    s = next(b for b in status["bots"] if b["id"] == bid)
    assert s["face"] == {"shape": "", "color": "#0a66c2", "icon": "linkedin"}
