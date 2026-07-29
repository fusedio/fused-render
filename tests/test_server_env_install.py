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

from fastapi.testclient import TestClient

from fused_render.server import create_app

HEADERS = {"X-Fused": "1"}


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
    resp = client.get("/api/env/progress?key=neverstarted", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["progress"] is None


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


def test_cancel_needs_a_key(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/env/cancel", json={}, headers=HEADERS)
    assert resp.status_code == 400
    assert "'key'" in resp.json()["error"]


def test_cancel_of_an_unknown_key_is_a_clean_no_op(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/env/cancel", json={"key": "neverstarted"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


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
