"""The session inbox's background poll must not degenerate into a full rescan
on every tick.

`core_apps/sessions/inbox.html` keeps the "running" dots live with a cheap
`mode="status"` poll (ids + mtimes, no parsing) and falls back to a full
`load()` — which re-parses EVERY transcript end to end — only when a session it
has never seen shows up. The two scans do not agree on WHICH sessions exist:

* `sessions.py::main` drops any transcript whose `_summarize_session` finds no
  timestamped entry (an empty file, a session that has only just been created,
  a truncated/corrupt one), and then clamps the survivors to `limit`.
* `sessions.py::main(mode="status")` reports EVERY `*.jsonl` on disk,
  unconditionally.

So one stray timestamp-less transcript — or a 501st session — made "an id I
don't know" permanently true, and the poll re-ran the full scan every 5s
forever. That is the usage bug these tests pin: the reload is gated on the
unknown session being *active*, which is exactly the state a genuinely new
session is in and the state a permanently-absent id can never reach.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "core_apps", "sessions")


@pytest.fixture()
def sessions_mod(tmp_path, monkeypatch):
    """`core_apps/sessions/sessions/sessions.py` with its store pointed at tmp."""
    monkeypatch.syspath_prepend(os.path.join(_APP, "sessions"))
    sys.modules.pop("sessions", None)
    import sessions as mod

    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(mod, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(mod, "NAMES_FILE", str(tmp_path / "names.json"))
    mod._projects = projects
    yield mod
    sys.modules.pop("sessions", None)


def _write(mod, session_id, lines):
    d = os.path.join(mod.PROJECTS_DIR, "-tmp-proj")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, session_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


@pytest.fixture()
def source():
    with open(os.path.join(_APP, "inbox.html"), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------- the disagreeing scans


def test_a_timestampless_transcript_is_reported_by_status_but_not_by_the_scan(
    sessions_mod,
):
    # The exact shape that armed the loop: the summarizer needs a timestamp to
    # build a row, the status poll needs nothing but the filename.
    _write(sessions_mod, "no-ts", [{"type": "user", "cwd": "/tmp/proj"}])

    assert sessions_mod.main()["sessions"] == []
    statuses = sessions_mod.main(mode="status")["statuses"]
    assert [s["sessionId"] for s in statuses] == ["no-ts"]


def test_the_scan_clamps_to_limit_while_status_does_not(sessions_mod):
    for i in range(4):
        _write(sessions_mod, f"s{i}", [
            {"type": "user", "timestamp": f"2026-01-0{i + 1}T00:00:00Z",
             "cwd": "/tmp/proj", "message": {"content": "hi"}},
        ])

    assert len(sessions_mod.main(limit=2)["sessions"]) == 2
    assert len(sessions_mod.main(mode="status")["statuses"]) == 4


# -------------------------------------------------- how the page reacts to it


def test_an_unknown_session_only_forces_a_reload_when_it_is_running(source):
    # The whole fix in one line: the reload predicate consults isRunning on the
    # polled mtime, so an id that the scan will never return (no timestamp, or
    # past the limit clamp) cannot arm it. A bare `else unknown = true` — the
    # old shape — must not come back.
    body = source[source.index("async function pollStatus"):]
    body = body[: body.index("\n}")]
    assert "isRunning(" in body, "the unknown-id branch must test for activity"
    assert "else unknown = true" not in body.replace("  ", " ")


def test_the_poll_reschedules_itself_instead_of_a_fixed_interval(source):
    # setInterval at a fixed 5s both stacks (a full load takes longer than the
    # period) and keeps paying for a repo where nothing is running. The poll
    # now re-arms itself and backs off when the inbox is idle.
    assert "setInterval(pollStatus" not in source
    assert "IDLE_POLL_MS" in source and "ACTIVE_POLL_MS" in source


def test_returning_to_the_tab_does_not_rescan_every_time(source):
    # visibilitychange fired a full load() on every flip back to the window.
    # It still refreshes, but not more often than RELOAD_COOLDOWN_MS.
    vis = source[source.index('"visibilitychange"'):]
    vis = vis[: vis.index("\n")]
    assert "load()" not in vis, "the raw full scan must not hang off the event"
    assert "RELOAD_COOLDOWN_MS" in source
