"""`/api/experts/*` through the real server (harness stubbed), and the shipped-expert seed."""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client
import json
import os

import pytest

from fused_render.bots import registry
from fused_render.experts import learn, seed, sessions, store


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


def test_seed_once_and_a_deleted_expert_stays_deleted():
    from fused_render import canvases
    assert seed.seed() == ["canvas-reference"]
    [b] = store.all_experts()
    assert b.meta["name"] == "Canvas Reference" and store.manifest(b)["shipped"] == "canvas-reference"
    assert store.manifest(b)["ask_me_about"].startswith("Lessons from past canvas tasks")
    # An install still carrying an earlier shipped description gets the current one; one the user wrote is kept.
    for old_text, kept in ((seed.PAST_ABOUT["canvas-reference"][0], False), ("my own words", True)):
        b.meta["expert"] = {**b.meta["expert"], "ask_me_about": old_text}
        b.save()
        seed.seed()
        got = store.manifest(store.all_experts()[0])["ask_me_about"]
        assert (got == old_text) is kept and (kept or got.startswith("Lessons from past canvas tasks"))
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


def test_an_installed_expert_gets_a_shipped_skill_added_later_once():
    """An install from before a skill was added (the witness lists no skills, or not this one) is handed the new
    file on the next seed; its other skills and its instructions are left alone, and deleting it sticks."""
    assert seed.seed() == ["canvas-reference"]
    b = store.all_experts()[0]
    d = store.expert_dir(b)
    new = os.path.join(d, "skills", store.RUN_SKILL)
    assert os.path.isfile(new)
    assert store.read_json(seed.witness_path(), {})["skills"]["canvas-reference"] == seed._skill_names("canvas-reference")
    # As an older version left it: no running skill, a witness with keys only, files the user changed or deleted.
    os.remove(new)
    os.remove(os.path.join(d, "skills", "edges-and-folders.md"))
    with open(os.path.join(d, "instructions.md"), "w", encoding="utf-8") as f:
        f.write("mine")
    with open(os.path.join(d, "skills", "canvas-folder-layout.md"), "w", encoding="utf-8") as f:
        f.write("# mine")
    keys = store.read_json(seed.witness_path(), {})["keys"]
    store.write_json(seed.witness_path(), {"keys": keys})
    assert seed.seed() == [] and len(store.all_experts()) == 1
    shipped = os.path.join(seed.SHIPPED_DIR, "canvas-reference", "skills", store.RUN_SKILL)
    assert store.read_text(new) == store.read_text(shipped)
    assert not os.path.exists(os.path.join(d, "skills", "edges-and-folders.md"))  # an original the user deleted
    assert store.read_text(os.path.join(d, "instructions.md")) == "mine"
    assert store.read_text(os.path.join(d, "skills", "canvas-folder-layout.md")) == "# mine"
    os.remove(new)  # offered once: the user's delete sticks
    assert seed.seed() == [] and not os.path.exists(new)


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


# ------------------------------------------------------------ review fixes ---
def test_settings_never_write_the_bot_memory_file_of_an_expert(client, expert):
    before = store.memory(expert)
    st, out = j(client.post(f"/api/bots/{expert.id}/settings", {"name": "Canvas Helper", "memory": "- a note"}))
    assert st == 200, out
    assert not os.path.exists(expert.memory_path) and store.memory(expert) == before


def _expert_package(path, pub, memory=b"- remember this\n", skills=()):
    from fused_render import appfile_container
    members = ([("bot.json", json.dumps(pub).encode())] + ([(".fused/memory.md", memory)] if memory else [])
               + [(f"skills/{n}.md", raw) for n, raw in skills])
    appfile_container.write(str(path), {"name": pub["name"]}, members, kind="bot")
    return str(path)


def test_an_imported_expert_skill_needs_no_trigger(tmp_path):
    """An expert's skill is a reference file read by its path: one without a `trigger:` line is complete, and the
    playbook warning ("it never runs until you add one") is not said of it. One with no text still is."""
    from fused_render.bots import botfile
    pub = {"name": "Skilled Expert", "kind": "expert", "expert": {"ask_me_about": "hand-made"}}
    out = botfile.install(_expert_package(tmp_path / "s.fusedbot", pub, memory=None,
                                          skills=[("ref", b"# Ref\n\nThe text.\n"), ("empty", b"# Empty\n")]), apps=[], memory=False)
    assert [s["title"] for s in store.skills(registry.get(out["bot_id"]))] == ["Empty", "Ref"]
    assert out["warnings"] == ["skill empty: it has no text"]


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


def test_memory_rules_example_is_only_a_format_and_shipped_instructions_end_with_them():
    shipped = open(os.path.join(seed.SHIPPED_DIR, "canvas-reference", "instructions.md"), encoding="utf-8").read()
    assert shipped.endswith(store.MEMORY_RULES) and store.DEFAULT_INSTRUCTIONS.endswith(store.MEMORY_RULES)
    assert "it is not a fact" in store.MEMORY_RULES and "canvas pull" not in store.MEMORY_RULES


def test_a_cut_short_seed_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "skills").mkdir(parents=True)
    (src / "instructions.md").write_text("the full text")
    real = seed.shutil.copyfile

    def partial(a, b):
        with open(b, "w") as f:
            f.write("the fu")
        raise OSError("disk full")
    monkeypatch.setattr(seed.shutil, "copyfile", partial)
    with pytest.raises(OSError):
        seed._copy(str(src), str(dst), overwrite=False)
    assert os.listdir(dst) == []
    monkeypatch.setattr(seed.shutil, "copyfile", real)
    seed._copy(str(src), str(dst), overwrite=False)  # a later non-overwriting pass copies it whole
    assert (dst / "instructions.md").read_text() == "the full text"


def test_a_changed_shipped_file_replaces_only_an_unedited_copy(tmp_path, monkeypatch):
    import shutil
    shipped = tmp_path / "shipped"
    shutil.copytree(seed.SHIPPED_DIR, shipped)
    monkeypatch.setattr(seed, "SHIPPED_DIR", str(shipped))
    assert seed.seed() == ["canvas-reference"]
    d = store.expert_dir(store.all_experts()[0])
    given = store.read_json(seed.witness_path(), {})["given"]["canvas-reference"]
    assert given["instructions.md"] == seed._sha(os.path.join(d, "instructions.md")) and len(given) == 1 + len(seed._skill_names("canvas-reference"))
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
    # An install from before the witness recorded hashes: a copy matching a known earlier version is unedited.
    w = store.read_json(seed.witness_path(), {})
    w.pop("given")
    store.write_json(seed.witness_path(), w)
    with open(os.path.join(d, "instructions.md"), "w", encoding="utf-8") as f:
        f.write("an old shipped text")
    monkeypatch.setitem(seed.PAST["canvas-reference"], "instructions.md", [seed._sha(os.path.join(d, "instructions.md"))])
    with open(os.path.join(d, "skills", "running-udfs.md"), "w", encoding="utf-8") as f:
        f.write("not a version we ever shipped")
    assert seed.seed() == []
    assert read("instructions.md").endswith("shipped v2\n") and read("skills/running-udfs.md") == "not a version we ever shipped"
    # With a hash on record, an earlier version is not ours any more: the user put it back by hand, and it stays.
    with open(os.path.join(d, "instructions.md"), "w", encoding="utf-8") as f:
        f.write("an old shipped text")
    assert seed.seed() == [] and read("instructions.md") == "an old shipped text"
    assert not seed.follows_shipped(store.all_experts()[0])


def test_the_record_of_offered_skills_only_grows_and_survives_a_failed_resync(monkeypatch):
    """A skill is offered once. The record is kept when the skill leaves the shipped folder (it may come back in
    a later release, and a user who deleted it must not get it again), and it is written even when the resync
    that follows the offer fails."""
    assert seed.seed() == ["canvas-reference"]
    w = store.read_json(seed.witness_path(), {})
    w["skills"]["canvas-reference"] = [n for n in w["skills"]["canvas-reference"] if n != store.RUN_SKILL] + ["gone.md"]
    store.write_json(seed.witness_path(), w)
    new = os.path.join(store.expert_dir(store.all_experts()[0]), "skills", store.RUN_SKILL)
    os.remove(new)

    def boom(key, given):
        raise OSError("disk")
    monkeypatch.setattr(seed, "_resync", boom)
    assert seed.seed() == [] and os.path.exists(new)  # offered again (the record said it never was) ...
    offered = store.read_json(seed.witness_path(), {})["skills"]["canvas-reference"]
    assert store.RUN_SKILL in offered and "gone.md" in offered  # ... and recorded, with what was offered before
    os.remove(new)
    assert seed.seed() == [] and not os.path.exists(new)  # deleted now: it stays deleted


def test_every_earlier_shipped_version_is_listed_and_the_current_one_is_not():
    for key, files in seed.PAST.items():
        for rel, hashes in files.items():
            assert seed._sha(os.path.join(seed.SHIPPED_DIR, key, rel)) not in hashes, rel
