"""The Claude Code harness (fused_render/bots/agent_engine.py, botmcp.py;
docs/bots.md §6), driven end to end against a fake `claude`
(tests/_bots_fake_claude.py) that spawns the real botmcp.py from the mcp.json
the engine wrote and calls its tools over MCP. A tiny threaded HTTP server
stands in for the two routes botmcp posts to; a fake Bot and a fake Browser
stand in for bot.py / browser.py."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.bots import agent_engine, apptools, tools

from _claude_stub_cli import write_stub_cli

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "_bots_fake_claude.py")
BOTMCP = os.path.join(os.path.dirname(HERE), "fused_render", "bots", "botmcp.py")
JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


# ----------------------------------------------------------------- fakes ---
def _el(ref, tag, text="", **kw):
    return {"ref": ref, "tag": tag, "text": text, **kw}


PAGES = {
    "https://shop.test/": {
        "title": "Shop", "text": "Welcome to the shop.",
        "elements": [_el("sb1", "a", "Products", href="/products"), _el("sb2", "button", "Buy now"),
                     _el("sb3", "input", "", name="q", empty=True)]},
    "https://shop.test/products": {
        "title": "Products", "text": "Widget $5. Gadget $7.",
        "elements": [_el("sb1", "a", "Home", href="/"), _el("sb4", "a", "Widget"), _el("sb5", "button", "Add to cart")]},
    "https://shop.test/thanks": {
        "title": "Thanks", "text": "Order placed.",
        "elements": [_el("sb1", "a", "Home"), _el("sb6", "a", "Orders"), _el("sb7", "a", "Help")]},
}
LINKS = {("https://shop.test/", "sb1"): "https://shop.test/products",
         ("https://shop.test/", "sb2"): "https://shop.test/thanks",
         ("https://shop.test/products", "sb1"): "https://shop.test/"}


LONG = "https://shop.test/long"   # four controls, two on screen at a time; `scroll` moves the window
PAGES[LONG] = {"title": "Long", "text": "A long list.",
               "elements": [_el(f"sb{i}", "a", f"Item {i}") for i in range(1, 5)]}


class FakeBrowser:
    def __init__(self):
        self.url = "about:blank"
        self.calls = []
        self.on_goto = None
        self.started = False
        self.observes = 0
        self.scroll_pos = 0
        self.shoot_actions = True
        self.shoot_seen = []   # shoot_actions as each action saw it

    def start(self, visible):
        self.started = True

    def alive(self):
        return self.started

    def status_cached(self):
        return {"running": self.started, "url": self.url}

    def observe(self):
        self.observes += 1
        page = PAGES.get(self.url, {"title": "", "text": "", "elements": []})
        els = [dict(e) for e in page["elements"]]
        if self.url == LONG:
            for i, e in enumerate(els):
                if not self.scroll_pos <= i < self.scroll_pos + 2:
                    e["offscreen"] = True
        return {"url": self.url, "title": page["title"], "text": page["text"], "elements": els, "tabs": []}

    def scroll(self, direction="down", **kw):
        self.calls.append(("scroll", direction))
        self.scroll_pos += 2 if direction == "down" else -2
        return {}

    def goto(self, url):
        self.shoot_seen.append(self.shoot_actions)
        self.calls.append(("goto", url))
        self.url = url if "://" in url else "https://" + url
        if self.on_goto:
            self.on_goto(url)
        return {"url": self.url}

    def click(self, ref="", text="", x=None, y=None, backend=None):
        self.calls.append(("click", ref, text))
        self.url = LINKS.get((self.url, ref), self.url)
        return {"url": self.url}

    def screenshot(self):
        return None

    def screenshot_jpeg(self):
        return JPEG


class FakeBot:
    def __init__(self, bid="b1"):
        self.id = bid
        self.meta = {"name": "Tester", "model": "haiku", "effort": "low", "approval": "ask", "routines": []}
        self.browser = FakeBrowser()
        self.events = []
        self.statuses = []
        self.inbox = []
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.wake = threading.Event()
        self.pause_flag = threading.Event()
        self.stop_flag = threading.Event()
        self.asking = False
        self.window_closed = False
        self.task_origin = "manual"
        self.task_started = time.time()
        self.task_dir = None
        self.outcomes = []
        self.thumbs = 0
        self.action_obs = []   # how many observes had run when each `action` was emitted
        self.status_log = []   # (status, kwargs) per set_status

    # transcript / status
    def emit(self, role, text, **extra):
        with self.cond:
            ev = {"seq": len(self.events) + 1, "ts": time.time(), "role": role, "text": text,
                  **{k: v for k, v in extra.items() if v is not None}}
            self.events.append(ev)
            if role == "action":
                self.action_obs.append(self.browser.observes)
            self.cond.notify_all()
        return ev

    def set_status(self, status, **kw):
        self.meta["status"] = status
        self.meta.update(kw)
        self.statuses.append(status)
        self.status_log.append((status, dict(kw)))

    def wait_event(self, role, timeout=15, pred=lambda ev: True):
        end = time.time() + timeout
        with self.cond:
            while True:
                hit = next((e for e in self.events if e["role"] == role and pred(e)), None)
                if hit or time.time() > end:
                    return hit
                self.cond.wait(0.05)

    def roles(self):
        return [e["role"] for e in self.events]

    def say(self, text):
        """What Bot.send does for a running task."""
        with self.lock:
            self.inbox.append(text)
        self.wake.set()

    # the surface agent_engine uses
    def _drain_inbox(self):
        with self.lock:
            msgs, self.inbox = self.inbox, []
        return msgs

    def stop(self):
        self.stop_flag.set()
        self.pause_flag.clear()
        self.wake.set()
        self.emit("system", "Stop requested")
        agent_engine.stop(self)

    def window(self, visible):
        self.meta["visible"] = visible
        if visible:
            self.pause_flag.set()
            self.meta["control"] = True

    def _closed_window_note(self, history):
        pass

    def _recover_popup(self):
        pass

    def collect_task_artifacts(self, final_msg):
        return []

    def _routine_outcome(self, task, result, message):
        self.outcomes.append((result, message))

    def _offer(self, d, obs, history):
        history.append(f"OFFERED (build): {d.get('name')}")
        return False

    def _offer_hints(self, task):
        return []

    def _step_thumb(self):
        self.thumbs += 1
        return f"{self.thumbs}.jpg"

    def _skill_dirs(self, task):
        return []

    def memory_for_prompt(self):
        return "- the shop search is at /search"

    def skills_for_prompt(self, task):
        return ""

    def past_conversation(self):
        return ["user: hello", "bot: hi there"]

    def contacts(self):
        return []

    def contact(self, d):
        return None

    def py_ref(self, d):
        return None, None, {}

    files = {}  # name -> path; set by a test to give the bot FILES

    def all_files(self):
        return [{"name": n, "size": os.path.getsize(p), "kind": "saved"} for n, p in self.files.items()]

    def resolve_file(self, name):
        if name in self.files:
            return self.files[name]
        raise ValueError(f"file not found: {name}")

    def task_artifacts(self):
        return []

    def declined_offers(self):
        return []

    def remember(self, note):
        return "remembered"

    def save(self):
        pass


# ---------------------------------------------------------------- server ---
BOTS = {}


class Routes(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bot(self, tail):
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "bots"] or parts[3] != tail:
            return None
        return BOTS.get(parts[2])

    def do_GET(self):
        bot = self._bot("tools")
        if bot is None:
            return self._send(404, {"error": "no such bot"})
        token = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query)).get("token")
        try:
            self._send(200, {"tools": agent_engine.roster_for(bot, token)})
        except agent_engine.StaleToken as e:
            self._send(409, {"error": str(e)})

    def do_POST(self):
        bot = self._bot("tool")
        if bot is None:
            return self._send(404, {"error": "no such bot"})
        if self.headers.get("X-Fused") != "1":
            return self._send(400, {"error": "missing X-Fused"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            self._send(200, agent_engine.handle_tool(bot, body.get("token"), body.get("name"), body.get("args")))
        except agent_engine.StaleToken as e:
            self._send(409, {"error": str(e)})


@pytest.fixture
def server(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Routes)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", origin)
    yield origin
    srv.shutdown()
    srv.server_close()
    BOTS.clear()


@pytest.fixture(autouse=True)
def quiet_apptools(monkeypatch):
    """No real apps / app tools under test: the roster and the first message
    see none."""
    monkeypatch.setattr(apptools, "available", lambda: False)
    monkeypatch.setattr(apptools, "apps", lambda *a, **k: [])
    monkeypatch.setattr(apptools, "registry", lambda *a, **k: [])
    monkeypatch.setattr(apptools, "apps_section", lambda *a, **k: "")
    monkeypatch.setattr(apptools, "prompt_section", lambda *a, **k: "")
    monkeypatch.setattr(apptools, "skill_section", lambda *a, **k: "")
    monkeypatch.setattr(agent_engine, "RETRY_SLEEP_S", 0.0)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    body = f"#!{sys.executable}\nimport runpy\nrunpy.run_path({FAKE!r}, run_name='__main__')\n"
    path = write_stub_cli(tmp_path / "bin", body)
    # fused-render's claude_health.resolve() reads this override first;
    # _bots_conftest points it at a missing file.
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", path)
    log = tmp_path / "fake.jsonl"
    monkeypatch.setenv("BOTS_FAKE_LOG", str(log))

    def rows(key=None):
        if not log.exists():
            return []
        out = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        return [r for r in out if key in r] if key else out
    return rows


def start(bot, script, monkeypatch, task="say hi"):
    monkeypatch.setenv("BOTS_FAKE_SCRIPT", json.dumps(script))
    BOTS[bot.id] = bot
    t = threading.Thread(target=agent_engine.run, args=(bot, task, task), daemon=True)
    t.start()
    return t


def finish(t, timeout=30):
    t.join(timeout)
    assert not t.is_alive(), "the task thread did not end"


def run_task(bot, script, monkeypatch, task="say hi"):
    finish(start(bot, script, monkeypatch, task))


# ----------------------------------------------------------------- tests ---
def test_text_then_done(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"text": "Hi! How can I help?"}, {"result": "Hi! How can I help?"}], monkeypatch)
    assert bot.roles() == ["system", "done"], bot.events
    assert bot.events[0]["text"] == "Task started: say hi"
    assert bot.events[-1]["text"] == "Hi! How can I help?"
    assert bot.meta["status"] == "idle"
    assert bot.outcomes == [("done", "Hi! How can I help?")]
    assert agent_engine.session(bot) is None  # the token is gone with the task

    # The spawn: docs §6 argv, mcp.json, the first message, low effort's budget clamp.
    argv = fake_cli("argv")[0]["argv"]
    for flag in ("-p", "--verbose", "--replay-user-messages", "--strict-mcp-config", "--autocompact",
                 "--disable-slash-commands", "--tools=", "--setting-sources="):
        assert flag in argv
    # Bot threads (docs §6): sessions persist so the next turn can --resume them; a fake Bot without a
    # conversation record runs every turn fresh, so nothing is resumed here.
    assert "--no-session-persistence" not in argv and "--resume" not in argv
    assert argv[argv.index("--autocompact") + 1] == str(agent_engine.WINDOW_DEFAULT)
    assert "--include-partial-messages" not in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__bot__*"
    with open(argv[argv.index("--system-prompt-file") + 1]) as f:
        assert f.read() == agent_engine.SYSTEM_PROMPT
    cfg = json.load(open(argv[argv.index("--mcp-config") + 1]))
    srv = cfg["mcpServers"]["bot"]
    assert srv["args"][1:3] == [server, "b1"] and srv["timeout"] == (agent_engine.APPROVAL_WAIT_S + 60) * 1000
    tools_listed = fake_cli("tools")[0]["tools"]
    assert "goto" in tools_listed and "tool" not in tools_listed and "text" not in tools_listed
    first = fake_cli("user")[0]["user"]
    assert first.startswith("YOU: 'Tester' · model haiku")
    assert "MEMORY (notes you saved" in first and "bot: hi there" in first
    assert first.rstrip().endswith("TASK: say hi")
    assert {"control": "set_max_thinking_tokens"}.items() <= fake_cli("control")[0].items()


def test_actions_carry_change_report_and_compact_page(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [
        {"text": "Opening the shop."},
        {"tool": "goto", "args": {"url": "https://shop.test/"}},
        {"text": "Going to products."},
        {"tool": "click", "args": {"ref": "sb1"}},
        {"text": "Widget costs $5."},
        {"result": "Widget costs $5."}], monkeypatch)
    roles = bot.roles()
    assert roles == ["system", "thought", "action", "thought", "action", "done"], bot.events
    acts = [e for e in bot.events if e["role"] == "action"]
    # The chip says what the step changed (tools.ui_summary), not the model's status line.
    assert acts[0]["text"] == "goto https://shop.test/"
    # A navigation reads as where we are now; the controls diff is noise on a new page (it is kept for same-page changes).
    assert acts[0]["result"] == 'now at shop.test · "Shop"'
    assert "detail" not in acts[0]  # the change report fits on the chip: nothing more to disclose
    assert acts[1]["text"] == 'click "Products"' and acts[1]["thumb"] == "2.jpg"
    assert acts[1]["result"] == 'now at shop.test/products · "Products"'
    # Each action is emitted after its post-step observe (goto: pre + post; click: post).
    assert bot.action_obs == [2, 3]
    # The action's own screenshot is off for the task (the observe takes one), and back on after.
    assert bot.browser.shoot_seen == [False] and bot.browser.shoot_actions is True
    assert bot.meta["step"] == 2 and bot.meta["step_cap"] == agent_engine.MAX_STEPS
    assert bot.events[-1]["text"] == "Widget costs $5."
    calls = fake_cli("call")
    assert "CHANGE: url changed to https://shop.test/" in calls[0]["result"]
    assert "CURRENT PAGE\nurl: https://shop.test/" in calls[0]["result"]
    assert 'sb2 button "Buy now"' in calls[0]["result"]
    assert "CHANGE: url changed to https://shop.test/products" in calls[1]["result"]
    assert "Widget $5." in calls[1]["result"] and calls[1]["images"] == []
    assert bot.browser.calls == [("goto", "https://shop.test/"), ("click", "sb1", "Products")]


def test_risky_click_approved(server, fake_cli, monkeypatch):
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "Bought."}], monkeypatch)
    ev = bot.wait_event("approval")
    assert ev and ev["text"].startswith('About to click "Buy now". The button says "Buy now"')
    assert ("click", "sb2", "Buy now") not in bot.browser.calls
    bot.say("approve")
    finish(t)
    assert ("click", "sb2", "Buy now") in bot.browser.calls
    res = fake_cli("call")[1]["result"]
    assert res.startswith('APPROVED by the user: click "Buy now"') and "url changed to https://shop.test/thanks" in res
    assert "waiting" in bot.statuses


def test_risky_click_denied(server, fake_cli, monkeypatch):
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "Did not buy."}], monkeypatch)
    assert bot.wait_event("approval")
    bot.say("no, too expensive")
    finish(t)
    assert ("click", "sb2", "Buy now") not in bot.browser.calls
    res = fake_cli("call")[1]["result"]
    assert res.startswith('DENIED by the user: click "Buy now". They said: "no, too expensive". Do not retry it;')
    assert "USER INSTRUCTION" not in res  # a plain "no …" is the verdict (with its reason), not an instruction (OpenBot _NO)
    assert any(e["role"] == "system" and e["text"].startswith("Denied") for e in bot.events)


def test_denied_click_is_not_asked_again(server, fake_cli, monkeypatch):
    """Seen live: the model re-issued a denied click one step later. The second
    identical call is refused without a second card; a new user message clears it."""
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "Did not buy."}], monkeypatch)
    assert bot.wait_event("approval")
    bot.say("no")
    finish(t)
    assert ("click", "sb2", "Buy now") not in bot.browser.calls
    assert sum(1 for e in bot.events if e["role"] == "approval") == 1
    results = [r["result"] for r in fake_cli("call")]
    assert any(r.startswith("DENIED EARLIER by the user") for r in results)


def test_auto_approval_skips_the_gate(server, fake_cli, monkeypatch):
    bot = FakeBot()
    bot.meta["approval"] = "auto"
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                   {"tool": "click", "args": {"ref": "sb2"}}, {"result": "ok"}], monkeypatch)
    assert "approval" not in bot.roles()
    assert ("click", "sb2", "Buy now") in bot.browser.calls


def test_ask_waits_for_the_answer(server, fake_cli, monkeypatch):
    bot = FakeBot()
    t = start(bot, [{"tool": "ask", "args": {"message": "Which size?", "options": ["S", "M", "L"]}},
                    {"result": "Got it: M."}], monkeypatch)
    q = bot.wait_event("question")
    assert q["text"] == "Which size?" and q["options"] == ["S", "M", "L"]
    assert bot.asking and bot.meta["status"] == "waiting"
    assert bot.meta["waiting_on"] == q["seq"]  # the card the bot is blocked on
    bot.say("M")
    finish(t)
    assert fake_cli("call")[0]["result"] == "USER ANSWER: M"
    assert bot.events[-1]["role"] == "done" and not bot.asking
    assert bot.meta["waiting_on"] is None
    assert ("running", {"waiting_on": None}) in bot.status_log


def test_mid_task_message_rides_on_the_next_result(server, fake_cli, monkeypatch):
    bot = FakeBot()
    bot.browser.on_goto = lambda url: bot.say("also check the prices") if url.endswith("shop.test/") else None
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                   {"tool": "click", "args": {"ref": "sb1"}},
                   {"result": "Prices: $5, $7."}], monkeypatch)
    calls = fake_cli("call")
    assert "USER INSTRUCTION" not in calls[0]["result"]
    assert calls[1]["result"].rstrip().endswith("USER INSTRUCTION (mid-task, overrides the task): also check the prices")
    assert len(fake_cli("user")) == 1


def test_message_after_last_tool_gets_its_own_turn(server, fake_cli, monkeypatch):
    bot = FakeBot()
    bot.browser.on_goto = lambda url: bot.say("and the gadget?")
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/products"}},
                   {"text": "Widget is $5."}, {"result": "Widget is $5."},
                   {"text": "Gadget is $7."}, {"result": "Gadget is $7."}], monkeypatch)
    users = fake_cli("user")
    assert len(users) == 2 and users[1]["user"] == "USER INSTRUCTION (mid-task, overrides the task): and the gadget?"
    assert [e["text"] for e in bot.events if e["role"] in ("thought", "done")] == ["Widget is $5.", "Gadget is $7."]
    assert bot.events[-1]["role"] == "done"


def test_py_app_without_file_puts_the_skill_in_the_result(server, fake_cli, monkeypatch):
    """The first message is sent once, so a SKILL.md that `py app` loads mid-task
    must reach the model in that call's result (the steps engine re-renders
    APP SKILLS instead)."""
    bot = FakeBot()
    bot.py_ref = lambda d: ("/apps/ledger" if d.get("app") == "ledger" else None, str(d.get("file") or ""), d.get("args") or {})
    bot.run_py = lambda d: ("py ledger", "RESULT:\nLoaded ledger's SKILL.md. Callable files: totals.py."
                            if not d.get("file") else "RESULT:\n{\"total\": 3}")
    mounted = []
    monkeypatch.setattr(apptools, "skill_section",
                        lambda dirs: mounted.append(list(dirs)) or "\n\nAPP SKILLS (...):\n=== ledger ===\n## totals.py\nargs: none")
    run_task(bot, [{"tool": "py", "args": {"app": "ledger"}},
                   {"tool": "py", "args": {"app": "ledger", "file": "totals.py"}},
                   {"result": "Total 3."}], monkeypatch)
    calls = fake_cli("call")
    assert "Loaded ledger's SKILL.md" in calls[0]["result"] and "=== ledger ===\n## totals.py" in calls[0]["result"]
    assert "=== ledger ===" not in calls[1]["result"]  # a file run carries only its value
    assert [m for m in mounted if m] == [["/apps/ledger"]]  # (the first message mounts [] here)
    assert [e["text"] for e in bot.events if e["role"] == "action"] == ["py ledger", "py ledger"]


def test_screenshot_returns_an_image(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                   {"tool": "screenshot", "args": {}}, {"result": "A shop."}], monkeypatch)
    shot = fake_cli("call")[1]
    assert shot["images"] == ["image/jpeg"] and "screenshot of https://shop.test/" in shot["result"]


def test_repeat_note_and_auto_screenshot(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/products"}},
                   {"tool": "click", "args": {"ref": "sb5"}},
                   {"tool": "click", "args": {"ref": "sb5"}},
                   {"result": "Stuck on add to cart."}], monkeypatch)
    calls = fake_cli("call")
    assert "nothing visible changed" in calls[1]["result"] and calls[1]["images"] == []
    assert "NOTE: you repeated 'click \"Add to cart\"' 2 times" in calls[2]["result"]
    assert calls[2]["images"] == ["image/jpeg"]
    # Nothing changed, so the text the model already holds is not sent again.
    assert "Widget $5." in calls[0]["result"]
    for c in calls[1:]:
        assert "VISIBLE TEXT: (unchanged since your last view)" in c["result"] and "Widget $5." not in c["result"]
    acts = [e for e in bot.events if e["role"] == "action"]
    assert acts[1]["result"] == acts[2]["result"] == "nothing visible changed"


def test_scroll_is_progress_not_a_repeat(server, fake_cli, monkeypatch):
    """The same label twice is a repeat only when nothing changed: two scrolls
    down a long page moved the viewport each time."""
    bot = FakeBot()
    run_task(bot, [{"tool": "goto", "args": {"url": LONG}},
                   {"tool": "scroll", "args": {"direction": "down"}},
                   {"tool": "scroll", "args": {"direction": "down"}},
                   {"result": "Seen it all."}], monkeypatch)
    calls = fake_cli("call")
    for c in calls[1:]:
        assert "CHANGE: the page scrolled; a different part is on screen." in c["result"]
        assert "NOTE: you repeated" not in c["result"] and c["images"] == []
        assert "VISIBLE TEXT: (unchanged since your last view)" in c["result"]
    acts = [e for e in bot.events if e["role"] == "action"]
    assert acts[2]["result"] == "scrolled"


def test_stale_token(server, fake_cli, monkeypatch):
    bot = FakeBot()
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    try:
        with pytest.raises(agent_engine.StaleToken):
            agent_engine.handle_tool(bot, "not-the-token", "observe", {})
        with pytest.raises(agent_engine.StaleToken):
            agent_engine.roster_for(bot, None)
        req = urllib.request.Request(f"{server}/api/bots/b1/tool", method="POST",
                                     data=json.dumps({"name": "observe", "args": {}, "token": "old"}).encode(),
                                     headers={"X-Fused": "1", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 409
        assert agent_engine.roster_for(bot, token)  # the current one still works
        old = token
        agent_engine.register_task(bot)  # a new task: the old token goes stale
        with pytest.raises(agent_engine.StaleToken):
            agent_engine.handle_tool(bot, old, "observe", {})
    finally:
        agent_engine._end_session(agent_engine.session(bot))


def test_stop_mid_tool(server, fake_cli, monkeypatch):
    bot = FakeBot()
    t = start(bot, [{"tool": "ask", "args": {"message": "Shall I continue?"}},
                    {"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"result": "never"}], monkeypatch)
    q = bot.wait_event("question")
    assert bot.meta["waiting_on"] == q["seq"]
    t0 = time.time()
    bot.stop()
    finish(t, timeout=15)
    assert bot.meta["waiting_on"] is None  # Stop ended the wait: no live card on an idle bot
    assert time.time() - t0 < 5  # the interrupt ends it; no SIGTERM wait
    assert bot.events[-1]["role"] == "system" and bot.events[-1]["text"] == "Stopped"
    assert "error" not in bot.roles() and "done" not in bot.roles()
    assert bot.browser.calls == []  # nothing after the stop ran
    assert {"control": "interrupt"}.items() <= fake_cli("control")[-1].items()
    assert bot.outcomes == [("stopped", "")]


def test_three_failed_model_calls_end_the_task(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"fail": "API Error: 500 internal"}] * 3, monkeypatch)
    errs = [e for e in bot.events if e["role"] == "error"]
    assert len(errs) == 4 and errs[0]["text"] == "Model call failed: API Error: 500 internal"
    assert errs[-1]["text"].startswith("RuntimeError: model call failed")
    assert bot.meta["status"] == "error"


def test_one_failed_model_call_is_retried(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"fail": "API Error: 500"}, {"result": "Recovered."}], monkeypatch)
    assert [e["role"] for e in bot.events] == ["system", "error", "done"]
    assert fake_cli("user")[1]["user"].startswith("The last model call failed.")


def test_process_death_is_an_error(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"exit": 3}], monkeypatch)
    assert bot.events[-1]["role"] == "error" and "Claude Code exited (code 3)" in bot.events[-1]["text"]
    assert bot.meta["status"] == "error"


def test_step_cap(server, fake_cli, monkeypatch):
    monkeypatch.setattr(agent_engine, "MAX_STEPS", 2)
    bot = FakeBot()
    run_task(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}}] * 3 + [{"result": "Partial."}], monkeypatch)
    calls = fake_cli("call")
    assert calls[2]["result"].startswith("STEP LIMIT") and calls[2]["is_error"]
    assert bot.events[-1]["role"] == "done" and bot.events[-1]["text"] == "Partial."


def test_no_cli_is_an_error(server, monkeypatch, tmp_path):
    bot = FakeBot()
    BOTS[bot.id] = bot
    agent_engine.run(bot, "say hi", "say hi")
    assert bot.events[-1]["role"] == "error" and "Claude Code CLI" in bot.events[-1]["text"]


def test_roster_follows_the_bot(server, tmp_path):
    bot = FakeBot()
    names = [t["name"] for t in tools.roster(bot)]
    assert "upload" not in names and "readfile" not in names and "text" not in names and "tool" not in names
    assert "observe" in names
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    bot.files = {"notes.txt": str(f)}
    names = [t["name"] for t in tools.roster(bot)]
    assert "upload" in names and "readfile" in names


@pytest.mark.skipif(not shutil.which("sips"), reason="filereader shrinks images through sips (macOS only)")
def test_readfile_text_and_image(server, fake_cli, monkeypatch, tmp_path):
    bot = FakeBot()
    (tmp_path / "notes.csv").write_text("a,b\n1,2\n")
    png = tmp_path / "pic.png"
    subprocess.run(["sips", "-s", "format", "png", "--out", str(png), "-z", "20", "20",
                    "/System/Library/CoreServices/DefaultDesktop.heic"], capture_output=True)
    if not png.exists():  # no stock wallpaper on this Mac: a 1x1 PNG by hand
        import base64
        png.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
    bot.files = {"notes.csv": str(tmp_path / "notes.csv"), "pic.png": str(png)}
    run_task(bot, [{"tool": "readfile", "args": {"file": "notes.csv"}},
                   {"tool": "readfile", "args": {"file": "pic.png"}},
                   {"tool": "readfile", "args": {"file": "../../etc/passwd"}},
                   {"result": "done"}], monkeypatch)
    calls = fake_cli("call")
    assert "FILE notes.csv" in calls[0]["result"] and "a,b\n1,2" in calls[0]["result"]
    assert calls[1]["images"] == ["image/jpeg"] and "IMAGE pic.png" in calls[1]["result"]
    assert calls[2]["result"].startswith("error: file not found")
    # a readfile is not a browser step: no page view, no thumbnail
    assert "COMPACT" not in calls[0]["result"] and "now at" not in calls[0]["result"]


def test_free_text_at_the_approval_card_is_an_instruction(server, fake_cli, monkeypatch):
    """Not yes, not no: the message is a mid-task instruction, the card stays
    live (waiting_on still points at it) and a later yes approves."""
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "Bought."}], monkeypatch)
    card = bot.wait_event("approval")
    assert bot.meta["waiting_on"] == card["seq"]
    bot.say("use the gift card at checkout")
    note = bot.wait_event("note")
    assert note and note["text"] == 'Noted; still waiting for Approve / Deny on: click "Buy now"'
    assert bot.meta["status"] == "waiting" and bot.meta["waiting_on"] == card["seq"]
    assert ("click", "sb2", "Buy now") not in bot.browser.calls
    bot.say("yes")
    finish(t)
    assert ("click", "sb2", "Buy now") in bot.browser.calls
    res = fake_cli("call")[1]["result"]
    assert res.startswith('APPROVED by the user: click "Buy now"')
    assert res.rstrip().endswith("USER INSTRUCTION (mid-task, overrides the task): use the gift card at checkout")
    assert "DENIED" not in res and not any(e["text"].startswith("Denied") for e in bot.events)
    assert sum(1 for e in bot.events if e["role"] == "approval") == 1
    assert bot.meta["waiting_on"] is None


def test_free_text_then_no_is_denied(server, fake_cli, monkeypatch):
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "Did not buy."}], monkeypatch)
    assert bot.wait_event("approval")
    bot.say("how much is shipping?")
    assert bot.wait_event("note")
    bot.say("no")
    finish(t)
    res = fake_cli("call")[1]["result"]
    assert res.startswith('DENIED by the user: click "Buy now".')
    assert "USER INSTRUCTION (mid-task, overrides the task): how much is shipping?" in res
    assert ("click", "sb2", "Buy now") not in bot.browser.calls


class _Proc:
    """Just enough of a Popen for _drive: canned stdout lines."""
    def __init__(self, events):
        import io
        self.stdout = io.BytesIO(b"".join((json.dumps(e) + "\n").encode() for e in events))

    def wait(self, timeout=None):
        return 0


def test_batched_ref_call_is_not_run_after_the_page_moved(server, monkeypatch):
    """Two tool_use blocks in ONE assistant message: both were decided against
    the same page. The first click navigates (refs renumbered on the new
    snapshot); the second, ref-based, is refused with the fresh page instead
    of clicking whatever now carries that ref. A non-ref call in a batch runs."""
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    sess = agent_engine.session(bot)
    try:
        agent_engine._observe(bot, sess)  # the page the model was shown
        msg = {"type": "assistant", "message": {"id": "msg_1", "role": "assistant", "content": [
            {"type": "text", "text": "Opening products, then the widget."},
            {"type": "tool_use", "id": "t1", "name": "mcp__bot__click", "input": {"ref": "sb1"}},
            {"type": "tool_use", "id": "t2", "name": "mcp__bot__click", "input": {"ref": "sb4"}},
            {"type": "tool_use", "id": "t3", "name": "mcp__bot__remember", "input": {"text": "widgets live under Products"}}]}}
        out = agent_engine._drive(bot, sess, _Proc([msg, {"type": "result", "subtype": "success", "result": "ok"}]))
        assert out == ("done", "ok") and sess.tool_uses == 3

        first = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        assert "url changed to https://shop.test/products" in first["content"][0]["text"]
        second = agent_engine.handle_tool(bot, token, "click", {"ref": "sb4"})
        text = second["content"][0]["text"]
        assert second["isError"] is False
        assert text.startswith("(not run: you issued several actions in one message and the page changed after the first")
        assert "CURRENT PAGE\nurl: https://shop.test/products" in text
        assert bot.browser.calls == [("click", "sb1", "Products")]  # sb4 was never clicked
        assert any(e["role"] == "note" and e["text"] == "Skipped a queued click: the page changed after the action before it."
                   for e in bot.events)
        third = agent_engine.handle_tool(bot, token, "remember", {"text": "widgets live under Products"})
        assert third["content"][0]["text"].startswith("remembered")

        # The next message's call is not part of that batch: its refs are fresh.
        sess.saw_tool_use(1, "msg_2")
        fourth = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        assert "url changed to https://shop.test/" in fourth["content"][0]["text"]
        assert bot.browser.calls[-1] == ("click", "sb1", "Home")
    finally:
        agent_engine._end_session(sess)


def test_dropped_tool_use_does_not_make_every_call_a_batch(server):
    """A tool_use block that never reaches the handler (the CLI refused an
    unknown tool itself) leaves `tool_uses` one ahead of `steps` for good.
    Separate single-block messages must still all run."""
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    sess = agent_engine.session(bot)
    try:
        agent_engine._observe(bot, sess)
        sess.saw_tool_use(1, "mX")  # dropped: no handle_tool for it
        sess.saw_tool_use(1, "mA")
        a = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        sess.saw_tool_use(1, "mB")
        b = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        assert "not run" not in a["content"][0]["text"] and "not run" not in b["content"][0]["text"]
        assert bot.browser.calls == [("click", "sb1", "Products"), ("click", "sb1", "Home")]
        assert not any(e["role"] == "note" for e in bot.events)
    finally:
        agent_engine._end_session(sess)


def test_batch_guard_sees_a_message_streamed_in_two_events(server):
    """The CLI may deliver one assistant message's blocks as several events
    with the same id (the first event carrying the text and first tool_use,
    the next the second tool_use). The bot-tool count accumulates per id, so
    the second ref call is still refused once the first moved the page."""
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    sess = agent_engine.session(bot)
    try:
        agent_engine._observe(bot, sess)
        ev1 = {"type": "assistant", "message": {"id": "msg_s", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__bot__click", "input": {"ref": "sb1"}}]}}
        ev2 = {"type": "assistant", "message": {"id": "msg_s", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__bot__click", "input": {"ref": "sb1"}},   # already seen: not counted twice
            {"type": "tool_use", "id": "t2", "name": "mcp__bot__click", "input": {"ref": "sb4"}}]}}
        agent_engine._drive(bot, sess, _Proc([ev1, ev2, {"type": "result", "subtype": "success", "result": "ok"}]))
        assert sess.tool_uses == 2 and sess.cur_mcp == 2
        first = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        second = agent_engine.handle_tool(bot, token, "click", {"ref": "sb4"})
        assert "url changed to https://shop.test/products" in first["content"][0]["text"]
        assert second["content"][0]["text"].startswith("(not run: you issued several actions in one message")
        assert bot.browser.calls == [("click", "sb1", "Products")]
    finally:
        agent_engine._end_session(sess)


def test_builtin_calls_do_not_misalign_the_batch_guard(server):
    """Super Bot: a built-in Read counts a step off the stream before the bot
    call of the same message is handled, so step numbers and tool_use blocks
    do not line up. The guard keys on the message, not on indexes: one bot
    call per message is never a batch, however many built-ins sit beside it."""
    bot = super_bot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    sess = agent_engine.session(bot)
    try:
        agent_engine._observe(bot, sess)

        def msg(mid):
            return {"type": "assistant", "message": {"id": mid, "role": "assistant", "content": [
                {"type": "tool_use", "id": f"{mid}-r", "name": "Read", "input": {"file_path": "/tmp/notes.md"}},
                {"type": "tool_use", "id": f"{mid}-c", "name": "mcp__bot__click", "input": {"ref": "sb1"}}]}}
        agent_engine._drive(bot, sess, _Proc([msg("msg_a"), {"type": "result", "subtype": "success", "result": "ok"}]))
        a = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        agent_engine._drive(bot, sess, _Proc([msg("msg_b"), {"type": "result", "subtype": "success", "result": "ok"}]))
        b = agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})
        assert "not run" not in a["content"][0]["text"] and "not run" not in b["content"][0]["text"]
        assert bot.browser.calls == [("click", "sb1", "Products"), ("click", "sb1", "Home")]
        assert sess.steps == 4  # two built-ins counted off the stream, two bot calls handled
        assert not any(e["role"] == "note" for e in bot.events)
    finally:
        agent_engine._end_session(sess)


def test_stop_during_the_approval_card(server, fake_cli, monkeypatch):
    """Stop while a risky click waits on its card ends the step cleanly
    (`Stopped`, no error event), like Stop during `ask`."""
    bot = FakeBot()
    t = start(bot, [{"tool": "goto", "args": {"url": "https://shop.test/"}},
                    {"tool": "click", "args": {"ref": "sb2"}},
                    {"result": "never"}], monkeypatch)
    card = bot.wait_event("approval")
    assert bot.meta["waiting_on"] == card["seq"]
    bot.stop()
    finish(t, timeout=15)
    assert bot.events[-1]["role"] == "system" and bot.events[-1]["text"] == "Stopped"
    assert "error" not in bot.roles() and bot.meta["waiting_on"] is None
    assert bot.browser.calls == [("goto", "https://shop.test/")]  # the click never ran
    assert bot.outcomes == [("stopped", "")]


def test_skipped_while_paused_is_a_note(server):
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    sess = agent_engine.session(bot)
    try:
        agent_engine._observe(bot, sess)
        sess.saw_tool_use(1, "m1")
        bot.pause_flag.set()
        box = {}
        th = threading.Thread(target=lambda: box.update(out=agent_engine.handle_tool(bot, token, "click", {"ref": "sb1"})))
        th.start()
        time.sleep(0.3)
        bot.pause_flag.clear()
        bot.wake.set()
        th.join(10)
        assert box["out"]["content"][0]["text"].startswith("(skipped click: paused by the user")
        assert [e["text"] for e in bot.events if e["role"] == "note"] == [
            'Skipped click "Products": you were driving; acting on the page as it is now.']
        assert bot.browser.calls == []
    finally:
        agent_engine._end_session(sess)


def test_first_message_carries_the_current_page(server, fake_cli, monkeypatch):
    """A follow-up task on a page the browser already shows: the page rides on
    the first message with valid refs, so the model acts without `observe`."""
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    run_task(bot, [{"tool": "click", "args": {"ref": "sb1"}},
                   {"tool": "click", "args": {"ref": "sb1"}},
                   {"result": "Back home."}], monkeypatch)
    first = fake_cli("user")[0]["user"]
    head, _, tail = first.rpartition("\n\nTASK: ")
    assert tail.strip() == "say hi"
    assert "CURRENT PAGE (where your browser is right now; these refs are valid):\nurl: https://shop.test/\n" in head
    assert 'sb2 button "Buy now"' in head and "Welcome to the shop." in head
    calls = fake_cli("call")
    assert "CHANGE: url changed to https://shop.test/products" in calls[0]["result"]  # diffed against the seed
    assert "https://shop.test/ was ALREADY VISITED" in calls[1]["result"]  # the seeded page counts as seen


def test_blank_browser_is_not_seeded(server, fake_cli, monkeypatch):
    bot = FakeBot()  # about:blank
    run_task(bot, [{"result": "Hi."}], monkeypatch)
    assert "CURRENT PAGE" not in fake_cli("user")[0]["user"]


def test_change_report_sees_a_scroll():
    els = [_el(f"sb{i}", "a", f"Item {i}") for i in range(1, 5)]

    def page(on):
        return {"url": "u", "title": "t", "text": "x",
                "elements": [dict(e, **({} if i in on else {"offscreen": True})) for i, e in enumerate(els)]}
    assert tools.change_report(page({0, 1}), page({2, 3})) == "CHANGE: the page scrolled; a different part is on screen."
    same = tools.change_report(page({0, 1}), page({0, 1}))
    assert same == "CHANGE: nothing visible changed (same url, controls and text)." and tools.unchanged(same)
    assert tools.text_unchanged(same) and tools.text_unchanged(tools.change_report(page({0}), page({1})))
    assert not tools.text_unchanged("CHANGE: the page text changed; the controls did not.")
    obs = page({0})
    assert "VISIBLE TEXT:\nx" in tools.format_observation(obs, compact=True)
    short = tools.format_observation(obs, compact=True, text=False)
    assert short.endswith("VISIBLE TEXT: (unchanged since your last view)") and "\nx" not in short


def test_change_report_sees_a_typed_value():
    # `type` and `select` change nothing innerText or the signatures can see; the
    # field's value is the news (for the model, and for the chip the user reads).
    def page(q, pick="Red"):
        return {"url": "u", "title": "t", "text": "x", "elements": [
            dict(_el("sb1", "input", ""), name="q", empty=not q, value=q),
            dict(_el("sb2", "select", "Colour"), value=pick, options=["Red", "Blue"]),
            _el("sb3", "button", "Search")]}
    typed = tools.change_report(page(""), page("nike shoes"))
    assert typed == 'CHANGE: "q" now holds "nike shoes".' and not tools.unchanged(typed)
    assert tools.ui_summary("type", "ok, now at u", typed, page("nike shoes")) == '"q" now holds "nike shoes"'
    picked = tools.change_report(page("", "Red"), page("", "Blue"))
    assert picked == 'CHANGE: "Colour" now holds "Blue".'
    assert tools.unchanged(tools.change_report(page("a"), page("a")))


def test_element_names_fall_back_past_the_ref():
    # An unlabeled search box has no text: the chip and the approval preview
    # name it by its form name, then by what kind of field it is, never `sb162`.
    assert tools.what_is({"ref": "sb1", "tag": "input", "text": "", "name": "q", "empty": True}) == "q"
    assert tools.what_is({"ref": "sb1", "tag": "input", "type": "search", "empty": True}, "sb1") == "search box"
    assert tools.what_is({"ref": "sb1", "tag": "textarea", "empty": True}, "sb1") == "text field"
    assert tools.what_is({"ref": "sb1", "tag": "select"}, "sb1") == "dropdown"
    assert tools.what_is({"ref": "sb1", "tag": "a"}, "sb1") == "sb1"
    obs = {"elements": [{"ref": "sb2", "tag": "input", "type": "search", "empty": True}]}
    assert tools.describe(None, "type", {"ref": "sb2", "text": "rust", "submit": True}, obs) == 'type "rust" into "search box" and press Enter'
    # scroll chips say whether it moved, not what the DOM-scan window picked up
    assert tools.ui_summary("scroll", "ok", "CHANGE: 45 new controls appeared: a \"x\", a \"y\".") == "scrolled · 45 more controls in view"
    assert tools.ui_summary("scroll", "ok", "CHANGE: the page scrolled; a different part is on screen.") == "scrolled"
    assert tools.ui_summary("scroll", "ok", "CHANGE: nothing visible changed (same url, controls and text).") == "nothing more to scroll to"


def test_ui_summary_and_detail():
    s, d = tools.ui_summary, tools.ui_detail
    # errors: the first line, one line, capped
    err = "error: " + "x" * 300 + "\nmore"
    assert s("click", err) == tools._clip(err.splitlines()[0]) and len(s("click", err)) <= 160
    assert d("click", err, s("click", err)) == err[:1500]
    # browser steps: the change, without "CHANGE: " and the trailing period
    ch = "CHANGE: url changed to https://a.test/x."
    # a navigation: where we are now (host + title), with nothing more to disclose
    assert s("goto", "ok, now at https://a.test/x", ch) == "now at a.test/x"
    assert s("goto", "ok, now at https://www.a.test/x/", "CHANGE: url changed to https://www.a.test/x/ (title 'X').", {"title": "X"}) == 'now at a.test/x · "X"'
    assert d("goto", "ok, now at https://a.test/x", "now at a.test/x", ch) is None
    assert d("goto", "ok, now at https://a.test/x", "now at a.test/x", "CHANGE: url changed to https://a.test/x; a popup/dialog opened.") \
        == "url changed to https://a.test/x; a popup/dialog opened"
    assert s("click", "ok, now at u", "CHANGE: nothing visible changed (same url, controls and text).") == "nothing visible changed"
    long_ch = "CHANGE: 9 new controls appeared: " + ", ".join(f'a "Link number {i}"' for i in range(9)) + "."
    summ = s("click", "ok, now at u", long_ch)
    assert len(summ) <= 160 and summ.endswith("…") and d("click", "ok, now at u", summ, long_ch).startswith("9 new controls")
    assert s("select", 'ok, chose "Large"', "CHANGE: nothing visible changed (same url, controls and text).") == \
        'chose "Large" · nothing visible changed'
    assert s("wait", "not found after 10 s", "CHANGE: nothing visible changed (same url, controls and text).") == "not found after 10 s"
    # observe / read / screenshot
    obs = {"elements": [{}] * 12, "tabs": [{}, {}, {}]}
    assert s("observe", "CURRENT PAGE\n…", "", obs) == "12 elements · 3 tabs"
    assert s("observe", "CURRENT PAGE\n…", "", {"elements": [{}], "tabs": [{}]}) == "1 elements"
    assert d("observe", "CURRENT PAGE\nurl: u", "1 elements") == "CURRENT PAGE\nurl: u"
    assert s("read", "TEXT:\n" + "a" * 2345) == "2,345 chars" and len(d("read", "TEXT:\n" + "a" * 2345, "")) == 1500
    assert s("screenshot", "ok, screenshot of u attached") == "captured"
    # tool / py
    res = "RESULT:\n" + '{"total": 3, "rows": [' + "1, " * 100 + "]}"
    summ = s("py", res)
    assert summ.startswith(f"result · {len(res) - len('RESULT:') - 1:,} chars · " + '{"total": 3') and len(summ) <= 160
    assert d("py", res, summ) == res[:1500]
    assert s("tool", "error: no such tool") == "error: no such tool"
    # show / build: never the model's instructions
    shown = "the app card is in the chat (/Users/me/Fused/apps/prices). Now `done` in one line; do not describe the app's contents."
    assert s("show", shown, label='show "Price Tracker"') == "Price Tracker card posted"
    assert s("show", shown) == "prices card posted" and d("show", shown, "prices card posted") is None
    built = ("started; Claude is building it now (it runs unattended; a few minutes). Link for the user: http://x/render?path=a . "
             "Now `done`: tell the user you are building it \"A\", include that exact link, and that they will hear when it is ready.")
    assert s("build", built) == "Claude is building it now · link posted"
    assert "Now `done`" not in d("build", built, s("build", built)) and "http://x/render?path=a" in d("build", built, "")
    assert s("build", built.replace("building it now", "updating the existing app now")) == "Claude is updating it now · link posted"
    # save / texts / the rest
    assert s("save", "saved notes.md to the user's Inbox (/Users/me/Fused/bots/t, 12345 chars)") == "saved to Inbox · 12,345 chars"
    texts = "TEXTS with Mom (oldest first):\n[Oct 01 10:00] me: hi\n[Oct 01 10:05] Mom: hello"
    assert s("texts", texts) == "2 messages" and d("texts", texts, "2 messages") == texts
    assert s("texts", "no texts with Mom yet") == "no texts with Mom yet"
    assert s("remember", "remembered") == "remembered" and d("remember", "remembered", "remembered") is None
    for name, raw in (("goto", "ok"), ("show", shown), ("build", built), ("py", res), ("read", "TEXT:\nx\ny")):
        out = s(name, raw, "CHANGE: url changed to u.")
        assert "\n" not in out and len(out) <= 160 and "Now `done`" not in out


# ---------------------------------------------------------------- botmcp ---
class McpPipe:
    def __init__(self, argv):
        self.p = subprocess.Popen([sys.executable, BOTMCP] + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.seq = 0

    def send(self, method, params=None, rid=None):
        if rid is None:
            self.seq += 1
            rid = self.seq
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return rid

    def read(self):
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(5)


def test_botmcp_round_trip(server):
    bot = FakeBot()
    bot.browser.url = "https://shop.test/"
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    pipe = McpPipe([server, bot.id, token])
    try:
        pipe.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        init = pipe.read()["result"]
        assert init["capabilities"] == {"tools": {}} and init["serverInfo"]["name"] == "bot"
        pipe.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        pipe.send("tools/list")
        listed = pipe.read()["result"]["tools"]
        assert [t["name"] for t in listed] == [t["name"] for t in tools.roster(bot)]
        assert listed[0]["inputSchema"]["type"] == "object"
        pipe.send("tools/call", {"name": "observe", "arguments": {}})
        out = pipe.read()["result"]
        assert out["isError"] is False and "CURRENT PAGE\nurl: https://shop.test/" in out["content"][0]["text"]
        pipe.send("ping")
        assert pipe.read()["result"] == {}
        pipe.send("nope/nothing")
        assert pipe.read()["error"]["code"] == -32601
    finally:
        pipe.close()
        agent_engine._end_session(agent_engine.session(bot))


def test_botmcp_blocked_call_does_not_stall_ping(server):
    bot = FakeBot()
    BOTS[bot.id] = bot
    token = agent_engine.register_task(bot)
    pipe = McpPipe([server, bot.id, token])
    try:
        call = pipe.send("tools/call", {"name": "ask", "arguments": {"message": "?"}})
        assert bot.wait_event("question")
        ping = pipe.send("ping")
        first = pipe.read()
        assert first["id"] == ping and first["result"] == {}
        bot.say("yes")
        second = pipe.read()
        assert second["id"] == call and second["result"]["content"][0]["text"] == "USER ANSWER: yes"
    finally:
        pipe.close()
        agent_engine._end_session(agent_engine.session(bot))


def test_botmcp_stale_token_is_a_tool_error(server):
    bot = FakeBot()
    BOTS[bot.id] = bot
    agent_engine.register_task(bot)
    pipe = McpPipe([server, bot.id, "stale"])
    try:
        pipe.send("tools/call", {"name": "observe", "arguments": {}})
        out = pipe.read()["result"]
        assert out["isError"] is True and "409" in out["content"][0]["text"]
    finally:
        pipe.close()
        agent_engine._end_session(agent_engine.session(bot))


# ------------------------------------------------------------------ Super Bot ---
# bot.py KINDS "super" (docs §5 "Super Bot"): Claude Code's own tools on, permission
# prompts as approval cards, built-in calls as action rows, a bigger step cap,
# and the web guard.
def super_bot():
    bot = FakeBot("ea1")
    bot.meta.update({"name": "Super Bot", "kind": "super", "super_access": "ask", "model": "sonnet"})
    return bot


def _perm(tool, **inp):
    return {"tool": "permission", "args": {"tool_name": tool, "input": inp, "tool_use_id": "tu1"}}


def _answer(rows, i=0):
    """The JSON the permission tool returned on the i-th permission call."""
    calls = [r for r in rows("call") if r["call"] == "permission"]
    return json.loads(calls[i]["result"])


def test_super_argv_prompt_and_roster(server, fake_cli, monkeypatch):
    bot = super_bot()
    run_task(bot, [{"result": "ok"}], monkeypatch)
    argv = fake_cli("argv")[0]["argv"]
    assert "--tools=" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__bot__permission"
    assert argv[argv.index("--add-dir") + 1] == os.path.expanduser("~")
    assert "--setting-sources=" in argv and "--allowedTools" in argv
    assert "permission" in fake_cli("tools")[0]["tools"]
    with open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8") as f:
        sp = f.read()
    assert "YOU ARE THE SUPER BOT" in sp and "INBOX:" in sp
    assert "Mac access: ask before writes" in fake_cli("user")[0]["user"]


def test_ordinary_bot_keeps_the_locked_argv(server, fake_cli, monkeypatch):
    bot = FakeBot()
    run_task(bot, [{"result": "ok"}], monkeypatch)
    argv = fake_cli("argv")[0]["argv"]
    assert "--tools=" in argv and "--permission-prompt-tool" not in argv and "--add-dir" not in argv
    assert "permission" not in fake_cli("tools")[0]["tools"]


def test_super_full_access_argv(server, fake_cli, monkeypatch):
    bot = super_bot()
    bot.meta["super_access"] = "full"
    run_task(bot, [{"result": "ok"}], monkeypatch)
    argv = fake_cli("argv")[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert "Mac access: unattended" in fake_cli("user")[0]["user"]


def test_super_permission_card_approved(server, fake_cli, monkeypatch):
    bot = super_bot()
    t = start(bot, [_perm("Bash", command="make test"), {"result": "ran it"}], monkeypatch)
    card = bot.wait_event("approval")
    assert card and card["text"].startswith("About to run `make test`.") and card["detail"] == "run `make test`"
    assert bot.meta["status"] == "waiting"
    bot.say("yes")
    finish(t)
    ans = _answer(fake_cli)
    assert ans == {"behavior": "allow", "updatedInput": {"command": "make test"}}
    assert "approval" in bot.roles() and bot.roles()[-1] == "done"


def test_super_permission_card_denied_and_not_asked_twice(server, fake_cli, monkeypatch):
    bot = super_bot()
    t = start(bot, [_perm("Write", file_path="/tmp/x.md", content="hi"), _perm("Write", file_path="/tmp/x.md", content="hi"),
                    {"result": "could not write"}], monkeypatch)
    assert bot.wait_event("approval")
    seq = bot.meta["waiting_on"]
    bot.say("leave it alone")  # neither a yes nor a no: an instruction; the card stays up (same gate as a risky click)
    assert bot.wait_event("note")
    assert bot.meta["status"] == "waiting" and bot.meta["waiting_on"] == seq
    bot.say("no, not that file")
    finish(t)
    first, second = _answer(fake_cli, 0), _answer(fake_cli, 1)
    assert first["behavior"] == "deny" and "DENIED by the user: write /tmp/x.md" in first["message"] and "not that file" in first["message"]
    # The instruction heard while the card was up rides on the deny message (an allow has no room for text).
    assert "USER INSTRUCTION (mid-task, overrides the task): leave it alone" in first["message"]
    assert second["behavior"] == "deny" and "DENIED EARLIER" in second["message"]
    assert bot.roles().count("approval") == 1


def test_super_reads_never_ask(server, fake_cli, monkeypatch):
    bot = super_bot()
    run_task(bot, [_perm("Read", file_path="/etc/hosts"), _perm("Grep", pattern="x"), {"result": "ok"}], monkeypatch)
    assert "approval" not in bot.roles()
    assert _answer(fake_cli, 0)["behavior"] == "allow" and _answer(fake_cli, 1)["behavior"] == "allow"


def test_super_unattended_allows_until_the_web_is_touched(server, fake_cli, monkeypatch):
    bot = super_bot()
    bot.meta["super_access"] = "full"
    t = start(bot, [_perm("Bash", command="ls"), {"tool": "goto", "args": {"url": "https://shop.test/"}},
                    _perm("Bash", command="rm -rf build"), {"result": "ok"}], monkeypatch)
    card = bot.wait_event("approval")
    assert card and "read the web" in card["text"] and "rm -rf build" in card["text"]
    bot.say("ok")
    finish(t)
    assert _answer(fake_cli, 0)["behavior"] == "allow"  # before the web: Claude's own judgement
    assert _answer(fake_cli, 1)["behavior"] == "allow"  # after it: the user's
    assert bot.roles().count("approval") == 1


def test_super_webfetch_counts_as_touching_the_web(server, fake_cli, monkeypatch):
    bot = super_bot()
    bot.meta["super_access"] = "full"
    t = start(bot, [{"builtin": "WebFetch", "args": {"url": "https://x.test/"}, "output": "a page"},
                    _perm("Edit", file_path="/tmp/a.py"), {"result": "ok"}], monkeypatch)
    assert bot.wait_event("approval")
    bot.say("yes")
    finish(t)
    assert _answer(fake_cli, 0)["behavior"] == "allow"


def test_super_builtin_calls_become_action_rows(server, fake_cli, monkeypatch):
    bot = super_bot()
    home = os.path.expanduser("~")
    run_task(bot, [{"text": "Reading the file."},
                   {"builtin": "Read", "args": {"file_path": f"{home}/Documents/notes.md"}, "output": "line one\nline two"},
                   {"builtin": "Bash", "args": {"command": "ls -la"}, "output": "total 0", "error": True},
                   {"result": "two lines"}], monkeypatch)
    acts = [e for e in bot.events if e["role"] == "action"]
    assert [a["text"] for a in acts] == ["read ~/Documents/notes.md", "run `ls -la`"]
    # One line on the chip, the whole output behind it (the same two audiences as every other action).
    assert acts[0]["result"] == "line one" and acts[0]["detail"] == "line one\nline two"
    assert acts[1]["result"].startswith("error:")
    assert bot.roles()[:3] == ["system", "thought", "action"]
    assert bot.meta["step"] == 2


def test_super_step_cap_is_larger_and_counts_builtins(server, fake_cli, monkeypatch):
    monkeypatch.setattr(agent_engine, "SUPER_MAX_STEPS", 3)
    bot = super_bot()
    script = [{"builtin": "Read", "args": {"file_path": "/a"}, "output": "x"} for _ in range(3)]
    script += [{"tool": "observe", "args": {}}, {"result": ""}]
    run_task(bot, script, monkeypatch)
    calls = [r for r in fake_cli("call") if r["call"] == "observe"]
    assert calls and calls[0]["result"].startswith("STEP LIMIT: this task has used its 3 steps")


def test_super_mid_task_message_is_not_the_cards_answer(server, fake_cli, monkeypatch):
    bot = super_bot()
    bot.say("also check the dates")  # lands before the permission call
    t = start(bot, [_perm("Bash", command="date"), {"tool": "observe", "args": {}}, {"result": "ok"}], monkeypatch)
    card = bot.wait_event("approval")
    assert card
    bot.say("yes")
    finish(t)
    assert _answer(fake_cli)["behavior"] == "allow"
    obs = [r for r in fake_cli("call") if r["call"] == "observe"][0]
    assert "USER INSTRUCTION (mid-task, overrides the task): also check the dates" in obs["result"]


def test_builtin_label():
    L = agent_engine.builtin_label
    home = os.path.expanduser("~")
    assert L("Bash", {"command": "  git   status "}) == "run `git status`"
    assert L("Write", {"file_path": f"{home}/a/b.txt"}) == "write ~/a/b.txt"
    assert L("Edit", {"file_path": "/tmp/x"}) == "edit /tmp/x"
    assert L("Glob", {"pattern": "**/*.py", "path": "/tmp"}) == "find **/*.py in /tmp"
    assert L("WebFetch", {"url": "https://x.test/a"}) == "fetch https://x.test/a"
    assert L("Frobnicate", {"thing": "it"}) == "Frobnicate it"
    assert len(L("Bash", {"command": "x" * 500})) < 140


# ---- Bot threads: one conversation, resumed per turn, rolled over on budget (docs §6) ------------
class ConvBot(FakeBot):
    """FakeBot plus the conversation record `run()` resumes (bot.py's surface, in memory)."""

    def __init__(self, bid="c1"):
        super().__init__(bid)
        self.seq = 0
        self.conv = {"session_id": None, "tokens": 0, "since_seq": 0, "summary": "", "sent": {}, "prompt_hash": "",
                     "turns": 0, "rollovers": 0}
        self.summaries = []

    def conversation(self):
        return self.conv

    def conversation_update(self, **kw):
        self.conv.update(kw)

    def conversation_rollover(self, summary, prompt_hash=""):
        self.conv.update(session_id=None, tokens=0, since_seq=len(self.events), summary=summary, sent={},
                         prompt_hash=prompt_hash, web_touched=False, rollovers=self.conv["rollovers"] + 1)

    def transcript_lines(self, since_seq=0, limit=None):
        if len(self.events) < 3:
            return []  # turn 1: a thin transcript (the digest, no model call)
        return [f"[#{i}] USER: earlier ask {i}" for i in range(1, 7)]  # enough lines for a summary call

    def summarize_conversation(self, since_seq=0, previous=""):
        self.summaries.append((since_seq, previous))
        return "SUMMARY NOTE: the user asked for three posts; #2 was Beta." + (" | " + previous if previous else "")


def test_conversation_resumes_and_rolls_over(server, fake_cli, monkeypatch):
    bot = ConvBot()
    # Turn 1: no session yet -> a fresh one, seeded from the (thin) digest, no --resume.
    run_task(bot, [{"text": "Hello."}, {"result": "Hi."}], monkeypatch)  # the text step carries `usage`
    argv1 = fake_cli("argv")[0]["argv"]
    assert "--resume" not in argv1 and "--no-session-persistence" not in argv1
    first = fake_cli("user")[0]["user"]
    # A thin transcript seeds the session with the digest (no model call), under the summary heading.
    assert first.startswith("YOU: 'Tester'") and "CONVERSATION SUMMARY" in first and "bot: hi there" in first
    assert first.rstrip().endswith("TASK: say hi")
    sid = bot.conv["session_id"]
    assert sid and sid.startswith("fake-session-") and bot.conv["tokens"] > 0 and bot.conv["turns"] == 1
    assert bot.conv["prompt_hash"] and "MEMORY" in bot.conv["sent"]
    assert bot.summaries == []  # a thin transcript takes the digest: no model call

    # Turn 2: the same conversation is resumed; the preamble carries only what changed.
    run_task(bot, [{"result": "Again."}], monkeypatch, task="and the second one?")
    argv2 = fake_cli("argv")[1]["argv"]
    assert argv2[argv2.index("--resume") + 1] == sid
    second = fake_cli("user")[1]["user"]
    assert second.startswith("TURN 2 ·") and "YOU: 'Tester'" not in second and "CONVERSATION SUMMARY" not in second
    assert "Unchanged since earlier in this conversation (still apply): MEMORY" in second
    assert second.rstrip().endswith("TASK: and the second one?")
    assert bot.conv["session_id"] == sid and bot.conv["turns"] == 2

    # Turn 3: over budget -> rollover: a summary seeds a fresh session, no --resume, and the user hears once.
    bot.conv["tokens"] = agent_engine.rollover_at("haiku")
    run_task(bot, [{"result": "Fresh."}], monkeypatch, task="third")
    argv3 = fake_cli("argv")[2]["argv"]
    assert "--resume" not in argv3
    third = fake_cli("user")[2]["user"]
    assert "CONVERSATION SUMMARY" in third and "SUMMARY NOTE: the user asked for three posts" in third
    assert len(bot.summaries) == 1 and bot.conv["rollovers"] == 2  # turn 1 was a (silent) rollover from nothing
    assert bot.summaries[0][1].startswith("user: hello")  # the previous note (turn 1's digest) was folded in
    assert bot.conv["session_id"] and bot.conv["session_id"] != sid  # the new process minted a new session
    assert any(e["role"] == "note" and e["text"].startswith("Conversation compacted at") for e in bot.events)


def test_context_window_rule():
    assert agent_engine.context_window("haiku") == agent_engine.WINDOW_DEFAULT
    assert agent_engine.context_window("claude-sonnet-5") == agent_engine.WINDOW_1M
    assert agent_engine.context_window("sonnet[1m]") == agent_engine.WINDOW_1M
    assert agent_engine.rollover_at("haiku") == agent_engine.WINDOW_DEFAULT // 2


# ---- Super Bot: Claude Code's own AskUserQuestion becomes a question card, not an approval ----

def _ask_q(*questions):
    return _perm("AskUserQuestion", questions=list(questions))


def _q(text, *labels, multi=False):
    return {"question": text, "header": "Pick", "options": [{"label": l, "description": ""} for l in labels], "multiSelect": multi}


def test_super_ask_user_question_is_a_question_card(server, fake_cli, monkeypatch):
    bot = super_bot()
    t = start(bot, [_ask_q(_q("Which size?", "Small", "Large")), {"result": "ok"}], monkeypatch)
    card = bot.wait_event("question")
    assert card["text"] == "Which size?" and card["options"] == ["Small", "Large"]
    assert "approval" not in bot.roles() and bot.meta["status"] == "waiting"
    bot.say("large")  # a click or a typed label, any case
    finish(t)
    ans = _answer(fake_cli)
    assert ans["behavior"] == "allow"
    assert ans["updatedInput"]["answers"] == {"Which size?": "Large"}
    assert [o["label"] for o in ans["updatedInput"]["questions"][0]["options"]] == ["Small", "Large"]  # nothing typed: untouched


def test_super_ask_user_question_typed_answer_joins_the_options(server, fake_cli, monkeypatch):
    bot = super_bot()
    t = start(bot, [_ask_q(_q("Which size?", "Small", "Large")), {"result": "ok"}], monkeypatch)
    bot.wait_event("question")
    bot.say("medium, please")
    finish(t)
    ans = _answer(fake_cli)["updatedInput"]
    assert ans["answers"] == {"Which size?": "medium, please"}
    opts = ans["questions"][0]["options"]
    assert [o["label"] for o in opts] == ["Small", "Large", "medium, please"]
    assert opts[-1]["description"] == agent_engine.TYPED_OPTION_NOTE


def test_super_ask_user_question_multi_select_keeps_commas_in_labels(server, fake_cli, monkeypatch):
    bot = super_bot()
    t = start(bot, [_ask_q(_q("Which days?", "Mon, Tue", "Wed", "Thu", multi=True)), {"result": "ok"}], monkeypatch)
    card = bot.wait_event("question")
    assert card["text"].startswith("Which days?") and "commas" in card["text"]
    bot.say("wed, Mon, Tue, and Fri if free")
    finish(t)
    ans = _answer(fake_cli)["updatedInput"]
    # Labels in option order (the join the CLI checks), the typed remainder last and whole.
    assert ans["answers"] == {"Which days?": "Mon, Tue, Wed, and Fri if free"}
    assert [o["label"] for o in ans["questions"][0]["options"]] == ["Mon, Tue", "Wed", "Thu", "and Fri if free"]
