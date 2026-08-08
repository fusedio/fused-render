"""The session-transcript scan behind the Claude Sessions inbox
(core_apps/sessions/sessions/sessions.py, the runPython target of inbox.py).

Two properties this covers, both of them user-visible:

* **Nothing printed.** Whatever a runPython target writes to stdout is surfaced
  in the browser console by the fused.runPython bridge, so a debug print on the
  scan path is console spam, not a log.
* **Parsed summaries are cached by (path, mtime+size).** The full scan reads and
  json-parses every line of every transcript — tens of megabytes for a heavy
  user — so an unchanged transcript must never be re-parsed, a changed one must
  be, and the cache must not keep entries for transcripts that are gone.

Also covers `allSessionIds`: the scan reports every id it SAW, including
transcripts it could not summarize, which is what stops the page's 5s status
poll from mistaking such a file for a new session and firing a full rescan on
every tick, forever.

The module is a standalone script (stdlib only, no fused_render import — it
ships inside the read-only sessions mount), so it is loaded by path and its
directory constants are redirected at the module object, the same way
test_claude_sessions_api.py redirects PROJECTS_DIR.
"""
import importlib.util
import json
import os
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_PY = os.path.join(REPO_ROOT, "core_apps", "sessions", "sessions", "sessions.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("_sessions_scan", SCAN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def scan(tmp_path):
    """The scan module pointed at an empty projects dir and a private state dir."""
    mod = _load_module()
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "state"
    mod.PROJECTS_DIR = str(projects)
    mod._STATE_DIR = str(state)
    mod.CACHE_FILE = str(state / "summary_cache.json")
    mod.NAMES_FILE = str(state / "session_names.json")
    mod.projects_dir = projects  # test-side handle
    return mod


def _transcript(scan, session_id, *, encoded_dir="-home-x", lines=None):
    d = os.path.join(scan.PROJECTS_DIR, encoded_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{session_id}.jsonl")
    if lines is None:
        lines = [{
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/home/x",
            "message": {"role": "user", "content": "hello"},
        }]
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return path


def _count_parses(scan):
    """Wrap _summarize_session with a counter; returns the list of parsed paths."""
    parsed = []
    original = scan._summarize_session

    def spy(path, project_dirname):
        parsed.append(path)
        return original(path, project_dirname)

    scan._summarize_session = spy
    return parsed


# -- no console spam ----------------------------------------------------------


def test_scan_prints_nothing(scan, capsys):
    _transcript(scan, "aaa")
    scan.main()
    assert capsys.readouterr().out == ""


def test_status_poll_prints_nothing(scan, capsys):
    _transcript(scan, "aaa")
    scan.main(mode="status")
    assert capsys.readouterr().out == ""


def test_session_detail_prints_nothing(scan, capsys):
    _transcript(scan, "aaa")
    scan.session_detail("aaa")
    assert capsys.readouterr().out == ""


# -- parsed-summary cache ----------------------------------------------------


def test_unchanged_transcript_is_not_reparsed(scan):
    _transcript(scan, "aaa")
    first = scan.main()
    parsed = _count_parses(scan)
    second = scan.main()
    assert parsed == []
    assert [s["sessionId"] for s in second["sessions"]] == \
        [s["sessionId"] for s in first["sessions"]]
    assert second["sessions"][0]["userMessages"] == 1


def test_changed_transcript_is_reparsed(scan):
    path = _transcript(scan, "aaa")
    scan.main()
    parsed = _count_parses(scan)
    time.sleep(0.01)  # a same-nanosecond append would keep the stamp identical
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user",
            "timestamp": "2026-01-02T00:00:00Z",
            "message": {"role": "user", "content": "again"},
        }) + "\n")
    out = scan.main()
    assert parsed == [path]
    assert out["sessions"][0]["userMessages"] == 2
    assert out["sessions"][0]["endedAt"] == "2026-01-02T00:00:00Z"


def test_activity_mtime_is_never_served_from_cache(scan):
    """The "running" badge value depends on the wall clock, so it is recomputed
    on every scan and must not be stored in the cache entry."""
    path = _transcript(scan, "aaa")
    scan.main()
    entry = json.load(open(scan.CACHE_FILE, encoding="utf-8"))["entries"][path]
    assert "mtime" not in entry["summary"]
    # ...and the scan still returns one
    assert scan.main()["sessions"][0]["mtime"]


def test_cache_drops_entries_for_deleted_transcripts(scan):
    keep = _transcript(scan, "aaa")
    gone = _transcript(scan, "bbb")
    scan.main()
    os.remove(gone)
    scan.main()
    entries = json.load(open(scan.CACHE_FILE, encoding="utf-8"))["entries"]
    assert list(entries) == [keep]


def test_unreadable_cache_falls_back_to_parsing(scan):
    _transcript(scan, "aaa")
    scan.main()
    os.makedirs(scan._STATE_DIR, exist_ok=True)
    with open(scan.CACHE_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    parsed = _count_parses(scan)
    out = scan.main()
    assert len(parsed) == 1
    assert len(out["sessions"]) == 1


def test_cache_from_a_different_version_is_ignored(scan):
    path = _transcript(scan, "aaa")
    scan.main()
    os.makedirs(scan._STATE_DIR, exist_ok=True)
    with open(scan.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": scan.CACHE_VERSION + 1, "entries": {path: {}}}, f)
    parsed = _count_parses(scan)
    assert len(scan.main()["sessions"]) == 1
    assert parsed == [path]


# -- allSessionIds: what stops the poll from rescanning forever ---------------


def test_all_session_ids_includes_unsummarizable_transcripts(scan):
    _transcript(scan, "aaa")
    # housekeeping-only, no timestamped message: no summary, but it IS a file
    # the status poll will report, so the scan has to admit it saw it.
    _transcript(scan, "bbb", lines=[{"type": "summary", "summary": "x"}])
    out = scan.main()
    assert [s["sessionId"] for s in out["sessions"]] == ["aaa"]
    assert out["allSessionIds"] == ["aaa", "bbb"]
    poll_ids = {s["sessionId"] for s in scan.main(mode="status")["statuses"]}
    assert poll_ids <= set(out["allSessionIds"])  # nothing looks "new"


def test_all_session_ids_covers_rows_beyond_the_limit(scan):
    for sid in ("aaa", "bbb", "ccc"):
        _transcript(scan, sid)
    out = scan.main(limit=1)
    assert len(out["sessions"]) == 1
    assert out["allSessionIds"] == ["aaa", "bbb", "ccc"]


def test_inbox_passes_all_session_ids_through(tmp_path):
    """inbox.py is what the page actually calls, so the id set has to survive
    its triage overlay — and its stdout has to stay clean end to end."""
    import subprocess
    import sys

    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "-home-x"
    projects.mkdir(parents=True)
    (projects / "aaa.jsonl").write_text(json.dumps({
        "type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/home/x",
        "message": {"role": "user", "content": "hello"},
    }) + "\n")
    (projects / "bbb.jsonl").write_text(json.dumps({"type": "summary"}) + "\n")
    env = dict(os.environ, HOME=str(home),
               FUSED_RENDER_HOME=str(tmp_path / "fr-home"))
    env.pop("USERPROFILE", None)
    out = subprocess.run(
        [sys.executable, "-c",
         "import inbox, json; print(json.dumps(inbox.main()['allSessionIds']))"],
        cwd=os.path.join(REPO_ROOT, "core_apps", "sessions"),
        env=env, capture_output=True, text=True, check=True,
    )
    assert json.loads(out.stdout.strip()) == ["aaa", "bbb"]


def test_unsummarizable_transcript_is_remembered_not_reparsed(scan):
    _transcript(scan, "bbb", lines=[{"type": "summary", "summary": "x"}])
    scan.main()
    parsed = _count_parses(scan)
    out = scan.main()
    assert parsed == []
    assert out["sessions"] == []
    assert out["allSessionIds"] == ["bbb"]
