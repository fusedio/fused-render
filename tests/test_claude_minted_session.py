"""The session id is MINTED BY `_start`, not waited for.

Claude Code announces the session it minted in the first `system` row of
`out.jsonl` — two to four seconds after the process starts. Everything that
identifies a conversation used to wait on that row: the Tasks row a send should
appear in, the mark that says a turn just started, a page re-attaching to its
own run. So the first seconds of every new chat were seconds in which this app
could not say what it had just started.

`_start` mints a uuid4 instead, passes it to the CLI as `--session-id`, writes
it into `meta.json` and hands it back to the caller. Three facts follow, and
each has a test below: the argv carries it (and never carries `--resume`
alongside), the run dir names its own session from the instant it exists, and
the very first poll already answers with it.

A RESUME IS UNTOUCHED. It already names the conversation, `--session-id` would
contradict `--resume`, and the CLI refuses that pair — so nothing is minted and
nothing is passed.
"""
import importlib.util
import json
import os
import re

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")

_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_mint_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load("agent")
    mod.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(mod, "_claude_bin", lambda: "/bin/claude")
    return mod


class _HostProc:
    """Stands in for the session_host.py process `_start` Popens. The CLI's
    argv is built inside THAT process from the JSON request written to this
    stub's stdin, so a test that wants the argv captures the request and makes
    the same `_claude_argv` call `session_host.main()` makes."""

    pid = 4242

    class _Stdin:
        def __init__(self, seen):
            self._seen = seen
            self._buf = b""

        def write(self, data):
            self._buf += data

        def close(self):
            self._seen["req"] = json.loads(self._buf.decode("utf-8"))

    def __init__(self, seen):
        self.stdin = _HostProc._Stdin(seen)


@pytest.fixture
def started(agent, tmp_path, monkeypatch):
    """Run `_start` and hand back `(result, req, run_dir)`."""
    project = tmp_path / "proj"
    project.mkdir()
    seen = {}
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: _HostProc(seen))

    def start(session_id=""):
        out = agent._start(str(project), "pull today's news", session_id, "", "")
        assert "error" not in out, out
        return out, seen["req"], os.path.join(agent.RUNS, out["run_id"])

    return start


def _argv(agent, req, run_dir):
    """The same call `session_host.main()` makes."""
    return agent._claude_argv(
        run_dir, req["pane"], req["cli_mode"] or None, req["session_id"],
        req["model"], req["effort"], req["extra_read_dirs"], req["file"],
        req.get("new_session_id", ""))


# ------------------------------------------------------------- a fresh chat


def test_a_fresh_send_returns_the_session_it_will_run_in(started):
    """THE POINT OF THE WHOLE CHANGE. The caller learns the conversation's id in
    the same breath it starts it — which is what lets the page say "a turn just
    started HERE" (`POST /api/tasks/running`) before the CLI has written a
    byte."""
    out, _req, _run_dir = started()
    assert _UUID4.match(out["session_id"]), out["session_id"]
    assert out["run_id"]


def test_the_minted_id_is_on_disk_before_the_cli_is_even_spawned(started):
    """`meta.json` is written by `_start` itself, so the run dir names its own
    session from the instant it exists. `resumed_from` stays "" — the two are
    the ANSWER and the QUESTION, and a fresh chat has no question."""
    out, _req, run_dir = started()
    meta = json.loads(open(os.path.join(run_dir, "meta.json"),
                           encoding="utf-8").read())
    assert meta["session_id"] == out["session_id"]
    assert meta["resumed_from"] == ""


def test_the_cli_is_told_the_id_and_is_not_asked_to_resume(agent, started):
    """`--session-id <uuid>` and no `--resume`. The two are mutually exclusive
    for the CLI, and a run that will not spawn is a far worse failure than the
    lag this closes."""
    out, req, run_dir = started()
    assert req["new_session_id"] == out["session_id"]
    cmd = _argv(agent, req, run_dir)
    assert cmd[cmd.index("--session-id") + 1] == out["session_id"]
    assert "--resume" not in cmd


def test_the_run_names_its_session_before_any_poll_has_run(agent, started):
    """`_run_own_session`'s third source, and the only one that exists in the
    seconds this feature is about: no poll has written the `session` file and
    the CLI has announced nothing, so both older sources answer ""."""
    out, _req, run_dir = started()
    meta = json.loads(open(os.path.join(run_dir, "meta.json"),
                           encoding="utf-8").read())
    assert not os.path.exists(os.path.join(run_dir, "session"))
    assert agent._session_from_out(run_dir) == ""
    assert agent._run_own_session(run_dir, meta) == out["session_id"]


def test_the_live_run_lookup_finds_the_chat_by_its_minted_id(agent, started,
                                                             monkeypatch):
    """A page re-attaching to its own run asks `_live_run` by session id. Before
    the mint that lookup answered "" for every brand-new chat until the CLI
    spoke — the exact window in which leaving and coming back loses the run."""
    out, _req, run_dir = started()
    meta = json.loads(open(os.path.join(run_dir, "meta.json"),
                           encoding="utf-8").read())
    monkeypatch.setattr(agent, "_alive", lambda d: True)
    monkeypatch.setattr(agent, "_turn_state", lambda d: (True, False))
    found = agent._live_run(meta["file"], out["session_id"])
    assert found["run_id"] == os.path.basename(run_dir)


# ---------------------------------------------------------------- a resume


def test_a_resume_mints_nothing_and_still_resumes(agent, started):
    """The caller already named the conversation. Nothing is minted, nothing is
    written to meta beside `resumed_from`, and the argv carries `--resume`
    alone."""
    out, req, run_dir = started("sess-old")
    assert out["session_id"] == "sess-old"
    assert req["new_session_id"] == ""
    meta = json.loads(open(os.path.join(run_dir, "meta.json"),
                           encoding="utf-8").read())
    assert meta["resumed_from"] == "sess-old"
    assert "session_id" not in meta
    cmd = _argv(agent, req, run_dir)
    assert cmd[cmd.index("--resume") + 1] == "sess-old"
    assert "--session-id" not in cmd


# ------------------------------------------------------------------ the poll


def _write_out(run_dir, rows):
    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_the_first_poll_already_names_the_session(agent, started):
    """Nothing in `out.jsonl` carries an id yet — the CLI has not got to its
    first `system` row — and the poll still answers with the conversation's
    name, seeded off meta."""
    out, _req, run_dir = started()
    _write_out(run_dir, [{"type": "stream_event"}])
    data = agent._poll(os.path.basename(run_dir))
    assert data["session_id"] == out["session_id"]


def test_a_seeded_id_does_not_latch_and_the_clis_own_word_wins(agent, started,
                                                               monkeypatch):
    """SEEDED, NEVER PREFERRED. The `recorded` marker is one-shot and a seed has
    not earned it: if the CLI ever answers with a different id — an old binary
    that ignores `--session-id` — that is the id the run records, and the
    `session` file ends up naming the session that actually exists."""
    _out, _req, run_dir = started()
    run_id = os.path.basename(run_dir)
    # The run has to look ALIVE: `_poll` refuses to record a session for a run
    # it has just declared dead, and this stub never spawned a real process.
    monkeypatch.setattr(agent, "_alive", lambda d: True)
    _write_out(run_dir, [{"type": "stream_event"}])
    agent._poll(run_id)
    assert not os.path.exists(os.path.join(run_dir, "recorded")), \
        "a seed must leave the one-shot marker unclaimed"

    _write_out(run_dir, [{"type": "system", "session_id": "cli-minted-this"}])
    data = agent._poll(run_id)
    assert data["session_id"] == "cli-minted-this"
    assert open(os.path.join(run_dir, "session"),
                encoding="utf-8").read().strip() == "cli-minted-this"
    assert os.path.exists(os.path.join(run_dir, "recorded"))
