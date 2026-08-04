"""Stopping a turn in the claude template.

`agent.py` has always been able to kill a run (`action="cancel"` → SIGTERM to
the process group, plus a release of every parked approval), but nothing in the
chat window ever called it: a turn that went off the rails could only be waited
out or escaped by navigating away, which orphans the subprocess. The chat now
offers a stop button on the working line and binds Escape to the same thing.

Two contracts are worth pinning here, and neither is visible from a test of the
approval bridge:

* the **page reaches the backend's cancel** — the button and the key both send
  the one action `main()` dispatches, with the live run's id;
* **a stopped run does not read as a crash.** Killing claude leaves the run dead
  with no `result` row, which `_poll` reports as an error *by design* (a
  truncated reply must never render as a clean success). When the user asked for
  the stop, that error is the expected outcome, not news — so the page suppresses
  it and says "stopped" instead, while keeping whatever text had streamed.

The end-of-run decision is one pure function in the page (`runEnding`), extracted
and executed under node rather than asserted about as source: what matters is the
text the user ends up reading.
"""
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

# Both chats offer the stop: claude_split is a fork of claude, so the feature is
# duplicated and every contract below is asserted against BOTH copies (D146 — a
# rule in two implementations needs a test, not a comment). `agent` is
# parametrised in lockstep with the html, so a template is never checked against
# the other one's backend.
TEMPLATES = ["claude", "claude_split"]


def _dir(template):
    return os.path.join("fused_render", "templates", template)


def _load(template, name):
    path = os.path.join(_dir(template), name + ".py")
    spec = importlib.util.spec_from_file_location(f"{template}_stop_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=TEMPLATES)
def template(request):
    return request.param


@pytest.fixture
def agent(template):
    return _load(template, "agent")


def _html(template="claude"):
    return open(os.path.join(_dir(template), "template.html"), encoding="utf-8").read()


# ------------------------------------------------------- page reaches cancel

def test_the_page_sends_the_cancel_action_the_backend_dispatches(agent, template):
    """D146-shaped: the action name lives in two places (the page's call and
    `main()`'s dispatch), so a test holds them together rather than a comment."""
    html = _html(template)
    assert 'action: "cancel"' in html, "the page never calls the backend's cancel"
    # ...and the name it sends is one `main()` actually routes, rather than
    # falling through to "unknown action" — which would fail silently as a
    # stop button that does nothing.
    assert agent.main(action="cancel", run_id="no-such-run") == {"cancelled": "no-such-run"}
    assert "unknown action" in str(agent.main(action="stop"))


def test_stopping_is_offered_both_as_a_button_and_as_escape(template):
    """Two affordances, one path: the click target is discoverable, the key is
    what a terminal user reaches for. Both must call the same stopRun()."""
    html = _html(template)
    assert "function stopRun(" in html
    # the working line's button, whose label is also where the key is taught
    # (the hint row below is space-between around the selectors, and a left item
    # long enough to say it wrapped that row onto a second line mid-turn)
    assert 'id="stopbtn"' in html, "no stop control on the page"
    assert "stop · esc" in html, "the button does not teach the key"
    assert '"Escape"' in html, "Escape is not bound"


# ------------------------------------------- the backend contract runEnding sits on

def test_a_killed_run_polls_as_done_with_an_error(agent, tmp_path, monkeypatch):
    """The shape the page's stop path has to absorb. A run killed mid-turn is
    dead with no `result` row, and `_poll` reports that as an error on purpose
    — this test pins that it still does, because `runEnding` below is written
    to swallow exactly this error and nothing else."""
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    (run_dir / "out.jsonl").write_text("")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    monkeypatch.setattr(agent, "_alive", lambda _d: False)

    out = agent._poll("run")
    assert out["done"] is True
    assert out["error"], "a killed run must not poll as a clean success"


# ------------------------------------------------------------- runEnding, in node

def _run_ending(data, stopped, template="claude"):
    """Run the page's real `runEnding` over one terminal poll payload."""
    html = _html(template)
    start = html.index("function runEnding(")
    # up to the next top-level definition — `runEnding` is deliberately pure, so
    # nothing below it (which touches `document` and `fused`) may come along.
    fn = html[start:html.index("\nasync function stopRun(", start)]
    script = fn + "\nconsole.log(JSON.stringify(runEnding(%s, %s)));" % (
        json.dumps(data), json.dumps(stopped))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(autouse=True)
def _needs_node(request):
    if request.function.__name__.startswith("test_the_ending") and not shutil.which("node"):
        pytest.skip("node is needed to run the page's own end-of-run decision")


def test_the_ending_of_an_unstopped_failure_is_still_an_error(template):
    end = _run_ending({"done": True, "error": "claude exited with an error", "text": ""},
                      False, template)
    assert end["error"] == "claude exited with an error"
    assert not end["note"]
    assert end["keepText"] is False


def test_the_ending_of_a_stopped_run_is_a_note_not_an_error(template):
    """The kill IS the error; reporting it would blame the user's own click."""
    end = _run_ending(
        {"done": True, "error": "claude exited before completing the reply",
         "text": "I was hal"}, True, template)
    assert not end["error"], end
    assert "stopped" in end["note"].lower()
    # the partial reply is real work and stays on screen
    assert end["keepText"] is True


def test_the_ending_of_a_clean_run_says_nothing(template):
    end = _run_ending({"done": True, "error": "", "text": "all done"}, False, template)
    assert not end["error"] and not end["note"]
    assert end["keepText"] is True


def test_the_ending_of_a_run_that_beat_the_stop_says_so(template):
    """Clicking stop a beat after the last poll: the reply is whole, so it is
    shown whole — but the user is told why nothing was cut off, rather than
    being left wondering whether the button works."""
    end = _run_ending({"done": True, "error": "", "text": "all done"}, True, template)
    assert not end["error"]
    assert end["note"], "a stop that did not land must not be silent"
    assert end["keepText"] is True


def test_the_two_chats_decide_endings_identically():
    """claude_split is a fork, so `runEnding` exists twice. Compared by what the
    two copies DO — the exact note a user reads included — rather than by their
    source, so the wording cannot drift while the comments are free to differ."""
    cases = [
        ({"done": True, "error": "boom", "text": ""}, False),
        ({"done": True, "error": "boom", "text": "partial"}, True),
        ({"done": True, "error": "", "text": "done"}, False),
        ({"done": True, "error": "", "text": "done"}, True),
    ]
    for data, stopped in cases:
        assert _run_ending(data, stopped, "claude") == \
               _run_ending(data, stopped, "claude_split"), (data, stopped)
