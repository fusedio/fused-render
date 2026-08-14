"""End-to-end tests for the joblib model viewer template, driven through
Playwright against a real fused-render server (FUSED_RENDER_CORE_TEMPLATES
points at this checkout's templates so edits are served live).

Unlike every other template's e2e test, this one also needs the optional
`fused` local-compute-backend package importable in the SAME interpreter that
runs the server: reader.py's dependencies (joblib/scikit-learn/xgboost/
lightgbm) only ever get installed through that engine's per-folder project-env
mechanism (SPEC PY-16) — without it, the built-in executor runs reader.py on
the app's own interpreter with no per-template venv at all, and every open
reports "No module named 'joblib'". This mirrors model_card's own note about
needing the engine switched for its tokenizer section, except here it's not
optional: verified manually against the running app (fused package installed
via `uv pip install fused==2.9.3b3`) before this test was written.

First run downloads scikit-learn/xgboost/lightgbm into a fresh per-template
venv (SPEC PY-16/18), which can take a minute or more and needs network
access — this is why it is skipped by default rather than wired into CI (no
existing template test needs extra deps at pytest time; this is the first).
Run explicitly:
    PYTHONPATH=<checkout> python -m pytest \
        fused_render/templates/joblib_model/tests/test_viewer_e2e.py -o addopts=""
"""
import os
import pickle
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# repo root: joblib_model/tests -> joblib_model -> templates -> fused_render -> root
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SHELL = os.path.join(ROOT, "fused_render", "static", "shell-dist", "index.html")

pytestmark = [
    pytest.mark.skipif(not os.path.exists(SHELL), reason="React shell not built"),
    pytest.mark.skipif(
        os.environ.get("FUSED_RENDER_E2E_JOBLIB") != "1",
        reason="downloads a real ML env on first run; set FUSED_RENDER_E2E_JOBLIB=1 to opt in",
    ),
]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_fixtures(work_dir):
    import joblib
    import numpy as np
    from sklearn.preprocessing import StandardScaler
    from xgboost import XGBClassifier

    rng = np.random.RandomState(0)
    x = rng.rand(200, 6)
    y = rng.randint(0, 3, size=200)
    scaler = StandardScaler().fit(x)
    clf = XGBClassifier(n_estimators=5, max_depth=2, eval_metric="mlogloss")
    clf.fit(scaler.transform(x), y)
    bundle = {"scaler": scaler, "classifier": clf, "dataset": "synthetic_e2e_v1"}
    joblib.dump(bundle, os.path.join(work_dir, "bundle.joblib"))

    class _Reducer:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    with open(os.path.join(work_dir, "evil.pkl"), "wb") as handle:
        pickle.dump(_Reducer(), handle)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("joblib-home")
    work = tmp_path_factory.mktemp("joblib-work")
    _make_fixtures(str(work))
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["FUSED_RENDER_HOME"] = str(home)
    env["FUSED_RENDER_CORE_TEMPLATES"] = os.path.join(ROOT, "fused_render", "templates")
    log_path = home / "server.log"
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
                raise RuntimeError("server exited:\n" +
                                   log_path.read_text("utf-8", errors="replace")[-2000:])
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("server did not come up")
    yield {"port": port, "work": str(work)}
    proc.kill()
    proc.wait(timeout=15)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _embed_url(port, path):
    return f"http://127.0.0.1:{port}/explorer/embed/" + quote(path.replace("\\", "/"), safe="/:")


def _wait(fn, timeout=90, step=0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def _open(browser, port, path):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(_embed_url(port, path), wait_until="domcontentloaded")

    def find_frame():
        page.keyboard.press("Escape")
        return next((f for f in page.frames if "/render" in f.url), None)

    frame = _wait(find_frame, timeout=30, step=0.5)
    return page, frame


def test_safe_bundle_renders_all_sections(server, browser):
    page, frame = _open(browser, server["port"], os.path.join(server["work"], "bundle.joblib"))
    # timeout=240: a cold run pays for a real `uv sync` of scikit-learn/xgboost/
    # lightgbm/pandas into this template's own venv (SPEC PY-16/18) before the
    # first render can happen at all — confirmed via the server log the one time
    # this legitimately took over 90s. A warm venv (every run after the first on
    # this machine) returns in a few seconds, same as the reader.py unit tests.
    state = _wait(lambda: frame.evaluate(
        """() => {
            const pill = document.querySelector('h1 .pill');
            if (!pill || pill.textContent !== 'Loaded') return null;
            // The tree diagram is a SEPARATE lazily-fetched runPython call made
            // after the initial render (a 15-tree ensemble isn't eagerly
            // included), so it can still be loading after the pill flips.
            const svgRects = document.querySelectorAll('.treebox svg rect').length;
            if (svgRects === 0) return null;
            return {
              h2s: [...document.querySelectorAll('h2')].map(h => h.textContent),
              treeOptions: (document.querySelector('select') || {}).options?.length,
              svgRects,
            };
        }""") or None, timeout=240)
    assert "Structure" in state["h2s"]
    assert any(h.startswith("Feature importance") for h in state["h2s"])
    assert any(h.startswith("Trees") for h in state["h2s"])
    assert state["svgRects"] > 0, "tree diagram should have drawn at least one node"
    page.close()


def test_dangerous_pickle_shows_blocked_banner_not_a_crash(server, browser):
    page, frame = _open(browser, server["port"], os.path.join(server["work"], "evil.pkl"))
    state = _wait(lambda: frame.evaluate(
        """() => {
            const pill = document.querySelector('h1 .pill');
            const note = document.querySelector('.note.danger');
            if (!pill || pill.textContent !== 'Blocked' || !note) return null;
            return { noteText: note.textContent, hasErrOverlay: !!document.querySelector('.err') };
        }""") or None, timeout=30)
    assert "nt.system" in state["noteText"] or "os.system" in state["noteText"] or "system" in state["noteText"]
    page.close()
