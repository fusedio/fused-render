"""experts core: store, model, readtools, memory, the inbox and the lessons step of the Learn loop
(docs/experts.md). No real model, no network beyond a loopback fake of `/api/ai`."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.experts import (consolidate, distill, engine, knowledge, memory, model, observations,
                                  readtools, store)


def _done(bot):
    """The handled inbox entries (observations/done), oldest first."""
    d = os.path.join(store.expert_dir(bot), "observations", "done")
    rows = [store.read_json(os.path.join(d, n), None) for n in (os.listdir(d) if os.path.isdir(d) else [])]
    return sorted(rows, key=lambda o: o["ts"])


def _acts(bot, quiet=True):
    """(action, target) of every activity line, oldest first."""
    return [(ln["action"], ln["target"]) for ln in reversed(knowledge.activity(bot, limit=500, quiet=quiet))]


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
    assert not {"proposed", "promoted", "rejected"} & set(memory.items(bot)[1])
    assert memory.find(bot, "canvases live under USER/")["id"] == c["id"]
    assert [i["id"] for i in memory.match(bot, "canvases")] == [c["id"]]
    assert memory.forget(bot, c["id"]) == 1
    assert memory.forget(bot, "use fused.load for udfs") == 1
    assert memory.items(bot) == []
    memory.add(bot, "alpha lesson one")
    memory.add(bot, "alpha lesson two")
    assert memory.forget(bot, "alpha") == 2 and memory.forget(bot, "zzz") == 0


# ------------------------------------------------------------ observations

def test_inbox_write_due_archive(bot):
    assert not consolidate.due(bot)
    o1 = observations.write(bot, kind="user_correction", claim="x", trust="outcome", session="s")
    time.sleep(0.01)
    o2 = observations.write(bot, kind="gap", claim="y", trust="gap", consult="c1")
    assert [o["id"] for o in observations.pending(bot)] == [o1["id"], o2["id"]]
    assert [o["id"] for o in consolidate.waiting(bot)] == [o1["id"], o2["id"]]
    assert not consolidate.due(bot)
    assert consolidate.due(bot, now=time.time() + 86400 + 5)  # by age
    assert consolidate.due(bot, force=True)
    observations.write(bot, kind="unanswered", claim="z", trust="outcome")
    assert consolidate.due(bot)  # by count
    consolidate._archive(bot, [o1["id"], "../evil"], {})
    assert len(consolidate.waiting(bot)) == 2 and consolidate.status(bot)["waiting"] == 2
    assert [(o["id"], o["outcome"]["op"]) for o in _done(bot)] == [(o1["id"], "nothing")]


def test_an_entry_from_the_user_is_due_at_once_and_learning_can_be_switched_off(bot):
    from fused_render.experts import consult
    observations.write(bot, kind="user_correction", claim="x", trust="outcome", session="s", by_user=False)
    assert not consolidate.due(bot)
    observations.write(bot, kind="user_correction", claim="y", trust="outcome", session="s", by_user=True,
                       quote="no, do y instead")
    assert consolidate.due(bot)  # a correction with a verified quote
    consolidate._archive(bot, [o["id"] for o in consolidate.waiting(bot)], {})
    consult.inbox_write(bot, "taught", "Always z.", by_user=False)
    assert not consolidate.due(bot)
    consult.inbox_write(bot, "forget", "z", by_user=True)
    assert consolidate.due(bot)
    bot.meta["expert"] = {"learning": False}
    assert not consolidate.due(bot) and not consolidate.due(bot, force=True)
    assert consolidate.run(bot, force=True) == {"ran": False} and knowledge.activity(bot) == []


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
    assert r["ran"] is True and all(ln["ok"] for ln in r["lines"])
    claims = {i["claim"]: i for i in memory.items(bot)}
    assert set(claims) == {"existing lesson", "A crisp new lesson."}
    assert claims["existing lesson"]["count"] == 2
    assert "could not answer X" not in fake_text.calls[0]  # gaps are not offered as lessons
    assert observations.pending(bot) == [] and len(_done(bot)) == 4
    # Everything it did is on the activity log, each line with its stage, a reason and where it came from.
    # Honest labels: the new lesson was seen in one session (`noticed`, not used yet); the old one is now seen in
    # two, so it is used in answers for the first time (`learned`).
    assert _acts(bot) == [("noticed", claims["A crisp new lesson."]["id"]), ("learned", old["id"]), ("noticed", ""),
                          ("could_not_find", "")]
    first = knowledge.activity(bot)[-1]
    assert first["stage"] == "lessons" and first["why"] and first["evidence"] == {"session": "s9", "item": o1["id"]}
    assert _acts(bot, quiet=False) == [("noticed", claims["A crisp new lesson."]["id"]), ("learned", old["id"]),
                                       ("could_not_find", "")]


def test_consolidate_unparsable_keeps_pending_and_falls_back_after_three(bot, fake_text):
    assert consolidate.run(bot) == {"ran": False}
    memory.add(bot, "Lesson A")
    observations.write(bot, kind="user_correction", claim="lesson a.", trust="outcome", session="s1")
    assert consolidate.run(bot)["ran"] is False  # 1 < consolidate_at, young
    assert fake_text.calls == []
    observations.write(bot, kind="user_correction", claim="Lesson B", trust="outcome")
    observations.write(bot, kind="gap", claim="gap", trust="gap")
    # 1st failure: an unreadable answer adds nothing and leaves everything waiting; the log says so.
    fake_text.replies.append("I could not decide, sorry [not json")
    consolidate.run(bot)
    line = knowledge.activity(bot)[0]
    assert line["action"] == "skipped" and "could not be read" in line["why"] and "1 of 3" in line["summary"]
    assert [i["claim"] for i in memory.items(bot)] == ["Lesson A"] and len(observations.pending(bot)) == 3
    assert consolidate.failures(bot) == 1
    # 2nd: a failing call, same.
    fake_text.replies.append(lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    consolidate.run(bot)
    assert "down" in knowledge.activity(bot)[0]["why"] and consolidate.failures(bot) == 2
    assert [i["claim"] for i in memory.items(bot)] == ["Lesson A"]
    # 3rd consecutive: exact-match fallback, then the counter resets.
    fake_text.replies.append("still nothing")
    consolidate.run(bot)
    assert sorted((i["claim"], i["count"]) for i in memory.items(bot)) == [("Lesson A", 2), ("Lesson B", 1)]
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
    assert consolidate.run(bot, force=True)["ran"]
    assert [(i["claim"], i["count"]) for i in memory.items(bot)] == [("existing", 2)]
    assert _acts(bot) == [("learned", old["id"])]  # a bump of a lesson that does not exist is not even tried
    assert observations.pending(bot) == [] and len(_done(bot)) == 4


def test_consolidate_bumps_confirmed_lessons_once_per_session_without_the_model(bot, fake_text):
    bot.meta["expert"] = {"memory": {"propose_at": 9}}  # count 3 would otherwise earn a skill (distill)
    lesson = memory.add(bot, "Users want a Parquet loader node.", kind="repeated_request", session="s1")

    def confirm(session):
        return observations.write(bot, kind="repeated_request", claim="Users want a Parquet loader node.",
                                  trust="outcome", session=session, confirms=lesson["id"])

    seen = [confirm("s2"), confirm("s2"), confirm("s3"), confirm("s1")]
    assert consolidate.run(bot, force=True)["ran"]
    assert fake_text.calls == []  # only confirmations: no model decision
    item = memory.items(bot)[0]
    assert item["count"] == 3 and item["sessions"] == ["s1", "s2", "s3"]  # s2 and s1 again count once
    done = {o["id"]: o for o in _done(bot)}
    assert all(done[o["id"]]["outcome"]["op"] == "bump" and done[o["id"]]["outcome"]["item"] == lesson["id"]
               for o in seen)
    # Counting is quiet, except the one bump that makes the lesson used in answers for the first time.
    shown = knowledge.activity(bot, quiet=False)
    assert [(ln["action"], ln["target"], ln["why"]) for ln in shown] == [
        ("learned", lesson["id"], "Seen in a second task. Now used in answers.")]
    assert [ln["action"] for ln in knowledge.activity(bot)].count("counted") == 3


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
    consolidate.run(bot, force=True)
    assert c["id"] not in fake_text.calls[0] and new["id"] in fake_text.calls[0] and stale["id"] in fake_text.calls[0]
    kinds = {i["claim"]: (i["kind"], i["count"]) for i in memory.items(bot)}
    assert kinds == {"Known lesson.": ("lesson", 2), "Asked once.": ("repeated_request", 1),
                     "Stale one.": ("user_correction", 1)}


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
    consolidate.run(bot, force=True)
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
    done = {o["id"]: o["outcome"] for o in _done(bot)}
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
    assert consolidate.run(bot, force=True)["ran"]
    prompt = fake_text.calls[0]
    assert contra["id"] in prompt and '"same"' in prompt and "never" in prompt
    items = {i["id"]: i for i in memory.items(bot)}
    assert items[old["id"]]["count"] == 2  # unchanged
    assert items[old["id"]]["contradicted"] == 1 and items[old["id"]]["contradicted_sessions"] == ["live"]
    new = [i for i in items.values() if i["id"] != old["id"]]
    assert [(i["claim"], i["kind"], i["count"]) for i in new] == [(REGIONAL, "user_correction", 1)]  # added once
    done = {o["id"]: o["outcome"] for o in _done(bot)}
    assert {k: done[contra["id"]][k] for k in ("op", "item", "new")} == {
        "op": "contradict", "item": old["id"], "new": new[0]["id"]}
    assert done[corr["id"]]["op"] == "add"
    # The contradicted lesson is flagged in every answer and never written into a skill.
    assert "(contradicted in 1 session(s); prefer newer lessons on this)" in memory.index_text(bot)
    memory.bump(bot, old["id"], session="s9")
    assert old["id"] not in [i["id"] for i in distill.candidates(bot)]


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
    consolidate.run(bot, force=True)
    items = {i["claim"]: i for i in memory.items(bot)}
    assert items[BAR]["count"] == 1 and items[BAR]["contradicted"] == 1
    assert items["Some other lesson."]["count"] == 1
    assert items[REGIONAL]["kind"] == "advice_contradicted"  # no USER line said so
    assert items["Never trust X."]["kind"] == "advice_contradicted"
    assert items["Bar charts are fine."]["kind"] == "user_correction"
    done = {o["id"]: o["outcome"] for o in _done(bot)}
    assert done[contra["id"]]["op"] == "contradict" and done[contra["id"]]["new"] == items[REGIONAL]["id"]
    assert done[loose["id"]]["op"] == "add"


def test_consolidate_a_contradiction_the_model_drops_still_counts(bot, fake_text):
    old = memory.add(bot, BAR, session="s0")
    contra = observations.write(bot, kind="advice_contradicted", claim=BAR, trust="outcome", session="live",
                                contradicts=old["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": contra["id"], "op": "add"}]))  # the old text itself
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert len(memory.items(bot)) == 1 and item["count"] == 1 and item["contradicted"] == 1
    out = _done(bot)[0]["outcome"]
    assert out["op"] == "contradict" and out["item"] == old["id"] and "new" not in out


def test_consolidate_without_an_engine_does_nothing(bot, fake_text, monkeypatch):
    monkeypatch.setattr(model, "available", lambda b: (False, "needs Claude, Codex or Copilot"))
    observations.write(bot, kind="user_correction", claim="one", trust="outcome")
    r = consolidate.run(bot, force=True)
    assert r["ran"] is False and r["reason"] == "needs Claude, Codex or Copilot"
    assert memory.items(bot) == [] and len(observations.pending(bot)) == 1 and fake_text.calls == []


def test_a_failing_step_is_logged_and_the_others_still_run(bot, fake_text, monkeypatch):
    from fused_render.experts import gaps

    def boom(bot, ctx):
        raise RuntimeError("disk on fire")
        yield
    monkeypatch.setattr(gaps, "step", boom)
    o = observations.write(bot, kind="user_correction", claim="one", trust="outcome")
    fake_text.replies.append(json.dumps([{"obs": o["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)
    assert r["ran"] and [i["claim"] for i in memory.items(bot)] == ["one"]
    (line,) = [ln for ln in knowledge.activity(bot) if ln["action"] == "skipped"]
    assert line["stage"] == "research" and "disk on fire" in line["why"]


# -------------------------------------------------------------- parse_json

def test_parse_json_keeps_a_fenced_block_inside_a_string_byte_for_byte():
    body = "Intro.\n\n```python\nx = [1, 2]\n```\n\nOutro with } and ]."
    raw = json.dumps({"action": "create", "body": body})
    assert engine.parse_json(raw, dict)["body"] == body
    assert engine.parse_json("```json\n" + raw + "\n```", dict)["body"] == body  # outer wrapper only
    assert engine.parse_json("Here you go:\n" + raw + "\nHope that helps [really].", dict)["body"] == body


def test_parse_json_lists_prose_and_failures():
    assert engine.parse_json('Answer: [{"obs": "o1", "op": "nothing"}] ok', list) == [{"obs": "o1", "op": "nothing"}]
    assert engine.parse_json('see [note] then [1, "]"]', list) == [1, "]"]
    assert engine.parse_json('{"a": "unclosed', dict) is None
    assert engine.parse_json(None, list) is None
    assert engine.parse_json("[1]", dict) is None



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
