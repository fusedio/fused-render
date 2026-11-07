"""opencode_host.py: the OpenCode session host, driven end to end against a
fake ACP peer (tests/fake_opencode_acp.py) exactly the way `_start` will
drive the real one — a detached subprocess, a JSON request on stdin, the
inbox written with `agent._write_inbox_entry` / `_write_control_request`,
and every assertion read back off the files the rest of the app reads:
`out.jsonl` through agent.py's own readers (`_turn_state`,
`_starts_new_turn`, `_segments_from_rows`, `_session_from_out`), the
transcript through `tasks_store._parse_head` and
`session_liveness.transcript_turn_open`, the cards through `perm/*.req.json`.

The OpenCode twin of tests/test_gemini_host.py: one test per invariant in
docs/COPILOT_BACKEND.md §6 (1–6) and GEMINI §6 1–12, 14 and 16 (13 and 15
do not apply: there is no `session/set_mode` and no OAuth prompt), plus
what is OpenCode's own (docs/OPENCODE_BACKEND.md): `session/new`'s
`configOptions` as the model's source, the LIVE `set_model` as ONE request,
a mode switch refused as `respawn required`, a `tool_call` whose partial
`rawInput` is merged with its updates by id, the verified permission card
answered by option KIND, and `session/set_config_option` / `session/set_mode`
never on the wire.

Where a check reaches into agent.py for OpenCode's own plumbing (WS-C:
`_SELF_MINTED_ID_BACKENDS`, `_spawn_env`'s branch, `_account_error`'s
words, `_OPENCODE_MODES`), it is skipped until that lands — the host and
its wire are what this file pins.
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
HOST = os.path.join(AGENT_DIR, "opencode_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
PERMISSION_SERVER = os.path.join(AGENT_DIR, "permission_server.py")
FAKE = os.path.abspath(os.path.join("tests", "fake_opencode_acp.py"))
MODEL = "anthropic/claude-sonnet-5-5"
OTHER_MODEL = "openai/gpt-5.5"
NOTICE = "Handled by OpenCode · model " + MODEL
#: What a session may ever send on this wire (D5/D7, §3): never
#: `session/set_mode`, never `session/set_config_option`.
WIRE = {"initialize", "session/new", "session/load", "session/prompt",
        "session/set_model", "session/cancel"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    mod = _load("claude_agent_for_opencode_tests", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def host_mod():
    return _load("opencode_host_under_test", HOST)


def _agent_knows_opencode(agent) -> bool:
    """WS-C's plumbing in agent.py (the fifth self-minted-id backend)."""
    return "opencode" in getattr(agent, "_SELF_MINTED_ID_BACKENDS", ()) \
        and "opencode" in getattr(agent, "BACKENDS", ())


def _opencode_root() -> str:
    """`agent_roots.opencode_dir()` (WS-A), or the host's own fallback
    spelling of the same root until it lands."""
    import agent_roots  # importable once agent.py seeded the shared dir
    fn = getattr(agent_roots, "opencode_dir", None)
    if callable(fn):
        return fn()
    return os.path.join(os.environ["FUSED_RENDER_HOME"], "opencode")


def _wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


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
                 transcript_seed=None, cli_mode="default", messages=(),
                 seed_bucket=None, model=MODEL, mcp=None, fake_model=None):
        import agent_roots
        self.agent = agent
        # A resume names its session; a fresh one learns it off the init row,
        # because OpenCode mints it (`ses_<id>` in `session/new`).
        self._sid = ("ses_" + uuid.uuid4().hex[:26]) if resume else ""
        self.cwd = str(tmp_path / "proj")
        os.makedirs(self.cwd, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        # `_start`'s meta for OpenCode: no `session_id` on a fresh run.
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": self._sid, "file": self.cwd,
                       "backend": "opencode"}, fh)
        if mcp is not None:
            # What `_opencode_argv` leaves for the host when the pane or the
            # terminal tools exist: `_write_mcp_config`'s own shape.
            with open(os.path.join(self.run_dir, "mcp.json"), "w") as fh:
                json.dump({"mcpServers": mcp}, fh)
        projects = os.path.join(_opencode_root(), "projects")
        self.cwd_bucket = os.path.join(projects, agent_roots.munge(self.cwd))
        self.bucket = os.path.join(projects, seed_bucket) if seed_bucket else self.cwd_bucket
        self.registry_dir = os.path.join(_opencode_root(), "sessions")
        if transcript_seed:
            os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
            with open(self.transcript, "w") as fh:
                for row in transcript_seed:
                    fh.write(json.dumps(row) + "\n")
        self.log = os.path.join(self.run_dir, "fake.log")
        self.gate = os.path.join(self.run_dir, "gate")
        self.env_dump = os.path.join(self.run_dir, "fake.env.json")
        for text in messages:
            agent._write_inbox_entry(self.run_dir, text)
        # The real spawn line is `opencode acp` (§3); the model rides the
        # injected config, which the fake reads as `-m` (what its
        # `configOptions[id=model].currentValue` answers).
        fake_model = model if fake_model is None else fake_model
        argv = [sys.executable, FAKE, "acp",
                *(["-m", fake_model] if fake_model else []), "--log", self.log,
                "--check-echo", os.path.join(self.run_dir, "out.jsonl"),
                "--dump-env", self.env_dump] + list(fake_args)
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": cli_mode,
               "session_id": self._sid, "model": model,
               "effort": "", "extra_read_dirs": [],
               "new_session_id": "", "argv": argv, "backend": "opencode"}
        host_env = dict(os.environ)
        host_env.pop("FUSED_RENDER_ORIGIN", None)
        host_env.update({"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "5",
                         "FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS": "0.05"})
        host_env.update(env or {})
        self.err = open(os.path.join(self.run_dir, "host.err.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, HOST], stdin=subprocess.PIPE,
                                     stderr=self.err, env=host_env)
        self.proc.stdin.write(json.dumps(req).encode("utf-8"))
        self.proc.stdin.close()

    # --- what the host wrote
    @property
    def sid(self):
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
        return [e["msg"].get("method") for e in self.fake()
                if "msg" in e and e["msg"].get("method")]

    def model_values(self):
        return [e["msg"]["params"]["modelId"] for e in self.fake()
                if e.get("msg", {}).get("method") == "session/set_model"]

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

    def assert_wire(self):
        """Invariant: nothing but WIRE ever goes to the CLI."""
        assert set(self.methods_seen()) <= WIRE, self.methods_seen()

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
    """§6.1 — and the rest of what the handshake leaves behind: the D18
    notice as the transcript's first row, the registry row, the pid file
    naming the CLI (not the host)."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1 and os.path.exists(s.transcript))
    first = s.out()[0]
    assert first["type"] == "system" and first["subtype"] == "init"
    # OPENCODE'S id, off the `session/new` reply — the request named none.
    assert first["session_id"] == s.sid == s.minted()
    assert s.sid.startswith("ses_")
    assert os.path.basename(s.transcript) == s.minted() + ".jsonl"
    assert first["backend"] == "opencode" and first["model"] == MODEL
    assert first["permissionMode"] == "default"
    assert first["slash_commands"] == ["help", "init"]
    assert first["cwd"] == s.cwd and first["timestamp"].endswith("Z")
    assert agent._session_from_out(s.run_dir) == s.sid

    assert _wait_for(lambda: len(s.tx()) >= 1)
    notice = s.tx()[0]
    assert notice["type"] == "system" and notice["subtype"] == "task_notification"
    assert notice["summary"] == NOTICE
    assert notice["cwd"] == s.cwd and notice["sessionId"] == s.sid

    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    assert cli_pid != s.proc.pid
    host = json.load(open(os.path.join(s.run_dir, "host.json")))
    assert host["pid"] == s.proc.pid and host["backend"] == "opencode"
    assert host["session_id"] == s.sid
    assert agent._host_alive(s.run_dir)
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % cli_pid)))
    assert reg == {"sessionId": s.sid, "cwd": s.cwd, "status": "idle",
                   "updatedAt": reg["updatedAt"], "pid": cli_pid}
    # The transcript and registry live under `opencode_dir()` (D3).
    assert s.transcript.startswith(_opencode_root() + os.sep)
    # The handshake sent nothing but `initialize` + `session/new`: no
    # `session/set_mode` (D5), no `session/set_config_option`, and no
    # `session/set_model` either — the session already runs the model.
    assert s.methods_seen() == ["initialize", "session/new"]


def test_a_fresh_session_is_recorded_as_opencode(start, agent):
    """D2: OpenCode's minted id gets its session-settings record — model,
    backend (no effort: OpenCode has none) — from the host itself."""
    if not _agent_knows_opencode(agent):
        pytest.skip("agent.py does not list opencode yet (WS-C)")
    s = start()
    assert s.sid
    assert agent._session_backend(s.sid) == "opencode"
    assert agent._session_settings(s.sid) == (MODEL, "")


def test_a_spawn_without_a_model_adopts_the_sessions_current_one(start, agent):
    """D7: a request naming no model adopts `configOptions[id=model].
    currentValue` off the `session/new` reply — the init row, the D18
    notice and the settings record name it, never "" — and sends no
    `session/set_model` for it."""
    s = start(model="", fake_model="fused/m1")
    assert s.sid
    assert s.out()[0]["model"] == "fused/m1"
    assert s.tx()[0]["summary"] == "Handled by OpenCode · model fused/m1"
    assert s.model_values() == []
    if _agent_knows_opencode(agent):
        assert agent._session_settings(s.sid) == ("fused/m1", "")


def test_a_spawn_naming_another_model_sets_it_once_before_the_first_prompt(start):
    """D7 at spawn: the model rides the injected config, so normally the
    session already runs it; when the reply says otherwise (a resumed
    session left on another model in a terminal) ONE `session/set_model`
    puts it right, ahead of the first prompt."""
    s = start(model=OTHER_MODEL, fake_model="fused/m1")
    s.say("go")
    s.wait_results(1)
    methods = s.methods_seen()
    assert s.model_values() == [OTHER_MODEL]
    assert methods.index("session/set_model") < methods.index("session/prompt")
    assert s.out()[0]["model"] == OTHER_MODEL
    s.assert_wire()


def test_the_echo_is_written_before_the_prompt_goes_out(start, agent):
    """§6.3 — the fake reads out.jsonl the instant `session/prompt` reaches
    it and finds the echo already there; while it holds the prompt open,
    `_turn_state` reads the turn as open."""
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "gate-x")])
    s.gate = os.path.join(agent.RUNS, "gate-x")
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    assert _wait_for(lambda: s.prompts_seen()
                     and any("echo_seen" in e for e in s.fake()))
    checks = [e for e in s.fake() if "echo_seen" in e]
    assert checks == [{"echo_seen": True, "text": "hi", "t": checks[0]["t"]}]
    rows = s.out()
    echo = [r for r in rows if agent._starts_new_turn(r)]
    assert len(echo) == 1 and echo[0]["message"]["content"][0]["text"] == "hi"
    assert echo[0]["session_id"] == s.sid
    assert not s.results()
    assert agent._turn_state(s.run_dir) == (True, False)
    assert s.inbox_pending() == []
    assert agent._inbox_drained_any(s.run_dir)
    s.open_gate()
    s.wait_results(1)


def test_the_first_poll_of_a_new_session_shows_the_d18_notice(start, agent):
    """D18 LIVE: "Handled by OpenCode · model <id>" is on the first poll of
    a NEW session, mid-turn — written right after the echo. Once only."""
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
    """§6.2, and the whole of the streamed shape `_poll` parses — with
    OpenCode's own noise absorbed: the prompt's `user_message_chunk` echo
    and the per-prompt `available_commands_update` reach no row, the
    `usage_update`'s `cost` rides none, and the reply's `usage` is the
    result's."""
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
    # The reply's `usage {inputTokens, outputTokens, totalTokens}` (§3).
    assert result["usage"]["input_tokens"] == 1234
    assert result["usage"]["output_tokens"] == 7
    assert result["usage"]["cache_read_input_tokens"] == 0
    assert result["num_turns"] == 1
    assert rows[-1]["type"] == "result"
    assert agent._turn_state(s.run_dir) == (False, False)
    # ONE user row (the echo): the CLI's `user_message_chunk` made none.
    assert [r for r in rows if r.get("type") == "user"] == \
        [next(r for r in rows if agent._starts_new_turn(r))]
    # ONE init row: the same command list re-announced is no change.
    assert len([r for r in rows if r.get("subtype") == "init"]) == 1
    assert "cost" not in json.dumps(rows) and "currency" not in json.dumps(rows)
    starts = [r for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "message_start"]
    assert len(starts) == 1
    assert agent._context_usage(starts[0]["event"]["message"]) is None
    assert starts[0]["event"]["message"]["model"] == MODEL
    status = [r for r in rows if r.get("type") == "system"
              and r.get("subtype") == "status"]
    assert status and status[0]["context_window"] == 200000
    assert rows.index(status[0]) < rows.index(result)
    turn_rows = rows[rows.index(next(r for r in rows if agent._starts_new_turn(r))):]
    segments = agent._segments_from_rows(turn_rows)
    assert [(sg["kind"], sg["text"]) for sg in segments] == \
        [("notice", NOTICE), ("text", "Hello world!")]
    finals = [r for r in rows if r.get("type") == "assistant"]
    assert len(finals) == 1
    assert finals[0]["message"]["content"] == [{"type": "text", "text": "Hello world!"}]
    assert finals[0]["message"]["usage"]["input_tokens"] == 1234
    assert finals[0]["message"]["usage"]["output_tokens"] == 7
    assert all(r.get("timestamp", "").endswith("Z") for r in rows)

    tx = s.tx()
    cwd, first_ts, prompt, pane, entrypoint, settled = tasks_store._parse_head(
        s.transcript)
    assert (cwd, prompt, entrypoint, settled) == (s.cwd, "hi", "sdk-cli", True)
    assert first_ts is not None
    user = [r for r in tx if r.get("type") == "user"]
    assert len(user) == 1 and user[0]["isMeta"] is False
    assert user[0]["sessionId"] == s.sid and user[0]["parentUuid"]
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    assert tx[-1]["type"] == "system" and tx[-1]["subtype"] == "turn_duration"
    assert tx[-2]["type"] == "assistant"
    assert tx[-2]["message"]["content"][0]["text"] == "Hello world!"
    assert tx[-2]["parentUuid"] == user[0]["uuid"]
    assert tx[-1]["parentUuid"] == tx[-2]["uuid"]
    cli_pid = int(open(os.path.join(s.run_dir, "pid")).read())
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % cli_pid)))
    assert reg["status"] == "idle"
    p = agent._poll(os.path.basename(s.run_dir))
    assert (p["done"], p["text"], p["session_id"], p["error"]) == \
        (True, "Hello world!", s.sid, "")
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]] == \
        [("notice", NOTICE), ("text", "Hello world!")]
    s.assert_wire()


def test_a_tool_call_becomes_the_claude_tool_rows(start, agent):
    """D14 on the wire, in the VERIFIED order: a `tool_call` whose
    `rawInput` is `{cwd}` alone, an `in_progress` update completing it,
    a `completed` update with `content` + `rawOutput`. ONE tool row, raised
    from the MERGED input (command, description, cwd), never one saying
    "bash"; the result as a `tool_result` user row sharing the id."""
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
    assert block["input"]["_acp"] == {"kind": "execute", "title": "ls -la"}
    assert "bash" not in json.dumps([b["event"]["content_block"] for b in starts])
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
    assert s.results()[0]["num_turns"] == 3
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["error"] == ""
    assert [sg["kind"] for sg in _reply(p["segments"])] == ["text", "tool", "text"]
    tx = s.tx()
    assert [r["type"] for r in tx if r["type"] in ("user", "assistant")] == \
        ["user", "assistant", "assistant", "user", "assistant"]
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_a_complete_first_row_is_raised_at_once_and_only_once(start, agent):
    """D14's other case: a `tool_call` already carrying its command is
    raised on that row, and the `in_progress` update re-sending the same
    input raises no second one."""
    s = start(fake_args=["--tool", "--complete-input"])
    s.say("list")
    s.wait_results(1)
    rows = s.out()
    starts = [r["event"]["content_block"] for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "content_block_start"]
    assert len(starts) == 1 and starts[0]["input"]["command"] == "ls -la"
    results = [r for r in rows if r.get("type") == "user"
               and r["message"]["content"][0]["type"] == "tool_result"]
    assert len(results) == 1
    # `rawOutput.output` alone would have been the result too (no content).
    assert results[0]["message"]["content"][0]["content"] == "a.txt\nb.txt"


def test_a_write_tool_call_maps_to_write_from_its_raw_input(start):
    """D14: an edit of a file that does not exist is a Write of
    `rawInput.content`, the path off `filePath` / `locations`."""
    s = start(fake_args=["--tool", "--tool-kind", "edit"])
    s.say("make it")
    s.wait_results(1)
    blocks = [r["event"]["content_block"] for r in s.out() if r.get("type") == "stream_event"
              and r["event"]["type"] == "content_block_start"]
    assert len(blocks) == 1
    assert blocks[0]["name"] == "Write"
    assert blocks[0]["input"]["file_path"] == "new_file.txt"
    assert blocks[0]["input"]["content"] == "hi"


def test_partial_raw_input_merge_in_process(host_mod, agent, tmp_path, monkeypatch):
    """D14, the merge itself, on a host whose peer is a stub: the partial
    `tool_call` is HELD (no row), the update's `rawInput` merges over it
    key by key — `{cwd}` + `{command, description, cwd}` — and one row
    goes up carrying all three; the `completed` update closes it with
    `rawOutput.output`. A `tool_call` that is complete raises at once."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, _log = _host_in_process(agent, host_mod, tmp_path)
    try:
        assert host.handshake()
        with host.lock:
            host.turn_open = True
            host.turn_started = time.monotonic()
            host.turn_started_at = time.time()
        upd = host._on_notification
        upd("session/update", {"sessionId": host.sid, "update": {
            "sessionUpdate": "tool_call", "toolCallId": "c1", "title": "bash",
            "kind": "execute", "status": "pending", "locations": [{"path": host.cwd}],
            "rawInput": {"cwd": host.cwd}}})
        assert "c1" in host.held and "c1" not in host.tools
        assert not [r for r in _rows(os.path.join(run_dir, "out.jsonl"))
                    if r.get("type") == "assistant"]
        upd("session/update", {"sessionId": host.sid, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "c1",
            "status": "in_progress", "kind": "execute", "title": "echo hi",
            "rawInput": {"command": "echo hi", "description": "say hi", "cwd": host.cwd}}})
        assert "c1" in host.tools and "c1" not in host.held
        assert host.calls["c1"]["rawInput"] == {"command": "echo hi", "description": "say hi",
                                                "cwd": host.cwd}
        name, inp = host.tools["c1"]
        assert (name, inp["command"], inp["description"]) == ("Bash", "echo hi", "say hi")
        upd("session/update", {"sessionId": host.sid, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "c1",
            "status": "completed", "title": "echo hi",
            "rawOutput": {"output": "hi\n", "metadata": {"exit": 0, "truncated": False}}}})
        rows = _rows(os.path.join(run_dir, "out.jsonl"))
        tool_use = [r for r in rows if r.get("type") == "assistant"
                    and r["message"]["content"][0]["type"] == "tool_use"]
        results = [r for r in rows if r.get("type") == "user"]
        assert len(tool_use) == 1 and tool_use[0]["message"]["content"][0]["input"]["command"] == "echo hi"
        assert len(results) == 1 and results[0]["message"]["content"][0]["content"] == "hi\n"
        # A partial call that completes with NO command still gets its row
        # (whatever it carried) ahead of its result — never a result for a
        # row that does not exist.
        upd("session/update", {"sessionId": host.sid, "update": {
            "sessionUpdate": "tool_call", "toolCallId": "c2", "title": "bash",
            "kind": "execute", "status": "pending", "rawInput": {"cwd": host.cwd}}})
        upd("session/update", {"sessionId": host.sid, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "c2",
            "status": "failed", "content": [{"type": "content",
                                             "content": {"type": "text", "text": "nope"}}]}})
        rows = _rows(os.path.join(run_dir, "out.jsonl"))
        kinds = [(r["type"], r["message"]["content"][0]["type"]) for r in rows
                 if r.get("type") in ("assistant", "user")]
        assert kinds[-2:] == [("assistant", "tool_use"), ("user", "tool_result")]
        assert rows[-1]["message"]["content"][0]["is_error"] is True
        with host.lock:
            host.turn_open = False
    finally:
        host.close()


def test_tool_mapping_table(host_mod, tmp_path):
    m = host_mod._map_tool
    # `rawInput` first (D14): the command, the description.
    name, inp = m("execute", "echo probe-hello",
                  {"command": "echo probe-hello", "description": "say hi", "cwd": "/x"}, [], "")
    assert (name, inp["command"], inp["description"]) == ("Bash", "echo probe-hello", "say hi")
    # No rawInput: the title (the command line, once complete) is the command.
    name, inp = m("execute", "git status", None, [], "")
    assert (name, inp["command"], inp["description"]) == ("Bash", "git status", "git status")
    assert inp["_acp"] == {"kind": "execute", "title": "git status"}
    assert m("read", "Read", {"filePath": "a.py"}, [], "")[1]["file_path"] == "a.py"
    assert m("read", "Read", {}, [{"path": "b.py"}], "")[1]["file_path"] == "b.py"
    assert m("fetch", "Fetch", {"url": "https://x"}, [], "") == (
        "WebFetch", {"url": "https://x", "_acp": {"kind": "fetch", "title": "Fetch"}})
    name, inp = m("search", "Search", {"pattern": "foo", "path": "src"}, [], "")
    assert (name, inp["pattern"], inp["path"]) == ("Grep", "foo", "src")
    name, inp = m("edit", "Create", {"filePath": "fresh.txt", "content": "x"}, [], str(tmp_path))
    assert (name, inp["file_path"], inp["content"]) == ("Write", "fresh.txt", "x")
    (tmp_path / "there.txt").write_text("a\nb\nc\n")
    name, inp = m("edit", "Edit", {"filePath": "there.txt", "diff": "d"}, [], str(tmp_path))
    assert (name, inp["file_path"], inp["diff"]) == ("Edit", "there.txt", "d")
    # OpenCode's edit tool: `oldString` / `newString` drawn as one hunk.
    name, inp = m("edit", "Edit", {"filePath": "there.txt", "oldString": "b\n",
                                   "newString": "B\n"}, [], str(tmp_path))
    assert (name, inp["file_path"]) == ("Edit", "there.txt")
    assert "-b\n+B\n" in inp["diff"]
    assert not any(ln.startswith(("--- ", "+++ ")) for ln in inp["diff"].splitlines())
    # ...and the change ONLY in ACP `content` (M2) still draws a diff.
    diff = [{"type": "diff", "path": "there.txt", "oldText": "a\nb\nc\n", "newText": "a\nB\nc\n"}]
    name, inp = m("edit", "Edit", {}, [], str(tmp_path), diff)
    assert (name, inp["file_path"]) == ("Edit", "there.txt") and "-b\n+B\n" in inp["diff"]
    name, inp = m("edit", "Create", {}, [], str(tmp_path),
                  [{"type": "diff", "path": "brand_new.txt", "oldText": None,
                    "newText": "hello\n"}])
    assert (name, inp["file_path"], inp["content"]) == ("Write", "brand_new.txt", "hello\n")
    # fused's own pane tools, however the title spells them.
    assert m("other", "app_state (fused_approvals MCP Server)", None, [], "")[0] == \
        "mcp__fused_approvals__app_state"
    assert m("other", "fused_approvals_app_state", {"reason": "look"}, [], "")[0] == \
        "mcp__fused_approvals__app_state"
    assert m("other", "app_state", {}, [], "")[0] == "mcp__fused_approvals__app_state"
    name, inp = m("other", "terminal_send(id: t1, text: ls -la)", None, [], "")
    assert name == "mcp__fused_approvals__terminal_send"
    assert (inp["id"], inp["text"]) == ("t1", "ls -la")
    # An unmapped kind keeps the call's own input under `_acp.raw`.
    name, inp = m("other", "fused_approvals_app_state", {"reason": "look"}, [], "")
    assert inp["reason"] == "look"
    assert inp["_acp"] == {"kind": "other", "title": "fused_approvals_app_state",
                           "raw": {"reason": "look"}}
    assert m("other", "echo (weather MCP Server)", None, [], "")[0] == "weather-echo"
    assert m("other", "some free title", {}, [], "")[0] == "some free title"
    assert m("other", "github_list_issues", {}, [], "")[0] == "github_list_issues"
    assert m("", "", None, None, "")[0] == "tool"
    for kind in ("execute", "read", "fetch", "search", "edit", "think"):
        acp = m(kind, "T", {}, [], str(tmp_path))[1]["_acp"]
        assert (acp["kind"], acp["title"]) == (kind, "T")
    # The hold rule (D14): what counts as a complete input per kind.
    c = host_mod._input_complete
    assert not c("execute", {"cwd": "/x"}) and c("execute", {"command": "ls"})
    assert not c("edit", {}) and c("edit", {"filePath": "a"})
    assert c("other", {}) and c("", None) and c("think", {})
    # ...and the merge: newer over older, nothing dropped.
    assert host_mod._merge_input({"cwd": "/x"}, {"command": "ls", "cwd": "/y"}) == \
        {"cwd": "/y", "command": "ls"}
    assert host_mod._merge_input(None, {"a": 1}) == {"a": 1}
    # `rawOutput {output, metadata}` → the output alone.
    t = host_mod._tool_output_text
    assert t({"rawOutput": {"output": "hi\n", "metadata": {"exit": 0}}}) == "hi\n"
    assert t({"content": [{"type": "content", "content": {"type": "text", "text": "c"}}],
              "rawOutput": {"output": "r"}}) == "c"
    assert t({"rawOutput": "plain"}) == "plain"


def test_a_second_prompt_waits_for_the_first_result(start, agent):
    """§6.4 / D6: a user row arriving mid-turn is HELD in inbox/ and sent
    only after the result — never a second `session/prompt` while one is
    open."""
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
    agent._poll(os.path.basename(s.run_dir))
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["text"] == "Hello world!"
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]] == \
        [("text", "Hello world!")]


def test_two_inbox_rows_at_once_are_two_sequential_prompts(start, agent):
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
    out.jsonl; turns the transcript does not have are appended; a resume
    writes no fresh D18 notice; the resume's own model is read off the
    load reply's `configOptions`."""
    seed = [
        {"type": "system", "subtype": "task_notification", "cwd": "/x",
         "summary": NOTICE, "timestamp": "2026-10-16T10:00:00.000Z", "sessionId": "old"},
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
    load = next(e["msg"]["params"] for e in s.fake()
                if e.get("msg", {}).get("method") == "session/load")
    assert load == {"sessionId": s.sid, "cwd": s.cwd, "mcpServers": []}
    assert "session/new" not in s.methods_seen()
    s.say("more")
    s.wait_results(1)
    assert not any(r.get("subtype") == "task_notification" for r in s.out())
    assert [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"] == \
        ["replayed q1", "replayed q2", "more"]
    cwd, _ts, prompt, _pane, entrypoint, settled = tasks_store._parse_head(s.transcript)
    assert (prompt, entrypoint, settled) == ("replayed q1", "sdk-cli", True)
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    s.assert_wire()


def test_the_backfill_diffs_prompts_by_text_not_by_count(start):
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


def test_an_unknown_session_on_load_fails_the_handshake(start, agent):
    """D3/§10: `session/load` of an id OpenCode does not have answers
    `-32602 "session not found"` (verified): the readers' failure shape,
    a clean exit, no transcript split off under the new cwd."""
    s = start(resume=True, fake_args=["--unknown-session"], messages=["hi"])
    _assert_failed_start(s, agent, "session not found")
    assert "session/load" in s.methods_seen()
    assert "session/prompt" not in s.methods_seen()
    assert "session/load failed" in s.results()[0]["result"]


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
    """§6.6 + D4: the card file is permission_server's exact shape, built
    from the card's `rawInput {command}` MERGED with the `tool_call` rows
    that announced the call (the description lives there), the answer is
    picked by option KIND (the verified `once`/`always`/`reject` ids), it is
    answered once, and a `mode` on the allow switches NOTHING on the wire
    (D5: a mode is a respawn) — the card's own allow_always is all."""
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert set(card) == {"id", "tool", "input", "tool_use_id", "created_at"}
    assert _PERM_ID.match(card["id"])
    assert card["tool"] == "Bash" and card["tool_use_id"] == "call-perm"
    assert card["input"]["command"] == "rm -rf build"
    assert card["input"]["description"] == "clean the build dir"
    assert card["input"]["_acp"]["kind"] == "execute"
    assert isinstance(card["created_at"], float)
    perms = agent._permissions(s.run_dir)
    assert len(perms) == 1 and perms[0]["decision"] == "" \
        and perms[0]["tool_use_id"] == "call-perm"
    assert not s.results()
    s.decide(card["id"], decision="allow", scope="session", mode="auto")
    s.wait_results(1)
    rpc_id = _perm_rpc_id(s)
    answers = _perm_answers(s, rpc_id)
    assert answers == [{"jsonrpc": "2.0", "id": rpc_id, "result": {
        "outcome": {"outcome": "selected", "optionId": "always"}}}]
    assert s.results()[0]["is_error"] is False
    time.sleep(0.2)
    s.assert_wire()
    # The tool row the announcement raised carries the same merged input.
    blocks = [r["event"]["content_block"] for r in s.out() if r.get("type") == "stream_event"
              and r["event"]["type"] == "content_block_start"]
    assert [b["input"]["command"] for b in blocks] == ["rm -rf build"]


def test_an_allow_once_answers_allow_once(start):
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="once")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["once"]
    s.assert_wire()


def test_a_deny_answers_reject_once(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="deny")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]


def test_a_card_for_a_call_never_announced_still_raises(start):
    """The card is the first word on its call (no `tool_call` rows): the
    card's own `kind`, `title` and `rawInput` carry it."""
    s = start(fake_args=["--perm", "--perm-bare"])
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert card["tool"] == "Bash" and card["input"]["command"] == "rm -rf build"
    assert card["input"]["description"] == "rm -rf build"   # the title
    s.decide(card["id"], decision="allow", scope="once")
    s.wait_results(1)
    assert s.results()[0]["is_error"] is False


def test_an_unanswered_card_denies_itself_after_the_timeout(start):
    s = start(fake_args=["--perm"], env={"FUSED_RENDER_PERMISSION_TIMEOUT": "0.5"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    s.wait_results(1)
    res = json.load(open(os.path.join(s.run_dir, "perm", card["id"] + ".res.json")))
    assert res == {"decision": "deny", "reason": "timeout"}
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]


def test_an_interrupt_cancels_the_turn_and_answers_once(start, agent):
    """The Stop button's road: `session/cancel` and the `control_response`
    `_await_control_response` waits for, both at once; every open card
    rejected exactly once; the result on the cancelled reply, carrying no
    reply text; the transcript ending on Claude Code's own stop marker."""
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
    assert results[0]["result"] == ""
    rows = s.out()
    kinds = [r["type"] for r in rows]
    answer = kinds.index("control_response")
    assert answer < kinds.index("result")
    assert rows[answer]["response"] == {"request_id": rid, "subtype": "success",
                                        "response": {"still_queued": []}}
    assert rows[-1]["type"] == "result"
    assert agent._turn_state(s.run_dir) == (False, False)
    assert any(e.get("msg", {}).get("method") == "session/cancel" for e in s.fake())
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]
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


def test_unsupported_control_requests_are_refused_and_a_mode_switch_needs_a_respawn(
        start, agent):
    """D5: modes are the `permission` block of the config injected at
    spawn, so `set_permission_mode` to another mode is answered
    `error "respawn required"` and nothing reaches the CLI; to the mode the
    session already runs it is a no-op success; an unknown one is refused
    by name. The background-task requests are `unsupported by opencode`."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    for subtype in ("get_task_output", "stop_task"):
        errors = []
        rid = s.control(subtype, task_id="t1")
        assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                             start_offset=offset, errors=errors) is None
        assert errors == ["unsupported by opencode"]
    for mode in ("auto", "acceptEdits"):
        errors = []
        rid = s.control("set_permission_mode", mode=mode)
        assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                             start_offset=offset, errors=errors) is None
        assert errors == ["respawn required"]
    rid = s.control("set_permission_mode", mode="default")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    errors = []
    rid = s.control("set_permission_mode", mode="plan")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset, errors=errors) is None
    assert errors == ["unknown mode 'plan'"]
    s.say("hi")
    s.wait_results(1)
    # The mode never moved, and the wire saw no switch of any kind.
    assert s.out()[0]["permissionMode"] == "default"
    assert "session/set_mode" not in s.methods_seen()
    assert "session/set_config_option" not in s.methods_seen()
    s.assert_wire()


def test_the_spawn_mode_is_recorded_and_sent_nowhere(start):
    """D5: each CLI-wire mode is the init row's `permissionMode`, and no
    request carries it (the injected config did, before the host ran)."""
    for cli_mode in ("auto", "acceptEdits", "default"):
        s = start(cli_mode=cli_mode)
        s.say("go")
        s.wait_results(1)
        assert s.out()[0]["permissionMode"] == cli_mode
        assert s.methods_seen() == ["initialize", "session/new", "session/prompt"]


def test_a_model_change_is_live_and_one_request(start, agent):
    """D7 / invariant 14: `set_model` becomes ONE `session/set_model`
    (`provider/model` id), answered after OpenCode's reply, and every later
    row — message_start, the assistant rows, the next init row — names the
    new model; nothing respawns and no `set_config_option` is sent."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("set_model", model=OTHER_MODEL)
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    assert s.model_values() == [OTHER_MODEL]
    sent = [e for e in s.fake() if e.get("msg", {}).get("method") == "session/set_model"]
    assert sent[0]["msg"]["params"] == {"sessionId": s.sid, "modelId": OTHER_MODEL}
    s.say("hi")
    s.wait_results(1)
    finals = [r for r in s.out() if r.get("type") == "assistant"]
    assert finals and all(r["message"]["model"] == OTHER_MODEL for r in finals)
    assert all(r["message"]["model"] == OTHER_MODEL
               for r in s.tx() if r.get("type") == "assistant")
    starts = [r for r in s.out() if r.get("type") == "stream_event"
              and r["event"]["type"] == "message_start"]
    assert starts[-1]["event"]["message"]["model"] == OTHER_MODEL
    # Still ONE request after the turn; the `config_option_update` the CLI
    # sent back moved nothing.
    assert s.model_values() == [OTHER_MODEL]
    assert s.methods_seen().count("session/set_model") == 1
    errors = []
    rid = s.control("set_model", model="")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset, errors=errors) is None
    assert errors == ["no model named"]
    assert s.model_values() == [OTHER_MODEL]
    assert "session/set_config_option" not in s.methods_seen()
    s.assert_wire()
    if _agent_knows_opencode(agent):
        assert agent._session_settings(s.sid)[0] in (MODEL, OTHER_MODEL)


def test_a_json_rpc_error_is_an_api_error_row_and_an_error_result(start, agent):
    """D13: the readers' own failure shape — `isApiErrorMessage` on the
    assistant row in both files, `is_error` on the result."""
    s = start(fake_args=["--error"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True
    assert results[0]["subtype"] == "error_during_execution"
    assert results[0]["result"] == "Rate limit exceeded: too many requests"
    errs = [r for r in s.out() if r.get("isApiErrorMessage") is True]
    assert len(errs) == 1 and errs[0]["type"] == "assistant"
    assert errs[0]["message"]["content"] == [{"type": "text",
                                              "text": "Rate limit exceeded: too many requests"}]
    assert agent._is_api_error_row(errs[0])
    assert [r["message"]["content"][0]["text"] for r in s.out()
            if r.get("type") == "assistant"] == ["Hello world",
                                                 "Rate limit exceeded: too many requests"]
    with open(s.transcript, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert any('"isApiErrorMessage": true' in ln for ln in lines)
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    if _agent_knows_opencode(agent):
        # D13's `limit_error_markers`: "rate limit" / "too many requests".
        assert agent._account_error(results[0]["result"], "opencode") != results[0]["result"]


def test_a_dead_transport_ends_the_turn_and_the_host(start):
    s = start(fake_args=["--die"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    with open(os.path.join(s.run_dir, "err.log"), "ab") as fh:
        fh.write(b"boom from opencode\n")
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True
    assert results[0]["result"].startswith("opencode exited (code 3)")
    assert "boom from opencode" in results[0]["result"]
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
    assert os.path.exists(os.path.join(s.run_dir, "pid"))
    assert agent._turn_state(s.run_dir) == (False, False)


def test_a_cancelled_marker_before_the_spawn_spawns_nothing(agent, tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir()
    (run_dir / "cancelled").write_text("1")
    req = {"agent": AGENT, "run_dir": str(run_dir), "file": str(tmp_path),
           "cwd": str(tmp_path), "pane": False, "cli_mode": "", "session_id": "",
           "model": "", "effort": "", "extra_read_dirs": [], "new_session_id": "x",
           "argv": [sys.executable, FAKE], "backend": "opencode"}
    proc = subprocess.run([sys.executable, HOST], input=json.dumps(req).encode(),
                          capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert b"cancelled before the CLI spawned" in proc.stderr
    assert not (run_dir / "out.jsonl").exists()
    assert not (run_dir / "pid").exists()


def _assert_failed_start(s, agent, needle):
    """A session that never came up: the readers' failure shape — init
    first, the `isApiErrorMessage` row, an error result — a clean host
    exit, and nothing left behind that lists it."""
    assert s.proc.wait(timeout=15) == 0
    rows = s.out()
    assert [r["type"] for r in rows] == ["system", "assistant", "result"], rows
    assert rows[0]["subtype"] == "init"
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
    assert s.inbox_pending() == []
    assert len(os.listdir(os.path.join(s.run_dir, "inbox", "done"))) == 1


def test_the_pane_mcp_server_rides_session_new(start, agent):
    """D8: the `mcp.json` `_opencode_argv` wrote (the pane server, with
    `--no-approve`) reaches OpenCode as `session/new`'s own `mcpServers`
    list — ACP's stdio shape — and a run without one sends `[]`."""
    s = start()
    assert s.sid
    new = next(e["msg"]["params"] for e in s.fake()
               if e.get("msg", {}).get("method") == "session/new")
    assert new == {"cwd": s.cwd, "mcpServers": []}
    s2 = start(mcp={"fused_approvals": {
        "command": sys.executable,
        "args": ["permission_server.py", "/perm", "--no-approve"],
        "env": {"PYTHONUTF8": "1", "FUSED_RENDER_TERMINAL_ORIGIN": "http://x"},
        "timeout": 1000}})
    assert s2.sid
    new = next(e["msg"]["params"] for e in s2.fake()
               if e.get("msg", {}).get("method") == "session/new")
    assert new["mcpServers"] == [{
        "name": "fused_approvals", "command": sys.executable,
        "args": ["permission_server.py", "/perm", "--no-approve"],
        "env": [{"name": "PYTHONUTF8", "value": "1"},
                {"name": "FUSED_RENDER_TERMINAL_ORIGIN", "value": "http://x"}]}]


def test_late_slash_commands_ride_a_fresh_init_row(start, agent):
    s = start(fake_args=["--late-commands"])
    assert _wait_for(lambda: any(r.get("slash_commands") for r in s.out()))
    inits = [r for r in s.out() if r.get("subtype") == "init"]
    assert inits[0]["session_id"] == s.sid
    assert inits[-1]["slash_commands"] == ["help", "init"]
    s.say("hi")
    s.wait_results(1)
    rows = s.out()
    first_echo = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    assert all(r.get("subtype") == "init" for r in rows[:first_echo])
    assert agent._turn_state(s.run_dir) == (False, False)


def test_a_command_list_after_a_result_waits_for_the_next_echo(start, agent):
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
    assert rows[second_echo - 1]["slash_commands"] == ["help", "init", "after"]


def test_two_open_cards_are_each_answered_once_on_an_interrupt(start, agent):
    s = start(fake_args=["--perms", "2"],
              env={"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "0.3"})
    s.say("clean")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=10.0,
                                         start_offset=offset) == {"still_queued": []}
    assert s.proc.wait(timeout=15) == 0
    asked = [e["sent"]["id"] for e in s.fake()
             if (e.get("sent") or {}).get("method") == "session/request_permission"]
    assert len(asked) == 2
    for perm_rpc in asked:
        answers = _perm_answers(s, perm_rpc)
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]
    assert len(s.results()) == 1


def test_the_cli_gets_opencodes_env(start, agent):
    """§3 / D19: the launch contract passes the environment through —
    `OPENCODE_CONFIG_CONTENT` (the injected config) and
    `OPENCODE_DISABLE_AUTOUPDATE=1`, which `agent._opencode_argv` exported
    for the spawn, reach the CLI as found; `OPENCODE_CONFIG_DIR` is left
    alone; Claude's switches are stripped once agent.py's `_spawn_env` has
    its opencode branch (WS-C)."""
    config = json.dumps({"model": MODEL, "permission": {"bash": "ask"}})
    s = start(env={"CLAUDE_CODE_SHELL_PREFIX": "/x/prefix.sh",
                   "OPENCODE_CONFIG_CONTENT": config,
                   "OPENCODE_DISABLE_AUTOUPDATE": "1",
                   "OPENCODE_CONFIG_DIR": "/somewhere/opencode-config"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    env = json.load(open(s.env_dump))
    assert env["OPENCODE_CONFIG_CONTENT"] == config
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert env["OPENCODE_CONFIG_DIR"] == "/somewhere/opencode-config"
    assert "NO_BROWSER" not in env or env.get("NO_BROWSER") == os.environ.get("NO_BROWSER")
    assert "COPILOT_GITHUB_TOKEN" not in env
    if _agent_knows_opencode(agent):
        assert not [k for k in env if k.startswith("CLAUDE_CODE_")]


def test_a_stop_that_kills_the_transport_reads_as_a_stop(start, agent):
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "never")])
    s.say("go")
    assert _wait_for(lambda: s.prompts_seen())
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


def test_a_cancel_opencode_never_answers_is_settled_by_the_host(start, agent):
    """The interrupt is answered at once, and a prompt that ignores the
    cancel is settled by the host after `FUSED_OPENCODE_HOST_CANCEL_GRACE_
    SECONDS` (result `cancelled`, the interrupt mark) and the CLI killed."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--ignore-cancel"],
              env={"FUSED_OPENCODE_HOST_CANCEL_GRACE_SECONDS": "0.6"})
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
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--perm-after-cancel", "--perm-bare"])
    s.say("go")
    assert _wait_for(lambda: s.prompts_seen())
    s.control("interrupt")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "cancelled"
    asked = [e["sent"]["id"] for e in s.fake()
             if (e.get("sent") or {}).get("method") == "session/request_permission"]
    assert len(asked) == 1
    answers = _perm_answers(s, asked[0])
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]
    assert s.perm_requests() == []


def test_lone_surrogates_survive_both_directions(start, agent):
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
            fh.read().decode("ascii")
    assert tasks_store._parse_head(s.transcript)[2] == "hi \ud83d"
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["text"] == "Hello\ud83d world!"


def test_accept_edits_on_an_edit_card_is_that_cards_allow_always(start):
    """D4/D5 with no live mode switch (Copilot's narrowing): "allow, and
    accept edits" on an EDIT card is that card's allow_always — OpenCode
    stops asking for that tool — and nothing session-wide goes out. The
    card is OpenCode's edit shape: the path in `rawInput.filePath` and
    `locations`, the change as `oldString`/`newString` and as a `diff`
    content entry, and the card shows the diff."""
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
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["always"]
    time.sleep(0.2)
    s.assert_wire()
    assert s.out()[0]["permissionMode"] == "default"


def test_accept_edits_on_a_shell_card_is_allow_once(start):
    """...and on a Bash card it is a plain allow_once, whatever scope the
    card sent: edits widen, shell commands do not."""
    s = start(fake_args=["--perm"])
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    assert s.perm_requests()[0]["tool"] == "Bash"
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="session",
             mode="acceptEdits")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["once"]
    s.assert_wire()


def test_stop_asking_on_a_card_is_its_allow_always_and_no_wire_switch(start):
    """"Allow, and stop asking" (mode `auto`): the widest this card can
    give is its own allow_always; the mode itself is a respawn, so the
    host's mode stays and no `session/set_mode` / `set_config_option` goes
    out."""
    s = start(fake_args=["--perm"])
    s.say("clean")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="once", mode="auto")
    s.wait_results(1)
    answers = _perm_answers(s, _perm_rpc_id(s))
    assert [a["result"]["outcome"]["optionId"] for a in answers] == ["always"]
    # The next prompt still ASKS (the mode never moved: the fake raises a
    # card on every prompt) and the session carries on under it.
    s.say("again")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    s.decide(s.perm_requests()[1]["id"], decision="allow", scope="once")
    s.wait_results(2)
    assert [r["permissionMode"] for r in s.out() if r.get("subtype") == "init"] == ["default"]
    s.assert_wire()


def test_a_replay_that_lands_after_the_load_reply_is_still_backfilled(start):
    s = start(resume=True, fake_args=["--replay", "2", "--replay-after-reply"],
              messages=["more"])
    s.wait_results(1)
    out = s.out()
    assert [r["type"] for r in out if r["type"] != "result"][:1] == ["system"]
    assert not any("replayed" in json.dumps(r) for r in out), out
    users = [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"]
    assert users == ["replayed q1", "replayed q2", "more"]
    agents = [r["message"]["content"][0]["text"] for r in s.tx()
              if r["type"] == "assistant" and r["message"]["content"][0].get("type") == "text"]
    assert agents[:2] == ["replayed a1", "replayed a2"]


def test_an_old_cancelled_marker_does_not_turn_a_crash_into_a_stop(start):
    s = start(fake_args=["--die"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    marker = os.path.join(s.run_dir, "cancelled")
    open(marker, "w").close()
    old = time.time() - 60
    os.utime(marker, (old, old))
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "error"
    assert results[0]["result"].startswith("opencode exited (code 3)")
    assert any(r.get("isApiErrorMessage") for r in s.tx())
    assert not any(tasks_store.is_interrupt_mark(tasks_store.first_text(
        r["message"]["content"])) for r in s.tx() if r["type"] == "user")


def test_a_resume_writes_to_the_transcript_it_already_has(start):
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
    s = start(resume=True, fake_args=["--replay", "1", "--replay-gap"])
    assert _wait_for(lambda: len(s.tx()) >= 4)
    time.sleep(0.2)
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["gap q0", "replayed q1"]


# ------------------------------------------------- the host, in process

def _host_in_process(agent, host_mod, tmp_path, fake_args=()):
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    run_dir = os.path.join(agent.RUNS, "inproc-" + os.urandom(3).hex())
    os.makedirs(os.path.join(run_dir, "inbox"))
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"resumed_from": "", "file": cwd, "backend": "opencode"}, fh)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "session_id": "", "backend": "opencode"}, fh)
    log = os.path.join(run_dir, "fake.log")
    cli = subprocess.Popen([sys.executable, FAKE, "acp", "-m", MODEL, "--log", log]
                           + list(fake_args), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    req = {"agent": AGENT, "run_dir": run_dir, "file": cwd, "cwd": cwd,
           "pane": False, "cli_mode": "default", "session_id": "",
           "model": MODEL, "effort": "", "extra_read_dirs": [],
           "new_session_id": "", "argv": [], "backend": "opencode"}
    return host_mod._Host(agent, req, cli, host_mod._Peer(cli)), run_dir, log


def test_a_send_landing_as_the_reap_fires_is_still_delivered(
        agent, host_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.setattr(host_mod, "_IDLE_REAP_SECONDS", 0.0)
    monkeypatch.setattr(host_mod, "_DRAIN_INTERVAL_SECONDS", 0.02)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)
    real, calls = host.drain_inbox, []

    def drain():
        held = real()
        calls.append(1)
        if len(calls) == 2:
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
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)
    seen = []

    def record(sid, model="", effort="", backend=""):
        seen.append({
            "args": (sid, model, effort, backend),
            "out": _rows(os.path.join(run_dir, "out.jsonl")),
            "transcripts": os.path.exists(os.path.join(_opencode_root(), "projects")),
            "registry": os.path.exists(host.registry_path),
            "host_sid": json.load(open(os.path.join(run_dir, "host.json")))["session_id"]})

    monkeypatch.setattr(agent, "_record_settings", record)
    try:
        assert host.handshake()
        assert len(seen) == 1
        assert seen[0]["args"] == (host.sid, MODEL, "", "opencode")
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
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path,
                                          ["--perm", "--perm-bare"])
    posts = []
    monkeypatch.setattr(host_mod, "_post_card",
                        lambda run_dir, kind, req_id: posts.append((kind, req_id)))
    real_map = host_mod._map_tool

    def map_then_cancel(*a, **k):
        out = real_map(*a, **k)
        host.reject_pending("cancelled")
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
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]
        perm = os.path.join(run_dir, "perm")
        assert not os.path.isdir(perm) or not os.listdir(perm)
        assert posts == []
    finally:
        host.close()


def test_a_card_parked_after_the_cancels_snapshot_is_still_cleared(
        agent, host_mod, tmp_path, monkeypatch):
    import threading
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path,
                                          ["--perm", "--perm-bare"])
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
            assert entry["perm_id"] == ""
            go.set()
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
        assert [a["result"]["outcome"]["optionId"] for a in answers] == ["reject"]
    finally:
        go.set()
        host.close()


def test_a_permission_that_throws_is_still_answered_once(
        agent, host_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)

    def boom(*a, **k):
        raise RuntimeError("mapping exploded")

    monkeypatch.setattr(host_mod, "_map_tool", boom)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path,
                                          ["--perm", "--perm-bare"])
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


def test_permission_outcome_by_kind(host_mod):
    """D4, the table: allow + session → allow_always, allow → allow_once,
    everything else → reject_once, by KIND over the verified option list;
    no usable option → cancelled."""
    opts = [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
            {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
            {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]
    o = host_mod._permission_outcome
    sel = lambda oid: {"outcome": {"outcome": "selected", "optionId": oid}}  # noqa: E731
    assert o(opts, {"decision": "allow", "scope": "session"}) == sel("always")
    assert o(opts, {"decision": "allow", "scope": "once"}) == sel("once")
    assert o(opts, {"decision": "allow"}) == sel("once")
    assert o(opts, {"decision": "deny"}) == sel("reject")
    assert o(opts, {"decision": "deny", "reason": "timeout"}) == sel("reject")
    assert o(opts, {}) == sel("reject")
    assert o([], {"decision": "allow"}) == {"outcome": {"outcome": "cancelled"}}
    assert o(None, {"decision": "deny"}) == {"outcome": {"outcome": "cancelled"}}


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
    """D4: an OpenCode run's pane server offers `app_state` (and the
    terminals) but never `approve` — OpenCode asks over ACP and the host
    answers."""
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


def test_the_echo_carries_the_inbox_rows_uuid(start, agent):
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


def test_the_hosts_mode_table_is_agent_pys(host_mod, agent):
    """`_OPENCODE_MODES` (D5): the CLI-wire modes the host accepts are the
    ones `agent._OPENCODE_MODES` maps onto a `permission` block — pinned
    equal once WS-C lands; the host's own set is pinned regardless."""
    assert tuple(host_mod._OPENCODE_MODES) == ("default", "acceptEdits", "auto")
    table = getattr(agent, "_OPENCODE_MODES", None)
    if table is None:
        pytest.skip("agent.py has no _OPENCODE_MODES yet (WS-C)")
    assert set(table) == set(host_mod._OPENCODE_MODES)


def test_the_host_names_no_opencode_home_or_config(host_mod):
    """Invariant 16's OpenCode twin, for this file: the host never names
    OpenCode's own store or config (`~/.opencode`, `.opencode`,
    `OPENCODE_CONFIG_DIR`) — those are `opencode_health`'s; it reaches its
    own root through `opencode_dir()` only (D3, D15, D19)."""
    with open(HOST, encoding="utf-8") as fh:
        src = fh.read()
    assert "~/.opencode" not in src and "'.opencode'" not in src \
        and '".opencode"' not in src
    assert "OPENCODE_CONFIG_DIR" not in src
    assert "opencode_dir" in src
    assert "session/set_config_option" not in src.replace(
        "`session/set_config_option` is never", "").replace(
        "`session/set_config_option`", "")
    assert '"session/set_mode"' not in src
