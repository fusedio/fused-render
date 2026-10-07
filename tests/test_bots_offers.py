"""The bot's app offers (`offer`), the step-1 app hints and the iMessage reply
hints — a port of OpenBot tests/test_offers.py onto fused_render.bots.

They build a Bot without a browser (Bot.__new__) and stub the two things an
offer touches outside the transcript: waiting on the user and starting a build."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import textwrap
import threading
import time

import pytest

from fused_render.bots import apptools
from fused_render.bots import bot as agents
from fused_render.bots import paths as bpaths


def make_app(root, folder, tools=None, body=None):
    """A fake fused-render app under `root`: index.html with the marker, t.py,
    and an mcp.toml unless `tools` is "" (OpenBot tests/conftest.py make_app)."""
    d = os.path.join(root, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write('<!doctype html><html><head><meta name="fused-app" /><title>Fake</title></head><body></body></html>')
    with open(os.path.join(d, "t.py"), "w") as f:
        f.write(body or textwrap.dedent('''
            def main(x: int = 1, mode: str = "a"):
                return {"x": x, "mode": mode}
        '''))
    if tools is None:
        tools = textwrap.dedent('''
            [[tool]]
            name = "fake_echo"
            description = "Echoes x back. Read-only."
            file = "t.py"
            entrypoint = "main"
        ''')
    if tools:
        with open(os.path.join(d, "mcp.toml"), "w") as f:
            f.write(tools)
    return d


def read_events(b):
    with open(b.events_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A Bot with a folder and a transcript but no Chrome: enough for offers."""
    ws = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    apps_root = bpaths.apps_root()
    os.makedirs(apps_root)
    monkeypatch.setattr(apptools, "ROOTS", [apps_root])
    monkeypatch.setattr(apptools, "SKIP_DIR", "/nonexistent")
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    b = agents.Bot.__new__(agents.Bot)
    b.id = "t1"
    b.dir = bpaths.bot_dir("t1")
    b.cache_dir = bpaths.bot_cache_dir("t1")
    os.makedirs(b.dir, exist_ok=True)
    b.events_path = os.path.join(b.dir, "events.jsonl")
    b.lock = threading.RLock()
    b.meta = {"id": "t1", "name": "Tester", "status": "idle"}
    b.seq = 0
    b.inbox, b.wake = [], threading.Event()
    b.stop_flag, b.pause_flag = threading.Event(), threading.Event()
    b.asking, b._offers, b.task_origin = False, 0, "manual"
    b.thread, b.engine = None, None
    b.apps_root = apps_root
    return b


def answer_with(b, answers, timed_out=False):
    b._await_answer = lambda timeout=None: (list(answers), timed_out)


# ---- regexes -------------------------------------------------------------------
@pytest.mark.parametrize("task", [
    "build me a price tracker for these three laptops",
    "can you make a dashboard for this?",
    "update the expense tracker app to show totals",
    "set up a daily tracker",
    "create a page for my notes",
])
def test_asks_for_app(task):
    assert agents._ASKS_FOR_APP.search(task)


@pytest.mark.parametrize("task", [
    "make sure the app is open before you post",
    "update my LinkedIn profile page",
    "fill the form on the contact page",
    "write a report on Tesla's Q3",
    "check my linkedin feed",
])
def test_not_asking_for_app(task):
    assert not agents._ASKS_FOR_APP.search(task)


def test_app_worthy():
    assert agents._APP_WORTHY.search("track the price of the Pixel every day")
    assert agents._APP_WORTHY.search("compare these three laptops")
    assert not agents._APP_WORTHY.search("what is the capital of France")


def test_relevant_apps_scores_name_and_words():
    items = [{"folder": "expense-tracker", "name": "Expense tracker", "desc": "Log expenses by category and see monthly totals."},
             {"folder": "linkedin-digest", "name": "LinkedIn digest", "desc": "Summaries of your feed."}]
    hits = agents._relevant_apps("add this month's expenses to the expense tracker", items)
    assert [a["folder"] for _, a in hits] == ["expense-tracker"]
    assert hits[0][0] >= 3  # the whole name is in the task
    assert agents._relevant_apps("what is the weather in Paris", items) == []


# ---- verdicts ------------------------------------------------------------------
@pytest.mark.parametrize("said,want", [
    ("Build it", "yes"), ("yes please", "yes"), ("ok", "yes"), ("go for it", "yes"), ("use it", "yes"),
    ("Not now", "no"), ("no thanks", "no"), ("nah", "no"), ("maybe later", "no"),
    ("call it Budget instead", None),
])
def test_offer_verdict_loose(said, want):
    assert agents.Bot._offer_verdict([said], "Build it") == want


@pytest.mark.parametrize("said,want", [
    ("yes", "yes"), ("Build it", "yes"), ("ok, please", "yes"), ("no", "no"), ("not now thanks", "no"),
    ("ok now go to linkedin and check my feed", None), ("go to https://example.com", None), ("no idea, check the site", None),
])
def test_offer_verdict_strict(said, want):
    assert agents.Bot._offer_verdict([said], "Build it", strict=True) == want


# ---- hints ---------------------------------------------------------------------
def test_offer_hints(bot):
    make_app(bot.apps_root, "expense-tracker", tools="")
    with open(os.path.join(bot.apps_root, "expense-tracker", "README.md"), "w") as f:
        f.write("# Expense tracker\n\nLog expenses by category.\n")
    hints = bot._offer_hints("add this week's expenses to my expense tracker")
    assert any("Expense tracker" in h and "`offer`" in h for h in hints)
    assert any("straight to `build`" in h for h in bot._offer_hints("build me a small app that lists my routines"))
    assert any("want again" in h for h in bot._offer_hints("track the price of this laptop every day"))
    assert bot._offer_hints("what is the capital of France") == []
    bot.task_origin = "routine"
    assert bot._offer_hints("track the price of this laptop every day") == []


def test_offer_hints_skip_declined(bot):
    make_app(bot.apps_root, "expense-tracker", tools="")
    bot.meta["offers_declined"] = {"expense-tracker": time.time()}
    assert bot._offer_hints("open the expense tracker and add lunch") == []


# ---- the offer itself ----------------------------------------------------------
def test_build_offer_accepted_starts_build(bot):
    calls = []
    bot.build = lambda name, spec, fresh=False: (calls.append((name, spec)) or ("build \"Price watch\"", "started; link X"))
    answer_with(bot, ["Build it"])
    history = []
    stopped = bot._offer({"name": "Price watch", "text": "A page listing the three laptops with their prices.",
                          "message": "I found the prices. Want a page for them?"}, {}, history)
    assert stopped is False
    assert calls == [("Price watch", "A page listing the three laptops with their prices.")]
    q = [e for e in read_events(bot) if e["role"] == "question"][0]
    assert q["options"] == ["Build it", "Not now"]
    assert q["offer"]["kind"] == "build" and q["offer"]["name"] == "Price watch"
    assert "pending_offer" not in bot.meta  # settled
    assert any("USER ACCEPTED" in h for h in history)
    assert bot.meta["status"] == "running"


def test_build_offer_takes_spec_spelling_of_the_tool_table(bot):
    calls = []
    bot.build = lambda name, spec, fresh=False: (calls.append(spec) or ("build", "started"))
    answer_with(bot, ["Build it"])
    bot._offer({"name": "Price watch", "spec": "the agent engine spells it spec", "message": "Want it?"}, {}, [])
    assert calls == ["the agent engine spells it spec"]


def test_build_offer_accept_with_note_reaches_spec(bot):
    calls = []
    bot.build = lambda name, spec, fresh=False: (calls.append(spec) or ("build", "started"))
    answer_with(bot, ["yes, but call the columns Model and Price"])
    bot._offer({"name": "Price watch", "text": "spec"}, {}, [])
    assert "call the columns Model and Price" in calls[0]


def test_offer_declined_is_remembered_and_not_repeated(bot):
    answer_with(bot, ["Not now"])
    history = []
    bot._offer({"name": "Price watch", "text": "spec"}, {}, history)
    assert "price watch" in bot.declined_offers()
    assert any("DECLINED" in h for h in history)
    bot._offers = 0  # a new task
    history = []
    bot._offer({"name": "Price watch", "text": "spec"}, {}, history)
    assert history and "declined" in history[-1]


def test_offer_limits(bot):
    history = []
    bot.task_origin = "routine"
    bot._offer({"name": "X", "text": "spec"}, {}, history)
    assert "routine" in history[-1]
    bot.task_origin = "manual"
    history = []
    bot._offer({"name": "X"}, {}, history)  # no spec, no such app
    assert "needs `text`" in history[-1]
    answer_with(bot, ["Build it"])
    bot.build = lambda name, spec, fresh=False: ("build", "started")
    bot._offer({"name": "X", "text": "spec"}, {}, history)
    history = []
    bot._offer({"name": "Y", "text": "spec"}, {}, history)
    assert "one offer per task" in history[-1]


def test_use_offer_shows_existing_app(bot):
    make_app(bot.apps_root, "expense-tracker", tools="")
    answer_with(bot, ["Use it"])
    history = []
    bot._offer({"name": "expense tracker", "message": "This one fits."}, {}, history)
    evs = read_events(bot)
    q = [e for e in evs if e["role"] == "question"][0]
    assert q["options"] == ["Use it", "Not now"] and q["offer"]["kind"] == "use" and q["app"]["dir"].endswith("expense-tracker")
    assert any(e["role"] == "thought" and e.get("app", {}).get("dir", "").endswith("expense-tracker") for e in evs)
    assert any("ACCEPTED your offer to use" in h for h in history)


def test_offer_sets_waiting_on_its_card_and_clears_it_after(bot):
    seen = {}

    def wait(timeout=None):
        seen.update(status=bot.meta["status"], waiting_on=bot.meta.get("waiting_on"))
        return ["Not now"], False
    bot._await_answer = wait
    bot._offer({"name": "Price watch", "text": "spec"}, {}, [])
    q = [e for e in read_events(bot) if e["role"] == "question"][0]
    assert seen == {"status": "waiting", "waiting_on": q["seq"]}
    assert bot.meta["status"] == "running" and bot.meta["waiting_on"] is None


def test_offer_stopped_meanwhile_leaves_no_waiting_on(bot):
    def wait(timeout=None):
        bot.stop_flag.set()
        return [], False
    bot._await_answer = wait
    assert bot._offer({"name": "Price watch", "text": "spec"}, {}, []) is True
    assert bot.meta.get("waiting_on") is None


def test_deliver_emits_a_note_not_a_thought(bot):
    d = make_app(bot.apps_root, "expense-tracker", tools="")
    res = apptools.RunResult(ok=True, text='{"total": 3}')
    label, out = bot._deliver("tool expense › sum", res, d, "sum", {"month": 9}, "tool-x-sum", tools=1)
    assert out.startswith("RESULT:")
    used = [e for e in read_events(bot) if (e.get("text") or "").startswith("Used ")]
    assert [e["role"] for e in used] == ["note"]
    assert json.loads(used[0]["detail"]) == {"month": 9}
    assert used[0]["app"]["dir"] == d  # the app card rides on the first use
    bot._deliver("tool expense › sum", res, d, "sum", {}, "tool-x-sum", tools=1)
    second = [e for e in read_events(bot) if (e.get("text") or "").startswith("Used ")][1]
    assert second["role"] == "note" and "app" not in second
    assert "Used " not in "\n".join(bot.past_conversation())


def test_unanswered_offer_stays_pending_then_bare_yes_builds(bot):
    answer_with(bot, [], timed_out=True)
    history = []
    bot._offer({"name": "Price watch", "text": "spec"}, {}, history)
    po = bot.meta.get("pending_offer")
    assert po and po["name"] == "Price watch" and po["seq"] > 0
    assert any("No answer" in h for h in history)
    started = threading.Event()

    def fake_build(name, spec, fresh=False):
        bot.meta.setdefault("builds", []).append({"name": name, "dir": os.path.join(bot.apps_root, "price-watch")})
        started.set()
        return "build", "started"
    bot.build = fake_build
    # an unrelated message clears the offer and is not consumed
    assert bot._answer_pending_offer(po, "ok now go to linkedin and check my feed") is False
    assert "pending_offer" not in bot.meta
    bot.meta["pending_offer"] = po
    assert bot._answer_pending_offer(po, "yes") is True
    assert started.wait(5)
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["role"] == "done" for e in read_events(bot)):
        time.sleep(0.05)
    assert any(e["role"] == "done" and "Building" in e["text"] for e in read_events(bot))


def test_yes_regex_does_not_take_go_to_as_approval():
    assert agents._YES.match("go ahead") and agents._YES.match("go") and agents._YES.match("yes")
    assert not agents._YES.match("go to google instead") and not agents._YES.match("go back")


def test_pending_offer_bare_no_declines(bot):
    po = {"kind": "build", "name": "Price watch", "dir": "", "spec": "spec", "seq": 3, "ts": time.time()}
    bot.meta["pending_offer"] = po
    assert bot._answer_pending_offer(po, "no thanks") is True
    assert "price watch" in bot.declined_offers() and "pending_offer" not in bot.meta


def test_past_conversation_labels_offers(bot):
    bot.emit("question", "Want an app for this?", options=["Build it", "Not now"], offer={"kind": "build", "name": "Price watch"})
    bot.emit("user", "Build it")
    lines = bot.past_conversation()
    assert lines[0].startswith("[#1] YOU OFFERED TO BUILD THE APP 'Price watch': Want an app")  # [#seq]: recall fetches it
    assert lines[1] == "[#2] USER: Build it"


# ---- iMessage reply hints (channels/router.py render, docs §10) ------------------
def test_outbound_text_adds_how_to_answer():
    from fused_render.bots.channels import router as _router
    from fused_render.bots.channels.imessage import CAPS as _caps
    assert _router.render({"role": "question", "text": "Want it?", "options": ["Build it", "Not now"]}, _caps) == \
        ("Want it?\n\nReply 1 Build it · 2 Not now", ["Build it", "Not now"])
    assert _router.render({"role": "done", "text": "All done."}, _caps) == ("All done.", [])
