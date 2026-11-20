"""The Cursor backend against the REAL `cursor-agent` and a real model:
docs/CURSOR_BACKEND.md §10's "verified live" list, re-checkable.

Opt-in twice over: marked `integration` (deselected by default; run with
`pytest -m integration -n0 tests/test_cursor_live.py`), and skipped unless
`CURSOR_API_KEY` is set and a binary resolves (`FUSED_RENDER_CURSOR_BIN`, else
`cursor-agent` on PATH). The key is read from the environment only and never
written anywhere by these tests.

Every child runs with an ISOLATED `HOME` (a temp dir): Cursor writes
`~/.cursor/cli-config.json` — `session/set_model` persists the picked model
there — and its first key-signed run stores an exchanged token under
`~/.config/cursor`. Nothing here touches the user's own Cursor config.

Prompts are tiny and every wait is bounded; a turn on Cursor's Auto takes
1–3 s. The host is spawned the way `_start` spawns it (a detached process,
a JSON request on stdin, the inbox written with `agent._write_inbox_entry`),
the same harness tests/test_cursor_host.py drives the fake with.
"""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.integration


def _resolve_bin():
    path = os.environ.get("FUSED_RENDER_CURSOR_BIN") or shutil.which("cursor-agent")
    return path if path and os.access(path, os.X_OK) else None


BIN = _resolve_bin()
if not os.environ.get("CURSOR_API_KEY") or not BIN:
    pytest.skip("needs CURSOR_API_KEY and a cursor-agent binary "
                "(FUSED_RENDER_CURSOR_BIN or cursor-agent on PATH)",
                allow_module_level=True)

AGENT_DIR = os.path.abspath(os.path.join("fused_render", "claude_agent"))
HOST = os.path.join(AGENT_DIR, "cursor_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
TURN = 120.0   # a generous bound for one real turn


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wait_for(predicate, timeout=TURN, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


@pytest.fixture(scope="module")
def live_home(tmp_path_factory):
    """One isolated HOME + FUSED_RENDER_HOME for the module (the key's token
    exchange happens once, not per test)."""
    root = tmp_path_factory.mktemp("cursor-live")
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(root / "home"))
    mp.setenv("FUSED_RENDER_HOME", str(root / "fused"))
    mp.setenv("FUSED_RENDER_CURSOR_BIN", BIN)
    os.makedirs(str(root / "home"))
    yield root
    mp.undo()


@pytest.fixture()
def agent(live_home, tmp_path, monkeypatch):
    mod = _load("claude_agent_for_cursor_live", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


class Live:
    """One real `cursor_host.py` over the real CLI."""

    def __init__(self, agent, cwd, resume_sid="", cli_mode="default", model="auto",
                 instructions=None):
        self.agent = agent
        self._sid = resume_sid
        self.cwd = str(cwd)
        os.makedirs(self.cwd, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": resume_sid, "file": self.cwd,
                       "backend": "cursor"}, fh)
        if instructions is not None:
            os.makedirs(os.path.join(self.run_dir, "cursor_agent"))
            with open(os.path.join(self.run_dir, "cursor_agent", "instructions.md"), "w") as fh:
                fh.write(instructions + "\n")
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": cli_mode,
               "session_id": resume_sid, "model": model, "effort": "",
               "extra_read_dirs": [], "new_session_id": "", "argv": [BIN, "acp"],
               "backend": "cursor"}
        env = dict(os.environ)
        env.pop("FUSED_RENDER_ORIGIN", None)
        env.update({"FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS": "0.1",
                    "FUSED_RENDER_PERMISSION_TIMEOUT": "120"})
        self.err = open(os.path.join(self.run_dir, "host.err.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, HOST], stdin=subprocess.PIPE,
                                     stderr=self.err, env=env)
        self.proc.stdin.write(json.dumps(req).encode("utf-8"))
        self.proc.stdin.close()

    # -- reading
    def out(self):
        return _rows(os.path.join(self.run_dir, "out.jsonl"))

    def results(self):
        return [r for r in self.out() if r.get("type") == "result"]

    def init(self):
        assert _wait_for(lambda: any(r.get("subtype") == "init" for r in self.out()), 60), \
            self.diag()
        return next(r for r in self.out() if r.get("subtype") == "init")

    @property
    def sid(self):
        if not self._sid:
            self._sid = self.init()["session_id"]
        return self._sid

    def tool_uses(self):
        return [b for r in self.out() if r.get("type") == "assistant"
                for b in r["message"]["content"] if b.get("type") == "tool_use"]

    def tool_results(self):
        return [b for r in self.out() if r.get("type") == "user"
                for b in r["message"].get("content", []) if b.get("type") == "tool_result"]

    def cards(self):
        perm = os.path.join(self.run_dir, "perm")
        try:
            names = sorted(n for n in os.listdir(perm) if n.endswith(".req.json"))
        except FileNotFoundError:
            return []
        return [json.load(open(os.path.join(perm, n))) for n in names]

    def control_responses(self):
        return [r["response"] for r in self.out() if r.get("type") == "control_response"]

    def diag(self):
        def tail(name):
            try:
                with open(os.path.join(self.run_dir, name), "rb") as fh:
                    return fh.read()[-2000:].decode("utf-8", "replace")
            except OSError:
                return ""
        rows = [r for r in self.out() if r.get("type") != "stream_event"]
        return "out: %s\nerr.log: %s\nhost.err: %s" % (
            json.dumps(rows)[-3000:], tail("err.log"), tail("host.err.log"))

    # -- driving
    def say(self, text):
        self.agent._write_inbox_entry(self.run_dir, text)

    def control(self, subtype, **fields):
        return self.agent._write_control_request(self.run_dir, subtype, **fields)

    def decide(self, perm_id, **decision):
        with open(os.path.join(self.run_dir, "perm", perm_id + ".res.json"), "w") as fh:
            json.dump(decision, fh)

    def wait_results(self, n=1, timeout=TURN):
        assert _wait_for(lambda: len(self.results()) >= n, timeout), self.diag()
        return self.results()

    def wait_card_or_result(self, n_cards, n_results, timeout=TURN):
        assert _wait_for(lambda: len(self.cards()) >= n_cards
                         or len(self.results()) >= n_results, timeout), self.diag()
        return len(self.cards()) >= n_cards

    def stop(self):
        """SIGTERM first (the host's `close()` runs: the CLI gets its own
        SIGTERM, the registry row goes), then SIGKILL whatever is left."""
        pids = [self.proc.pid]
        try:
            pids.append(int(open(os.path.join(self.run_dir, "pid")).read().strip()))
        except (OSError, ValueError):
            pass
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pid in pids:
            for fn in (os.killpg, os.kill):
                try:
                    fn(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.err.close()


@pytest.fixture()
def start(agent, tmp_path):
    started = []

    def _start(cwd=None, **kw):
        s = Live(agent, cwd or (tmp_path / "proj"), **kw)
        started.append(s)
        return s

    yield _start
    for s in started:
        s.stop()


# ------------------------------------------------------------------ health

def test_health_reads_signed_in_by_api_key_with_base_model_ids(live_home):
    from fused_render import cursor_health
    snap = cursor_health.summary_refreshed()
    assert snap["found"] is True and snap["broken"] is False
    assert snap["signed_in"] is True
    assert snap["account"]["method"] == "api_key"
    ids = [m["id"] for m in snap["models"] or []]
    assert ids, snap
    # Base names (the parameterized picker), never variant strings such as
    # `gpt-5.5[effort=high]`; Cursor's Auto is a listed row.
    assert all("[" not in i and "=" not in i for i in ids), ids
    assert any(i.startswith("auto") for i in ids), ids
    # The catalog serves Cursor's own Auto row first, in place of the seed.
    from fused_render.agent_backends import cursor
    first = cursor.discovered_models(snap)[0]
    assert first.id.startswith("auto") and first.id in ids


# ------------------------------------------------------------- a plain turn

def test_a_plain_turn_streams_and_the_instructions_reach_the_model(start):
    """The D8 context block rides the first `session/prompt` as a leading
    text block: a distinctive rule in it is followed, so the multi-block
    prompt reached the model in order."""
    s = start(instructions="Always end every reply with the token ZEBRA-7 on its own line.")
    s.say("Reply with exactly: pong")
    [res] = s.wait_results(1)
    init = s.init()
    assert init["backend"] == "cursor"
    # `auto` resolved to the account's own Auto row, named by every row.
    model = init["model"]
    assert model.startswith("auto") and model != "auto"
    notice = [r for r in s.out() if r.get("subtype") == "task_notification"]
    assert notice and notice[0]["summary"] == "Handled by Cursor · model %s" % model
    deltas = [r for r in s.out() if r.get("type") == "stream_event"
              and r["event"].get("type") == "content_block_delta"]
    assert deltas, s.diag()
    assert res["subtype"] == "success" and res["is_error"] is False
    assert res["stop_reason"] == "end_turn"
    assert "pong" in res["result"].lower()
    assert "ZEBRA-7" in res["result"]
    # The echo and the transcript carry the user's words only.
    users = [r for r in s.out() if r.get("type") == "user" and "uuid" in r]
    assert users[0]["message"]["content"] == [{"type": "text",
                                               "text": "Reply with exactly: pong"}]


# ------------------------------------------------------- permissions (D4/D5)

SHELL = "Run this exact shell command with your shell tool, nothing else: %s"
EDIT = ("Create a file named %s containing the single word hello, using your file "
        "edit/write tool (not the shell). Do nothing else.")


def test_ask_mode_a_shell_card_allow_runs_and_deny_does_not(start, tmp_path):
    cwd = tmp_path / "ask"
    s = start(cwd=cwd, cli_mode="default")
    s.say(SHELL % "echo fused-live > out.txt")
    assert s.wait_card_or_result(1, 1), "no card for a shell command\n" + s.diag()
    card = s.cards()[0]
    assert card["tool"] == "Bash"
    assert card["input"]["command"] == "echo fused-live > out.txt"
    s.decide(card["id"], decision="allow")
    s.wait_results(1)
    assert (cwd / "out.txt").read_text().strip() == "fused-live"
    [use] = [u for u in s.tool_uses() if u["id"] == card["tool_use_id"]]
    assert use["input"]["command"] == "echo fused-live > out.txt"
    [res] = [r for r in s.tool_results() if r["tool_use_id"] == card["tool_use_id"]]
    assert res["is_error"] is False

    s.say(SHELL % "echo second > out2.txt")
    assert s.wait_card_or_result(2, 2), s.diag()
    card2 = s.cards()[1]
    assert card2["input"]["command"] == "echo second > out2.txt"
    s.decide(card2["id"], decision="deny")
    s.wait_results(2)
    assert not (cwd / "out2.txt").exists()
    [res2] = [r for r in s.tool_results() if r["tool_use_id"] == card2["tool_use_id"]]
    assert res2 == {"type": "tool_result", "tool_use_id": card2["tool_use_id"],
                    "content": "The user denied this request.", "is_error": True}


def test_ask_mode_an_edit_in_the_workspace_raises_no_card(start, tmp_path):
    """Observed: Cursor's own policy lets an edit inside the workspace run
    without asking, so even Ask mode shows no card for it. The row waits for
    the content (the diff arrives only on `completed`) and reads as a Write
    of it."""
    cwd = tmp_path / "ask-edit"
    s = start(cwd=cwd, cli_mode="default")
    s.say(EDIT % "note.txt")
    s.wait_results(1)
    assert s.cards() == []
    assert (cwd / "note.txt").read_text().strip() == "hello"
    writes = [u for u in s.tool_uses() if u["name"] in ("Write", "Edit")]
    assert writes, s.diag()
    assert writes[0]["name"] == "Write"   # a new file
    assert writes[0]["input"]["file_path"].endswith("note.txt")
    assert writes[0]["input"]["content"].strip() == "hello"
    res = [r for r in s.tool_results() if r["tool_use_id"] == writes[0]["id"]]
    assert res and "/dev/null" not in res[0]["content"]


def test_accept_edits_runs_an_edit_and_still_asks_for_a_shell_command(start, tmp_path):
    cwd = tmp_path / "accept"
    s = start(cwd=cwd, cli_mode="acceptEdits")
    s.say(EDIT % "a.txt")
    s.wait_results(1)
    assert s.cards() == []
    assert (cwd / "a.txt").read_text().strip() == "hello"
    s.say(SHELL % "echo accept > b.txt")
    assert s.wait_card_or_result(1, 2), "no card for a shell command\n" + s.diag()
    card = s.cards()[0]
    assert card["tool"] == "Bash" and card["input"]["command"] == "echo accept > b.txt"
    s.decide(card["id"], decision="deny")
    s.wait_results(2)
    assert not (cwd / "b.txt").exists()


def test_auto_mode_asks_nothing_and_the_row_shows_the_whole_command(start, tmp_path):
    cwd = tmp_path / "auto"
    s = start(cwd=cwd, cli_mode="auto")
    cmd = "echo fused-auto-mode-check > c.txt"
    s.say(SHELL % cmd)
    s.wait_results(1)
    assert s.cards() == []
    assert (cwd / "c.txt").read_text().strip() == "fused-auto-mode-check"
    bash = [u for u in s.tool_uses() if u["name"] == "Bash"]
    # Raised once, with the WHOLE command — never a half-streamed one.
    assert [u["input"]["command"] for u in bash] == [cmd]


# -------------------------------------------------------- model, stop, resume

def test_a_live_model_switch_moves_the_next_turns_rows(start, live_home):
    from fused_render import cursor_health
    ids = [m["id"] for m in cursor_health.summary()["models"] or []]
    target = next((i for i in ("gpt-5.4-mini", "gpt-5-mini", "claude-haiku-4-5")
                   if i in ids), None) or next(i for i in ids if not i.startswith("auto"))
    s = start()
    s.say("Reply with exactly: one")
    s.wait_results(1)
    rid = s.control("set_model", model=target)
    assert _wait_for(lambda: any(r.get("request_id") == rid
                                 for r in s.control_responses()), 60), s.diag()
    resp = next(r for r in s.control_responses() if r.get("request_id") == rid)
    assert resp["subtype"] == "success", resp
    s.say("Reply with exactly: two")
    s.wait_results(2)
    second = [r for r in s.out() if r.get("type") == "assistant"][-1]
    assert second["message"]["model"] == target


def test_an_interrupt_cancels_the_turn_within_the_grace(start):
    s = start()
    s.say("Count from 1 to 2000, one number per line, with no other text.")
    assert _wait_for(lambda: any(r.get("type") == "stream_event"
                                 and r["event"].get("type") == "content_block_delta"
                                 for r in s.out()), 90), s.diag()
    t0 = time.monotonic()
    s.control("interrupt")
    [res] = s.wait_results(1, timeout=30)
    assert time.monotonic() - t0 < 15.0
    assert res["stop_reason"] == "cancelled", res


def test_a_resume_replays_without_duplicates_and_remembers(start, agent):
    first = start(instructions="Be brief.")
    first.say("Reply with exactly: pong")
    first.wait_results(1)
    sid = first.sid
    first.stop()
    import agent_roots
    found = agent_roots.find_transcript(sid, first.cwd)
    assert found and found[0] == "cursor", found
    tx_path = found[1]
    assert tx_path.startswith(os.path.join(os.environ["FUSED_RENDER_HOME"], "cursor",
                                           "projects"))
    before = _rows(tx_path)

    second = start(resume_sid=sid)
    assert second.init()["session_id"] == sid
    # The replay (`session/load`) backfills nothing already there.
    time.sleep(1.0)
    after_load = _rows(tx_path)
    def prompts(rows):
        return [r for r in rows if r.get("type") == "user" and "uuid" in r
                and any(b.get("type") == "text" for b in r["message"].get("content", [])
                        if isinstance(b, dict))]
    assert len(prompts(after_load)) == len(prompts(before)), (before, after_load)
    texts = [b["text"] for r in prompts(after_load) for b in r["message"]["content"]
             if isinstance(b, dict) and b.get("type") == "text"]
    assert not any("<fused_render_context>" in t for t in texts), texts

    second.say("What exactly did you reply before? Answer with that one word only.")
    [res] = second.wait_results(1)
    assert "pong" in res["result"].lower(), res
    final = prompts(_rows(tx_path))
    assert len(final) == len(prompts(before)) + 1


# ------------------------------------------------------------ recap one-shot

def test_the_recap_one_shot_reads_the_prompt_from_stdin(agent, live_home):
    """`-p` with the prompt on stdin and NO positional prompt (with one, the
    CLI ignores stdin), and the reply off the `{type: "result"}` line."""
    from fused_render import agent_oneshot
    spec = agent_oneshot.prepare(
        "cursor", "", "Be brief.",
        "Summarise in one word, and that word must be exactly: hello", agent=agent)
    reply = agent_oneshot.run_sync(spec, 120)
    assert "hello" in reply.lower(), reply
