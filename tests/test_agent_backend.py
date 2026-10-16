"""agent.py's side of the second agent backend (docs/COPILOT_BACKEND.md, WS-C).

What is pinned here, per contract section:

  * D2 / invariant 8 — a session's backend is the ROOT its transcript sits
    under (or the record `_start` wrote), never the request's say-so: a
    resume naming the other backend is ignored, a fresh chat takes what it
    asked for;
  * D17 / invariant 10 — `_start` reads prefs.json itself and refuses a
    disabled agent before touching disk; the router 400s the same body;
  * §3 — `_copilot_argv` composition, the generated plugin root (D8), the
    pane-only mcp.json with `--no-approve` (D4), `--add-dir` / `--allow-tool`
    (D9), `--allow-tool write` for acceptEdits (D5);
  * D7 / §7 — `_send` respawns a Copilot session on a model change or an
    acceptEdits change, switches prompt<->auto live, refuses plan;
  * §7 caps — snapshots answer an empty timeline / `unsupported`, background
    tasks answer `unsupported`;
  * D12 — `copilot --resume=<sid>` as the terminal command, "Copilot: …"
    commit subject label;
  * D13 — the error markers are COPIES of `agent_backends/*.py`'s and are
    pinned equal, so neither side can drift;
  * D3 — `_cli_sessions` lists both roots, each row saying which.
"""
import importlib.util
import inspect
import json
import os
import pathlib
import re
import sys
import textwrap
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_agent
from fused_render.agent_backends import claude as backend_claude
from fused_render.agent_backends import copilot as backend_copilot
from fused_render.claude_agent import gate
from fused_render.server import create_app
from fused_render.server.routers import claude_agent as router

AGENT_PATH = os.path.join("fused_render", "claude_agent", "agent.py")
FUSED = {"X-Fused": "1"}
SID_CLAUDE = "11111111-1111-4111-8111-111111111111"
SID_COPILOT = "22222222-2222-4222-8222-222222222222"


def _load_agent():
    spec = importlib.util.spec_from_file_location("claude_agent_backend_test",
                                                  AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Both homes the module reads: the UNNESTED one (`FUSED_RENDER_HOME`,
    where the Copilot root and the session-settings store live) and the
    branch-resolved one (`FUSED_RENDER_HOME_DIR`, where prefs.json lives).
    Separate dirs, so a test that puts a file in the wrong one fails."""
    unnested = tmp_path / "home"
    resolved = tmp_path / "home-branch"
    unnested.mkdir()
    resolved.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(unnested))
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(resolved))
    for name in ("FUSED_RENDER_SKILL_PLUGIN_DIR", "FUSED_RENDER_WORKBENCH_PLUGIN_DIR",
                 "FUSED_RENDER_FUSED_CLI_DIR", "FUSED_RENDER_ORIGIN",
                 "FUSED_RENDER_COPILOT_BIN", "FUSED_RENDER_CLAUDE_BIN"):
        monkeypatch.delenv(name, raising=False)
    return {"unnested": str(unnested), "resolved": str(resolved)}


@pytest.fixture()
def agent(tmp_path, home, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    monkeypatch.setattr(mod, "SHOTS", str(tmp_path / "shots"))
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", str(claude_dir))
    monkeypatch.setattr(mod, "PROJECTS", str(claude_dir / "projects"))
    # `gh` is never run under test; the token is whatever the test says.
    monkeypatch.setattr(mod, "_gh_token", lambda *a, **k: "")
    return mod


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f), str(d)


def _prefs(home, **keys):
    with open(os.path.join(home["resolved"], "prefs.json"), "w") as fh:
        json.dump(keys, fh)


def _said(text, cwd):
    return {"type": "user", "cwd": cwd, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def _transcript(agent, backend, workdir, sid, rows):
    """A transcript under `backend`'s root, in `workdir`'s bucket."""
    root = dict(agent._transcript_roots())[backend]
    proj = os.path.join(root, agent._munge(workdir))
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, sid + ".jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _stub_host(tmp_path):
    """A host that records the request `_start` wrote and exits — enough to
    see what `_start` decided without a CLI."""
    path = tmp_path / "stub_host.py"
    path.write_text(textwrap.dedent("""\
        import json, os, sys
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        with open(os.path.join(req["run_dir"], "req.json"), "w") as fh:
            json.dump(req, fh)
    """))
    return str(path)


def _copilot_bin(tmp_path, monkeypatch):
    b = tmp_path / "bin" / "copilot"
    b.parent.mkdir(exist_ok=True)
    b.write_text("#!/bin/sh\nexit 0\n")
    b.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_COPILOT_BIN", str(b))
    return str(b)


def _req_of(agent, res):
    import time
    run_dir = os.path.join(agent.RUNS, res["run_id"])
    for _ in range(200):
        try:
            with open(os.path.join(run_dir, "req.json")) as fh:
                return json.load(fh), run_dir
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError("the stub host never wrote the request")


# ---------------------------------------------------------- pinned mirrors

def test_the_error_markers_and_labels_mirror_agent_backends(agent):
    """agent.py cannot import the package, so it carries copies (D13)."""
    assert agent._ACCOUNT_ERROR_MARKERS["claude"] == backend_claude.CLAUDE.account_error_markers
    # Claude's limit markers are the two `_account_error` matched before a
    # second backend existed, byte for byte.
    assert agent._LIMIT_ERROR_MARKERS["claude"] == ("usage limit reached", "session limit")
    assert agent._LIMIT_ERROR_MARKERS["claude"] == backend_claude.CLAUDE.limit_error_markers
    assert agent._ACCOUNT_ERROR_MARKERS["copilot"] == backend_copilot.COPILOT.account_error_markers
    assert agent._LIMIT_ERROR_MARKERS["copilot"] == backend_copilot.COPILOT.limit_error_markers
    assert agent._BACKEND_LABELS == {"claude": backend_claude.CLAUDE.label,
                                     "copilot": backend_copilot.COPILOT.label}
    assert set(agent.BACKENDS) == set(agent._BACKEND_LABELS)


def test_account_errors_are_worded_per_backend(agent):
    assert "Copilot" in agent._account_error("auth_required", "copilot")
    assert "Claude" in agent._account_error("/login", "claude")
    assert "Claude" in agent._account_error("/login")      # default backend
    assert "budget" in agent._account_error("premium request quota hit", "copilot")
    # Claude's wording never fires on Copilot's markers, and vice versa.
    assert agent._account_error("ai credits exhausted", "claude") == "ai credits exhausted"
    assert agent._account_error("invalid api key", "copilot") == "invalid api key"


def test_copilot_account_text_names_copilots_fix_and_claudes_is_unchanged(agent):
    """D18: a Copilot run never reads Claude's sign-in fix or link."""
    for raw in ("auth_required", "premium request quota hit"):
        text = agent._account_error(raw, "copilot")
        assert "claude" not in text.lower()
        # No guide link at all: the site has no Copilot section to open.
        assert "http" not in text and "Help:" not in text
    assert agent._account_error("auth_required", "copilot") == (
        "GitHub Copilot isn't signed in. Sign in from Preferences → Agents "
        "(or run `copilot login` in a terminal), then start a new chat here. "
        "(auth_required)")
    assert agent._account_error("premium request quota hit", "copilot") == (
        "Your GitHub Copilot plan's request budget was reached. Wait for it to "
        "reset, or upgrade the plan, then try again. (premium request quota hit)")
    # Claude's, byte for byte.
    assert agent._account_error("/login") == (
        "Claude Code isn't logged in. Open a terminal, run `claude`, type /login "
        "and finish the sign-in, then start a new chat here. Help: "
        "https://render.fused.io/#troubleshooting-login (/login)")
    assert agent._account_error("usage limit reached") == (
        "Your Claude plan's usage limit was reached. Wait for it to reset, or "
        "upgrade the plan, then try again. Help: "
        "https://render.fused.io/#troubleshooting-limit (usage limit reached)")


_CLAUDE_ONLY = re.compile(r"\bClaude\b|CLAUDE\.md|AskUserQuestion|ExitPlanMode|"
                          r"EnterWorktree|\bSkill\b|mcp__")


def test_the_copilot_agent_file_never_speaks_claude(agent, home, tmp_path, monkeypatch):
    """fused.agent.md is Copilot's system prompt: no "you are Claude", no
    CLAUDE.md, no Claude-only tool names — for every target shape, with every
    machine-wide note switched on. Claude's own prompt keeps its sentence."""
    canvases = tmp_path / "canvases"
    clone = canvases / "c1"
    clone.mkdir(parents=True)
    (clone / "index.html").write_text("<html></html>")
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(canvases))
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9")
    monkeypatch.setattr(agent, "_custom_env", lambda origin, file: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    page = tmp_path / "page.html"
    page.write_text("<html></html>")
    for i, (target, pane) in enumerate(((str(page), True), (str(clone), True),
                                         (str(plain), False))):
        run_dir = tmp_path / ("run%d" % i)
        run_dir.mkdir()
        md = (pathlib.Path(agent._copilot_plugin_dir(str(run_dir), target, pane))
              / "agents" / "fused.agent.md").read_text()
        assert not _CLAUDE_ONLY.search(md), (target, _CLAUDE_ONLY.search(md))
    # Claude's prompt is byte-identical: the sentence is still there for it.
    note = agent._fused_cli_note()
    assert note.endswith(" See that folder's CLAUDE.md for the details.")
    assert agent._fused_cli_note("copilot") == note[:-len(agent._CANVAS_CLAUDE_MD)]
    assert agent._appended_prompt(str(clone), True).endswith(
        note + agent._origin_note())


def test_the_command_menu_is_per_backend(agent, target, tmp_path, monkeypatch):
    """A Copilot chat's menu is what Copilot announced — never Claude's
    `.claude/` commands or its built-ins (whose /init writes a CLAUDE.md) —
    and a Copilot init row does not overwrite the folder's Claude menu."""
    file, workdir = target
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    cmd_dir = tmp_path / "proj" / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "deploy.md").write_text("Deploy it\n")
    claude_menu = agent._commands(file)
    names = {c["name"] for c in claude_menu["commands"]}
    assert {"deploy", "init"} <= names
    # Nothing announced yet: a Copilot menu is empty, not Claude's.
    assert agent._commands(file, "copilot")["commands"] == []
    # A Copilot run's init row lands in Copilot's own state.
    run_dir = os.path.join(agent.RUNS, "r-cmd")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": file, "message": "m", "backend": "copilot"}, fh)
    agent._capture_commands(run_dir, {"type": "system", "subtype": "init",
                             "slash_commands": ["plan", "init"]})
    menu = agent._commands(file, "copilot")
    assert [c["name"] for c in menu["commands"]] == ["plan", "init"]
    assert all(c["description"] == "" and c["source"] == "cli" for c in menu["commands"])
    assert menu["agents"] == []
    # The session names its own backend whatever `backend` says.
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    assert agent.main("commands", file=file, session_id=SID_COPILOT,
                      backend="claude") == menu
    # Claude's menu is untouched by the Copilot row.
    assert agent._commands(file) == claude_menu


# ---------------------------------------------------- backend resolution (D2)

def test_the_transcript_root_names_the_backend(agent, target):
    _file, workdir = target
    p_claude = _transcript(agent, "claude", workdir, SID_CLAUDE, [_said("a", workdir)])
    p_copilot = _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    assert agent._transcript_backend(p_claude) == "claude"
    assert agent._transcript_backend(p_copilot) == "copilot"
    assert agent._session_transcript_path(workdir, SID_COPILOT) == p_copilot
    assert agent._backend_for_session(SID_COPILOT, workdir) == "copilot"
    assert agent._backend_for_session(SID_CLAUDE, workdir) == "claude"
    # No transcript anywhere: Claude, which every pre-existing session is.
    assert agent._backend_for_session("33333333-3333-4333-8333-333333333333", workdir) == "claude"
    # A recorded hint wins over the (missing) root.
    assert agent._backend_for_session("44444444-4444-4444-8444-444444444444",
                                      workdir, hint="copilot") == "copilot"
    assert agent._backend_for_session(SID_COPILOT, workdir, hint="junk") == "copilot"


def test_the_record_carries_the_backend_once(agent):
    agent._record_settings(SID_COPILOT, backend="copilot")
    assert agent._session_backend(SID_COPILOT) == "copilot"
    # The 2-tuple reader keeps its shape for the callers that pin it.
    assert agent._session_settings(SID_COPILOT) == ("", "")
    agent._record_settings(SID_COPILOT, model="gpt-5.4", backend="nonsense")
    assert agent._session_backend(SID_COPILOT) == "copilot"
    assert agent._session_settings(SID_COPILOT) == ("gpt-5.4", "")


def test_a_resume_ignores_the_requests_backend(agent, target, home, tmp_path,
                                               monkeypatch):
    """Invariant 8: the session is a Copilot one; the request says Claude;
    the Copilot host is what spawns, and `backend` rides meta + the record."""
    file, workdir = target
    _prefs(home, copilot_enabled=True)
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    _copilot_bin(tmp_path, monkeypatch)
    stub = _stub_host(tmp_path)
    monkeypatch.setitem(agent._HOST_FILES, "copilot", stub)
    monkeypatch.setitem(agent._HOST_FILES, "claude", str(tmp_path / "never.py"))
    res = agent._start(file, "again", SID_COPILOT, "", "", "prompt",
                       has_pane=False, backend="claude")
    assert res["backend"] == "copilot" and res["session_id"] == SID_COPILOT
    req, run_dir = _req_of(agent, res)
    assert req["backend"] == "copilot"
    assert req["session_id"] == SID_COPILOT and req["new_session_id"] == ""
    # A resume carries no --session-id; the host does session/load.
    assert "--session-id" not in req["argv"]
    with open(os.path.join(run_dir, "meta.json")) as fh:
        assert json.load(fh)["backend"] == "copilot"
    assert agent._session_backend(SID_COPILOT) == "copilot"


def test_a_fresh_copilot_chat_learns_its_id_from_the_stream(
        agent, target, home, tmp_path, monkeypatch):
    """`copilot --acp` refuses `--session-id` (verified 2026-10-16), so a
    fresh Copilot chat mints nothing: `_start` answers `session_id: ""`, and
    the first poll that sees the host's init row records Copilot's own id —
    the `session` file, the marker, and the settings record `_start` could
    not write."""
    file, workdir = target
    _prefs(home, copilot_enabled=True)
    _copilot_bin(tmp_path, monkeypatch)
    monkeypatch.setitem(agent._HOST_FILES, "copilot", _stub_host(tmp_path))
    res = agent._start(file, "hi", "", "gpt-5.4", "high", "acceptEdits",
                       has_pane=False, backend="copilot")
    assert res["backend"] == "copilot" and res["session_id"] == ""
    req, run_dir = _req_of(agent, res)
    assert req["backend"] == "copilot"
    assert req["session_id"] == "" and req["new_session_id"] == ""
    assert req["argv"][1:3] == ["--acp", "--no-auto-update"]
    assert "--session-id" not in req["argv"]
    assert req["cli_mode"] == "acceptEdits"
    with open(os.path.join(run_dir, "meta.json")) as fh:
        meta = json.load(fh)
    assert "session_id" not in meta and meta["backend"] == "copilot"
    # The host's init row names Copilot's own id; the poll records it.
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        for row in ({"type": "system", "subtype": "init", "session_id": SID_COPILOT},
                    {"type": "result", "subtype": "success", "is_error": False,
                     "result": "ok", "session_id": SID_COPILOT}):
            fh.write(json.dumps(row) + "\n")
    assert agent._session_backend(SID_COPILOT) == ""     # nothing named it yet
    # The FIRST poll that reports the id has already recorded it as Copilot:
    # the server resolves an id's agent off this record and falls back to
    # Claude, so it must never see the id first.
    assert agent._poll(res["run_id"])["session_id"] == SID_COPILOT
    assert agent._session_backend(SID_COPILOT) == "copilot"
    with open(os.path.join(run_dir, "session")) as fh:
        assert fh.read() == SID_COPILOT
    assert os.path.exists(os.path.join(run_dir, "recorded"))
    assert agent._session_backend(SID_COPILOT) == "copilot"
    assert agent._session_settings(SID_COPILOT) == ("gpt-5.4", "high")


def _fresh_copilot_run(agent, file, name="20261016-130000-aaa", rows=()):
    """A fresh Copilot chat's run dir as `_start` leaves it: meta with no
    `session_id`, a live host, and whatever the host has streamed."""
    run_dir = os.path.join(agent.RUNS, name)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": file, "message": "hi", "resumed_from": "",
                   "mode": "prompt", "backend": "copilot",
                   "model": "gpt-5.4", "effort": ""}, fh)
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "session_id": "", "file": file,
                   "mode": None, "model": "gpt-5.4", "effort": "",
                   "read_dirs": [], "backend": "copilot"}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return run_dir


def test_an_errored_first_turn_still_records_before_naming(agent, target):
    """The `recorded` latch skips an errored poll; the settings record does
    not — the id goes out on that poll all the same."""
    file, _ = target
    run_dir = _fresh_copilot_run(agent, file, rows=(
        {"type": "system", "subtype": "init", "session_id": SID_COPILOT},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "result": "boom", "session_id": SID_COPILOT}))
    p = agent._poll(os.path.basename(run_dir))
    assert p["session_id"] == SID_COPILOT and p["error"]
    assert agent._session_backend(SID_COPILOT) == "copilot"
    assert agent._session_settings(SID_COPILOT) == ("gpt-5.4", "")
    assert not os.path.exists(os.path.join(run_dir, "recorded"))


def test_cancel_names_a_fresh_copilot_id_only_once_recorded(agent, target, monkeypatch):
    file, _ = target
    run_dir = _fresh_copilot_run(agent, file, rows=(
        {"type": "system", "subtype": "init", "session_id": SID_COPILOT},))
    monkeypatch.setattr(agent, "_cancel", lambda run_id, **kw: {"ok": True})
    out = agent.main("cancel", run_id=os.path.basename(run_dir))
    assert out["session_id"] == SID_COPILOT
    assert agent._session_backend(SID_COPILOT) == "copilot"


def test_a_fresh_copilot_run_is_found_by_file_then_by_id(agent, target):
    """Before its id is known a fresh Copilot run answers to its FILE; once
    the host has said the id, to that too — `_live_host` (sends) and
    `_live_run` (re-attach) alike, and neither hands the id out."""
    file, _ = target
    run_dir = _fresh_copilot_run(agent, file, rows=(
        {"type": "user", "message": {"role": "user", "content": "hi"}},))
    name = os.path.basename(run_dir)
    assert agent._live_host(file) == {"run_id": name}
    assert agent._live_host(file, SID_COPILOT) == {"run_id": ""}
    assert agent._live_run(file)["run_id"] == name
    assert agent._live_run(file, SID_COPILOT)["run_id"] == ""
    # The host's init row lands: found by id, before any poll.
    _out(run_dir, [{"type": "system", "subtype": "init", "session_id": SID_COPILOT},
                   {"type": "user", "message": {"role": "user", "content": "hi"}}])
    assert agent._live_host(file, SID_COPILOT) == {"run_id": name}
    assert agent._live_run(file, SID_COPILOT) == {"run_id": name, "backend": "copilot"}


def test_send_keeps_the_session_id_the_copilot_host_wrote(agent, target, home,
                                                         monkeypatch):
    """`_send` re-records model/mode in host.json while the Copilot host may
    be rewriting it with its own session id: re-read, two keys, replace."""
    file, _ = target
    _prefs(home, copilot_enabled=True)
    run_dir = _fresh_copilot_run(agent, file)
    path = os.path.join(run_dir, "host.json")
    real = agent._write_control_request

    def host_handshakes_meanwhile(*a, **k):
        real(*a, **k)
        with open(path) as fh:
            host = json.load(fh)
        host["session_id"] = SID_COPILOT
        with open(path, "w") as fh:
            json.dump(host, fh)
    monkeypatch.setattr(agent, "_write_control_request", host_handshakes_meanwhile)
    assert agent._send(os.path.basename(run_dir), "m", permission_mode="auto") == {"sent": True}
    with open(path) as fh:
        host = json.load(fh)
    assert host["session_id"] == SID_COPILOT and host["mode"] == "auto"
    assert not [n for n in os.listdir(run_dir) if n.endswith(".tmp")]


def test_a_later_copilot_poll_still_names_the_session(agent, tmp_path):
    """A later turn's rows need not repeat the init row: the poll answers
    with the id the run already recorded, never ""."""
    run_dir = _live_run(agent, tmp_path)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m", "resumed_from": "",
                   "backend": "copilot"}, fh)
    with open(os.path.join(run_dir, "session"), "w") as fh:
        fh.write(SID_COPILOT)
    open(os.path.join(run_dir, "recorded"), "w").close()
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hi"}}}) + "\n")
    assert agent._poll("run-1")["session_id"] == SID_COPILOT


def test_a_fresh_claude_chat_still_mints_its_id(agent, target, tmp_path, monkeypatch):
    file, _ = target
    monkeypatch.setitem(agent._HOST_FILES, "claude", _stub_host(tmp_path))
    res = agent._start(file, "hi", "", "", "", "", has_pane=False)
    assert res["backend"] == "claude" and res["session_id"]
    req, run_dir = _req_of(agent, res)
    assert req["new_session_id"] == res["session_id"] and "argv" not in req
    with open(os.path.join(run_dir, "meta.json")) as fh:
        assert json.load(fh)["session_id"] == res["session_id"]


def test_the_host_request_keys_are_session_hosts_plus_two(agent, target, home,
                                                          tmp_path, monkeypatch):
    """What copilot_host.py codes against: session_host's keys + argv + backend."""
    file, _workdir = target
    _prefs(home, copilot_enabled=True)
    _copilot_bin(tmp_path, monkeypatch)
    monkeypatch.setitem(agent._HOST_FILES, "copilot", _stub_host(tmp_path))
    res = agent._start(file, "hi", "", "", "", "", has_pane=False, backend="copilot")
    req, _ = _req_of(agent, res)
    assert set(req) == {"agent", "run_dir", "file", "cwd", "pane", "cli_mode",
                        "session_id", "model", "effort", "extra_read_dirs",
                        "new_session_id", "argv", "backend"}


# ------------------------------------------------------- the gate (D17, #10)

def test_the_prefs_gate_mirrors_prefs_py(agent, home):
    assert agent._backend_enabled("claude")          # no file: Claude on
    assert not agent._backend_enabled("copilot")     # …Copilot off
    _prefs(home, copilot_enabled=True)
    assert agent._backend_enabled("copilot")
    _prefs(home, claude_enabled=False, copilot_enabled=True)
    assert not agent._backend_enabled("claude")
    # Both cleared by hand reads as Claude on — `registry.enabled_ids` says so.
    _prefs(home, claude_enabled=False, copilot_enabled=False)
    assert agent._backend_enabled("claude")
    assert not agent._backend_enabled("nope")
    # The file is read from the branch-resolved home, not the unnested one.
    os.remove(os.path.join(home["resolved"], "prefs.json"))
    with open(os.path.join(home["unnested"], "prefs.json"), "w") as fh:
        json.dump({"copilot_enabled": True}, fh)
    assert not agent._backend_enabled("copilot")


def test_a_disabled_agent_is_refused_before_a_run_dir_exists(agent, target, home):
    file, _ = target
    res = agent._start(file, "hi", "", "", "", "", has_pane=False, backend="copilot")
    assert res == {"error": "Copilot is turned off in Preferences",
                   "reason": "backend_disabled", "backend": "copilot"}
    assert os.listdir(agent.RUNS) == []
    assert "unknown agent backend" in agent._start(
        file, "hi", "", "", "", "", has_pane=False, backend="gemini")["error"]
    _prefs(home, copilot_enabled=True)
    res = agent._start(file, "hi", "", "", "", "plan", has_pane=False, backend="copilot")
    assert res["error"] == "plan mode is not available for Copilot"
    assert os.listdir(agent.RUNS) == []


def test_a_resumed_session_of_a_disabled_agent_is_refused_too(agent, target, home):
    """The gate reads the SESSION's backend, so a resume body that names
    nothing (or Claude) still cannot wake a Copilot chat while it is off."""
    file, workdir = target
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    res = agent._start(file, "again", SID_COPILOT, "", "", "", has_pane=False)
    assert res["reason"] == "backend_disabled" and res["backend"] == "copilot"


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _recording_main(seen):
    """A stand-in `agent.main` that records what reached it."""
    def main(action="", file="", backend="", permission_mode="", message="",
             run_id="", session_id=""):
        seen.append({"action": action, "backend": backend})
        return {"run_id": "r-1"}
    main.__signature__ = inspect.signature(main)
    return main


@pytest.fixture()
def stub(monkeypatch):
    seen = []
    main = _recording_main(seen)
    monkeypatch.setattr(claude_agent, "agent_module",
                        lambda: types.SimpleNamespace(main=main))
    monkeypatch.setattr(gate, "_folder_busy", lambda params, body=None: "")
    monkeypatch.setattr(gate, "_file_owner",
                        lambda params, envelope, body=None, late=False: None)
    return seen


@pytest.mark.parametrize("action", ["start", "send"])
def test_the_router_400s_a_disabled_backend(client, stub, monkeypatch, action):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": False})
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": action, "file": "/w/a.html", "message": "m",
                          "backend": "copilot", "run_id": "r"})
    assert r.status_code == 400
    assert r.json() == {"error": "Copilot is turned off in Preferences",
                        "reason": "backend_disabled", "backend": "copilot"}
    assert stub == [], "a refused request never reaches the handler"
    # Claude on, Copilot on: both pass; nothing named passes.
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": True})
    for backend in ("claude", "copilot", ""):
        r = client.post("/api/claude/agent", headers=FUSED,
                        json={"action": action, "file": "/w/a.html",
                              "message": "m", "backend": backend, "run_id": "r"})
        assert r.status_code == 200, backend
    assert [s["backend"] for s in stub] == ["claude", "copilot", ""]


def test_the_router_400s_plan_mode_for_copilot(client, stub, monkeypatch):
    from fused_render.shell import prefs
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": True})
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "start", "file": "/w/a.html", "message": "m",
                          "backend": "copilot", "permission_mode": "plan"})
    assert r.status_code == 400
    assert r.json()["reason"] == "unsupported"
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "start", "file": "/w/a.html", "message": "m",
                          "backend": "claude", "permission_mode": "plan"})
    assert r.status_code == 200
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "poll", "run_id": "r", "backend": "nope"})
    assert r.status_code == 200, "the gate is on start/send only"


# ------------------------------------------------------------ the spawn (§3)

def _skills(tmp_path, name, skills):
    root = tmp_path / name
    for s in skills:
        (root / "skills" / s).mkdir(parents=True)
        (root / "skills" / s / "SKILL.md").write_text("# " + s)
    return str(root)


def test_copilot_argv_composition(agent, target, home, tmp_path, monkeypatch):
    file, workdir = target
    binary = _copilot_bin(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_RENDER_SKILL_PLUGIN_DIR", _skills(tmp_path, "fused-plugin", ["a", "b"]))
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "cli"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argv = agent._copilot_argv(str(run_dir), True, "acceptEdits", "", "gpt-5.4",
                               "high", [str(tmp_path / "extra")], file)
    assert argv[0] == binary
    assert argv[1:3] == ["--acp", "--no-auto-update"]
    # `--acp` refuses `--session-id`: never emitted, fresh (here) or resume
    # (the no-pane test below).
    assert "--session-id" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.4"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    plugin = argv[argv.index("--plugin-dir") + 1]
    assert plugin == str(run_dir / "plugin")
    assert argv[argv.index("--agent") + 1] == "fused:fused"
    assert argv[argv.index("--additional-mcp-config") + 1] == "@" + str(run_dir / "mcp.json")
    adds = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert adds == [agent.SHOTS, str(tmp_path / "extra")]
    allows = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow-tool"]
    assert allows == ["shell(fused:*)", "write"]
    # No Claude-only plumbing leaks in.
    for flag in ("-p", "--resume", "--append-system-prompt", "--permission-prompt-tool",
                 "--permission-mode", "--mcp-config", "--effort"):
        assert flag not in argv
    # The generated plugin (D8).
    with open(run_dir / "plugin" / "plugin.json") as fh:
        assert json.load(fh)["name"] == "fused"
    agent_md = (run_dir / "plugin" / "agents" / "fused.agent.md").read_text()
    assert agent_md.startswith("---\nname: fused\n")
    # Same prompt as Claude's --append-system-prompt, with the pane tool in
    # Copilot's MCP spelling (`<server>-<tool>`, §10).
    assert agent._appended_prompt(file, True, "copilot").replace(
        "mcp__fused_approvals__", "fused_approvals-") in agent_md
    assert "fused_approvals-app_state" in agent_md
    assert "mcp__fused_approvals__" not in agent_md
    skills = run_dir / "plugin" / "skills"
    assert skills.is_symlink() and sorted(os.listdir(skills)) == ["a", "b"]
    # mcp.json: the pane tools only (D4) — `--no-approve` on the server argv.
    with open(run_dir / "mcp.json") as fh:
        server = json.load(fh)["mcpServers"]["fused_approvals"]
    assert "--no-approve" in server["args"]
    assert agent._state_dir(str(run_dir)) in server["args"]


def test_copilot_argv_without_a_pane_has_no_mcp_and_no_write_grant(
        agent, target, home, tmp_path, monkeypatch):
    file, _ = target
    _copilot_bin(tmp_path, monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    argv = agent._copilot_argv(str(run_dir), False, None, SID_COPILOT, "", "",
                               [], file)
    assert "--additional-mcp-config" not in argv
    assert not (run_dir / "mcp.json").exists()
    assert "--session-id" not in argv and "--resume" not in argv
    assert "write" not in argv and "--model" not in argv
    assert not (run_dir / "plugin" / "skills").exists()   # no plugin exported


def test_two_plugins_make_skills_a_dir_of_links(agent, home, tmp_path, monkeypatch):
    """A canvas target gets the workbench skills beside fused's — one
    `skills/` name, so it is a real dir of per-skill symlinks."""
    canvases = tmp_path / "canvases"
    clone = canvases / "c1"
    clone.mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(canvases))
    monkeypatch.setenv("FUSED_RENDER_SKILL_PLUGIN_DIR", _skills(tmp_path, "p1", ["a"]))
    monkeypatch.setenv("FUSED_RENDER_WORKBENCH_PLUGIN_DIR", _skills(tmp_path, "p2", ["udf"]))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plugin = agent._copilot_plugin_dir(str(run_dir), str(clone), False)
    skills = os.path.join(plugin, "skills")
    assert os.path.isdir(skills) and not os.path.islink(skills)
    assert sorted(os.listdir(skills)) == ["a", "udf"]
    assert os.path.islink(os.path.join(skills, "udf"))
    # The Claude side agrees on which roots a canvas gets.
    assert len(agent._plugin_argv(str(clone))) == 4


def _health(home, snap):
    """The Copilot health snapshot, where `copilot_health` caches it (the
    branch-resolved shell home)."""
    with open(os.path.join(home["resolved"], "copilot-health.json"), "w") as fh:
        fh.write(snap if isinstance(snap, str) else json.dumps(snap))


def _no_gh(*a, **k):
    raise AssertionError("gh must not run unless the snapshot says method=gh")


def _exe(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


def test_copilot_spawn_env_without_a_gh_snapshot_never_runs_gh(agent, home, monkeypatch):
    """D10 as amended: the token is injected only when the cached snapshot
    says `account.method == "gh"`. No snapshot, a signed-out one, another
    method, or junk → no token, and no `gh` subprocess at all."""
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(agent, "_gh_token", _no_gh)
    monkeypatch.setattr(agent, "_gh_bin", _no_gh)
    assert "COPILOT_GITHUB_TOKEN" not in agent._spawn_env(backend="copilot")
    for snap in ({"account": None}, {"account": {"method": "copilot"}},
                 {"account": {"method": "token"}}, {"account": "gh"}, [], "{nope"):
        _health(home, snap)
        assert "COPILOT_GITHUB_TOKEN" not in agent._spawn_env(backend="copilot"), snap
    # A snapshot under the UNNESTED home is not the shell's: not read.
    os.remove(os.path.join(home["resolved"], "copilot-health.json"))
    with open(os.path.join(home["unnested"], "copilot-health.json"), "w") as fh:
        json.dump({"account": {"method": "gh"}}, fh)
    assert "COPILOT_GITHUB_TOKEN" not in agent._spawn_env(backend="copilot")


def test_copilot_spawn_env_strips_claude_and_adds_the_gh_token(agent, home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/x")
    monkeypatch.setenv("COPILOT_HOME", "/somewhere/copilot-home")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    _health(home, {"account": {"method": "gh", "email": None}})
    seen = []
    monkeypatch.setattr(agent, "_gh_bin", lambda path: seen.append(path) or "/gh")
    monkeypatch.setattr(agent, "_gh_token",
                        lambda gh, env=None: "ghp_test" if gh == "/gh" else "")
    env = agent._spawn_env("chat", backend="copilot")
    assert not [k for k in env if k.startswith("CLAUDE_CODE_")]
    assert env["COPILOT_GITHUB_TOKEN"] == "ghp_test"
    assert env["COPILOT_HOME"] == "/somewhere/copilot-home"   # left alone
    assert "FUSED_CLAUDE_CMD_LOG" not in env     # no terminal mirror (D9)
    # `gh` is resolved against the AUGMENTED PATH, not this process's.
    assert seen and "/opt/homebrew/bin" in seen[0].split(os.pathsep)
    # A token the user exported wins, and `gh` is not even asked.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "mine")
    monkeypatch.setattr(agent, "_gh_bin", _no_gh)
    assert agent._spawn_env(backend="copilot")["COPILOT_GITHUB_TOKEN"] == "mine"
    # `gh` yields nothing: stays absent.
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN")
    monkeypatch.setattr(agent, "_gh_bin", lambda path: "")
    monkeypatch.setattr(agent, "_gh_token", lambda gh, env=None: "")
    assert "COPILOT_GITHUB_TOKEN" not in agent._spawn_env(backend="copilot")
    # Claude's env is untouched by any of this.
    claude_env = agent._spawn_env()
    assert claude_env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] == "1"
    assert "COPILOT_GITHUB_TOKEN" not in claude_env


def test_gh_is_resolved_like_github_setup(agent, home, tmp_path, monkeypatch):
    """Override → the spawn PATH → the app's own `<home>/bin` → the
    package-manager prefixes, as `github_setup.resolve()` does — the copies
    pinned equal to its constants."""
    from fused_render import github_setup
    assert agent._GH_BIN_ENV == github_setup.BIN_ENV
    assert agent._GH_CANDIDATES == github_setup.POSIX_CANDIDATES
    monkeypatch.delenv("FUSED_RENDER_GH_BIN", raising=False)
    monkeypatch.setattr(agent, "_GH_CANDIDATES", ())
    assert agent._gh_bin(str(tmp_path / "empty")) == ""
    own = _exe(os.path.join(home["resolved"], "bin", "gh"))
    assert agent._gh_bin(str(tmp_path / "empty")) == own
    on_path = _exe(str(tmp_path / "brew" / "gh"))
    assert agent._gh_bin(str(tmp_path / "brew")) == on_path
    forced = _exe(str(tmp_path / "forced" / "gh"))
    monkeypatch.setenv("FUSED_RENDER_GH_BIN", forced)
    assert agent._gh_bin(str(tmp_path / "brew")) == forced
    # A stale override is not fallen through (the user named that binary).
    monkeypatch.setenv("FUSED_RENDER_GH_BIN", str(tmp_path / "gone"))
    assert agent._gh_bin(str(tmp_path / "brew")) == ""


def test_the_gh_token_is_cached_per_process(agent, monkeypatch):
    mod = _load_agent()
    calls = []

    class Res:
        returncode = 0
        stdout = "tok\n"
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **k: calls.append((argv, k.get("env"))) or Res())
    mod._GH_TOKEN_CACHE.update(at=0.0, gh="", token="")
    assert mod._gh_token("/usr/bin/gh", {"PATH": "/p"}) == "tok"
    assert mod._gh_token("/usr/bin/gh") == "tok"
    assert calls == [(["/usr/bin/gh", "auth", "token"], {"PATH": "/p"})]
    assert mod._gh_token("") == ""                      # no gh: never runs
    # A miss is not cached: a `gh auth login` counts on the next spawn.
    Res.returncode = 1
    mod._GH_TOKEN_CACHE.update(at=0.0, gh="", token="")
    assert mod._gh_token("/usr/bin/gh") == ""
    Res.returncode = 0
    assert mod._gh_token("/usr/bin/gh") == "tok"
    assert len(calls) == 3


def test_copilot_bin_ladder(agent, tmp_path, monkeypatch):
    """FUSED_RENDER_COPILOT_BIN (where `copilot_health.adopt` publishes a
    shell-only find) → PATH → the install prefixes, which are
    `copilot_health`'s own list."""
    from fused_render import copilot_health
    assert agent._COPILOT_CANDIDATES == copilot_health.POSIX_CANDIDATES
    assert copilot_health.BIN_ENV == "FUSED_RENDER_COPILOT_BIN"
    on_path = _exe(str(tmp_path / "path" / "copilot"))
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    assert agent._copilot_bin() == on_path
    forced = _exe(str(tmp_path / "forced" / "copilot"))
    monkeypatch.setenv("FUSED_RENDER_COPILOT_BIN", forced)
    assert agent._copilot_bin() == forced
    # Nothing on PATH: the user-prefix candidates (here ~/.bun/bin).
    monkeypatch.delenv("FUSED_RENDER_COPILOT_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    bun = _exe(str(tmp_path / "h" / ".bun" / "bin" / "copilot"))
    monkeypatch.setattr(agent, "_COPILOT_CANDIDATES",
                        tuple(c for c in agent._COPILOT_CANDIDATES if c.startswith("~")))
    assert agent._copilot_bin() == bun


# -------------------------------------------------------- _send (D7, §7)

def _live_run(agent, tmp_path, backend="copilot", mode=None, model="auto"):
    run_dir = os.path.join(agent.RUNS, "run-1")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "session_id": SID_COPILOT, "file": "/f",
                   "mode": mode, "model": model, "effort": "", "read_dirs": [],
                   "backend": backend}, fh)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m", "resumed_from": "",
                   "mode": "prompt", "session_id": SID_COPILOT, "backend": backend}, fh)
    return run_dir


def _inbox_rows(run_dir):
    inbox = os.path.join(run_dir, "inbox")
    out = []
    for name in sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []:
        with open(os.path.join(inbox, name)) as fh:
            out.append(json.load(fh))
    return out


@pytest.fixture()
def send_env(agent, home, monkeypatch):
    _prefs(home, copilot_enabled=True)    # `_send` gates on the run's agent
    cancelled = []
    monkeypatch.setattr(agent, "_cancel", lambda run_id, **kw: cancelled.append(run_id))
    monkeypatch.setattr(agent, "_join_pending_stop", lambda run_dir, timeout=None: True)
    return cancelled


def test_a_copilot_model_change_respawns(agent, tmp_path, send_env):
    run_dir = _live_run(agent, tmp_path)
    assert agent._send("run-1", "more", model="gpt-5.4") == {"respawn": True}
    assert send_env == ["run-1"]
    assert _inbox_rows(run_dir) == []            # no set_model, no user row
    # Same model: an ordinary send.
    assert agent._send("run-1", "more", model="auto") == {"sent": True}
    rows = _inbox_rows(run_dir)
    assert [r["type"] for r in rows] == ["user"]
    # "" (an unnamed spawn) and "auto" are one model: no respawn, no set_model.
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "mode": None, "model": "", "effort": "",
                   "read_dirs": [], "backend": "copilot"}, fh)
    assert agent._send("run-1", "more", model="auto") == {"sent": True}
    assert send_env == ["run-1"]
    assert [r["type"] for r in _inbox_rows(run_dir)] == ["user", "user"]


def test_a_claude_model_change_is_still_a_control_request(agent, tmp_path, send_env):
    run_dir = _live_run(agent, tmp_path, backend="claude", model="sonnet")
    assert agent._send("run-1", "more", model="opus") == {"sent": True}
    assert send_env == []
    assert [r.get("request", {}).get("subtype") for r in _inbox_rows(run_dir)][0] == "set_model"


def test_copilot_modes_prompt_auto_switch_live_accept_edits_respawns(agent, tmp_path, send_env):
    run_dir = _live_run(agent, tmp_path, mode=None)
    assert agent._send("run-1", "m", permission_mode="auto") == {"sent": True}
    rows = _inbox_rows(run_dir)
    assert rows[0]["type"] == "control_request"
    assert rows[0]["request"] == {"subtype": "set_permission_mode", "mode": "auto"}
    with open(os.path.join(run_dir, "host.json")) as fh:
        assert json.load(fh)["mode"] == "auto"
    assert agent._send("run-1", "m", permission_mode="prompt") == {"sent": True}
    assert _inbox_rows(run_dir)[-2]["request"] == {"subtype": "set_permission_mode",
                                                   "mode": None}
    assert send_env == []
    assert agent._send("run-1", "m", permission_mode="acceptEdits") == {"respawn": True}
    assert send_env == ["run-1"]


def test_leaving_accept_edits_respawns_and_plan_is_refused(agent, tmp_path, send_env):
    _live_run(agent, tmp_path, mode="acceptEdits")
    assert agent._send("run-1", "m", permission_mode="auto") == {"respawn": True}
    res = agent._send("run-1", "m", permission_mode="plan")
    assert res["error"] == "plan mode is not available for Copilot"


def test_a_host_without_the_field_reads_as_claude(agent, tmp_path, send_env):
    """A Claude host written before `backend` existed: meta decides, and a
    meta without it is Claude — nothing respawns on a model change."""
    run_dir = _live_run(agent, tmp_path, backend="claude", model="sonnet")
    with open(os.path.join(run_dir, "host.json")) as fh:
        host = json.load(fh)
    del host["backend"]
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump(host, fh)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m"}, fh)
    assert agent._send("run-1", "m", model="opus") == {"sent": True}
    assert send_env == []


def test_a_send_into_a_live_host_of_a_disabled_agent_is_refused(
        agent, tmp_path, home, send_env):
    """Invariant 10 on the live path: Copilot is switched off while its host
    is up — the follow-up is refused with the gate's body and NOTHING is
    written (no inbox row, no control request, no respawn). The request
    naming no backend (or Claude) changes nothing: the run's agent decides."""
    run_dir = _live_run(agent, tmp_path)
    assert agent._send("run-1", "ok") == {"sent": True}     # on: goes through
    before = _inbox_rows(run_dir)
    _prefs(home, copilot_enabled=False)
    for kw in ({}, {"model": "gpt-5.4"}, {"permission_mode": "acceptEdits"},
               {"permission_mode": "auto"}):
        assert agent._send("run-1", "more", **kw) == {
            "error": "Copilot is turned off in Preferences",
            "reason": "backend_disabled", "backend": "copilot"}, kw
    assert _inbox_rows(run_dir) == before and send_env == []
    with open(os.path.join(run_dir, "host.json")) as fh:
        assert json.load(fh)["mode"] is None
    # Claude, with Copilot off: unaffected.
    os.rename(os.path.join(agent.RUNS, "run-1"), os.path.join(agent.RUNS, "old"))
    _live_run(agent, tmp_path, backend="claude", model="sonnet")
    assert agent._send("run-1", "more") == {"sent": True}


def test_the_router_gates_a_bare_send_on_the_runs_backend(client, stub, monkeypatch,
                                                         agent, tmp_path):
    """A send that names no backend is gated on the RUN's (its meta.json),
    and a send naming Claude into a Copilot run is too (invariant 8)."""
    from fused_render.shell import prefs
    run_dir = _live_run(agent, tmp_path)
    module = types.SimpleNamespace(main=_recording_main(stub), RUNS=agent.RUNS,
                                   _bad_id=agent._bad_id,
                                   _run_backend=agent._run_backend)
    monkeypatch.setattr(claude_agent, "agent_module", lambda: module)
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": False})
    for backend in ("", "claude"):
        r = client.post("/api/claude/agent", headers=FUSED,
                        json={"action": "send", "run_id": "run-1", "message": "m",
                              "backend": backend})
        assert r.status_code == 400, backend
        assert r.json()["reason"] == "backend_disabled"
    assert stub == []
    # Copilot on: the same bare send goes through; a run that is not on disk
    # falls back to the request's say-so (nothing named → not gated here).
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": True})
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "send", "run_id": "run-1", "message": "m"})
    assert r.status_code == 200
    monkeypatch.setattr(prefs, "read_prefs", lambda: {"copilot_enabled": False})
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "send", "run_id": "nope", "message": "m"})
    assert r.status_code == 200
    assert os.path.isdir(run_dir)


# ------------------------------------------------------------- caps (§7)

def test_snapshots_answer_empty_for_a_copilot_session(agent, target):
    file, workdir = target
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    tl = agent._snapshots(file, False, False, session_id=SID_COPILOT)
    assert tl["versions"] == [] and tl["offer"] is False and "error" not in tl
    assert tl["file"] == file
    assert agent._snapshot_plan(file, "v1", session_id=SID_COPILOT) == {
        "ok": False, "error": "unsupported"}
    assert agent._snapshot_revert(file, "v1", False, session_id=SID_COPILOT) == {
        "ok": False, "error": "unsupported"}
    # Through main(), which now passes the session along.
    assert agent.main("snapshots", file=file, session_id=SID_COPILOT)["versions"] == []


def test_background_tasks_answer_unsupported_for_copilot(agent, tmp_path):
    _live_run(agent, tmp_path)
    assert agent._task_output("run-1", "b1") == {"error": "unsupported"}
    assert agent._task_stop("run-1", "b1") == {"error": "unsupported"}


def test_the_terminal_command_uses_the_sessions_backend(agent, target, monkeypatch):
    file, workdir = target
    monkeypatch.setattr(agent, "_plugin_argv", lambda target=None: [])
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    out = agent._terminal_command(file, SID_COPILOT, backend="claude")
    assert out["backend"] == "copilot"
    assert out["command"] == "cd %s && copilot --resume=%s" % (workdir, SID_COPILOT)
    assert agent._terminal_command(file, backend="copilot")["command"].endswith("&& copilot")
    out = agent._terminal_command(file)
    assert out["backend"] == "claude" and " claude" in out["command"]
    assert out["command"] == backend_claude.CLAUDE.terminal_resume(workdir, "")
    assert agent._terminal_command(file, SID_COPILOT)["command"] == \
        backend_copilot.COPILOT.terminal_resume(workdir, SID_COPILOT)


def test_the_commit_subject_names_the_agent(agent, tmp_path, monkeypatch):
    """`_commit_turn` is swept against a real repo elsewhere; here only the
    subject line it would pass to git, by the label table."""
    seen = []
    monkeypatch.setattr(agent, "_app_dir_for", lambda p: str(tmp_path))
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(agent, "_request_sync", lambda d: None)

    class Done:
        returncode = 0

    def run(argv, **kw):
        seen.append(argv)
        if "diff" in argv:
            Done.returncode = 1    # something staged
        else:
            Done.returncode = 0
        return Done
    monkeypatch.setattr(agent.subprocess, "run", run)
    monkeypatch.setattr(agent.shutil, "which", lambda n: "/usr/bin/git")
    agent._commit_turn(str(tmp_path / "f"), "fix the header", "copilot")
    commit = [a for a in seen if "commit" in a][0]
    assert commit[commit.index("-m") + 1] == "Copilot: fix the header"
    seen.clear()
    agent._commit_turn(str(tmp_path / "f"), "", "")
    commit = [a for a in seen if "commit" in a][0]
    assert commit[commit.index("-m") + 1] == "Claude turn"


# -------------------------------------------------- listing + defaults (D3)

def test_cli_sessions_lists_both_roots_with_backend_per_row(agent, target):
    _file, workdir = target
    _transcript(agent, "claude", workdir, SID_CLAUDE, [_said("claude one", workdir)])
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("copilot one", workdir)])
    rows = {r["id"]: r for r in agent._cli_sessions(workdir)}
    assert rows[SID_CLAUDE]["backend"] == "claude"
    assert rows[SID_COPILOT]["backend"] == "copilot"
    assert rows[SID_COPILOT]["preview"] == "copilot one"
    # No store at all under either root: an empty list, not an error.
    assert agent._cli_sessions(str(os.path.join(workdir, "nowhere"))) == []


def test_registry_running_reads_both_roots(agent, target):
    _file, workdir = target
    for backend, sessions_dir in agent._registry_roots():
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, "p.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "sessionId": "s-" + backend,
                       "cwd": workdir, "status": "busy"}, fh)
    assert agent._registry_running(workdir) == {"s-claude", "s-copilot"}


def test_defaults_carry_the_backend(agent, target, home):
    file, workdir = target
    # A new Copilot chat opens on `auto`, not Claude's global settings.
    os.makedirs(agent.CLAUDE_DIR, exist_ok=True)
    with open(os.path.join(agent.CLAUDE_DIR, "settings.json"), "w") as fh:
        json.dump({"model": "haiku", "effortLevel": "low"}, fh)
    assert agent._defaults(file, backend="copilot") == {
        "model": "auto", "effort": "", "source": "backend", "backend": "copilot",
        "recorded": {"model": "", "effort": ""}}
    d = agent._defaults(file)
    assert (d["model"], d["backend"]) == ("haiku", "claude")
    # A named Copilot session: its record, raw, never folded to a Claude name.
    _transcript(agent, "copilot", workdir, SID_COPILOT, [_said("b", workdir)])
    agent._record_settings(SID_COPILOT, model="claude-sonnet-4.5", backend="copilot")
    d = agent._defaults(file, SID_COPILOT, backend="claude")
    assert (d["model"], d["backend"], d["source"]) == ("claude-sonnet-4.5", "copilot", "record")
    # …and off the transcript when there is no record.
    _transcript(agent, "copilot", workdir, SID_CLAUDE, [
        _said("b", workdir),
        {"type": "assistant", "message": {"role": "assistant", "model": "gpt-5.4",
                                          "content": []}}])
    d = agent._defaults(file, SID_CLAUDE)
    assert (d["model"], d["backend"], d["source"]) == ("gpt-5.4", "copilot", "session")


def test_history_carries_the_backend_and_the_first_turn_notice(agent, target):
    file, workdir = target
    _transcript(agent, "copilot", workdir, SID_COPILOT, [
        {"type": "system", "subtype": "task_notification",
         "summary": "Handled by GitHub Copilot · model auto", "status": "",
         "sessionId": SID_COPILOT},
        dict(_said("hello", workdir), uuid="u1"),
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "model": "auto",
                     "content": [{"type": "text", "text": "hi"}]}},
    ])
    h = agent._history(file, SID_COPILOT)
    assert h["backend"] == "copilot"
    notices = [s for t in h["turns"] for s in t.get("segments", [])
               if s.get("kind") == "notice"]
    assert notices and notices[0]["text"] == "Handled by GitHub Copilot · model auto"
    assert [t["role"] for t in h["turns"]][-2:] == ["user", "assistant"]
    # No transcript yet: the record speaks.
    agent._record_settings(SID_CLAUDE, backend="copilot")
    assert agent._history(file, SID_CLAUDE)["backend"] == "copilot"
    assert agent._history(file, "55555555-5555-4555-8555-555555555555")["backend"] == "claude"


def test_main_accepts_backend_everywhere_and_poll_reports_it(agent, tmp_path, target):
    assert "backend" in inspect.signature(agent.main).parameters
    file, _ = target
    assert agent.main("defaults", file=file, backend="copilot")["backend"] == "copilot"
    run_dir = _live_run(agent, tmp_path)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init",
                             "session_id": SID_COPILOT}) + "\n")
    assert agent._poll("run-1")["backend"] == "copilot"


# ------------------------------------------------- the meter's window size

def _reply(model="auto", used=1000):
    return {"type": "assistant", "message": {
        "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": used, "output_tokens": 5}}}


def _out(run_dir, rows, mode="w"):
    with open(os.path.join(run_dir, "out.jsonl"), mode) as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_poll_context_carries_the_newest_copilot_window(agent, tmp_path):
    """Gap 2: the host reports the size on init or a later status row; the
    poll's `context` carries the newest as `window`, and keeps it once the
    row that said it is behind the cursor."""
    run_dir = _live_run(agent, tmp_path)
    _out(run_dir, [
        {"type": "system", "subtype": "init", "session_id": SID_COPILOT,
         "context_window": 128000},
        _reply(used=1000)])
    ctx = agent._poll("run-1")["context"]
    assert ctx["window"] == 128000 and ctx["input_tokens"] == 1000
    _out(run_dir, [{"type": "system", "subtype": "status",
                    "context_window": 200000}, _reply(used=2000)], mode="a")
    assert agent._poll("run-1")["context"]["window"] == 200000
    # Rows past the cursor no longer say it: the record does.
    assert agent._run_context_window(run_dir, [_reply()]) == 200000
    assert agent._run_context_window(run_dir, []) == 200000
    # A subagent's row, a non-int, a bool and a zero are not the window.
    for bad in ({"type": "system", "subtype": "status", "context_window": 9,
                 "parent_tool_use_id": "t"},
                {"type": "system", "subtype": "status", "context_window": "9"},
                {"type": "system", "subtype": "status", "context_window": True},
                {"type": "system", "subtype": "status", "context_window": 0}):
        assert agent._row_context_window(bad) == 0


def test_poll_context_has_no_window_until_known_and_claude_is_untouched(agent, tmp_path):
    run_dir = _live_run(agent, tmp_path)
    _out(run_dir, [{"type": "system", "subtype": "init",
                    "session_id": SID_COPILOT}, _reply()])
    assert "window" not in agent._poll("run-1")["context"]
    assert not os.path.exists(os.path.join(run_dir, "context_window"))
    # A Claude run: no key, no record, even if a row happened to carry one.
    claude = os.path.join(agent.RUNS, "run-2")
    os.makedirs(claude)
    with open(os.path.join(claude, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m", "backend": "claude"}, fh)
    with open(os.path.join(claude, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    _out(claude, [{"type": "system", "subtype": "init", "session_id": SID_CLAUDE,
                   "context_window": 1}, _reply("claude-opus-5-5")])
    assert "window" not in agent._poll("run-2")["context"]
    assert not os.path.exists(os.path.join(claude, "context_window"))


def test_history_context_carries_the_copilot_window(agent, target):
    file, workdir = target
    _transcript(agent, "copilot", workdir, SID_COPILOT, [
        dict(_said("hello", workdir), uuid="u1"),
        dict(_reply(used=4321), uuid="a1")])
    # No run dir left: the reading has no window.
    assert "window" not in agent._history(file, SID_COPILOT)["context"]
    run_dir = os.path.join(agent.RUNS, "20261016-120000-aaa")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": file, "message": "hello", "resumed_from": "",
                   "session_id": SID_COPILOT, "backend": "copilot"}, fh)
    # A run no poll ever watched: one scan of out.jsonl, then recorded.
    _out(run_dir, [{"type": "system", "subtype": "init", "session_id": SID_COPILOT},
                   {"type": "system", "subtype": "status", "context_window": 64000},
                   _reply()])
    ctx = agent._history(file, SID_COPILOT)["context"]
    assert ctx["window"] == 64000 and ctx["input_tokens"] == 4321
    with open(os.path.join(run_dir, "context_window")) as fh:
        assert fh.read() == "64000"


# ------------------------------------------------ stop and generic errors

def _ended_run(agent, backend, name, rows, cancelled=False, alive=True):
    run_dir = os.path.join(agent.RUNS, name)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m", "resumed_from": "",
                   "backend": backend}, fh)
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()) if alive else "2147483646")
    _out(run_dir, rows)
    if cancelled:
        open(os.path.join(run_dir, "cancelled"), "w").close()
    return name


_INIT = {"type": "system", "subtype": "init", "session_id": SID_COPILOT}
_EMPTY_ERROR = {"type": "result", "subtype": "error_during_execution",
                "is_error": True, "result": "", "stop_reason": "cancelled"}
_DELTA = {"type": "stream_event", "event": {
    "type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}}


def test_a_stopped_copilot_turn_reads_as_a_stop(agent):
    """The host answers a Stop with an `is_error` result and no text. The
    poll reports it like Claude's interrupted turn — cancelled, WITH an error
    (the agent-named fallback) — because the page says "Stopped." only for a
    cancelled run whose error is non-empty (run-controller `runEnding`); an
    empty one reads as "the turn finished before the stop landed"."""
    p = agent._poll(_ended_run(agent, "copilot", "r-stop", [_INIT, _EMPTY_ERROR],
                               cancelled=True))
    assert p["done"] and p["cancelled"] is True
    assert p["error"] == "copilot exited with an error"
    # Claude's interrupted turn keeps its error; the page folds it itself.
    p = agent._poll(_ended_run(agent, "claude", "r-stop-c", [_INIT, _EMPTY_ERROR],
                               cancelled=True))
    assert p["cancelled"] is True and p["error"] == "claude exited with an error"


@pytest.mark.parametrize("backend", ["copilot", "claude"])
def test_generic_errors_name_the_runs_agent(agent, backend):
    """Every fallback error names the run's own CLI — Claude's strings byte
    for byte as before."""
    # An empty error result that is NOT a Stop is still an error, named.
    assert agent._poll(_ended_run(agent, backend, "r-1", [_INIT, _EMPTY_ERROR])
                       )["error"] == backend + " exited with an error"
    # Dead without a result: unexpected exit, before or after some text.
    assert agent._poll(_ended_run(agent, backend, "r-2", [_INIT], alive=False)
                       )["error"] == backend + " exited unexpectedly"
    assert agent._poll(_ended_run(agent, backend, "r-3", [_INIT, _DELTA], alive=False)
                       )["error"] == backend + " exited before completing the reply"


# ------------------------------------------ Claude stays byte-identical

def test_claudes_limit_rewrite_fires_on_exactly_what_it_did(agent):
    """"usage limit" alone was never rewritten for Claude; only the two
    markers it always matched are."""
    assert agent._account_error("hit a usage limit somewhere") == \
        "hit a usage limit somewhere"
    assert agent._account_error("Claude AI usage limit reached|1700000000") \
        .startswith("Your Claude plan's usage limit was reached.")
    assert agent._account_error("session limit").startswith("Your Claude plan")


def test_a_claude_start_records_on_the_old_condition_and_no_backend(
        agent, target, tmp_path, monkeypatch):
    """A Claude chat's settings record is written only when there is a model
    or an effort to say (as before), and never carries `backend` (absent
    reads as Claude). A Copilot resume's record does."""
    file, workdir = target
    monkeypatch.setitem(agent._HOST_FILES, "claude", _stub_host(tmp_path))
    path = agent._state_file(*agent.SESSION_SETTINGS)
    res = agent._start(file, "hi", "", "", "", "", has_pane=False)
    _req_of(agent, res)
    assert not os.path.exists(path), "nothing to say: no record, as before"
    res = agent._start(file, "hi", "", "opus", "", "", has_pane=False)
    _req_of(agent, res)
    with open(path) as fh:
        rec = json.load(fh)[res["session_id"]]
    assert rec["model"] == "opus" and "backend" not in rec


def test_claude_history_ignores_a_system_task_notification_row(agent, target):
    """The persisted-notice passthrough is Copilot's: a Claude transcript
    carrying the same row reads exactly as it did (no notice segment)."""
    file, workdir = target
    rows = [{"type": "system", "subtype": "task_notification",
             "summary": "should not show", "sessionId": SID_CLAUDE},
            dict(_said("hello", workdir), uuid="u1"),
            {"type": "assistant", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-opus-5-5",
                         "content": [{"type": "text", "text": "hi"}]}}]
    _transcript(agent, "claude", workdir, SID_CLAUDE, rows)
    h = agent._history(file, SID_CLAUDE)
    assert h["backend"] == "claude"
    assert not [s for t in h["turns"] for s in t.get("segments", [])
                if s.get("kind") == "notice"]
    _transcript(agent, "claude", workdir, SID_CLAUDE, rows[1:])
    assert agent._history(file, SID_CLAUDE)["turns"] == h["turns"]


# ----------------------------------------------- bots keep Claude (D17/Q7)

def test_a_bot_starts_on_claude_while_claude_is_off(agent, target, home, tmp_path,
                                                    monkeypatch):
    file, _ = target
    _prefs(home, claude_enabled=False, copilot_enabled=True)
    monkeypatch.setitem(agent._HOST_FILES, "claude", _stub_host(tmp_path))
    assert agent._start(file, "hi", "", "", "", "", has_pane=False,
                        backend="claude")["reason"] == "backend_disabled"
    res = agent._start(file, "hi", "", "", "", "", has_pane=False,
                       backend="claude", bot="b1")
    assert res["backend"] == "claude" and res["run_id"]
    _req, run_dir = _req_of(agent, res)
    with open(os.path.join(run_dir, "meta.json")) as fh:
        assert json.load(fh)["bot"] == "b1"
    # …and its follow-ups too (`_send` reads meta.bot).
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"pid": os.getpid(), "mode": None, "model": "", "effort": "",
                   "read_dirs": [], "backend": "claude"}, fh)
    assert agent._send(res["run_id"], "more") == {"sent": True}
    # No exemption for a bot on Copilot.
    _prefs(home, claude_enabled=True, copilot_enabled=False)
    assert agent._start(file, "hi", "", "", "", "", has_pane=False,
                        backend="copilot", bot="b1")["reason"] == "backend_disabled"
    assert "bot" not in inspect.signature(agent.main).parameters


@pytest.mark.parametrize("reason", ["backend_disabled", "unsupported"])
def test_the_router_answers_the_handlers_own_refusal_with_400(client, monkeypatch,
                                                              reason):
    """A refusal `_start`/`_send` make themselves (a bare start, a resume
    whose own agent is off) is a 400 with the same body, not a 200."""
    body = {"error": "Copilot is turned off in Preferences", "reason": reason,
            "backend": "copilot"}

    def main(action="", file="", backend="", permission_mode="", message="",
             run_id="", session_id=""):
        return dict(body)
    main.__signature__ = inspect.signature(main)
    monkeypatch.setattr(claude_agent, "agent_module",
                        lambda: types.SimpleNamespace(main=main))
    monkeypatch.setattr(gate, "_folder_busy", lambda params, body=None: "")
    monkeypatch.setattr(gate, "_file_owner",
                        lambda params, envelope, body=None, late=False: None)
    for action in ("start", "send"):
        r = client.post("/api/claude/agent", headers=FUSED,
                        json={"action": action, "file": "/w/a.html",
                              "message": "m", "run_id": "r"})
        assert r.status_code == 400 and r.json() == body, action
    # Any other answer, or another action, keeps its 200.
    r = client.post("/api/claude/agent", headers=FUSED,
                    json={"action": "poll", "run_id": "r"})
    assert r.status_code == 200
