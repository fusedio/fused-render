"""Blocked means "the chat ends on an error row", and the usage limit's Retry now.

Akshil, 2026-11-23: "even when my task is done it shows blocked. If there is no
error at the bottom of the task it should be done. If it is blocked there must
be an error at the bottom of the chat that tells you why. And if something
comes after that error's timestamp, it is not blocked any more." — and "add
retry / force start when I have a message queued because I hit my session
limit; I can switch Claude accounts and then the wait is gone."

Every scenario below is played out against BOTH readers of one transcript: the
tasks row (`GET /api/tasks`) and the chat's own rule for which error rows it
still draws (agent.py `_drop_resolved_errors`). The row may say `blocked` only
when the chat's last row is an error.

Nothing here spawns anything or reads the real ~/.claude.
"""
import importlib.util
import json
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule, schedule_wake, session_liveness
from fused_render import tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

WRITE = {"X-Fused": "1"}
SID = "sess-limit"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "PROJECTS_DIR", str(d))
    # The scheduler's own transcript reader (`_comeback_stale`).
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()
    schedule._events.clear()
    schedule._watched.clear()
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    # No session reads as mid-turn: every transcript here is minutes old.
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)
    yield
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()
    schedule._events.clear()
    schedule._watched.clear()


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"message": prompt, "session_id": session_id,
                      "target": target})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture(scope="module")
def agent():
    path = os.path.join("fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_limit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------- records


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _user(text, offset):
    return {"type": "user", "timestamp": _iso(offset),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _reply(text, offset):
    return {"type": "assistant", "timestamp": _iso(offset),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _tool_call(offset):
    return {"type": "assistant", "timestamp": _iso(offset),
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1",
                                     "name": "Bash", "input": {}}]}}


def _limit(offset, resets_in=3600.0):
    """The row Claude Code writes when the plan's window is spent, with the
    window it reported (`quotaLimits`)."""
    row = _reply("You've hit your session limit · resets 7:20pm", offset)
    row["isApiErrorMessage"] = True
    row["apiErrorStatus"] = 429
    row["quotaLimits"] = {"status": "rejected", "rateLimitType": "five_hour",
                          "resetsAt": int(time.time() + resets_in)}
    return row


def _broke(offset):
    row = _reply("API Error: Can't reach the API server (ENOTFOUND)", offset)
    row["isApiErrorMessage"] = True
    return row


def _write(projects_dir, records, cwd="/p"):
    d = projects_dir / ("-encoded-" + SID)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{SID}.jsonl"
    lines = []
    for i, record in enumerate(records):
        record = dict(record)
        record.setdefault("cwd", cwd)
        record.setdefault("sessionId", SID)
        record.setdefault("uuid", f"{SID}-{i}")
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")
    # Minutes old: the row reads the transcript's verdict, not its liveness.
    old = time.time() - 600
    os.utime(path, (old, old))
    return path


def _chat_bottom_is_error(agent, records) -> bool:
    """What the CHAT draws at the bottom of this transcript: the turns as
    `_history` shapes them, then `_drop_resolved_errors`."""
    turns = []
    for row in records:
        if row["type"] == "user":
            turns.append({"role": "user", "text": "x"})
        elif row.get("isApiErrorMessage"):
            turn = {"role": "error",
                    "text": row["message"]["content"][0]["text"]}
            if row.get("quotaLimits"):
                turn["quota"] = agent._quota_info(
                    {"rate_limit_info": row["quotaLimits"]})
            turns.append(turn)
        else:
            content = row["message"]["content"]
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text")
            turns.append({"role": "assistant", "text": text,
                          **({} if text else {"segments": [{"kind": "tool"}]})})
    shown = agent._drop_resolved_errors(turns)
    return bool(shown) and shown[-1]["role"] == "error"


def _row(client):
    tasks_mod.reset_cache()
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()["tasks"]}[SID]


def _book_comeback(client, target, due_in=3600.0):
    """What the chat posts when a turn ends on the limit (`scheduleComeback`)."""
    r = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": schedule.COMEBACK_PROMPT,
        "due": _iso(due_in), "session_id": SID,
        "title": schedule.COMEBACK_TITLE})
    assert r.status_code == 200, r.text
    return r.json()["entry"]


def _backdate(entry_id, seconds_ago):
    """When the comeback was booked — moved into the past so a reply written
    "a minute ago" in the fixture is newer than it, as it would be live."""
    schedule._update(entry_id, created=datetime.fromtimestamp(
        time.time() - seconds_ago, timezone.utc).isoformat())


def _comebacks():
    return [e for e in schedule.list_entries()
            if e.get("title") == schedule.COMEBACK_TITLE]


def _pending_comebacks():
    return [e for e in _comebacks() if e["state"] == schedule.PENDING]


def _retry(client):
    return client.post("/api/tasks/limit/retry", headers=WRITE,
                       json={"key": SID})


def _assert_agrees(agent, records, row):
    """THE RULE: blocked exactly when the chat ends on an error row."""
    assert (row["status"] == "blocked") == _chat_bottom_is_error(agent, records)


# ----------------------------------------------------------------- (1) hit


def test_1_a_limit_hit_is_blocked_with_the_row_showing(client, projects_dir,
                                                       target, agent):
    records = [_user("wire up the chart", -900), _limit(-890)]
    _write(projects_dir, records)
    comeback = _book_comeback(client, target)

    row = _row(client)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "usage_limit"
    assert row["resumes_at"] > time.time()
    assert _chat_bottom_is_error(agent, records)
    _assert_agrees(agent, records, row)
    # The comeback is untouched by the listing: nothing has answered since.
    assert [e["id"] for e in _pending_comebacks()] == [comeback["id"]]


# ------------------------------------------------- (2) retry, account switched


def test_2_retry_now_after_an_account_switch_runs_and_lands_done(
        client, projects_dir, target, agent, spawned):
    records = [_user("wire up the chart", -900), _limit(-890)]
    _write(projects_dir, records)
    comeback = _book_comeback(client, target)
    _backdate(comeback["id"], 880)

    r = _retry(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["note"] == ""
    assert body["comeback"] == comeback["id"]
    # The continue prompt went NOW, into the same conversation…
    assert len(spawned) == 1
    assert spawned[0]["session_id"] == SID
    assert spawned[0]["message"] == schedule.COMEBACK_PROMPT
    # …and the booked comeback is still there until something answers.
    assert [e["id"] for e in _pending_comebacks()] == [comeback["id"]]

    # The other account answers.
    retry_id = body["entry"]["id"]
    schedule._update(retry_id, state=schedule.SENT, turn="ok",
                     turn_at=_iso(-60), claude_session_id=SID)
    records += [_user(schedule.COMEBACK_PROMPT, -120), _tool_call(-100),
                _reply("Picked it up — the chart is wired.", -60)]
    _write(projects_dir, records)

    row = _row(client)
    assert row["status"] == "done"
    assert row["blocked_reason"] == ""
    assert row["resumes_at"] == 0.0
    _assert_agrees(agent, records, row)
    # THE COMEBACK IS RETIRED BY THE SERVER, no chat frame needed.
    assert _pending_comebacks() == []
    assert {e["state"] for e in _comebacks()} == {schedule.CANCELLED}


# -------------------------------------------------- (3) retry, still limited


def test_3_retry_while_still_limited_is_blocked_again_with_one_comeback(
        client, projects_dir, target, agent, spawned):
    records = [_user("wire up the chart", -900), _limit(-890)]
    _write(projects_dir, records)
    first = _book_comeback(client, target)
    _backdate(first["id"], 880)

    body = _retry(client).json()
    schedule._update(body["entry"]["id"], state=schedule.SENT, turn="failed",
                     turn_at=_iso(-60), claude_session_id=SID)
    records += [_user(schedule.COMEBACK_PROMPT, -120), _limit(-110)]
    _write(projects_dir, records)

    row = _row(client)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "usage_limit"
    _assert_agrees(agent, records, row)
    # Nothing answered, so the first comeback stands…
    assert [e["id"] for e in _pending_comebacks()] == [first["id"]]
    # …and when the chat books another for the same limit, it REPLACES it.
    second = _book_comeback(client, target, due_in=3700)
    assert [e["id"] for e in _pending_comebacks()] == [second["id"]]
    assert _row(client)["resumes_at"] > 0


def test_3b_a_second_retry_never_piles_up_comebacks(client, projects_dir,
                                                    target, spawned):
    _write(projects_dir, [_user("wire up the chart", -900), _limit(-890)])
    _book_comeback(client, target)
    for _ in range(3):
        assert _retry(client).status_code == 200
    assert len(_pending_comebacks()) == 1
    # A press while the first retry is still in flight starts nothing new.
    assert len(spawned) == 1
    assert len([e for e in schedule.list_entries() if "retry_of" in e]) == 1


# ------------------------------------------- (4) reset passes, comeback runs


def test_4_the_comeback_runs_itself_after_the_reset_and_lands_done(
        client, projects_dir, target, agent, spawned):
    records = [_user("wire up the chart", -900), _limit(-890, resets_in=-120)]
    _write(projects_dir, records)
    comeback = _book_comeback(client, target, due_in=-60)
    _backdate(comeback["id"], 880)

    sent = schedule.tick()
    assert [e["id"] for e in sent] == [comeback["id"]], \
        "a comeback whose chat is still stopped is sent, not retired"
    assert spawned and spawned[0]["session_id"] == SID

    schedule._update(comeback["id"], state=schedule.SENT, turn="ok",
                     turn_at=_iso(-30), claude_session_id=SID)
    records += [_user(schedule.COMEBACK_PROMPT, -50),
                _reply("Continuing — done now.", -30)]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] == "done"
    assert row["blocked_reason"] == ""
    _assert_agrees(agent, records, row)


# ------------------------------------------- (5) reset passes, nothing ran


def test_5_a_reset_with_nothing_run_is_not_blocked_without_a_row(
        client, projects_dir, agent):
    """The chat stops drawing a limit row once its reset is behind us; the
    row stops saying blocked at the same moment — with no comeback booked and
    nothing sent since."""
    records = [_user("wire up the chart", -900), _limit(-890, resets_in=-60)]
    _write(projects_dir, records)
    row = _row(client)
    assert not _chat_bottom_is_error(agent, records)
    assert row["status"] == "done"
    assert row["blocked_reason"] == ""
    assert row["failed"] is False
    _assert_agrees(agent, records, row)


def test_5b_a_limit_row_without_a_reset_stays_blocked(client, projects_dir,
                                                      agent):
    """No `quotaLimits` on the row: the chat keeps drawing it, so the row
    keeps saying why."""
    row_ = _limit(-890)
    del row_["quotaLimits"]
    records = [_user("wire up the chart", -900), row_]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] == "blocked" and row["blocked_reason"] == "usage_limit"
    _assert_agrees(agent, records, row)


# --------------------------------- (6) finished elsewhere / page reloaded


def test_6_a_task_finished_elsewhere_is_done_and_its_comeback_retired(
        client, projects_dir, target, agent):
    records = [_user("wire up the chart", -900), _limit(-890)]
    _write(projects_dir, records)
    comeback = _book_comeback(client, target)
    _backdate(comeback["id"], 880)
    # Continued in the terminal on another account — no chat frame was open.
    records += [_user("go on", -300), _reply("Done.", -280)]
    _write(projects_dir, records)

    row = _row(client)
    assert row["status"] == "done"
    assert row["resumes_at"] == 0.0
    _assert_agrees(agent, records, row)
    assert _pending_comebacks() == []


def test_6b_a_stale_comeback_is_called_off_at_send_time(
        client, projects_dir, target, spawned):
    """No listing ran between the reply and the comeback's due time (the app
    was closed): the scheduler itself sees the conversation went on, and
    cancels instead of sending."""
    records = [_user("wire up the chart", -900), _limit(-890),
               _user("go on", -300), _reply("Done.", -280)]
    _write(projects_dir, records)
    entry = schedule.create(str(target), schedule.COMEBACK_PROMPT,
                            datetime.fromtimestamp(time.time() - 30,
                                                   timezone.utc),
                            session_id=SID, title=schedule.COMEBACK_TITLE)
    _backdate(entry["id"], 880)

    assert schedule.tick() == []
    assert spawned == []
    assert {e["state"] for e in _comebacks()} == {schedule.CANCELLED}


def test_6c_a_tool_call_after_the_limit_is_the_window_open(
        client, projects_dir, agent):
    """The API served the retry's first request — a bare tool call — so the
    chat has already dropped the limit row (Akshil, 2026-10-18). The row
    follows: not blocked."""
    records = [_user("wire up the chart", -900), _limit(-890),
               _user("try again", -300), _tool_call(-280)]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] != "blocked"
    _assert_agrees(agent, records, row)


# ------------------------------------------- (7) an ordinary failure


def test_7_a_non_limit_error_is_blocked_and_a_follow_up_clears_it(
        client, projects_dir, agent):
    records = [_user("wire up the chart", -900), _broke(-890)]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "failed"
    _assert_agrees(agent, records, row)

    records += [_user("try again", -300), _reply("Done this time.", -280)]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] == "done"
    assert row["blocked_reason"] == ""
    _assert_agrees(agent, records, row)


def test_7b_a_tool_call_does_not_clear_an_ordinary_failure(
        client, projects_dir, agent):
    """Only the LIMIT is cleared by a bare tool call: an ordinary failure needs
    an answer with words, in the chat and on the row alike."""
    records = [_user("wire up the chart", -900), _tool_call(-895),
               _broke(-890)]
    _write(projects_dir, records)
    row = _row(client)
    assert row["status"] == "blocked"
    _assert_agrees(agent, records, row)


def test_7c_a_scheduled_run_the_watcher_failed_reads_the_transcript(
        client, projects_dir, target, agent):
    """The watcher filed `failed` but the transcript ends on the plan's limit
    row: the row says usage limit (not "failed"), as the chat does."""
    records = [_user("pull the news", -900), _limit(-890)]
    _write(projects_dir, records)
    entry = schedule.create(str(target), "pull the news",
                            datetime.fromtimestamp(time.time() - 905,
                                                   timezone.utc),
                            session_id=SID)
    schedule._update(entry["id"], state=schedule.SENT, turn="failed",
                     fired=_iso(-905), claude_session_id=SID,
                     turn_at=_iso(-880))
    row = _row(client)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "usage_limit"
    _assert_agrees(agent, records, row)


# ------------------------------------------------------- the endpoint itself


def test_retry_without_a_comeback_continues_a_limited_chat(
        client, projects_dir, target, spawned):
    """The comeback was cancelled (or never booked): Retry now still sends the
    continue prompt, into the folder the conversation ran in."""
    _write(projects_dir, [_user("wire up the chart", -900), _limit(-890)],
           cwd=str(target))
    r = _retry(client)
    assert r.status_code == 200, r.text
    assert len(spawned) == 1 and spawned[0]["session_id"] == SID
    assert spawned[0]["message"] == schedule.COMEBACK_PROMPT
    assert _pending_comebacks() == []


def test_retry_refuses_a_chat_the_limit_did_not_stop(client, projects_dir,
                                                     spawned):
    _write(projects_dir, [_user("wire up the chart", -900),
                          _reply("done", -890)])
    r = _retry(client)
    assert r.status_code == 409
    assert spawned == []


def test_retry_needs_the_header_and_a_key(client):
    assert client.post("/api/tasks/limit/retry",
                       json={"key": SID}).status_code in (400, 403)
    assert client.post("/api/tasks/limit/retry", headers=WRITE,
                       json={}).status_code == 400
    assert client.post("/api/tasks/limit/retry", headers=WRITE,
                       json={"key": "pending:abc"}).status_code == 400


def test_the_comeback_constants_match_the_chats():
    """The chat books the comeback (`protocol/quota.ts`) and the server keeps
    it single and retires it (`schedule`): the title and prompt are a wire
    fact between the two and must read the same on both sides."""
    with open(os.path.join("frontend", "src", "apps", "claude", "protocol",
                           "quota.ts"), encoding="utf-8") as f:
        source = f.read()
    flat = " ".join(source.split())
    assert f'CONTINUE_TITLE = "{schedule.COMEBACK_TITLE}"' in flat
    prompt = schedule.COMEBACK_PROMPT
    head, tail = prompt[:40], prompt[-40:]
    assert head in source.replace('" +\n  "', "")
    assert tail in source.replace('" +\n  "', "")
