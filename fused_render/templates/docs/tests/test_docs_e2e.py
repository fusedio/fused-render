import importlib.util
import os
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
SHELL = os.path.join(ROOT, "fused_render", "static", "shell-dist", "index.html")
_spec = importlib.util.spec_from_file_location(
    "_docs_e2e_pandoc_bin", os.path.join(ROOT, "fused_render", "templates", "shared", "pandoc_bin.py"))
pandoc_bin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pandoc_bin)
PANDOC = pandoc_bin.path()
MOD = "Meta" if sys.platform == "darwin" else "Control"

pytestmark = [
    pytest.mark.skipif(not os.path.exists(SHELL), reason="React shell not built"),
    pytest.mark.skipif(not PANDOC, reason="pandoc not installed"),
]

SOURCE = """---
title: Quarterly Plan
subtitle: Draft for review
---

Text with ==highlight==, x^2^, H~2~O and a note.[^1]

- [x] Ship the editor
- [ ] Write the docs

| Name | Value |
|------|-------|
| a    | 1     |

[Check this]{.comment-start id="0" author="Alice" date="2026-10-01T10:00:00Z"}Commented sentence.[]{.comment-end id="0"}

[^1]: The footnote text.
"""

CARET_AFTER = r"""(word) => {
  const w = document.createTreeWalker(document.querySelector('.ProseMirror'), NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const i = n.textContent.indexOf(word);
    if (i >= 0) {
      const r = document.createRange();
      r.setStart(n, i + word.length);
      r.collapse(true);
      getSelection().removeAllRanges();
      getSelection().addRange(r);
      return true;
    }
  }
  return false;
}"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(fn, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"condition not met within {timeout}s")


def _docx_html(path):
    return subprocess.run([PANDOC, "-f", "docx", "-t", "html", "-s", "--track-changes=all", "--wrap=none", path],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("docs-home")
    work = tmp_path_factory.mktemp("docs-work")
    port = _free_port()
    env = dict(os.environ, PYTHONPATH=ROOT, FUSED_RENDER_HOME=str(home),
               FUSED_RENDER_APP_HOME=str(tmp_path_factory.mktemp("docs-app-home")),
               FUSED_RENDER_CORE_TEMPLATES=os.path.join(ROOT, "fused_render", "templates"))
    log_path = home / "server.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "fused_render.cli", "serve", "--port", str(port),
                                 "--no-browser", "--start-dir", str(work)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        _until(lambda: _ping(port) or (proc.poll() is not None and _fail(log_path)), 60)
        yield {"port": port, "work": work}
    finally:
        proc.kill()
        proc.wait(timeout=15)


def _ping(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2).read()
        return True
    except OSError:
        return False


def _fail(log_path):
    raise RuntimeError("server exited:\n" + log_path.read_text("utf-8", errors="replace")[-2000:])


@pytest.fixture
def doc(server):
    def make(name):
        path = os.path.join(str(server["work"]), name)
        subprocess.run([PANDOC, "-f", "markdown+mark", "-t", "docx", "-o", path], input=SOURCE, text=True, encoding="utf-8", check=True)
        return path
    return make


@pytest.fixture(scope="module")
def page(server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser.new_page(viewport={"width": 1400, "height": 900})
        browser.close()


def _open(page, server, path):
    url_path = "/".join(quote(seg, safe="") for seg in path.split("/") if seg)
    page.goto(f"http://127.0.0.1:{server['port']}/view/{url_path}?template=docs&mode=edit")

    def ready():
        page.keyboard.press("Escape")
        frame = next((f for f in page.frames if "/render" in f.url), None)
        return frame if frame and frame.evaluate("!!document.querySelector('.ProseMirror')") else None
    frame = _until(ready, 60)
    frame.locator(".ProseMirror p").first.click()
    return frame


def _type_after(page, frame, word, text):
    assert frame.evaluate(CARET_AFTER, word)
    time.sleep(0.15)
    page.keyboard.type(text)


def test_round_trip_keeps_word_features(server, doc, page):
    path = doc("roundtrip.docx")
    frame = _open(page, server, path)
    rendered = frame.evaluate("""({
      title: document.querySelector('.ProseMirror p.title')?.textContent,
      subtitle: document.querySelector('.ProseMirror p.subtitle')?.textContent,
      marks: ['mark', 'sup:not(.fn)', 'sub', 'sup.fn', '.comment', 'th'].map((s) => document.querySelectorAll('.ProseMirror ' + s).length),
      tasks: [...document.querySelectorAll('.ProseMirror li.task')].map((li) => li.dataset.checked),
    })""")
    assert rendered == {"title": "Quarterly Plan", "subtitle": "Draft for review", "marks": [1, 1, 1, 1, 1, 2], "tasks": ["true", "false"]}

    _type_after(page, frame, "and a note.", " Edited.")
    box = frame.locator("li.task[data-checked=false]").bounding_box()
    page.mouse.click(box["x"] + 8, box["y"] + 12)
    _until(lambda: frame.locator("#saveState").inner_text() == "Saved")

    html = _docx_html(path)
    for expected in ('<h1 class="title">Quarterly Plan</h1>', 'class="subtitle">Draft for review', "<mark>highlight</mark>",
                     "x<sup>2</sup>", "H<sub>2</sub>O", "Edited.", "The footnote text.", 'data-author="Alice"', "<thead>"):
        assert expected in html, expected
    assert html.count('type="checkbox" checked') == 2


def test_saves_queue_and_recover_from_conflict(server, doc, page):
    path = doc("reliability.docx")
    frame = _open(page, server, path)
    _type_after(page, frame, "and a note.", " Alpha")
    page.keyboard.press(f"{MOD}+s")
    page.keyboard.type(" Beta", delay=30)
    _until(lambda: "Alpha Beta" in _docx_html(path))

    subprocess.run([PANDOC, "-f", "markdown", "-t", "docx", "-o", path], input="Changed elsewhere\n", text=True, encoding="utf-8", check=True)
    page.keyboard.type(" Gamma")
    _until(lambda: frame.locator("#banner").is_visible())
    assert "Gamma" not in _docx_html(path)
    frame.get_by_role("button", name="Keep my version").click()
    _until(lambda: "Alpha Beta Gamma" in _docx_html(path))
    page.keyboard.type(" Delta")
    _until(lambda: "Delta" in _docx_html(path))
    assert frame.locator("#banner").is_hidden()
