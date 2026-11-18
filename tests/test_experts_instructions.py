"""Settings > Instructions for an expert: `GET` / `POST /api/experts/{id}/instructions` through the real server."""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client
import json
import os

import pytest

from fused_render import lan
from fused_render.bots import agent_engine, registry
from fused_render.experts import seed, store


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


@pytest.fixture
def expert():
    return registry.create(name="Canvas Helper", kind="expert", expert={"ask_me_about": "Canvases"})


def url(bot):
    return f"/api/experts/{bot.id}/instructions"


def on_disk(bot):
    return store.read_text(store.instructions_path(bot))


def test_read_is_what_the_expert_runs_with(client, expert):
    st, out = j(client.get(url(expert)))
    assert st == 200, out
    assert out == {"text": store.DEFAULT_INSTRUCTIONS.strip(), "path": store.instructions_path(expert),
                   "limit": store.INSTRUCTIONS_MAX, "legacy": False, "shipped": "", "follows_shipped": False}
    os.remove(store.instructions_path(expert))  # no file: the default text, as the harness gets it
    assert j(client.get(url(expert)))[1]["text"] == store.instructions(expert) == store.DEFAULT_INSTRUCTIONS.strip()


def test_save_writes_the_whole_file_and_starts_a_fresh_chat_session(client, expert):
    before = agent_engine._sha(agent_engine.system_prompt_for(expert, store.expert_dir(expert)))
    st, out = j(client.post(url(expert), {"text": "  You answer about maps.\r\n\r\nBe brief.  "}))
    assert st == 200, out
    assert out["text"] == "You answer about maps.\n\nBe brief." and out["legacy"] is False
    assert on_disk(expert) == "You answer about maps.\n\nBe brief.\n"
    assert store.instructions(expert) == out["text"]
    # The chat resumes its session only while the system prompt's hash is the one the session was born with
    # (agent_engine._conversation_plan): a save changes it.
    assert agent_engine._sha(agent_engine.system_prompt_for(expert, store.expert_dir(expert))) != before
    assert [n for n in os.listdir(store.expert_dir(expert)) if n.endswith(".tmp")] == []


def test_a_legacy_settings_extra_is_shown_and_folded_into_the_file_on_save(client, expert):
    expert.meta["instructions"] = "Always answer in French."
    expert.save()
    st, out = j(client.get(url(expert)))
    shown = out["text"]
    assert out["legacy"] is True and shown == store.instructions(expert)
    assert shown.endswith("## From the user (Settings)\n\nAlways answer in French.")
    assert "French" not in on_disk(expert)
    hash_before = agent_engine._sha(store.instructions(expert))
    st, out = j(client.post(url(expert), {"text": shown}))  # saved as shown: nothing the expert is told changes
    assert st == 200 and out["text"] == shown and out["legacy"] is False
    assert on_disk(expert) == shown + "\n" and registry.get(expert.id).meta["instructions"] == ""
    assert agent_engine._sha(store.instructions(expert)) == hash_before
    # The user took the old text out: it is gone, not applied from the extra behind their back.
    expert.meta["instructions"] = "Always answer in French."
    st, out = j(client.post(url(expert), {"text": "Answer in English."}))
    assert st == 200 and store.instructions(expert) == "Answer in English." and expert.meta["instructions"] == ""


def test_refused_saves_change_nothing(client, expert, monkeypatch):
    expert.meta["instructions"] = "Extra."
    was = on_disk(expert)
    st, out = j(client.post(url(expert), {"text": "x" * (store.INSTRUCTIONS_MAX + 1)}))
    assert st == 400 and "50,001 characters" in out["error"] and "50,000" in out["error"]
    for body in ({"text": "   \n"}, {"text": 7}, {}):
        st, out = j(client.post(url(expert), body))
        assert st == 400 and out["error"], body
    st, _ = j(client.post(url(expert), {"text": "ok"}, headers={"X-Fused": ""}))
    assert st == 403
    with monkeypatch.context() as m:
        m.setattr(registry, "workspace_claimed", lambda: False)
        st, out = j(client.post(url(expert), {"text": "ok"}))
        assert st == 409 and out["reason"] == "workspace_busy"
    assert on_disk(expert) == was and expert.meta["instructions"] == "Extra."
    st, out = j(client.post(url(expert), {"text": "x" * store.INSTRUCTIONS_MAX}))  # the limit itself fits, uncut
    assert st == 200 and len(on_disk(expert)) == store.INSTRUCTIONS_MAX + 1


def test_only_an_expert_has_the_route(client):
    bot = registry.create(name="Scout")
    for st, out in (j(client.get(url(bot))), j(client.post(url(bot), {"text": "hello"}))):
        assert st == 404 and "no such expert" in out["error"]
    assert not os.path.exists(os.path.join(bot.public_dir, "instructions.md"))


def test_the_settings_route_no_longer_takes_an_experts_instructions(client, expert):
    st, out = j(client.post(f"/api/bots/{expert.id}/settings", {"name": "Canvas Helper", "instructions": "from a stale dialog"}))
    assert st == 200, out
    assert not registry.get(expert.id).meta.get("instructions") and "stale" not in store.instructions(expert)
    bot = registry.create(name="Scout")  # an ordinary bot's box is as it was
    assert j(client.post(f"/api/bots/{bot.id}/settings", {"name": "Scout", "instructions": "Be brief."}))[0] == 200
    assert registry.get(bot.id).meta["instructions"] == "Be brief."


def test_a_shipped_expert_follows_updates_until_its_instructions_are_saved(client, tmp_path, monkeypatch):
    import shutil
    shipped = tmp_path / "shipped"
    shutil.copytree(seed.SHIPPED_DIR, shipped)
    monkeypatch.setattr(seed, "SHIPPED_DIR", str(shipped))
    assert "canvas-reference" in seed.seed()
    b = seed._existing("canvas-reference")
    st, out = j(client.get(url(b)))
    assert out["shipped"] == "canvas-reference" and out["follows_shipped"] is True
    # A save that changes nothing leaves the file as shipped: it still follows.
    was = on_disk(b)
    st, out = j(client.post(url(b), {"text": out["text"]}))
    assert st == 200 and on_disk(b) == was and out["follows_shipped"] is True
    st, out = j(client.post(url(b), {"text": out["text"] + "\n\nAlso: be brief."}))
    assert st == 200 and out["follows_shipped"] is False
    with open(shipped / "canvas-reference" / "instructions.md", "a", encoding="utf-8") as f:
        f.write("\nshipped v2\n")
    seed.seed()
    assert on_disk(b).endswith("Also: be brief.\n")  # theirs now


def test_the_phone_door_passes_the_read_and_the_save():
    path = "/api/experts/b_1/instructions"
    assert lan._door_for(path, "GET") and lan._door_for(path, "POST")
    assert lan._door_for(path, "DELETE") is None and lan._door_for(path + "/x", "GET") is None
    assert lan._door_for("/api/bots/b_1/settings", "POST")  # the settings write it sits beside
    assert lan._door_for("/api/experts/ask", "POST") is None
