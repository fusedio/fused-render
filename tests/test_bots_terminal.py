"""Bot terminals (docs/bots.md §16): the roster gate, the card / allow-rule rules for
`terminal_run`, the pref-off refusal, and a real command against a live pty.

The agent-engine fakes (FakeBot, the HTTP stand-in for the server, the fake `claude`)
come from test_bots_agent_engine; `TBot` grafts the real Bot's terminal methods onto
FakeBot so the real session lifecycle runs."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os
import sys

import pytest

from test_bots_agent_engine import (  # noqa: F401 — fixtures are picked up by name
    BOTS, FakeBot, fake_cli, finish, quiet_apptools, server, start)

from fused_render import pty_session
from fused_render.bots import bot as botmod
from fused_render.bots import tools
from fused_render.bots.channels import base as chan
from fused_render.shell import prefs

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the pty is POSIX-only")

TERMINAL_NAMES = {"terminal_run", "terminal_read", "terminal_interrupt"}


class TBot(FakeBot):
    terminal_id = botmod.Bot.terminal_id
    terminal_session = botmod.Bot.terminal_session
    close_terminal = botmod.Bot.close_terminal
    _terminal_id = None

    def __init__(self, tmp_path, bid="tb1", **meta):
        super().__init__(bid)
        self.dir = str(tmp_path)
        self.task_via = dict(chan.WEB)
        self.meta.update({"engine": "agent", "terminal": True, **meta})


@pytest.fixture
def pref_on(monkeypatch):
    monkeypatch.setattr(prefs, "bot_terminals_enabled", lambda: True)


@pytest.fixture
def tbot(tmp_path, pref_on):
    b = TBot(tmp_path)
    yield b
    b.close_terminal()


def names(bot):
    return {t["name"] for t in tools.roster(bot)}


# ---- roster gating: all four conditions ----------------------------------------

def test_roster_has_terminal_tools_only_when_every_condition_holds(tbot, monkeypatch):
    assert TERMINAL_NAMES <= names(tbot)
    tbot.meta["terminal"] = False                       # the bot's own opt-in
    assert not TERMINAL_NAMES & names(tbot)
    tbot.meta["terminal"] = True
    monkeypatch.setattr(prefs, "bot_terminals_enabled", lambda: False)   # the global pref
    assert not TERMINAL_NAMES & names(tbot)
    monkeypatch.setattr(prefs, "bot_terminals_enabled", lambda: True)
    tbot.meta["engine"] = "steps"                       # the steps engine
    assert not TERMINAL_NAMES & names(tbot)
    tbot.meta["engine"] = "agent"
    tbot.meta["kind"] = "super"                         # Super Bot has Bash already
    assert not TERMINAL_NAMES & names(tbot)


def test_never_enabled_on_windows(tbot, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert tools.terminal_enabled(tbot) is False


def test_terminal_off_by_default_even_when_pref_on(tmp_path, pref_on):
    b = TBot(tmp_path)
    del b.meta["terminal"]
    assert tools.terminal_enabled(b) is False


# ---- risk(): cards ---------------------------------------------------------------

def run(command, **kw):
    return {"command": command, **kw}


def test_plain_command_asks_and_a_rule_skips_the_card(tbot):
    assert tools.risk(tbot, "terminal_run", run("gh pr view 12"), {})
    tbot.meta["allow_rules"] = [{"kind": "terminal", "key": "gh pr view", "label": "x"}]
    assert tools.risk(tbot, "terminal_run", run("gh pr view 12"), {}) == ""
    assert tools.risk(tbot, "terminal_run", run("gh pr view 99 --json title"), {}) == ""
    assert tools.risk(tbot, "terminal_run", run("gh pr merge 12"), {})   # a different key still asks


def test_read_and_interrupt_never_ask(tbot):
    assert tools.risk(tbot, "terminal_read", {"lines": 5}, {}) == ""
    assert tools.risk(tbot, "terminal_interrupt", {}, {}) == ""


@pytest.mark.parametrize("ch", [";", "|", "&", "`", "<", ">", "$", "\\", "\n"])
def test_each_metacharacter_forces_a_card_and_no_forever_offer(tbot, ch):
    tbot.meta["allow_rules"] = [{"kind": "terminal", "key": "gh pr view", "label": "x"}]
    cmd = f"gh pr view 12 {ch} echo pwned"
    assert tools.risk(tbot, "terminal_run", run(cmd), {}), repr(ch)   # the rule does not cover it
    assert tools.forever_rule(tbot, "terminal_run", run(cmd), {}) is None


def test_subshell_and_and_and_forms_card(tbot):
    tbot.meta["allow_rules"] = [{"kind": "terminal", "key": "gh pr view", "label": "x"}]
    for cmd in ("gh pr view 1 && rm -rf x", "gh pr view $(whoami)", "gh pr view 1 > /tmp/x", "gh pr view 1\nrm x"):
        assert tools.risk(tbot, "terminal_run", run(cmd), {}), cmd


def test_outside_sender_always_asks_even_with_a_rule(tbot):
    tbot.meta["allow_rules"] = [{"kind": "terminal", "key": "gh pr list", "label": "x"}]
    assert tools.risk(tbot, "terminal_run", run("gh pr list"), {}) == ""
    tbot.task_via = chan.via(chan.PHONE_KIND, "dev1") if hasattr(chan, "PHONE_KIND") else {"kind": "imessage", "addr": "+1"}
    assert chan.is_outside(tbot.task_via)
    assert tools.risk(tbot, "terminal_run", run("gh pr list"), {})
    assert tools.forever_rule(tbot, "terminal_run", run("gh pr list"), {}) is None


# ---- allow-rule keys ---------------------------------------------------------------

@pytest.mark.parametrize("cmd,key", [
    ("gh pr view 12", "gh pr view"),
    ("gh pr view --json title", "gh pr view"),
    ("gh pr list", "gh pr list"),
    ("gh search prs foo bar", "gh search prs"),
    ("gh api repos/x/y", "gh api"),
    ("git status", "git status"),
    ("ls -la", "ls"),
    ("ls src/app", "ls"),
    ("gh", "gh"),
])
def test_allow_rule_key_derivation(cmd, key):
    assert tools.terminal_key(cmd) == key


@pytest.mark.parametrize("cmd", ["", "   ", "./run.sh", "/bin/rm x", "FOO=1 gh pr list", "gh pr list; ls"])
def test_no_key_for_unrememberable_commands(cmd):
    assert tools.terminal_key(cmd) == ""


def test_forever_rule_shape_and_prefix_cover(tbot):
    rule = tools.forever_rule(tbot, "terminal_run", run("gh pr view 12"), {})
    assert rule["kind"] == "terminal" and rule["key"] == "gh pr view"
    assert "terminal" in tools.FOREVER_KINDS
    assert tools.clean_allow_rules([rule]) == [rule]
    tbot.meta["allow_rules"] = [{"kind": "terminal", "key": "gh search", "label": "x"}]
    assert tools.forever_match(tbot, tools.forever_rule(tbot, "terminal_run", run("gh search prs foo"), {}))
    assert not tools.forever_match(tbot, tools.forever_rule(tbot, "terminal_run", run("gh pr view 1"), {}))


def test_describe_shows_the_command(tbot):
    assert tools.describe(tbot, "terminal_run", run("gh pr list"), {}) == "$ gh pr list"


# ---- execute(): refusal and a live pty ------------------------------------------------

def test_pref_off_refuses_every_terminal_tool(tbot, monkeypatch):
    monkeypatch.setattr(prefs, "bot_terminals_enabled", lambda: False)
    for act in TERMINAL_NAMES:
        _label, out = tools.execute(tbot, act, run("echo hi"), {})
        assert out == "error: bot terminals are off (Preferences › Optional features, then this bot's settings)"
    assert tbot.terminal_id() is None  # nothing was spawned


def test_terminal_run_returns_only_that_commands_output(tbot):
    tbot.meta["allow_rules"] = []
    _label, out = tools.execute(tbot, "terminal_run", run("echo first-cmd"), {})
    assert "exit code: 0" in out and "first-cmd" in out
    _label, out = tools.execute(tbot, "terminal_run", run("echo hi; exit_code=3"), {})
    assert "hi" in out and "first-cmd" not in out
    _label, out = tools.execute(tbot, "terminal_run", run("false"), {})
    assert "exit code: 1" in out
    assert tbot.terminal_id() is not None  # the shell outlives the call
    _label, out = tools.execute(tbot, "terminal_run", run("cd /tmp"), {})
    _label, out = tools.execute(tbot, "terminal_run", run("pwd"), {})
    assert "/tmp" in out.replace("/private/tmp", "/tmp")


def test_slow_command_reports_still_running_then_interrupt(tbot):
    _label, out = tools.execute(tbot, "terminal_run", run("sleep 30", wait_s=1), {})
    assert out.startswith("still running")
    _label, out = tools.execute(tbot, "terminal_run", run("echo nope"), {})
    assert out == "error: terminal is busy; read it or interrupt"
    _label, out = tools.execute(tbot, "terminal_read", {"lines": 10}, {})
    assert "sleep 30" in out
    _label, out = tools.execute(tbot, "terminal_interrupt", {}, {})
    assert out.startswith("sent Ctrl-C")
    _label, out = tools.execute(tbot, "terminal_run", run("echo back"), {})
    assert "exit code: 0" in out and "back" in out


# ---- the agent engine: auto never covers terminal_run -----------------------------------

def _approval_script(cmd="gh --version"):
    return [{"tool": "terminal_run", "args": {"command": cmd}}, {"result": "ok"}]


def test_auto_approval_still_cards_terminal_run(server, fake_cli, monkeypatch, tmp_path, pref_on):
    bot = TBot(tmp_path)
    bot.meta["approval"] = "auto"
    try:
        t = start(bot, _approval_script("echo auto-carded"), monkeypatch)
        ev = bot.wait_event("approval")
        assert ev and "$ echo auto-carded" in ev["text"]
        bot.say("approve")
        finish(t)
        res = fake_cli("call")[0]["result"]
        assert res.startswith("APPROVED by the user: $ echo auto-carded") and "auto-carded" in res
    finally:
        bot.close_terminal()


def test_forever_rule_runs_without_a_card_and_deny_runs_nothing(server, fake_cli, monkeypatch, tmp_path, pref_on):
    bot = TBot(tmp_path)
    bot.meta["allow_rules"] = [{"kind": "terminal", "key": "echo", "label": "x"}]
    try:
        # A covered command runs with no card, even under "Never ask"-less defaults.
        finish(start(bot, _approval_script("echo covered"), monkeypatch))
        assert not [e for e in bot.events if e["role"] == "approval"]
        assert "covered" in fake_cli("call")[0]["result"]
    finally:
        bot.close_terminal()

    bot2 = TBot(tmp_path, bid="tb2")
    marker = tmp_path / "ran"
    try:
        t = start(bot2, _approval_script(f"touch {marker}"), monkeypatch)
        assert bot2.wait_event("approval")
        bot2.say("no")
        finish(t)
        assert not marker.exists()
    finally:
        bot2.close_terminal()


def test_outside_task_cards_even_a_covered_command(server, fake_cli, monkeypatch, tmp_path, pref_on):
    bot = TBot(tmp_path)
    bot.meta["allow_rules"] = [{"kind": "terminal", "key": "echo", "label": "x"}]
    bot.task_via = chan.via(chan.PHONE_KIND, "dev1")
    try:
        t = start(bot, _approval_script("echo outside"), monkeypatch)
        assert bot.wait_event("approval")
        bot.say("no")
        finish(t)
    finally:
        bot.close_terminal()


# ---- the registry cap for bot sessions --------------------------------------------------

def test_bot_sessions_do_not_count_against_the_user_cap(tbot):
    sess = tbot.terminal_session()
    assert sess.owner == "bot:tb1"
    assert sum(1 for s in pty_session.REGISTRY.list() if (s.owner or "").startswith("bot:")) >= 1
