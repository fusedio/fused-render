"""The session inbox's List/Board choice has to survive leaving the page.

`core_apps/sessions/inbox.html` renders two layouts and keeps the choice in the
`layout` URL param, like every other filter on that page. A param alone was not
enough to REMEMBER the choice, though, and the reason is structural rather than
an oversight:

* The shell replays a file's last query on a bare open through per-file session
  restore (LSN-*, `platform/lib/session.ts`).
* Both halves of it — `useSessionRestore` and `useSessionTracking` — opt out
  when `writable !== true`, because the sidecar write is server-refused on a
  read-only mount and firing it is a guaranteed-null round trip.
* The sessions app IS a read-only mount (`ensure_builtin_mounts` stamps
  `read_only: True` on the `:archive:` record).

So `?layout=board` was never restorable, and every fresh visit to /sessions
snapped a board user back to the list. localStorage is the one store this page
owns, and these tests pin the precedence it introduced: the param still wins
when present (a shared link means what it says), the remembered value is only a
fallback, and nothing here may throw when storage is blocked.

The behaviour is checked by running the page's REAL layout code under node (the
`_js_block` approach of test_map_template_escaping.py / test_calls.py — a copy
of the logic in the test would keep passing after the shipping code regressed).
"""
import json
import os
import re
import shutil
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INBOX = os.path.join(os.path.dirname(_HERE), "core_apps", "sessions", "inbox.html")

LAYOUT_KEY = "fused.sessions.layout"


@pytest.fixture()
def source():
    with open(_INBOX, encoding="utf-8") as f:
        return f.read()


def _layout_block(src):
    """The shipping layout declarations, verbatim: LAYOUT_KEY .. curLayout."""
    start = src.find("const LAYOUT_KEY")
    assert start != -1, "inbox.html no longer declares LAYOUT_KEY — did the memory go away?"
    tail = 'asLayout(storedLayout()) || "list";'
    end = src.find(tail, start)
    assert end != -1, "curLayout no longer falls back through storedLayout()"
    return src[start:end + len(tail)]


def _run(tmp_path, src, body, *, param=None, stored=None, blocked=False):
    """Drive the real block with stubbed params/localStorage; return its stdout.

    `param` is what fused.params.get("layout") answers ("" when absent, which is
    what the runtime actually returns for an unset param — not undefined).
    `blocked` models private mode: both storage calls throw.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the page's JS")
    if blocked:
        store = ("{ getItem() { throw new Error('blocked'); },"
                 "  setItem() { throw new Error('blocked'); } }")
    else:
        held = {} if stored is None else {LAYOUT_KEY: stored}
        store = ("{ writes: [],"
                 f" getItem(k) {{ return {json.dumps(held)}[k] ?? null; }},"
                 " setItem(k, v) { this.writes.push([k, v]); } }")
    params = "{ get: (k) => (%s)[k] ?? '' }" % json.dumps(
        {} if param is None else {"layout": param})
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        f"globalThis.localStorage = {store};\n"
        f"globalThis.fused = {{ params: {params} }};\n"
        f"{_layout_block(src)}\n{body}\n",
        encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _layout(tmp_path, src, **kw):
    return _run(tmp_path, src, "console.log(curLayout());", **kw)


# ------------------------------------------------------------- precedence

def test_the_default_is_still_the_list(tmp_path, source):
    # No param, nothing remembered: the page opens the way it always has. The
    # memory must not change what a first-time visitor sees.
    assert _layout(tmp_path, source) == "list"


def test_the_remembered_layout_is_used_when_the_url_says_nothing(tmp_path, source):
    # The whole point: a bare /sessions open for someone who works the board.
    assert _layout(tmp_path, source, stored="board") == "board"
    assert _layout(tmp_path, source, stored="list") == "list"


def test_the_url_param_beats_the_memory(tmp_path, source):
    # A shared or bookmarked link means what it says, in both directions —
    # including the case that looks like a no-op but is not: remembered board,
    # link says list.
    assert _layout(tmp_path, source, param="list", stored="board") == "list"
    assert _layout(tmp_path, source, param="board", stored="list") == "board"


def test_a_layout_the_page_cannot_render_falls_through(tmp_path, source):
    # A hand-typed param or a stale key must not reach the renderer, which would
    # take neither branch and leave #content empty. Junk in the param still
    # yields to a good memory rather than skipping straight to the default.
    assert _layout(tmp_path, source, param="kanban") == "list"
    assert _layout(tmp_path, source, stored="grid") == "list"
    assert _layout(tmp_path, source, param="grid", stored="board") == "board"


# ---------------------------------------------------------------- writing

def test_picking_a_layout_records_it_under_the_shared_key(tmp_path, source):
    got = _run(tmp_path, source,
               "rememberLayout('board');"
               " console.log(JSON.stringify(localStorage.writes));")
    assert json.loads(got) == [[LAYOUT_KEY, "board"]]


def test_blocked_storage_costs_the_memory_and_nothing_else(tmp_path, source):
    # Private mode / storage-denied embeds: reading and writing both throw, and
    # neither may take the page down — the param still holds for this visit.
    assert _layout(tmp_path, source, param="board", blocked=True) == "board"
    assert _run(tmp_path, source,
                "rememberLayout('board'); console.log('survived');",
                blocked=True) == "survived"


# ------------------------------------------------- the choice reaches storage

def test_the_toggle_writes_the_memory_and_not_only_the_param(source):
    # The regression this guards is a one-line revert away: dropping
    # rememberLayout from the click handler leaves everything above green while
    # the page forgets again.
    # Two places sweep the toggle's buttons — render() repaints the active
    # class, and this one wires the click. Take the wiring, not the repaint.
    m = re.search(
        r'querySelectorAll\("#layout-toggle button"\)[^;]*?b\.onclick.*?\}\);',
        source, re.S)
    assert m, "the layout toggle no longer wires its buttons"
    handler = m.group(0)
    assert "rememberLayout(" in handler
    assert 'fused.params.set("layout"' in handler
