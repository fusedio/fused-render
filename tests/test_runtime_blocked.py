"""The injected runtime says the open gate's refusal as a sentence.

A Community copy Fused blocked (hard, or soft and not said yes to yet) answers
423 `{"blocked": {...}}` on every run door — `/api/run`, its socket, and the
background start/restart (docs/CONTRIB_LOOP.md §18.4 B4). That body has no
`ok` and no `error`, so before this the page's runPython rejected with an
Error whose message was `undefined`. The runtime now turns it into one Error
carrying the block's own sentence.

String-contract checks over the shipped `static/runtime.js`, plus the
sentence itself, built by running `blockedError` under node when node is on
the machine.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import fused_render

RUNTIME = (Path(fused_render.__file__).parent / "static" / "runtime.js").read_text(encoding="utf-8")


def test_run_and_daemon_doors_route_the_423_through_blocked_error():
    assert "function blockedError(blocked)" in RUNTIME
    # runPython's handler: the socket strips `status`, so the body decides.
    assert "if (data.blocked && data.ok === undefined) throw blockedError(data.blocked);" in RUNTIME
    # fused.daemon.start()/restart() (and run/call's bring-up through start).
    assert "if (res.status === 423 && data && data.blocked) throw blockedError(data.blocked);" in RUNTIME


def _blocked_error_source() -> str:
    m = re.search(r"\n  function blockedError\(blocked\) \{\n.*?\n  \}\n", RUNTIME, re.S)
    assert m, "blockedError not found at the runtime's top level"
    return m.group(0)


def _say(blocked: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        _blocked_error_source()
        + "const e = blockedError(" + json.dumps(blocked) + ");"
        + "console.log(JSON.stringify({message: e.message, type: e.type, status: e.status,"
        + " level: e.blocked.level}));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_hard_block_says_why_and_what_to_do():
    got = _say({"level": "hard", "reason": "It uploads your files.", "what_to_do": "Remove it."})
    assert got == {
        "message": "This app is blocked by Fused: It uploads your files. Remove it.",
        "type": "blocked",
        "status": 423,
        "level": "hard",
    }


def test_a_soft_block_points_at_open_anyway():
    got = _say({"level": "soft", "reason": "It asks for broad access.", "what_to_do": ""})
    assert got["message"] == (
        "This app waits for your OK before it runs: It asks for broad access."
        " Open its app page and choose Open anyway to use it."
    )


def test_a_bare_block_still_reads_as_a_sentence():
    assert _say({})["message"] == "This app is blocked by Fused."
