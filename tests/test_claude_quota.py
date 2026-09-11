"""The plan window the CLI reports beside every reply, and what `_poll` makes of it.

Claude Code writes one `rate_limit_event` row after EVERY API response (CLI
2.1.267/268, verified over 390 real `out.jsonl` files): the status of the plan
window that gated the request, the epoch second it resets, which window it is,
and the utilization of every window. The page used to drop the row and, on a
limit hit, regex the reset time back out of the failure TEXT — so these pin the
row's translation into `quota`, the one field the comeback is scheduled on.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_quota_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _poll(agent, run_dir, rows, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")
    return agent._poll("run")


def _event(status="allowed", resets_at=1789066800, kind="five_hour", **extra):
    """A `rate_limit_event` as CLI 2.1.267 writes it (captured verbatim, ids
    trimmed)."""
    info = {"status": status, "resetsAt": resets_at, "rateLimitType": kind,
            "overageStatus": "rejected",
            "overageDisabledReason": "member_zero_credit_limit",
            "isUsingOverage": False,
            "unifiedWindows": {
                "five_hour": {"utilization": 0.17, "resetsAt": resets_at},
                "seven_day": {"utilization": 0.06, "resetsAt": 1789614000}}}
    info.update(extra)
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "uuid": "u", "session_id": "s"}


def _limit_hit(resets_at=1789066800):
    """The three rows a headless run writes when the plan window is spent
    (captured from run 20260910-195317-f9d26d)."""
    text = "You've hit your session limit · resets 12:30am (Asia/Calcutta)"
    return [
        _event("rejected", resets_at,
               unifiedWindows={"five_hour": {"utilization": 1,
                                             "resetsAt": resets_at},
                               "seven_day": {"utilization": 0.83,
                                             "resetsAt": 1789376400}}),
        {"type": "assistant", "session_id": "s", "is_api_error_message": True,
         "error": "rate_limit",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": True,
         "result": text, "session_id": "s"},
    ]


def test_no_event_means_no_quota(agent, run_dir):
    data = _poll(agent, run_dir, [{"type": "system", "subtype": "init",
                                   "session_id": "s"}])
    assert data["quota"] is None


def test_the_event_is_translated_to_snake_case(agent, run_dir):
    data = _poll(agent, run_dir, [_event()])
    assert data["quota"] == {
        "status": "allowed", "type": "five_hour", "resets_at": 1789066800,
        "utilization": None,
        "windows": {"five_hour": {"utilization": 0.17,
                                  "resets_at": 1789066800},
                    "seven_day": {"utilization": 0.06,
                                  "resets_at": 1789614000}}}


def test_a_warning_carries_its_utilization(agent, run_dir):
    """`allowed_warning` is the CLI's own threshold signal — the row grows a
    top-level `utilization` and the page shows a pill off it."""
    data = _poll(agent, run_dir, [_event("allowed_warning", utilization=0.93,
                                         surpassedThreshold=0.9)])
    assert data["quota"]["status"] == "allowed_warning"
    assert data["quota"]["utilization"] == 0.93


def test_the_latest_event_wins(agent, run_dir):
    data = _poll(agent, run_dir, [_event("allowed", resets_at=1),
                                  _event("allowed_warning", resets_at=2)])
    assert data["quota"]["status"] == "allowed_warning"
    assert data["quota"]["resets_at"] == 2


def test_a_limit_hit_reports_rejected_beside_the_error(agent, run_dir):
    """THE case the field exists for: the turn ends with the CLI's own limit
    text as `error` (rewritten by `_account_error`, same as before) AND
    `quota.status == rejected` with the epoch the window reopens — which is
    what the page schedules the comeback on."""
    data = _poll(agent, run_dir, _limit_hit(), alive=False)
    assert data["done"] is True
    assert "usage limit was reached" in data["error"]
    assert "resets 12:30am" in data["error"]
    assert data["quota"]["status"] == "rejected"
    assert data["quota"]["resets_at"] == 1789066800
    assert data["quota"]["windows"]["five_hour"]["utilization"] == 1


def test_an_unreadable_event_is_ignored(agent, run_dir):
    data = _poll(agent, run_dir, [
        {"type": "rate_limit_event", "rate_limit_info": "nope"},
        {"type": "rate_limit_event", "rate_limit_info": {"resetsAt": "soon"}},
    ])
    assert data["quota"] is None


def test_history_carries_the_transcripts_quota_on_the_error_turn(agent, tmp_path,
                                                                  monkeypatch):
    """The persisted transcript spells the same facts `quotaLimits`, camelCase,
    on the failed assistant row — a restored limit card gets the reset time
    from there."""
    file = tmp_path / "proj" / "index.html"
    file.parent.mkdir()
    file.write_text("<html></html>")
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    pdir = projects / agent._munge(agent._workdir(str(file)))
    pdir.mkdir(parents=True)
    rows = [
        {"type": "user", "uuid": "u1",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "isApiErrorMessage": True,
         "apiErrorStatus": 429, "error": "rate_limit",
         "quotaLimits": {"status": "rejected", "resetsAt": 1789066800,
                         "rateLimitType": "five_hour"},
         "message": {"role": "assistant", "content": [
             {"type": "text",
              "text": "You've hit your session limit · resets 12:30am"}]}},
    ]
    (pdir / "sess.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    data = agent._history(str(file), "sess")
    err = [t for t in data["turns"] if t["role"] == "error"]
    assert len(err) == 1
    assert err[0]["quota"]["status"] == "rejected"
    assert err[0]["quota"]["resets_at"] == 1789066800
    assert err[0]["quota"]["windows"] == {}
