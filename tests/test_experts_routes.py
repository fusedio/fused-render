"""`/api/experts/*` through the real server (harness stubbed), and the shipped-expert seed."""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client
import json
import os

import pytest

from fused_render.bots import registry
from fused_render.experts import quiet, seed, sessions, store


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


@pytest.fixture
def expert(monkeypatch):
    monkeypatch.setattr(quiet, "caller", lambda run_id: {"run_id": run_id, "session_id": "s-" + run_id,
                                                         "backend": "claude", "target": "/work/canvas"})
    return registry.create(name="Canvas Helper", kind="expert", expert={"ask_me_about": "Canvases\nand more"})


def test_list_context_and_use_recorded(client, expert):
    st, out = j(client.get("/api/experts"))
    assert st == 200 and out == [{"id": expert.id, "name": "Canvas Helper", "ask_me_about": "Canvases and more"}]
    os.makedirs(os.path.join(store.expert_dir(expert), "skills"), exist_ok=True)
    with open(os.path.join(store.expert_dir(expert), "skills", "a.md"), "w") as f:
        f.write("# Alpha skill\n\nbody\n")
    st, out = j(client.post("/api/experts/context", {"expert": "canvas helper", "run_id": "r1"}))
    assert st == 200, out
    assert out["memory"] == store.memory(expert) and out["memory_path"] == store.memory_path(expert)
    assert out["skills"] == [{"title": "Alpha skill", "path": os.path.join(store.expert_dir(expert), "skills", "a.md")}]
    assert quiet._load(expert)["sessions"]["s-r1"]["run_ids"] == ["r1"]


def test_context_and_ask_need_the_workspace(client, expert, monkeypatch):
    monkeypatch.setattr(registry, "workspace_claimed", lambda: False)
    for path, body in (("/api/experts/context", {"expert": expert.id, "run_id": "r1"}),
                       ("/api/experts/ask", {"expert": expert.id, "question": "q?", "run_id": "r1"})):
        st, out = j(client.post(path, body))
        assert st == 409 and out["reason"] == "workspace_busy", out
    assert quiet._load(expert)["sessions"] == {}  # no use recorded


def test_errors(client, expert):
    st, out = j(client.post("/api/experts/context", {"expert": "nobody", "run_id": "r1"}))
    assert st == 404 and "no such expert" in out["error"]
    st, out = j(client.post("/api/experts/ask", {"expert": expert.id, "question": "  "}))
    assert st == 400 and "question" in out["error"]
    st, _ = j(client.post("/api/experts/context", {"expert": expert.id}, headers={"X-Fused": ""}))
    assert st == 403
    st, out = j(client.get(f"/api/experts/{expert.id}/sessions/nope"))
    assert st == 404


def test_ask_runs_a_session_and_records_use(client, expert, monkeypatch):
    seen = {}

    def harness(bot, prompt, write):
        seen.update(prompt=prompt, write=write)
        return {"ok": True, "answer": "Pull first.", "error": "", "transcript": [{"role": "prompt", "text": prompt}]}
    monkeypatch.setattr(sessions, "harness", harness)
    st, out = j(client.post("/api/experts/ask", {"expert": expert.id, "question": "How do I edit?", "run_id": "r7"}))
    assert st == 200, out
    assert out["answer"] == "Pull first." and out["id"] == expert.id and out["name"] == "Canvas Helper"
    assert seen["write"] is False and "a claude task session working in /work/canvas" in seen["prompt"]
    assert quiet._load(expert)["sessions"]["s-r7"]["run_ids"] == ["r7"]
    st, rows = j(client.get(f"/api/experts/{expert.id}/sessions"))
    assert [r["id"] for r in rows["sessions"]] == [out["session"]] and "transcript" not in rows["sessions"][0]
    assert rows["sessions"][0]["trigger"]["session_id"] == "s-r7"
    st, row = j(client.get(f"/api/experts/{expert.id}/sessions/{out['session']}"))
    assert st == 200 and row["kind"] == "ask" and row["transcript"][0]["role"] == "prompt"


def test_memory_with_changes_and_hand_edits(client, expert):
    before = store.memory(expert)
    with open(store.memory_path(expert), "a", encoding="utf-8") as f:
        f.write("- [1 · 2026-11-17] Told in chat.\n")
    chat = sessions.note_chat(expert, before, "remember this")
    st, out = j(client.get(f"/api/experts/{expert.id}/memory"))
    assert st == 200
    assert out["text"] == store.memory(expert) and out["path"] == store.memory_path(expert)
    assert out["lines"] == len(out["text"].splitlines()) and out["bytes"] == len(out["text"].encode())
    assert (out["limit_lines"], out["limit_bytes"]) == (sessions.LIMIT_LINES, sessions.LIMIT_BYTES)
    assert out["edited_by_hand"] is False
    assert out["changes"] == [{"id": chat["id"], "kind": "chat", "started": chat["started"],
                               "trigger": {"message": "remember this"}}]
    with open(store.memory_path(expert), "a", encoding="utf-8") as f:
        f.write("- [1 · 2026-11-17] Typed by hand.\n")
    assert j(client.get(f"/api/experts/{expert.id}/memory"))[1]["edited_by_hand"] is True


def test_seed_once_and_a_deleted_expert_stays_deleted():
    from fused_render import canvases
    assert seed.seed() == ["canvas-reference"]
    [b] = store.all_experts()
    assert b.meta["name"] == "Canvas Reference" and store.manifest(b)["shipped"] == "canvas-reference"
    assert store.manifest(b)["ask_me_about"].startswith("Writing canvases")
    assert b.meta["face"]["shape"] == "hexagon"
    assert b.folders() == [os.path.abspath(canvases.canvases_base())]
    d = store.expert_dir(b)
    shipped = os.path.join(seed.SHIPPED_DIR, "canvas-reference")
    assert open(os.path.join(d, "instructions.md")).read() == open(os.path.join(shipped, "instructions.md")).read()
    assert sorted(os.listdir(os.path.join(d, "skills"))) == sorted(os.listdir(os.path.join(shipped, "skills")))
    assert store.memory(b) == store.EMPTY_MEMORY
    assert "memory wins" in store.instructions(b) and "read_file" not in store.instructions(b)
    assert seed.seed() == [] and len(store.all_experts()) == 1
    registry.delete(b.id)
    assert seed.seed() == [] and store.all_experts() == []


def test_a_cut_short_seed_is_finished():
    from fused_render import canvases
    from fused_render.bots import bot as bm
    # What a seed killed right after create() leaves: the default instructions, no skills, no face, no folders.
    b = bm.create(name="Canvas Reference", kind="expert", greet=False,
                  expert={"ask_me_about": "x", "shipped": "canvas-reference"})
    d = store.expert_dir(b)
    assert open(os.path.join(d, "instructions.md")).read() == store.DEFAULT_INSTRUCTIONS
    assert not b.meta.get("face") and not b.meta.get("folders")
    assert seed.seed() == ["canvas-reference"]
    [b] = store.all_experts()
    shipped = os.path.join(seed.SHIPPED_DIR, "canvas-reference")
    assert open(os.path.join(d, "instructions.md")).read() == open(os.path.join(shipped, "instructions.md")).read()
    assert sorted(os.listdir(os.path.join(d, "skills"))) == sorted(os.listdir(os.path.join(shipped, "skills")))
    assert b.meta["face"]["shape"] == "hexagon" and b.folders() == [os.path.abspath(canvases.canvases_base())]


def test_a_cut_short_seed_keeps_what_the_user_changed(tmp_path):
    from fused_render.bots import bot as bm
    b = bm.create(name="Canvas Reference", kind="expert", greet=False,
                  expert={"ask_me_about": "x", "shipped": "canvas-reference"})
    with open(os.path.join(store.expert_dir(b), "instructions.md"), "w") as f:
        f.write("mine")
    b.meta["folders"] = bm.clean_folders([str(tmp_path)])
    b.save()
    seed.seed()
    [b] = store.all_experts()
    assert open(os.path.join(store.expert_dir(b), "instructions.md")).read() == "mine"
    assert b.folders() == [os.path.abspath(str(tmp_path))]
