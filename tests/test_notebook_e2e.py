"""End-to-end test for the notebook template: a real server (with
FUSED_RENDER_CORE_TEMPLATES pointing at this checkout's templates), a real
kernel daemon + kernel_body subprocess, driven through Playwright. Skipped
when playwright or the built React shell is missing.

Run serially: PYTHONPATH=<checkout> python -m pytest tests/test_notebook_e2e.py -o addopts=""
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(ROOT, "fused_render", "static", "shell-dist", "index.html")
# daemon state nests under the (isolated) app home — see kernel.py _cache_dir
DEFAULT_DAEMON_STATE = os.path.expanduser("~/.cache/fused-render-notebook/daemon.json")

pytestmark = pytest.mark.skipif(
    not os.path.exists(SHELL), reason="React shell not built")

SAMPLE_NB = {
    "cells": [
        {"cell_type": "markdown", "id": "m1", "metadata": {},
         "source": "# Sample notebook\n\nHello *world*"},
        # saved output deliberately differs from what the code prints, so the
        # test can tell a saved render from a fresh execution
        {"cell_type": "code", "id": "c1", "metadata": {}, "execution_count": 1,
         "source": "print('hello run')",
         "outputs": [
             {"output_type": "stream", "name": "stdout", "text": "saved hello\n"},
             {"output_type": "display_data", "data": {
                 "text/html": "<img src='/no-such-output-image' onerror='window.__nbXss=true'>"}},
         ]},
        {"cell_type": "code", "id": "c2", "metadata": {},
         "execution_count": None, "source": "x = 41", "outputs": []},
        {"cell_type": "code", "id": "c3", "metadata": {},
         "execution_count": None, "source": "x + 1", "outputs": []},
        {"cell_type": "code", "id": "c4", "metadata": {},
         "execution_count": None,
         "source": "import matplotlib\nmatplotlib.use('Agg')\n"
                   "import matplotlib.pyplot as plt\n"
                   "plt.plot([1, 2, 3])\nplt.show()",
         "outputs": []},
        {"cell_type": "code", "id": "c5", "metadata": {},
         "execution_count": None, "source": "1/0", "outputs": []},
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _daemon_state_path(home):
    return os.path.join(str(home), "cache", "notebook-daemon", "daemon.json")


def _quit_notebook_daemon(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        urllib.request.urlopen(
            f"http://127.0.0.1:{st['port']}/quit?t={st.get('token', '')}",
            timeout=3).read()
    except (OSError, ValueError):
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("nb-home")
    work = tmp_path_factory.mktemp("nb-work")
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["FUSED_RENDER_HOME"] = str(home)
    env["FUSED_RENDER_CORE_TEMPLATES"] = os.path.join(ROOT, "fused_render", "templates")
    log_path = home / "server.log"
    # log to a file — an undrained PIPE fills and blocks the server mid-run
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fused_render.cli", "serve",
             "--port", str(port), "--no-browser", "--start-dir", str(work)],
            env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2).read()
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(
                    "server exited:\n" + log_path.read_text("utf-8", errors="replace")[-2000:])
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("server did not come up")
    yield {"port": port, "work": work, "home": home}
    _quit_notebook_daemon(_daemon_state_path(home))
    proc.kill()
    proc.wait(timeout=15)


@pytest.fixture(scope="module")
def page_and_nb(server):
    nb_path = os.path.join(str(server["work"]), "sample.ipynb")
    with open(nb_path, "w", encoding="utf-8") as f:
        json.dump(SAMPLE_NB, f, indent=1)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        url_path = nb_path.replace("\\", "/")
        page.goto(f"http://127.0.0.1:{server['port']}/view/{url_path}")
        frame = _find_frame(page)
        _wait_for(lambda: frame.locator(".cell").count() == 6 or None, 30)
        yield page, frame, nb_path
        browser.close()


def _find_frame(page, containing=None):
    def frame_or_escape():
        # the shell's welcome tour blocks the view iframe until dismissed
        page.keyboard.press("Escape")
        return next((f for f in page.frames if "/render" in f.url
                     and (containing is None or containing in f.url)), None)

    return _wait_for(frame_or_escape, 30, step=0.5)


def _view_url(path):
    # mirror the shell's /view codec: forward slashes, per-segment encoding
    return "/view/" + "/".join(
        quote(seg, safe="") for seg in path.replace("\\", "/").split("/") if seg)


def _wait_for(fn, timeout, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s: {fn}")


def _run_cell(frame, cell_id):
    frame.locator(f'.cell[data-id="{cell_id}"] .run-btn').dispatch_event("click")


def _cell_text(frame, cell_id, selector):
    loc = frame.locator(f'.cell[data-id="{cell_id}"] {selector}')
    return loc.inner_text() if loc.count() else ""


def test_notebook_end_to_end(page_and_nb):
    page, frame, nb_path = page_and_nb

    # notebook is the default mode for .ipynb; saved outputs render on load
    assert frame.locator("#nb").count() == 1
    assert "Sample notebook" in _cell_text(frame, "m1", ".md-view h1")
    assert "saved hello" in _cell_text(frame, "c1", ".console")
    _wait_for(lambda: frame.locator(".html-out").count() == 1 or None, 10)
    time.sleep(0.5)  # let a failing image fire its onerror handler if scripts can run
    assert frame.evaluate("window.__nbXss === true") is False

    # run the print cell: fresh stdout replaces the saved output, count bumps
    _run_cell(frame, "c1")
    _wait_for(lambda: "hello run" in _cell_text(frame, "c1", ".console"), 90)
    _wait_for(lambda: _cell_text(frame, "c1", ".count") == "[1]", 15)
    # the gutter shows the cell's run duration
    _wait_for(lambda: re.match(r"^\d+(\.\d+)?s$", _cell_text(frame, "c1", ".time") or ""), 15)

    # state persists across cells
    _run_cell(frame, "c2")
    _run_cell(frame, "c3")
    _wait_for(lambda: "42" in _cell_text(frame, "c3", ".outputs"), 30)
    _wait_for(lambda: _cell_text(frame, "c3", ".count") == "[3]", 15)

    # matplotlib cell yields an inline image
    _run_cell(frame, "c4")
    _wait_for(lambda: frame.locator('.cell[data-id="c4"] .outputs img').count() == 1, 90)

    # error cell shows a traceback
    _run_cell(frame, "c5")
    _wait_for(lambda: "ZeroDivisionError" in _cell_text(frame, "c5", ".outputs .err-out"), 30)

    # an explicit save persists the outputs as valid nbformat JSON on disk
    _wait_for(lambda: frame.locator("#status-text").inner_text() == "Unsaved changes", 15)
    frame.locator("#save").dispatch_event("click")
    try:
        _wait_for(lambda: frame.locator("#status-text").inner_text() == "Saved", 30)
    except AssertionError:
        raise AssertionError(
            "save did not land; status="
            f"{frame.locator('#status-text').inner_text()!r} "
            f"toast={frame.locator('#toast').inner_text()!r}")
    with open(nb_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["nbformat"] == 4
    by_id = {c["id"]: c for c in saved["cells"]}
    assert by_id["c1"]["outputs"][0]["text"] == "hello run\n"
    assert by_id["c1"]["execution_count"] == 1
    assert by_id["c3"]["outputs"][0]["data"]["text/plain"] == "42"
    assert any((o.get("data") or {}).get("image/png")
               for o in by_id["c4"]["outputs"])
    errors = [o for o in by_id["c5"]["outputs"] if o["output_type"] == "error"]
    assert errors and errors[0]["ename"] == "ZeroDivisionError"
    assert by_id["m1"].get("outputs") is None  # markdown cells carry no outputs

    # restart clears kernel state: x is gone
    frame.locator("#restart").dispatch_event("click")
    _wait_for(lambda: "Idle" in frame.locator("#kchip-text").inner_text(), 60)
    _run_cell(frame, "c3")
    _wait_for(lambda: "NameError" in _cell_text(frame, "c3", ".outputs .err-out"), 30)


def test_restart_failure_leaves_kernel_retryable(page_and_nb):
    page, frame, _ = page_and_nb

    def fail_restart(route):
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"error": "test restart failure"}))

    page.route("**/kernel/restart?*", fail_restart)
    try:
        frame.locator("#restart").dispatch_event("click")
        _wait_for(lambda: "Kernel dead" in frame.locator("#kchip-text").inner_text(), 15)
    finally:
        page.unroute("**/kernel/restart?*", fail_restart)

    _run_cell(frame, "c1")
    _wait_for(lambda: "hello run" in _cell_text(frame, "c1", ".console"), 60)


# ------------------------------------------------------------------ Ask AI
# /api/ai is intercepted with Playwright routes, so these cover the template's
# use of fused.ai against both response shapes runtime.js parses: plain JSON
# (the non-ndjson arm — errors) and streaming NDJSON (chunk lines + done).

def _ndjson_ai_response(chunks, full_text):
    lines = [json.dumps({"type": "chunk", "text": t}) for t in chunks]
    lines.append(json.dumps({"type": "done", "ok": True,
                             "result": {"text": full_text, "model": "test",
                                        "usage": None}}))
    return "\n".join(lines) + "\n"


def _ai_row(frame, cell_id):
    return frame.locator(f'.cell[data-id="{cell_id}"] .ai-row')


def _ai_status(frame, cell_id):
    return frame.locator(f'.cell[data-id="{cell_id}"] .ai-status')


def _cell_code(frame, cell_id):
    return frame.locator(f'.cell[data-id="{cell_id}"] .cm-content').inner_text()


def test_ask_ai_streaming_writes_code(page_and_nb):
    page, frame, nb_path = page_and_nb
    seen = {}

    def handler(route):
        seen["body"] = json.loads(route.request.post_data)
        route.fulfill(status=200, content_type="application/x-ndjson",
                      body=_ndjson_ai_response(
                          ["```python\nprint('ai w", "rote this')\n```"],
                          "```python\nprint('ai wrote this')\n```"))

    page.route("**/api/ai", handler)
    try:
        # rail sparkle button opens the prompt row
        frame.locator('.cell[data-id="c2"] .rail button[title^="Ask AI"]').dispatch_event("click")
        _wait_for(lambda: _ai_row(frame, "c2").is_visible(), 10)
        box = _ai_row(frame, "c2").locator("input")
        box.fill("print a message")
        box.press("Enter")
        _wait_for(lambda: "Review the code" in _ai_status(frame, "c2").inner_text(), 20)
        # fences stripped, code landed in the editor
        assert _cell_code(frame, "c2").strip() == "print('ai wrote this')"
        body = seen["body"]
        assert body["stream"] is True
        assert "exactly one Jupyter notebook cell" in body["system_prompt"]
        assert "Request: print a message" in body["prompt"]
        assert "print('hello run')" in body["prompt"]  # earlier-cell context
        # generated code is runnable through the normal path
        _run_cell(frame, "c2")
        _wait_for(lambda: "ai wrote this" in _cell_text(frame, "c2", ".console"), 60)
    finally:
        page.unroute("**/api/ai")


def test_ask_ai_plain_json_error_restores_cell(page_and_nb):
    page, frame, nb_path = page_and_nb

    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": False, "error": {
                          "type": "ai_unavailable", "message": "no cli"}}))

    page.route("**/api/ai", handler)
    try:
        before = _cell_code(frame, "c3")
        # Ctrl+I on the focused cell opens the prompt row (press focuses the
        # editor itself — a real click can be swallowed by the shell tour overlay)
        frame.locator('.cell[data-id="c3"] .cm-content').press("Control+i")
        _wait_for(lambda: _ai_row(frame, "c3").is_visible(), 10)
        box = _ai_row(frame, "c3").locator("input")
        box.fill("do something")
        box.press("Enter")
        _wait_for(lambda: "AI is unavailable" in _ai_status(frame, "c3").inner_text(), 20)
        assert _cell_code(frame, "c3") == before
    finally:
        page.unroute("**/api/ai")


def test_ask_ai_markdown_cell(page_and_nb):
    page, frame, nb_path = page_and_nb
    seen = {}
    md = "## AI section\n\nInner code survives:\n\n```python\nx = 1\n```"
    wrapped = "```markdown\n" + md + "\n```"

    def handler(route):
        seen["body"] = json.loads(route.request.post_data)
        route.fulfill(status=200, content_type="application/x-ndjson",
                      body=_ndjson_ai_response([wrapped[:20], wrapped[20:]], wrapped))

    page.route("**/api/ai", handler)
    try:
        frame.locator('.cell[data-id="m1"] .rail button[title^="Ask AI"]').dispatch_event("click")
        _wait_for(lambda: _ai_row(frame, "m1").is_visible(), 10)
        box = _ai_row(frame, "m1").locator("input")
        assert box.get_attribute("placeholder") == "Ask AI to write markdown…"
        box.fill("write a section")
        box.press("Enter")
        _wait_for(lambda: "Review the markdown" in _ai_status(frame, "m1").inner_text(), 20)
        # outer fence stripped, inner one kept; result rendered, not left as source
        assert "AI section" in _cell_text(frame, "m1", ".md-view h2")
        view = _cell_text(frame, "m1", ".md-view")
        assert "x = 1" in view and "```" not in view
        body = seen["body"]
        assert "cell of type markdown" in body["system_prompt"]
        assert "Current markdown cell content" in body["prompt"]
    finally:
        page.unroute("**/api/ai")


def test_ask_ai_fix_error_sends_traceback(page_and_nb):
    page, frame, nb_path = page_and_nb
    seen = {}

    def handler(route):
        seen["body"] = json.loads(route.request.post_data)
        route.fulfill(status=200, content_type="application/x-ndjson",
                      body=_ndjson_ai_response(["print('fixed')"], "print('fixed')"))

    page.route("**/api/ai", handler)
    try:
        # c5 still holds its ZeroDivisionError output from the main test
        frame.locator('.cell[data-id="c5"] .rail button[title^="Ask AI"]').dispatch_event("click")
        _wait_for(lambda: _ai_row(frame, "c5").is_visible(), 10)
        fix = _ai_row(frame, "c5").locator("button", has_text="Fix error")
        assert fix.is_visible()
        fix.dispatch_event("click")
        _wait_for(lambda: "Review the code" in _ai_status(frame, "c5").inner_text(), 20)
        assert _cell_code(frame, "c5").strip() == "print('fixed')"
        body = seen["body"]
        assert "Fix the error in this cell." in body["prompt"]
        assert "ZeroDivisionError" in body["prompt"]
        assert "1/0" in body["prompt"]  # the cell's own code travels too
    finally:
        page.unroute("**/api/ai")


# ------------------------------------------------- manual save + undo/redo

def _delete_via_menu(frame, cell_id):
    frame.locator(f'.cell[data-id="{cell_id}"] .menu-pop button',
                  has_text="Delete cell").dispatch_event("click")


def _cell_ids(frame):
    return [frame.locator(".cell").nth(i).get_attribute("data-id")
            for i in range(frame.locator(".cell").count())]


def test_manual_save_only(page_and_nb):
    page, frame, nb_path = page_and_nb
    before = open(nb_path, encoding="utf-8").read()

    editor = frame.locator('.cell[data-id="c1"] .cm-content')
    editor.press("End")  # element-scoped press focuses the editor itself
    editor.press_sequentially("  # extra")
    _wait_for(lambda: "# extra" in _cell_code(frame, "c1"), 10)
    _wait_for(lambda: frame.locator("#status-text").inner_text() == "Unsaved changes", 10)
    assert frame.locator("#save").is_enabled()

    # nothing writes without an explicit save (bounded wait)
    time.sleep(3)
    assert open(nb_path, encoding="utf-8").read() == before

    # Ctrl+S with focus still inside the editor
    editor.press("Control+s")
    _wait_for(lambda: frame.locator("#status-text").inner_text() == "Saved", 20)
    assert not frame.locator("#save").is_enabled()
    saved = open(nb_path, encoding="utf-8").read()
    assert saved != before and "# extra" in saved


def test_structural_undo_redo_restores_deleted_cell(page_and_nb):
    page, frame, nb_path = page_and_nb
    ids = _cell_ids(frame)
    pos = ids.index("c1")

    _delete_via_menu(frame, "c1")
    assert "c1" not in _cell_ids(frame)
    assert frame.locator("#undo").is_enabled()

    frame.locator("#undo").dispatch_event("click")
    ids = _cell_ids(frame)
    assert ids.index("c1") == pos  # back at the same position
    assert "print('hello run')" in _cell_code(frame, "c1")  # source intact
    assert "hello run" in _cell_text(frame, "c1", ".console")  # outputs intact
    assert frame.locator("#redo").is_enabled()

    frame.locator("#redo").dispatch_event("click")
    assert "c1" not in _cell_ids(frame)
    frame.locator("#undo").dispatch_event("click")  # leave the notebook intact
    assert "c1" in _cell_ids(frame)


def test_undo_shortcuts_inside_and_outside_editors(page_and_nb):
    page, frame, nb_path = page_and_nb

    # pending text change in c2's editor
    editor = frame.locator('.cell[data-id="c2"] .cm-content')
    editor.press("End")
    editor.press_sequentially("  # zzz")
    _wait_for(lambda: "# zzz" in _cell_code(frame, "c2"), 10)

    # the user's exact scenario: delete ANOTHER cell, then Ctrl+Shift+Z with
    # focus still inside c2's editor -> the deleted cell comes back
    _delete_via_menu(frame, "c3")
    assert "c3" not in _cell_ids(frame)
    editor.press("Control+Shift+Z")
    _wait_for(lambda: "c3" in _cell_ids(frame), 10)
    assert "# zzz" in _cell_code(frame, "c2")  # structural undo left text alone

    # plain Ctrl+Z inside the editor stays CM text undo — no structural effect
    _delete_via_menu(frame, "c3")
    assert "c3" not in _cell_ids(frame)
    editor.press("Control+z")
    _wait_for(lambda: "# zzz" not in _cell_code(frame, "c2"), 10)
    assert "c3" not in _cell_ids(frame)  # deleted cell did NOT come back

    # plain Ctrl+Y inside the editor is CM text redo (Mod-y in the bundle)
    editor.press("Control+y")
    _wait_for(lambda: "# zzz" in _cell_code(frame, "c2"), 10)
    assert "c3" not in _cell_ids(frame)

    # plain Ctrl+Z outside any editor falls back to structural undo
    frame.evaluate("() => document.activeElement && document.activeElement.blur()")
    frame.locator("body").press("Control+z")
    _wait_for(lambda: "c3" in _cell_ids(frame), 10)


# ------------------------------------------------ new notebook / save a copy
# These navigate the shared page away from sample.ipynb, so they run last.

def test_blank_ipynb_scaffolds_notebook(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    blank_path = os.path.join(str(server["work"]), "blank.ipynb")
    open(blank_path, "w", encoding="utf-8").close()

    page.goto(f"http://127.0.0.1:{server['port']}" + _view_url(blank_path))
    frame = _find_frame(page)
    _wait_for(lambda: frame.locator(".cell").count() == 1, 30)
    assert frame.locator(".cell .cm-content").count() == 1  # one blank code cell
    _wait_for(lambda: frame.locator("#status-text").inner_text() == "Unsaved changes", 10)
    assert frame.locator("#save").is_enabled()
    assert open(blank_path, encoding="utf-8").read() == ""  # nothing written at load

    frame.locator(".cell .cm-content").press("Control+s")
    _wait_for(lambda: frame.locator("#status-text").inner_text() == "Saved", 20)
    with open(blank_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["nbformat"] == 4 and saved["nbformat_minor"] >= 5
    assert len(saved["cells"]) == 1
    assert saved["cells"][0]["cell_type"] == "code" and saved["cells"][0]["id"]


def test_new_notebook_action(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    frame = _find_frame(page)  # on blank.ipynb (clean) from the previous test
    fresh_path = os.path.join(str(server["work"]), "fresh.ipynb")

    frame.locator("#file-menu").dispatch_event("click")
    frame.locator("#new-nb").dispatch_event("click")
    _wait_for(lambda: frame.locator("#modal-overlay").is_visible(), 10)
    assert frame.locator("#modal-title").inner_text() == "New notebook"
    # the folder picker resolves to the notebook's own folder
    _wait_for(lambda: "nb-work" in frame.locator("#modal-loc").inner_text(), 30)

    # a taken name shows an inline error and keeps the modal open
    frame.locator("#modal-name").fill("blank")
    frame.locator("#modal-ok").dispatch_event("click")
    _wait_for(lambda: "already exists" in frame.locator("#modal-error").inner_text(), 10)
    assert frame.locator("#modal-overlay").is_visible()

    frame.locator("#modal-name").fill("fresh")
    frame.locator("#modal-ok").dispatch_event("click")
    _wait_for(lambda: os.path.exists(fresh_path), 15)
    with open(fresh_path, encoding="utf-8") as f:
        disk = json.load(f)
    assert disk["nbformat"] == 4 and disk["cells"][0]["cell_type"] == "code"
    # the top window navigated (shell SPA router) to the new file's /view URL.
    # Poll via evaluate — Playwright's cached page.url lags a pushState made
    # from the iframe's realm when nothing else drives CDP traffic.
    _wait_for(lambda: "fresh.ipynb" in page.evaluate("() => location.pathname"), 15)

    # the new notebook runs against its own kernel
    frame = _find_frame(page, containing="fresh.ipynb")
    _wait_for(lambda: frame.locator(".cell").count() == 1, 30)
    editor = frame.locator(".cell .cm-content")
    editor.press_sequentially("print(40 + 2)")
    _wait_for(lambda: "print(40 + 2)" in editor.inner_text(), 10)
    frame.locator(".cell .run-btn").dispatch_event("click")
    _wait_for(lambda: "42" in (frame.locator(".cell .console").inner_text()
                               if frame.locator(".cell .console").count() else ""), 90)
    _wait_for(lambda: frame.locator(".cell .count").inner_text() == "[1]", 15)


def test_save_copy_as_action(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    frame = _find_frame(page)  # on fresh.ipynb, dirty (unsaved code + output)
    copy_path = os.path.join(str(server["work"]), "fresh-copy.ipynb")
    url_before = page.evaluate("() => location.pathname")

    frame.locator("#file-menu").dispatch_event("click")
    frame.locator("#copy-nb").dispatch_event("click")
    _wait_for(lambda: frame.locator("#modal-overlay").is_visible(), 10)
    # name is pre-filled from the current file; "open after saving" defaults on
    assert frame.locator("#modal-name").input_value() == "fresh copy"
    assert frame.locator("#modal-check").is_checked()
    frame.locator("#modal-check").set_checked(False, force=True)  # decline the switch
    frame.locator("#modal-name").fill("fresh-copy")
    frame.locator("#modal-ok").dispatch_event("click")
    _wait_for(lambda: os.path.exists(copy_path), 15)
    with open(copy_path, encoding="utf-8") as f:
        disk = json.load(f)
    cell = disk["cells"][0]
    assert "print(40 + 2)" in cell["source"]  # current in-memory doc
    assert any("42" in str(o.get("text", "")) for o in cell["outputs"])
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)
    time.sleep(1)  # any (declined) navigation would have landed by now
    assert page.evaluate("() => location.pathname") == url_before  # stayed put
    # the original keeps its own dirty state — a copy does not clean it
    assert frame.locator("#status-text").inner_text() == "Unsaved changes"


def test_modal_accepts_pasted_paths(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    frame = _find_frame(page)  # still on fresh.ipynb
    work = str(server["work"])
    deep = os.path.join(work, "deep")
    os.makedirs(deep, exist_ok=True)

    def save_copy_to(value):
        frame.locator("#file-menu").dispatch_event("click")
        frame.locator("#copy-nb").dispatch_event("click")
        _wait_for(lambda: frame.locator("#modal-overlay").is_visible(), 10)
        # a forced click on the label-wrapped checkbox is flaky; the submit
        # path reads .checked directly, so set the property
        frame.locator("#modal-check").evaluate("el => { el.checked = false; }")
        frame.locator("#modal-name").fill(value)
        frame.locator("#modal-ok").dispatch_event("click")

    # absolute path with backslashes overrides the browsed folder
    save_copy_to(deep + "\\abs-back")
    _wait_for(lambda: os.path.exists(os.path.join(deep, "abs-back.ipynb")), 15)
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)

    # absolute path with forward slashes
    save_copy_to(deep.replace("\\", "/") + "/abs-fwd")
    _wait_for(lambda: os.path.exists(os.path.join(deep, "abs-fwd.ipynb")), 15)
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)

    # relative subpath resolves against the browsed folder
    save_copy_to("deep/rel-sub")
    _wait_for(lambda: os.path.exists(os.path.join(deep, "rel-sub.ipynb")), 15)
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)

    # a name already ending .ipynb is not doubled
    save_copy_to("suffixed.IPYNB")
    _wait_for(lambda: os.path.exists(os.path.join(work, "suffixed.IPYNB")), 15)
    assert not os.path.exists(os.path.join(work, "suffixed.IPYNB.ipynb"))
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)

    # a missing parent folder is an inline error naming the parsed parent
    save_copy_to("C:\\definitely\\missing\\nb")
    _wait_for(lambda: "Folder does not exist" in frame.locator("#modal-error").inner_text(), 15)
    assert "C:/definitely/missing" in frame.locator("#modal-error").inner_text()
    assert frame.locator("#modal-overlay").is_visible()
    frame.locator("#modal-cancel").dispatch_event("click")
    _wait_for(lambda: frame.locator("#modal-overlay").is_hidden(), 10)


# ------------------------------------------------------------ command mode

def test_command_mode_shortcuts(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    nb_path = os.path.join(str(server["work"]), "cmd.ipynb")
    nb = {"cells": [
        {"cell_type": "code", "id": "k1", "metadata": {}, "execution_count": None,
         "source": "print('one')", "outputs": []},
        {"cell_type": "code", "id": "k2", "metadata": {}, "execution_count": None,
         "source": "x = 5", "outputs": []},
        {"cell_type": "code", "id": "k3", "metadata": {}, "execution_count": None,
         "source": "print('go')", "outputs": []},
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    with open(nb_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)
    page.goto(f"http://127.0.0.1:{server['port']}" + _view_url(nb_path))
    frame = _find_frame(page, containing="cmd.ipynb")
    _wait_for(lambda: frame.locator(".cell").count() == 3, 30)
    body = frame.locator("body")

    def cmd_key(key, **mods):
        # real key delivery to the iframe body is focus-flaky under the shell;
        # the command-mode handler doesn't require trusted events
        body.dispatch_event("keydown", {"key": key, **mods})

    def selected_id():
        loc = frame.locator(".cell.selected")
        return loc.get_attribute("data-id") if loc.count() else None

    # Esc in an editor enters command mode on that cell
    frame.locator('.cell[data-id="k2"] .cm-content').press("Escape")
    _wait_for(lambda: selected_id() == "k2", 10)

    # A/B insert code cells above/below the selection, selecting the new cell
    cmd_key("a")
    _wait_for(lambda: frame.locator(".cell").count() == 4, 10)
    ids = _cell_ids(frame)
    assert ids[1] == selected_id() and ids[1] not in ("k1", "k2", "k3")
    cmd_key("b")
    _wait_for(lambda: frame.locator(".cell").count() == 5, 10)
    assert _cell_ids(frame)[2] == selected_id()

    # D,D deletes; Z restores; Shift+Z redoes the delete
    cmd_key("d")
    cmd_key("d")
    _wait_for(lambda: frame.locator(".cell").count() == 4, 10)
    cmd_key("z")
    _wait_for(lambda: frame.locator(".cell").count() == 5, 10)
    cmd_key("Z", shiftKey=True)
    _wait_for(lambda: frame.locator(".cell").count() == 4, 10)

    # M/Y round-trips a cell's type, keeping its source
    frame.locator('.cell[data-id="k2"] .gutter').dispatch_event("click")
    _wait_for(lambda: selected_id() == "k2", 10)
    cmd_key("m")
    _wait_for(lambda: frame.locator('.cell[data-id="k2"] .md-view').count() == 1, 10)
    cmd_key("y")  # selection follows the rebuilt cell
    _wait_for(lambda: frame.locator('.cell[data-id="k2"] .cm-content').count() == 1, 10)
    assert "x = 5" in _cell_code(frame, "k2")

    # X then V moves a cell (fresh id, same source), recorded for undo
    frame.locator('.cell[data-id="k1"] .gutter').dispatch_event("click")
    _wait_for(lambda: selected_id() == "k1", 10)
    cmd_key("x")
    _wait_for(lambda: "k1" not in _cell_ids(frame), 10)
    frame.locator('.cell[data-id="k3"] .gutter').dispatch_event("click")
    _wait_for(lambda: selected_id() == "k3", 10)
    cmd_key("v")
    _wait_for(lambda: frame.locator(".cell").count() == 4, 10)
    ids = _cell_ids(frame)
    pasted = ids[ids.index("k3") + 1]
    assert pasted != "k1"
    assert "print('one')" in _cell_code(frame, pasted)

    # typing inside an editor never leaks into command mode
    n = frame.locator(".cell").count()
    frame.locator('.cell[data-id="k2"] .cm-content').press_sequentially("a")
    _wait_for(lambda: "a" in _cell_code(frame, "k2"), 10)
    assert frame.locator(".cell").count() == n

    # Shift+Enter in command mode runs the selected cell
    frame.locator('.cell[data-id="k3"] .gutter').dispatch_event("click")
    _wait_for(lambda: selected_id() == "k3", 10)
    cmd_key("Enter", shiftKey=True)
    _wait_for(lambda: "go" in _cell_text(frame, "k3", ".console"), 90)
    _wait_for(lambda: _cell_text(frame, "k3", ".count") == "[1]", 15)


# --------------------------------------------------- daemon death recovery

def test_daemon_recovery_and_isolation(page_and_nb, server):
    page, _frame, _nb = page_and_nb
    frame = _find_frame(page, containing="cmd.ipynb")  # kernel is live here
    state_path = _daemon_state_path(server["home"])
    assert os.path.exists(state_path)  # daemon state lives in the isolated home
    with open(state_path, encoding="utf-8") as f:
        st = json.load(f)
    if os.path.exists(DEFAULT_DAEMON_STATE):  # a developer daemon is untouched
        with open(DEFAULT_DAEMON_STATE, encoding="utf-8") as f:
            assert json.load(f).get("port") != st["port"]

    # kill the daemon out from under the open page
    urllib.request.urlopen(
        f"http://127.0.0.1:{st['port']}/quit?t={st['token']}", timeout=5).read()
    time.sleep(1)

    # a never-run cell (the one pasted after k3) executes after one transparent
    # recovery, with the honest fresh-kernel notice — never "Failed to fetch"
    ids = _cell_ids(frame)
    target = ids[ids.index("k3") + 1]
    _run_cell(frame, target)
    _wait_for(lambda: "reconnected with a fresh kernel"
              in frame.locator("#toast").inner_text(), 60)
    _wait_for(lambda: "one" in _cell_text(frame, target, ".console"), 120)
    assert "Failed to fetch" not in frame.locator("body").inner_text()


def test_saved_html_output_is_sandboxed(page_and_nb, server):
    # a .ipynb from anywhere can carry a pre-baked text/html output — opening
    # it must never execute script in the template's (same-)origin: html
    # outputs render inside a scriptless sandboxed iframe
    page, _frame, _nb = page_and_nb
    hostile = {
        "cells": [{
            "cell_type": "code", "id": "h1", "metadata": {},
            "execution_count": 1, "source": "pass",
            "outputs": [{
                "output_type": "display_data", "metadata": {},
                "data": {"text/html": [
                    "<script>window.__pwned = 1</script>",
                    '<img src="x" onerror="window.__pwned = 2">',
                    "<b>bold ok</b>",
                ]},
            }],
        }],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    path = os.path.join(str(server["work"]), "hostile.ipynb")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hostile, f)

    page.goto(f"http://127.0.0.1:{server['port']}" + _view_url(path))
    frame = _find_frame(page, containing="hostile.ipynb")
    _wait_for(lambda: frame.locator(".cell").count() == 1, 30)
    out = frame.locator('.cell[data-id="h1"] .outputs iframe.html-out')
    _wait_for(lambda: out.count() == 1, 15)
    assert out.get_attribute("sandbox") == ""
    assert "bold ok" in (out.get_attribute("srcdoc") or "")
    time.sleep(0.5)  # the script / img onerror would have fired by now
    assert frame.evaluate("() => window.__pwned") is None


def test_reaped_kernel_recovers_transparently(page_and_nb, server):
    # the daemon reaps kernels idle > 30 min while staying alive itself; a
    # cached kernel_id must recover with a fresh kernel, not surface
    # "unknown kernel_id" (still on hostile.ipynb from the previous test)
    page, _frame, _nb = page_and_nb
    frame = _find_frame(page, containing="hostile.ipynb")
    # h1's saved execution_count is already 1 — wait on .time, which only a
    # real execution sets, so the shutdown below can't race kernel startup
    _run_cell(frame, "h1")
    _wait_for(lambda: re.match(r"^\d", _cell_text(frame, "h1", ".time") or ""), 90)

    with open(_daemon_state_path(server["home"]), encoding="utf-8") as f:
        st = json.load(f)
    base = f"http://127.0.0.1:{st['port']}"
    nb_path = os.path.join(str(server["work"]), "hostile.ipynb")

    def post(route, payload):
        req = urllib.request.Request(
            f"{base}{route}?t={st['token']}",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        return json.load(urllib.request.urlopen(req, timeout=10))

    kid = post("/kernel/ensure", {"nb_path": nb_path, "python": ""})["kernel_id"]
    post("/kernel/shutdown", {"kernel_id": kid})  # what the idle reaper does
    post("/kernel/shutdown", {"kernel_id": kid})  # idempotent for a gone kernel

    # new source, so fresh console output proves the re-run truly executed
    editor = frame.locator('.cell[data-id="h1"] .cm-content')
    editor.click()
    editor.press("Control+a")
    editor.press_sequentially("print('back')")
    _run_cell(frame, "h1")
    _wait_for(lambda: "shut down while idle" in frame.locator("#toast").inner_text(), 60)
    _wait_for(lambda: "back" in _cell_text(frame, "h1", ".console"), 90)
    assert "unknown kernel_id" not in frame.locator("body").inner_text()
