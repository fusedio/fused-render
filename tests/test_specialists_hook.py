"""Pull by signal (H, Y), stuck detection (K), behaviour signals (S) and the Stop dispatch in a home, through
`POST /api/specialists/hook`; and `specialist_hook.py` itself (fail-open, output shape)."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _specialists_conftest import *  # noqa: F401,F403
from _specialists_conftest import make_specialist, needs_git

HOOK = os.path.join("fused_render", "claude_agent", "specialist_hook.py")


@pytest.fixture(autouse=True)
def _no_transcripts(monkeypatch):
    from fused_render.specialists import receipts

    monkeypatch.setattr(receipts, "transcript_lines", lambda cwd, sid: 3)


def _hook(client, **body):
    status, _, raw = client.post("/api/specialists/hook", body)
    assert status == 200, raw
    return json.loads(raw)


def _events(client, sid):
    return json.loads(client.get("/api/specialists/task-events?session_id=" + sid)[2])["events"]


@needs_git
def test_signal_pull_by_folder_once_per_memory_version(client, tmp_path):
    from fused_render.specialists import history, receipts

    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    spec = make_specialist("Canvas", "Fused canvases", triggers={"folders": [str(proj)], "keywords": []})
    out = _hook(client, event="prompt", session_id="s1", cwd=str(proj / "sub"), prompt="fix it")
    ctx = out["additionalContext"]
    assert ctx.startswith("[fused:specialist-guidance receipt=r-") and "Guidance from the Canvas specialist" in ctx
    assert "this task works in " + str(proj) in ctx
    (rec,) = receipts.for_session("s1")
    assert rec["trigger"] == "signal" and rec["match"]["folder"] == str(proj)
    deadline = time.monotonic() + 5  # the reviewer's cursor is filled in after the hook is answered
    while receipts.get(spec.id, rec["id"])["lines"] != 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert receipts.get(spec.id, rec["id"])["lines"] == 3
    assert _hook(client, event="prompt", session_id="s1", cwd=str(proj), prompt="again") == {}
    # a new memory version pulls again
    with open(os.path.join(spec.home, "memory.md"), "a") as fh:
        fh.write("- [m-aaaa] New. {basis=guess}\n")
    history.commit(spec.id, "memory")
    assert "additionalContext" in _hook(client, event="prompt", session_id="s1", cwd=str(proj), prompt="x")
    assert len(receipts.for_session("s1")) == 2
    assert [e["trigger"] for e in _events(client, "s1") if e["kind"] == "pull"] == ["signal", "signal"]


def test_no_match_no_context(client, tmp_path):
    make_specialist("Canvas", "Fused canvases", triggers={"folders": [str(tmp_path / "elsewhere")],
                                                         "keywords": ["canvas"]})
    assert _hook(client, event="prompt", session_id="s1", cwd=str(tmp_path), prompt="canvasses are fine") == {}


def test_keyword_match_and_two_specialists(client, tmp_path):
    from fused_render.specialists import receipts

    make_specialist("Canvas", "Fused canvases", triggers={"folders": [], "keywords": ["canvas"]})
    make_specialist("Data", "datasets", triggers={"folders": [], "keywords": ["S3 bucket"]})
    out = _hook(client, event="prompt", session_id="s2", cwd=str(tmp_path),
                prompt="Load the s3 bucket into the CANVAS please")
    ctx = out["additionalContext"]
    assert "Guidance from the Canvas specialist" in ctx and "Guidance from the Data specialist" in ctx
    assert "the message mentions canvas" in ctx
    assert sorted(r["match"]["keywords"][0] for r in receipts.for_session("s2")) == ["S3 bucket", "canvas"]


def test_stuck_at_n_once_per_signature(client, tmp_path, monkeypatch):
    from fused_render.specialists import signals

    monkeypatch.setenv("FUSED_RENDER_SPECIALIST_STUCK_N", "3")
    make_specialist("Canvas", "Fused canvases")
    fail = dict(event="failure", session_id="s3", cwd=str(tmp_path), tool_name="Bash",
                tool_input={"command": "make"})
    assert _hook(client, error="Exit code 2\nmake: *** [all] Error 2", **fail) == {}
    assert _hook(client, error="Exit code 2\nmake: *** [all] Error 2", **fail) == {}
    out = _hook(client, error="Exit code 2\nother tail", **fail)
    assert out["additionalContext"].startswith("The same step has failed 3 times (Bash: Exit code #).")
    assert "(Canvas)" in out["additionalContext"] and "specialist_breakout" in out["additionalContext"]
    assert _hook(client, error="Exit code 2", **fail) == {}  # once per signature
    assert [e["kind"] for e in _events(client, "s3")] == ["stuck"]
    assert [s["kind"] for s in signals.since("s3", 0)] == ["failure"] * 4
    # a different failure counts on its own
    assert _hook(client, error="No such file", **fail) == {}


def test_interrupt_is_a_signal_not_a_failure(client, tmp_path):
    from fused_render.specialists import signals

    make_specialist("Canvas", "Fused canvases")
    _hook(client, event="failure", session_id="s4", cwd=str(tmp_path), tool_name="Bash", error="x",
          is_interrupt=True)
    assert [s["kind"] for s in signals.since("s4", 0)] == ["interrupt"]


def test_stop_in_a_home_dispatches_the_turn(client, monkeypatch):
    from fused_render.specialists import breakouts, changes, drafts, history

    spec = make_specialist("Canvas", "Fused canvases")
    seen = []
    monkeypatch.setattr(history, "after_turn", lambda i, k, s: seen.append(("history", i, k, s)))
    monkeypatch.setattr(changes, "after_turn", lambda i, s: seen.append(("changes", i, s)))
    monkeypatch.setattr(drafts, "after_turn", lambda i, s: seen.append(("drafts", i, s)))
    monkeypatch.setattr(breakouts, "session_ids", lambda i: {"bs1": "open"})
    _hook(client, event="stop", session_id="ms1", cwd=spec.home)
    _hook(client, event="stop", session_id="bs1", cwd=spec.home)
    assert ("history", spec.id, "main", "ms1") in seen and ("history", spec.id, "breakout", "bs1") in seen
    assert ("changes", spec.id, "ms1") in seen and ("drafts", spec.id, "bs1") in seen
    # prompts in a home never pull (its own memory is already loaded)
    assert _hook(client, event="prompt", session_id="ms1", cwd=spec.home, prompt="canvas") == {}


def test_bad_session_id_is_ignored(client, tmp_path):
    make_specialist("Canvas", "x", triggers={"folders": [str(tmp_path)], "keywords": []})
    assert _hook(client, event="prompt", session_id="../x", cwd=str(tmp_path), prompt="") == {}


# ---------------------------------------------------------------- the script ---
class _Stub:
    def __init__(self, answer):
        self.bodies = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                stub.bodies.append((self.path, json.loads(self.rfile.read(n)), self.headers.get("X-Fused")))
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _run(event, stdin, origin):
    env = dict(os.environ, FUSED_RENDER_ORIGIN=origin)
    env.pop("FUSED_RENDER_HOME_DIR", None)
    return subprocess.run([sys.executable, "-I", HOOK, event], input=json.dumps(stdin), capture_output=True,
                          text=True, env=env, timeout=20)


@pytest.mark.parametrize("event,name", [("prompt", "UserPromptSubmit"), ("failure", "PostToolUseFailure")])
def test_script_prints_additional_context(event, name):
    stub = _Stub({"additionalContext": "guidance"})
    try:
        out = _run(event, {"session_id": "s", "cwd": "/x", "prompt": "p", "tool_response": "big"}, stub.origin)
        assert out.returncode == 0
        assert json.loads(out.stdout) == {"hookSpecificOutput": {"hookEventName": name,
                                                                 "additionalContext": "guidance"}}
        path, body, fused = stub.bodies[0]
        assert path == "/api/specialists/hook" and fused == "1"
        assert body == {"session_id": "s", "cwd": "/x", "prompt": "p", "event": event}
    finally:
        stub.close()


def test_script_stop_prints_nothing_and_fails_open():
    stub = _Stub({"additionalContext": "ignored"})
    try:
        out = _run("stop", {"session_id": "s", "cwd": "/x"}, stub.origin)
        assert out.returncode == 0 and out.stdout == "" and stub.bodies
    finally:
        stub.close()
    for event, origin, stdin in (("prompt", "http://127.0.0.1:9", {"session_id": "s"}),
                                 ("prompt", "", {"session_id": "s"}), ("bogus", "http://127.0.0.1:9", {}),
                                 ("prompt", "http://127.0.0.1:9", "not json")):
        out = _run(event, stdin, origin)
        assert out.returncode == 0 and out.stdout == ""
