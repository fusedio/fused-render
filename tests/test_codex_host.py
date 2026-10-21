"""codex_host.py: the Codex session host, driven end to end against a fake
`codex app-server` (tests/fake_codex_app_server.py) exactly the way `_start`
will drive the real one — a detached subprocess, a JSON request on stdin, the
inbox written with `agent._write_inbox_entry` / `_write_control_request`, and
every assertion read back off the files the rest of the app reads:
`out.jsonl` through agent.py's own readers (`_turn_state`, `_starts_new_turn`,
`_segments_from_rows`, `_session_from_out`, `_poll`), the transcript through
`tasks_store._parse_head` and `session_liveness.transcript_turn_open`, the
cards through `perm/*.req.json`.

The twin of tests/test_copilot_host.py. One test per invariant in
docs/COPILOT_BACKEND.md §6 (1–6) and docs/CODEX_BACKEND.md §6 (13–15), plus
the row contract's edges: the D14 item mapping, the held inbox, interrupt →
control_response, permission accept / acceptForSession / decline / timeout,
the card's mode reaching the next `turn/start`, a failed turn, a refused
`turn/start`, both signed-out roads, transport death, the D5 policy at spawn,
the D3 backfill and the idle reap (with its in-process edges).
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

from fused_render import session_liveness, tasks_store

AGENT_DIR = os.path.abspath(os.path.join("fused_render", "claude_agent"))
HOST = os.path.join(AGENT_DIR, "codex_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
FAKE = os.path.abspath(os.path.join("tests", "fake_codex_app_server.py"))
MODEL = "gpt-6-luna"
NOTICE = "Handled by OpenAI Codex · model %s" % MODEL
V2 = {"accept", "acceptForSession", "decline", "cancel"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    mod = _load("claude_agent_for_codex_tests", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def host_mod():
    return _load("codex_host_under_test", HOST)


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
                 transcript_seed=None, cli_mode=None, messages=(),
                 seed_bucket=None):
        import agent_roots  # importable once agent.py seeded the shared dir
        self.agent = agent
        # A resume names its thread; a fresh one learns it off the init row,
        # because Codex mints it (D2).
        self._sid = str(uuid.uuid4()) if resume else ""
        self.cwd = str(tmp_path / "proj")
        os.makedirs(self.cwd, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        # `_start`'s meta for a self-minted id: no `session_id` on a fresh run.
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": self._sid, "file": self.cwd,
                       "backend": "codex"}, fh)
        self.cwd_bucket = os.path.join(agent_roots.projects_dir("codex"),
                                       agent_roots.munge(self.cwd))
        self.bucket = os.path.join(agent_roots.projects_dir("codex"), seed_bucket) \
            if seed_bucket else self.cwd_bucket
        self.registry_dir = agent_roots.sessions_dir("codex")
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
        # The argv `agent._codex_argv` builds starts `codex app-server`; the
        # host runs whatever it is handed, verbatim.
        argv = [sys.executable, FAKE, "app-server", "--log", self.log,
                "--check-echo", os.path.join(self.run_dir, "out.jsonl"),
                "--dump-env", self.env_dump] + list(fake_args)
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": cli_mode,
               "session_id": self._sid, "model": MODEL,
               "effort": "medium", "extra_read_dirs": [],
               "new_session_id": "", "argv": argv, "backend": "codex"}
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
        """The id the fake's `thread/start` reply named."""
        return next(e["sent"]["result"]["thread"]["id"] for e in self.fake()
                    if "thread" in ((e.get("sent") or {}).get("result") or {}))

    def out(self):
        return _rows(os.path.join(self.run_dir, "out.jsonl"))

    def tx(self):
        return _rows(self.transcript)

    def fake(self):
        return _rows(self.log)

    def results(self):
        return [r for r in self.out() if r.get("type") == "result"]

    def sent_by_host(self, method):
        return [e for e in self.fake() if e.get("msg", {}).get("method") == method]

    def turn_starts(self):
        return [e["msg"]["params"] for e in self.sent_by_host("turn/start")]

    def methods_seen(self):
        return [e["msg"].get("method") for e in self.fake() if "msg" in e]

    def overlaps(self):
        return [e for e in self.fake() if e.get("overlap")]

    def approvals(self):
        """`(rpc id, method)` of every approval request the fake raised."""
        return [(e["sent"]["id"], e["sent"]["method"]) for e in self.fake()
                if "requestApproval" in str((e.get("sent") or {}).get("method"))]

    def answers(self, rpc_id):
        return [e["msg"] for e in self.fake()
                if "msg" in e and "method" not in e["msg"]
                and e["msg"].get("id") == rpc_id]

    def decisions(self):
        """Every approval's answers, in order: `[[decision, ...], ...]`."""
        return [[a["result"]["decision"] for a in self.answers(rid)]
                for rid, _m in self.approvals()]

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

    def cli_pid(self):
        return int(open(os.path.join(self.run_dir, "pid")).read())

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
            pids.add(self.cli_pid())
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


def _assert_v2_only(s):
    """Invariant 14 over everything the host ever answered to an approval:
    one answer per request, every one a v2 decision word."""
    for rid, _method in s.approvals():
        answers = s.answers(rid)
        assert len(answers) == 1, (rid, answers)
        assert set(answers[0]["result"]) == {"decision"}
        assert answers[0]["result"]["decision"] in V2


# ------------------------------------------------------------- invariants

def test_first_row_is_init_with_the_session_id(start, agent):
    """§6.1 — and the rest the handshake leaves behind: CODEX'S thread id on
    the init row (uuid7-shaped, off the `thread/start` reply), the D18
    notice as the transcript's first row, the registry row, the pid file
    naming the CLI (not the host), host.json re-written with the id, and the
    session-settings record naming the backend (D2)."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1 and os.path.exists(s.transcript))
    first = s.out()[0]
    assert first["type"] == "system" and first["subtype"] == "init"
    assert first["session_id"] == s.sid == s.minted()
    assert uuid.UUID(s.sid).version == 7
    assert not agent._bad_id(s.sid)
    assert os.path.basename(s.transcript) == s.minted() + ".jsonl"
    assert first["backend"] == "codex" and first["model"] == MODEL
    assert first["slash_commands"] == [] and first["tools"] == []
    assert first["cwd"] == s.cwd and first["timestamp"].endswith("Z")
    assert agent._session_from_out(s.run_dir) == s.sid

    tx = s.tx()
    assert tx[0]["type"] == "system" and tx[0]["subtype"] == "task_notification"
    assert tx[0]["summary"] == NOTICE
    assert tx[0]["cwd"] == s.cwd and tx[0]["sessionId"] == s.sid

    cli_pid = s.cli_pid()
    assert cli_pid != s.proc.pid
    host = json.load(open(os.path.join(s.run_dir, "host.json")))
    assert host["pid"] == s.proc.pid and host["backend"] == "codex"
    assert host["session_id"] == s.sid
    assert agent._host_alive(s.run_dir)
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % cli_pid)))
    assert reg == {"sessionId": s.sid, "cwd": s.cwd, "status": "idle",
                   "updatedAt": reg["updatedAt"], "pid": cli_pid}
    assert agent._session_backend(s.sid) == "codex"
    assert agent._session_settings(s.sid) == (MODEL, "medium")

    # D5 + D8 on `thread/start`: the Ask policy pair as Codex spells it on
    # the thread (`sandbox` STRING), the model, the appended system prompt.
    params = s.sent_by_host("thread/start")[0]["msg"]["params"]
    assert params["cwd"] == s.cwd and params["model"] == MODEL
    assert (params["approvalPolicy"], params["sandbox"]) == ("untrusted", "read-only")
    assert isinstance(params["developerInstructions"], str)
    assert params["developerInstructions"].strip()
    assert "thread/resume" not in s.methods_seen()


def test_initialized_precedes_every_other_request(start):
    """§6.13: `initialize`, its reply, THEN the bare `initialized`
    notification, and only then anything else. (The fake refuses a request
    sent early — stricter than 0.163.0, which was measured answering one.)"""
    s = start()
    s.say("hi")
    s.wait_results(1)
    log = s.fake()
    inbound = [e for e in log if "msg" in e]
    assert inbound[0]["msg"]["method"] == "initialize"
    assert inbound[0]["msg"]["params"]["clientInfo"]["name"] == "fused-render"
    assert inbound[1]["msg"] == {"jsonrpc": "2.0", "method": "initialized"}
    init_reply = next(e for e in log if (e.get("sent") or {}).get("id") == 1
                      and "userAgent" in (e["sent"].get("result") or {}))
    assert inbound[1]["t"] > init_reply["t"]
    assert [m for m in s.methods_seen()][:3] == ["initialize", "initialized",
                                                 "thread/start"]
    assert not [e for e in log if "early" in e]


def test_the_echo_is_written_before_the_turn_starts(start, agent):
    """§6.3 — the fake reads out.jsonl the instant `turn/start` reaches it and
    finds the echo already there; while the turn is held open `_turn_state`
    reads it open."""
    gate = os.path.join(agent.RUNS, "gate-x")
    s = start(fake_args=["--block-until", gate])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    assert _wait_for(lambda: s.turn_starts())
    checks = [e for e in s.fake() if "echo_seen" in e]
    assert checks == [{"echo_seen": True, "text": "hi", "t": checks[0]["t"]}]
    echo = [r for r in s.out() if agent._starts_new_turn(r)]
    assert len(echo) == 1 and echo[0]["message"]["content"][0]["text"] == "hi"
    assert echo[0]["session_id"] == s.sid
    assert not s.results()
    assert agent._turn_state(s.run_dir) == (True, False)
    assert s.inbox_pending() == []
    p = agent._poll(os.path.basename(s.run_dir))
    assert not p["done"]
    assert [(sg["kind"], sg["text"]) for sg in p["segments"]][:1] == [("notice", NOTICE)]
    open(gate, "w").close()
    s.wait_results(1)


def test_every_turn_ends_in_exactly_one_result(start, agent):
    """§6.2, and the whole streamed shape `_poll` parses: deltas, the
    finalized assistant row, usage off `thread/tokenUsage/updated`, the
    context window as a status row INSIDE the turn, the transcript the tasks
    readers parse, and `turn/start` carrying the turn's values (D5, D7)."""
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    results = s.wait_results(1)
    time.sleep(0.3)   # nothing else may follow the result
    rows = s.out()
    assert [r["type"] for r in rows].count("result") == 1
    result = results[0]
    assert result["subtype"] == "success" and result["is_error"] is False
    assert result["stop_reason"] == "end_turn" and result["backend"] == "codex"
    assert result["result"] == "Hello world!"
    assert result["session_id"] == s.sid
    # Codex's `inputTokens` (1234) INCLUDES its cached 100: split the way
    # Claude's usage is, so the meter's sum is 1234 and not 1334.
    assert result["usage"]["input_tokens"] == 1134
    assert result["usage"]["cache_read_input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 7
    assert result["num_turns"] == 1
    assert rows[-1]["type"] == "result"
    assert agent._turn_state(s.run_dir) == (False, False)
    starts = [r for r in rows if r.get("type") == "stream_event"
              and r["event"]["type"] == "message_start"]
    assert len(starts) == 1
    reading = agent._context_usage(starts[0]["event"]["message"])
    assert (reading["input_tokens"], reading["cache_read_input_tokens"],
            reading["model"]) == (1134, 100, MODEL)
    status = [r for r in rows if r.get("subtype") == "status"]
    assert [r["context_window"] for r in status] == [258400]
    echo_at = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    assert echo_at < rows.index(status[0]) < rows.index(result)
    segments = agent._segments_from_rows(rows[echo_at:])
    assert [(sg["kind"], sg["text"]) for sg in segments] == \
        [("notice", NOTICE), ("text", "Hello world!")]
    finals = [r for r in rows if r.get("type") == "assistant"]
    assert len(finals) == 1
    assert finals[0]["message"]["content"] == [{"type": "text", "text": "Hello world!"}]
    assert all(r.get("timestamp", "").endswith("Z") for r in rows)
    # The turn's values, per D5/D7: Ask = untrusted + readOnly, the model and
    # effort the session was spawned with.
    assert s.turn_starts() == [{
        "threadId": s.sid, "input": [{"type": "text", "text": "hi"}],
        "approvalPolicy": "untrusted", "sandboxPolicy": {"type": "readOnly"},
        "model": MODEL, "effort": "medium"}]

    tx = s.tx()
    cwd, first_ts, prompt, pane, entrypoint, settled = tasks_store._parse_head(
        s.transcript)
    assert (cwd, prompt, entrypoint, settled) == (s.cwd, "hi", "sdk-cli", True)
    assert first_ts is not None
    user = [r for r in tx if r.get("type") == "user"]
    assert len(user) == 1 and user[0]["isMeta"] is False and user[0]["parentUuid"]
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    assert tx[-1]["type"] == "system" and tx[-1]["subtype"] == "turn_duration"
    assert tx[-2]["type"] == "assistant"
    assert tx[-2]["message"]["content"][0]["text"] == "Hello world!"
    # The uuid chain: each row's parent is the row before it.
    assert all(b["parentUuid"] == a["uuid"] for a, b in zip(tx, tx[1:]))
    # `thread/name/updated {threadName}` → the transcript's title.
    assert [r["aiTitle"] for r in tx if r.get("type") == "ai-title"] == ["A fake thread"]
    reg = json.load(open(os.path.join(s.registry_dir, "%d.json" % s.cli_pid())))
    assert reg["status"] == "idle"
    p = agent._poll(os.path.basename(s.run_dir))
    assert (p["done"], p["text"], p["session_id"], p["error"]) == \
        (True, "Hello world!", s.sid, "")


def test_tool_items_become_the_claude_tool_rows(start, agent):
    """D14 on the wire, in `auto` (nothing asks): a `commandExecution` is
    Bash, a new-file `fileChange` is Write, `mcpToolCall` is
    `mcp__<server>__<tool>`, `webSearch` is WebSearch — each a
    `content_block_start` + `assistant` tool_use and a `tool_result` user row
    (never a text one: it must not start a turn), the item id shared by all."""
    s = start(cli_mode="auto",
              fake_args=["--tool", "--edit", "made.txt", "--mcp", "--web"])
    s.say("go")
    s.wait_results(1)
    assert s.approvals() == []   # `never`: no card at all
    rows = s.out()
    blocks = [r["event"]["content_block"] for r in rows
              if r.get("type") == "stream_event"
              and r["event"]["type"] == "content_block_start"]
    assert [b["name"] for b in blocks] == [
        "Bash", "Write", "mcp__fused_approvals__app_state", "WebSearch"]
    bash, write, mcp, web = blocks
    assert bash["input"]["command"] == "/bin/bash -lc 'ls -la'"
    assert bash["input"]["description"] == "ls -la"
    assert bash["input"]["_codex"] == {"type": "commandExecution", "itemId": bash["id"]}
    # Codex sends the path absolute; the card reads it cwd-relative.
    assert write["input"]["file_path"] == "made.txt"
    assert write["input"]["content"] == "hi\n"
    assert mcp["input"]["reason"] == "look"
    assert web["input"]["query"] == "fused render"
    results = {r["message"]["content"][0]["tool_use_id"]: r["message"]["content"][0]
               for r in rows if r.get("type") == "user"
               and r["message"]["content"][0]["type"] == "tool_result"}
    assert results[bash["id"]] == {"type": "tool_result", "tool_use_id": bash["id"],
                                   "content": "a.txt\nb.txt", "is_error": False}
    assert results[write["id"]]["content"] == "completed"
    assert results[mcp["id"]]["content"] == "state: ok"
    assert results[web["id"]]["content"] == "Fused — https://fused.io"
    assert not any(r["is_error"] for r in results.values())
    assert not any(agent._starts_new_turn(r) for r in rows
                   if r.get("type") == "user" and r["message"]["content"][0]["type"]
                   == "tool_result")
    echo_at = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    segments = _reply(agent._segments_from_rows(rows[echo_at:]))
    # fused's own pane tool is the one `_segments_from_rows` strips (it is
    # machinery, not work): three tool chips for four tool rows.
    assert [sg["kind"] for sg in segments] == ["text", "tool", "tool", "tool", "text"]
    assert [sg["name"] for sg in segments if sg["kind"] == "tool"] == \
        ["Bash", "Write", "WebSearch"]
    # The first message ended at the first tool, the final one is the reply.
    assert s.results()[0]["result"] == "!"
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_item_mapping_table(host_mod, tmp_path):
    m = host_mod._map_item
    cwd = str(tmp_path)
    name, inp = m({"type": "commandExecution", "id": "c1", "command": "bash -lc 'x | y'",
                   "commandActions": [{"command": "x"}, {"command": "y"}]}, cwd)
    assert (name, inp["command"], inp["description"]) == ("Bash", "bash -lc 'x | y'", "x; y")
    assert m({"type": "commandExecution", "id": "c2", "command": "ls",
              "commandActions": []}, cwd)[1]["description"] == "ls"
    # fileChange: every change an add → Write; any update/delete → Edit.
    add = {"type": "fileChange", "id": "f1", "changes": [
        {"path": os.path.join(cwd, "a.txt"), "kind": {"type": "add"}, "diff": "A\n"}]}
    assert m(add, cwd)[0] == "Write" and m(add, cwd)[1]["content"] == "A\n"
    assert m(add, cwd)[1]["file_path"] == "a.txt"
    upd = {"type": "fileChange", "id": "f2", "changes": [
        {"path": os.path.join(cwd, "b.txt"), "kind": {"type": "update"}, "diff": "@@ x"},
        {"path": "/elsewhere/c.txt", "kind": {"type": "add"}, "diff": "C"}]}
    name, inp = m(upd, cwd)
    assert (name, inp["file_path"]) == ("Edit", "b.txt")
    assert inp["diff"] == "--- b.txt\n@@ x--- /elsewhere/c.txt\nC"
    assert m({"type": "fileChange", "id": "f3", "changes": [
        {"path": "/x/d.txt", "kind": {"type": "delete"}, "diff": ""}]}, cwd)[0] == "Edit"
    # A path outside the cwd stays absolute.
    assert m({"type": "fileChange", "id": "f4", "changes": [
        {"path": "/x/e.txt", "kind": {"type": "update"}, "diff": "d"}]}, cwd)[1][
        "file_path"] == "/x/e.txt"
    name, inp = m({"type": "mcpToolCall", "id": "m1", "server": "github",
                   "tool": "list_issues", "arguments": {"repo": "r"}}, cwd)
    assert (name, inp["repo"]) == ("mcp__github__list_issues", "r")
    assert m({"type": "mcpToolCall", "id": "m2", "server": "s", "tool": "t",
              "arguments": [1]}, cwd)[1]["arguments"] == [1]
    assert m({"type": "webSearch", "id": "w1", "query": "",
              "action": {"type": "search", "query": "q"}}, cwd)[1]["query"] == "q"
    for kind in ("plan", "reasoning", "agentMessage", "userMessage", "imageView",
                 "contextCompaction", "functionCallOutput"):
        assert m({"type": kind, "id": "x"}, cwd) == ("", {})
    for item in (add, upd):
        assert m(item, cwd)[1]["_codex"] == {"type": "fileChange", "itemId": item["id"]}
    r = host_mod._item_result
    assert r({"type": "commandExecution", "status": "completed", "exitCode": 2,
              "aggregatedOutput": "boom"}) == ("boom", True)
    assert r({"type": "commandExecution", "status": "completed", "exitCode": 0,
              "aggregatedOutput": None}, "streamed") == ("streamed", False)
    assert r({"type": "commandExecution", "status": "declined",
              "aggregatedOutput": None}) == ("declined", True)
    assert r({"type": "fileChange", "status": "failed"}) == ("failed", True)
    assert r({"type": "mcpToolCall", "status": "failed",
              "error": {"message": "nope"}}) == ("nope", True)


def test_a_second_prompt_waits_for_the_first_result(start, agent):
    """§6.4 / D6: a user row arriving mid-turn is HELD in inbox/ and sent only
    after the turn's result — never a second `turn/start` while one is open."""
    gate = os.path.join(agent.RUNS, "gate-held")
    s = start(fake_args=["--block-until", gate])
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("one")
    assert _wait_for(lambda: s.turn_starts())
    s.say("two")
    time.sleep(0.4)
    assert len(s.inbox_pending()) == 1, "the follow-up must stay in inbox/"
    assert len(s.turn_starts()) == 1
    open(gate, "w").close()
    s.wait_results(2)
    assert s.inbox_pending() == []
    log = s.fake()
    starts = [e for e in log if e.get("msg", {}).get("method") == "turn/start"]
    completions = [e for e in log
                   if (e.get("sent") or {}).get("method") == "turn/completed"]
    assert len(starts) == 2 and len(completions) == 2
    assert starts[1]["t"] > completions[0]["t"], "second turn sent before the first ended"
    assert [e["msg"]["params"]["input"][0]["text"] for e in starts] == ["one", "two"]
    assert s.overlaps() == []
    rows = s.out()
    kinds = [r["type"] for r in rows]
    second_echo = [i for i, r in enumerate(rows) if agent._starts_new_turn(r)][1]
    assert second_echo > kinds.index("result")
    assert agent._turn_state(s.run_dir) == (False, False)


def test_two_inbox_rows_at_once_are_two_sequential_turns(start, agent):
    s = start(messages=["one", "two"])
    s.wait_results(2)
    assert s.overlaps() == []
    assert [p["input"][0]["text"] for p in s.turn_starts()] == ["one", "two"]
    rows = s.out()
    kinds = [r["type"] for r in rows]
    echoes = [i for i, r in enumerate(rows) if agent._starts_new_turn(r)]
    assert echoes[0] < kinds.index("result") < echoes[1]
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["one", "two"]


def test_a_resume_backfills_the_transcript_and_never_out(start, agent):
    """§6.5 + D3: `thread/resume`'s reply carries the history; turns the
    transcript lacks (taken in a terminal) are appended; nothing of it — nor
    the usage the server sends after the reply — reaches out.jsonl; no fresh
    D18 notice; the next turn's status row carries the window learned
    between turns."""
    seed = [
        {"type": "system", "subtype": "task_notification", "cwd": "/x",
         "summary": NOTICE, "timestamp": "2026-10-21T10:00:00.000Z", "sessionId": "old"},
        {"type": "user", "cwd": "/x", "timestamp": "2026-10-21T10:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
         "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
        {"type": "assistant", "cwd": "/x", "timestamp": "2026-10-21T10:00:02.000Z",
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
    tx = s.tx()
    assert not any(r["type"] == "assistant" and any(
        b.get("type") in ("tool_use", "thinking") for b in r["message"]["content"])
        for r in tx)
    users = [r["message"]["content"][0]["text"] for r in tx if r["type"] == "user"]
    assert users == ["replayed q1", "replayed q2"]
    assert tx[-1]["message"]["content"][0]["text"] == "replayed a2"
    assert tx[-2]["entrypoint"] == "sdk-cli" and tx[-2]["sessionId"] == s.sid
    resume = s.sent_by_host("thread/resume")[0]["msg"]["params"]
    assert resume["threadId"] == s.sid and resume["cwd"] == s.cwd
    assert (resume["approvalPolicy"], resume["sandbox"]) == ("untrusted", "read-only")
    assert resume["developerInstructions"].strip()
    assert "thread/start" not in s.methods_seen()
    s.say("more")
    s.wait_results(1)
    assert not any(r.get("subtype") == "task_notification" for r in s.out())
    # The window the server named after the resume reply is told once: on
    # the init row when it beat it, else as a status row inside this turn —
    # never as a row between turns.
    assert [r["context_window"] for r in s.out()
            if r.get("subtype") in ("init", "status") and r.get("context_window")] \
        == [258400]
    assert [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"] == \
        ["replayed q1", "replayed q2", "more"]
    _cwd, _ts, prompt, _pane, entrypoint, settled = tasks_store._parse_head(s.transcript)
    assert (prompt, entrypoint, settled) == ("replayed q1", "sdk-cli", True)
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


def test_the_backfill_diffs_prompts_by_text_not_by_count(start):
    seed = [
        {"type": "user", "timestamp": "2026-10-21T10:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
         "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
        {"type": "assistant", "timestamp": "2026-10-21T10:00:02.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
         "uuid": "a1", "parentUuid": "u1"},
        {"type": "user", "timestamp": "2026-10-21T10:00:03.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "refused"}]},
         "uuid": "u2", "parentUuid": "a1", "entrypoint": "sdk-cli"},
        {"type": "assistant", "isApiErrorMessage": True,
         "timestamp": "2026-10-21T10:00:04.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "no"}]},
         "uuid": "a2", "parentUuid": "u2"},
    ]
    s = start(resume=True, fake_args=["--replay", "2"], transcript_seed=seed)
    assert _wait_for(lambda: len(s.tx()) >= 6)
    time.sleep(0.3)
    assert [r["message"]["content"][0]["text"] for r in s.tx() if r["type"] == "user"] == \
        ["replayed q1", "refused", "replayed q2"]


def test_a_resume_writes_to_the_transcript_it_already_has(start):
    seed = [{"type": "user", "timestamp": "2026-10-21T10:00:01.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]},
             "uuid": "u1", "parentUuid": None, "entrypoint": "sdk-cli"},
            {"type": "assistant", "timestamp": "2026-10-21T10:00:02.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
             "uuid": "a1", "parentUuid": "u1"}]
    s = start(resume=True, transcript_seed=seed, seed_bucket="-old-place")
    s.say("more")
    s.wait_results(1)
    assert [r["message"]["content"][0]["text"] for r in s.tx()
            if r["type"] == "user"] == ["replayed q1", "more"]
    assert not os.path.exists(s.cwd_bucket)


# ------------------------------------------------------------ permissions

_PERM_ID = re.compile(r"^\d{6}-\d{3}-[0-9a-f]{6}$")


def test_a_command_card_allowed_once_answers_accept(start, agent):
    """§6.6 + §6.14 + D4: the card file is permission_server's exact shape,
    dressed from the item the request names; the answer is `accept`, sent
    once; the item then runs."""
    s = start(fake_args=["--tool"])
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert set(card) == {"id", "tool", "input", "tool_use_id", "created_at"}
    assert _PERM_ID.match(card["id"])
    assert card["tool"] == "Bash" and card["input"]["description"] == "ls -la"
    assert card["input"]["_codex"]["type"] == "commandExecution"
    assert card["tool_use_id"] == card["input"]["_codex"]["itemId"]
    # The card's tool_use_id is the tool row's id already on the page.
    tool_ids = [r["message"]["content"][0]["id"] for r in s.out()
                if r.get("type") == "assistant"
                and r["message"]["content"][0]["type"] == "tool_use"]
    assert card["tool_use_id"] in tool_ids
    perms = agent._permissions(s.run_dir)
    assert len(perms) == 1 and perms[0]["decision"] == ""
    assert not s.results()
    s.decide(card["id"], decision="allow", scope="once")
    s.wait_results(1)
    assert s.approvals()[0][0] == 0, "the real server's first request id is 0"
    assert s.decisions() == [["accept"]]
    _assert_v2_only(s)
    res = [r["message"]["content"][0] for r in s.out() if r.get("type") == "user"
           and r["message"]["content"][0]["type"] == "tool_result"]
    assert res[0]["is_error"] is False and res[0]["content"] == "a.txt\nb.txt"
    assert s.results()[0]["is_error"] is False


def test_allow_for_the_session_answers_accept_for_session(start):
    """D4: scope `session` → `acceptForSession`, so Codex stops asking for
    that command — the next turn runs it with no card."""
    s = start(fake_args=["--tool"])
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="session")
    s.wait_results(1)
    assert s.decisions() == [["acceptForSession"]]
    s.say("again")
    s.wait_results(2)
    assert len(s.approvals()) == 1
    _assert_v2_only(s)


def test_a_denied_card_answers_decline_and_the_tool_reads_failed(start, agent):
    s = start(fake_args=["--tool"])
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="deny")
    s.wait_results(1)
    assert s.decisions() == [["decline"]]
    _assert_v2_only(s)
    res = [r["message"]["content"][0] for r in s.out() if r.get("type") == "user"
           and r["message"]["content"][0]["type"] == "tool_result"]
    assert res == [{"type": "tool_result", "tool_use_id": res[0]["tool_use_id"],
                    "content": "declined", "is_error": True}]
    # `decline` lets the turn carry on to its end.
    assert s.results()[0]["is_error"] is False


def test_an_unanswered_card_declines_itself_after_the_timeout(start):
    s = start(fake_args=["--tool"], env={"FUSED_RENDER_PERMISSION_TIMEOUT": "0.5"})
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    s.wait_results(1)
    res = json.load(open(os.path.join(s.run_dir, "perm", card["id"] + ".res.json")))
    assert res == {"decision": "deny", "reason": "timeout"}
    assert s.decisions() == [["decline"]]


def test_an_edit_card_shows_the_change_and_accept_edits_is_for_the_session(start):
    """D4/D14: the file card's REQUEST carries no change (`{itemId, reason,
    grantRoot}`, verified) — the card is the `item/started` fileChange it
    names: an update is an Edit with Codex's diff. "Allow, and accept edits"
    on it is that card's `acceptForSession`, and the mode is NOT stored:
    the next turn still runs under Ask."""
    s = start(fake_args=["--edit", "there.txt"])
    with open(os.path.join(s.cwd, "there.txt"), "w") as fh:
        fh.write("one\ntwo\nthree\n")
    s.say("change it")
    assert _wait_for(lambda: s.perm_requests())
    card = s.perm_requests()[0]
    assert card["tool"] == "Edit" and card["input"]["file_path"] == "there.txt"
    assert "-two\n+2\n" in card["input"]["diff"]
    assert s.approvals()[0][1] == "item/fileChange/requestApproval"
    s.decide(card["id"], decision="allow", scope="once", mode="acceptEdits")
    s.wait_results(1)
    assert s.decisions() == [["acceptForSession"]]
    s.say("again")
    s.wait_results(2)
    assert [(p["approvalPolicy"], p["sandboxPolicy"]) for p in s.turn_starts()] == \
        [("untrusted", {"type": "readOnly"})] * 2
    _assert_v2_only(s)


def test_accept_edits_on_a_command_card_is_a_plain_accept(start):
    """acceptEdits widens EDITS only: on a command card it is `accept`, even
    when the card said "for the session"."""
    s = start(fake_args=["--tool"])
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="session",
             mode="acceptEdits")
    s.wait_results(1)
    assert s.decisions() == [["accept"]]


def test_allow_and_stop_asking_moves_the_next_turn_to_never(start):
    """D4/D5: mode `auto` on a card → that card's `acceptForSession`, and the
    host's stored mode becomes auto, which the NEXT `turn/start` carries
    (`never` + `workspaceWrite`) — no respawn, the same CLI throughout."""
    s = start(fake_args=["--tool"])
    s.say("list")
    assert _wait_for(lambda: s.perm_requests())
    pid = s.cli_pid()
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="once", mode="auto")
    s.wait_results(1)
    assert s.decisions() == [["acceptForSession"]]
    s.say("again")
    s.wait_results(2)
    assert [(p["approvalPolicy"], p["sandboxPolicy"]) for p in s.turn_starts()] == [
        ("untrusted", {"type": "readOnly"}), ("never", {"type": "workspaceWrite"})]
    assert s.cli_pid() == pid and s.proc.poll() is None
    _assert_v2_only(s)


# ------------------------------------------------------------- interrupt

def test_an_interrupt_stops_the_turn_and_answers_once(start, agent):
    """The Stop button's road (D6): `turn/interrupt {threadId, turnId}` with
    the id the `turn/start` reply named, the `control_response` at once, the
    open card answered `cancel` exactly once (deny on disk), the result
    `cancelled` with no reply text, the transcript's interrupt mark — and the
    session survives for the next turn."""
    s = start(fake_args=["--tool"])
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
    assert results[0]["is_error"] is True and results[0]["stop_reason"] == "cancelled"
    assert results[0]["result"] == ""
    rows = s.out()
    kinds = [r["type"] for r in rows]
    assert kinds.index("control_response") < kinds.index("result")
    assert rows[kinds.index("control_response")]["response"] == {
        "request_id": rid, "subtype": "success", "response": {"still_queued": []}}
    assert rows[-1]["type"] == "result"
    assert agent._turn_state(s.run_dir) == (False, False)
    turn_id = next(e["sent"]["result"]["turn"]["id"] for e in s.fake()
                   if "turn" in ((e.get("sent") or {}).get("result") or {}))
    assert [e["msg"]["params"] for e in s.sent_by_host("turn/interrupt")] == \
        [{"threadId": s.sid, "turnId": turn_id}]
    assert s.decisions() == [["cancel"]]
    res = json.load(open(os.path.join(s.run_dir, "perm", card["id"] + ".res.json")))
    assert res == {"decision": "deny", "reason": "cancelled"}
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and p["error"]
    assert p["permissions"][0]["decision"] == "deny"
    marks = [r for r in s.tx() if r["type"] == "user"
             and tasks_store.is_interrupt_mark(
                 tasks_store.first_text(r["message"]["content"]))]
    assert len(marks) == 1
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    s.say("again")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    s.decide(s.perm_requests()[1]["id"], decision="allow", scope="once")
    s.wait_results(2)
    assert s.results()[1]["is_error"] is False
    _assert_v2_only(s)


def test_an_interrupt_mid_message_closes_what_streamed(start, agent):
    """The verified road with no card: the interrupted turn never completes
    the message it cut, so the host closes it — the streamed "Hello world"
    is kept as an assistant row — and still writes no reply text on the
    error result (copilot_host's M1)."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate])
    s.say("count")
    assert _wait_for(lambda: s.turn_starts())
    assert _wait_for(lambda: any(r.get("type") == "stream_event" for r in s.out()))
    s.control("interrupt")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "cancelled" and results[0]["result"] == ""
    finals = [r for r in s.out() if r.get("type") == "assistant"]
    assert [f["message"]["content"][0]["text"] for f in finals] == ["Hello world"]


def test_an_interrupt_with_no_turn_open_is_answered_at_once(start, agent):
    s = start()
    assert _wait_for(lambda: len(s.out()) >= 1)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {"still_queued": []}
    assert "turn/interrupt" not in s.methods_seen()


def test_two_open_cards_are_each_answered_once_on_an_interrupt(start, agent):
    s = start(fake_args=["--perms", "2"],
              env={"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "0.3"})
    s.say("clean")
    assert _wait_for(lambda: len(s.perm_requests()) == 2)
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=10.0,
                                         start_offset=offset) == {"still_queued": []}
    assert s.proc.wait(timeout=15) == 0   # reaped: every teardown road ran
    assert s.decisions() == [["cancel"], ["cancel"]]
    _assert_v2_only(s)
    assert len(s.results()) == 1


def test_a_card_raised_while_interrupting_is_cancelled_at_once(start, agent):
    """Codex asks once more while winding an interrupt down: answered
    `cancel` at once, no card ever written for the page to draw."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--perm-after-interrupt"])
    s.say("go")
    assert _wait_for(lambda: s.turn_starts())
    assert _wait_for(lambda: any(r.get("type") == "stream_event" for r in s.out()))
    s.control("interrupt")
    results = s.wait_results(1)
    assert results[0]["stop_reason"] == "cancelled"
    assert s.decisions() == [["cancel"]]
    assert s.perm_requests() == []


def test_an_interrupt_codex_never_completes_is_settled_by_the_host(start, agent):
    """The interrupt is answered at once; a turn that ignores it is settled
    by the host after the grace (result `cancelled`, the interrupt mark) and
    the CLI killed."""
    gate = os.path.join(agent.RUNS, "never")
    s = start(fake_args=["--block-until", gate, "--ignore-interrupt"],
              env={"FUSED_CODEX_HOST_CANCEL_GRACE_SECONDS": "0.6"})
    s.say("go")
    assert _wait_for(lambda: s.turn_starts())
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    asked = time.monotonic()
    rid = s.control("interrupt")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {"still_queued": []}
    assert time.monotonic() - asked < 2.0, "answered at once, not after the grace"
    results = s.wait_results(1, timeout=10)
    assert len(results) == 1 and results[0]["stop_reason"] == "cancelled"
    assert s.proc.wait(timeout=15) == 0
    assert _wait_for(lambda: not agent._pid_alive(str(s.cli_pid())), timeout=5)
    assert tasks_store.is_interrupt_mark(tasks_store.first_text(
        [r for r in s.tx() if r["type"] == "user"][-1]["message"]["content"]))
    assert len(s.results()) == 1


def test_a_stop_that_kills_the_transport_reads_as_a_stop(start, agent):
    s = start(fake_args=["--block-until", os.path.join(agent.RUNS, "never")])
    s.say("go")
    assert _wait_for(lambda: s.turn_starts())
    open(os.path.join(s.run_dir, "cancelled"), "w").close()
    os.kill(s.cli_pid(), signal.SIGKILL)
    results = s.wait_results(1)
    assert results[0]["is_error"] is True and results[0]["stop_reason"] == "cancelled"
    assert results[0]["result"] == ""
    assert not any(r.get("isApiErrorMessage") for r in s.out())
    assert tasks_store.is_interrupt_mark(tasks_store.first_text(
        [r for r in s.tx() if r["type"] == "user"][-1]["message"]["content"]))
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())


# ------------------------------------------------- model and mode, live

def test_model_and_mode_changes_ride_the_next_turn_without_a_respawn(start, agent):
    """§6.15 / D5 / D7: `set_model` and `set_permission_mode` are stored and
    answered `success` at once, and the NEXT `turn/start` carries them — the
    same CLI process, no respawn, no RPC of their own. Anything else is
    refused "unsupported by codex"."""
    s = start()
    s.say("one")
    s.wait_results(1)
    pid = s.cli_pid()
    offset = os.path.getsize(os.path.join(s.run_dir, "out.jsonl"))
    rid = s.control("set_model", model="gpt-6.1-sol")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    rid = s.control("set_permission_mode", mode="acceptEdits")
    assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                         start_offset=offset) == {}
    for subtype in ("get_task_output", "stop_task"):
        errors = []
        rid = s.control(subtype, task_id="t1")
        assert agent._await_control_response(s.run_dir, rid, timeout=5.0,
                                             start_offset=offset, errors=errors) is None
        assert errors == ["unsupported by codex"]
    # Control responses after a `result` move no turn state.
    assert agent._turn_state(s.run_dir) == (False, False)
    s.say("two")
    s.wait_results(2)
    second = s.turn_starts()[1]
    assert second["model"] == "gpt-6.1-sol" and second["effort"] == "medium"
    assert (second["approvalPolicy"], second["sandboxPolicy"]) == \
        ("untrusted", {"type": "workspaceWrite"})
    rid = s.control("set_permission_mode", mode="default")
    s.say("three")
    s.wait_results(3)
    assert (s.turn_starts()[2]["approvalPolicy"], s.turn_starts()[2]["sandboxPolicy"]) \
        == ("untrusted", {"type": "readOnly"})
    assert s.cli_pid() == pid and s.proc.poll() is None
    assert s.methods_seen().count("thread/start") == 1
    # The next message names the new model.
    finals = [r for r in s.out() if r.get("type") == "assistant"]
    assert finals[-1]["message"]["model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("cli_mode,thread,turn", [
    (None, ("untrusted", "read-only"), ("untrusted", {"type": "readOnly"})),
    ("default", ("untrusted", "read-only"), ("untrusted", {"type": "readOnly"})),
    ("acceptEdits", ("untrusted", "workspace-write"),
     ("untrusted", {"type": "workspaceWrite"})),
    ("auto", ("never", "workspace-write"), ("never", {"type": "workspaceWrite"})),
])
def test_the_mode_at_spawn_is_the_d5_policy_pair(start, cli_mode, thread, turn):
    """D5 at spawn, on both requests that carry it: `thread/start`'s string
    `sandbox` and every `turn/start`'s structured `sandboxPolicy`. Never
    `danger-full-access`."""
    s = start(cli_mode=cli_mode)
    s.say("go")
    s.wait_results(1)
    params = s.sent_by_host("thread/start")[0]["msg"]["params"]
    assert (params["approvalPolicy"], params["sandbox"]) == thread
    t = s.turn_starts()[0]
    assert (t["approvalPolicy"], t["sandboxPolicy"]) == turn
    assert "danger" not in json.dumps(s.fake())


def test_accept_edits_lets_a_workspace_edit_through_unasked(start):
    """D5's acceptEdits row, end to end against the fake's model of Codex:
    `workspace-write` applies a file change inside the workspace with no
    card, while a shell command still asks."""
    s = start(cli_mode="acceptEdits", fake_args=["--edit", "made.txt", "--tool"])
    s.say("go")
    assert _wait_for(lambda: s.perm_requests())
    assert [m for _i, m in s.approvals()] == ["item/commandExecution/requestApproval"]
    s.decide(s.perm_requests()[0]["id"], decision="allow", scope="once")
    s.wait_results(1)
    assert [m for _i, m in s.approvals()] == ["item/commandExecution/requestApproval"]


# ---------------------------------------------------------------- failure

def test_a_failed_turn_is_an_api_error_row_and_an_error_result(start, agent):
    """D13: `turn/completed status: failed` → `isApiErrorMessage` on an
    assistant row in both files, the turn's own `error.message` on it and on
    the error result; a `willRetry` error notification says nothing."""
    s = start(fake_args=["--error"])
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True and results[0]["stop_reason"] == "error"
    assert results[0]["result"] == "You've hit your usage limit."
    errs = [r for r in s.out() if r.get("isApiErrorMessage") is True]
    assert len(errs) == 1 and agent._is_api_error_row(errs[0])
    assert errs[0]["message"]["content"] == [
        {"type": "text", "text": "You've hit your usage limit."}]
    # What streamed before the failure was still finalized first.
    assert [r["message"]["content"][0]["text"] for r in s.out()
            if r.get("type") == "assistant"] == ["Hello world", "You've hit your usage limit."]
    assert any(r.get("isApiErrorMessage") for r in s.tx())
    assert not session_liveness.transcript_turn_open(s.transcript, time.time())
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and "usage limit" in p["error"]


def test_a_failed_turn_with_no_words_of_its_own_uses_the_error_notice(start):
    """D13's fallback: the failed turn's `error` is null, so the last final
    `error` notification's message is the failure's text."""
    s = start(fake_args=["--error-notice"])
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["result"] == "stream disconnected before completion"


def test_a_refused_turn_start_ends_the_turn_as_a_failure(start, agent):
    """No turn ever began on the server, so no `turn/completed` will come:
    the refusal itself closes the turn, with exactly one result."""
    s = start(fake_args=["--refuse-turn"])
    s.say("go")
    results = s.wait_results(1)
    time.sleep(0.3)
    assert len(s.results()) == 1
    assert results[0]["is_error"] is True
    assert "model not supported" in results[0]["result"]
    assert agent._turn_state(s.run_dir) == (False, False)
    s.say("again")
    s.wait_results(2)   # the session is still up


def test_a_dead_transport_ends_the_turn_and_the_host(start):
    """Invariant 2 with no request pending: Codex answers `turn/start` at
    once, so a server that dies mid-turn leaves nothing for the transport
    failure to settle — the host's EOF hook does, with the exit code and the
    CLI's own stderr."""
    s = start(fake_args=["--die"])
    assert _wait_for(lambda: len(s.out()) >= 1)
    with open(os.path.join(s.run_dir, "err.log"), "ab") as fh:
        fh.write(b"boom from codex\n")
    s.say("go")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True
    assert results[0]["result"].startswith("codex exited (code 3)")
    assert "boom from codex" in results[0]["result"]
    assert len(s.results()) == 1
    assert s.proc.wait(timeout=10) == 0
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    assert not os.path.exists(os.path.join(s.registry_dir, "%d.json" % s.cli_pid()))


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
    assert results[0]["result"].startswith("codex exited (code 3)")


def _assert_failed_start(s, agent, needle):
    """A session that never came up: init first (no id), the
    `isApiErrorMessage` row, an error result, a clean exit, nothing left
    behind that lists it, the opening message retired (copilot_host's M7)."""
    assert s.proc.wait(timeout=15) == 0
    rows = s.out()
    assert [r["type"] for r in rows] == ["system", "assistant", "result"], rows
    assert rows[0]["subtype"] == "init" and rows[0]["session_id"] == ""
    assert rows[1]["isApiErrorMessage"] is True and agent._is_api_error_row(rows[1])
    assert needle in rows[1]["message"]["content"][0]["text"]
    assert rows[2]["is_error"] is True and needle in rows[2]["result"]
    assert agent._turn_state(s.run_dir) == (False, False)
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and needle in p["error"]
    assert not os.path.exists(s.bucket)
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    assert not os.path.isdir(s.registry_dir) or not os.listdir(s.registry_dir)
    assert s.inbox_pending() == []
    assert len(os.listdir(os.path.join(s.run_dir, "inbox", "done"))) == 1


def test_a_refused_thread_start_is_an_api_error_and_a_clean_exit(start, agent):
    """The brief's signed-out shape: `thread/start` refused -32000
    "Authentication required" (assumed — 0.163.0 does NOT do this, see the
    next test): init, the error row naming it, an error result, a clean exit
    that leaves no transcript and retires the opening message."""
    s = start(fake_args=["--signed-out"], messages=["hi"])
    _assert_failed_start(s, agent, "Authentication required")
    assert "thread/start" in s.methods_seen()
    assert "turn/start" not in s.methods_seen()


def test_signed_out_codex_fails_the_first_turn(start, agent):
    """What 0.163.0 actually does signed out (measured 2026-10-21 with an
    empty CODEX_HOME: `account/read` says `account: null`, `thread/start`
    SUCCEEDS and mints a thread, the 401 comes when the model is called):
    the session comes up, and its first turn fails as an API error the
    account markers can read."""
    s = start(fake_args=["--signed-out-turn"])
    s.say("hi")
    results = s.wait_results(1)
    assert results[0]["is_error"] is True and results[0]["stop_reason"] == "error"
    assert "401 Unauthorized" in results[0]["result"]
    assert s.out()[0]["session_id"] == s.sid and s.sid
    p = agent._poll(os.path.basename(s.run_dir))
    assert p["done"] and "401 Unauthorized" in p["error"]


def test_a_cli_that_exits_before_the_handshake_fails_loudly(start, agent):
    """The CLI refuses to start (a bad `-c` override, a broken install): not
    a session that is merely slow — init, an error quoting its exit code and
    its own stderr, a clean host exit."""
    s = start(fake_args=["--exit-at-start"], messages=["hi"])
    _assert_failed_start(s, agent, "error: invalid -c override")
    assert "(code 2)" in s.results()[0]["result"]
    assert "codex exited during initialize" in s.results()[0]["result"]
    assert s.fake() == []   # it never read a byte


def test_a_missing_binary_is_a_failed_start(agent, tmp_path):
    run_dir = tmp_path / "runs" / "r-missing"
    (run_dir / "inbox").mkdir(parents=True)
    req = {"agent": AGENT, "run_dir": str(run_dir), "file": str(tmp_path),
           "cwd": str(tmp_path), "pane": False, "cli_mode": None, "session_id": "",
           "model": MODEL, "effort": "", "extra_read_dirs": [], "new_session_id": "",
           "argv": [str(tmp_path / "no-such-codex"), "app-server"], "backend": "codex"}
    env = dict(os.environ)
    env.pop("FUSED_RENDER_ORIGIN", None)
    proc = subprocess.run([sys.executable, HOST], input=json.dumps(req).encode(),
                          capture_output=True, timeout=30, env=env)
    assert proc.returncode == 0
    assert b"no-such-codex" in (run_dir / "err.log").read_bytes()
    assert not (run_dir / "pid").exists()


def test_a_cancelled_marker_before_the_spawn_spawns_nothing(agent, tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir()
    (run_dir / "cancelled").write_text("1")
    req = {"agent": AGENT, "run_dir": str(run_dir), "file": str(tmp_path),
           "cwd": str(tmp_path), "pane": False, "cli_mode": "", "session_id": "",
           "model": "", "effort": "", "extra_read_dirs": [], "new_session_id": "x",
           "argv": [sys.executable, FAKE], "backend": "codex"}
    proc = subprocess.run([sys.executable, HOST], input=json.dumps(req).encode(),
                          capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert b"cancelled before the CLI spawned" in proc.stderr
    assert not (run_dir / "out.jsonl").exists()
    assert not (run_dir / "pid").exists()


# ------------------------------------------------------------------ misc

def test_the_idle_reap_ends_the_session(start, agent):
    s = start(env={"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "0.4"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    s.say("hi")
    s.wait_results(1)
    assert s.proc.wait(timeout=10) == 0
    assert not os.path.exists(os.path.join(s.run_dir, "host.json"))
    assert not agent._host_alive(s.run_dir)
    assert _wait_for(lambda: not agent._alive(s.run_dir), timeout=5.0)
    assert not os.path.exists(os.path.join(s.registry_dir, "%d.json" % s.cli_pid()))
    assert os.path.exists(os.path.join(s.run_dir, "pid"))   # left for `_alive`
    assert agent._turn_state(s.run_dir) == (False, False)
    # The app-server exits on stdin EOF: the reap closed it, nothing killed it.
    assert any(e.get("eof") for e in s.fake())


def test_thinking_streams_once_as_thinking_deltas(start, agent):
    """`item/reasoning/summaryTextDelta` is thinking; the raw `textDelta` of
    the SAME reasoning item is not told a second time."""
    s = start(fake_args=["--think"])
    s.say("hi")
    s.wait_results(1)
    rows = s.out()
    echo_at = next(i for i, r in enumerate(rows) if agent._starts_new_turn(r))
    kinds = [sg["kind"] for sg in _reply(agent._segments_from_rows(rows[echo_at:]))]
    assert kinds[0] == "thinking" and "text" in kinds
    final = [r for r in rows if r.get("type") == "assistant"][0]
    assert final["message"]["content"] == [
        {"type": "thinking", "thinking": "Let me think"},
        {"type": "text", "text": "Hello world!"}]
    assert "RAW" not in json.dumps(rows)


def test_lone_surrogates_survive_both_directions(start, agent):
    s = start(fake_args=["--surrogate"])
    s.say("hi \ud83d")
    results = s.wait_results(1)
    assert results[0]["is_error"] is False
    assert results[0]["result"] == "Hello\ud83d world!"
    assert s.turn_starts()[0]["input"][0]["text"] == "hi \ud83d"
    for path in (os.path.join(s.run_dir, "out.jsonl"), s.transcript):
        with open(path, "rb") as fh:
            fh.read().decode("ascii")   # escaped, never raw
    assert tasks_store._parse_head(s.transcript)[2] == "hi \ud83d"


def test_the_echo_carries_the_inbox_rows_uuid(start, agent):
    s = start()
    s.say("hi")
    s.wait_results(1)
    done = os.path.join(s.run_dir, "inbox", "done")
    entries = [json.load(open(os.path.join(done, n), encoding="utf-8"))
               for n in sorted(os.listdir(done)) if n.endswith(".json")]
    stamped = [e["uuid"] for e in entries if e.get("type") == "user"]
    assert len(stamped) == 1
    echo = [r for r in s.out() if agent._starts_new_turn(r)]
    assert [r.get("uuid") for r in echo] == stamped
    assert agent._inbox_waiting(s.run_dir) == []


def test_the_cli_gets_codexs_env(start, agent):
    """§3: `CODEX_HOME` reaches the CLI exactly as found (it is where the
    credential lives). Claude's `CLAUDE_CODE_*` switches are stripped once
    agent.py's codex branch of `_spawn_env` exists (WS-C); until then the
    Claude env is what any backend but copilot gets."""
    s = start(env={"CODEX_HOME": "/x/codex-home",
                   "CLAUDE_CODE_SHELL_PREFIX": "/x/prefix.sh"})
    assert _wait_for(lambda: len(s.out()) >= 1)
    env = json.load(open(s.env_dump))
    assert env["CODEX_HOME"] == "/x/codex-home"
    if "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING" not in agent._spawn_env(
            "", backend="codex"):
        assert not [k for k in env if k.startswith("CLAUDE_CODE_")]


# ------------------------------------------------- the host, in process

def _host_in_process(agent, host_mod, tmp_path, fake_args=()):
    cwd = str(tmp_path / "proj")
    os.makedirs(cwd, exist_ok=True)
    run_dir = os.path.join(agent.RUNS, "inproc-" + os.urandom(3).hex())
    os.makedirs(os.path.join(run_dir, "inbox"))
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"resumed_from": "", "file": cwd, "backend": "codex"}, fh)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "session_id": "", "backend": "codex"}, fh)
    log = os.path.join(run_dir, "fake.log")
    cli = subprocess.Popen([sys.executable, FAKE, "app-server", "--log", log]
                           + list(fake_args), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd)
    req = {"agent": AGENT, "run_dir": run_dir, "file": cwd, "cwd": cwd,
           "pane": False, "cli_mode": None, "session_id": "", "model": MODEL,
           "effort": "", "extra_read_dirs": [], "new_session_id": "",
           "argv": [], "backend": "codex"}
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
        if len(calls) == 2:   # the pass the idle timer exits after
            agent._write_inbox_entry(run_dir, "late")
        return held

    host.drain_inbox = drain
    host.run()
    out = _rows(os.path.join(run_dir, "out.jsonl"))
    assert [r["type"] for r in out].count("result") == 1
    assert out[[r["type"] for r in out].index("result")]["is_error"] is False
    prompts = [e["msg"]["params"]["input"][0]["text"] for e in _rows(log)
               if e.get("msg", {}).get("method") == "turn/start"]
    assert prompts == ["late"]
    assert not os.path.exists(os.path.join(run_dir, "host.json"))
    assert host.cli.poll() == 0


def test_a_cli_that_ignores_eof_is_killed(agent, host_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    monkeypatch.setattr(host_mod, "_EXIT_WAIT_SECONDS", 0.3)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--ignore-eof"])
    assert host.handshake()
    host.close()
    assert host.cli.returncode == -signal.SIGKILL
    assert any(e.get("eof") for e in _rows(log)), "stdin was closed first"


def test_the_settings_record_precedes_every_file_naming_the_id(
        agent, host_mod, tmp_path, monkeypatch):
    import agent_roots
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path)
    seen = []

    def record(sid, model="", effort="", backend=""):
        seen.append({
            "args": (sid, model, effort, backend),
            "out": _rows(os.path.join(run_dir, "out.jsonl")),
            "transcripts": os.path.exists(agent_roots.projects_dir("codex")),
            "registry": os.path.exists(host.registry_path),
            "host_sid": json.load(open(os.path.join(run_dir, "host.json")))["session_id"]})

    monkeypatch.setattr(agent, "_record_settings", record)
    try:
        assert host.handshake()
        assert len(seen) == 1
        assert seen[0]["args"] == (host.sid, MODEL, "", "codex")
        assert (seen[0]["out"], seen[0]["transcripts"], seen[0]["registry"],
                seen[0]["host_sid"]) == ([], False, False, "")
    finally:
        host.close()


def test_a_permission_that_throws_is_still_answered_once(
        agent, host_mod, tmp_path, monkeypatch):
    """Anything going wrong while a card is built answers the request
    `decline`, once — Codex is never left blocked, and nothing the user did
    asked for the turn to stop."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--tool"])

    def boom(*a, **k):
        raise RuntimeError("card exploded")

    monkeypatch.setattr(host, "_card_tool", boom)
    try:
        assert host.handshake()
        host.send_prompt("clean")

        def answers():
            asked = [e["sent"]["id"] for e in _rows(log)
                     if "requestApproval" in str((e.get("sent") or {}).get("method"))]
            return [e["msg"] for e in _rows(log) if asked and "msg" in e
                    and e["msg"].get("id") == asked[0] and "method" not in e["msg"]]

        assert _wait_for(lambda: answers())
        assert _wait_for(lambda: not host.turn_open)
        assert [a["result"] for a in answers()] == [{"decision": "decline"}]
        assert host.pending_perm == {}
        assert not os.path.isdir(os.path.join(run_dir, "perm")) or \
            not os.listdir(os.path.join(run_dir, "perm"))
    finally:
        host.close()


def test_a_cancel_landing_before_the_card_is_written_raises_no_card(
        agent, host_mod, tmp_path, monkeypatch):
    """copilot_host's M1, on this wire: a cancel (`reject_pending`) between
    the request's registration and its card answers it `cancel`, and the
    card is never raised."""
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    host, run_dir, log = _host_in_process(agent, host_mod, tmp_path, ["--tool"])
    posts = []
    monkeypatch.setattr(host_mod, "_post_card",
                        lambda run_dir, kind, req_id: posts.append((kind, req_id)))
    real = host._card_tool

    def card_then_cancel(*a, **k):
        out = real(*a, **k)
        host.reject_pending("cancelled")   # the gap the race lived in
        return out

    monkeypatch.setattr(host, "_card_tool", card_then_cancel)
    try:
        assert host.handshake()
        host.send_prompt("clean")
        assert _wait_for(lambda: not host.turn_open)
        answered = [e["msg"]["result"] for e in _rows(log)
                    if "msg" in e and "method" not in e["msg"]
                    and "decision" in (e["msg"].get("result") or {})]
        assert answered == [{"decision": "cancel"}]
        perm = os.path.join(run_dir, "perm")
        assert not os.path.isdir(perm) or not os.listdir(perm)
        assert posts == []
    finally:
        host.close()
