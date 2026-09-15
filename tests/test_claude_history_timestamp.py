"""A restored user turn says WHEN it was sent.

The chat draws the time in the left icon lane on hover (chat v2 design §C), and
the only record of it is the transcript row's own `timestamp` — an ISO 8601
string Claude Code writes on every record. `_history` parses it once, server
side, and hands user turns `ts` in epoch SECONDS, because a page that got the
string would have to parse dates in JS and every reader would do it differently.

Two halves are load-bearing and both are asserted here:

* the key is on USER turns only — an assistant reply is dated by the message it
  answers, and a second clock in the transcript is noise;
* the key is ABSENT, never zero, when the row has no parseable timestamp. `0` is
  a real instant (1970) and a hover confidently showing "Jan 1, 00:00" is worse
  than a hover showing nothing. Old transcripts and the fixtures in this suite's
  neighbours (test_claude_app_state.py writes a row with no timestamp and
  asserts the WHOLE turn dict) both take that road.
"""
import datetime
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_ts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def _user(text, **extra):
    row = {"type": "user",
           "message": {"role": "user",
                       "content": [{"type": "text", "text": text}]}}
    row.update(extra)
    return row


def _assistant(text, **extra):
    row = {"type": "assistant",
           "message": {"role": "assistant",
                       "content": [{"type": "text", "text": text}]}}
    row.update(extra)
    return row


# ------------------------------------------------------------------- the parse

def test_an_iso_utc_stamp_becomes_epoch_seconds(agent):
    """The shape a real transcript carries: UTC, millisecond precision, `Z`."""
    stamp = "2026-09-14T21:59:03.500Z"
    expected = datetime.datetime(2026, 9, 14, 21, 59, 3, 500000,
                                 tzinfo=datetime.timezone.utc).timestamp()
    assert agent._row_ts({"timestamp": stamp}) == expected


def test_a_stamp_with_no_zone_is_read_as_utc(agent):
    """A row that lost its `Z` is still UTC (review #8).

    `datetime.timestamp()` on a NAIVE value applies the server's own offset, so
    the same transcript dated a message hours apart depending on where the
    machine running fused-render happened to be — silently, and never on the
    developer's own box when that box is already on UTC. The parse now says
    what the CLI means instead of what the host's TZ does, which is also why
    this asserts against an explicit UTC datetime rather than against
    `fromisoformat(raw).timestamp()`.
    """
    naive = "2026-09-14T21:59:03.500"
    expected = datetime.datetime(2026, 9, 14, 21, 59, 3, 500000,
                                 tzinfo=datetime.timezone.utc).timestamp()
    assert agent._row_ts({"timestamp": naive}) == expected
    # …and identical to the same instant written with the suffix.
    assert agent._row_ts({"timestamp": naive + "Z"}) == expected


def test_an_explicit_offset_is_honoured(agent):
    """A zone that IS stated wins: the UTC default is for the absent one."""
    assert (agent._row_ts({"timestamp": "2026-09-14T23:59:03.500+02:00"})
            == agent._row_ts({"timestamp": "2026-09-14T21:59:03.500Z"}))


@pytest.mark.parametrize("row", [
    {},
    {"timestamp": None},
    {"timestamp": ""},
    {"timestamp": "yesterday"},
    {"timestamp": 1757885943},
])
def test_anything_unparseable_is_no_answer_rather_than_zero(agent, row):
    assert agent._row_ts(row) is None


# ------------------------------------------------------------- the restored turn

def test_a_restored_user_turn_carries_its_timestamp(agent, transcript):
    stamp = "2026-09-14T21:59:03.500Z"
    expected = datetime.datetime(2026, 9, 14, 21, 59, 3, 500000,
                                 tzinfo=datetime.timezone.utc).timestamp()
    turns = transcript([_user("fix the header", timestamp=stamp),
                        _assistant("done", timestamp=stamp)])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["ts"] == expected
    # The assistant side is untouched: it is dated by the message it answers.
    assert "ts" not in turns[1]


def test_a_row_without_a_timestamp_carries_no_key_at_all(agent, transcript):
    """Not `0`, not `None` — absent. The page treats the key as optional and
    simply draws no time, which is the honest answer for a row that has none."""
    turns = transcript([_user("fix the header")])
    assert turns == [{"role": "user", "text": "fix the header", "uuid": ""}]


def test_an_unparseable_timestamp_is_the_same_as_none(agent, transcript):
    turns = transcript([_user("fix the header", timestamp="not a date")])
    assert "ts" not in turns[0]
