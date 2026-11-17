"""experts core: store, model, readtools, memory, observations, consolidate,
gate, proposals (docs/experts.md §4). No real model, no network beyond a
loopback fake of `/api/ai`."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.experts import (consolidate, gate, memory, model, observations, proposals, readtools,
                                  store)


class FakeBot:
    SKILL_BODY_CAP = 6000

    def __init__(self, root, bid="b1", expert=None, name="Canvas Expert"):
        self.id = bid
        self.meta = {"kind": "expert", "name": name, "expert": expert or {}}
        self.public_dir = os.path.join(str(root), bid)
        self.dir = os.path.join(self.public_dir, ".fused")
        os.makedirs(self.dir, exist_ok=True)
        self.ai_calls = []

    @property
    def skills_dir(self):
        return os.path.join(self.public_dir, "skills")

    def skills(self):
        out = []
        if not os.path.isdir(self.skills_dir):
            return out
        for n in sorted(os.listdir(self.skills_dir)):
            if not n.endswith(".md"):
                continue
            with open(os.path.join(self.skills_dir, n), encoding="utf-8") as f:
                lines = f.read().split("\n")
            title = lines[0][2:].strip()
            trigger = lines[1].split(":", 1)[1].strip()
            out.append({"name": n[:-3], "title": title, "trigger": trigger,
                        "body": "\n".join(lines[2:]).strip()})
        return out

    def skill_save(self, title, trigger, body, name=None):
        if not trigger:
            raise ValueError("a playbook needs at least one trigger word or phrase")
        if not body:
            raise ValueError("a playbook needs steps")
        name = re.sub(r"[^a-z0-9]+", "-", (name or title).lower()).strip("-")[:60]
        os.makedirs(self.skills_dir, exist_ok=True)
        with open(os.path.join(self.skills_dir, name + ".md"), "w", encoding="utf-8") as f:
            f.write(f"# {title}\ntrigger: {trigger}\n\n{body}\n")
        return name

    def _ai_call(self, ai, prompt, **kw):
        self.ai_calls.append((prompt, kw))
        return ai.text(prompt, **kw)


@pytest.fixture
def bot(tmp_path):
    return FakeBot(tmp_path)


@pytest.fixture
def fake_text(monkeypatch):
    """`model.text` answered by a queue (or a function) of replies."""
    calls = []
    replies = []

    def text(b, prompt, *, system="", timeout=180):
        calls.append(prompt)
        if not replies:
            raise AssertionError("unexpected model call")
        r = replies.pop(0)
        return r(prompt) if callable(r) else r

    monkeypatch.setattr(model, "text", text)
    monkeypatch.setattr(model, "available", lambda b: (True, ""))
    # Gap repair's research run (consolidate step c): nothing found.
    monkeypatch.setattr(model, "tool_turn", lambda b, prompt, **k: '{"found": false, "answer": "", "refs": [], '
                                                                  '"why": "nothing"}')
    text.calls, text.replies = calls, replies
    return text


# ------------------------------------------------------------------- store

def test_manifest_defaults_and_is_expert(bot):
    m = store.manifest(bot)
    assert m["engine"] == "auto" and m["model"] == ""
    assert m["memory"] == {"consolidate_at": 3, "consolidate_after_s": 86400, "propose_at": 3,
                           "index_lines": 200, "limit": 50, "tidy_at": 30, "stale_days": 14, "rare_uses": 2,
                           "heavy_uses": 5}
    assert "user_correction" in m["learns_from"] and m["reads"] == []
    bot.meta["expert"] = {"engine": "codex", "memory": {"propose_at": 5, "index_lines": "x"},
                          "shipped": "canvas-reference"}
    m = store.manifest(bot)
    assert m["engine"] == "codex" and m["memory"]["propose_at"] == 5 and m["memory"]["index_lines"] == 200
    m["memory"]["propose_at"] = 9
    assert bot.meta["expert"]["memory"]["propose_at"] == 5  # a copy
    assert store.is_expert(bot) and store.is_expert({"kind": "expert"}) and not store.is_expert({"kind": "bot"})
    assert store.expert_dir(bot) == os.path.join(bot.dir, "expert") and os.path.isdir(store.expert_dir(bot))
    assert store.lock(bot) is store.lock(bot)
    assert re.fullmatch(r"m[0-9a-f]{8}", store.new_id("m"))


def test_json_helpers_and_find(tmp_path, monkeypatch):
    p = str(tmp_path / "x" / "a.json")
    assert store.read_json(p, {"d": 1}) == {"d": 1}
    store.write_json(p, {"a": 1})
    assert store.read_json(p, None) == {"a": 1}
    assert [n for n in os.listdir(tmp_path / "x")] == ["a.json"]
    jl = str(tmp_path / "l.jsonl")
    for i in range(5):
        store.append_jsonl(jl, {"i": i})
    with open(jl, "a") as f:
        f.write("not json\n")
    assert [r["i"] for r in store.read_jsonl(jl)] == [0, 1, 2, 3, 4]
    assert [r["i"] for r in store.read_jsonl(jl, limit=2)] == [3, 4]

    a = FakeBot(tmp_path, "a1", name="Canvas Expert", expert={"shipped": "canvas-reference"})
    b = FakeBot(tmp_path, "b2", name="Other")
    b.meta["kind"] = "bot"
    from fused_render.bots import registry
    monkeypatch.setattr(registry, "all", lambda: [a, b])
    assert store.all_experts() == [a]
    assert store.find("a1") is a and store.find("canvas expert") is a
    assert store.find("canvas-reference") is a and store.find("Other") is None


# ------------------------------------------------------------------- model

class _B:
    def __init__(self, bid, default_model):
        self.id, self.default_model = bid, default_model


def _backends(monkeypatch, enabled, default):
    from fused_render.agent_backends import registry as reg
    monkeypatch.setattr(reg, "enabled_ids", lambda: list(enabled))
    monkeypatch.setattr(reg, "is_enabled", lambda b: b in enabled)
    monkeypatch.setattr(reg, "default_id", lambda: default)
    monkeypatch.setattr(reg, "get", lambda b: _B(b, {"claude": "sonnet", "codex": "gpt-x",
                                                     "copilot": "cp-1"}.get(b, "")))


def test_provider_for_auto_resolution(bot, monkeypatch):
    _backends(monkeypatch, ["claude", "copilot", "codex"], "codex")
    assert model.provider_for(bot) == ("claude", "sonnet")  # auto never picks Codex, even as the default
    _backends(monkeypatch, ["gemini", "copilot", "codex"], "codex")
    assert model.provider_for(bot) == ("copilot", "cp-1")  # no Claude: Copilot
    _backends(monkeypatch, ["codex"], "codex")
    with pytest.raises(model.Unavailable, match="Codex runs an expert only when chosen"):
        model.provider_for(bot)
    assert model.fenced(bot) is False  # unavailable: not fenced
    bot.meta["expert"] = {"engine": "codex"}
    assert model.provider_for(bot) == ("codex", "gpt-x")  # named explicitly: allowed
    assert model.fenced(bot) is False
    bot.meta["expert"] = {}
    _backends(monkeypatch, ["claude"], "claude")
    assert model.fenced(bot) is True
    bot.meta["model"] = "opus"
    assert model.provider_for(bot) == ("claude", "opus")  # the bot's own model meta
    for local_or_foreign in ("local-4b", "gpt-5", "mlx-community/gemma"):
        bot.meta["model"] = local_or_foreign
        assert model.provider_for(bot) == ("claude", "sonnet"), local_or_foreign
    bot.meta["model"] = "claude-opus-4-1"
    assert model.provider_for(bot) == ("claude", "claude-opus-4-1")
    bot.meta["expert"] = {"model": "haiku"}
    assert model.provider_for(bot) == ("claude", "haiku")
    bot.meta["expert"] = {"engine": "codex"}
    with pytest.raises(model.Unavailable, match="Codex is turned off"):
        model.provider_for(bot)
    _backends(monkeypatch, ["gemini"], "gemini")
    bot.meta["expert"] = {}
    with pytest.raises(model.Unavailable):
        model.provider_for(bot)
    assert model.available(bot) == (False, "needs Claude, Codex or Copilot")
    _backends(monkeypatch, ["claude"], "claude")
    assert model.available(bot) == (True, "")
    assert model.ENGINE_NOTES["codex"].startswith("On Codex the expert can read files outside its folders")


def test_text_goes_through_ai_call(bot, monkeypatch):
    _backends(monkeypatch, ["codex"], "codex")
    bot.meta["expert"] = {"engine": "codex"}

    class FakeAI:
        @staticmethod
        def text(prompt, **kw):
            return "ok:" + prompt

    from fused_render.bots import bot as botmod
    monkeypatch.setattr(botmod, "_fused_ai", lambda: FakeAI)
    assert model.text(bot, "hi", system="sys") == "ok:hi"
    (_, kw), = bot.ai_calls
    assert kw["provider"] == "codex" and kw["model"] == "gpt-x" and kw["system_prompt"] == "sys"


class _FakeAI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    state: dict = {}

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):  # noqa: N802
        st = self.state
        body = self._body()
        if self.path.startswith("/api/ai/turns/"):
            st["results"].append((self.path, body, self.headers.get("X-Fused")))
            st["answered"].set()
            out = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        st["request"] = (body, self.headers.get("X-Fused"))
        mode = st.get("mode")
        if mode == "http_error":
            out = json.dumps({"ok": False, "error": {"type": "bad_request",
                                                     "message": "Codex is turned off"}}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/x-ndjson")
        self.end_headers()

        def frame(f):
            self.wfile.write((json.dumps(f) + "\n").encode())
            self.wfile.flush()

        frame({"type": "turn", "id": "turn_abc"})
        frame({"type": "chunk", "text": "thinking "})
        for i, call in enumerate(st.get("calls", [])):
            st["answered"].clear()
            frame({"type": "tool_call", "id": f"call_{i}", "name": call[0], "input": call[1]})
            st["answered"].wait(5)
        if mode == "error_done":
            frame({"type": "done", "ok": False, "error": {"type": "ai_error", "message": "the CLI fell over"}})
        elif mode == "no_done":
            pass
        else:
            frame({"type": "chunk", "text": "final"})
            frame({"type": "done", "ok": True, "result": {"text": "the final answer"}})


@pytest.fixture
def fake_server(monkeypatch):
    _FakeAI.state = {"results": [], "answered": threading.Event()}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", f"http://127.0.0.1:{srv.server_address[1]}")
    _backends(monkeypatch, ["claude"], "claude")
    yield _FakeAI.state
    srv.shutdown()
    srv.server_close()


def test_tool_turn_streams_and_posts_results(bot, fake_server):
    fake_server["calls"] = [("read_file", {"path": "/x"}), ("search", {"query": "boom"})]
    seen = []

    def handler(name, inp):
        seen.append((name, inp))
        if name == "search":
            raise RuntimeError("kaboom")
        return "file text", False

    out = model.tool_turn(bot, "q?", system="be good", tools=readtools.specs(), handler=handler, timeout=20)
    assert out == "the final answer"
    body, xf = fake_server["request"]
    assert xf == "1" and body["stream"] is True and body["provider"] == "claude"
    assert body["model"] == "sonnet" and body["systemPrompt"] == "be good"
    assert [t["name"] for t in body["tools"]] == ["read_file", "list_dir", "search"]
    assert seen == [("read_file", {"path": "/x"}), ("search", {"query": "boom"})]
    (p1, r1, h1), (p2, r2, _) = fake_server["results"]
    assert p1 == "/api/ai/turns/turn_abc/results" and h1 == "1"
    assert r1 == {"id": "call_0", "content": "file text", "isError": False}
    assert r2["id"] == "call_1" and r2["isError"] is True and "kaboom" in r2["content"]


def test_tool_turn_errors(bot, fake_server):
    for mode, said in (("http_error", "Codex is turned off"), ("error_done", "the CLI fell over"),
                       ("no_done", "ended without an answer")):
        fake_server["mode"] = mode
        with pytest.raises(RuntimeError, match=said):
            model.tool_turn(bot, "q", system="", tools=readtools.specs(),
                            handler=lambda n, i: ("", False), timeout=20)


# --------------------------------------------------------------- readtools

def test_readtools_fence_and_tools(tmp_path):
    root = tmp_path / "root"
    (root / "sub" / ".git").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "a.py").write_text("print('hello canvas')\n")
    (root / "sub" / "udf_canvas.py").write_text("x = 1\nHELLO world\n")
    (root / "sub" / ".git" / "hello.txt").write_text("hello\n")
    (root / "node_modules" / "hello.js").write_text("hello\n")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "s.txt").write_text("hello secret\n")
    os.symlink(outside / "s.txt", root / "link.txt")
    os.symlink(outside, root / "linkdir")
    events = []
    h = readtools.make_handler([str(root)], on_tool=lambda n, i, r: events.append(n))

    text, err = h("read_file", {"path": str(root / "a.py")})
    assert not err and "hello canvas" in text
    text, err = h("read_file", {"path": "sub/udf_canvas.py"})  # relative to the only root
    assert not err and "HELLO world" in text
    text, err = h("read_file", {"path": str(outside / "s.txt")})
    assert err and text.startswith("not allowed")
    text, err = h("read_file", {"path": str(root / "link.txt")})
    assert err and text.startswith("not allowed")
    text, err = h("read_file", {"path": str(root / ".." / "secret" / "s.txt")})
    assert err and text.startswith("not allowed")
    text, err = h("list_dir", {"path": str(root / "linkdir")})
    assert err and text.startswith("not allowed")

    text, err = h("list_dir", {})
    assert not err and str(root.resolve()) in text
    text, err = h("list_dir", {"path": str(root)})
    assert not err and "sub/" in text and "a.py" in text and "link.txt" not in text

    text, err = h("search", {"query": "canvas"})
    assert not err and "udf_canvas.py" in text and "a.py" not in text
    text, err = h("search", {"query": "hello", "content": True})
    assert not err and "a.py:1" in text and "udf_canvas.py:2" in text
    assert ".git" not in text and "node_modules" not in text and "secret" not in text
    text, err = h("search", {"query": "hello", "content": True, "glob": "*.py", "path": str(root / "sub")})
    assert "udf_canvas.py" in text and "a.py" not in text
    text, err = h("search", {"query": "x", "path": str(outside)})
    assert err and text.startswith("not allowed")
    assert h("nope", {}) == ("unknown tool: nope", True)
    text, err = h("read_file", {})
    assert err
    assert h("read_file", None)[1] is True  # bad input: an error, not a raise
    assert len(events) == 15


def test_readtools_caps(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    for i in range(260):
        (root / f"f{i:03}.txt").write_text("needle\n" * 2)
    (root / "big.txt").write_text("x" * 50_000)
    h = readtools.make_handler([str(root)])
    text, _ = h("list_dir", {"path": str(root)})
    assert len(text.splitlines()) == 1 + 200 + 1 and "more" in text.splitlines()[-1]
    text, _ = h("search", {"query": "f"})
    assert text.startswith("50 hits") and "first 50" in text
    text, _ = h("search", {"query": "needle", "content": True})
    assert text.startswith("50 hits")
    text, _ = h("read_file", {"path": "big.txt"})
    assert len(text) < 23_000
    monkeypatch.setattr(readtools, "SEARCH_FILE_MAX", 10)
    text, _ = h("search", {"query": "needle", "content": True})
    assert text.startswith("no line")
    deep = root / "d1" / "d2" / "d3" / "d4" / "d5" / "d6" / "d7" / "d8" / "d9"
    deep.mkdir(parents=True)
    (deep / "deepfile.txt").write_text("x")
    text, _ = h("search", {"query": "deepfile"})
    assert text.startswith("no file")


def test_resolve_reads(tmp_path, monkeypatch):
    base = tmp_path / "canvas"
    for env, scopes in (("prod", ("user", "team")), ("unstable", ("public",)), (".sync", ("user",))):
        for s in scopes:
            (base / env / s).mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(base))
    folder = tmp_path / "docs"
    folder.mkdir()
    got = readtools.resolve_reads({"reads": [
        {"kind": "canvas-mirror", "scopes": ["team", "public", "user"]},
        {"kind": "folder", "path": str(folder)},
        {"kind": "folder", "path": str(tmp_path / "missing")},
        {"kind": "folder", "path": "relative"}]})
    assert got == [str((base / "prod" / "team").resolve()), str((base / "prod" / "user").resolve()),
                   str((base / "unstable" / "public").resolve()), str(folder.resolve())]
    assert readtools.resolve_reads({}) == []


# ------------------------------------------------------------------ memory

def test_memory_add_dedupe_bump_forget_index(bot):
    a = memory.add(bot, "Use  fused.load for UDFs.", session="s1")
    assert a["count"] == 1 and a["sessions"] == ["s1"] and a["trust"] == "outcome"
    b = memory.add(bot, "use fused.load for udfs", session="s1")  # same session: no count
    assert b["id"] == a["id"] and b["count"] == 1
    b = memory.add(bot, "USE fused.load for UDFs!", session="s2", trust="user")
    assert b["count"] == 2 and b["sessions"] == ["s1", "s2"] and b["trust"] == "user"
    assert memory.bump(bot, a["id"], session="s2")["count"] == 2
    assert memory.bump(bot, a["id"])["count"] == 3  # empty session always counts
    assert memory.bump(bot, "nope") is None
    c = memory.add(bot, "Canvases live under user/")
    with pytest.raises(ValueError):
        memory.add(bot, "   ")
    assert [i["id"] for i in memory.items(bot)] == [a["id"], c["id"]]
    # c is an outcome lesson seen once: kept, but not loaded into every answer until a second session sees it.
    assert memory.index_text(bot).splitlines() == [f"- [{a['id']}] Use fused.load for UDFs. (seen 3×)"]
    assert not memory.indexed(memory.items(bot)[1])
    memory.add(bot, "Canvases live under user/", session="s7")
    assert memory.index_text(bot).splitlines() == [
        f"- [{a['id']}] Use fused.load for UDFs. (seen 3×)", f"- [{c['id']}] Canvases live under user/ (seen 2×)"]
    bot.meta["expert"] = {"memory": {"index_lines": 1}}
    assert len(memory.index_text(bot).splitlines()) == 1
    assert memory.mark(bot, c["id"], proposed=True, bogus=1)["proposed"] is True
    assert "bogus" not in memory.items(bot)[1]
    assert memory.forget(bot, c["id"]) == 1
    assert memory.forget(bot, "use fused.load for udfs") == 1
    assert memory.items(bot) == []
    memory.add(bot, "alpha lesson one")
    memory.add(bot, "alpha lesson two")
    assert memory.forget(bot, "alpha") == 2 and memory.forget(bot, "zzz") == 0


# ------------------------------------------------------------ observations

def test_observations_write_due_archive(bot):
    assert not observations.due(bot)
    o1 = observations.write(bot, kind="user_correction", claim="x", trust="outcome", session="s")
    time.sleep(0.01)
    o2 = observations.write(bot, kind="gap", claim="y", trust="gap", consult="c1")
    assert [o["id"] for o in observations.pending(bot)] == [o1["id"], o2["id"]]
    assert not observations.due(bot)
    assert observations.due(bot, now=time.time() + 86400 + 5)  # by age
    observations.write(bot, kind="unanswered", claim="z", trust="outcome")
    assert observations.due(bot)  # by count
    observations.archive(bot, [o1["id"], "../evil"])
    assert len(observations.pending(bot)) == 2
    assert [o["id"] for o in observations.done(bot)] == [o1["id"]]


# ------------------------------------------------------------- consolidate

def test_consolidate_applies_parsed_edits(bot, fake_text):
    old = memory.add(bot, "existing lesson")
    o1 = observations.write(bot, kind="user_correction", claim="new thing learned", trust="outcome", session="s9")
    o2 = observations.write(bot, kind="error_then_fix", claim="existing again", trust="outcome", session="s9")
    o3 = observations.write(bot, kind="unanswered", claim="noise", trust="outcome")
    g = observations.write(bot, kind="gap", claim="could not answer X", trust="gap")
    fake_text.replies.append(lambda p: "Sure!\n```json\n" + json.dumps([
        {"obs": o1["id"], "op": "add", "claim": "A crisp new lesson."},
        {"obs": o2["id"], "op": "bump", "item": old["id"]},
        {"obs": o3["id"], "op": "nothing"},
        {"obs": g["id"], "op": "add", "claim": "gap must not land"}]) + "\n```")
    r = consolidate.run(bot)
    assert r["ran"] is True and len(r["added"]) == 1 and r["bumped"] == [old["id"]] and r["ignored"] == 2
    claims = {i["claim"]: i for i in memory.items(bot)}
    assert set(claims) == {"existing lesson", "A crisp new lesson."}
    assert claims["existing lesson"]["count"] == 2
    assert "could not answer X" not in fake_text.calls[0]  # gaps are not offered as lessons
    assert observations.pending(bot) == [] and len(observations.done(bot)) == 4


def test_consolidate_unparsable_keeps_pending_and_falls_back_after_three(bot, fake_text):
    assert consolidate.run(bot) == {"ran": False, "added": [], "bumped": [], "ignored": 0}
    memory.add(bot, "Lesson A")
    observations.write(bot, kind="user_correction", claim="lesson a.", trust="outcome", session="s1")
    assert consolidate.run(bot)["ran"] is False  # 1 < consolidate_at, young
    assert fake_text.calls == []
    observations.write(bot, kind="user_correction", claim="Lesson B", trust="outcome")
    observations.write(bot, kind="gap", claim="gap", trust="gap")
    # 1st failure: an unreadable answer adds nothing and leaves everything pending.
    fake_text.replies.append("I could not decide, sorry [not json")
    r = consolidate.run(bot)
    assert r["ran"] is False and "could not be read" in r["reason"] and "1 of 3" in r["reason"]
    assert [i["claim"] for i in memory.items(bot)] == ["Lesson A"] and len(observations.pending(bot)) == 3
    assert consolidate.failures(bot) == 1
    # 2nd: a failing call, same.
    fake_text.replies.append(lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    r = consolidate.run(bot)
    assert r["ran"] is False and "down" in r["reason"] and consolidate.failures(bot) == 2
    assert [i["claim"] for i in memory.items(bot)] == ["Lesson A"]
    # 3rd consecutive: exact-match fallback, then the counter resets.
    fake_text.replies.append("still nothing")
    r = consolidate.run(bot)
    assert r["ran"] and len(r["added"]) == 1 and len(r["bumped"]) == 1 and r["ignored"] == 1
    assert sorted(i["claim"] for i in memory.items(bot)) == ["Lesson A", "Lesson B"]
    assert observations.pending(bot) == [] and consolidate.failures(bot) == 0


def test_consolidate_unknown_ops_and_omissions_are_nothing(bot, fake_text):
    old = memory.add(bot, "existing")
    o1 = observations.write(bot, kind="user_correction", claim="one", trust="outcome")
    o2 = observations.write(bot, kind="user_correction", claim="two", trust="outcome")
    o3 = observations.write(bot, kind="user_correction", claim="three", trust="outcome")
    observations.write(bot, kind="user_correction", claim="omitted", trust="outcome")
    fake_text.replies.append(json.dumps([{"obs": o1["id"], "op": "rewrite", "claim": "x"},
                                         {"obs": o2["id"], "op": "bump", "item": "mnope"},
                                         {"obs": o3["id"], "op": "bump", "item": old["id"]}]))
    r = consolidate.run(bot, force=True)
    assert r["ran"] and r["added"] == [] and r["bumped"] == [old["id"]] and r["ignored"] == 3
    assert [i["claim"] for i in memory.items(bot)] == ["existing"]
    assert observations.pending(bot) == [] and len(observations.done(bot)) == 4


def test_consolidate_bumps_confirmed_lessons_once_per_session_without_the_model(bot, fake_text):
    lesson = memory.add(bot, "Users want a Parquet loader node.", kind="repeated_request", session="s1")

    def confirm(session):
        return observations.write(bot, kind="repeated_request", claim="Users want a Parquet loader node.",
                                  trust="outcome", session=session, confirms=lesson["id"])

    seen = [confirm("s2"), confirm("s2"), confirm("s3"), confirm("s1")]
    r = consolidate.run(bot, force=True)
    assert fake_text.calls == []  # only confirmations: no model decision
    assert r["ran"] and r["added"] == [] and r["bumped"] == [lesson["id"]] and r["ignored"] == 0
    item = memory.items(bot)[0]
    assert item["count"] == 3 and item["sessions"] == ["s1", "s2", "s3"]  # s2 and s1 again count once
    done = {o["id"]: o for o in observations.done(bot)}
    assert all(done[o["id"]]["outcome"]["op"] == "bump" and done[o["id"]]["outcome"]["item"] == lesson["id"]
               for o in seen)


def test_consolidate_sends_unconfirmed_observations_to_the_model(bot, fake_text):
    lesson = memory.add(bot, "Known lesson.")
    c = observations.write(bot, kind="user_correction", claim="Known lesson.", trust="outcome", session="s5",
                           confirms=lesson["id"])
    stale = observations.write(bot, kind="user_correction", claim="Stale one.", trust="outcome", session="s5",
                               confirms="mgone")
    assert c["confirms"] == lesson["id"] and "confirms" not in stale  # write validates the id
    new = observations.write(bot, kind="repeated_request", claim="Asked once.", trust="outcome", session="s5")
    fake_text.replies.append(lambda p: json.dumps([{"obs": new["id"], "op": "add"},
                                                   {"obs": stale["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)
    assert c["id"] not in fake_text.calls[0] and new["id"] in fake_text.calls[0] and stale["id"] in fake_text.calls[0]
    assert r["bumped"] == [lesson["id"]] and len(r["added"]) == 2
    kinds = {i["claim"]: i["kind"] for i in memory.items(bot)}
    assert kinds == {"Known lesson.": "lesson", "Asked once.": "repeated_request", "Stale one.": "user_correction"}


@pytest.mark.parametrize("kind", ["repeated_request", "user_correction", "error_then_fix", "advice_confirmed",
                                  "advice_contradicted"])
def test_consolidate_add_stores_the_observation_kind(bot, fake_text, kind):
    o = observations.write(bot, kind=kind, claim=f"A {kind} lesson.", trust="outcome", session="s1")
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    assert [(i["claim"], i["kind"]) for i in memory.items(bot)] == [(f"A {kind} lesson.", kind)]


def test_a_request_pattern_is_never_bumped_onto_a_non_request_lesson(bot, fake_text):
    naming = memory.add(bot, "A UDF node that loads data is named load_<thing>; a chart node plot_<thing>.",
                        kind="user_correction", session="s0")
    req = memory.add(bot, "The user asks for a CSV export node.", kind="repeated_request", session="s0")
    o1 = observations.write(bot, kind="repeated_request", trust="outcome", session="s1",
                            claim="The user asks for a Parquet loader node wired to a monthly-revenue chart node.")
    o2 = observations.write(bot, kind="repeated_request", claim="The user asks for a CSV export node.",
                            trust="outcome", session="s1")
    o3 = observations.write(bot, kind="user_correction", claim="Name loaders load_<thing>.",
                            trust="outcome", session="s1")
    o4 = observations.write(bot, kind="repeated_request", claim="The user asks for a map node.",
                            trust="outcome", session="s1", confirms=naming["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": o1["id"], "op": "bump", "item": naming["id"]},
                                                   {"obs": o2["id"], "op": "bump", "item": req["id"]},
                                                   {"obs": o3["id"], "op": "bump", "item": naming["id"]},
                                                   {"obs": o4["id"], "op": "bump", "item": naming["id"]}]))
    r = consolidate.run(bot, force=True)
    prompt = fake_text.calls[0]
    assert "SAME lesson" in prompt and "When in doubt" in prompt and "may only be bumped onto" in prompt
    assert f"[{naming['id']}] (user_correction)" in prompt
    assert o4["id"] in prompt  # a request pattern "confirming" a non-request lesson goes to the model
    items = {i["id"]: i for i in memory.items(bot)}
    assert items[naming["id"]]["count"] == 2 and items[req["id"]]["count"] == 2  # o3 and o2 bumped
    new = [i for i in items.values() if i["id"] not in (naming["id"], req["id"])]
    assert sorted(i["claim"] for i in new) == ["The user asks for a Parquet loader node wired to a "
                                               "monthly-revenue chart node.", "The user asks for a map node."]
    assert all(i["kind"] == "repeated_request" and i["trust"] == "outcome" for i in new)
    assert sorted(r["bumped"]) == sorted([naming["id"], req["id"]]) and len(r["added"]) == 2
    done = {o["id"]: o["outcome"] for o in observations.done(bot)}
    assert done[o1["id"]]["op"] == "add" and done[o2["id"]] == {**done[o2["id"]], "op": "bump", "item": req["id"]}


# ---------------------------------------------------------- contradictions

def test_memory_contradict_counts_once_per_session_and_marks_the_index(bot):
    a = memory.add(bot, "Show monthly revenue as a bar chart.", trust="user")
    assert a["contradicted"] == 0 and a["contradicted_sessions"] == []
    assert memory.contradict(bot, "mnope", session="s1") is None
    for s in ("s1", "s1", "s2", ""):
        it = memory.contradict(bot, a["id"], session=s)
    assert it["contradicted"] == 3 and it["contradicted_sessions"] == ["s1", "s2"]
    assert it["count"] == 1  # never a bump
    assert memory.index_text(bot) == (f"- [{a['id']}] Show monthly revenue as a bar chart. (seen 1×) "
                                      "(contradicted in 3 session(s); prefer newer lessons on this)")
    # An item stored before the field existed reads as never contradicted.
    assert memory.contradicted({"id": "mold"}) == 0


def test_observation_contradicts_is_validated_like_confirms(bot):
    lesson = memory.add(bot, "Known lesson.")
    o = observations.write(bot, kind="advice_contradicted", claim="Corrected.", trust="outcome",
                           contradicts=lesson["id"], by_user=True)
    stale = observations.write(bot, kind="advice_contradicted", claim="Corrected.", trust="outcome",
                               contradicts="mgone")
    assert o["contradicts"] == lesson["id"] and o["by_user"] is True
    assert "contradicts" not in stale and "by_user" not in stale
    observations.archive(bot, [o["id"]], {o["id"]: {"op": "contradict", "item": lesson["id"], "new": "mx"}})
    assert observations.done(bot)[0]["outcome"] == {**observations.done(bot)[0]["outcome"], "op": "contradict",
                                                    "item": lesson["id"], "new": "mx"}


BAR = "For a sales chart always show monthly revenue as a bar chart, never a line chart."
REGIONAL = ("For regional sales canvases use a line chart with one line per region; bar charts are only for the "
            "company-wide Sales Dashboard.")


def test_consolidate_live_case_a_contradiction_never_bumps_the_lesson_it_contradicts(bot, fake_text):
    """The live run: lesson m64e724f4 (count 2) was contradicted by the user; it used to be bumped to 3."""
    old = memory.add(bot, BAR, session="s0")
    memory.bump(bot, old["id"], session="s1")
    assert memory.items(bot)[0]["count"] == 2
    contra = observations.write(bot, kind="advice_contradicted", claim=REGIONAL, trust="outcome", session="live",
                                contradicts=old["id"], by_user=True, about=["charts"])
    corr = observations.write(bot, kind="user_correction", claim=REGIONAL, trust="outcome", session="live",
                              about=["charts"])
    fake_text.replies.append(lambda p: json.dumps([
        {"obs": contra["id"], "op": "same", "as": corr["id"]},
        {"obs": corr["id"], "op": "add", "claim": REGIONAL}]))
    r = consolidate.run(bot, force=True)
    prompt = fake_text.calls[0]
    assert contra["id"] in prompt and '"same"' in prompt and "never" in prompt
    items = {i["id"]: i for i in memory.items(bot)}
    assert items[old["id"]]["count"] == 2  # unchanged
    assert items[old["id"]]["contradicted"] == 1 and items[old["id"]]["contradicted_sessions"] == ["live"]
    new = [i for i in items.values() if i["id"] != old["id"]]
    assert [(i["claim"], i["kind"], i["count"]) for i in new] == [(REGIONAL, "user_correction", 1)]  # added once
    assert r["ran"] and r["added"] == [new[0]["id"]] and old["id"] not in r["bumped"]
    assert r["contradicted"] == [old["id"]]
    done = {o["id"]: o["outcome"] for o in observations.done(bot)}
    assert {k: done[contra["id"]][k] for k in ("op", "item", "new")} == {
        "op": "contradict", "item": old["id"], "new": new[0]["id"]}
    assert done[corr["id"]]["op"] == "add"
    # The contradicted lesson is flagged in every answer and never proposed as a skill fix.
    assert "(contradicted in 1 session(s); prefer newer lessons on this)" in memory.index_text(bot)
    memory.bump(bot, old["id"], session="s9")
    assert old["id"] not in [i["id"] for i in proposals.candidates(bot)]


def test_consolidate_a_model_bump_of_a_contradiction_is_an_add(bot, fake_text):
    old = memory.add(bot, BAR, session="s0")
    other = memory.add(bot, "Some other lesson.", session="s0")
    contra = observations.write(bot, kind="advice_contradicted", claim=REGIONAL, trust="outcome", session="live",
                                contradicts=old["id"])
    loose = observations.write(bot, kind="advice_contradicted", claim="Never trust X.", trust="outcome",
                               session="live")
    corr = observations.write(bot, kind="user_correction", claim="Bar charts are fine.", trust="outcome",
                              session="live")
    fake_text.replies.append(lambda p: json.dumps([
        {"obs": contra["id"], "op": "bump", "item": old["id"]},
        {"obs": loose["id"], "op": "bump", "item": other["id"]},
        {"obs": corr["id"], "op": "bump", "item": old["id"]}]))  # bumps a lesson this batch contradicts
    r = consolidate.run(bot, force=True)
    items = {i["claim"]: i for i in memory.items(bot)}
    assert items[BAR]["count"] == 1 and items[BAR]["contradicted"] == 1
    assert items["Some other lesson."]["count"] == 1
    assert items[REGIONAL]["kind"] == "advice_contradicted"  # no USER line said so
    assert items["Never trust X."]["kind"] == "advice_contradicted"
    assert items["Bar charts are fine."]["kind"] == "user_correction"
    assert r["bumped"] == [] and len(r["added"]) == 3
    done = {o["id"]: o["outcome"] for o in observations.done(bot)}
    assert done[contra["id"]]["op"] == "contradict" and done[contra["id"]]["new"] == items[REGIONAL]["id"]
    assert done[loose["id"]]["op"] == "add"


def test_consolidate_a_contradiction_the_model_drops_still_counts(bot, fake_text):
    old = memory.add(bot, BAR, session="s0")
    contra = observations.write(bot, kind="advice_contradicted", claim=BAR, trust="outcome", session="live",
                                contradicts=old["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": contra["id"], "op": "add"}]))  # the old text itself
    r = consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert len(memory.items(bot)) == 1 and item["count"] == 1 and item["contradicted"] == 1
    assert r["ignored"] == 0 and r["added"] == [] and r["bumped"] == []
    out = observations.done(bot)[0]["outcome"]
    assert out["op"] == "contradict" and out["item"] == old["id"] and "new" not in out


def test_consolidate_without_an_engine_does_nothing(bot, fake_text, monkeypatch):
    monkeypatch.setattr(model, "available", lambda b: (False, "needs Claude, Codex or Copilot"))
    observations.write(bot, kind="user_correction", claim="one", trust="outcome")
    r = consolidate.run(bot, force=True)
    assert r["ran"] is False and r["reason"] == "needs Claude, Codex or Copilot"
    assert memory.items(bot) == [] and len(observations.pending(bot)) == 1 and fake_text.calls == []


# -------------------------------------------------------------------- gate

QUESTIONS = """# Checks

## How do I read one UDF in another?
- must: mentions referencing the upstream UDF by name
- must: points at a real canvas file

## Where do canvases live?
* Must: names the user folder
"""


def _grader(prompt):
    return json.dumps({"passed": "GOOD" in prompt, "why": "checked"})


def test_gate_questions_run_status_latest(bot, fake_text):
    assert gate.questions(bot) == []
    with open(os.path.join(bot.public_dir, "questions.md"), "w") as f:
        f.write(QUESTIONS)
    qs = gate.questions(bot)
    assert qs == [{"q": "How do I read one UDF in another?",
                   "must": ["mentions referencing the upstream UDF by name", "points at a real canvas file"]},
                  {"q": "Where do canvases live?", "must": ["names the user folder"]}]
    fake_text.replies.extend([_grader] * 2)
    seen = []

    def answer(b, q, override):
        seen.append(override)
        return "GOOD answer" if "UDF" in q else "meh"

    r = gate.run(bot, answer_fn=answer)
    assert (r["score"], r["total"]) == (1, 2) and seen == [None, None]
    assert [x["passed"] for x in r["results"]] == [True, False] and r["results"][0]["answer"] == "GOOD answer"
    assert gate.latest(bot)["id"] == r["id"]
    # skills_override: never persisted
    fake_text.replies.extend([_grader] * 2)
    r2 = gate.run(bot, skills_override=[], answer_fn=lambda b, q, o: "GOOD")
    assert r2["score"] == 2 and gate.latest(bot)["id"] == r["id"]
    assert not os.path.exists(os.path.join(store.expert_dir(bot), "checks", r2["id"] + ".json"))
    # unreadable grader / crashing answer: failed questions, not a crash
    fake_text.replies.extend(["no json here"])
    r3 = gate.run(bot, skills_override=[], answer_fn=lambda b, q, o: (_ for _ in ()).throw(ValueError("x"))
                  if "UDF" in q else "GOOD")
    assert r3["score"] == 0 and "could not be read" in r3["results"][1]["why"]


def test_gate_start_status(bot, fake_text, monkeypatch):
    with open(os.path.join(bot.public_dir, "questions.md"), "w") as f:
        f.write(QUESTIONS)
    assert gate.status(bot) == {"running": False, "run_id": None, "done": 0, "total": 0, "error": ""}
    release = threading.Event()

    def answer(b, q, o):
        release.wait(5)
        return "GOOD"

    monkeypatch.setattr(gate, "_default_answer_fn", lambda: answer)
    fake_text.replies.extend([_grader] * 2)
    rid = gate.start(bot)
    assert gate.start(bot) == rid  # one at a time
    st = gate.status(bot)
    assert st["running"] and st["run_id"] == rid and st["total"] == 2
    release.set()
    deadline = time.time() + 5
    while gate.status(bot)["running"] and time.time() < deadline:
        time.sleep(0.02)
    st = gate.status(bot)
    assert not st["running"] and st["done"] == 2
    assert gate.latest(bot)["id"] == rid and gate.latest(bot)["score"] == 2

    # a crash inside the thread is recorded and never leaves running stuck
    monkeypatch.setattr(gate, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rid2 = gate.start(bot)
    deadline = time.time() + 5
    while gate.status(bot)["running"] and time.time() < deadline:
        time.sleep(0.02)
    st = gate.status(bot)
    assert st["run_id"] == rid2 and not st["running"] and "boom" in st["error"]
    saved = store.read_json(os.path.join(store.expert_dir(bot), "checks", rid2 + ".json"), None)
    assert "boom" in saved["error"] and gate.latest(bot)["id"] == rid


# --------------------------------------------------------------- proposals

def _seen(bot, claim, n):
    it = None
    for i in range(n):
        it = memory.add(bot, claim, session=f"s{i}")
    return it


def test_candidates_thresholds(bot):
    a = _seen(bot, "lesson a", 3)
    _seen(bot, "lesson b", 2)
    c = _seen(bot, "lesson c", 4)
    d = _seen(bot, "lesson d", 3)
    memory.mark(bot, d["id"], rejected=True)
    g = memory.add(bot, "gap thing", trust="gap")
    for i in range(3):
        memory.bump(bot, g["id"])
    assert [i["id"] for i in proposals.candidates(bot)] == [c["id"], a["id"]]
    bot.meta["expert"] = {"memory": {"propose_at": 2}}
    assert len(proposals.candidates(bot)) == 3


def test_prepare_update_with_diff_runs_no_checks(bot, fake_text, monkeypatch):
    bot.skill_save("Reading UDFs", "udf, load", "Step one.\nStep two.", name="reading-udfs")
    with open(os.path.join(bot.public_dir, "questions.md"), "w") as f:
        f.write("## How do I read a UDF?\n- must: mentions fused.load\n")
    a = _seen(bot, "Use fused.load to read a UDF", 3)
    new_body = "Step one.\nStep two.\nUse fused.load(name)."
    fake_text.replies.append(json.dumps({"action": "update", "skill": "reading-udfs", "title": "Reading UDFs",
                                         "trigger": "udf, load", "body": new_body}))  # the only model call
    monkeypatch.setattr(gate, "run", lambda *a, **k: pytest.fail("a proposal never runs the quality gate"))
    monkeypatch.setattr(gate, "_default_answer_fn", lambda: pytest.fail("a proposal never answers gate questions"))
    p = proposals.prepare(bot, [a])
    assert p["status"] == "ready" and p["action"] == "update" and p["skill"] == "reading-udfs"
    assert p["score_before"] is None and p["score_after"] is None and "before_reused" not in p
    assert fake_text.replies == []
    assert "+Use fused.load(name)." in p["diff"] and "skills/reading-udfs.md" in p["diff"]
    text, _detail = proposals.card_text(p)
    assert "check" not in text.lower() and text.endswith("Approve?")
    # drafting wrote nothing: skill unchanged, no gate run persisted
    assert bot.skills()[0]["body"] == "Step one.\nStep two."
    assert gate.latest(bot) is None
    assert memory.items(bot)[0]["proposed"] is True
    assert proposals.candidates(bot) == []
    assert proposals.get(bot, p["id"]) == p and proposals.listing(bot) == [p]
    text, detail = proposals.card_text(p)
    assert "update the skill" in text and detail == p["diff"]
    shown = proposals.mark_shown(bot, p["id"], 42)
    assert shown["status"] == "shown" and shown["card_seq"] == 42

    done = proposals.approve(bot, p["id"])
    assert done["status"] == "approved" and done["skill"] == "reading-udfs"
    assert bot.skills()[0]["body"] == new_body
    # The skill carries the lesson now: it left memory for the archive (reason promoted, with the skill).
    assert memory.items(bot) == []
    arch = memory.archived(bot)
    assert [(r["id"], r["reason"], r["skill"]) for r in arch] == [(a["id"], "promoted", "reading-udfs")]
    with pytest.raises(ValueError, match="already approved"):
        proposals.reject(bot, p["id"])
    with pytest.raises(ValueError):
        proposals.approve(bot, "pnope")


def test_prepare_create_fallback_and_reject(bot, fake_text):
    a = _seen(bot, "Canvases live under the user folder", 3)
    b = _seen(bot, "Team canvases are read-only copies", 3)
    fake_text.replies.append("```\nnot json at all\n```")  # unparsable: deterministic draft, no gate (no questions)
    p = proposals.prepare(bot, [a["id"], b])
    assert p["action"] == "create" and p["skill"] is None and p["title"].startswith("Lessons: ")
    assert p["trigger"] and "- Canvases live under the user folder" in p["body"]
    assert p["diff"].startswith("--- /dev/null") and p["score_before"] is None and p["score_after"] is None
    assert len(fake_text.calls) == 1 and bot.skills() == []
    r = proposals.reject(bot, p["id"])
    assert r["status"] == "rejected" and all(i["rejected"] for i in memory.items(bot))
    assert bot.skills() == []
    with pytest.raises(ValueError):
        proposals.prepare(bot, [])

    # a "create" naming an existing skill's slug is an update of that skill
    bot.skill_save("Canvas Basics", "canvas", "Old body.")
    c = _seen(bot, "Clones sit in user/", 3)
    fake_text.replies.append(json.dumps({"action": "create", "title": "Canvas Basics", "trigger": "canvas",
                                         "body": "Old body.\nClones sit in user/."}))
    p2 = proposals.prepare(bot, [c])
    assert p2["action"] == "update" and p2["skill"] == "canvas-basics"
    assert proposals.approve(bot, p2["id"])["skill"] == "canvas-basics"
    assert len(bot.skills()) == 1 and "Clones sit" in bot.skills()[0]["body"]


# ------------------------------------------------- readtools: secrets, size, broad roots

def test_readtools_refuse_hidden_and_secret_files(tmp_path):
    root = tmp_path / "root"
    (root / ".git").mkdir(parents=True)
    (root / ".ssh").mkdir()
    (root / "keys").mkdir()
    (root / ".env").write_text("TOKEN=hello\n")
    (root / ".git" / "config").write_text("hello\n")
    (root / ".ssh" / "known").write_text("hello\n")
    for n in ("server.pem", "tls.KEY", "id_rsa", "id_ed25519.pub", "credentials", "cert.p12", "c.pfx"):
        (root / "keys" / n).write_text("hello\n")
    (root / ".netrc").write_text("hello\n")
    (root / ".fused-canvas.json").write_text('{"hello": 1}\n')
    (root / "ok.txt").write_text("hello\n")
    h = readtools.make_handler([str(root)])
    for rel in (".env", ".git/config", ".ssh/known", ".netrc", "keys/server.pem", "keys/tls.KEY", "keys/id_rsa",
                "keys/id_ed25519.pub", "keys/credentials", "keys/cert.p12", "keys/c.pfx"):
        text, err = h("read_file", {"path": str(root / rel)})
        assert err and "hidden or secret" in text, rel
    text, err = h("list_dir", {"path": str(root / ".git")})
    assert err and "hidden or secret" in text
    text, err = h("search", {"query": "x", "path": str(root / ".ssh")})
    assert err
    text, err = h("read_file", {"path": str(root / ".fused-canvas.json")})
    assert not err and "hello" in text  # the canvas marker stays readable
    text, _ = h("list_dir", {"path": str(root / "keys")})
    assert text.splitlines()[1:] == []
    text, _ = h("list_dir", {"path": str(root)})
    assert ".fused-canvas.json" in text and ".env" not in text and "ok.txt" in text
    text, _ = h("search", {"query": "hello", "content": True})
    assert "ok.txt" in text and ".fused-canvas.json" in text
    for bad in (".env", "config", "known", "pem", "id_rsa", "credentials", "netrc", "p12"):
        assert bad not in text, bad
    text, _ = h("search", {"query": "id"})
    assert text.startswith("no file")


def test_readtools_refuse_files_over_20mb(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "big.csv").write_text("x" * 100)
    h = readtools.make_handler([str(root)])
    assert h("read_file", {"path": "big.csv"})[1] is False
    monkeypatch.setattr(readtools, "READ_MAX_BYTES", 50)
    text, err = h("read_file", {"path": "big.csv"})
    assert err and "too large" in text


def test_broad_roots_are_refused(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    (home / "docs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert readtools.too_broad("/") and readtools.too_broad(str(home)) and readtools.too_broad(str(tmp_path))
    assert readtools.too_broad("~") and not readtools.too_broad(str(home / "docs"))
    got = readtools.resolve_reads({"reads": [{"kind": "folder", "path": p} for p in
                                             ("/", "~", str(tmp_path / "home"), str(home / "docs"))]})
    assert got == [str((home / "docs").resolve())]
    h = readtools.make_handler(["/", str(home)])
    assert h("list_dir", {}) == ("no readable folders", True)


# ------------------------------------------------- proposals: async request, failures

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def shown(monkeypatch):
    from fused_render.experts import cards
    seen = []
    monkeypatch.setattr(cards, "show", lambda b, p: seen.append(p) or True)
    return seen


def test_request_is_async_single_flight_and_shows_the_card(bot, fake_text, shown):
    a = _seen(bot, "Use fused.load to read a UDF", 3)
    b = _seen(bot, "Canvases live under user/", 3)
    go = threading.Event()

    def slow(prompt):
        go.wait(5)
        return json.dumps({"action": "create", "title": "UDF tips", "trigger": "udf", "body": "- fused.load"})

    fake_text.replies.append(slow)
    p = proposals.request(bot, [a, b], topic="udfs")
    assert p["status"] == "preparing" and p["items"] == [a["id"], b["id"]] and p["topic"] == "udfs"
    assert all(i["proposed"] for i in memory.items(bot))  # marked at once
    assert proposals.candidates(bot) == []
    with pytest.raises(ValueError, match="already being prepared"):
        proposals.request(bot, [a])
    assert proposals.get(bot, p["id"])["status"] == "preparing"
    go.set()
    assert _wait(lambda: proposals.get(bot, p["id"])["status"] == "ready")
    done = proposals.get(bot, p["id"])
    assert done["title"] == "UDF tips" and done["diff"].startswith("--- /dev/null") and done["error"] == ""
    assert _wait(lambda: shown) and shown[0]["id"] == p["id"]
    # Single-flight is over: another request is allowed, but not for lessons in an open proposal.
    with pytest.raises(ValueError, match="no lessons"):
        proposals.request(bot, [a, b])


def test_open_proposals_exclude_their_items_from_candidates(bot):
    a = _seen(bot, "lesson a", 3)
    proposals._save(bot, {"id": "pold", "ts": 1, "status": "shown", "items": [a["id"]]})
    assert proposals.candidates(bot) == []
    proposals._save(bot, {"id": "pold", "ts": 1, "status": "rejected", "items": [a["id"]]})
    assert [i["id"] for i in proposals.candidates(bot)] == [a["id"]]


def test_request_failure_unmarks_the_lessons(bot, fake_text, shown, monkeypatch):
    a = _seen(bot, "lesson a", 3)
    monkeypatch.setattr(proposals, "_draft", lambda b, lessons: (_ for _ in ()).throw(RuntimeError("gate fell over")))
    p = proposals.request(bot, [a])
    assert _wait(lambda: proposals.get(bot, p["id"])["status"] == "failed")
    failed = proposals.get(bot, p["id"])
    assert "gate fell over" in failed["error"] and not memory.items(bot)[0]["proposed"]
    assert [i["id"] for i in proposals.candidates(bot)] == [a["id"]] and shown == []
    assert [x["status"] for x in proposals.listing(bot)] == ["failed"]


def test_a_stale_preparing_record_does_not_block(bot, fake_text, shown):
    a = _seen(bot, "lesson a", 3)
    proposals._save(bot, {"id": "pstale", "ts": time.time() - 2 * proposals.PREPARE_STALE_S,
                          "status": "preparing", "items": []})
    fake_text.replies.append("not json")
    p = proposals.request(bot, [a])
    assert proposals.get(bot, "pstale")["status"] == "failed" and "interrupted" in proposals.get(bot, "pstale")["error"]
    assert _wait(lambda: proposals.get(bot, p["id"])["status"] == "ready")


def test_a_failed_approve_fails_the_proposal_for_good(bot, fake_text, monkeypatch):
    from fused_render.experts import cards
    a = _seen(bot, "lesson a", 3)
    fake_text.replies.append("not json")
    p = proposals.prepare(bot, [a])
    monkeypatch.setattr(bot, "skill_save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ValueError, match="could not save the skill: disk full"):
        proposals.approve(bot, p["id"])
    after = proposals.get(bot, p["id"])
    assert after["status"] == "failed" and "disk full" in after["error"]
    assert not memory.items(bot)[0]["proposed"]
    with pytest.raises(ValueError, match="failed"):
        proposals.approve(bot, p["id"])  # never retried
    assert cards.show(bot, after) is False  # never re-shown
    assert proposals.mark_shown(bot, p["id"], 7)["status"] == "failed"


STRAY = ("### name: chart-type-conventions\ntitle: Chart type conventions\nTRIGGER: chart, sales chart\n\n"
         "# Chart type conventions\n\n## Sales charts\n\n- Monthly revenue as a bar chart.")


def test_draft_body_loses_copied_metadata_lines_on_create(bot, fake_text):
    a = _seen(bot, "Sales charts show monthly revenue as bars", 3)
    fake_text.replies.append(json.dumps({"action": "create", "skill": None, "title": "Chart type conventions",
                                         "trigger": "chart, sales chart", "body": STRAY}))
    assert "must NOT contain name, title or trigger" in proposals.DRAFT_PROMPT
    p = proposals.prepare(bot, [a])
    assert p["action"] == "create"
    assert p["body"] == "## Sales charts\n\n- Monthly revenue as a bar chart."
    assert "name:" not in p["diff"] and "trigger:" not in p["diff"]


def test_draft_body_loses_copied_metadata_lines_on_update(bot, fake_text):
    bot.skill_save("Chart type conventions", "chart", "## Sales charts", name="chart-type-conventions")
    a = _seen(bot, "Sales charts show monthly revenue as bars", 3)
    fake_text.replies.append(json.dumps({"action": "update", "skill": "chart-type-conventions",
                                         "title": "Chart type conventions", "trigger": "chart", "body": STRAY}))
    p = proposals.prepare(bot, [a])
    assert p["action"] == "update" and p["skill"] == "chart-type-conventions"
    assert p["body"] == "## Sales charts\n\n- Monthly revenue as a bar chart."
    assert proposals._clean_body("title: x\n\n", "x") == ""
    assert proposals._clean_body("# Other heading\nbody", "Title") == "# Other heading\nbody"
