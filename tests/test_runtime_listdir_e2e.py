"""End-to-end round trip for `fused.listDir`: a real server, a plain page
served through /render (which injects static/runtime.js), driven by
Playwright. Skipped when playwright is missing — the same skip the notebook
e2e uses; the string contract in test_runtime_listdir.py still runs everywhere.

Run serially: PYTHONPATH=<checkout> python -m pytest tests/test_runtime_listdir_e2e.py -o addopts=""
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(ROOT, "fused_render", "static", "shell-dist", "index.html")

# create_app refuses to start without the built shell, so this is a skip,
# not a failure, on a checkout that never ran the frontend build.
pytestmark = pytest.mark.skipif(
    not os.path.exists(SHELL), reason="React shell not built")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("ld-home")
    work = tmp_path_factory.mktemp("ld-work")
    port = _free_port()
    env = dict(os.environ)
    # Prepend, don't replace: the interpreter running pytest may find its
    # deps through PYTHONPATH too.
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
    env["FUSED_RENDER_HOME"] = str(home)
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
    yield {"port": port, "work": work}
    proc.kill()
    proc.wait(timeout=15)


def test_listdir_round_trip(server):
    work = server["work"]
    (work / "sub").mkdir()
    (work / "a.txt").write_text("x", encoding="utf-8")
    html = work / "page.html"
    html.write_text("<!doctype html><html><head><title>t</title></head>"
                    "<body>listDir</body></html>", encoding="utf-8")
    url = f"http://127.0.0.1:{server['port']}/render?path={quote(str(html), safe='')}&_preview=1"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.wait_for_function("window.fused && typeof window.fused.listDir === 'function'")

            # A directory: the /api/fs/list shape, echoed path, sorted entries.
            res = page.evaluate("p => fused.listDir(p)", str(work))
            assert res["path"] == str(work)
            assert res["truncated"] is False
            names = {e["name"]: e for e in res["entries"]}
            assert names["sub"]["is_dir"] is True and names["sub"]["size"] is None
            assert names["a.txt"]["is_dir"] is False and names["a.txt"]["size"] == 1
            assert isinstance(names["a.txt"]["mtime"], (int, float))

            # A file, and a missing path: the helper rejects with the server's
            # own message rather than resolving to an empty listing.
            for bad in (work / "a.txt", work / "nope"):
                msg = page.evaluate(
                    "p => fused.listDir(p).then(() => null, e => e.message)", str(bad))
                assert msg and "not a directory" in msg, (bad, msg)
        finally:
            browser.close()
