"""`/api/experts/*` (docs/experts.md §8) against a real server (`client`).

The expert is a bot written straight through the bots store with
`kind: "expert"`; every model-facing function (`consult.consult`,
`consolidate.run`, `proposals.*`, `cards.show`, `gate.start`,
`capture.capture_session`, `model.available`) is monkeypatched, so nothing here
calls a model or reads a real transcript.
"""
import json
import time

import pytest

from _bots_conftest import *  # noqa: F401,F403

from fused_render.bots import paths as bpaths
from fused_render.bots import store as bstore
from fused_render.experts import capture, cards, consolidate, consult, gate, memory, model, proposals

BID = "e1e1e1e1"


@pytest.fixture
def expert(client, monkeypatch):
    bpaths.place(BID, "Canvas Expert")
    bstore.write_meta(BID, {"id": BID, "name": "Canvas Expert", "kind": "expert", "model": "", "effort": "",
                            "status": "idle", "instructions": "", "created": time.time(), "task": "",
                            "step": 0, "url": None, "title": None,
                            "expert": {"ask_me_about": "Writing canvases", "offered_to": {"targets": ["canvas"]},
                                       "shipped": "canvas-reference"}})
    monkeypatch.setattr(model, "available", lambda bot: (True, ""))
    from fused_render.bots import registry
    return registry.get(BID)


def _json(res):
    status, _headers, body = res
    return status, json.loads(body or b"null")


def test_list(client, expert, monkeypatch):
    status, body = _json(client.get("/api/experts"))
    assert status == 200
    assert body == [{"id": BID, "name": "Canvas Expert", "ask_me_about": "Writing canvases", "engine": "auto",
                     "model": "", "available": True, "reason": ""}]
    monkeypatch.setattr(model, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    _s, body = _json(client.get("/api/experts"))
    assert body[0]["available"] is False and body[0]["reason"] == "needs Claude, Codex or Copilot"


def test_consult_happy_path_and_the_caller(client, expert, monkeypatch, tmp_path):
    from fused_render.claude_agent import agent_module
    agent = agent_module()
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    clone = tmp_path / "proj"
    clone.mkdir()
    run = tmp_path / "runs" / "20261116-101010-abcdef"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"file": str(clone / "a.py"), "backend": "codex"}))
    (run / "session").write_text("sess-9")
    seen = {}

    def fake(bot, question, context="", caller=None):
        seen.update(bot=bot.id, question=question, context=context, caller=caller)
        return {"id": "c1", "answer": "A", "refs": ["/r"], "used": ["m1"], "unsure": False, "extra": 1}

    monkeypatch.setattr(consult, "consult", fake)
    status, body = _json(client.post("/api/experts/consult", {
        "expert": "canvas expert", "question": " how? ", "context": "ctx", "run_id": run.name}))
    assert status == 200
    assert body == {"id": "c1", "answer": "A", "refs": ["/r"], "used": ["m1"], "unsure": False}
    assert seen["bot"] == BID and seen["question"] == "how?" and seen["context"] == "ctx"
    assert seen["caller"] == {"run_id": run.name, "session_id": "sess-9", "backend": "codex",
                              "target": str(clone), "canvas": ""}
    # By the manifest's shipped key too, and with no run id.
    status, _b = _json(client.post("/api/experts/consult", {"expert": "canvas-reference", "question": "q"}))
    assert status == 200 and seen["caller"]["run_id"] == "" and seen["caller"]["session_id"] == ""
    # A run id that tries to climb out is only an id.
    client.post("/api/experts/consult", {"expert": BID, "question": "q", "run_id": "../x"})
    assert seen["caller"]["session_id"] == "" and seen["caller"]["target"] == ""


def test_consult_errors(client, expert, monkeypatch):
    monkeypatch.setattr(consult, "consult", lambda *a, **k: pytest.fail("must not run"))
    status, body = _json(client.post("/api/experts/consult", {"expert": "nobody", "question": "q"}))
    assert status == 404 and "no such expert" in body["error"]
    status, body = _json(client.post("/api/experts/consult", {"expert": BID, "question": ""}))
    assert status == 400 and "question" in body["error"]
    status, body = _json(client.post("/api/experts/consult", {"question": "q"}))
    assert status == 400 and "expert" in body["error"]
    monkeypatch.setattr(model, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    status, body = _json(client.post("/api/experts/consult", {"expert": BID, "question": "q"}))
    assert status == 409 and body == {"error": "needs Claude, Codex or Copilot", "reason": "unavailable"}

    def raises(*a, **k):
        raise model.Unavailable("Codex is turned off in Preferences")

    monkeypatch.setattr(model, "available", lambda bot: (True, ""))
    monkeypatch.setattr(consult, "consult", raises)
    status, body = _json(client.post("/api/experts/consult", {"expert": BID, "question": "q"}))
    assert status == 409 and body["reason"] == "unavailable" and "Codex" in body["error"]


@pytest.mark.parametrize("path,body", [
    ("/api/experts/consult", {"expert": BID, "question": "q"}),
    (f"/api/experts/{BID}/memory/forget", {"id": "x"}),
    (f"/api/experts/{BID}/checks/run", {}),
    (f"/api/experts/{BID}/consolidate", {}),
    (f"/api/experts/{BID}/capture", {"session_id": "s"}),
    (f"/api/experts/{BID}/settings", {"engine": "auto"}),
])
def test_every_post_needs_x_fused(client, expert, path, body):
    status, _h, _b = client._do(__import__("urllib.request").request.Request(
        client.base + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"}))
    assert status == 403


def test_memory_and_forget(client, expert):
    item = memory.add(expert, "Use fused.load for UDF chaining.", trust="user")
    once = memory.add(expert, "Read canvas.toml first.", session="s1")
    status, body = _json(client.get(f"/api/experts/{BID}/memory"))
    assert status == 200 and [i["id"] for i in body["items"]] == [item["id"], once["id"]]
    assert [i["indexed"] for i in body["items"]] == [True, False]  # an outcome lesson seen once is not loaded
    assert item["id"] in body["index"] and once["id"] not in body["index"]
    assert [(i["contradicted"], i["contradicted_sessions"]) for i in body["items"]] == [(0, []), (0, [])]
    memory.contradict(expert, item["id"], session="s7")
    status, body = _json(client.get(f"/api/experts/{BID}/memory"))
    assert (body["items"][0]["contradicted"], body["items"][0]["contradicted_sessions"]) == (1, ["s7"])
    assert "(contradicted in 1 session(s); prefer newer lessons on this)" in body["index"]
    client.post(f"/api/experts/{BID}/memory/forget", {"id": once["id"]})
    status, body = _json(client.post(f"/api/experts/{BID}/memory/forget", {"id": item["id"]}))
    assert status == 200 and body == {"removed": 1}
    status, body = _json(client.post(f"/api/experts/{BID}/memory/forget", {}))
    assert status == 400
    status, body = _json(client.get("/api/experts/nobody/memory"))
    assert status == 404 and body["error"]


def test_observations_consults_proposals(client, expert, monkeypatch):
    from fused_render.experts import observations
    observations.write(expert, kind="user_correction", claim="c", trust="outcome")
    status, body = _json(client.get(f"/api/experts/{BID}/observations"))
    assert status == 200 and len(body["pending"]) == 1 and body["due"] is False
    monkeypatch.setattr(consult, "log", lambda bot, limit=100: [{"id": "a", "ts": 1}, {"id": "b", "ts": 2}])
    status, body = _json(client.get(f"/api/experts/{BID}/consults"))
    assert status == 200 and [c["id"] for c in body["consults"]] == ["b", "a"]
    monkeypatch.setattr(proposals, "listing", lambda bot: [{"id": "p1"}])
    assert _json(client.get(f"/api/experts/{BID}/proposals")) == (200, {"proposals": [{"id": "p1"}]})


def test_checks(client, expert, monkeypatch):
    status, body = _json(client.get(f"/api/experts/{BID}/checks"))
    assert status == 200 and body["latest"] is None and body["questions"] == 0
    assert body["status"]["running"] is False
    status, body = _json(client.post(f"/api/experts/{BID}/checks/run", {}))
    assert status == 400 and "no questions" in body["error"]
    monkeypatch.setattr(gate, "questions", lambda bot: [{"q": "Q?", "must": ["x"]}])
    monkeypatch.setattr(gate, "start", lambda bot: "r123")
    assert _json(client.post(f"/api/experts/{BID}/checks/run", {})) == (200, {"run_id": "r123"})


def test_consolidate_starts_in_the_background_and_returns_at_once(client, expert, monkeypatch):
    calls = []
    monkeypatch.setattr(consolidate, "run", lambda *a, **k: pytest.fail("never synchronous in a request"))
    monkeypatch.setattr(consolidate, "start", lambda bot, force=False: calls.append(force) or True)
    status, body = _json(client.post(f"/api/experts/{BID}/consolidate", {"force": True}))
    assert status == 200 and body == {"started": True} and calls == [True]
    monkeypatch.setattr(consolidate, "start", lambda bot, force=False: False)  # running, or nothing to do
    status, body = _json(client.post(f"/api/experts/{BID}/consolidate", None, raw=True))
    assert status == 200 and body == {"started": False}
    status, body = _json(client.post(f"/api/experts/{BID}/consolidate", {"force": "yes"}))
    assert status == 400
    monkeypatch.setattr(model, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    status, body = _json(client.post(f"/api/experts/{BID}/consolidate", {}))
    assert status == 409 and body["reason"] == "unavailable"


def test_propose_is_one_request_for_every_candidate(expert, monkeypatch):
    from fused_render.experts import routes
    calls = []
    monkeypatch.setattr(proposals, "candidates", lambda bot: [{"id": "m1"}, {"id": "m2"}])
    monkeypatch.setattr(proposals, "request", lambda bot, items, topic="", gap="": (
        calls.append([i["id"] for i in items]) or {"id": "p9", "status": "preparing"}))
    assert routes.propose(expert) == ["p9"] and calls == [["m1", "m2"]]

    def busy(bot, items, topic="", gap=""):
        raise ValueError(proposals.BUSY)
    monkeypatch.setattr(proposals, "request", busy)
    assert routes.propose(expert) == []


def test_proposals_list_every_status_newest_first(client, expert):
    for i, st in enumerate(proposals.STATUSES):
        proposals._save(expert, {"id": f"p{i}", "ts": 100 + i, "status": st, "items": [],
                                 **({"error": "it broke"} if st == "failed" else {})})
    status, body = _json(client.get(f"/api/experts/{BID}/proposals"))
    assert status == 200
    assert [p["status"] for p in body["proposals"]] == list(reversed(proposals.STATUSES))
    assert body["proposals"][0]["error"] == "it broke"
    assert proposals.STATUSES == ("preparing", "ready", "shown", "approved", "rejected", "failed")


def _run_dir(monkeypatch, tmp_path, roster):
    from fused_render.claude_agent import agent_module
    agent = agent_module()
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    run = tmp_path / "runs" / "20261116-111111-aaaaaa"
    run.mkdir(parents=True, exist_ok=True)
    if roster is not None:
        (run / "experts.json").write_text(json.dumps(roster))
    elif (run / "experts.json").exists():
        (run / "experts.json").unlink()
    return run.name


def test_consult_only_an_expert_offered_to_the_session(client, expert, monkeypatch, tmp_path):
    monkeypatch.setattr(consult, "consult", lambda bot, q, c="", caller=None: {
        "id": "c1", "answer": "A", "refs": [], "used": [], "unsure": False})
    cases = [([{"id": "someone-else", "name": "Canvas Expert"}], 403),
             ([{"id": BID, "name": "Renamed"}], 200),
             ([{"name": "canvas EXPERT"}], 200),
             ([{"name": "Other"}], 403),
             ([], 403),
             (None, 200)]
    for roster, want in cases:
        rid = _run_dir(monkeypatch, tmp_path, roster)
        status, body = _json(client.post("/api/experts/consult", {"expert": BID, "question": "q", "run_id": rid}))
        assert status == want, roster
        if want == 403:
            assert body == {"error": "that expert is not offered to this session"}
    # A local call (no run id) may consult any expert.
    _run_dir(monkeypatch, tmp_path, [])
    assert _json(client.post("/api/experts/consult", {"expert": BID, "question": "q"}))[0] == 200


def test_capture(client, expert, monkeypatch):
    sess = {"session_id": "s1", "path": "/t.jsonl"}
    monkeypatch.setattr(capture, "session_by_id", lambda sid: sess if sid == "s1" else None)
    monkeypatch.setattr(capture, "capture_session", lambda bot, s: [{"id": "o1", "session": s["session_id"]}])
    status, body = _json(client.post(f"/api/experts/{BID}/capture", {"session_id": "s1"}))
    assert status == 200 and body == {"observations": [{"id": "o1", "session": "s1"}]}
    status, body = _json(client.post(f"/api/experts/{BID}/capture", {"session_id": "nope"}))
    assert status == 404 and "nope" in body["error"]
    status, body = _json(client.post(f"/api/experts/{BID}/capture", {}))
    assert status == 400


def test_settings_and_engines(client, expert, monkeypatch):
    from fused_render.agent_backends import registry as backends
    monkeypatch.setattr(backends, "is_enabled", lambda bid: bid in ("claude", "copilot"))
    status, body = _json(client.post(f"/api/experts/{BID}/settings", {"engine": "copilot", "model": " gpt-5 "}))
    assert status == 200 and body["engine"] == "copilot" and body["model"] == "gpt-5"
    assert body["ask_me_about"] == "Writing canvases"  # the rest of the manifest is kept
    assert bstore.read_meta(BID)["expert"]["engine"] == "copilot"
    status, body = _json(client.post(f"/api/experts/{BID}/settings", {"engine": "codex"}))
    assert status == 400 and "turned off" in body["error"]
    status, body = _json(client.post(f"/api/experts/{BID}/settings", {"engine": "gemini"}))
    assert status == 400 and "engine must be one of" in body["error"]
    status, body = _json(client.post(f"/api/experts/{BID}/settings", {"model": 3}))
    assert status == 400

    status, body = _json(client.get(f"/api/experts/{BID}/engines"))
    assert status == 200 and [e["id"] for e in body] == ["claude", "codex", "copilot"]
    assert [e["enabled"] for e in body] == [True, False, True]
    for row in body:
        assert set(row) == {"id", "label", "enabled", "models", "default_model", "note"}
        assert all(set(m) == {"id", "label"} for m in row["models"])
    notes = {e["id"]: e["note"] for e in body}
    assert notes["claude"] == notes["copilot"] == "" and "outside its folders" in notes["codex"]
    assert _json(client.get("/api/experts/nobody/engines"))[0] == 404


def test_engines_report_the_claude_model_really_used(client, expert, monkeypatch):
    from fused_render.agent_backends import registry as backends
    monkeypatch.setattr(backends, "is_enabled", lambda bid: bid == "claude")
    monkeypatch.setattr(backends, "enabled_ids", lambda: ["claude"])

    def claude_default(bot_model, man_model=""):
        meta = bstore.read_meta(BID)
        meta["model"] = bot_model
        meta["expert"]["model"] = man_model
        bstore.write_meta(BID, meta)
        from fused_render.bots import registry
        b = registry.get(BID)
        b.meta["model"], b.meta["expert"] = bot_model, dict(meta["expert"])
        status, body = _json(client.get(f"/api/experts/{BID}/engines"))
        assert status == 200
        return body[0]["default_model"]

    assert claude_default("local-4b") == "sonnet"  # a local model never goes to Claude
    assert claude_default("gpt-5") == "sonnet"
    assert claude_default("opus") == "opus"
    assert claude_default("opus", "haiku") == "haiku"
    assert claude_default("") == "sonnet"
