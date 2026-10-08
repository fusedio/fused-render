"""The steps engine (OpenBot's JSON-action loop): the reply parser, and one
whole task driven by scripted model replies against a fake browser — events,
the approval gate on a "Buy now" click answered through `bot.send`, step
thumbnails, and the engine choice in `start_task`."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import apptools, store
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths
from fused_render.bots import registry, steps_engine
from fused_render.bots.steps_engine import _parse


# ---- _parse --------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ('{"action": "goto", "url": "https://a.test"}', {"action": "goto", "url": "https://a.test"}),
    ('```json\n{"action": "done", "message": "ok"}\n```', {"action": "done", "message": "ok"}),
    ('```\n{"action": "back"}\n```', {"action": "back"}),
    ('Sure! Here is my action: {"action": "click", "ref": "sb3"} hope that helps', {"action": "click", "ref": "sb3"}),
    ('{"action": "done", "message": "line one\nline two\tend"}', {"action": "done", "message": "line one\nline two\tend"}),
    ('{"action": "scroll", "direction": "down"} {"action": "back"}', {"action": "scroll", "direction": "down"}),
    ('{"action": "wait"} trailing } garbage {', {"action": "wait"}),
    ('prose {not json} then {"action": "read"}', {"action": "read"}),
])
def test_parse_accepts_what_models_produce(raw, want):
    assert _parse(raw) == want


@pytest.mark.parametrize("raw", ["", None, "I will click the button.", "[1, 2, 3]", '{"action": "done", "message": "cut off'])
def test_parse_rejects_non_objects(raw):
    assert _parse(raw) is None


@pytest.mark.parametrize("d,url", [
    # what Gemma 4 E4B actually sends for "go to https://example.com"
    ({"action": "goto", "to": "https://example.com", "seconds": 0, "message": ""}, "https://example.com"),
    ({"action": "goto", "args": {"url": "https://a.test/x"}}, "https://a.test/x"),
    ({"action": "goto", "text": "www.example.com"}, "https://www.example.com"),
    ({"action": "tab", "tab": "new", "to": "example.org/page?q=1"}, "https://example.org/page?q=1"),
    ({"action": "goto", "url": "https://kept.test", "to": "https://other.test"}, "https://kept.test"),
])
def test_repair_moves_a_misfiled_url(d, url):
    assert steps_engine._repair(d)["url"] == url


@pytest.mark.parametrize("d", [
    {"action": "text", "to": "Mom", "text": "hi"},              # `to` is a contact here
    {"action": "texts", "to": "+15551234567"},
    {"action": "goto", "to": "the example page"},              # not an address: left for "no url"
    {"action": "tab", "tab": "switch", "to": "https://a.test"},
])
def test_repair_leaves_other_actions_alone(d):
    before = dict(d)
    assert steps_engine._repair(d) == before


def test_repair_passes_none_through():
    assert steps_engine._repair(None) is None


def test_system_prompt_fills_the_apps_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    sp = steps_engine.system_prompt()
    assert "@APPS_ROOT@" not in sp and str(tmp_path / "ws" / "app") in sp
    assert '"action": "<goto|click|type' in sp  # the steps engine still speaks JSON actions


# ---- one task, end to end --------------------------------------------------------
class FakeBrowser:
    """The slice of browser.Browser the steps engine and Bot touch."""

    def __init__(self, data_dir, cache_dir, proc=None):
        self.data_dir, self.cache_dir = data_dir, cache_dir
        self.proc = proc  # a Bot passes its browser's BrowserProcess; the fake never spawns Chrome
        self.profile = os.path.join(data_dir, "profile")
        self.downloads = os.path.join(data_dir, "downloads")
        self.shot_path = os.path.join(cache_dir, "shot.png")
        self.lock = threading.RLock()
        self.encrypt = False
        self.thumb_bytes = None
        self.url = "about:blank"
        self.calls = []

    # lifecycle
    def start(self):
        self.calls.append(("start",))

    def stop(self, seal=None):
        self.calls.append(("stop",))

    def alive(self):
        return True

    def shared(self):
        return False

    def sleep(self):
        self.calls.append(("stop",))
        return True

    def recover_stuck_google_popup(self):
        return False

    def status(self):
        return {"running": True, "url": self.url, "title": "Shop", "sealed": False, "encrypt": False}

    status_cached = status

    def tabs(self):
        return [{"i": 0, "id": "t", "title": "Shop", "url": self.url, "active": True, "ws": ""}]

    def shot_ts(self):
        return 0.0

    def list_files(self, folder, limit=20):
        return []

    # page
    def observe(self):
        self.thumb_bytes = b"\xff\xd8fake-jpeg"
        return {"url": self.url, "title": "Shop", "text": "A laptop. 999 USD.", "tabs": self.tabs(), "downloads": [],
                "elements": [{"ref": "sb1", "tag": "a", "text": "Home", "href": "/"},
                             {"ref": "sb2", "tag": "button", "text": "Buy now"}]}

    def goto(self, url):
        self.calls.append(("goto", url))
        self.url = url
        return {"url": url}

    def click(self, ref="", text="", x=None, y=None, backend=None):
        self.calls.append(("click", ref, text))
        self.url = self.url.rstrip("/") + "/thanks"
        return {"url": self.url}

    def screenshot(self, timeout=30):
        return self.shot_path


class FakeAI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def text(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if not self.replies:
            return json.dumps({"action": "done", "message": "out of script"})
        return self.replies.pop(0)


@pytest.fixture
def steps_bot(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    os.makedirs(bpaths.apps_root())
    monkeypatch.setattr(apptools, "ROOTS", [bpaths.apps_root()])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    monkeypatch.setattr(apptools, "_cache_at", 0.0)
    monkeypatch.setattr(botmod, "Browser", FakeBrowser)
    registry.reset_for_tests()
    bid = "s1"
    os.makedirs(bpaths.bot_dir(bid))
    store.write_meta(bid, {"id": bid, "name": "Shopper", "model": "sonnet", "effort": "low", "status": "idle",
                           "instructions": "", "created": time.time(), "task": "", "step": 0, "url": None, "title": None})
    b = registry.get(bid)
    yield b
    b.stop()
    if b.thread:
        b.thread.join(5)
    registry.reset_for_tests()


def _events(b):
    return [ev for _, ev in store.iter_events(b.events_path)]


def _wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_start_task_picks_steps_without_a_claude_cli(steps_bot):
    # conftest points FUSED_RENDER_*CLAUDE_BIN at a missing path: `auto` resolves to steps.
    assert botmod._engine_for(steps_bot.meta) == "steps"
    assert botmod._engine_for({"model": "local-4b", "engine": "auto"}) == "steps"
    assert botmod._engine_for({"model": "opus", "engine": "agent"}) == "agent"
    assert botmod._engine_for({"model": "opus", "engine": "steps"}) == "steps"


def test_task_runs_and_the_approval_gate_holds_a_buy_click(steps_bot, monkeypatch):
    b = steps_bot
    ai = FakeAI([
        json.dumps({"thought": "Opening the shop.", "action": "goto", "url": "https://shop.test"}),
        json.dumps({"thought": "Buying the laptop.", "action": "click", "ref": "sb2"}),
        json.dumps({"thought": "Bought.", "action": "done", "message": "Ordered the laptop for 999 USD."}),
    ])
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)

    def approve():
        assert _wait_for(lambda: any(e["role"] == "approval" for e in _events(b)))
        assert _wait_for(lambda: b.asking)
        assert b.meta["status"] == "waiting"
        assert b.browser.calls.count(("click", "sb2", "Buy now")) == 0  # nothing ran before the yes
        b.send("approve")
    t = threading.Thread(target=approve)
    t.start()
    b.start_task("buy the laptop")
    assert b.engine == "steps"
    b.thread.join(15)
    t.join(5)
    assert not b.thread.is_alive()

    evs = _events(b)
    roles = [e["role"] for e in evs]
    assert evs[0]["role"] == "system" and evs[0]["text"] == "Task started: buy the laptop"
    assert [e["text"] for e in evs if e["role"] == "thought"] == ["Opening the shop.", "Buying the laptop.", "Bought."]
    actions = [e for e in evs if e["role"] == "action"]
    assert actions[0]["text"] == "goto https://shop.test" and actions[0]["result"] == "ok, now at https://shop.test"
    assert actions[1]["text"] == 'click "Buy now"' and actions[1]["result"].startswith("ok, now at https://shop.test/thanks")
    # step thumbnails: the FILE NAME of cache/<id>/steps/<seq>.jpg
    for a in actions:
        assert a["thumb"] == f"{a['seq']}.jpg"
        assert os.path.isfile(os.path.join(b.steps_dir, a["thumb"]))
    appr = next(e for e in evs if e["role"] == "approval")
    assert appr["text"].startswith('About to click "Buy now". The button says "Buy now"') and appr["text"].endswith("Approve?")
    assert appr["detail"] == 'click "Buy now"'
    assert roles.index("approval") < roles.index("action", roles.index("approval"))
    assert any(e["role"] == "user" and e["text"] == "approve" for e in evs)
    assert evs[-1]["role"] == "done" and evs[-1]["text"] == "Ordered the laptop for 999 USD."
    assert ("click", "sb2", "Buy now") in b.browser.calls
    assert b.meta["status"] == "idle"
    # the model saw the JSON system prompt and the approval in its history
    prompts = [p for p, _ in ai.calls]
    assert all(kw["system_prompt"] == steps_engine.system_prompt() for _, kw in ai.calls)
    assert "APPROVED by the user: click \"Buy now\"" in prompts[2]
    assert 'sb2 button "Buy now"' in prompts[1]  # tools.element_lines format
    # every model call reached the usage ledger
    assert sum(1 for _ in open(bpaths.usage_path())) == 3


def test_a_denied_click_does_not_run(steps_bot, monkeypatch):
    b = steps_bot
    ai = FakeAI([
        json.dumps({"thought": "Buying.", "action": "click", "ref": "sb2"}),
        json.dumps({"thought": "Fine.", "action": "done", "message": "Left it in the cart."}),
    ])
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)

    def deny():
        assert _wait_for(lambda: b.asking)
        b.send("no, just check the price")
    t = threading.Thread(target=deny)
    t.start()
    b.start_task("buy it")
    b.thread.join(15)
    t.join(5)
    evs = _events(b)
    assert not any(c[0] == "click" for c in b.browser.calls)
    assert any(e["role"] == "system" and e["text"] == "Denied; the bot will try something else." for e in evs)
    assert "DENIED by the user: click \"Buy now\". Do not retry it; USER: no, just check the price" not in ai.calls[1][0]  # _NO drops the no-line
    assert "DENIED by the user: click \"Buy now\"" in ai.calls[1][0]
    assert evs[-1]["role"] == "done"


# ---- the local tier: greet, the download question, a cold model -----------------
class FakeModels:
    """`fused_ai.models`: a catalog that knows the local-4b repo, and a download."""

    def __init__(self, downloaded=False):
        self.downloaded = downloaded
        self.downloads = []

    def catalog(self):
        return {"capabilities": [{"capability": "text-generation", "models": [
            {"id": botmod.LOCAL_MODELS["local-4b"], "size_gb": 5.2, "downloaded": self.downloaded}]}]}

    def download(self, model_id, capability=None, on_progress=None, timeout=None):
        self.downloads.append((model_id, capability))
        for done in (0, 40, 100):
            on_progress({"id": "job-1", "state": "running", "done": done, "total": 100})
        self.downloaded = True
        return {"id": "job-1", "state": "done"}


class _Loading(Exception):
    type = "model_loading"


def _local(b, ai):
    b.meta["model"] = "local-4b"
    b.save()
    ai.models = FakeModels()
    return ai


def test_greet_on_an_undownloaded_local_model_asks_nothing(steps_bot, monkeypatch):
    b = steps_bot
    ai = _local(b, FakeAI([]))
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    b.greet()
    b.thread.join(5)
    assert not b.thread.is_alive()
    assert _events(b) == [] and ai.calls == [] and ai.models.downloads == []
    assert b.meta["status"] == "idle"
    # ...so the user's first message starts a task instead of answering a hidden question
    b.send("what is on example.com?")
    assert _wait_for(lambda: any(e["role"] == "question" for e in _events(b)))
    assert b.engine == "steps" and b.meta["task"] == "what is on example.com?"


def test_greet_on_a_downloaded_local_model_says_hi(steps_bot, monkeypatch):
    b = steps_bot
    ai = _local(b, FakeAI(["Hi, I'm Shopper."]))
    ai.models.downloaded = True
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    b.greet()
    b.thread.join(5)
    assert [(e["role"], e["text"]) for e in _events(b)] == [("done", "Hi, I'm Shopper.")]
    assert "local-4b" in b.model_ready


def test_local_task_downloads_on_yes_then_waits_for_a_cold_model(steps_bot, monkeypatch):
    b = steps_bot
    ai = _local(b, FakeAI([
        json.dumps({"thought": "Opening it.", "action": "goto", "url": "https://example.com"}),
        json.dumps({"thought": "Got it.", "action": "done", "message": "The title is Example Domain."}),
    ]))
    loads = []
    real_text = ai.text

    def text(prompt, **kw):  # the first call after the download finds the model still loading
        if not loads:
            loads.append(1)
            raise _Loading("model is loading")
        return real_text(prompt, **kw)
    ai.text = text
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    monkeypatch.setattr(botmod, "MODEL_LOADING_SLEEP_S", 0)
    b.start_task("title of example.com")
    assert _wait_for(lambda: b.asking)
    q = next(e for e in _events(b) if e["role"] == "question")
    assert q["text"] == "This bot's model needs to download (~5.2 GB) before it can run locally. Download it now?"
    assert q["options"] == ["Download now", "Cancel"] and b.meta["status"] == "waiting"
    b.send("Download now")
    b.thread.join(15)
    assert not b.thread.is_alive()
    assert ai.models.downloads == [(botmod.LOCAL_MODELS["local-4b"], "text-generation")]
    evs = _events(b)
    sys_lines = [e["text"] for e in evs if e["role"] == "system"]
    assert sys_lines == ["Task started: title of example.com", "Downloading model… 0%", "Downloading model… 40%",
                         "Downloading model… 100%", "Model downloaded.", "Local model is loading into memory; waiting…"]
    assert [e["text"] for e in evs if e["role"] == "action"] == ["goto https://example.com"]
    assert evs[-1]["role"] == "done" and evs[-1]["text"] == "The title is Example Domain."
    assert b.meta.get("dl_pct") is None and b.meta["status"] == "idle"
    # every model call resolved the alias to the repo id
    assert {kw["model"] for _, kw in ai.calls} == {botmod.LOCAL_MODELS["local-4b"]}


def test_local_task_cancel_does_not_download(steps_bot, monkeypatch):
    b = steps_bot
    ai = _local(b, FakeAI([]))
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    b.start_task("anything")
    assert _wait_for(lambda: b.asking)
    b.send("Cancel")
    b.thread.join(5)
    assert ai.models.downloads == [] and ai.calls == []
    assert _events(b)[-1]["text"].startswith("OK, I won't download the model.")


def test_ask_sets_waiting_on_and_step_cap(steps_bot, monkeypatch):
    b = steps_bot
    ai = FakeAI([
        json.dumps({"thought": "Need a size.", "action": "ask", "message": "Which size?", "options": ["S", "M"]}),
        json.dumps({"thought": "Ok.", "action": "done", "message": "Picked M."}),
    ])
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    seen = {}

    def answer():
        assert _wait_for(lambda: b.asking)
        seen.update(status=b.meta["status"], waiting_on=b.meta.get("waiting_on"),
                    step=b.meta.get("step"), step_cap=b.meta.get("step_cap"))
        b.send("M")
    t = threading.Thread(target=answer)
    t.start()
    b.start_task("pick a size")
    b.thread.join(15)
    t.join(5)
    q = next(e for e in _events(b) if e["role"] == "question")
    assert seen == {"status": "waiting", "waiting_on": q["seq"], "step": 1, "step_cap": botmod.MAX_STEPS}
    assert b.meta["status"] == "idle" and b.meta["waiting_on"] is None


def test_approval_sets_waiting_on_its_card(steps_bot, monkeypatch):
    b = steps_bot
    ai = FakeAI([
        json.dumps({"thought": "Buying.", "action": "click", "ref": "sb2"}),
        json.dumps({"thought": "Done.", "action": "done", "message": "Bought."}),
    ])
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    seen = {}

    def approve():
        assert _wait_for(lambda: b.asking)
        seen["waiting_on"] = b.meta.get("waiting_on")
        b.send("approve")
    t = threading.Thread(target=approve)
    t.start()
    b.start_task("buy it")
    b.thread.join(15)
    t.join(5)
    appr = next(e for e in _events(b) if e["role"] == "approval")
    assert seen["waiting_on"] == appr["seq"]
    assert b.meta["waiting_on"] is None


def test_a_repeated_call_is_a_note_and_actions_carry_a_one_liner_and_detail(steps_bot, monkeypatch):
    b = steps_bot
    ai = FakeAI([
        json.dumps({"thought": "Reading.", "action": "read"}),
        json.dumps({"thought": "Again.", "action": "read"}),
        json.dumps({"thought": "Ok.", "action": "done", "message": "Read it."}),
    ])
    monkeypatch.setattr(botmod, "_fused_ai", lambda: ai)
    raw = "RESULT:\n" + "x" * 2000
    monkeypatch.setattr(steps_engine.tools, "call_key", lambda bot, act, d: ("read",) if act == "read" else None)
    monkeypatch.setattr(steps_engine.tools, "execute", lambda bot, act, d, obs: ("read page", raw))
    monkeypatch.setattr(steps_engine.tools, "describe", lambda bot, act, d, obs: "read page")
    monkeypatch.delattr(steps_engine.tools, "ui_summary", raising=False)  # the local fallback
    b.start_task("read it")
    b.thread.join(15)
    evs = _events(b)
    act = [e for e in evs if e["role"] == "action"]
    assert len(act) == 1
    assert act[0]["result"] == "x" * 159 + "…" and act[0]["detail"] == raw[:1500]
    notes = [e for e in evs if e["role"] == "note"]
    assert [n["text"] for n in notes] == ["Already ran read page; using the result above."]
    assert not any(e["role"] == "thought" and e["text"].startswith("Already ran") for e in evs)


def test_one_liner_prefers_ui_summary_and_survives_it_raising(monkeypatch):
    monkeypatch.setattr(steps_engine.tools, "ui_summary", lambda n, r, change="", obs=None, label="": "Read 3 items", raising=False)
    assert steps_engine._one_liner("read", "long\ntext", {}) == "Read 3 items"

    def boom(*a, **kw):
        raise ValueError("bug")
    monkeypatch.setattr(steps_engine.tools, "ui_summary", boom, raising=False)
    assert steps_engine._one_liner("goto", "\n  ok,   now at x\nmore", {}) == "ok, now at x"
    assert len(steps_engine._one_liner("goto", "y" * 500, {})) == 160


def test_bad_json_three_times_errors_the_task(steps_bot, monkeypatch):
    b = steps_bot
    monkeypatch.setattr(botmod, "_fused_ai", lambda: FakeAI(["I think I should click.", "", "still prose"]))
    b.start_task("anything")
    b.thread.join(15)
    evs = _events(b)
    bad = [e for e in evs if e["role"] == "error" and e["text"] == "Model returned no usable JSON; retrying"]
    assert len(bad) == 3 and bad[0]["result"] == "23 chars: I think I should click."
    assert bad[1]["result"] == "0 chars (empty reply)"
    assert evs[-1]["role"] == "error" and "model kept returning invalid JSON" in evs[-1]["text"]
    assert b.meta["status"] == "error"
    assert os.path.isfile(os.path.join(b.cache_dir, "badjson", "1.txt"))
