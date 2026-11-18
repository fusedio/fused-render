"""`/api/experts/*` through the real server (harness stubbed), and the shipped-expert seed."""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client
import json
import os

import pytest

from fused_render.bots import registry
from fused_render.experts import learn, seed, sessions, store

#: The shipped experts, in the order `seed.seed` installs them.
SHIPPED = ["canvas-reference", "data-expert"]


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


@pytest.fixture
def expert(monkeypatch):
    monkeypatch.setattr(learn, "caller", lambda run_id: {"run_id": run_id, "session_id": "s-" + run_id,
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
    assert learn._load(expert)["sessions"]["s-r1"]["run_ids"] == ["r1"]


def test_context_and_ask_need_the_workspace(client, expert, monkeypatch):
    monkeypatch.setattr(registry, "workspace_claimed", lambda: False)
    for path, body in (("/api/experts/context", {"expert": expert.id, "run_id": "r1"}),
                       ("/api/experts/ask", {"expert": expert.id, "question": "q?", "run_id": "r1"})):
        st, out = j(client.post(path, body))
        assert st == 409 and out["reason"] == "workspace_busy", out
    assert learn._load(expert)["sessions"] == {}  # no use recorded


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
    assert learn._load(expert)["sessions"]["s-r7"]["run_ids"] == ["r7"]
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


def test_settings_never_write_the_bot_memory_file_of_an_expert(client, expert):
    before = store.memory(expert)
    st, out = j(client.post(f"/api/bots/{expert.id}/settings", {"name": "Canvas Helper", "memory": "- a note"}))
    assert st == 200, out
    assert not os.path.exists(expert.memory_path) and store.memory(expert) == before


def test_seed_once_and_a_deleted_expert_stays_deleted():
    from fused_render import canvases
    assert seed.seed() == SHIPPED
    b, data = seed._existing("canvas-reference"), seed._existing("data-expert")
    assert len(store.all_experts()) == 2 and data.meta["name"] == "Data Expert"
    assert b.meta["name"] == "Canvas Reference" and store.manifest(b)["shipped"] == "canvas-reference"
    assert store.manifest(b)["ask_me_about"].startswith("Lessons from past canvas tasks")
    # The shipped description changed since it was given: an install still carrying what it was given gets the
    # new one; one the user wrote is kept.
    w = store.read_json(seed.witness_path(), {})
    w["given"]["canvas-reference"]["ask_me_about"] = "what an earlier version shipped"
    store.write_json(seed.witness_path(), w)
    for text, kept in (("what an earlier version shipped", False), ("my own words", True)):
        b.meta["expert"] = {**b.meta["expert"], "ask_me_about": text}
        b.save()
        seed.seed()
        got = store.manifest(seed._existing("canvas-reference"))["ask_me_about"]
        assert (got == text) is kept and (kept or got.startswith("Lessons from past canvas tasks"))
    assert b.meta["face"]["shape"] == "hexagon"
    assert b.folders() == [os.path.abspath(canvases.canvases_base())]
    d = store.expert_dir(b)
    shipped = os.path.join(seed.SHIPPED_DIR, "canvas-reference")
    assert open(os.path.join(d, "instructions.md")).read() == open(os.path.join(shipped, "instructions.md")).read()
    assert sorted(os.listdir(os.path.join(d, "skills"))) == sorted(os.listdir(os.path.join(shipped, "skills")))
    assert store.memory(b) == store.EMPTY_MEMORY
    assert "memory wins" in store.instructions(b) and "read_file" not in store.instructions(b)
    assert seed.seed() == [] and len(store.all_experts()) == 2
    registry.delete(b.id)
    assert seed.seed() == [] and [x.id for x in store.all_experts()] == [data.id]


def test_an_installed_expert_gets_a_shipped_skill_added_later_once():
    """An install from before a skill was added (the witness does not list it as offered) is handed the new file
    on the next seed; its other skills and its instructions are left alone, and deleting it sticks."""
    assert seed.seed() == SHIPPED
    b = seed._existing("canvas-reference")
    d = store.expert_dir(b)
    new = os.path.join(d, "skills", store.RUN_SKILL)
    assert os.path.isfile(new)
    assert store.read_json(seed.witness_path(), {})["skills"]["canvas-reference"] == seed._skill_names("canvas-reference")
    # As an older version left it: no running skill, not on record as offered, files the user changed or deleted.
    os.remove(new)
    os.remove(os.path.join(d, "skills", "edges-and-folders.md"))
    with open(os.path.join(d, "instructions.md"), "w", encoding="utf-8") as f:
        f.write("mine")
    with open(os.path.join(d, "skills", "canvas-folder-layout.md"), "w", encoding="utf-8") as f:
        f.write("# mine")
    w = store.read_json(seed.witness_path(), {})
    w["skills"]["canvas-reference"].remove(store.RUN_SKILL)
    store.write_json(seed.witness_path(), w)
    assert seed.seed() == [] and len(store.all_experts()) == 2
    shipped = os.path.join(seed.SHIPPED_DIR, "canvas-reference", "skills", store.RUN_SKILL)
    assert store.read_text(new) == store.read_text(shipped)
    assert not os.path.exists(os.path.join(d, "skills", "edges-and-folders.md"))  # an original the user deleted
    assert store.read_text(os.path.join(d, "instructions.md")) == "mine"
    assert store.read_text(os.path.join(d, "skills", "canvas-folder-layout.md")) == "# mine"
    os.remove(new)  # offered once: the user's delete sticks
    assert seed.seed() == [] and not os.path.exists(new)


def test_a_seed_never_makes_a_second_expert_for_a_shipped_key():
    from fused_render.bots import bot as bm
    # What a seed that ended before its witness was written leaves: the expert, no record of it.
    b = bm.create(name="Canvas Reference", kind="expert", greet=False,
                  expert={"ask_me_about": "x", "shipped": "canvas-reference"})
    assert seed.seed() == SHIPPED
    assert seed._existing("canvas-reference").id == b.id and len(store.all_experts()) == 2 and seed.seed() == []


def test_a_changed_shipped_file_replaces_only_an_unedited_copy(tmp_path, monkeypatch):
    import shutil
    shipped = tmp_path / "shipped"
    shutil.copytree(seed.SHIPPED_DIR, shipped)
    monkeypatch.setattr(seed, "SHIPPED_DIR", str(shipped))
    assert seed.seed() == SHIPPED
    d = store.expert_dir(seed._existing("canvas-reference"))
    given = store.read_json(seed.witness_path(), {})["given"]["canvas-reference"]
    assert given["instructions.md"] == seed._sha(os.path.join(d, "instructions.md"))
    assert sorted(given) == sorted(["ask_me_about", *seed._files("canvas-reference")])
    src = shipped / "canvas-reference"
    with open(os.path.join(d, "skills", "edges-and-folders.md"), "a", encoding="utf-8") as f:
        f.write("\nmine\n")                       # edited by the user
    os.remove(os.path.join(d, "skills", "canvas-folder-layout.md"))   # deleted by the user
    for rel in ("instructions.md", "skills/edges-and-folders.md", "skills/canvas-folder-layout.md", "skills/running-udfs.md"):
        with open(src / rel, "a", encoding="utf-8") as f:
            f.write("\nshipped v2\n")
    assert seed.seed() == []
    read = lambda rel: store.read_text(os.path.join(d, rel))  # noqa: E731
    assert read("instructions.md").endswith("shipped v2\n") and read("skills/running-udfs.md").endswith("shipped v2\n")
    assert read("skills/edges-and-folders.md").endswith("mine\n")      # an edited copy is theirs
    assert not os.path.exists(os.path.join(d, "skills", "canvas-folder-layout.md"))  # a deleted skill stays deleted


def test_memory_rules_example_is_only_a_format_and_shipped_instructions_end_with_them():
    assert seed.shipped_keys() == SHIPPED
    for key in SHIPPED:
        shipped = open(os.path.join(seed.SHIPPED_DIR, key, "instructions.md"), encoding="utf-8").read()
        assert shipped.endswith(store.MEMORY_RULES), key
    assert store.DEFAULT_INSTRUCTIONS.endswith(store.MEMORY_RULES)
    assert "it is not a fact" in store.MEMORY_RULES and "canvas pull" not in store.MEMORY_RULES


@pytest.mark.parametrize("key", SHIPPED)
def test_a_shipped_expert_is_installable_as_written(key):
    """What the seed and the bot's skill form would refuse or cut: a description over one line or its limit, a face
    the picker cannot draw, a skill over the expert limit, a skill naming a skill file it does not ship."""
    import re
    from fused_render.bots import bot as bm
    folder = os.path.join(seed.SHIPPED_DIR, key)
    with open(os.path.join(folder, "expert.json"), encoding="utf-8") as f:
        spec = json.load(f)
    assert store.line(spec["ask_me_about"]) == spec["ask_me_about"]  # one line, within the limit: never cut
    bm.check_face({}, spec["face"], strict=True)
    names = seed._skill_names(key)
    for rel in ["instructions.md"] + ["skills/" + n for n in names]:
        text = open(os.path.join(folder, rel), encoding="utf-8").read()
        assert len(text) <= bm.Bot.EXPERT_SKILL_BODY_CAP or rel == "instructions.md", rel
        assert len(text) <= store.INSTRUCTIONS_MAX
        for named in re.findall(r"`(?:skills/)?([a-z0-9-]+\.md)`", text):
            assert named in names or named in ("memory.md", "memory-archive.md", "instructions.md"), (rel, named)


def test_the_running_udfs_skill_is_one_text_in_every_shipped_expert():
    copies = {key: open(p, encoding="utf-8").read() for key in SHIPPED
              if os.path.isfile(p := os.path.join(seed.SHIPPED_DIR, key, "skills", store.RUN_SKILL))}
    assert len(copies) == len(SHIPPED) and len(set(copies.values())) == 1  # edit them together


def test_the_data_experts_profiling_udfs_are_one_udf_each():
    """Each ready UDF in the profiling skill parses, is one `@fused.udf` function and is announced under the file
    name the skill tells the expert to write."""
    import ast
    import re
    text = open(os.path.join(seed.SHIPPED_DIR, "data-expert", "skills", "profiling-with-udfs.md"), encoding="utf-8").read()
    blocks = re.findall(r"## [^\n]*`([a-z_]+\.py)`.*?```python\n(.*?)```", text, re.S)
    assert [n for n, _ in blocks] == ["profile_parquet.py", "profile_table.py", "profile_folder.py",
                                      "profile_raster.py", "column_values.py"]
    for name, src in blocks:
        funcs = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)]
        assert len(funcs) == 1 and ast.unparse(funcs[0].decorator_list[0]) == "fused.udf", name


def test_an_expert_is_not_exported(client, expert, tmp_path):
    from fused_render.bots import package
    with pytest.raises(ValueError, match="expert cannot be exported"):
        package.write(expert.id, memory=True, apps=[], file_name=None, dest_dir=str(tmp_path))
    st, out = j(client.get(f"/api/bots/{expert.id}/package"))
    assert st == 400 and out["error"] == package.EXPERT_REFUSAL


def test_an_expert_never_takes_a_local_model(client, expert):
    from fused_render.bots import bot as bm
    local = next(iter(bm.LOCAL_MODELS))
    made = registry.create(name="Local Expert", kind="expert", model=local)
    assert made.meta["model"] == bm.DEFAULT_MODEL
    with pytest.raises(ValueError, match="Claude model"):
        bm.check_settings(expert.meta, model=local)


def _expert_package(path, pub, memory=b"- remember this\n"):
    from fused_render import appfile_container
    members = [("bot.json", json.dumps(pub).encode())] + ([(".fused/memory.md", memory)] if memory else [])
    appfile_container.write(str(path), {"name": pub["name"]}, members, kind="bot")
    return str(path)


def test_an_expert_package_takes_no_local_model_and_no_bot_memory(tmp_path):
    from fused_render.bots import bot as bm
    from fused_render.bots import botfile
    local = next(iter(bm.LOCAL_MODELS))
    pub = {"name": "Hand Expert", "kind": "expert", "model": local, "expert": {"ask_me_about": "hand-made"}}
    out = botfile.install(_expert_package(tmp_path / "new.fusedbot", pub), apps=[], memory=True)
    b = registry.get(out["bot_id"])
    assert store.is_expert(b) and b.meta["model"] == bm.DEFAULT_MODEL
    assert botfile.EXPERT_MEMORY_NOT_IMPORTED in out["warnings"]
    assert not os.path.exists(b.memory_path) and store.memory(b) == store.EMPTY_MEMORY
    # The update path: the same hand-made package, matched by id.
    out = botfile.install(_expert_package(tmp_path / "up.fusedbot", {**pub, "id": b.id}), apps=[], memory=True,
                          mode="update", target=b.id)
    assert out["updated"] is True and botfile.EXPERT_MEMORY_NOT_IMPORTED in out["warnings"]
    assert b.meta["model"] == bm.DEFAULT_MODEL and not os.path.exists(b.memory_path)
