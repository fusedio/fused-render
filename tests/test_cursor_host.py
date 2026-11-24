"""cursor_host.py: the Cursor CLI session host, driven end to end against a
fake ACP peer (tests/fake_cursor_acp.py) the way `_start` drives the real
one — a detached subprocess, a JSON request on stdin, the inbox written with
`agent._write_inbox_entry` / `_write_control_request`, and every assertion
read back off the files the rest of the app reads.

The Cursor twin of tests/test_opencode_host.py, trimmed to what the shared
host skeleton does not already pin there and what is Cursor's own
(docs/CURSOR_BACKEND.md): the parameterized model picker asked for at
`initialize`, `agent` pinned with `session/set_mode`, the model set live and
a refused one falling back to the session's own, the appended prompt riding
the process's FIRST prompt only (and stripped off a replayed one), a
`tool_call` held until it starts so a half-streamed command never reaches a
row, the card's `` `cmd` `` title, answers by KIND that never pick
`allow-always`, the session-scoped allow kept by the host, the approval
modes as the host's own policy (no wire request), the question card
skipped, and the CLI's extension requests refused.
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

import pytest

AGENT_DIR = os.path.abspath(os.path.join("fused_render", "claude_agent"))
HOST = os.path.join(AGENT_DIR, "cursor_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
FAKE = os.path.abspath(os.path.join("tests", "fake_cursor_acp.py"))
NOTICE = "Handled by Cursor · model auto-smart"
WIRE = {"initialize", "session/new", "session/load", "session/prompt",
        "session/set_model", "session/set_mode", "session/cancel"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    mod = _load("claude_agent_for_cursor_tests", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def host_mod():
    return _load("cursor_host_under_test", HOST)


def _wait_for(predicate, timeout=15.0, interval=0.05):
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


class Session:
    def __init__(self, agent, tmp_path, resume_sid="", fake_args=(), cli_mode="default",
                 model="auto", instructions=None, transcript_seed=None):
        import agent_roots
        self.agent = agent
        self._sid = resume_sid
        self.cwd = str(tmp_path / "proj")
        os.makedirs(self.cwd, exist_ok=True)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": self._sid, "file": self.cwd,
                       "backend": "cursor"}, fh)
        if instructions is not None:
            os.makedirs(os.path.join(self.run_dir, "cursor_agent"))
            with open(os.path.join(self.run_dir, "cursor_agent", "instructions.md"), "w") as fh:
                fh.write(instructions + "\n")
        self.bucket = os.path.join(agent_roots.cursor_dir(), "projects",
                                   agent_roots.munge(self.cwd))
        if transcript_seed:
            os.makedirs(self.bucket, exist_ok=True)
            with open(os.path.join(self.bucket, resume_sid + ".jsonl"), "w") as fh:
                for row in transcript_seed:
                    fh.write(json.dumps(row) + "\n")
        self.log = os.path.join(self.run_dir, "fake.log")
        self.gate = os.path.join(self.run_dir, "gate")
        self.env_dump = os.path.join(self.run_dir, "fake.env.json")
        argv = [sys.executable, FAKE, "acp", "--log", self.log,
                "--check-echo", os.path.join(self.run_dir, "out.jsonl"),
                "--dump-env", self.env_dump] + list(fake_args)
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": cli_mode,
               "session_id": self._sid, "model": model, "effort": "",
               "extra_read_dirs": [], "new_session_id": "", "argv": argv,
               "backend": "cursor"}
        env = dict(os.environ)
        env.pop("FUSED_RENDER_ORIGIN", None)
        env.update({"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "5",
                    "FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS": "0.05",
                    "FUSED_RENDER_PERMISSION_TIMEOUT": "20"})
        self.err = open(os.path.join(self.run_dir, "host.err.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, HOST], stdin=subprocess.PIPE,
                                     stderr=self.err, env=env)
        self.proc.stdin.write(json.dumps(req).encode("utf-8"))
        self.proc.stdin.close()

    @property
    def sid(self):
        if not self._sid:
            assert _wait_for(lambda: any(r.get("subtype") == "init" for r in self.out())), \
                "no init row\nhost.err: %s" % self.host_err()
            self._sid = next(r for r in self.out() if r.get("subtype") == "init")["session_id"]
        return self._sid

    @property
    def transcript(self):
        return os.path.join(self.bucket, self.sid + ".jsonl")

    def out(self):
        return _rows(os.path.join(self.run_dir, "out.jsonl"))

    def tx(self):
        return _rows(self.transcript)

    def fake(self):
        return _rows(self.log)

    def results(self):
        return [r for r in self.out() if r.get("type") == "result"]

    def sent(self, method):
        return [e["msg"] for e in self.fake() if e.get("msg", {}).get("method") == method]

    def methods(self):
        return [e["msg"]["method"] for e in self.fake()
                if "msg" in e and e["msg"].get("method")]

    def answers(self):
        return [e["answer"] for e in self.fake() if "answer" in e]

    def perm_requests(self):
        perm = os.path.join(self.run_dir, "perm")
        try:
            names = sorted(n for n in os.listdir(perm) if n.endswith(".req.json"))
        except FileNotFoundError:
            return []
        return [json.load(open(os.path.join(perm, n))) for n in names]

    def decide(self, perm_id, **decision):
        with open(os.path.join(self.run_dir, "perm", perm_id + ".res.json"), "w") as fh:
            json.dump(decision, fh)

    def host_err(self):
        with open(os.path.join(self.run_dir, "host.err.log"), "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    def say(self, text):
        self.agent._write_inbox_entry(self.run_dir, text)

    def control(self, subtype, **fields):
        return self.agent._write_control_request(self.run_dir, subtype, **fields)

    def wait_results(self, n=1, timeout=20.0):
        assert _wait_for(lambda: len(self.results()) >= n, timeout), \
            "no result row\nout: %s\nhost.err: %s" % (self.out(), self.host_err())
        return self.results()

    def control_responses(self):
        return [r["response"] for r in self.out() if r.get("type") == "control_response"]

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


# ------------------------------------------------------------- handshake

def test_the_handshake_mints_the_id_pins_agent_and_asks_for_base_model_ids(start, agent):
    s = start()
    assert _wait_for(lambda: os.path.exists(s.transcript) and s.tx())
    init = s.out()[0]
    assert init["type"] == "system" and init["subtype"] == "init"
    # The catalog's `auto` is resolved to Cursor's own Auto row.
    assert init["backend"] == "cursor" and init["model"] == "auto-smart"
    assert init["slash_commands"] in ([], ["copy-request-id"])
    assert s.tx()[0]["summary"] == NOTICE
    assert s.transcript.startswith(os.path.join(os.environ["FUSED_RENDER_HOME"], "cursor"))
    initialize = s.sent("initialize")[0]["params"]
    # D7: base model names, never Cursor's exploded variant strings.
    assert initialize["clientCapabilities"] == {"_meta": {"parameterizedModelPicker": True}}
    # D5: Cursor's own `agent` mode, never plan/ask; the model already is
    # the one asked for, so no `session/set_model`.
    assert _wait_for(lambda: s.sent("session/set_mode"))
    assert [m["params"]["modeId"] for m in s.sent("session/set_mode")] == ["agent"]
    assert s.sent("session/set_model") == []
    assert agent._session_backend(s.sid) == "cursor"


def test_a_named_model_is_set_before_the_first_prompt(start):
    s = start(model="gpt-5.5")
    s.say("go")
    s.wait_results(1)
    methods = s.methods()
    assert [m["params"]["modelId"] for m in s.sent("session/set_model")] == ["gpt-5.5"]
    assert methods.index("session/set_model") < methods.index("session/prompt")
    assert s.out()[0]["model"] == "gpt-5.5"


def test_a_refused_model_falls_back_to_the_sessions_own(start):
    s = start(model="nope-1", fake_args=["--current-model", "composer-2.5"])
    assert _wait_for(lambda: any(r.get("subtype") == "init" for r in s.out()))
    assert s.out()[0]["model"] == "composer-2.5"
    assert "session/set_model nope-1 refused" in s.host_err()


def test_signed_out_fails_the_handshake_with_the_clis_words(start, agent):
    s = start(fake_args=["--signed-out"])
    res = s.wait_results(1)[0]
    assert res["is_error"] and "Authentication required" in res["result"]
    # The CLI's actionable sentence rides along (it is under `data`).
    assert "Please run 'agent login' first" in res["result"]
    worded = agent._account_error(res["result"], "cursor")
    assert worded.startswith("Cursor CLI isn't signed in.")
    assert "cursor-agent login" in worded


# ----------------------------------------------------------- the prompt (D8)

def test_the_appended_prompt_rides_the_first_prompt_only(start):
    s = start(instructions="You are scoped to proj.")
    s.say("first")
    s.wait_results(1)
    s.say("second")
    s.wait_results(2)
    prompts = [m["params"]["prompt"] for m in s.sent("session/prompt")]
    assert len(prompts) == 2
    assert prompts[0][0]["text"].startswith("<fused_render_context>\nYou are scoped to proj.")
    assert prompts[0][1] == {"type": "text", "text": "first"}
    assert prompts[1] == [{"type": "text", "text": "second"}]
    # The echo and the transcript keep the user's words only.
    echoes = [r for r in s.out() if r.get("type") == "user"
              and r["message"]["content"][0].get("type") == "text"]
    assert [e["message"]["content"][0]["text"] for e in echoes] == ["first", "second"]
    users = [r for r in s.tx() if r.get("type") == "user"]
    assert all("fused_render_context" not in json.dumps(r) for r in users)
    # The echo precedes the prompt (invariant 3).
    assert all(e["echo_seen"] for e in s.fake() if "echo_seen" in e)


def test_a_turn_ends_in_one_success_result(start, agent):
    s = start()
    s.say("hi")
    res = s.wait_results(1)
    assert len(res) == 1 and res[0]["subtype"] == "success"
    assert res[0]["result"] == "Hello world"
    assert agent._turn_state(s.run_dir) == (False, False)


# --------------------------------------------------------------- tools (D14)

def test_a_tool_row_waits_for_the_call_to_start(start):
    """The first `tool_call` carries a half-streamed command (`ls -`); the
    row that goes up names the whole one, once."""
    s = start(fake_args=["--tool"])
    s.say("list")
    s.wait_results(1)
    uses = [b for r in s.out() if r.get("type") == "assistant"
            for b in r["message"]["content"] if b.get("type") == "tool_use"]
    assert len(uses) == 1
    assert uses[0]["name"] == "Bash" and uses[0]["input"]["command"] == "ls -la"
    results = [b for r in s.out() if r.get("type") == "user"
               for b in r["message"].get("content", []) if b.get("type") == "tool_result"]
    assert results == [{"type": "tool_result", "tool_use_id": "call-1",
                        "content": "a.txt", "is_error": False}]


def test_tool_mapping(host_mod, tmp_path):
    m = host_mod._map_tool
    assert m("execute", "`git status`", None)[1]["command"] == "git status"
    assert m("execute", "x", {"command": "ls"})[1]["command"] == "ls"
    (tmp_path / "a.py").write_text("x")
    name, inp = m("edit", "Edit", {"path": str(tmp_path / "a.py")}, cwd=str(tmp_path),
                  content=[{"type": "diff", "path": str(tmp_path / "a.py"),
                            "oldText": "x\n", "newText": "y\n"}])
    assert name == "Edit" and "-x" in inp["diff"] and "+y" in inp["diff"]
    assert m("edit", "Write", {"path": "new.txt"}, cwd=str(tmp_path))[0] == "Write"
    assert m("delete", "Delete", {"path": "a.py"})[0] == "Delete"
    assert m("read", "Read", {"path": "a.py"})[1]["file_path"] == "a.py"
    assert m("search", "grep", {"pattern": "foo", "path": "src"})[1] == {
        "pattern": "foo", "path": "src", "_acp": {"kind": "search", "title": "grep"}}
    name, inp = m("other", "x", {"providerIdentifier": "fused_render",
                                 "toolName": "app_state", "args": {"reason": "r"}})
    assert name == "mcp__fused_render__app_state" and inp["reason"] == "r"
    assert m("other", "fused_render: terminal_list", None)[0] == \
        "mcp__fused_render__terminal_list"
    assert m("other", "x", {"providerIdentifier": "github", "toolName": "pr"})[0] == "github-pr"


# ---------------------------------------------------------- permissions (D4/D5)

def _card(s, n=1):
    assert _wait_for(lambda: len(s.perm_requests()) >= n), s.host_err()
    return s.perm_requests()[n - 1]


def _permission_answers(s):
    return [a["result"]["outcome"] for a in s.answers()
            if isinstance(a.get("result"), dict) and "outcome" in a["result"]]


def test_a_shell_card_shows_the_command_and_allow_is_allow_once(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    card = _card(s)
    assert card["tool"] == "Bash" and card["input"]["command"] == "rm -rf build"
    s.decide(card["id"], decision="allow")
    s.wait_results(1)
    assert _permission_answers(s) == [{"outcome": "selected", "optionId": "allow-once"}]


def test_a_deny_is_reject_once(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    s.decide(_card(s)["id"], decision="deny")
    s.wait_results(1)
    assert _permission_answers(s) == [{"outcome": "selected", "optionId": "reject-once"}]


def test_always_approve_is_kept_by_the_host_never_cursors_allow_always(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    s.decide(_card(s)["id"], decision="allow", scope="session")
    s.wait_results(1)
    s.say("clean again")
    s.wait_results(2)
    # The second identical call raised no card: the host allowed it itself.
    assert len(s.perm_requests()) == 1
    assert _permission_answers(s) == [{"outcome": "selected", "optionId": "allow-once"}] * 2


def test_auto_mode_answers_every_card_itself(start):
    s = start(fake_args=["--perm"], cli_mode="auto")
    s.say("clean")
    s.wait_results(1)
    assert s.perm_requests() == []
    assert _permission_answers(s) == [{"outcome": "selected", "optionId": "allow-once"}]


def test_accept_edits_allows_an_edit_card_but_raises_a_shell_one(start, tmp_path):
    s = start(fake_args=["--perm-edit", "a.txt"], cli_mode="acceptEdits")
    s.say("edit")
    s.wait_results(1)
    assert s.perm_requests() == []
    s2 = start(fake_args=["--perm"], cli_mode="acceptEdits")
    s2.say("clean")
    card = _card(s2)
    s2.decide(card["id"], decision="deny")
    s2.wait_results(1)


def test_an_edit_card_carries_its_diff(start):
    s = start(fake_args=["--perm-edit", "a.txt"])
    s.say("edit")
    card = _card(s)
    assert card["tool"] in ("Edit", "Write")
    assert card["input"]["file_path"] == "a.txt"
    s.decide(card["id"], decision="deny")
    s.wait_results(1)


def test_a_mode_switch_is_the_hosts_own_and_sends_nothing(start):
    s = start(fake_args=["--perm"])
    assert _wait_for(lambda: any(r.get("subtype") == "init" for r in s.out()))
    rid = s.control("set_permission_mode", mode="auto")
    assert _wait_for(lambda: any(r.get("request_id") == rid for r in s.control_responses()))
    resp = next(r for r in s.control_responses() if r["request_id"] == rid)
    assert resp["subtype"] == "success"
    s.say("clean")
    s.wait_results(1)
    assert s.perm_requests() == []
    # Only the handshake's own `agent` pin ever reached the CLI.
    assert [m["params"]["modeId"] for m in s.sent("session/set_mode")] == ["agent"]
    bad = s.control("set_permission_mode", mode="plan")
    assert _wait_for(lambda: any(r.get("request_id") == bad for r in s.control_responses()))
    assert next(r for r in s.control_responses()
                if r["request_id"] == bad)["subtype"] == "error"


def test_allow_and_stop_asking_moves_the_mode(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    s.decide(_card(s)["id"], decision="allow", mode="auto")
    s.wait_results(1)
    s.say("again")
    s.wait_results(2)
    assert len(s.perm_requests()) == 1


def test_a_question_card_is_skipped_without_a_card(start):
    s = start(fake_args=["--question"])
    s.say("ask me")
    s.wait_results(1)
    assert s.perm_requests() == []
    assert _permission_answers(s) == [
        {"outcome": "selected", "optionId": "__ask_question_skip__"}]


def test_an_extension_request_is_refused_and_the_turn_goes_on(start):
    s = start(fake_args=["--ext"])
    s.say("todo")
    res = s.wait_results(1)
    assert res[0]["subtype"] == "success"
    errs = [a for a in s.answers() if "error" in a]
    assert errs and errs[0]["error"]["code"] == -32601


def test_permission_outcome_never_picks_allow_always(host_mod):
    opts = [{"optionId": "allow-once", "kind": "allow_once"},
            {"optionId": "allow-always", "kind": "allow_always"},
            {"optionId": "reject-once", "kind": "reject_once"}]
    out = host_mod._permission_outcome
    assert out(opts, {"decision": "allow", "scope": "session"})["outcome"]["optionId"] == "allow-once"
    assert out(opts, {"decision": "allow"})["outcome"]["optionId"] == "allow-once"
    assert out(opts, {"decision": "deny"})["outcome"]["optionId"] == "reject-once"
    only_always = [{"optionId": "allow-always", "kind": "allow_always"}]
    assert out(only_always, {"decision": "allow"}) == {"outcome": {"outcome": "cancelled"}}


# ------------------------------------------------------------- resume (D3)

def test_a_resume_backfills_the_replay_without_the_context_block(start):
    sid = "11111111-2222-4333-8444-555555555555"
    s = start(resume_sid=sid, fake_args=["--replay", "2"], transcript_seed=[
        {"type": "user", "uuid": "u1", "sessionId": sid,
         "message": {"role": "user", "content": [{"type": "text", "text": "replayed q1"}]}}])
    assert _wait_for(lambda: any(r.get("subtype") == "init" for r in s.out()))
    assert _wait_for(lambda: len(s.tx()) >= 3)
    users = [r["message"]["content"][0]["text"] for r in s.tx() if r.get("type") == "user"]
    # q1 was already there (its replay carried the context block, stripped
    # before the match); q2 was a terminal turn, appended.
    assert users == ["replayed q1", "replayed q2"]
    assert not any(r.get("type") == "assistant" for r in s.out())
    assert s.sent("session/load")[0]["params"]["sessionId"] == sid


# ------------------------------------------------------- stop, errors, env

def test_an_interrupt_cancels_the_turn(start, agent):
    s = start(fake_args=["--block-until", "/nonexistent/never"])
    s.say("long")
    assert _wait_for(lambda: s.sent("session/prompt"))
    s.control("interrupt")
    res = s.wait_results(1)
    assert res[0]["stop_reason"] == "cancelled"
    assert s.sent("session/cancel")


def test_a_json_rpc_error_is_an_api_error_row(start, agent):
    s = start(fake_args=["--error"])
    s.say("hi")
    res = s.wait_results(1)[0]
    assert res["is_error"] and "Rate limit" in res["result"]
    assert any(r.get("isApiErrorMessage") for r in s.out())
    assert agent._account_error(res["result"], "cursor").startswith(
        "Your Cursor plan's usage limit was reached.")


def test_the_cli_gets_cursors_env(start):
    s = start()
    assert _wait_for(lambda: os.path.exists(s.env_dump))
    env = json.load(open(s.env_dump))
    assert env.get("NO_OPEN_BROWSER") == "1"
    assert not any(k.startswith("CLAUDE_CODE_") for k in env)


def test_the_idle_reap_terminates_a_cli_that_ignores_eof(start):
    """Cursor's ACP server ignores stdin EOF (observed); the host's reap
    SIGTERMs it at once instead of waiting out the grace and SIGKILLing."""
    s = start()
    assert _wait_for(lambda: any(r.get("subtype") == "init" for r in s.out()))
    cli = int(open(os.path.join(s.run_dir, "pid")).read())
    t0 = time.monotonic()
    s.proc.wait(timeout=30)   # the idle reap (5 s here) closes the session
    gone = _wait_for(lambda: not os.path.exists("/proc/%d" % cli)
                     or open("/proc/%d/stat" % cli).read().split()[2] == "Z", timeout=5)
    assert gone, "the CLI outlived its host"
    assert time.monotonic() - t0 < 12
    assert any(e.get("sigterm") for e in s.fake()), "stopped by SIGKILL, not SIGTERM"


def test_the_hosts_mode_table_is_agent_pys(host_mod, agent):
    assert set(host_mod._CURSOR_MODES) == set(agent._CURSOR_MODES)
    assert host_mod._INSTRUCTIONS_FILE == agent._CURSOR_INSTRUCTIONS


# ------------------------------------------ fixes from the live run (2026-11-20)

def test_auto_resolves_to_cursors_own_auto_row_not_the_last_picked_model(start):
    """`session/set_model auto` is refused by the real CLI (`Invalid model
    value: auto`), and `set_model` is persisted to its config — so a spawn
    asking for `auto` used to adopt whatever model was picked last,
    anywhere. It resolves to the listed Auto row (`auto-smart`) instead."""
    s = start(model="auto", fake_args=["--current-model", "gpt-5.5"])
    s.say("go")
    s.wait_results(1)
    assert [m["params"]["modelId"] for m in s.sent("session/set_model")] == ["auto-smart"]
    assert s.out()[0]["model"] == "auto-smart"
    assert "refused" not in s.host_err()


def test_a_live_switch_to_auto_sends_cursors_own_id(start):
    s = start(model="gpt-5.5")
    assert _wait_for(lambda: any(r.get("subtype") == "init" for r in s.out()))
    rid = s.control("set_model", model="auto")
    assert _wait_for(lambda: any(r.get("request_id") == rid for r in s.control_responses()))
    resp = next(r for r in s.control_responses() if r.get("request_id") == rid)
    assert resp["subtype"] == "success"
    assert [m["params"]["modelId"] for m in s.sent("session/set_model")][-1] == "auto-smart"


def _tool_results(s):
    return [b for r in s.out() if r.get("type") == "user"
            for b in r["message"].get("content", []) if b.get("type") == "tool_result"]


def test_a_denied_call_reads_denied_not_an_empty_success(start):
    """Cursor ends a rejected call with a BARE `completed` (observed): the
    tool result says the user denied it, as an error."""
    s = start(fake_args=["--perm"])
    s.say("clean")
    s.decide(_card(s)["id"], decision="deny")
    s.wait_results(1)
    assert _tool_results(s) == [{"type": "tool_result", "tool_use_id": "call-perm-0",
                                 "content": "The user denied this request.",
                                 "is_error": True}]


def test_an_allowed_call_keeps_its_output(start):
    s = start(fake_args=["--perm"])
    s.say("clean")
    s.decide(_card(s)["id"], decision="allow")
    s.wait_results(1)
    assert _tool_results(s) == [{"type": "tool_result", "tool_use_id": "call-perm-0",
                                 "content": "ok", "is_error": False}]


def test_an_uncarded_new_file_edit_shows_its_content(start, tmp_path):
    """Cursor announces an edit with an empty `rawInput` and sends its diff
    only on `completed`, in a mangled new-file shape (`-- /dev/null` /
    `++ b/<path>\\n<content>`, observed). The row waits for the content and
    reads as a Write of it; the result is a clean header-free diff."""
    path = str(tmp_path / "proj" / "note.txt")
    s = start(fake_args=["--edit-new", path])
    s.say("write")
    s.wait_results(1)
    uses = [b for r in s.out() if r.get("type") == "assistant"
            for b in r["message"]["content"] if b.get("type") == "tool_use"]
    assert len(uses) == 1
    assert uses[0]["name"] == "Write"
    assert uses[0]["input"]["file_path"] == path
    assert uses[0]["input"]["content"] == "hello"
    [res] = _tool_results(s)
    assert res["is_error"] is False
    assert "/dev/null" not in res["content"] and "+hello" in res["content"]


def _starts(rows, tid):
    """Indexes of the `content_block_start` rows for tool id `tid`."""
    return [i for i, r in enumerate(rows)
            if r.get("type") == "stream_event"
            and r["event"].get("type") == "content_block_start"
            and r["event"]["content_block"].get("id") == tid]


def test_a_held_edit_announces_its_file_before_its_row(start, tmp_path):
    """The edit's row waits for `completed` (D14), but Cursor names the file
    within a second and then goes quiet while it writes it: the page needs a
    live "working on <file>" in that gap. That is a bare
    `content_block_start` (no `assistant` row), so the transcript still gets
    the call once, complete."""
    path = str(tmp_path / "proj" / "note.txt")
    s = start(fake_args=["--edit-new", path])
    s.say("write")
    s.wait_results(1)
    rows = s.out()
    tid = "call-edit-1"
    first_assistant = next(i for i, r in enumerate(rows) if r.get("type") == "assistant"
                           and any(b.get("id") == tid for b in r["message"]["content"]))
    starts = _starts(rows, tid)
    # the announce, then the one `_raise_tool` emits right before its row
    assert len(starts) == 2 and starts[0] < starts[1] < first_assistant
    assert not any(r.get("type") == "assistant" for r in rows[starts[0]:starts[1]])
    early = rows[starts[0]]["event"]["content_block"]
    # the early name depends on whether the file exists yet when the host handles
    # the update, which races with the CLI writing it; only the path is promised
    assert early["name"] in ("Write", "Edit") and early["input"]["file_path"] == path
    holders = [r for r in rows if r.get("type") == "assistant"
               and any(b.get("id") == tid for b in r["message"]["content"])]
    assert len(holders) == 1
    assert holders[0]["message"]["content"][0]["input"]["content"] == "hello"


def test_a_shell_call_is_not_announced_early(start):
    """A shell call's early `rawInput` is a truncated command: no bare
    `content_block_start`, only the one that rides with its row."""
    s = start(fake_args=["--tool"])
    s.say("list")
    s.wait_results(1)
    assert len(_starts(s.out(), "call-1")) == 1


def test_normalize_diff_repairs_cursors_new_file_shape(host_mod):
    fixed = host_mod._normalize_diff({"type": "diff", "path": "/p/a.txt",
                                      "oldText": "-- /dev/null",
                                      "newText": "++ b//p/a.txt\nline1\nline2"})
    assert fixed["oldText"] == "" and fixed["newText"] == "line1\nline2"
    plain = {"type": "diff", "path": "a", "oldText": "hello", "newText": "bye"}
    assert host_mod._normalize_diff(plain) is plain
