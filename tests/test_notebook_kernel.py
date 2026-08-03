"""Protocol tests for the notebook template's mini-kernel (kernel_body.py):
spawn it with this interpreter, feed execute ops over stdin, and assert the
JSON-lines events — streams, last-expression display, state persistence
across cells, error shape, and the stdin interrupt path."""

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

BODY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "notebook", "kernel_body.py")
KERNEL = os.path.join(os.path.dirname(BODY), "kernel.py")


def _load_kernel_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nb_kernel", KERNEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class KernelProc:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, BODY],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        self.events = []
        self.lock = threading.Lock()
        self.new_event = threading.Condition(self.lock)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            ev = json.loads(line)
            with self.new_event:
                self.events.append(ev)
                self.new_event.notify_all()

    def send(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pred, timeout=15):
        deadline = time.monotonic() + timeout
        with self.new_event:
            while True:
                for ev in self.events:
                    if pred(ev):
                        return ev
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"event not seen within {timeout}s; got {self.events}")
                self.new_event.wait(remaining)

    def run(self, exec_id, code, timeout=15):
        self.send({"op": "execute", "id": exec_id, "code": code})
        self.wait_for(lambda e: e.get("type") == "done" and e.get("id") == exec_id,
                      timeout)
        with self.lock:
            return [e for e in self.events if e.get("id") == exec_id]

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture()
def kernel():
    k = KernelProc()
    k.wait_for(lambda e: e.get("type") == "ready")
    yield k
    k.close()


def _of_type(events, t):
    return [e for e in events if e.get("type") == t]


def test_ready_event(kernel):
    ev = kernel.wait_for(lambda e: e.get("type") == "ready")
    assert ev["python"] and ev["version"]


def test_stream_stdout(kernel):
    events = kernel.run("e1", "print('hello')\nprint('world')")
    text = "".join(e["text"] for e in _of_type(events, "stream")
                   if e["name"] == "stdout")
    assert text == "hello\nworld\n"


def test_execution_timing_events(kernel):
    events = kernel.run("e1", "import time; time.sleep(0.2)")
    types = [e["type"] for e in events]
    assert types.index("started") < types.index("done")
    done = _of_type(events, "done")[0]
    # Windows sleep/monotonic granularity undershoots — allow slack
    assert done["duration_ms"] >= 150


def test_stream_stderr(kernel):
    events = kernel.run("e1", "import sys; sys.stderr.write('oops\\n')")
    text = "".join(e["text"] for e in _of_type(events, "stream")
                   if e["name"] == "stderr")
    assert text == "oops\n"


def test_last_expression_display(kernel):
    events = kernel.run("e1", "a = 20\na + 22")
    results = _of_type(events, "execute_result")
    assert results and results[0]["data"] == {"text/plain": "42"}


def test_statement_only_has_no_result(kernel):
    events = kernel.run("e1", "a = 1")
    assert not _of_type(events, "execute_result")


def test_none_result_is_suppressed(kernel):
    events = kernel.run("e1", "None")
    assert not _of_type(events, "execute_result")


def test_state_persists_across_cells(kernel):
    kernel.run("e1", "x = 1")
    events = kernel.run("e2", "x + 1")
    assert _of_type(events, "execute_result")[0]["data"] == {"text/plain": "2"}


def test_repr_html_display(kernel):
    code = ("class T:\n"
            "    def _repr_html_(self):\n"
            "        return '<b>rich</b>'\n"
            "T()")
    events = kernel.run("e1", code)
    assert _of_type(events, "execute_result")[0]["data"] == {"text/html": "<b>rich</b>"}


def test_error_event(kernel):
    events = kernel.run("e1", "y = 1\nraise ValueError('boom')")
    err = _of_type(events, "error")[0]
    assert err["ename"] == "ValueError" and err["evalue"] == "boom"
    tb = "\n".join(err["traceback"])
    assert "<cell>" in tb and "kernel_body" not in tb
    # the kernel keeps serving after an error
    events = kernel.run("e2", "y")
    assert _of_type(events, "execute_result")[0]["data"] == {"text/plain": "1"}


def test_syntax_error(kernel):
    events = kernel.run("e1", "def broken(:")
    err = _of_type(events, "error")[0]
    assert err["ename"] == "SyntaxError"


def test_output_truncation(kernel):
    events = kernel.run("e1", "print('x' * 1000000)\nprint('y' * 3000000)",
                        timeout=60)
    total = sum(len(e["text"]) for e in _of_type(events, "stream"))
    assert total <= 2 * 1024 * 1024 + 100
    assert any("truncated" in e["text"] for e in _of_type(events, "stream"))
    # the cap is per execution — the next cell streams again
    events = kernel.run("e2", "print('fresh')")
    assert any("fresh" in e["text"] for e in _of_type(events, "stream"))


def test_interrupt_aborts_queued_cells(kernel):
    kernel.send({"op": "execute", "id": "e1", "code": "import time\ntime.sleep(60)"})
    kernel.wait_for(lambda e: e.get("type") == "started" and e.get("id") == "e1")
    # e2/e3 sit on the queue behind the sleeping cell; the stdin pipe is FIFO,
    # so they are queued before the interrupt is processed
    kernel.send({"op": "execute", "id": "e2", "code": "x = 'ran'"})
    kernel.send({"op": "execute", "id": "e3", "code": "y = 'ran'"})
    kernel.send({"op": "interrupt"})
    for eid in ("e1", "e2", "e3"):
        kernel.wait_for(lambda e, eid=eid: e.get("type") == "done" and e.get("id") == eid)
    with kernel.lock:
        started = {e["id"] for e in kernel.events if e.get("type") == "started"}
        errs = {e["id"] for e in kernel.events if e.get("type") == "error"}
    assert started == {"e1"}
    assert {"e1", "e2", "e3"} <= errs
    # the queued cells never executed
    events = kernel.run("e4", "('x' in dir(), 'y' in dir())")
    assert _of_type(events, "execute_result")[0]["data"] == {"text/plain": "(False, False)"}


def test_interrupt_while_idle_is_a_noop(kernel):
    kernel.run("e1", "1")
    # a stray interrupt with nothing running must not leave a pending SIGINT
    # that swallows the next execute (Windows lock waits are uninterruptible)
    kernel.send({"op": "interrupt"})
    events = kernel.run("e2", "2 + 2")
    assert _of_type(events, "execute_result")[0]["data"] == {"text/plain": "4"}
    assert not _of_type(events, "error")


def test_interrupt_running_cell(kernel):
    kernel.send({"op": "execute", "id": "e1", "code": "import time\ntime.sleep(60)"})
    time.sleep(0.5)  # let the cell reach the sleep
    kernel.send({"op": "interrupt"})
    kernel.wait_for(lambda e: e.get("type") == "done" and e.get("id") == "e1",
                    timeout=15)
    with kernel.lock:
        errs = [e for e in kernel.events
                if e.get("type") == "error" and e.get("id") == "e1"]
    assert errs and errs[0]["ename"] == "KeyboardInterrupt"
    # still serving afterwards
    events = kernel.run("e2", "1 + 1")
    assert _of_type(events, "execute_result")[0]["data"] == {"text/plain": "2"}


def test_matplotlib_figure(kernel):
    pytest.importorskip("matplotlib")
    code = ("import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3])\n"
            "plt.show()")
    events = kernel.run("e1", code, timeout=60)
    disp = _of_type(events, "display_data")
    assert disp and disp[0]["data"]["image/png"]
    # plt.show() under Agg must not spam the non-interactive warning
    stderr = "".join(e["text"] for e in _of_type(events, "stream")
                     if e.get("name") == "stderr")
    assert "non-interactive" not in stderr
    # figures are closed after the cell — a no-plot cell emits none
    events = kernel.run("e2", "z = 1")
    assert not _of_type(events, "display_data")


# --------------------------------------------------- modal path resolution

@pytest.fixture(scope="module")
def kernel_mod():
    return _load_kernel_module()


def test_resolve_plain_name(kernel_mod, tmp_path):
    r = kernel_mod._resolve_dest(str(tmp_path), "plain")
    assert r["path"] == str(tmp_path).replace(os.sep, "/") + "/plain.ipynb"
    assert r["name"] == "plain.ipynb"


def test_resolve_keeps_existing_suffix(kernel_mod, tmp_path):
    r = kernel_mod._resolve_dest(str(tmp_path), "done.IPYNB")
    assert r["path"].endswith("/done.IPYNB")
    assert not r["path"].lower().endswith(".ipynb.ipynb")


def test_resolve_relative_subpath(kernel_mod, tmp_path):
    (tmp_path / "sub").mkdir()
    r = kernel_mod._resolve_dest(str(tmp_path), "sub/x")
    assert r["path"].endswith("/sub/x.ipynb")
    assert r["dir"].endswith("/sub")


def test_resolve_absolute_overrides_directory(kernel_mod, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    r = kernel_mod._resolve_dest(str(tmp_path), str(other / "abs"))
    assert r["dir"] == str(other).replace(os.sep, "/")
    assert r["path"].endswith("/other/abs.ipynb")


@pytest.mark.skipif(os.name != "nt", reason="Windows separator semantics")
def test_resolve_windows_backslashes(kernel_mod, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    r = kernel_mod._resolve_dest(str(tmp_path), "sub\\y")
    assert r["path"].endswith("/sub/y.ipynb")
    r = kernel_mod._resolve_dest(str(tmp_path), str(sub) + "\\z")
    assert r["path"].endswith("/sub/z.ipynb")


def test_resolve_missing_parent_is_error(kernel_mod, tmp_path):
    r = kernel_mod._resolve_dest(str(tmp_path), "no-such-dir/x")
    assert "Folder does not exist" in r["error"]
    assert "no-such-dir" in r["error"]


def test_resolve_empty_name_is_error(kernel_mod, tmp_path):
    assert "error" in kernel_mod._resolve_dest(str(tmp_path), "  ")


def test_resolve_with_src_checks_parent_via_stat_not_local_probe(
        kernel_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(kernel_mod, "_remote_meta",
                        lambda src, p: seen.append(p) or {"remote": True, "is_dir": True})
    monkeypatch.setattr(kernel_mod.os.path, "isdir",
                        lambda _: pytest.fail("must not probe a possibly mounted parent"))
    r = kernel_mod._resolve_dest("/mnt/data", "sub/x", "http://127.0.0.1:1")
    assert r["path"].endswith("/sub/x.ipynb")
    assert seen and seen[0].replace(os.sep, "/").endswith("/sub")


def test_resolve_remote_missing_parent_is_error(kernel_mod, monkeypatch):
    def stat_404(src, p):
        raise urllib.error.HTTPError(src, 404, "not found", {}, None)

    monkeypatch.setattr(kernel_mod, "_remote_meta", stat_404)
    r = kernel_mod._resolve_dest("/mnt/data", "nope/x", "http://127.0.0.1:1")
    assert "Folder does not exist" in r["error"]


def test_resolve_remote_parent_that_is_a_file_is_error(kernel_mod, monkeypatch):
    monkeypatch.setattr(kernel_mod, "_remote_meta",
                        lambda src, p: {"remote": True, "is_dir": False})
    r = kernel_mod._resolve_dest("/mnt/data", "file.txt/x", "http://127.0.0.1:1")
    assert "Folder does not exist" in r["error"]


def test_resolve_unresponsive_mount_stat_propagates(kernel_mod, monkeypatch):
    def stat_503(src, p):
        raise urllib.error.HTTPError(src, 503, "mount unresponsive", {}, None)

    monkeypatch.setattr(kernel_mod, "_remote_meta", stat_503)
    with pytest.raises(urllib.error.HTTPError):
        kernel_mod._resolve_dest("/mnt/data", "x", "http://127.0.0.1:1")


# ------------------------------------------------- daemon cache dir per home

def test_cache_dir_prefers_resolved_home_dir(kernel_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "h1"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "h2"))
    assert kernel_mod._cache_dir() == os.path.join(
        str(tmp_path / "h1"), "cache", "notebook-daemon")


def test_cache_dir_falls_back_to_home_env(kernel_mod, monkeypatch, tmp_path):
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    assert kernel_mod._cache_dir() == os.path.join(
        str(tmp_path), "cache", "notebook-daemon")


def test_cache_dir_default_without_server_env(kernel_mod, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_HOME_DIR", raising=False)
    monkeypatch.delenv("FUSED_RENDER_HOME", raising=False)
    assert kernel_mod._cache_dir() == os.path.expanduser(
        "~/.cache/fused-render-notebook")


def test_listdir_does_not_fallback_to_a_kernel_scan_when_stat_fails(
        kernel_mod, monkeypatch, tmp_path):
    def stat_failed(*_):
        raise OSError("server unavailable")

    monkeypatch.setattr(kernel_mod, "_remote_meta", stat_failed)
    monkeypatch.setattr(kernel_mod.os, "listdir",
                        lambda _: pytest.fail("must not scan an unverified mount"))
    with pytest.raises(OSError, match="server unavailable"):
        kernel_mod._listdir(str(tmp_path), "http://127.0.0.1:9999")


def _daemon_request(state, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}{path}?t={state['token']}",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    # above the daemon's own 20s ready-wait so a slow spawn 500s, not timeouts
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


@pytest.fixture()
def daemon_state(tmp_path):
    home = tmp_path / "home"
    env = dict(os.environ, FUSED_RENDER_HOME=str(home))
    proc = subprocess.Popen(
        [sys.executable, KERNEL, "--serve"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state_path = home / "cache" / "notebook-daemon" / "daemon.json"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not state_path.exists():
        time.sleep(0.05)
    assert state_path.exists(), "daemon did not write its state file"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    yield state
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{state['port']}/quit?t={state['token']}",
            timeout=3).read()
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def test_daemon_serializes_restart_and_execute(daemon_state, tmp_path):
    kernel = _daemon_request(daemon_state, "/kernel/ensure", {
        "nb_path": str(tmp_path / "test.ipynb"), "python": sys.executable})
    for _ in range(10):
        errors = []
        barrier = threading.Barrier(2)

        def request(path, body):
            try:
                barrier.wait()
                _daemon_request(daemon_state, path, body)
            except BaseException as exc:  # communicate thread failures to pytest
                errors.append(exc)

        threads = [
            threading.Thread(target=lambda: request(
                "/kernel/restart", {"kernel_id": kernel["kernel_id"]}),
                daemon=True),
            threading.Thread(target=lambda: request(
                "/kernel/execute", {"kernel_id": kernel["kernel_id"],
                                    "cell_id": "c1", "code": "1 + 1"}),
                daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive()
        assert not errors


def test_daemon_shutdown_is_idempotent_and_ensure_recovers(daemon_state, tmp_path):
    body = {"nb_path": str(tmp_path / "nb.ipynb"), "python": sys.executable}
    k1 = _daemon_request(daemon_state, "/kernel/ensure", body)
    _daemon_request(daemon_state, "/kernel/shutdown", {"kernel_id": k1["kernel_id"]})
    _daemon_request(daemon_state, "/kernel/shutdown", {"kernel_id": k1["kernel_id"]})
    _daemon_request(daemon_state, "/kernel/shutdown", {"kernel_id": "bogus"})
    k2 = _daemon_request(daemon_state, "/kernel/ensure", body)
    r = _daemon_request(daemon_state, "/kernel/execute", {
        "kernel_id": k2["kernel_id"], "cell_id": "c1", "code": "1 + 1"})
    assert r["exec_id"]


def test_daemon_concurrent_shutdown_and_ensure_never_orphan(daemon_state, tmp_path):
    # shutdown replaces the map entry pop with an atomic pop-then-kill; a
    # concurrent ensure that re-inserts the same kernel_id must never have
    # its fresh kernel evicted (the orphaned-subprocess race)
    body = {"nb_path": str(tmp_path / "nb.ipynb"), "python": sys.executable}
    kid = _daemon_request(daemon_state, "/kernel/ensure", body)["kernel_id"]
    for _ in range(10):
        t0 = time.monotonic()
        errors = []
        barrier = threading.Barrier(2)

        def request(path, payload):
            try:
                barrier.wait()
                _daemon_request(daemon_state, path, payload)
            except urllib.error.HTTPError:
                pass  # a raced ensure may lose its kernel and 500 — but never hang
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=lambda: request(
                "/kernel/shutdown", {"kernel_id": kid}), daemon=True),
            threading.Thread(target=lambda: request("/kernel/ensure", body),
                             daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive()
        assert not errors
        # a raced ensure must fail fast, never sit out the 20s ready-wait
        assert time.monotonic() - t0 < 15
    final = _daemon_request(daemon_state, "/kernel/ensure", body)
    r = _daemon_request(daemon_state, "/kernel/execute", {
        "kernel_id": final["kernel_id"], "cell_id": "c1", "code": "40 + 2"})
    assert r["exec_id"]


def _daemon_get(state, path_and_query):
    url = f"http://127.0.0.1:{state['port']}{path_and_query}&t={state['token']}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


@contextlib.contextmanager
def _stat_stub(payload):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _fake_venv(root):
    py = (root / ".venv" / "Scripts" / "python.exe" if os.name == "nt"
          else root / ".venv" / "bin" / "python")
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")


def _envs_query(nb, src=None):
    q = "/envs?nb_path=" + urllib.parse.quote(str(nb))
    if src is not None:
        q += "&src=" + urllib.parse.quote(src)
    return q


def test_envs_lists_local_venv(daemon_state, tmp_path):
    _fake_venv(tmp_path)
    r = _daemon_get(daemon_state, _envs_query(tmp_path / "nb.ipynb"))
    assert any(e["label"].startswith(".venv") for e in r["envs"])


def test_envs_walks_when_stat_says_local(daemon_state, tmp_path):
    _fake_venv(tmp_path)
    with _stat_stub({"remote": False, "is_dir": True}) as src:
        r = _daemon_get(daemon_state, _envs_query(tmp_path / "nb.ipynb", src))
    assert any(e["label"].startswith(".venv") for e in r["envs"])


def test_envs_skips_venv_walk_on_mount_backed_paths(daemon_state, tmp_path):
    _fake_venv(tmp_path)
    with _stat_stub({"remote": True, "is_dir": True}) as src:
        r = _daemon_get(daemon_state, _envs_query(tmp_path / "nb.ipynb", src))
    assert r["envs"] == [{"label": "App environment", "path": ""}]


def test_envs_skips_venv_walk_when_stat_unreachable(daemon_state, tmp_path):
    _fake_venv(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = _daemon_get(daemon_state,
                    _envs_query(tmp_path / "nb.ipynb", f"http://127.0.0.1:{port}"))
    assert r["envs"] == [{"label": "App environment", "path": ""}]


def test_daemon_ensure_with_missing_notebook_dir_still_starts(daemon_state, tmp_path):
    # the notebook dir may be gone (unmounted, deleted) — the kernel falls
    # back to no cwd instead of failing the spawn
    body = {"nb_path": str(tmp_path / "gone" / "nb.ipynb"),
            "python": sys.executable}
    k = _daemon_request(daemon_state, "/kernel/ensure", body)
    assert k["state"] in ("idle", "busy")
    r = _daemon_request(daemon_state, "/kernel/execute", {
        "kernel_id": k["kernel_id"], "cell_id": "c1", "code": "1 + 1"})
    assert r["exec_id"]
