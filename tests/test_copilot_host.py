"""copilot_host.py: the Copilot session host, driven end to end against a fake
ACP peer (tests/fake_copilot_acp.py) exactly the way `_start` will drive the
real one — a detached subprocess, a JSON request on stdin, the inbox written
with `agent._write_inbox_entry` / `_write_control_request`, and every
assertion read back off the files the rest of the app reads: `out.jsonl`
through agent.py's own readers (`_turn_state`, `_starts_new_turn`,
`_segments_from_rows`, `_session_from_out`), the transcript through
`tasks_store._parse_head` and `session_liveness.transcript_turn_open`, the
cards through `perm/*.req.json`.

One test per invariant in docs/COPILOT_BACKEND.md §6 (1–6), plus the row
contract's edges: tool mapping, the held inbox, interrupt → control_response,
permission allow_always / timeout, an error reply, the signed-out refusal,
transport death, the D5 mode at spawn, the D3 backfill, the idle reap (and
its two in-process edges), and permission_server's `--no-approve`.
"""
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

import pytest

from _mcp_stdio import MCPServer
from fused_render import session_liveness, tasks_store

AGENT_DIR = os.path.abspath(os.path.join("fused_render", "claude_agent"))
HOST = os.path.join(AGENT_DIR, "copilot_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
PERMISSION_SERVER = os.path.join(AGENT_DIR, "permission_server.py")
FAKE = os.path.abspath(os.path.join("tests", "fake_copilot_acp.py"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    mod = _load("claude_agent_for_copilot_tests", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def host_mod():
    return _load("copilot_host_under_test", HOST)


def _wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


NOTICE = "Handled by GitHub Copilot · model gpt-5.4"


def _reply(segments):
    """The reply's own segments, without a new session's D18 notice."""
    return [sg for sg in segments if sg["kind"] != "notice"]


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


class Session:
    """One host subprocess against the fake, with the files it writes."""

    def __init__(self, agent, tmp_path, resume=False, fake_args=(), env=None,
                 transcript_seed=None, cli_mode=None, messages=(),
                 seed_bucket=None):
        import agent_roots  # importable once agent.py seeded the shared dir
        self.agent = agent
        # A resume names its session; a fresh one learns it off the init row,
        # because Copilot mints it (`--acp` refuses `--session-id`).
        self._sid = str(uuid.uuid4()) if resume else ""
        self.cwd = str(tmp_path / "proj")
        os.makedirs(self.cwd, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        # `_start`'s meta for Copilot: no `session_id` on a fresh run.
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": self._sid, "file": self.cwd,
                       "backend": "copilot"}, fh)
        # Where this session's transcript lives: the cwd's own bucket, or —
        # `seed_bucket`, a resume whose transcript is elsewhere (a moved
        # folder) — that one.
        self.cwd_bucket = os.path.join(agent_roots.projects_dir("copilot"),
                                       agent_roots.munge(self.cwd))
        self.bucket = os.path.join(agent_roots.projects_dir("copilot"), seed_bucket) \
            if seed_bucket else self.cwd_bucket
        self.registry_dir = agent_roots.sessions_dir("copilot")
        if transcript_seed:
            os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
            with open(self.transcript, "w") as fh:
                for row in transcript_seed:
                    fh.write(json.dumps(row) + "\n")
        self.log = os.path.join(self.run_dir, "fake.log")
        self.gate = os.path.join(self.run_dir, "gate")
        self.env_dump = os.path.join(self.run_dir, "fake.env.json")
        # Inbox rows already waiting when the host comes up — `_start`'s
        # first message plus whatever `_send` raced in behind it.
        for text in messages:
            agent._write_inbox_entry(self.run_dir, text)
        argv = [sys.executable, FAKE, "--acp", "--no-auto-update",
                "--model", "gpt-5.4", "--log", self.log,
                "--check-echo", os.path.join(self.run_dir, "out.jsonl"),
                "--dump-env", self.env_dump] + list(fake_args)
        # The request `agent._start` writes, key for key (`cli_mode` is
        # PERMISSION_MODES' value: None for prompt).
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": cli_mode,
               "session_id": self._sid, "model": "gpt-5.4",
               "effort": "medium", "extra_read_dirs": [],
               "new_session_id": "", "argv": argv, "backend": "copilot"}
        host_env = dict(os.environ)
        host_env.pop("FUSED_RENDER_ORIGIN", None)
        # A token preset, so `_spawn_env` never shells out to `gh auth token`.
        host_env.update({"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "5",
                         "FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS": "0.05",
                         "COPILOT_GITHUB_TOKEN": "test-token"})
        host_env.update(env or {})
        self.err = open(os.path.join(self.run_dir, "host.err.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, HOST], stdin=subprocess.PIPE,
                                     stderr=self.err, env=host_env)
        self.proc.stdin.write(json.dumps(req).encode("utf-8"))
        self.proc.stdin.close()

    # --- what the host wrote
    @property
    def sid(self):
        """The session: a resume's own, or whatever the first init row says
        (waited for) — "" when the session never came up."""
        if not self._sid:
            assert _wait_for(lambda: any(r.get("subtype") == "init" for r in self.out())), \
                "no init row\nhost.err: %s" % self.host_err()
            self._sid = next(r for r in self.out()
                             if r.get("subtype") == "init")["session_id"]
        return self._sid

    @property
    def transcript(self):
        return os.path.join(self.bucket, self.sid + ".jsonl") if self.sid else ""

    def minted(self):
        """The id the fake's `session/new` reply named."""
        return next(e["sent"]["result"]["sessionId"] for e in self.fake()
                    if "sessionId" in ((e.get("sent") or {}).get("result") or {}))

    def out(self):
        return _rows(os.path.join(self.run_dir, "out.jsonl"))

    def tx(self):
        return _rows(self.transcript)

    def fake(self):
        return _rows(self.log)

    def results(self):
        return [r for r in self.out() if r.get("type") == "result"]

    def prompts_seen(self):
        return [e for e in self.fake()
                if e.get("msg", {}).get("method") == "session/prompt"]

    def methods_seen(self):
        return [e["msg"].get("method") for e in self.fake() if "msg" in e]

    def config_values(self):
        return [e["msg"]["params"]["value"] for e in self.fake()
                if e.get("msg", {}).get("method") == "session/set_config_option"]

    def overlaps(self):
        return [e for e in self.fake() if e.get("overlap")]

    def inbox_pending(self):
        inbox = os.path.join(self.run_dir, "inbox")
        return sorted(n for n in os.listdir(inbox) if n.endswith(".json"))

    def perm_requests(self):
        perm = os.path.join(self.run_dir, "perm")
        try:
            names = sorted(n for n in os.listdir(perm) if n.endswith(".req.json"))
        except FileNotFoundError:
            return []
        return [json.load(open(os.path.join(perm, n))) for n in names]

    def decide(self, perm_id, **decision):
        with open(os.path.join(self.run_dir, "perm", perm_id + ".res.json"),
                  "w") as fh:
            json.dump(decision, fh)

    def host_err(self):
        with open(os.path.join(self.run_dir, "host.err.log"), "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    # --- driving it
    def say(self, text):
        self.agent._write_inbox_entry(self.run_dir, text)

    def control(self, subtype, **fields):
        return self.agent._write_control_request(self.run_dir, subtype, **fields)

    def open_gate(self):
        open(self.gate, "w").close()

    def wait_results(self, n=1, timeout=15.0):
        assert _wait_for(lambda: len(self.results()) >= n, timeout), \
            "no result row\nout: %s\nhost.err: %s" % (self.out(), self.host_err())
        return self.results()

    def stop(self):
        pids = {self.proc.pid}
        try:
            pids.add(int(open(os.path.join(self.run_dir, "pid")).read().strip()))
        except (OSError, ValueError):
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

    def _start(**kw):
        s = Session(agent, tmp_path, **kw)
        started.append(s)
        return s

    yield _start
    for s in started:
        s.stop()


# ------------------------------------------------------------- invariants

def test_first_row_is_init_with_the_session_id(start, agent):
    """§6.1 — and the rest of what the handshake leaves behind: the D18 notice
    as the transcript's first row, the registry row, the pid file naming the
    CLI (not the host)."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1 and os.path.exists(s.transcript))
    first = s.out()[0]
    assert first["type"] == "system" and first["subtype"] == "init"
    # COPILOT'S id, off the `session/new` reply — the request named none.
    assert first["session_id"] == s.sid == s.minted()
    assert uuid.UUID(s.sid)
    assert os.path.basename(s.transcript) == s.minted() + ".jsonl"
    assert first["backend"] == "copilot" and first["model"] == "gpt-5.4"
    assert first["slash_commands"] == ["help", "model"]
    assert first["cwd"] == s.cwd and first["timestamp"].endswith("Z")
    assert agent._session_from_out(s.run_dir) == s.sid

    assert _wait_for(lambda: len(s.tx()) >= 2)
    notice = s.tx()[0]
    assert notice["type"] == "system" and notice["subtype"] == "task_notification"
    assert notice["summary"] == "Handled by GitHub Copilot · model gpt-5.4"
    assert notice["cwd"] == s.cwd and notice["sessionId"] == s.sid
    assert any(r.get("type") == "ai-title" and r["aiTitle"] == "A fake session"
               for r in s.tx())

    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    assert cli_pid != s.proc.pid
    host = json.load(open(os.path.join(s.run_dir, "host.json")))
    assert host["pid"] == s.proc.pid and host["backend"] == "copilot"
    # Written "" at spawn, re-written once Copilot named the session.
    assert host["session_id"] == s.sid
    assert agent._host_alive(s.run_dir)
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % cli_pid)))
    assert reg == {"sessionId": s.sid, "cwd": s.cwd, "status": "idle",
                   "updatedAt": reg["updatedAt"], "pid": cli_pid}


def test_a_fresh_session_is_recorded_as_copilot(start, agent):
    """D2: Copilot's minted id gets its session-settings record — model,
    effort, backend — from the host itself, so `_session_backend` names it
    copilot from the first instant the id is visible anywhere."""
    s = start()
    assert s.sid
    assert agent._session_backend(s.sid) == "copilot"
    assert agent._session_settings(s.sid) == ("gpt-5.4", "medium")


def test_the_echo_is_written_before_the_prompt_goes_out(start, agent):
    """§6.3 — the fake reads out.jsonl the instant `session/prompt` reaches
    it and finds the echo already there; while it holds the prompt open,
    `_turn_state` reads the turn as open."""
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "gate-x")])
    s.gate = os.path.join(agent.RUNS, "gate-x")
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    assert _wait_for(lambda: s.prompts_seen())
    checks = [e for e in s.fake() if "echo_seen" in e]
    assert checks == [{"echo_seen": True, "text": "hi", "t": checks[0]["t"]}]
    rows = s.out()
    echo = [r for r in rows if agent._starts_new_turn(r)]
    assert len(echo) == 1 and echo[0]["message"]["content"][0]["text"] == "hi"
    assert echo[0]["session_id"] == s.sid
    assert not s.results()
    assert agent._turn_state(s.run_dir) == (True, False)
    assert s.inbox_pending() == []   # moved to done/ once sent
    assert agent._inbox_drained_any(s.run_dir)
    s.open_gate()
    s.wait_results(1)


def test_the_first_poll_of_a_new_session_shows_the_d18_notice(start, agent):
    """D18 LIVE: "Handled by GitHub Copilot · model <id>" is on the first
    poll of a NEW session, mid-turn — written right after the echo, so the
    echo still opens the turn, the notice moves no turn state, and the
    `result` closes it as before. Once only: turn two has none, nor does a
    resume."""
    gate = os.path.join(agent.RUNS, "gate-notice")
    s = start(fake_args=["--block-until", gate])
    s.say("hi")
    assert _wait_for(lambda: s.prompts_seen())
    rows = s.out()
    echo = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    assert rows[echo + 1]["subtype"] == "task_notification"
    assert rows[echo + 1]["summary"] == NOTICE
    assert agent._turn_state(s.run_dir) == (True, False)
    p = agent._poll(os.path.basename(s.run_dir))
    assert not p["done"]
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]][:1] == [("notice", NOTICE)]
    open(gate, "w").close()
    s.wait_results(1)
    assert agent._turn_state(s.run_dir) == (False, False)
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["text"] == "Hello world!"
    s.say("two")
    s.wait_results(2)
    assert [r["summary"] for r in s.out()
            if r.get("subtype") == "task_notification"] == [NOTICE]
    assert agent._turn_state(s.run_dir) == (False, False)


def test_every_prompt_ends_in_exactly_one_result(start, agent):
    """§6.2, and the whole of the streamed shape `_poll` parses."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    results = s.wait_results(1)
    time.sleep(0.3)   # nothing else may follow the result
    rows = s.out()
    assert [r["type"] for r in rows].count("result") == 1
    result = results[0]
    assert result["subtype"] == "success" and result["is_error"] is False
    assert result["stop_reason"] == "end_turn"
    assert result["result"] == "Hello world!"
    assert result["session_id"] == s.sid
    assert result["usage"]["input_tokens"] == 1234
    assert result["usage"]["cache_read_input_tokens"] == 100
    assert result["num_turns"] == 1
    assert rows[-1]["type"] == "result"
    # The echo opens the turn; the result closes it; nothing reopens it.
    assert agent._turn_state(s.run_dir) == (False, False)
    # message_start carries the usage_update's `used` as the context reading.
    starts = [r for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "message_start"]
    assert len(starts) == 1
    reading = agent._context_usage(starts[0]["event"]["message"])
    assert (reading["input_tokens"], reading["cache_read_input_tokens"],
            reading["model"]) == (1234, 0, "gpt-5.4")
    # The late `size` landed as a status row INSIDE the turn, not after it.
    status = [r for r in rows if r.get("type") == "system"
              and r.get("subtype") == "status"]
    assert status and status[0]["context_window"] == 200000
    assert rows.index(status[0]) < rows.index(result)
    # Deltas, then the finalized assistant row with the same text.
    turn_rows = rows[rows.index(next(r for r in rows if agent._starts_new_turn(r))):]
    segments = agent._segments_from_rows(turn_rows)
    # A NEW session's first turn opens on the D18 notice, then the reply.
    assert [(sg["kind"], sg["text"]) for sg in segments] == \
        [("notice", NOTICE), ("text", "Hello world!")]
    finals = [r for r in rows if r.get("type") == "assistant"]
    assert len(finals) == 1
    assert finals[0]["message"]["content"] == [{"type": "text", "text": "Hello world!"}]
    assert finals[0]["message"]["usage"]["input_tokens"] == 1234
    # L16: the reply's `outputTokens` rides the last message's delta + row.
    assert finals[0]["message"]["usage"]["output_tokens"] == 7
    deltas = [r for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "message_delta"]
    assert deltas[-1]["event"]["usage"]["output_tokens"] == 7
    assert result["usage"]["output_tokens"] == 7
    # Every row is stamped.
    assert all(r.get("timestamp", "").endswith("Z") for r in rows)

    # ...and the transcript the tasks readers parse.
    tx = s.tx()
    cwd, first_ts, prompt, pane, entrypoint, settled = tasks_store._parse_head(
        s.transcript)
    assert (cwd, prompt, entrypoint, settled) == (s.cwd, "hi", "sdk-cli", True)
    assert first_ts is not None
    user = [r for r in tx if r.get("type") == "user"]
    assert len(user) == 1 and user[0]["isMeta"] is False
    assert user[0]["sessionId"] == s.sid and user[0]["parentUuid"]
    assert user[0]["uuid"]
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    assert tx[-1]["type"] == "system" and tx[-1]["subtype"] == "turn_duration"
    assert tx[-2]["type"] == "assistant"
    assert tx[-2]["message"]["content"][0]["text"] == "Hello world!"
    # The turn's own uuid chain: each row's parent is the row before it.
    assert tx[-2]["parentUuid"] == user[0]["uuid"]
    assert tx[-1]["parentUuid"] == tx[-2]["uuid"]
    # The registry flipped back to idle on the turn edge.
    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % cli_pid)))
    assert reg["status"] == "idle"
    # ...and the composite reader the page calls agrees with every part.
    p = agent._poll(os.path.basename(s.run_dir))
    assert (p["done"], p["text"], p["session_id"], p["error"]) == \
        (True, "Hello world!", s.sid, "")
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]] == \
        [("notice", NOTICE), ("text", "Hello world!")]


def test_a_tool_call_becomes_the_claude_tool_rows(start, agent):
    """D14 on the wire: `content_block_start` + `assistant` tool_use, the
    result as a `tool_result` user row (never a text one — it must not start
    a turn), and the id shared by all three."""
    s = start(fake_args=["--tool"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("list")
    s.wait_results(1)
    rows = s.out()
    starts = [r for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "content_block_start"]
    assert len(starts) == 1
    block = starts[0]["event"]["content_block"]
    assert block["type"] == "tool_use" and block["id"] == "call-1"
    assert block["name"] == "Bash"
    assert block["input"]["command"] == "ls -la"
    assert block["input"]["description"] == "list files"
    # L11: kind + title only — a mapped kind's input already says the rest.
    assert block["input"]["_acp"] == {"kind": "execute", "title": "Run ls"}
    tool_rows = [r for r in rows if r.get("type") == "assistant"
                 and r["message"]["content"][0]["type"] == "tool_use"]
    assert len(tool_rows) == 1 and tool_rows[0]["message"]["content"] == [block]
    results = [r for r in rows if r.get("type") == "user"
               and r["message"]["content"][0]["type"] == "tool_result"]
    assert len(results) == 1
    res = results[0]["message"]["content"][0]
    assert res == {"type": "tool_result", "tool_use_id": "call-1",
                   "content": "a.txt\nb.txt", "is_error": False}
    assert not agent._starts_new_turn(results[0])
    turn_rows = rows[rows.index(next(r for r in rows if agent._starts_new_turn(r))):]
    segments = _reply(agent._segments_from_rows(turn_rows))
    kinds = [sg["kind"] for sg in segments]
    assert kinds == ["text", "tool", "text"], segments
    assert segments[1]["name"] == "Bash" and segments[1]["output"] == "a.txt\nb.txt"
    assert segments[0]["text"] == "Hello world" and segments[2]["text"] == "\n\n!"
    # Text before the call was finalized as its own assistant row (the
    # message ended at the tool call), num_turns counts both messages + tail.
    assert s.results()[0]["num_turns"] == 3
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["error"] == ""
    assert [sg["kind"] for sg in _reply(p["segments"])] == ["text", "tool", "text"]
    assert _reply(p["segments"])[1]["name"] == "Bash"
    assert _reply(p["segments"])[1]["output"] == "a.txt\nb.txt"
    # The transcript carries the same three rows and ends closed.
    tx = s.tx()
    assert [r["type"] for r in tx if r["type"] in ("user", "assistant")] == \
        ["user", "assistant", "assistant", "user", "assistant"]
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_tool_mapping_table(host_mod, tmp_path):
    m = host_mod._map_tool
    assert m("execute", "Run", {"command": "ls"}, [], str(tmp_path))[0] == "Bash"
    assert m("read", "Read", {"path": "a.py"}, [], "")[:1] == ("Read",)
    assert m("read", "Read", {}, [{"path": "b.py"}], "")[1]["file_path"] == "b.py"
    assert m("fetch", "Fetch", {"url": "https://x"}, [], "") == (
        "WebFetch", {"url": "https://x", "_acp": {"kind": "fetch", "title": "Fetch"}})
    name, inp = m("search", "Search", {"pattern": "foo", "path": "src"}, [], "")
    assert (name, inp["pattern"], inp["path"]) == ("Grep", "foo", "src")
    name, inp = m("edit", "Create", {"path": "fresh.txt", "content": "x"}, [], str(tmp_path))
    assert (name, inp["file_path"], inp["content"]) == ("Write", "fresh.txt", "x")
    (tmp_path / "there.txt").write_text("old")
    name, inp = m("edit", "Edit", {"path": "there.txt", "diff": "d"}, [], str(tmp_path))
    assert (name, inp["file_path"], inp["diff"]) == ("Edit", "there.txt", "d")
    # M2: an existing file edited with the change ONLY in ACP `content` —
    # the card's diff is built from it (header-free unified diff), never "".
    diff = [{"type": "diff", "path": "there.txt",
             "oldText": "a\nb\nc\n", "newText": "a\nB\nc\n"}]
    name, inp = m("edit", "Edit", {}, [], str(tmp_path), diff)
    assert (name, inp["file_path"]) == ("Edit", "there.txt")
    assert inp["diff"].startswith("@@ ")
    assert "-b\n+B\n" in inp["diff"]
    assert not any(ln.startswith(("--- ", "+++ ")) for ln in inp["diff"].splitlines())
    # ...and a new file from the same shape is a Write of its `newText`.
    name, inp = m("edit", "Create", {}, [], str(tmp_path),
                  [{"type": "diff", "path": "brand_new.txt", "oldText": None,
                    "newText": "hello\n"}])
    assert (name, inp["file_path"], inp["content"]) == ("Write", "brand_new.txt", "hello\n")
    name, inp = m("other", "fused_approvals-app_state", {"reason": "look"}, [], "")
    assert name == "mcp__fused_approvals__app_state" and inp["reason"] == "look"
    # An unmapped kind keeps Copilot's own input under `_acp.raw`.
    assert inp["_acp"] == {"kind": "other", "title": "fused_approvals-app_state",
                           "raw": {"reason": "look"}}
    assert m("other", "github-list_issues", {}, [], "")[0] == "github-list_issues"
    assert m("", "", None, None, "")[0] == "tool"
    # Every mapping carries `_acp` with the kind and title it came from.
    for kind in ("execute", "read", "fetch", "search", "edit", "think"):
        acp = m(kind, "T", {}, [], str(tmp_path))[1]["_acp"]
        assert (acp["kind"], acp["title"]) == (kind, "T")


def test_a_second_prompt_waits_for_the_first_result(start, agent):
    """§6.4 / D6: a user row arriving mid-turn is HELD in inbox/ (undrained,
    so the composer shows it queued) and sent only after the result — never
    a second `session/prompt` while one is open."""
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "gate-held")])
    s.gate = os.path.join(agent.RUNS, "gate-held")
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("one")
    assert _wait_for(lambda: s.prompts_seen())
    s.say("two")
    time.sleep(0.4)
    assert len(s.inbox_pending()) == 1, "the follow-up must stay in inbox/"
    assert len(s.prompts_seen()) == 1
    echoes = [r for r in s.out() if agent._starts_new_turn(r)]
    assert [r["message"]["content"][0]["text"] for r in echoes] == ["one"]
    s.open_gate()
    s.wait_results(2)
    assert s.inbox_pending() == []
    log = s.fake()
    prompts = [e for e in log if e.get("msg", {}).get("method") == "session/prompt"]
    replies = [e for e in log if "sent" in e and e["sent"].get("id") is not None
               and "result" in e["sent"] and "stopReason" in e["sent"]["result"]]
    assert len(prompts) == 2 and len(replies) == 2
    assert prompts[1]["t"] > replies[0]["t"], "second prompt sent before first reply"
    assert [e["msg"]["params"]["prompt"][0]["text"] for e in prompts] == ["one", "two"]
    assert s.overlaps() == []
    assert all(e["echo_seen"] for e in log if "echo_seen" in e)
    rows = s.out()
    kinds = [r["type"] for r in rows]
    first_result = kinds.index("result")
    second_echo = [i for i, r in enumerate(rows) if agent._starts_new_turn(r)][1]
    assert second_echo > first_result
    assert agent._turn_state(s.run_dir) == (False, False)
    # A page polling across both turns is handed turn two alone once its
    # cursor has stepped past turn one's result + echo (two polls: the first
    # advances the cursor, the second reads from it).
    agent._poll(os.path.basename(s.run_dir))
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["text"] == "Hello world!"
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]] == \
        [("text", "Hello world!")]


def test_two_inbox_rows_at_once_are_two_sequential_prompts(start, agent):
    """D6, the other way in: both rows are already in inbox/ when the host
    drains for the first time (`_start`'s message and a `_send` racing in
    behind it). The first is sent, the second HELD in the same pass — never
    two prompts in flight — and sent after the first `result`."""
    s = start(messages=["one", "two"])
    s.wait_results(2)
    assert s.overlaps() == []
    log = s.fake()
    prompts = [e for e in log if e.get("msg", {}).get("method") == "session/prompt"]
    replies = [e for e in log if "result" in (e.get("sent") or {})
               and "stopReason" in e["sent"]["result"]]
    assert [e["msg"]["params"]["prompt"][0]["text"] for e in prompts] == ["one", "two"]
    assert prompts[1]["t"] > replies[0]["t"]
    rows = s.out()
    kinds = [r["type"] for r in rows]
    assert kinds.count("result") == 2
    echoes = [i for i, r in enumerate(rows) if agent._starts_new_turn(r)]
    assert echoes[0] < kinds.index("result") < echoes[1]
    assert s.inbox_pending() == []
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["one", "two"]


def test_a_resume_replays_into_the_transcript_and_never_into_out(start, agent):
    """§6.5 + D3's backfill: the `session/load` replay is swallowed from
    out.jsonl; turns the transcript does not have (taken in a terminal) are
    appended; a resume writes no fresh D18 notice."""
    seed = [
        {"type": "system", "subtype": "task_notification", "cwd": "/x",
         "summary": "Handled by GitHub Copilot · model gpt-5.4",
         "timestamp": "2026-10-16T10:00:00.000Z", "sessionId": "old"},
        {"type": "user", "cwd": "/x", "timestamp": "2026-10-16T10:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
         "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
        {"type": "assistant", "cwd": "/x", "timestamp": "2026-10-16T10:00:02.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "replayed a1"}]},
         "uuid": "a1", "parentUuid": "u1"},
    ]
    s = start(resume=True, fake_args=["--replay", "2"], transcript_seed=seed)
    assert _wait_for(lambda: len(s.out()) >= 1)
    assert _wait_for(lambda: len(s.tx()) >= 5)
    time.sleep(0.3)
    out = s.out()
    # The replay's thoughts, tool rows and usage reached nothing in out.jsonl
    # — the usage only as the init row's context window.
    assert [r["type"] for r in out] == ["system"], out
    assert out[0]["subtype"] == "init" and out[0]["session_id"] == s.sid
    assert out[0]["context_window"] == 200000
    tx = s.tx()
    assert not any(r["type"] == "assistant" and any(
        b.get("type") in ("tool_use", "thinking") for b in r["message"]["content"])
        for r in tx)
    assert len([r for r in tx if r["type"] == "system"
                and r["subtype"] == "task_notification"]) == 1
    users = [r["message"]["content"][0]["text"] for r in tx if r["type"] == "user"]
    assert users == ["replayed q1", "replayed q2"]
    assert tx[-1]["type"] == "assistant"
    assert tx[-1]["message"]["content"][0]["text"] == "replayed a2"
    assert tx[-2]["message"]["content"][0]["text"] == "replayed q2"
    assert tx[-2]["entrypoint"] == "sdk-cli" and tx[-2]["sessionId"] == s.sid
    assert "session/load" in [e.get("msg", {}).get("method") for e in s.fake()]
    assert "session/new" not in [e.get("msg", {}).get("method") for e in s.fake()]
    # A prompt after the resume still works and still hits out.jsonl.
    s.say("more")
    s.wait_results(1)
    # A resume owes no D18 notice, live or in the transcript.
    assert not any(r.get("subtype") == "task_notification" for r in s.out())
    assert [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"] == \
        ["replayed q1", "replayed q2", "more"]
    # The backfilled transcript is one the tasks readers parse, and closed.
    cwd, _ts, prompt, _pane, entrypoint, settled = tasks_store._parse_head(s.transcript)
    assert (prompt, entrypoint, settled) == ("replayed q1", "sdk-cli", True)
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_the_backfill_diffs_prompts_by_text_not_by_count(start):
    """D3: the transcript holds a fused prompt Copilot never recorded (a
    refused one) — by count the two sides look level and the terminal turn
    q2 would be lost; by text it is the one missing, and it alone is added."""
    seed = [
        {"type": "user", "timestamp": "2026-10-16T10:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
         "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
        {"type": "assistant", "timestamp": "2026-10-16T10:00:02.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
         "uuid": "a1", "parentUuid": "u1"},
        {"type": "user", "timestamp": "2026-10-16T10:00:03.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "refused"}]},
         "uuid": "u2", "parentUuid": "a1", "entrypoint": "sdk-cli"},
        {"type": "assistant", "isApiErrorMessage": True,
         "timestamp": "2026-10-16T10:00:04.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "no"}]},
         "uuid": "a2", "parentUuid": "u2"},
    ]
    s = start(resume=True, fake_args=["--replay", "2"], transcript_seed=seed)
    assert _wait_for(lambda: len(s.tx()) >= 6)
    time.sleep(0.3)
    assert [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"] == \
        ["replayed q1", "refused", "replayed q2"]


_PERM_ID = re.compile(r"^\d{6}-\d{3}-[0-9a-f]{6}$")


def _perm_answers(session, rpc_id):
    return [e["msg"] for e in session.fake()
            if e.get("msg", {}).get("id") == rpc_id and "method" not in e["msg"]]


def _perm_rpc_id(session):
    for e in session.fake():
        sent = e.get("sent") or {}
        if sent.get("method") == "session/request_permission":
            return sent["id"]
    return None


def test_a_permission_allow_for_the_session_answers_allow_always(start, agent):
    """§6.6 + D4/D5: the card file is permission_server's exact shape, the
    answer is picked by option KIND (the fake's ids differ from the kinds),
    it is answered once, and a `mode` on the allow flips allow_all on."""
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert set(card) == {"id", "tool", "input", "tool_use_id", "created_at"}
    assert _PERM_ID.match(card["id"])
    assert card["tool"] == "Bash" and card["tool_use_id"] == "call-perm"
    assert card["input"]["command"] == "rm -rf build"
    assert card["input"]["_acp"]["kind"] == "execute"
    assert isinstance(card["created_at"], float)
    # `_permissions` (what the page polls) reads it as a card with no decision.
    perms = agent._permissions(s.run_dir)
    assert len(perms) == 1 and perms[0]["decision"] == "" \
        and perms[0]["tool_use_id"] == "call-perm"
    assert not s.results()
    s.decide(card["id"], decision="allow", scope="session", mode="auto")
    s.wait_results(1)
    rpc_id = _perm_rpc_id(s)
    answers = _perm_answers(s, rpc_id)
    assert answers == [{"jsonrpc": "2.0", "id": rpc_id, "result": {
        "outcome": {"outcome": "selected", "optionId": "o-always"}}}]
    # The spawn's own `off` (prompt mode), then the card's `on`.
    assert s.config_values() == ["off", "on"]
    cfg = [e for e in s.fake()
           if e.get("msg", {}).get("method") == "session/set_config_option"
           and e["msg"]["params"]["value"] == "on"]
    assert cfg[0]["msg"]["params"] == {"sessionId": s.sid, "configId": "allow_all",
                                       "value": "on"}
    answered_at = next(e["t"] for e in s.fake() if e.get("msg") == answers[0])
    assert cfg[0]["t"] > answered_at, "allow_all must follow the RPC reply"
    assert s.results()[0]["is_error"] is False


def test_an_allow_once_answers_allow_once(start):
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="once")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-once"]
    assert s.config_values() == ["off"]   # the spawn's own; no card switch


def test_an_unanswered_card_denies_itself_after_the_timeout(start):
    """permission_server's own fallback: a deny recorded on disk (so the card
    stops reading "waiting"), `reject_once` on the wire, answered once."""
    s = start(fake_args=["--perm"], env={"FUSED_RENDER_PERMISSION_TIMEOUT": "0.5"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    s.wait_results(1)
    res = json.load(open(os.path.join(s.run_dir, "perm", card["id"] + ".res.json")))
    assert res == {"decision": "deny", "reason": "timeout"}
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]


def test_an_interrupt_cancels_the_turn_and_answers_once(start, agent):
    """The Stop button's road: `session/cancel` and the `control_response`
    `_await_control_response` waits for, both at once (M3: as Claude
    answers it); every open card rejected exactly once; the result on the
    cancelled reply, carrying no reply text (M1). The transcript ends on
    Claude Code's own stop marker, which the tasks readers already read as
    a closed turn."""
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    resp = agent._await_control_response(s.run_dir, rid, timeout=10.0,
                                         start_offset=offset)
    assert resp == {"still_queued": []}
    results = s.wait_results(1)
    assert len(results) == 1
    assert results[0]["is_error"] is True
    assert results[0]["stop_reason"] == "cancelled"
    # M1: the reply streamed "Hello world" before the stop; an error
    # result must not carry it, or `_poll` reads the prose as the error.
    assert results[0]["result"] == ""
    rows = s.out()
    kinds = [r["type"] for r in rows]
    answer = kinds.index("control_response")
    assert answer < kinds.index("result")
    assert rows[answer]["response"] == {"request_id": rid, "subtype": "success",
                                        "response": {"still_queued": []}}
    assert rows[-1]["type"] == "result"
    # `control_response` is not a turn row: the run reads closed.
    assert agent._turn_state(s.run_dir) == (False, False)
    assert any(e.get("msg", {}).get("method") == "session/cancel" for e in s.fake())
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]
    res = json.load(open(os.path.join(s.run_dir, "perm", card["id"] + ".res.json")))
    assert res == {"decision": "deny", "reason": "cancelled"}
    assert agent._permissions(s.run_dir)[0]["decision"] == "deny"
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["error"]
    assert p["permissions"][0]["decision"] == "deny"
    tx = s.tx()
    marks = [r for r in tx if r["type"] == "user"
             and tasks_store.is_interrupt_mark(
                 tasks_store.first_text(r["message"]["content"]))]
    assert len(marks) == 1
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    # ...and the host is still up for the next prompt (the session survives);
    # the fake asks again, and this time the card is allowed.
    s.say("again")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    s.decide(s.perm_requests()[1]["id"], decision="allow", scope="once")
    s.wait_results(2)
    assert s.results()[1]["is_error"] is False


def test_an_interrupt_with_no_turn_open_is_answered_at_once(start, agent):
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {"still_queued": []}
    assert not any(e.get("msg", {}).get("method") == "session/cancel" for e in s.fake())


def test_unsupported_control_requests_are_refused_and_mode_is_switched(start, agent):
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    errors = []
    rid = s.control("set_model", model="gpt-5.4-mini")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset, errors=errors) is None
    assert errors == ["unsupported by copilot"]
    for subtype in ("get_task_output", "stop_task"):
        errors = []
        rid = s.control(subtype, task_id="t1")
        assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                             start_offset=offset, errors=errors) is None
        assert errors == ["unsupported by copilot"]
    rid = s.control("set_permission_mode", mode="auto")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    rid = s.control("set_permission_mode", mode="prompt")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    assert s.config_values() == ["off", "on", "off"]   # spawn, auto, prompt


def test_a_json_rpc_error_is_an_api_error_row_and_an_error_result(start, agent):
    """D13: the readers' own failure shape — `isApiErrorMessage` on the
    assistant row in both files, `is_error` on the result."""
    s = start(fake_args=["--error"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True
    assert results[0]["subtype"] == "error_during_execution"
    assert results[0]["result"] == "credits exhausted"
    errs = [r for r in s.out() if r.get("isApiErrorMessage") is True]
    assert len(errs) == 1 and errs[0]["type"] == "assistant"
    assert errs[0]["message"]["content"] == [{"type": "text", "text": "credits exhausted"}]
    assert agent._is_api_error_row(errs[0])
    # The streamed text was still finalized before the failure row.
    assert [r["message"]["content"][0]["text"] for r in s.out()
            if r.get("type") == "assistant"] == ["Hello world", "credits exhausted"]
    with open(s.transcript, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert any('"isApiErrorMessage": true' in ln for ln in lines)
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_a_dead_transport_ends_the_turn_and_the_host(start):
    s = start(fake_args=["--die"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    with open(os.path.join(s.run_dir, "err.log"), "ab") as fh:
        fh.write(b"boom from copilot\n")
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True
    assert results[0]["result"].startswith("copilot exited (code 3)")
    assert "boom from copilot" in results[0]["result"]
    assert s.proc.wait(timeout=10) == 0
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    assert not os.path.exists(os.path.join(s.registry_dir, "%d.json" % cli_pid))


def test_the_idle_reap_ends_the_session(start, agent):
    s = start(env={"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "0.4"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    s.wait_results(1)
    assert s.proc.wait(timeout=10) == 0
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    assert not agent._host_alive(s.run_dir)
    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    assert _wait_for(lambda: not agent._alive(s.run_dir), timeout=5.0)
    assert not os.path.exists(os.path.join(s.registry_dir, "%d.json" % cli_pid))
    assert os.path.exists(os.path.join(s.run_dir, "pid"))   # left for `_alive`
    assert agent._turn_state(s.run_dir) == (False, False)


def test_a_cancelled_marker_before_the_spawn_spawns_nothing(agent, tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir()
    (run_dir / "cancelled").write_text("1")
    req = {"agent": AGENT, "run_dir": str(run_dir), "file": str(tmp_path),
           "cwd": str(tmp_path), "pane": False, "cli_mode": "", "session_id": "",
           "model": "", "effort": "", "extra_read_dirs": [], "new_session_id": "x",
           "argv": [sys.executable, FAKE], "backend": "copilot"}
    proc = subprocess.run([sys.executable, HOST], input=json.dumps(req).encode(),
                          capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert b"cancelled before the CLI spawned" in proc.stderr
    assert not (run_dir / "out.jsonl").exists()
    assert not (run_dir / "pid").exists()


def test_signed_out_is_an_api_error_and_a_clean_exit(start, agent):
    """A signed-out Copilot refuses `session/new` with -32000 "Authentication
    required" (verified live): the readers' failure shape — init, an
    `isApiErrorMessage` row naming it, an error result — then a clean exit
    that leaves no transcript for the tasks scan to list."""
    s = start(fake_args=["--signed-out"], messages=["hi"])
    _assert_failed_start(s, agent, "Authentication required")
    assert "session/new" in s.methods_seen()
    assert "session/prompt" not in s.methods_seen()


def test_a_session_id_flag_beside_acp_fails_loudly(start, agent):
    """`--acp` refuses `--session-id` (verified live: exit 2, a usage error
    on stderr). An argv that still carries it must not read as a session
    that is merely slow: init, then an error result quoting the CLI's own
    stderr and exit code, then the host is gone."""
    s = start(fake_args=["--session-id", str(uuid.uuid4())], messages=["hi"])
    _assert_failed_start(s, agent, "'--acp' cannot be used with '--session-id <id>'")
    assert "(code 2)" in s.results()[0]["result"]
    assert s.fake() == []   # it never read a byte


def _assert_failed_start(s, agent, needle):
    """A session that never came up: the readers' failure shape — init
    first (no id: none was ever named), the `isApiErrorMessage` row, an error
    result — a clean host exit, and nothing left behind that lists it."""
    assert s.proc.wait(timeout=15) == 0
    rows = s.out()
    assert [r["type"] for r in rows] == ["system", "assistant", "result"], rows
    assert rows[0]["subtype"] == "init" and rows[0]["session_id"] == ""
    assert rows[1]["isApiErrorMessage"] is True
    assert agent._is_api_error_row(rows[1])
    assert needle in rows[1]["message"]["content"][0]["text"]
    assert rows[2]["is_error"] is True and needle in rows[2]["result"]
    assert agent._turn_state(s.run_dir) == (False, False)
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and needle in p["error"]
    assert not os.path.exists(s.bucket)   # no transcript at all
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    assert not os.path.isdir(s.registry_dir) or not os.listdir(s.registry_dir)
    # M7: the opening message is retired, not left queued on a dead run.
    assert s.inbox_pending() == []
    assert len(os.listdir(os.path.join(s.run_dir, "inbox", "done"))) == 1


def test_auto_mode_turns_allow_all_on_before_the_first_prompt(start):
    """D5: `auto` is not argv — the host sets ACP's `agent` mode and
    `allow_all=on` after the handshake, answered before the prompt goes."""
    s = start(cli_mode="auto")
    s.say("go")
    s.wait_results(1)
    methods = s.methods_seen()
    assert s.config_values() == ["on"]
    mode = [e["msg"]["params"] for e in s.fake()
            if e.get("msg", {}).get("method") == "session/set_mode"]
    assert mode == [{"sessionId": s.sid, "modeId":
                     "https://agentclientprotocol.com/protocol/session-modes#agent"}]
    assert methods.index("session/set_mode") < methods.index("session/prompt")
    assert methods.index("session/set_config_option") < methods.index("session/prompt")


def test_late_slash_commands_ride_a_fresh_init_row(start, agent):
    """`available_commands_update` after the `session/new` reply: a second
    init row carries the list (it is turn preamble, so the turn state does
    not move), and the turn still closes."""
    s = start(fake_args=["--late-commands"])
    assert _wait_for(lambda: any(r.get("slash_commands") for r in s.out()))
    inits = [r for r in s.out() if r.get("subtype") == "init"]
    assert inits[0]["session_id"] == s.sid
    assert inits[-1]["slash_commands"] == ["help", "model"]
    s.say("hi")
    s.wait_results(1)
    rows = s.out()
    first_echo = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    assert all(r.get("subtype") == "init" for r in rows[:first_echo])
    assert agent._turn_state(s.run_dir) == (False, False)


def test_a_command_list_after_a_result_waits_for_the_next_echo(start, agent):
    """The road `_refresh_commands` guards: an init row after a `result`
    would reopen the closed turn for `_turn_state`, so the new list waits
    and rides an init row written just ahead of the next echo."""
    s = start(fake_args=["--commands-after-reply"])
    s.say("one")
    s.wait_results(1)
    assert _wait_for(lambda: any(e.get("sent", {}).get("params", {}).get(
        "update", {}).get("availableCommands", [{}])[-1].get("name") == "after"
        for e in s.fake()))
    time.sleep(0.3)
    assert s.out()[-1]["type"] == "result"
    assert agent._turn_state(s.run_dir) == (False, False)
    s.say("two")
    s.wait_results(2)
    rows = s.out()
    second_echo = [i for i, r in enumerate(rows) if agent._starts_new_turn(r)][1]
    assert rows[second_echo - 1]["subtype"] == "init"
    assert rows[second_echo - 1]["slash_commands"] == ["help", "model", "after"]


def test_thinking_streams_as_thinking_deltas(start, agent):
    s = start(fake_args=["--think"])
    s.say("hi")
    s.wait_results(1)
    rows = s.out()
    turn_rows = rows[next(i for i, r in enumerate(rows) if agent._starts_new_turn(r)):]
    kinds = [sg["kind"] for sg in _reply(agent._segments_from_rows(turn_rows))]
    assert kinds[0] == "thinking" and "text" in kinds
    final = [r for r in rows if r.get("type") == "assistant"][0]
    assert final["message"]["content"][0] == {"type": "thinking",
                                              "thinking": "Let me think"}


def test_two_open_cards_are_each_answered_once_on_an_interrupt(start, agent):
    """§6.6 with two cards up at once: the cancel rejects both, once each,
    and nothing answers either a second time on the way out."""
    s = start(fake_args=["--perms", "2"],
              env={"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "0.3"})
    s.say("clean")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=10.0,
                                         start_offset=offset) == {"still_queued": []}
    assert s.proc.wait(timeout=15) == 0   # reaped: every teardown road ran
    asked = [e["sent"]["id"] for e in s.fake()
             if (e.get("sent") or {}).get("method") == "session/request_permission"]
    assert len(asked) == 2
    for perm_rpc in asked:
        answers = _perm_answers(s, perm_rpc)
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]
    assert len(s.results()) == 1


def test_the_cli_gets_copilots_env_not_claudes(start):
    """`_spawn_env(backend="copilot")`: Claude's switches stripped (no
    checkpointing flag, no shell prefix), the login token passed through."""
    s = start(env={"CLAUDE_CODE_SHELL_PREFIX": "/x/prefix.sh"})
    # The fake dumps before it reads a byte, so the init row means it is there.
    assert _wait_for(lambda: len(s.out()) >= 1)
    env = json.load(open(s.env_dump))
    assert not [k for k in env if k.startswith("CLAUDE_CODE_")]
    assert env["COPILOT_GITHUB_TOKEN"] == "test-token"


def test_a_stop_that_kills_the_transport_reads_as_a_stop(start, agent):
    """M2: `_cancel`'s tree-kill (or a respawning `_send`) drops `cancelled`
    and then kills the CLI. That death is the user's Stop: the transcript
    ends on the interrupt mark, not an API-error row, and out.jsonl still
    gets its one result."""
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "never")])
    s.say("go")
    assert _wait_for(lambda: s.prompts_seen())
    # `_cancel`'s order: the marker DURING the turn, then the tree-kill.
    open(os.path.join(s.run_dir, "cancelled"), "w").close()
    os.kill(int(open(os.path.join(s.run_dir, "pid")).read()), signal.SIGKILL)
    results = s.wait_results(1)
    assert results[0]["is_error"] is True and results[0]["stop_reason"] == "cancelled"
    assert results[0]["result"] == ""
    assert not any(r.get("isApiErrorMessage") for r in s.out())
    tx = s.tx()
    assert not any(r.get("isApiErrorMessage") for r in tx)
    assert tasks_store.is_interrupt_mark(tasks_store.first_text(
        [r for r in tx if r["type"] == "user"][-1]["message"]["content"]))
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_a_cancel_copilot_never_answers_is_settled_by_the_host(start, agent):
    """M3: the interrupt is answered at once, and a prompt that ignores the
    cancel is settled by the host after the grace (result `cancelled`, the
    interrupt mark) and the CLI killed — never left open for `_cancel`'s
    5 s wait to tree-kill."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--ignore-cancel"],
              env={"FUSED_COPILOT_HOST_CANCEL_GRACE_SECONDS": "0.6"})
    s.say("go")
    assert _wait_for(lambda: s.prompts_seen())
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    asked = time.monotonic()
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {"still_queued": []}
    assert time.monotonic() - asked < 2.0, "answered at once, not after the grace"
    assert not s.results()
    results = s.wait_results(1, timeout=10)
    assert len(results) == 1 and results[0]["stop_reason"] == "cancelled"
    assert s.proc.wait(timeout=15) == 0
    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    assert _wait_for(lambda: not agent._pid_alive(str(cli_pid)), timeout=5)
    assert tasks_store.is_interrupt_mark(tasks_store.first_text(
        [r for r in s.tx() if r["type"] == "user"][-1]["message"]["content"]))
    assert len(s.results()) == 1


def test_a_card_raised_while_cancelling_is_refused_at_once(start, agent):
    """L6: Copilot asks once more while unwinding a cancel — answered
    `reject_once` at once, no card ever written for the page to draw."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--perm-after-cancel"])
    s.say("go")
    assert _wait_for(lambda: s.prompts_seen())
    s.control("interrupt")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "cancelled"
    asked = [e["sent"]["id"] for e in s.fake()
             if (e.get("sent") or {}).get("method") == "session/request_permission"]
    assert len(asked) == 1
    answers = _perm_answers(s, asked[0])
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]
    assert s.perm_requests() == []


def test_lone_surrogates_survive_both_directions(start, agent):
    """M4: half an emoji in the prompt and in a streamed chunk. Every line
    is ASCII-escaped JSON, so nothing raises and every reader parses it."""
    s = start(fake_args=["--surrogate"])
    s.say("hi \ud83d")
    results = s.wait_results(1)
    assert results[0]["is_error"] is False
    assert results[0]["result"] == "Hello\ud83d world!"
    echo = next(r for r in s.out() if agent._starts_new_turn(r))
    assert echo["message"]["content"][0]["text"] == "hi \ud83d"
    sent = s.prompts_seen()[0]["msg"]["params"]["prompt"][0]["text"]
    assert sent == "hi \ud83d"
    for path in (os.path.join(s.run_dir, "out.jsonl"), s.transcript):
        with open(path, "rb") as fh:
            fh.read().decode("ascii")   # escaped, never raw
    assert tasks_store._parse_head(s.transcript)[2] == "hi \ud83d"
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["text"] == "Hello\ud83d world!"


def test_accept_edits_on_an_edit_card_is_that_cards_allow_always(start):
    """M8 (D5 narrowed): "allow, accept edits" on an EDIT card answers
    allow_always on that card and never turns allow_all on — only `auto`
    does. The card is Copilot's usual edit shape: no diff in `rawInput`,
    the change as a `content` diff entry (M2), and the card shows it."""
    s = start(fake_args=["--perm-edit", "there.txt"])
    with open(os.path.join(s.cwd, "there.txt"), "w") as fh:
        fh.write("one\ntwo\nthree\n")
    s.say("change it")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert card["tool"] == "Edit" and card["input"]["file_path"] == "there.txt"
    assert "-two\n+2\n" in card["input"]["diff"]
    s.decide(card["id"], decision="allow", scope="once", mode="acceptEdits")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-always"]
    assert s.config_values() == ["off"]   # the spawn's own, nothing after


def test_accept_edits_on_a_shell_card_is_allow_once(start):
    """L3: acceptEdits widens EDITS only. On a Bash card it is a plain
    allow_once, even if the card said "for the session"."""
    s = start(fake_args=["--perm"])
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    assert s.perm_requests()[0]["tool"] == "Bash"
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="session",
             mode="acceptEdits")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-once"]
    assert s.config_values() == ["off"]


def test_an_old_cancelled_marker_does_not_turn_a_crash_into_a_stop(start):
    """L5: `cancelled` outlives the Stop that wrote it. One from before this
    prompt began means this death is a crash: the API-error row, not the
    interrupt mark."""
    s = start(fake_args=["--die"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    marker = os.path.join(s.run_dir, "cancelled")
    open(marker, "w").close()
    old = time.time() - 60
    os.utime(marker, (old, old))
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "error"
    assert results[0]["result"].startswith("copilot exited (code 3)")
    assert any(r.get("isApiErrorMessage") for r in s.tx())
    assert not any(tasks_store.is_interrupt_mark(tasks_store.first_text(
        r["message"]["content"])) for r in s.tx() if r["type"] == "user")


def test_a_resume_writes_to_the_transcript_it_already_has(start):
    """M9: a session whose transcript sits in another bucket (its folder was
    moved) is continued THERE, never split into a second file under the
    new cwd's bucket."""
    seed = [{"type": "user", "timestamp": "2026-10-16T10:00:01.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
             "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
            {"type": "assistant", "timestamp": "2026-10-16T10:00:02.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
             "uuid": "a1", "parentUuid": "u1"}]
    s = start(resume=True, transcript_seed=seed, seed_bucket="-old-place")
    s.say("more")
    s.wait_results(1)
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["replayed q1", "more"]
    assert not os.path.exists(s.cwd_bucket)


def test_replayed_prompts_with_no_reply_between_stay_two_turns(start):
    """L10: a replayed prompt whose turn said nothing (a tool, no text) is
    closed by the next non-prompt update — the next prompt is its own."""
    s = start(resume=True, fake_args=["--replay", "1", "--replay-gap"])
    assert _wait_for(lambda: len(s.tx()) >= 4)
    time.sleep(0.2)
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["gap q0", "replayed q1"]


# ------------------------------------------------- the host, in process
#
# The two reap edges a subprocess cannot hit on cue: driven against the same
# fake with the module's own clock knobs shrunk.

def _host_in_process(agent, host_mod, tmp_path, fake_args=()):
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    run_dir = os.path.join(agent.RUNS, "inproc-" + os.urandom(3).hex())
    os.makedirs(os.path.join(run_dir, "inbox"))
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"resumed_from": "", "file": cwd, "backend": "copilot"}, fh)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "session_id": "", "backend": "copilot"}, fh)
    log = os.path.join(run_dir, "fake.log")
    cli = subprocess.Popen([sys.executable, FAKE, "--acp", "--log", log]
                           + list(fake_args), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    req = {"agent": AGENT, "run_dir": run_dir, "file": cwd, "cwd": cwd,
           "pane": False, "cli_mode": None, "session_id": "", "model": "gpt-5.4",
           "effort": "", "extra_read_dirs": [], "new_session_id": "",
           "argv": [], "backend": "copilot"}
    return host_mod._Host(agent, req, cli, host_mod._Peer(cli)), run_dir, log


def test_a_send_landing_as_the_reap_fires_is_still_delivered(
        agent, host_mod, tmp_path, monkeypatch):
    """session_host's reap race: a `_send` writes the inbox after the loop's
    last drain and before it exits, and was already told `sent`. host.json
    goes first, then one more pass runs that turn to its `result` before
    stdin closes (EOF would end the ACP server mid-prompt)."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.setattr(host_mod, "_IDLE_REAP_SECONDS", 0.0)
    monkeypatch.setattr(host_mod, "_DRAIN_INTERVAL_SECONDS", 0.02)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)
    real, calls = host.drain_inbox, []

    def drain():
        held = real()
        calls.append(1)
        if len(calls) == 2:   # the pass the idle timer exits after
            agent._write_inbox_entry(run_dir, "late")
        return held

    host.drain_inbox = drain
    host.run()
    out = _rows(os.path.join(run_dir, "out.jsonl"))
    assert [r["type"] for r in out].count("result") == 1
    prompts = [e["msg"]["params"]["prompt"][0]["text"] for e in _rows(log)
               if e.get("msg", {}).get("method") == "session/prompt"]
    assert prompts == ["late"]
    assert not os.path.exists(os.path.join(run_dir, "host.json"))
    assert host.cli.poll() == 0


def test_a_cli_that_ignores_eof_is_killed(agent, host_mod, tmp_path, monkeypatch):
    """The reap: close stdin → wait → kill."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.setattr(host_mod, "_EXIT_WAIT_SECONDS", 0.3)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path,
                                          ["--ignore-eof"])
    assert host.handshake()
    host.close()
    assert host.cli.returncode == -signal.SIGKILL
    assert any(e.get("eof") for e in _rows(log)), "stdin was closed first"


def test_the_settings_record_precedes_every_file_naming_the_id(
        agent, host_mod, tmp_path, monkeypatch):
    """`_record_settings` runs after `session/new` answers and BEFORE the
    init row, the transcript, the registry row or host.json name the id —
    so no reader finds the session without its record."""
    import agent_roots
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)
    seen = []

    def record(sid, model="", effort="", backend=""):
        seen.append({
            "args": (sid, model, effort, backend),
            "out": _rows(os.path.join(run_dir, "out.jsonl")),
            "transcripts": os.path.exists(agent_roots.projects_dir("copilot")),
            "registry": os.path.exists(host.registry_path),
            "host_sid": json.load(open(os.path.join(run_dir, "host.json")))["session_id"]})

    monkeypatch.setattr(agent, "_record_settings", record)
    try:
        assert host.handshake()
        assert len(seen) == 1
        assert seen[0]["args"] == (host.sid, "gpt-5.4", "", "copilot")
        assert (seen[0]["out"], seen[0]["transcripts"], seen[0]["registry"],
                seen[0]["host_sid"]) == ([], False, False, "")
    finally:
        host.close()


def test_a_settings_record_that_fails_never_fails_the_handshake(
        agent, host_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)

    def boom(*a, **k):
        raise OSError("settings store is read-only")

    monkeypatch.setattr(agent, "_record_settings", boom)
    try:
        assert host.handshake()
        assert _rows(os.path.join(run_dir, "out.jsonl"))[0]["session_id"] == host.sid
    finally:
        host.close()


def test_a_cancel_landing_before_the_card_is_written_raises_no_card(
        agent, host_mod, tmp_path, monkeypatch):
    """M1: a cancel (`reject_pending`) that lands after the request is
    registered but before its card is on disk answers it, and the card is
    then never raised — no `.req.json` for the page to draw, no
    `card_raised` with no `card_cleared` to follow it."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--perm"])
    posts = []
    monkeypatch.setattr(host_mod, "_post_card",
                        lambda run_dir, kind, req_id: posts.append((kind, req_id)))
    real_map = host_mod._map_tool

    def map_then_cancel(*a, **k):
        out = real_map(*a, **k)
        host.reject_pending("cancelled")   # the gap the race lived in
        return out

    monkeypatch.setattr(host_mod, "_map_tool", map_then_cancel)
    try:
        assert host.handshake()
        host.send_prompt("clean")
        assert _wait_for(lambda: not host.turn_open)
        asked = [e["sent"]["id"] for e in _rows(log)
                 if (e.get("sent") or {}).get("method") == "session/request_permission"]
        answers = [e["msg"] for e in _rows(log)
                   if e.get("msg", {}).get("id") == asked[0] and "method" not in e["msg"]]
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]
        perm = os.path.join(run_dir, "perm")
        assert not os.path.isdir(perm) or not os.listdir(perm)
        assert posts == []
    finally:
        host.close()


def test_a_card_parked_after_the_cancels_snapshot_is_still_cleared(
        agent, host_mod, tmp_path, monkeypatch):
    """The other side of the same race (PR #1695): `reject_pending` has
    snapshotted the request while its card was not yet on disk, and the card
    parks before the cancel answers. Forced: the park is held in the tool
    mapping until the cancel's `_answer_perm` is about to run, and that
    waits for the card. The cancel must still find it — a deny on disk and
    a `card_cleared` for the id `card_raised` announced — and Copilot gets
    exactly one reject."""
    import threading
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--perm"])
    posts = []
    monkeypatch.setattr(host_mod, "_post_card",
                        lambda run_dir, kind, req_id: posts.append((kind, req_id)))
    go = threading.Event()
    real_map = host_mod._map_tool

    def held_map(*a, **k):
        assert go.wait(10)
        return real_map(*a, **k)

    monkeypatch.setattr(host_mod, "_map_tool", held_map)
    real_answer = host._answer_perm
    first = []

    def answer_after_the_park(rid, outcome):
        if not first:
            first.append(rid)
            entry = host.pending_perm[rid]
            assert entry["perm_id"] == ""      # snapshotted before the card
            go.set()                            # ...now let it park
            assert _wait_for(lambda: entry["perm_id"], timeout=10)
        return real_answer(rid, outcome)

    host._answer_perm = answer_after_the_park
    try:
        assert host.handshake()
        host.send_prompt("clean")
        assert _wait_for(lambda: host.pending_perm)
        host.reject_pending("cancelled")
        assert _wait_for(lambda: not host.turn_open)
        raised = [i for k, i in posts if k == "card_raised"]
        assert len(raised) == 1
        assert ("card_cleared", raised[0]) in posts
        res = json.load(open(os.path.join(run_dir, "perm", raised[0] + ".res.json")))
        assert res == {"decision": "deny", "reason": "cancelled"}
        asked = [e["sent"]["id"] for e in _rows(log)
                 if (e.get("sent") or {}).get("method") == "session/request_permission"]
        answers = [e["msg"] for e in _rows(log)
                   if e.get("msg", {}).get("id") == asked[0] and "method" not in e["msg"]]
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["o-no"]
    finally:
        go.set()
        host.close()


def test_a_permission_that_throws_is_still_answered_once(
        agent, host_mod, tmp_path, monkeypatch):
    """M5: anything going wrong while a card is built (here the tool
    mapping raises) answers the RPC `cancelled`, once — Copilot is never
    left blocked on a request nobody will answer."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)

    def boom(*a, **k):
        raise RuntimeError("mapping exploded")

    monkeypatch.setattr(host_mod, "_map_tool", boom)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--perm"])
    try:
        assert host.handshake()
        host.send_prompt("clean")

        def answers():
            asked = [e["sent"]["id"] for e in _rows(log)
                     if (e.get("sent") or {}).get("method") == "session/request_permission"]
            return [e["msg"] for e in _rows(log) if asked
                    and e.get("msg", {}).get("id") == asked[0] and "method" not in e["msg"]]

        assert _wait_for(lambda: answers())
        assert _wait_for(lambda: not host.turn_open)
        assert [a["result"] for a in answers()] == [{"outcome": {"outcome": "cancelled"}}]
        assert host.pending_perm == {}
        assert not os.path.isdir(os.path.join(run_dir, "perm")) or \
            not os.listdir(os.path.join(run_dir, "perm"))
    finally:
        host.close()


# ------------------------------------------------- permission_server flag

def _tools(argv):
    s = MCPServer([sys.executable, PERMISSION_SERVER] + [str(a) for a in argv])
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "test"}})
        return s, [t["name"] for t in s.call("tools/list")["result"]["tools"]]
    except Exception:
        s.close()
        raise


def test_no_approve_drops_the_approval_tool(tmp_path):
    """D4: a Copilot run's pane server offers `app_state` (and the terminals)
    but never `approve` — Copilot asks over ACP and the host answers."""
    s, names = _tools([tmp_path / "perm", "--no-approve"])
    try:
        assert names == []
        refused = s.call("tools/call", {"name": "approve", "arguments": {
            "tool_name": "Bash", "input": {}}})
        assert refused["error"]["code"] == -32601
    finally:
        s.close()
    s, names = _tools([tmp_path / "perm", tmp_path / "state", "--no-approve"])
    try:
        assert names == ["app_state"]
    finally:
        s.close()
    s, names = _tools(["--no-approve", tmp_path / "perm", tmp_path / "state"])
    try:
        assert names == ["app_state"], "the flag may sit anywhere in argv"
    finally:
        s.close()
    s, names = _tools([tmp_path / "perm", tmp_path / "state"])
    try:
        assert names == ["approve", "app_state"], "without the flag nothing changes"
    finally:
        s.close()


def test_the_echo_carries_the_inbox_rows_uuid(start, agent):
    """PR #1778: `agent._write_inbox_entry` stamps every entry with a uuid and
    `agent._drained_unechoed` settles an entry by that id on its echo, the way
    Claude Code's own replay does. This host's echo carries it too, so a
    Copilot follow-up lands by identity and never by its words alone."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    assert _wait_for(lambda: s.prompts_seen())
    done = os.path.join(s.run_dir, "inbox", "done")
    assert _wait_for(lambda: os.path.isdir(done) and os.listdir(done))
    entries = [json.load(open(os.path.join(done, n), encoding="utf-8"))
               for n in sorted(os.listdir(done)) if n.endswith(".json")]
    stamped = [e["uuid"] for e in entries if e.get("type") == "user"]
    assert len(stamped) == 1 and len(stamped[0]) == 36
    echo = [r for r in s.out() if agent._starts_new_turn(r)]
    assert [r.get("uuid") for r in echo] == stamped
    s.wait_results(1)
    assert agent._inbox_waiting(s.run_dir) == []
