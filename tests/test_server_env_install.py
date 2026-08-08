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


def _declare(folder, deps='"pyproj"'):
    """Give `folder` a pyproject.toml declaring `deps` — the project's environment."""
    import os

    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(str(folder), "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write("[project]\nname = 't'\nversion = '0.1.0'\n"
                 f"dependencies = [{deps}]\n")


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


def test_install_surfaces_a_malformed_manifest_instead_of_500ing(tmp_path):
    """A broken pyproject.toml reads as "no environment", and that is what is said.

    Not a crash and not a spawn: an unparseable manifest cannot be synced, and
    the honest answer names the path the script is actually on.
    """
    client = _client(tmp_path)
    (tmp_path / "pyproject.toml").write_text("this is not [ toml", encoding="utf-8")
    target = _py(tmp_path, "bad.py", "def main():\n    return 1\n")
    resp = client.post("/api/env/install", json={"py": str(target)}, headers=HEADERS)
    assert resp.status_code == 400
    assert "nothing to install" in resp.json()["error"]


@requires_fused
def test_install_derives_the_project_from_the_file_not_the_body(tmp_path, monkeypatch):
    """The rule that keeps the loader's venv and the run's venv the same one."""
    client = _client(tmp_path)
    _declare(tmp_path, '"pyproj", "imagecodecs"')
    target = _py(tmp_path, "declared.py", "def main():\n    return 1\n")
    started = []
    monkeypatch.setattr(
        "fused_render.envinstall.start",
        # `key` included because `start` reports the key it used and the endpoint
        # hands that straight to the client (D214) — a double that omits it is not
        # standing in for the real function.
        lambda project: started.append(project) or {"stage": "spawn", "done": False,
                                                    "key": "0" * 16},
    )
    resp = client.post(
        "/api/env/install",
        # A body naming something else entirely — it must be ignored.
        json={"py": str(target), "requirements": ["totally-different"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["requirements"] == ["pyproj", "imagecodecs"]
    assert body["project"] == str(tmp_path)
    assert started == [str(tmp_path)]


@requires_fused
def test_install_resolves_a_relative_py_against_the_page(tmp_path, monkeypatch):
    """Same `py`/`html` contract /api/run uses, so the loader addresses the
    identical file the failed run did."""
    client = _client(tmp_path)
    (tmp_path / "sub").mkdir()
    _declare(tmp_path / "sub", '"pyproj"')
    _py(tmp_path / "sub", "rel.py", "def main():\n    return 1\n")
    monkeypatch.setattr("fused_render.envinstall.start",
                        lambda project: {"done": False, "key": "0" * 16})
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
    # `start` reaches `fused.agent_core...` unguarded — no fused, no import.
    ImportError("No module named 'fused'"),
    ModuleNotFoundError("No module named 'fused.agent_core'"),
    # `_backend_attr` raises this BY DESIGN, with the diagnostic that matters.
    RuntimeError("this fused build's Backend has no '_python_executable', so the "
                 "install loader cannot tell which interpreter project venvs use"),
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
    _declare(tmp_path, '"pyproj"')
    target = _py(tmp_path, "declared.py", "def main():\n    return 1\n")
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
    style: { cssText: "" }, textContent: "", children: [], _h: {}, dataset: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    remove() { this.removed = true; },
    addEventListener(t, f) { (this._h[t] = this._h[t] || []).push(f); },
    removeEventListener(t, f) {
      const a = this._h[t] || []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1);
    },
  };
}
// `head` + getElementById because the indeterminate bar needs keyframes, which
// inline styles cannot express — so the loader injects one <style> ONCE and finds
// it by id on every call after that (D213).
globalThis.document = {
  createElement: () => makeEl(),
  body: makeEl(),
  head: makeEl(),
  getElementById(id) {
    return this.head.children.find((c) => c.id === id) || null;
  },
};
"""


def _loader_js():
    """The install-loader block of runtime.js, verbatim."""
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    start = src.index("  const INSTALL_POLL_MS")
    return src[start:src.index("  function runPython(", start)]


def _loader_and_runpython_js():
    """The loader block PLUS `runPython`, so `handle`'s install gate is reachable.

    `handle` lives inside `runPython` and decides whether a `needs_install`
    becomes an install or an error — the single most important branch in this
    flow, and the one the narrower slice above cannot see at all. Everything
    `runPython` closes over that is defined further up the file is stubbed in
    `_RUNPYTHON_PRELUDE`.
    """
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    start = src.index("  const INSTALL_POLL_MS")
    return src[start:src.index("  function rawUrl(", start)]


# What `runPython` reads from the rest of runtime.js. Declared with `var` so the
# slice's own `const`/`function` declarations can never collide with them.
_RUNPYTHON_PRELUDE = """
var inflightByKey = new Map();
var callIds = 0;
function newCallId() { return "c" + (++callIds); }
function callHeaders(extra) { return Object.assign({}, extra || {}); }
function reportSuperseded() {}
var watched = [];
function watchPath(p) { watched.push(p); }
globalThis.window = { location: { search: "?path=/page.html" } };
"""


def _run_runpython(scenario):
    """Run `runPython` (and the loader it drives) under node against stubs."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to run runPython's own JS")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as f:
        f.write(_JS_PRELUDE + _RUNPYTHON_PRELUDE + _loader_and_runpython_js() + scenario)
        harness = f.name
    try:
        out = subprocess.run([node, harness], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(harness)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# The scenario every concurrency test below shares: /api/run answers
# `needs_install` for one project until the install lands, then succeeds.
_CONCURRENT_RUNS = """
let installs = 0, runs = 0, installed = false;
globalThis.fetch = (url, opts) => {
  if (url === "/api/run") {
    runs += 1;
    // Every concurrent call is answered from the SAME pre-install snapshot,
    // which is what really happens: all five preflights run before any of them
    // can have finished installing.
    const snapshot = installed;
    return Promise.resolve({ json: () => Promise.resolve(
      snapshot
        ? { ok: true, result: 42 }
        : { ok: false,
            needs_install: { key: "%(a)s", name: "my-app", requirements: ["cowsay"] },
            error: { type: "EnvNotInstalled",
                     message: "my-app declares dependencies that are not installed yet" },
            stdout: "" })});
  }
  if (url === "/api/env/install") {
    installs += 1;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(a)s", progress: { stage: "spawn", pct: 0, done: false } })});
  }
  if (url.startsWith("/api/env/progress")) {
    installed = true;
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: { stage: "done", pct: 100, done: true, error: null } })});
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
"""


def test_five_concurrent_scripts_in_one_project_all_succeed():
    """The case this whole change exists for, end to end through `handle`.

    PY-16 collapses every .py in a folder onto ONE key, so a page firing five
    runPython calls issues five concurrent /api/run's that all answer
    `needs_install` with the same key. With a page-scoped "already attempted"
    set, the first response installed and the other four read the key as
    already-attempted, fell through to the `!data.ok` branch, and rejected with
    the raw "declares dependencies that are not installed yet" text — the
    multi-script case failing precisely because it was multi-script.
    """
    result = _run_runpython((_CONCURRENT_RUNS + """
Promise.allSettled([
  runPython("a.py", {}, { key: "a" }),
  runPython("b.py", {}, { key: "b" }),
  runPython("c.py", {}, { key: "c" }),
  runPython("d.py", {}, { key: "d" }),
  runPython("e.py", {}, { key: "e" }),
]).then((settled) => {
  console.log(JSON.stringify({
    states: settled.map((r) => r.status),
    values: settled.map((r) => r.value),
    reasons: settled.map((r) => r.reason && r.reason.message),
    installs, runs,
  }));
});
""") % {"a": _KEY_A})
    assert result["states"] == ["fulfilled"] * 5, result["reasons"]
    assert result["values"] == [42] * 5
    assert result["installs"] == 1, (
        f"one project must mean one install POST, saw {result['installs']}"
    )


def test_a_late_response_from_before_the_install_still_retries():
    """The race the page-scoped set could not survive.

    A second call's /api/run was answered before the install began but arrives
    after it finished. The key is now "already installed", but this caller has
    not run yet — it must re-attempt, not report the stale needs_install as a
    failure.
    """
    result = _run_runpython((_CONCURRENT_RUNS + """
runPython("a.py", {}, { key: "a" }).then(() => {
  // Now that the install has settled, a caller whose /api/run was answered from
  // the pre-install snapshot arrives.
  const stale = { ok: false,
    needs_install: { key: "%(a)s", name: "my-app", requirements: ["cowsay"] },
    error: { type: "EnvNotInstalled", message: "not installed yet" }, stdout: "" };
  const realFetch = globalThis.fetch;
  let first = true;
  globalThis.fetch = (url, opts) => {
    if (url === "/api/run" && first) { first = false;
      return Promise.resolve({ json: () => Promise.resolve(stale) }); }
    return realFetch(url, opts);
  };
  return runPython("z.py", {}, { key: "z" });
}).then(
  (result) => console.log(JSON.stringify({ ok: true, result, installs })),
  (err) => console.log(JSON.stringify({ ok: false, message: err.message, installs }))
);
""") % {"a": _KEY_A})
    assert result["ok"] is True, result
    assert result["result"] == 42


def test_a_genuinely_stuck_install_still_fails_rather_than_looping():
    """The guard this must not lose: the SAME key coming back after a successful
    install means the loader and the run disagree, and installing forever hides
    that. One clear failure instead."""
    result = _run_runpython("""
let installs = 0;
globalThis.fetch = (url, opts) => {
  if (url === "/api/run")
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: false,
        needs_install: { key: "%(a)s", name: "my-app", requirements: ["cowsay"] },
        error: { type: "EnvNotInstalled", message: "not installed yet" },
        stdout: "" })});
  if (url === "/api/env/install") {
    installs += 1;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(a)s", progress: { stage: "spawn", pct: 0, done: false } })});
  }
  if (url.startsWith("/api/env/progress"))
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: { stage: "done", pct: 100, done: true, error: null } })});
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
runPython("a.py", {}, { key: "a" }).then(
  (result) => console.log(JSON.stringify({ ok: true, result, installs })),
  (err) => console.log(JSON.stringify({ ok: false, message: err.message,
                                        type: err.type, installs }))
);
""" % {"a": _KEY_A})
    assert result["ok"] is False
    assert result["installs"] == 1, "it must stop after one futile install, not loop"


def test_a_manifest_edit_and_rerun_in_the_same_page_installs_again():
    """A user who fixes pyproject.toml and re-runs must get an install.

    The key is stable per project now (it used to be derived from the requirement
    set, so editing deps minted a new key), so a page-scoped "already attempted"
    set made the second run report the raw needs_install error with no install
    offered and nothing telling the user to reload. The guard's scope is ONE call
    chain, which is the question it actually answers.
    """
    result = _run_runpython((_CONCURRENT_RUNS + """
runPython("a.py", {}, { key: "a" }).then(() => {
  installed = false;   // the user edits pyproject.toml; the venv is stale again
  return runPython("a.py", {}, { key: "a2" });
}).then(
  (result) => console.log(JSON.stringify({ ok: true, result, installs })),
  (err) => console.log(JSON.stringify({ ok: false, message: err.message, installs }))
);
""") % {"a": _KEY_A})
    assert result["ok"] is True, result
    assert result["installs"] == 2, (
        "the second run was refused instead of installing the edited manifest"
    )


def test_the_projects_manifest_is_watched_so_a_fix_triggers_a_reload():
    """A manifest edit has to reach the page without a manual reload.

    `needs_install` carries the project's pyproject.toml path precisely so the
    live-reload watcher can be pointed at it — otherwise the only feedback for
    "I fixed my dependencies" is a stale error overlay.
    """
    result = _run_runpython((_CONCURRENT_RUNS.replace(
        'requirements: ["cowsay"] }',
        'requirements: ["cowsay"], pyproject: "/proj/pyproject.toml" }',
    ) + """
runPython("a.py", {}, { key: "a" }).then(() => {
  console.log(JSON.stringify({ watched }));
});
""") % {"a": _KEY_A})
    assert "/proj/pyproject.toml" in result["watched"], result["watched"]


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
// Assigned when the row exists; `showInstall` runs synchronously inside
// `installEnv`, so it is there from the first poll onwards.
let row = null;
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install")
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(b)s", progress: { stage: "spawn", pct: 0, done: false } })});
  if (url.startsWith("/api/env/progress")) {
    polls += 1;
    if (polls === 1) {
      // The user clicks Cancel while the install is genuinely in flight. Reached
      // through the ROW for this key: each install owns its own button now, so
      // there is no single `installUi.cancel` to press.
      row = installing.get("%(a)s").row;
      row.cancel._h.click[0]();
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


def test_a_cancel_the_server_could_not_honour_is_never_silent():
    """`cancel()` reports False when there is nothing to kill yet.

    Inside the spawn window there is no recorded pid — the claim exists, `Popen`
    has not returned — so `/api/env/cancel` answers `cancelled: false` and the
    installer runs on. The client had already painted "cancelling…", the next
    `paint()` overwrote that text with the installer's own detail, the install
    finished, `poll()` RESOLVED, and the script the user had just cancelled ran
    anyway with nothing anywhere saying the cancel was dropped.

    Two things are asserted, because either alone still leaves a lie on screen:
    the user's intent wins over a resolved poll (the script does not run), and the
    dropped cancel is stated rather than painted over.
    """
    result = _run_loader("""
let polls = 0;
let row = null;
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install")
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(b)s", progress: { stage: "spawn", pct: 0, done: false } })});
  if (url.startsWith("/api/env/progress")) {
    polls += 1;
    if (polls === 1) {
      // Cancel lands inside the spawn window: nothing to kill yet. Reached through
      // the ROW for this key, since each install owns its own button now.
      row = installing.get("%(a)s").row;
      row.cancel._h.click[0]();
      return Promise.resolve({ json: () => Promise.resolve({ ok: true,
        progress: { stage: "install", pct: 25, detail: "downloading", done: false } })});
    }
    // ...and the install the user cancelled runs to completion.
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: { stage: "done", pct: 100, detail: "installed", done: true,
                  error: null } })});
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(
    { ok: true, cancelled: false }) });
};
installEnv({ key: "%(a)s", requirements: ["x"] }, "a.py", "a.html").then(
  () => console.log(JSON.stringify({ resolved: true, detail: row.detail.textContent })),
  (e) => console.log(JSON.stringify({ type: e.type, detail: row.detail.textContent })));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result.get("type") == "EnvInstallCancelled", (
        "the install resolved after the user cancelled it, so the script ran"
    )
    assert "could not be stopped" in (result.get("detail") or ""), (
        "a dropped cancel was painted over instead of being reported: "
        f"{result.get('detail')!r}"
    )


def test_one_finished_install_does_not_tear_the_overlay_off_another():
    """Two .py files with identical requirement sets share one venv key, so both
    calls track the SAME entry. A plain Set means the first to settle deletes it,
    `installing.size` hits 0, and the second install polls on with no UI and no
    cancel button.

    Asserted across the mount delay rather than synchronously, because the overlay
    no longer appears the instant an install starts: it is scheduled and mounts only
    if something is still running when the timer fires. So the sequence checked here
    is the whole lifecycle — the entry survives the first settle, the overlay does
    appear while a waiter is live, and it goes on the LAST settle.
    """
    result = _run_loader("""
const need = { key: "%(a)s", requirements: ["x"] };
showInstall(need);
showInstall(need);
hideInstall(need.key);
const afterFirst = installing.has(need.key);
setTimeout(() => {
  const mountedWhileLive = installUi.mounted;
  hideInstall(need.key);
  console.log(JSON.stringify({ afterFirst, mountedWhileLive,
                               afterSecond: installUi.mounted,
                               live: installing.size }));
}, 900);
""" % {"a": _KEY_A})
    assert result["afterFirst"] is True, (
        "the shared entry was dropped while a second install with the same key was live"
    )
    assert result["mountedWhileLive"] is True, (
        "the overlay never appeared for an install that outlived the mount delay"
    )
    assert result["afterSecond"] is False, "the overlay must go once the last one ends"
    assert result["live"] == 0, "the entry outlived its last waiter"


# --- the install stage has no percentage, so the bar must not claim one (D213) --
#
# The worker parks at pct 25 for the entire download (there is nothing to measure
# behind uv's captured output), so a bar sitting at 25% for four minutes reads as
# frozen — which is exactly what was reported. These assert the DOM-observable
# contract: `bar.dataset.indeterminate` is the flag the CSS animation hangs off.
# What they CANNOT see is whether the result looks right; that needs a human glance.


def test_the_install_stage_paints_an_indeterminate_bar():
    """A number nobody can compute must not be displayed as a number.

    `stage === "install"` is the one stage with no measurable progress, so the bar
    switches to the indeterminate marker instead of parking at 25%. Stages that DO
    carry a real value (`create` at 10, `done` at 100) must switch back to a real
    width, or a finished install would animate forever.
    """
    result = _run_loader("""
const ui = installRow();
const seen = [];
const snap = (label) => seen.push([label, ui.bar.dataset.indeterminate || null,
                                   ui.bar.style.width]);
paintInstall(ui, { stage: "create", pct: 10, detail: "preparing", done: false });
snap("create");
paintInstall(ui, { stage: "install", pct: 25, detail: "downloading (2m14s)",
                   done: false });
snap("install");
paintInstall(ui, { stage: "done", pct: 100, detail: "installed", done: true });
snap("done");
console.log(JSON.stringify({ seen, detail: ui.detail.textContent }));
""")
    by_label = {row[0]: row[1:] for row in result["seen"]}
    assert by_label["create"][0] is None, "a stage with a real pct must not animate"
    assert by_label["create"][1] == "10%"
    assert by_label["install"][0] == "1", "the install stage must be indeterminate"
    assert by_label["done"][0] is None, "a finished install must stop animating"
    assert by_label["done"][1] == "100%"
    assert result["detail"] == "installed"


def test_the_python_stage_paints_an_indeterminate_bar_too():
    """The interpreter download has no measurable progress either (D214).

    `_acquire_python` captures uv's output exactly as upstream's builder does, so
    `python` parks at pct 5 for the whole ~30MB fetch. A bar frozen at 5% is the same
    "it looks broken" this treatment exists to fix — and the stage list is explicit,
    so a stage added later renders as a plain bar rather than silently inheriting the
    sweep.
    """
    result = _run_loader("""
const ui = installRow();
const seen = [];
const snap = (label) => seen.push([label, ui.bar.dataset.indeterminate || null,
                                   ui.bar.style.width]);
paintInstall(ui, { stage: "python", pct: 5, detail: "downloading Python 3.12 (14s)",
                   done: false });
snap("python");
paintInstall(ui, { stage: "done", pct: 100, detail: "downloaded Python 3.12",
                   done: true });
snap("done");
console.log(JSON.stringify({ seen, detail: ui.detail.textContent }));
""")
    by_label = {row[0]: row[1:] for row in result["seen"]}
    assert by_label["python"][0] == "1", "the python stage must be indeterminate"
    assert by_label["done"][0] is None, "a finished download must stop animating"
    assert by_label["done"][1] == "100%"
    assert result["detail"] == "downloaded Python 3.12"


def test_the_interpreter_round_is_titled_PYTHON_not_the_project():
    """Naming the project during the interpreter download is a small lie with a
    real cost: the user watches "Preparing my-app" for minutes while nothing about
    my-app is happening, which is how a working install comes to look stuck.
    `need.python` is present only on that round."""
    result = _run_loader("""
const ui = showInstall({ key: "%(a)s", name: "my-app", requirements: ["tensorflow"],
                         python: "3.12" });
const first = ui.title.textContent;
const firstDetail = ui.detail.textContent;
hideInstall("%(a)s");
const second = showInstall({ key: "%(b)s", name: "my-app",
                             requirements: ["tensorflow"] });
console.log(JSON.stringify({ first, firstDetail, second: second.title.textContent,
                             secondDetail: second.detail.textContent }));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["first"] == "Installing Python 3.12"
    assert "tensorflow" not in result["firstDetail"], (
        "the interpreter round must not name packages it is not downloading"
    )
    # The package round is titled by the PROJECT — one row for the whole folder,
    # however many scripts wait on it — with the packages demoted to the detail.
    assert result["second"] == "Preparing my-app"
    assert "tensorflow" in result["secondDetail"]


def test_a_project_with_no_name_still_gets_a_readable_title():
    """`name` is additive (engine.py), so a client/server version skew must not
    render the literal `undefined` at the user."""
    result = _run_loader("""
const ui = showInstall({ key: "%(a)s", requirements: ["x"] });
console.log(JSON.stringify({ title: ui.title.textContent }));
""" % {"a": _KEY_A})
    assert result["title"] == "Preparing the environment"


def test_two_rounds_are_allowed_when_they_install_DIFFERENT_things():
    """Interpreter then packages, each under its own key (D214).

    The guard used to be a boolean, which was right while exactly one install could
    ever be needed. With the interpreter round it is not: the run's second
    needs_install is correct and necessary, and a boolean failed it as
    "something disagrees about the venv key" — leaving a page that had just
    downloaded Python unable to install anything with it.
    """
    result = _run_loader("""
const installed = new Set();
const pythonRound = { key: "%(a)s", requirements: ["tensorflow"], python: "3.12" };
const packageRound = { key: "%(b)s", requirements: ["tensorflow"] };
const seen = [];
// Round 1: the interpreter. New key -> install it.
seen.push(shouldInstall(pythonRound, installed));
installed.add(pythonRound.key);
// Round 2: the packages. DIFFERENT key -> still install.
seen.push(shouldInstall(packageRound, installed));
installed.add(packageRound.key);
// Round 3: the same package key back again -> nothing changed, so stop.
seen.push(shouldInstall(packageRound, installed));
console.log(JSON.stringify({ seen }));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["seen"] == [True, True, False], (
        "expected install, install, then refuse — got " + repr(result["seen"])
    )


def test_a_repeated_key_still_fails_instead_of_installing_forever():
    """The loop guard this replaced a boolean with must still hold. A run that keeps
    asking for the SAME key after a successful install means the loader and the run
    disagree about the venv key, and installing forever hides that."""
    result = _run_loader("""
const installed = new Set();
const need = { key: "%(a)s", requirements: ["x"] };
shouldInstall(need, installed);
installed.add(need.key);
console.log(JSON.stringify({
  again: shouldInstall(need, installed),
  // A needs_install with no key at all is not something to install either.
  keyless: shouldInstall({ requirements: ["x"] }, installed),
  absent: shouldInstall(undefined, installed),
}));
""" % {"a": _KEY_A})
    assert result["again"] is False
    assert result["keyless"] is False
    assert result["absent"] is False


def test_the_keyframes_are_injected_once_however_many_installs_run():
    """The overlay is built from inline styles, which cannot express keyframes, so
    one <style> is injected — and injected ONCE, or every install of a session
    leaves another copy behind in `document.head`."""
    result = _run_loader("""
const need = { key: "%(a)s", requirements: ["x"] };
showInstall(need);
showInstall(need);
hideInstall(need.key);
hideInstall(need.key);
showInstall(need);
console.log(JSON.stringify({ styles: document.head.children.length }));
""" % {"a": _KEY_A})
    assert result["styles"] == 1, result


def test_an_install_that_is_already_running_is_not_painted_as_zero_percent():
    """Re-opening a page mid-install must not read as a restart.

    `showInstall` used to assert `0%` unconditionally, so joining an install four
    minutes in painted 0% and then jumped to 25% on the first poll. The server is
    doing the right thing (`start()` JOINS a running install rather than
    duplicating it), but a user switching between apps saw 0% → 25% → freeze over
    and over and concluded it was looping. So the initial state claims no
    percentage at all, and the first real paint comes from the server's own record.
    """
    result = _run_loader("""
// The row for this key is registered BEFORE installEnv runs, so the recording
// setter is in place for `showInstall`'s own paints — which is where the
// zero-percent bug lived. Attaching it afterwards would still pass while leaving
// that exact regression invisible. `showInstall` reuses an existing entry for the key rather
// than building a second row, so the loader paints into this very bar.
const ui = installOverlay();
const row = installRow();
installing.set("%(a)s", { row, count: 0 });
const widths = [];
let w = row.bar.style.width;
Object.defineProperty(row.bar.style, "width", {
  get: () => w,
  set: (v) => { w = v; widths.push(v); },
});
let polls = 0;
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install")
    // What the server answers when it JOINS an install already in flight.
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true,
      key: "%(b)s",
      progress: { stage: "install", pct: 25, done: false,
                  detail: "downloading and installing 2 package(s): a, b (4m02s)" }})});
  if (url.startsWith("/api/env/progress")) {
    polls += 1;
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: polls === 1
        ? { stage: "install", pct: 25, done: false, detail: "downloading (4m03s)" }
        : { stage: "done", pct: 100, done: true, error: null, detail: "installed" }})});
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
installEnv({ key: "%(a)s", requirements: ["a", "b"] }, "a.py", "a.html").then(
  () => console.log(JSON.stringify({ ok: true, widths })),
  (e) => console.log(JSON.stringify({ ok: false, error: e.message, widths })));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["ok"] is True, result
    assert "0%" not in result["widths"], (
        f"a joined install was painted as 0% first: {result['widths']}"
    )
    assert result["widths"][-1] == "100%", result["widths"]


# --- several installs at once, each with its own row --------------------------
#
# The gap that let the shared-overlay bug ship: the only multi-install test above
# passes the SAME key twice. Under the folder rule that case is now handled a
# level up — `installEnv` dedups by key, so several .py files in one project join
# ONE install (see the dedup tests further down). The case that still produces
# concurrent rows is an HTML view calling .py files from DIFFERENT projects (or
# the D214 interpreter round alongside a package round), so N installs with N
# distinct keys run against what used to be one title, one detail line and one
# Cancel button.


def test_two_distinct_installs_get_their_own_rows():
    """Distinct keys must not share nodes.

    With one shared set, the title named whichever install started last and the
    detail line flipped between N pollers at 2Hz. This asserts the structural fix:
    two entries, two rows, two bars, both attached to the overlay.
    """
    result = _run_loader("""
const ui = installOverlay();
const a = showInstall({ key: "%(a)s", name: "geo", requirements: ["imagecodecs"] });
const b = showInstall({ key: "%(b)s", name: "zarr", requirements: ["s3fs"] });
console.log(JSON.stringify({
  live: installing.size,
  rows: ui.rows.children.length,
  sameRow: a === b,
  sameBar: a.bar === b.bar,
  sameCancel: a.cancel === b.cancel,
  titleA: a.title.textContent,
  titleB: b.title.textContent,
}));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["live"] == 2, result
    assert result["rows"] == 2, "both installs must be visible at once"
    assert result["sameRow"] is False
    assert result["sameBar"] is False
    assert result["sameCancel"] is False, (
        "one shared Cancel button means a single click cancels every install"
    )
    assert result["titleA"] == "Preparing geo"
    assert result["titleB"] == "Preparing zarr", (
        "the second install overwrote the first install's title"
    )


def test_painting_one_install_does_not_touch_another():
    """Each poller writes only its own row.

    Two pollers on one detail line is what made a legitimate pair of installs read
    as a single flickering one, so this pins the isolation directly rather than
    through the DOM shape alone.
    """
    result = _run_loader("""
const a = showInstall({ key: "%(a)s", name: "geo", requirements: ["imagecodecs"] });
const b = showInstall({ key: "%(b)s", name: "zarr", requirements: ["s3fs"] });
paintInstall(a, { stage: "install", pct: 25, detail: "fetching imagecodecs",
                  done: false });
paintInstall(b, { stage: "create", pct: 10, detail: "preparing s3fs", done: false });
console.log(JSON.stringify({
  detailA: a.detail.textContent, detailB: b.detail.textContent,
  indetA: a.bar.dataset.indeterminate || null,
  indetB: b.bar.dataset.indeterminate || null,
  widthB: b.bar.style.width,
}));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["detailA"] == "fetching imagecodecs"
    assert result["detailB"] == "preparing s3fs", "one poller overwrote the other's detail"
    assert result["indetA"] == "1", "the install stage must stay indeterminate"
    assert result["indetB"] is None, "a stage with a real pct must not animate"
    assert result["widthB"] == "10%"


def test_finishing_one_install_leaves_the_other_row_alone():
    """The row goes with its own key, and only that key."""
    result = _run_loader("""
const ui = installOverlay();
showInstall({ key: "%(a)s", requirements: ["imagecodecs"] });
const b = showInstall({ key: "%(b)s", requirements: ["s3fs"] });
paintInstall(b, { stage: "install", pct: 25, detail: "fetching s3fs", done: false });
hideInstall("%(a)s");
console.log(JSON.stringify({
  live: installing.size,
  stillB: installing.has("%(b)s"),
  detailB: b.detail.textContent,
  removedB: b.el.removed || false,
}));
""" % {"a": _KEY_A, "b": _KEY_B})
    assert result["live"] == 1
    assert result["stillB"] is True
    assert result["removedB"] is False, "the surviving install's row was torn out"
    assert result["detailB"] == "fetching s3fs", (
        "the finished install wiped the running one's detail"
    )


def test_a_fast_install_never_mounts_the_overlay():
    """An install that beats the mount delay must not flash a modal.

    The overlay used to mount synchronously, before anything was known about
    duration, so a warm-cache install measured in tens of milliseconds still threw a
    full-screen modal over the page and pulled it down again.
    """
    result = _run_loader("""
const need = { key: "%(a)s", requirements: ["x"] };
showInstall(need);
const duringInstall = installUi.mounted;
hideInstall(need.key);
// Past the delay: the cancelled timer must not mount an overlay after the fact.
setTimeout(() => {
  console.log(JSON.stringify({ duringInstall, afterDelay: installUi.mounted,
                               timer: installUi.mountTimer }));
}, 900);
""" % {"a": _KEY_A})
    assert result["duringInstall"] is False, "the overlay mounted before the delay"
    assert result["afterDelay"] is False, (
        "a finished install mounted the overlay after the fact"
    )
    assert result["timer"] is None, "the pending mount timer was left armed"


def test_the_poll_interval_ramps_then_backs_off():
    """The first second is polled fast, then it settles to the slow grid.

    A fixed 500ms grid meant a ~540ms install was not noticed until its second
    poll. What is asserted is the observable SCHEDULE — the gaps the loader asks
    for — because wall-clock timing in a test would be measuring the machine.

    Both the timer and the clock are stubbed, and the clock is the important one:
    the ramp is keyed on the INSTALL's age rather than on a poll count, so with
    real time frozen by an instant-firing timer it would stay in its fast phase
    forever and this test would assert nothing about the back-off.
    """
    result = _run_loader("""
const gaps = [];
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => { gaps.push(ms); return realSetTimeout(fn, 0); };
// A controllable clock: 200ms of install age per poll, so the 1000ms fast window
// is crossed on the fifth one.
let now = 0;
Date.now = () => now;
let polls = 0;
globalThis.fetch = (url) => {
  if (url === "/api/env/install")
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(b)s", progress: { stage: "install", pct: 25, done: false } })});
  polls += 1;
  now += 200;
  // Twelve unfinished polls, so the ramp has to cross its window and back off.
  return Promise.resolve({ json: () => Promise.resolve({ ok: true,
    progress: polls < 12
      ? { stage: "install", pct: 25, done: false }
      : { stage: "done", pct: 100, done: true, error: null } })});
};
installEnv({ key: "%(a)s", requirements: ["x"] }, "a.py", "a.html").then(
  () => console.log(JSON.stringify({ gaps })),
  (e) => console.log(JSON.stringify({ error: e.message, gaps })));
""" % {"a": _KEY_A, "b": _KEY_B})
    gaps = [g for g in result.get("gaps", []) if g in (100, 500)]
    assert gaps, result
    assert gaps[0] == 100, f"the first poll waited the full grid: {gaps}"
    assert 500 in gaps, f"the interval never backed off: {gaps}"
    # Monotonic: fast first, slow after — never back to fast.
    assert gaps == sorted(gaps), f"the ramp went backwards: {gaps}"


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
    # The retry, and its loop guard: re-running after the install is the whole
    # point, and looping on it forever is the way that goes wrong. The guard is
    # keyed on PROGRESS rather than a count since D214 — the interpreter round and
    # the package round are two legitimate installs — so what is pinned here is that
    # the set is threaded through the retry and consulted before installing.
    assert "handle(next, installed)" in src, "the run must be retried after an install"
    assert "shouldInstall(data.needs_install, installed)" in src, (
        "the retry must be gated on the progress rule, not on a bare boolean"
    )
    assert "installed.add(data.needs_install.key)" in src, (
        "a key must be recorded as installed, or the guard can never close"
    )
    # Verbatim errors survive to the page.
    assert "new Error(prog.error)" in src


# --- one install per project, not one per script (SPEC PY-16) ------------------
#
# The key is the project FOLDER now, so a page calling five .py files from one
# folder resolves to one key. The server was never at risk of doing the work
# twice — `start()` claims the key atomically and joins — so what these pin is
# the CLIENT side: one POST, one poller, one cancel listener, and every waiter
# settling together.

_DEDUP_PRELUDE = """
let posts = 0, polls = 0, cancels = 0, doneAfter = 2;
globalThis.fetch = (url, opts) => {
  if (url === "/api/env/install") {
    posts += 1;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, key: "%(a)s", progress: { stage: "spawn", pct: 0, done: false } })});
  }
  if (url.startsWith("/api/env/progress")) {
    polls += 1;
    const done = polls >= doneAfter;
    return Promise.resolve({ json: () => Promise.resolve({ ok: true,
      progress: { stage: done ? "done" : "install", pct: done ? 100 : 25,
                  done, error: OUTCOME } })});
  }
  if (url === "/api/env/cancel") {
    cancels += 1;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ok: true, cancelled: true })});
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
};
const need = { key: "%(a)s", name: "my-app", requirements: ["cowsay"] };
"""


def test_three_scripts_in_one_project_issue_one_install(monkeypatch):
    """Three concurrent runPython calls, one project: one POST and one poller.

    Before the dedup each caller built its own activeKey, poller and cancel
    listener against a SHARED row — three POSTs, three pollers at 2Hz, and three
    listeners on one Cancel button.
    """
    result = _run_loader(("const OUTCOME = null;" + _DEDUP_PRELUDE + """
Promise.all([
  installEnv(need, "a.py", "p.html"),
  installEnv(need, "b.py", "p.html"),
  installEnv(need, "c.py", "p.html"),
]).then((settled) => {
  console.log(JSON.stringify({
    posts, polls, rows: installing.size, resolved: settled.length,
    // Every caller must get the SAME promise, not three chains that happen to
    // agree — that is what makes one poller enough.
    shared: settled[0] === settled[1] && settled[1] === settled[2],
  }));
});
""") % {"a": _KEY_A})
    assert result["posts"] == 1, f"one project, {result['posts']} install POSTs"
    assert result["polls"] == 2, f"one poller expected, saw {result['polls']} polls"
    assert result["resolved"] == 3, "every caller must be resolved"
    assert result["shared"] is True
    assert result["rows"] == 0, "the row must be torn down once, by the last waiter"


def test_every_waiter_rejects_when_the_shared_install_fails():
    """A failure reaches all three, verbatim — not just whoever started it."""
    result = _run_loader((
        'const OUTCOME = "No solution found: imagecodecs has no wheels";'
        + _DEDUP_PRELUDE + """
Promise.allSettled([
  installEnv(need, "a.py", "p.html"),
  installEnv(need, "b.py", "p.html"),
  installEnv(need, "c.py", "p.html"),
]).then((settled) => {
  console.log(JSON.stringify({
    states: settled.map((r) => r.status),
    messages: settled.map((r) => r.reason && r.reason.message),
    types: settled.map((r) => r.reason && r.reason.type),
    posts, rows: installing.size,
  }));
});
""") % {"a": _KEY_A})
    assert result["states"] == ["rejected"] * 3
    assert all("imagecodecs" in m for m in result["messages"]), result["messages"]
    assert result["types"] == ["EnvInstallError"] * 3
    assert result["posts"] == 1
    assert result["rows"] == 0


def test_one_cancel_click_fires_one_cancel_request():
    """One row, one listener. Three chains meant a click sent three cancels and
    each chain's message overwrote the others'."""
    result = _run_loader(("const OUTCOME = null;" + _DEDUP_PRELUDE + """
doneAfter = 1000;   // never finishes on its own
const waiters = [
  installEnv(need, "a.py", "p.html"),
  installEnv(need, "b.py", "p.html"),
  installEnv(need, "c.py", "p.html"),
];
const row = installing.get("%(a)s").row;
const listeners = (row.cancel._h.click || []).length;
row.cancel._h.click.forEach((f) => f());
setTimeout(() => {
  Promise.allSettled(waiters).then(() => {});
  console.log(JSON.stringify({ cancels, listeners, posts }));
  process.exit(0);
}, 50);
""") % {"a": _KEY_A})
    assert result["listeners"] == 1, (
        f"{result['listeners']} cancel listeners on one row — a click fires that many"
    )
    assert result["cancels"] == 1
    assert result["posts"] == 1


def test_a_later_run_starts_a_fresh_install_rather_than_replaying_the_old_one():
    """The registry entry is dropped when the promise SETTLES.

    Otherwise a retry after a fixed pyproject.toml (or a `watchPath` reload) would
    resolve instantly against a stale result and run against an environment that
    was never rebuilt.
    """
    result = _run_loader(("const OUTCOME = null;" + _DEDUP_PRELUDE + """
installEnv(need, "a.py", "p.html")
  .then(() => { polls = 0; return installEnv(need, "a.py", "p.html"); })
  .then(() => { console.log(JSON.stringify({ posts, polls })); });
""") % {"a": _KEY_A})
    assert result["posts"] == 2, "the second run must not replay the first's promise"
    assert result["polls"] == 2


def test_the_dedup_registry_is_wired_into_the_loader():
    """Structural backstop, in the same shape as the wiring test above: the
    behaviour tests run the loader in isolation, so this pins that the registry
    is what `runPython`'s install path actually goes through."""
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    assert "installInFlight" in src, (
        "the registry is what makes N scripts in one project ONE install"
    )
    assert "handle(data, new Set())" in src, (
        "the loop guard must be scoped to ONE call chain. A page-scoped set makes "
        "concurrent scripts in one project reject with the raw needs_install "
        "error, and refuses the install after a user fixes their pyproject.toml"
    )
