"""A poll may only attach to a run about its own target (claude template).

Run ids are global — RUNS is one flat directory — and the `run` url param
survives some hops the target does not. The reproduction: open a folder
listing's claude pane, send a message, then click a SIBLING row. That is a
selection change, not a navigation, so the shell url keeps `run` while the
pane's `_file` moves to the new row — and the old conversation re-attached,
streaming, under whichever folder was clicked (found by duplicating an app
folder and switching between the two).

Two halves. The pane now drops the chat params on a retarget
(frontend listing/chat-params.ts — its own tests), and the agent refuses the
attach outright when the caller's target provably is not the run's: the belt
these tests cover.
"""
import importlib.util
import json
import os
import re

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html_pane():
    """The template with `// …` comments stripped, so a source pin cannot be
    satisfied by prose that merely NAMES the call it is looking for. Same guard
    as test_claude_kind.py's _pane_code."""
    with open(TEMPLATE, encoding="utf-8") as f:
        return re.sub(r"(?m)^\s*//.*$", "", f.read())


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f)


def _run_dir(agent, name, *, meta):
    d = os.path.join(agent.RUNS, name)
    os.makedirs(d)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    with open(os.path.join(d, "pid"), "w", encoding="utf-8") as f:
        f.write("2147483646")  # a pid that cannot exist — the run is over
    return d


MISMATCH = "run is for another target"


def test_polling_someone_else_s_run_is_refused(agent, target, tmp_path):
    _run_dir(agent, "20260819-120000-aaa", meta={"file": target, "message": "hi"})
    other = str(tmp_path / "proj-copy")
    out = agent._poll("20260819-120000-aaa", file=other)
    assert out["error"] == MISMATCH
    assert out["done"] is True
    assert out["text"] == "" and out["segments"] == []


def test_the_run_s_own_target_still_polls(agent, target):
    _run_dir(agent, "20260819-120000-bbb", meta={"file": target, "message": "hi"})
    assert agent._poll("20260819-120000-bbb", file=target)["error"] != MISMATCH


def test_the_same_target_spelled_unnormalized_still_polls(agent, target):
    """The check compares abspaths, not strings: `_file` off a url and the
    path `_start` wrote to meta.json may spell one target two ways."""
    _run_dir(agent, "20260819-120000-fff", meta={"file": target, "message": "hi"})
    spelled = os.path.join(os.path.dirname(target), ".", "index.html")
    assert agent._poll("20260819-120000-fff", file=spelled)["error"] != MISMATCH


def test_a_caller_with_no_target_is_not_refused(agent, target):
    """claude_spawn's record loop polls with no page and no target — the check
    must never take a run away from its own bookkeeping."""
    _run_dir(agent, "20260819-120000-ccc", meta={"file": target, "message": "hi"})
    assert agent._poll("20260819-120000-ccc")["error"] != MISMATCH


def test_a_run_that_never_recorded_a_target_is_not_refused(agent, target):
    """Refused only on a PROVABLE mismatch: a meta.json without `file` proves
    nothing, and an old run must keep re-attaching the way it always did."""
    _run_dir(agent, "20260819-120000-ddd", meta={"message": "hi"})
    assert agent._poll("20260819-120000-ddd", file=target)["error"] != MISMATCH


def test_an_unreadable_meta_is_not_refused(agent, target):
    d = _run_dir(agent, "20260819-120000-eee", meta={"file": target})
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert agent._poll("20260819-120000-eee", file=target)["error"] != MISMATCH


# ---- the page's half ---------------------------------------------------------

def test_every_poll_call_names_the_page_s_target(html_pane):
    """All of the page's polls carry `file: FILE`, or the agent's check never
    runs for the very caller it exists for."""
    calls = re.findall(r'action:\s*"poll"[^}]*', html_pane)
    assert calls, "no poll calls found — did the action move?"
    for call in calls:
        assert "file: FILE" in call, call


def test_the_page_recovers_from_a_mismatch_like_a_stale_param(html_pane):
    """resumeRun's unknown-run branch — clear the `run` param, no error banner —
    is the recovery for a refused target too."""
    assert re.search(
        r'probe\.error === "unknown run_id" \|\| probe\.error === "run is for another target"',
        html_pane)
