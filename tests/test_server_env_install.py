"""The install-loader HTTP surface: /api/env/install, /progress, /cancel
(SPEC PY-18, D173).

The endpoints the page shell's loader drives after /api/run answers
`needs_install`. What is asserted here rather than in test_env_install.py: the
trust boundary (X-Fused, like every other execute-adjacent endpoint), and the
one design rule that keeps the loader honest — **requirements are re-derived
from the .py on disk, never read out of the request body.** A client-supplied
list could name a different requirement set than the run will, and the loader
would then fill a venv the run never looks at: a permanent double download with
no error anywhere.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import engine
from fused_render.server import create_app

HEADERS = {"X-Fused": "1"}

# Two tests below reach `envinstall.venv_key_for`, which composes `fused`'s own
# `requirements_venv_id`/`venv_key` — deliberately, so the loader's key is the
# backend's key and not a re-derivation. That makes `fused` a real requirement
# for them, and the `test-python` matrix job does not install the extra.
#
# The skip is made LOUD rather than left silent: these two are the only coverage
# of "requirements come from the .py, never the request body", and the
# `fused-engine` job's zero-skip gate (.github/workflows/test.yml) lists this file
# so a skip THERE fails the build. A silent CI skip is how this whole class of bug
# reached a shipped DMG.
requires_fused = pytest.mark.skipif(
    not engine.available(),
    reason="needs the `fused` extra for the backend's own venv-key helpers "
           "(the fused-engine job asserts this does not skip)",
)


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _py(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_every_endpoint_requires_the_x_fused_header(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/env/install", json={"py": "/x.py"}).status_code == 403
    assert client.get("/api/env/progress?key=abc").status_code == 403
    assert client.post("/api/env/cancel", json={"key": "abc"}).status_code == 403


def test_install_needs_a_py_path(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/env/install", json={}, headers=HEADERS)
    assert resp.status_code == 400
    assert "'py'" in resp.json()["error"]


def test_install_reports_a_missing_file(tmp_path):
    client = _client(tmp_path)
    resp = client.post(
        "/api/env/install", json={"py": str(tmp_path / "nope.py")}, headers=HEADERS
    )
    assert resp.status_code == 400
    assert "no such Python file" in resp.json()["error"]


def test_install_refuses_a_script_with_nothing_to_install(tmp_path):
    """A header-less script runs on the app's interpreter — there is no venv.

    Saying so beats spawning a worker that would build an empty environment: the
    caller has misunderstood which path the script is on, and the message says
    which one it is actually on.
    """
    client = _client(tmp_path)
    target = _py(tmp_path, "plain.py", "def main():\n    return 1\n")
    resp = client.post("/api/env/install", json={"py": str(target)}, headers=HEADERS)
    assert resp.status_code == 400
    assert "nothing to install" in resp.json()["error"]


def test_install_surfaces_a_malformed_header_instead_of_500ing(tmp_path):
    client = _client(tmp_path)
    target = _py(tmp_path, "bad.py", "# /// script\n# dependencies = [oops\n# ///\n")
    resp = client.post("/api/env/install", json={"py": str(target)}, headers=HEADERS)
    assert resp.status_code == 400
    assert "PEP 723" in resp.json()["error"]


@requires_fused
def test_install_derives_requirements_from_the_file_not_the_body(tmp_path, monkeypatch):
    """The rule that keeps the loader's venv and the run's venv the same one."""
    client = _client(tmp_path)
    target = _py(
        tmp_path, "declared.py",
        '# /// script\n# dependencies = ["pyproj", "imagecodecs"]\n# ///\n'
        "def main():\n    return 1\n",
    )
    started = []
    monkeypatch.setattr(
        "fused_render.envinstall.start",
        lambda reqs: started.append(list(reqs)) or {"stage": "spawn", "done": False},
    )
    resp = client.post(
        "/api/env/install",
        # A body naming something else entirely — it must be ignored.
        json={"py": str(target), "requirements": ["totally-different"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["requirements"] == ["imagecodecs", "pyproj"]
    assert started == [["imagecodecs", "pyproj"]]


@requires_fused
def test_install_resolves_a_relative_py_against_the_page(tmp_path, monkeypatch):
    """Same `py`/`html` contract /api/run uses, so the loader addresses the
    identical file the failed run did."""
    client = _client(tmp_path)
    (tmp_path / "sub").mkdir()
    _py(tmp_path / "sub", "rel.py",
        '# /// script\n# dependencies = ["pyproj"]\n# ///\ndef main():\n    return 1\n')
    monkeypatch.setattr("fused_render.envinstall.start", lambda reqs: {"done": False})
    resp = client.post(
        "/api/env/install",
        json={"py": "rel.py", "html": str(tmp_path / "sub" / "page.html")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["requirements"] == ["pyproj"]


def test_progress_for_an_unknown_key_is_null_not_an_error(tmp_path):
    """The poller must be able to ask about an install that never started."""
    client = _client(tmp_path)
    resp = client.get("/api/env/progress?key=0123456789abcdef", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["progress"] is None


# --- `key` reaches the filesystem, and cancel() signals a pid out of it -------

TRAVERSAL = "../../../../../../tmp/anything"


@pytest.mark.parametrize("bad", [
    TRAVERSAL,
    "..",
    "/etc/passwd",
    "0123456789ABCDEF",          # uppercase is not a key we ever produce
    "0123456789abcde",           # 15 chars
    "0123456789abcdef0",         # 17 chars
    "0123456789abcdeg",          # 'g' is not hex
    "abc/../../def",
    "0123456789abcdef\n",        # `$` matches before a trailing newline; a key does not
    "",
])
def test_progress_rejects_a_key_that_is_not_a_key(tmp_path, bad):
    """`/api/env/progress` would otherwise read any progress.json on the disk."""
    client = _client(tmp_path)
    resp = client.get(
        "/api/env/progress", params={"key": bad}, headers=HEADERS
    )
    assert resp.status_code == 400, resp.text
    assert "valid install key" in resp.json()["error"]


@pytest.mark.parametrize("bad", [TRAVERSAL, "..", "/etc/passwd", ""])
def test_cancel_rejects_a_key_that_is_not_a_key(tmp_path, bad):
    """The dangerous one: cancel reads `pid` from the file and signals it.

    `_kill` escalates to `os.killpg` for a process-group leader, so a traversal
    here is not an information leak but an arbitrary-process-group kill.
    """
    client = _client(tmp_path)
    resp = client.post("/api/env/cancel", json={"key": bad}, headers=HEADERS)
    assert resp.status_code == 400, resp.text


def test_a_traversal_key_never_reaches_the_filesystem(tmp_path, monkeypatch):
    """Asserted at the envinstall layer too, so no future caller can skip it."""
    from fused_render import envinstall

    assert envinstall.valid_key("431848ef8d0cdcdd") is True
    assert envinstall.valid_key(TRAVERSAL) is False
    # Truly anchored: `$` alone would accept a trailing newline, so
    # "<key>\n" would reach os.path.join as a DIFFERENT progress directory
    # than "<key>" — the documented invariant, not a traversal.
    assert envinstall.valid_key("431848ef8d0cdcdd\n") is False
    with pytest.raises(ValueError, match="not a valid install key"):
        envinstall.progress_dir(TRAVERSAL)
    # And the two public readers stay quiet rather than raising into a handler.
    assert envinstall.progress(TRAVERSAL) is None
    assert envinstall.cancel(TRAVERSAL) is False


def test_cancel_of_a_bad_key_signals_nothing(tmp_path, monkeypatch):
    """The end the attack was aiming at: no process is ever signalled."""
    from fused_render import envinstall

    monkeypatch.setattr(
        envinstall, "_kill", lambda pid: pytest.fail(f"signalled pid {pid}")
    )
    assert envinstall.cancel(TRAVERSAL) is False
    client = _client(tmp_path)
    assert client.post(
        "/api/env/cancel", json={"key": TRAVERSAL}, headers=HEADERS
    ).status_code == 400


def test_progress_returns_the_record(tmp_path):
    import json

    from fused_render import envinstall

    client = _client(tmp_path)
    key = "abc123abc123abc1"
    d = envinstall.progress_dir(key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.json"), "w", encoding="utf-8") as f:
        json.dump({"stage": "install", "pct": 25, "detail": "downloading",
                   "done": False, "error": None, "pid": os.getpid(), "ts": 1.0}, f)
    resp = client.get(f"/api/env/progress?key={key}", headers=HEADERS)
    assert resp.json()["progress"]["stage"] == "install"
    assert resp.json()["progress"]["detail"] == "downloading"


def test_progress_during_the_spawn_window_is_not_null(tmp_path, monkeypatch):
    """The endpoint the loader polls must not answer null for a claimed install.

    A claim exists from before `_spawn` until the parent's first `_write` lands
    after `Popen` returns. Any poll inside that window — the caller that lost the
    claim, or a reloaded page that never POSTed — used to get
    `"progress": null`, and runtime.js turns that into the hard failure "the
    installer left no progress record" over an install that is running fine.
    """
    from fused_render import envinstall

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    client = _client(tmp_path)
    key = "0a5eeded00000001"
    assert envinstall._claim(key) is True  # the winner, still inside Popen
    body = client.get(f"/api/env/progress?key={key}", headers=HEADERS).json()
    assert body["progress"] is not None, "a claimed install is not 'never started'"
    assert body["progress"]["done"] is False


def test_cancel_needs_a_key(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/env/cancel", json={}, headers=HEADERS)
    assert resp.status_code == 400
    assert "'key'" in resp.json()["error"]


def test_cancel_of_an_unknown_key_is_a_clean_no_op(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/env/cancel", json={"key": "0123456789abcdef"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


@pytest.mark.parametrize("boom", [
    # `venv_key_for` imports `fused.agent_core...` unguarded — no fused, no import.
    ImportError("No module named 'fused'"),
    ModuleNotFoundError("No module named 'fused.agent_core'"),
    # `_backend_attr` raises this BY DESIGN, with the diagnostic that matters.
    RuntimeError("this fused build's Backend has no '_venvs_path', so the "
                 "install loader cannot tell where its script venvs live"),
])
def test_an_engine_that_cannot_answer_is_reported_not_500ed(tmp_path, monkeypatch, boom):
    """Reachable without the fused engine: a page loaded before the engine
    preference was switched, or any direct API call.

    `_backend_attr`'s message was written to be READ by the user — a 500 renders
    in the loader as a bare "HTTP 500" and throws that diagnostic away.
    """
    from fused_render import envinstall

    def _raise(*a, **kw):
        raise boom

    client = _client(tmp_path)
    target = _py(tmp_path, "declared.py",
                 '# /// script\n# dependencies = ["pyproj"]\n# ///\n'
                 "def main():\n    return 1\n")
    monkeypatch.setattr(envinstall, "start", _raise)
    monkeypatch.setattr(envinstall, "venv_key_for", _raise)
    monkeypatch.setattr(envinstall, "progress", _raise)
    monkeypatch.setattr(envinstall, "cancel", _raise)

    resp = client.post("/api/env/install", json={"py": str(target)}, headers=HEADERS)
    assert resp.status_code == 400, resp.text
    assert str(boom) in resp.json()["error"]

    resp = client.get("/api/env/progress?key=0123456789abcdef", headers=HEADERS)
    assert resp.status_code == 400, resp.text
    assert str(boom) in resp.json()["error"]

    resp = client.post("/api/env/cancel", json={"key": "0123456789abcdef"},
                       headers=HEADERS)
    assert resp.status_code == 400, resp.text
    assert str(boom) in resp.json()["error"]


# --- the loader's own JS, executed ---------------------------------------------
#
# The structural assertions below cannot see behaviour, and both bugs these two
# tests cover were behavioural: which key gets polled, and when the overlay is
# torn down. So the loader's real source is lifted out of runtime.js and run under
# node against a stub document/fetch — the same approach
# test_claude_permission_bridge.py uses for the card's own button builder.

_KEY_A = "a" * 16   # what /api/run's needs_install carried
_KEY_B = "b" * 16   # what /api/env/install re-derived off the .py on disk

_JS_PRELUDE = """
function makeEl() {
  return {
    style: { cssText: "" }, textContent: "", children: [], _h: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    remove() { this.removed = true; },
    addEventListener(t, f) { (this._h[t] = this._h[t] || []).push(f); },
    removeEventListener(t, f) {
      const a = this._h[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1);
    },
  };
}
globalThis.document = { createElement: () => makeEl(), body: makeEl() };
"""


def _loader_js():
    """The install-loader block of runtime.js, verbatim."""
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    start = src.index("  const INSTALL_POLL_MS")
    return src[start:src.index("  function runPython(", start)]


def _run_loader(scenario):
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to run the loader's own JS")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as f:
        f.write(_JS_PRELUDE + _loader_js() + scenario)
        harness = f.name
    try:
        out = subprocess.run([node, harness], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(harness)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_loader_polls_the_key_the_installer_actually_returned():
    """/api/env/install re-derives the requirements off the .py on disk and
    returns its OWN key. Editing a .py and letting live-reload re-run it is this
    app's core workflow, so the file really can change between /api/run's
    pre-flight and the POST — and then the install runs under key B while the
    page polls key A, `progress` is null, and the loader fails an install that is
    running perfectly with "the installer left no progress record".
    """
    result = _run_loader("""
const polled = [];
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install")
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(b)s", progress: { stage: "spawn", pct: 0, done: false } })});
  if (url.startsWith("/api/env/progress")) {
    const key = decodeURIComponent(url.split("key=")[1]);
    polled.push(key);
    return Promise.resolve({ json: () => Promise.resolve({ ok: true, key,
      progress: key === "%(b)s"
        ? { stage: "done", pct: 100, done: true, error: null } : null })});
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
installEnv({ key: "%(a)s", requirements: ["x"] }, "a.py", "a.html").then(
  () => console.log(JSON.stringify({ ok: true, polled })),
  (e) => console.log(JSON.stringify({ ok: false, error: e.message, polled })));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["ok"] is True, result
    assert _KEY_B in result["polled"]
    assert _KEY_A not in result["polled"], (
        "the loader polled the pre-flight key instead of the installer's own"
    )


def test_cancelling_cancels_the_install_that_is_actually_running():
    """Same key mix-up, in the direction that leaves a download running: the
    cancel POST has to name the installer's key, not the pre-flight one. The
    handler is registered before the POST resolves, so it must read the resolved
    key rather than the one it captured."""
    result = _run_loader("""
const cancelled = [];
let polls = 0;
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install")
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(b)s", progress: { stage: "spawn", pct: 0, done: false } })});
  if (url.startsWith("/api/env/progress")) {
    polls += 1;
    if (polls === 1) {
      // The user clicks Cancel while the install is genuinely in flight.
      installUi.cancel._h.click[0]();
      return Promise.resolve({ json: () => Promise.resolve({ ok: true,
        progress: { stage: "install", pct: 25, done: false } })});
    }
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: { stage: "done", pct: 100, done: true,
                  error: "the install was cancelled" } })});
  }
  cancelled.push(JSON.parse(opts.body).key);
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
installEnv({ key: "%(a)s", requirements: ["x"] }, "a.py", "a.html").then(
  () => console.log(JSON.stringify({ resolved: true, cancelled })),
  (e) => console.log(JSON.stringify({ type: e.type, cancelled })));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result.get("type") == "EnvInstallCancelled", result
    assert result["cancelled"] == [_KEY_B], (
        "cancel named the pre-flight key, so the running installer kept going"
    )


def test_one_finished_install_does_not_tear_the_overlay_off_another():
    """Two .py files with identical requirement sets share one venv key, so both
    calls track the SAME entry. A plain Set means the first to settle deletes it,
    `installing.size` hits 0, and the second install polls on with no UI and no
    cancel button."""
    result = _run_loader("""
const need = { key: "%(a)s", requirements: ["x"] };
showInstall(need);
showInstall(need);
hideInstall(need.key);
const afterFirst = installUi.mounted;
hideInstall(need.key);
console.log(JSON.stringify({ afterFirst, afterSecond: installUi.mounted }));
""" % {"a": _KEY_A})
    assert result["afterFirst"] is True, (
        "the overlay was removed while a second install with the same key was live"
    )
    assert result["afterSecond"] is False, "the overlay must go once the last one ends"


def test_the_runtime_drives_the_loader():
    """The client half, asserted structurally — nothing in this suite executes
    runtime.js (it needs a real browser), the same reasoning as
    test_calls.py::test_the_runtime_reports_supersession.

    Each assertion is one link in the chain that would otherwise break silently:
    a page would simply show the `EnvNotInstalled` error overlay and never
    install anything.
    """
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    assert "data.needs_install" in src, "the run response must be inspected"
    assert "installEnv(data.needs_install" in src, "and must drive the install"
    assert "/api/env/install" in src
    assert "/api/env/progress?key=" in src
    assert "/api/env/cancel" in src
    # The retry, and its one-shot guard: re-running after the install is the
    # whole point, and looping on it forever is the way that goes wrong.
    assert "handle(next, true)" in src, "the run must be retried once, and only once"
    # Verbatim errors survive to the page.
    assert "new Error(prog.error)" in src
