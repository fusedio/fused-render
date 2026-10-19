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
import time

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_hist", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _user(text):
    return {"type": "user",
            "message": {"role": "user",
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


def test_a_turn_that_recovered_after_an_error_shows_only_the_answer(
        agent, transcript):
    """A retry that then succeeded: the failure is RESOLVED, and a red row
    above the answer that followed would read as something still wrong
    (Akshil, 2026-10-16: no error card sticks once its issue is over)."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
        _assistant("Second attempt worked."),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[-1]["text"] == "Second attempt worked."


def test_the_last_failure_always_stays(agent, transcript):
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
    ])
    assert [t["role"] for t in turns] == ["user", "error"]


def test_a_failure_followed_only_by_another_failure_stays(agent, transcript):
    """Two different failures in a row: neither says the other is over, and a
    reader who retried twice should see what happened both times."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
        _user("go"),
        _assistant("API Error: Can't reach the API server", isApiErrorMessage=True),
    ])
    assert [t["role"] for t in turns] == ["user", "error", "user", "error"]


# ------------------------------------------------ a limit row that is over

LIMIT = "You've hit your session limit \u00b7 resets 4:50pm (Asia/Calcutta)"


def _limit_row(resets_at=None):
    extra = {"isApiErrorMessage": True, "apiErrorStatus": 429}
    if resets_at is not None:
        extra["quotaLimits"] = {"status": "rejected", "resetsAt": resets_at,
                                "rateLimitType": "five_hour"}
    return _assistant(LIMIT, **extra)


def test_a_limit_the_chat_moved_past_is_not_restored(agent, transcript):
    """The reader retried (or the comeback fired) and the next turn ran:
    whatever lifted the limit — the reset, another account — the card's
    "wait, then try again" is over, and above a reply it reads as a scar."""
    turns = transcript([
        _user("any regressions?"),
        _limit_row(),
        _user("any regressions?"),
        _assistant("None found."),
    ])
    assert [t["role"] for t in turns] == ["user", "user", "assistant"]


def test_a_limit_whose_reset_passed_is_not_restored(agent, transcript):
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) - 60),
    ])
    assert [t["role"] for t in turns] == ["user"]


def test_a_limit_still_binding_is_restored(agent, transcript):
    """Nothing after it and the window not yet open: still true, still red."""
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
    ])
    assert [t["role"] for t in turns] == ["user", "error"]
    assert turns[-1]["quota"]["status"] == "rejected"


def test_a_limit_with_only_a_retry_after_it_is_restored(agent, transcript):
    """A user turn and then nothing: the retry is in flight or died writing
    nothing, and the limit may well still bind."""
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
        _user("go"),
    ])
    assert [t["role"] for t in turns] == ["user", "error", "user"]


def test_a_limit_without_a_reset_and_nothing_after_is_restored(agent, transcript):
    """No `quotaLimits` (an older CLI) and no later turn: nothing says it is
    over, so it is not dropped on a guess."""
    turns = transcript([_user("go"), _limit_row()])
    assert [t["role"] for t in turns] == ["user", "error"]


def test_the_reset_rule_is_the_limits_alone(agent):
    """`_drop_resolved_errors`: a reply resolves ANY failure, but only the
    plan's limit is resolved by a clock — a network error with nothing after
    it stays however old it is."""
    old_limit = {"role": "error", "text": "Claude usage limit reached",
                 "quota": {"status": "rejected", "resets_at": 100}}
    old_break = {"role": "error", "text": "API Error: overloaded",
                 "quota": {"status": "allowed", "resets_at": 100}}
    user = {"role": "user", "text": "go"}
    assert agent._drop_resolved_errors([user, old_limit], now=200) == [user]
    assert agent._drop_resolved_errors([user, old_break], now=200) == [user, old_break]


def test_a_limit_followed_by_another_kind_of_failure_is_restored(agent, transcript):
    """A retry that died on the network lifts nothing: the limit may still
    bind, so both rows stay (only a REPLY or a newer limit row retires it)."""
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
        _user("go"),
        _assistant("API Error: Can't reach the API server", isApiErrorMessage=True),
    ])
    assert [t["role"] for t in turns] == ["user", "error", "user", "error"]
    assert "usage limit" in turns[1]["text"]


def test_a_newer_limit_row_retires_the_older_one(agent, transcript):
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
    ])
    assert [t["role"] for t in turns] == ["user", "user", "error"]


def test_a_bare_429_with_a_rejected_window_is_the_limit_too(agent, transcript):
    """The CLI sometimes says only "API Error: 429" and leaves the plan window
    on `quotaLimits`; the rejected status is the limit, so it retires."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: 429", isApiErrorMessage=True, apiErrorStatus=429,
                   quotaLimits={"status": "rejected", "resetsAt": int(time.time()) + 3600,
                                "rateLimitType": "five_hour"}),
        _assistant("Back on it."),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_a_bare_429_without_a_window_is_a_break_not_a_clock(agent, transcript):
    """No rejected window: not the limit, so a passed-looking time on it means
    nothing and only a reply resolves it. Alone at the tail it stays."""
    turns = transcript([
        _user("go"),
        _assistant("API Error: 429", isApiErrorMessage=True, apiErrorStatus=429),
    ])
    assert [t["role"] for t in turns] == ["user", "error"]
    assert "quota" not in turns[-1]


def test_a_limit_followed_by_a_tool_only_reply_is_not_restored(agent, transcript):
    """The retry ran a tool and is still at it (or died before saying a
    word): the API served that call, so the plan window is open and the
    card's "wait, then try again" is over — even with the reset still ahead
    (Akshil, 2026-10-18)."""
    tool_row = {"type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "ls"}}]}}
    turns = transcript([
        _user("go"),
        _limit_row(resets_at=int(time.time()) + 3600),
        _user("try again"),
        tool_row,
    ])
    assert [t["role"] for t in turns] == ["user", "user", "assistant"]


def test_a_tool_only_turn_between_two_failures_resolves_nothing(agent, transcript):
    """A retry that called a tool and then died again never answered: the
    assistant turn it leaves has segments but no words, and the first
    failure is as unresolved as the second (CodeRabbit #1699)."""
    tool_row = {"type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/x"}}]}}
    turns = transcript([
        _user("go"),
        _assistant("API Error: overloaded", isApiErrorMessage=True),
        _user("go"),
        tool_row,
        _assistant("API Error: overloaded again", isApiErrorMessage=True),
    ])
    roles = [t["role"] for t in turns]
    assert roles.count("error") == 2
    assert "overloaded" in [t for t in turns if t["role"] == "error"][0]["text"]
