"""The chat header names the session the way the rest of the app names it.

A session IS a task, 1:1 (`fused_render/tasks_store.py`), and every other
surface that prints that object prints TASK-nnn: the List row, the calendar
chip, the composer block inside this very template. The header printed the
first eight characters of the session uuid instead — the same object, in a
vocabulary nothing else on screen speaks, so it could not be carried to the
Tasks page or quoted to anyone.

Two claims are pinned here, and they are the two that can regress
independently: the header ASKS for the number (from the one endpoint that
allocates it), and it FAILS OPEN to the hash when the answer does not come.
The second is not a nicety — a session that has just been created is not in the
listing yet, and a chat whose header goes blank while it waits would be a worse
page than the one this replaces.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude",
                         "template.html")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def code(source) -> str:
    """The template with comments stripped — its comments RECORD the decisions
    and would otherwise satisfy a search for the thing they describe."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


def _fn(code: str, opening: str) -> str:
    body = code[code.index(opening):]
    return body[:body.index("\n}")]


def test_the_number_comes_from_the_listing_that_allocates_it(code):
    """TASK-nnn is allocated per project by `tasks_store` and only /api/tasks
    hands it out, so the header cannot derive it and must read it. The row is
    found by session id because a task's key IS the session id
    (routers/tasks.py `_collect`)."""
    load = _fn(code, "async function loadTaskId(")
    assert 'fetch("/api/tasks")' in load
    assert "t.key === id" in load
    assert "task.task_id" in load


def test_a_number_is_read_once_per_session_and_never_polled(code):
    """A number does not change once allocated, and the listing carries every
    task on the machine — so the cache is the point, not an optimisation."""
    load = _fn(code, "async function loadTaskId(")
    assert "if (taskIds.has(id) || taskIdBusy === id) return;" in load
    # nothing cached on the empty answer, so a session the listing has not seen
    # yet is retried rather than pinned to its hash forever
    assert "if (!num) return;" in load
    assert load.index("if (!num) return;") < load.index("taskIds.set(id, num)")


def test_nothing_is_cached_when_the_listing_cannot_be_read(code):
    load = _fn(code, "async function loadTaskId(")
    fail = load[load.index("} catch (err) {"):]
    assert "taskIds.set" not in fail


# --------------------------------------------- the page's own JS, under node
#
# Structure says the header reads the listing; it cannot say what it PAINTS.
# "Number when there is one, hash when there is not" is the whole behaviour and
# both directions are one `||` away from being backwards, so showSession is
# lifted out of the page and run against a stub for the DOM and params it
# touches. Same extraction as tests/test_claude_composer_block.py's, kept as
# its own copy for the reason that suite gives: the suites are independent and
# a shared harness would couple them.

_HARNESS = """
const calls = [];
const sessionEl = { textContent: "", title: "" };
const fused = { params: { get: () => SESSION } };
const taskIds = new Map(TASKS);
let taskIdBusy = "";
function loadTaskId(id) { calls.push(id); }
%s
}
showSession();
console.log(JSON.stringify({ text: sessionEl.textContent,
                             title: sessionEl.title, calls }));
"""


def _run(code: str, session, cached=()):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own header function")
    script = ("const SESSION = %s;\nconst TASKS = %s;\n"
              % (json.dumps(session), json.dumps([list(p) for p in cached]))
              + _HARNESS % _fn(code, "function showSession("))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_header_shows_the_task_id_once_it_is_known(code):
    got = _run(code, "sess-abcdef0123", cached=[("sess-abcdef0123", "TASK-023")])
    assert got["text"] == "TASK-023"


def test_it_falls_back_to_the_hash_and_still_asks(code):
    """The session with no number yet keeps exactly what the header showed
    before this change, and the ask goes out anyway — that is what turns the
    fallback into a first frame rather than a final answer."""
    got = _run(code, "sess-abcdef0123")
    assert got["text"] == "sess-abc"
    assert got["calls"] == ["sess-abcdef0123"]


def test_no_session_is_an_empty_cell_and_no_request(code):
    got = _run(code, "")
    assert got["text"] == ""
    assert got["calls"] == []


def test_the_full_session_id_stays_reachable_on_the_title(code):
    """The uuid is what a log line or a bug report needs; it just does not need
    header width."""
    got = _run(code, "sess-abcdef0123", cached=[("sess-abcdef0123", "TASK-023")])
    assert got["title"] == "sess-abcdef0123"
