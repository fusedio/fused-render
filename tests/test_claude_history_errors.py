"""A FAILED turn survives a reload as a failure (feedback R2-3 / R2-14).

Wi-Fi off, a 429, an exhausted plan limit: the live run shows the failure in
red off `_poll`'s `error` field, and QA confirmed that part works. Reload the
chat and the same turn came back as ordinary assistant prose — "API Error:
Can't reach the API server" set in normal type as though it were the model's
considered answer — because Claude Code records an API failure as a
`type: "assistant"` transcript row carrying the message as text, and
`_history` folded any assistant row's text into the assistant turn.

`isApiErrorMessage` is the one field that tells them apart, and ORDINARY rows
carry it as `false` on a current CLI, which is why the check is `is True`.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


@pytest.fixture
def agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_hist", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _user(text):
    return {"message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _assistant(text, **extra):
    row = {"type": "assistant",
           "message": {"role": "assistant",
                       "content": [{"type": "text", "text": text}]}}
    row.update(extra)
    return row


@pytest.fixture
def transcript(agent, tmp_path, monkeypatch):
    """Returns a writer for the session transcript `_history` will read."""
    target = tmp_path / "proj"
    target.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    proj_dir = projects / agent._munge(str(target))
    proj_dir.mkdir(parents=True)

    def write(rows):
        (proj_dir / (SESSION + ".jsonl")).write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return agent._history(str(target), SESSION)["turns"]

    return write


# ------------------------------------------------------------- the detector

def test_only_a_true_flag_is_an_api_error(agent):
    """`isApiErrorMessage: false` rides on EVERY assistant row of a current
    CLI, so a truthiness-or-presence test would paint the whole conversation
    red."""
    assert agent._is_api_error_row({"isApiErrorMessage": True}) is True
    assert agent._is_api_error_row({"isApiErrorMessage": False}) is False
    assert agent._is_api_error_row({}) is False
    assert agent._is_api_error_row({"apiErrorStatus": 429}) is False, (
        "the network cases carry no status at all, so the status is not the "
        "signal — the flag is")


# --------------------------------------------------------- the restored turn

def test_a_network_failure_restores_as_an_error_turn(agent, transcript):
    turns = transcript([
        _user("build it"),
        _assistant("API Error: Can't reach the API server — check your "
                   "internet or DNS (ENOTFOUND)", isApiErrorMessage=True),
    ])
    assert [t["role"] for t in turns] == ["user", "error"]
    assert "ENOTFOUND" in turns[-1]["text"]


def test_a_usage_limit_restores_with_the_SAME_copy_the_live_run_showed(
        agent, transcript):
    """`_poll` runs its `error` through `_account_error`; a restore that did
    not would show the raw CLI sentence for the live run's helpful one."""
    turns = transcript([
        _user("go"),
        _assistant("Claude usage limit reached", isApiErrorMessage=True,
                   apiErrorStatus=429),
    ])
    assert turns[-1]["role"] == "error"
    assert turns[-1]["text"] == agent._account_error("Claude usage limit reached")
    assert "usage limit" in turns[-1]["text"]


def test_the_reply_that_failed_keeps_its_own_prose(agent, transcript):
    """The failure is its own turn, so half an answer that did stream stays an
    assistant turn above it rather than being swallowed into the red line."""
    turns = transcript([
        _user("go"),
        _assistant("Here is half an ans"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant", "error"]
    assert turns[1]["text"] == "Here is half an ans"


def test_an_ordinary_reply_is_still_an_assistant_turn(agent, transcript):
    turns = transcript([
        _user("go"),
        _assistant("All done.", isApiErrorMessage=False),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[-1]["text"] == "All done."


def test_the_error_text_is_not_printed_twice(agent, transcript):
    """The row is kept out of the segment `stretch` on purpose:
    `_segments_from_rows` would turn its text into a text segment, so the
    failure would render once as a segment and once as the red line."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
    ])
    assert all("segments" not in t for t in turns)


def test_a_turn_that_recovered_after_an_error_shows_both(agent, transcript):
    """A retry that then succeeded: the failure happened and is worth seeing,
    and the answer that followed is a real assistant turn after it."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
        _assistant("Second attempt worked."),
    ])
    assert [t["role"] for t in turns] == ["user", "error", "assistant"]
