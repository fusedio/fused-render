"""An expert's token ledger (docs/experts.md §4 `usage.py`): one `usage.jsonl` line per model call, its stage
(explicit, the engine's scope, or the calling module), real counts when the gateway reports them and a
characters / 4 estimate when it does not, accounting that never breaks a call, `summary`, and
`GET /api/experts/{id}/usage`. No real model: a fake `fused_ai` client and a loopback fake of `/api/ai`."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _bots_conftest import *  # noqa: F401,F403

from fused_render.bots import bot as botmod
from fused_render.experts import model, readtools, usage


class FakeBot:
    def __init__(self, root, bid="u1"):
        self.id = bid
        self.meta = {"kind": "expert", "name": "Usage Expert", "expert": {}}
        self.dir = os.path.join(str(root), bid)
        os.makedirs(self.dir, exist_ok=True)
        self.ai_calls = []

    def _ai_call(self, ai, prompt, **kw):
        self.ai_calls.append((prompt, kw))
        return ai.text(prompt, **kw)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "provider_for", lambda b: ("claude", "sonnet"))
    return FakeBot(tmp_path)


class PlainAI:
    """A `fused_ai` stand-in with no `_post_json`: no usage comes back, the line is an estimate."""
    @staticmethod
    def text(prompt, **kw):
        return "x" * 40


class CountingAI:
    """A `fused_ai` stand-in that answers `/api/ai` like the Claude tier: `result.usage` in camelCase."""
    _DEFAULT_TIMEOUT_S = 120.0
    bodies: list = []

    @classmethod
    def _post_json(cls, path, body, timeout=120.0):
        cls.bodies.append((path, body, timeout))
        return {"ok": True, "result": {"text": "real", "usage": {"inputTokens": 1234, "outputTokens": 56,
                                                                 "totalTokens": 1290}}}

    @staticmethod
    def _error_from_payload(status, payload):
        return RuntimeError(payload["error"]["message"])

    @staticmethod
    def text(prompt, **kw):
        raise AssertionError("the counting path posts itself")


def _lines(bot):
    return usage.lines(bot)


def _call_from(module_name, fn, *a, **k):
    """Run `fn` from a frame whose module is `module_name` (what `infer_stage` reads)."""
    ns = {"__name__": module_name, "fn": fn, "a": a, "k": k}
    exec("def go():\n    return fn(*a, **k)\n", ns)
    return ns["go"]()


# ------------------------------------------------------------------ stages ---
@pytest.mark.parametrize("mod,stage", [("fused_render.experts.capture", "capture"),
                                       ("fused_render.experts.consolidate", "consolidate"),
                                       ("fused_render.experts.tidy", "tidy"),
                                       ("fused_render.experts.distill", "draft"),
                                       ("fused_render.experts.gaps", "research"),
                                       ("fused_render.experts.knowledge", "other"),
                                       ("somewhere.else", "other")])
def test_stage_is_inferred_from_the_calling_module(bot, monkeypatch, mod, stage):
    monkeypatch.setattr(botmod, "_fused_ai", lambda: PlainAI)
    assert _call_from(mod, model.text, bot, "hello", system="sys") == "x" * 40
    (line,) = _lines(bot)
    assert line["stage"] == stage


def test_explicit_stage_and_the_engine_scope(bot, monkeypatch):
    monkeypatch.setattr(botmod, "_fused_ai", lambda: PlainAI)
    _call_from("fused_render.experts.capture", model.text, bot, "q", stage="consult")
    with usage.stage("chat"):
        _call_from("fused_render.experts.capture", model.text, bot, "q")
        _call_from("fused_render.experts.capture", model.text, bot, "q", stage="tidy")
    model.text(bot, "q", stage="nonsense")
    assert [ln["stage"] for ln in _lines(bot)] == ["consult", "chat", "tidy", "other"]
    assert usage.current_stage() is None


# ------------------------------------------------------------------ text ---
def test_text_estimates_when_no_usage_comes_back(bot, monkeypatch):
    monkeypatch.setattr(botmod, "_fused_ai", lambda: PlainAI)
    assert model.text(bot, "p" * 100, system="s" * 300) == "x" * 40
    (line,) = _lines(bot)
    assert line["estimated"] is True and line["input_tokens"] == 100 and line["output_tokens"] == 10
    assert line["provider"] == "claude" and line["model"] == "sonnet" and line["rounds"] == 1
    assert line["ok"] is True and isinstance(line["ms"], int) and line["cache_read_tokens"] is None
    assert len(bot.ai_calls) == 1  # still through bot._ai_call (the bots ledger)


def test_text_takes_the_gateways_real_counts(bot, monkeypatch):
    CountingAI.bodies = []
    monkeypatch.setattr(botmod, "_fused_ai", lambda: CountingAI)
    assert model.text(bot, "hello", system="be brief", timeout=33) == "real"
    (path, body, timeout), = CountingAI.bodies
    assert path == "/api/ai" and "stream" not in body and timeout == 33
    assert body == {"prompt": "hello", "model": "sonnet", "provider": "claude", "systemPrompt": "be brief"}
    (line,) = _lines(bot)
    assert line["estimated"] is False and line["input_tokens"] == 1234 and line["output_tokens"] == 56
    assert len(bot.ai_calls) == 1


def test_text_failure_is_a_line_too(bot, monkeypatch):
    class Failing(CountingAI):
        @classmethod
        def _post_json(cls, path, body, timeout=120.0):
            return {"ok": False, "error": {"type": "ai_error", "message": "the CLI fell over"}}
    monkeypatch.setattr(botmod, "_fused_ai", lambda: Failing)
    with pytest.raises(RuntimeError, match="fell over"):
        model.text(bot, "q")
    (line,) = _lines(bot)
    assert line["ok"] is False and line["estimated"] is True


def test_accounting_failure_never_breaks_the_call(bot, monkeypatch):
    monkeypatch.setattr(botmod, "_fused_ai", lambda: CountingAI)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(usage.store, "append_jsonl", boom)
    assert model.text(bot, "q") == "real"
    monkeypatch.setattr(usage, "tokens_from_result", lambda r: 1 / 0)
    assert model.text(bot, "q") == "real"
    monkeypatch.setattr(usage, "resolve_stage", lambda s=None: 1 / 0)
    assert model.text(bot, "q") == "real"


# ------------------------------------------------------------------ tool turns ---
class _FakeAI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    state: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(n) or b"{}")
        st = self.state
        if self.path.startswith("/api/ai/turns/"):
            st["answered"].set()
            out = b'{"ok": true}'
            self.send_response(200)
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
        frame({"type": "turn", "id": "turn_1"})
        for i in range(st.get("calls", 0)):
            st["answered"].clear()
            frame({"type": "tool_call", "id": f"c{i}", "name": "read_file", "input": {"path": "/x"}})
            st["answered"].wait(5)
        frame({"type": "done", "ok": True, "result": {"text": "the answer", "usage": st.get("usage")}})


@pytest.fixture
def fake_server(monkeypatch):
    _FakeAI.state = {"answered": threading.Event()}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", f"http://127.0.0.1:{srv.server_address[1]}")
    yield _FakeAI.state
    srv.shutdown()
    srv.server_close()


def test_tool_turn_real_counts(bot, fake_server):
    fake_server["calls"] = 2
    fake_server["usage"] = {"inputTokens": 9000, "outputTokens": 300, "totalTokens": 9300}
    out = model.tool_turn(bot, "q", system="S" * 4000, tools=readtools.specs(),
                          handler=lambda n, i: ("file text", False), timeout=20, stage="consult")
    assert out == "the answer"
    (line,) = _lines(bot)
    assert line["stage"] == "consult" and line["estimated"] is False
    assert line["input_tokens"] == 9000 and line["output_tokens"] == 300
    assert line["rounds"] == 2  # the gateway reports no turn count: the tool calls this run made


def test_tool_turn_estimates_every_round(bot, fake_server):
    fake_server["calls"] = 3
    fake_server["usage"] = None  # the Copilot / Codex tiers
    model.tool_turn(bot, "q" * 400, system="S" * 4000, tools=readtools.specs(),
                    handler=lambda n, i: ("r" * 800, False), timeout=20)
    (line,) = _lines(bot)
    assert line["estimated"] is True and line["rounds"] == 3 and line["stage"] == "other"
    # 4 model rounds, each re-sending system + prompt (4400 chars) plus the tool traffic so far.
    assert line["input_tokens"] >= 4 * 4400 // 4 + (1 + 2 + 3) * 800 // 4
    assert line["output_tokens"] >= len("the answer") // 4


def test_tokens_from_result_shapes():
    assert usage.tokens_from_result(None) is None
    assert usage.tokens_from_result({"text": "x", "usage": None}) is None
    assert usage.tokens_from_result({"usage": {"inputTokens": "a"}}) is None
    got = usage.tokens_from_result({"usage": {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 70},
                                    "providerMetadata": {"claude": {"numTurns": 4}}})
    assert got == {"input_tokens": 5, "output_tokens": 6, "cache_read_tokens": 70, "rounds": 4}


# ------------------------------------------------------------------ summary ---
def test_summary_maths(bot):
    now = time.mktime((2026, 11, 17, 12, 0, 0, 0, 0, -1))
    day = 86400

    def put(ts, stage, i, o, c=None, est=False):
        line = usage.record(bot, stage=stage, provider="claude", model="sonnet", input_tokens=i, output_tokens=o,
                            cache_read_tokens=c, rounds=1, ms=10, ok=True, estimated=est)
        line["ts"] = ts
        return line
    rows = [put(now - 60, "consult", 1000, 100, 500), put(now - 120, "consult", 3000, 300, None, True),
            put(now - day, "capture", 200, 20), put(now - 3 * day, "chat", 50, 5, 7, True),
            put(now - 10 * day, "consult", 99999, 9999)]  # outside a 7-day window
    with open(usage._path(bot), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("not json\n")
    s = usage.summary(bot, days=7, now=now)
    assert s["days"] == 7
    assert s["by_stage"][0] == {"stage": "consult", "calls": 2, "input_tokens": 4000, "output_tokens": 400,
                                "cache_read_tokens": 500, "estimated_share": 0.5}
    assert [r["stage"] for r in s["by_stage"]] == ["consult", "capture", "chat"]
    assert s["by_stage"][2]["estimated_share"] == 1.0 and s["by_stage"][1]["estimated_share"] == 0.0
    assert s["by_day"] == [{"day": "2026-11-14", "calls": 1, "input_tokens": 50, "output_tokens": 5},
                           {"day": "2026-11-16", "calls": 1, "input_tokens": 200, "output_tokens": 20},
                           {"day": "2026-11-17", "calls": 2, "input_tokens": 4000, "output_tokens": 400}]
    assert s["total"] == {"calls": 4, "input_tokens": 4250, "output_tokens": 425, "cache_read_tokens": 507,
                          "estimated_share": 0.5}
    assert usage.summary(bot, days=30, now=now)["total"]["calls"] == 5
    assert usage.summary(bot, days=1, now=now)["total"]["calls"] == 2
    assert usage.summary(FakeBot(os.path.dirname(bot.dir), "empty"), now=now) == {
        "days": 7, "by_stage": [], "by_day": [],
        "total": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "estimated_share": 0.0}}


# ------------------------------------------------------------------ route ---
def test_usage_route(client, monkeypatch):
    from fused_render.bots import paths as bpaths
    from fused_render.bots import registry
    from fused_render.bots import store as bstore
    bid = "e2e2e2e2"
    bpaths.place(bid, "Usage Expert")
    bstore.write_meta(bid, {"id": bid, "name": "Usage Expert", "kind": "expert", "model": "", "effort": "",
                            "status": "idle", "instructions": "", "created": time.time(), "task": "", "step": 0,
                            "url": None, "title": None, "expert": {"ask_me_about": "x"}})
    ex = registry.get(bid)
    usage.record(ex, stage="consult", provider="claude", model="sonnet", input_tokens=10, output_tokens=2)
    status, _h, body = client.get(f"/api/experts/{bid}/usage?days=3")
    assert status == 200
    data = json.loads(body)
    assert data["days"] == 3 and data["total"]["calls"] == 1 and data["by_stage"][0]["stage"] == "consult"
    status, _h, _b = client.get("/api/experts/nope/usage")
    assert status == 404


def test_cached_prompt_is_not_counted_as_a_handful_of_tokens(bot, monkeypatch):
    # The gateway reports uncached input only: a prompt served from cache comes back as a few tokens and no
    # cache figure. The ledger then falls back to the characters sent, and says the input is an estimate.
    CountingAI.bodies = []
    monkeypatch.setattr(botmod, "_fused_ai", lambda: CountingAI)
    model.text(bot, "p" * 8000, system="s" * 12000)
    (line,) = _lines(bot)
    assert line["input_tokens"] == 5000 and line["estimated"] is True and line["output_tokens"] == 56
