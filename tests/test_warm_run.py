"""Pre-spawned idle interpreters for /api/run (SPEC PY-20, D1415).

Real subprocesses throughout: a faked Popen cannot see the lifecycle bugs this
feature is made of (pipe inheritance, orphans, a spare that is reused). Every
test turns the switch ON (conftest defaults it off) and `warm_run.reset()` runs at
teardown so no spare outlives a test.
"""
import asyncio
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from fused_render import engine, envinstall, executor, warm_run

pytestmark = pytest.mark.skipif(os.name != "posix", reason="warm run is POSIX only")

TRIVIAL = "def main(x=1):\n    return {'x': x}\n"


@pytest.fixture(autouse=True)
def _warm(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "1")
    monkeypatch.setattr(engine, "_backend", None)
    yield
    warm_run.reset()


# ---- helpers ------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(cond, timeout=10.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def ready_spares() -> list:
    with warm_run._POOL._lock:
        spares = [s for e in warm_run._POOL._entries.values() for s in e.spares]
    return [s for s in spares if s.alive() and s.is_ready(0)]


def wait_ready(n: int, timeout=10.0) -> None:
    assert wait_until(lambda: len(ready_spares()) >= n, timeout), (
        f"only {len(ready_spares())} of {n} spares became ready")


def script(tmp_path, src: str, name="t.py") -> str:
    p = tmp_path / name
    p.write_text(src)
    return str(p)


def run_b(path, params=None, timeout=30.0) -> dict:
    return executor.run_python(path, params or {}, timeout)


def run_f(path, params=None) -> dict:
    return asyncio.run(engine.run_python(path, params or {}))


def prime_builtin(tmp_path):
    assert run_b(script(tmp_path, TRIVIAL, "prime.py"))["ok"]
    wait_ready(warm_run.DEPTH)


def prime_fused(tmp_path):
    assert run_f(script(tmp_path, TRIVIAL, "prime.py"))["ok"]
    wait_ready(warm_run.DEPTH)


def children_of(pids) -> set:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    found = set()
    for line in out.splitlines():
        pid, ppid = (int(x) for x in line.split())
        if ppid in set(pids):
            found.add(pid)
    return found


def _strip(d):
    """Equality key: everything but timing, with the per-call temp dir and call-dir uuid (the
    fused handler's random names) normalised out of tracebacks."""
    import json
    import re

    text = json.dumps({k: v for k, v in d.items() if k != "duration_ms"},
                      sort_keys=True, default=repr)
    text = re.sub(r"/tmp[a-z0-9_]{8}/", "/TMP/", text)
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "UUID", text)


CASES = {
    "success": ("def main(x: int = 1):\n    return {'x': x}\n", {"x": "5"}),
    "raise": ("def main():\n    raise ValueError('boom')\n", {}),
    "exit3": ("import sys\ndef main():\n    sys.exit(3)\n", {}),
    "non-json": ("def main():\n    return object()\n", {}),
    "print-stderr": ("import sys\ndef main():\n    print('out')\n    sys.stderr.write('err\\n')\n    return 1\n", {}),
    "ctx": (
        "import os, sys\nP = __file__\nN = __name__\n"
        "def main():\n    return [os.path.basename(P), N, os.path.basename(os.getcwd())]\n", {}),
}


# ---- builtin kind ----------------------------------------------------------------

def test_builtin_claims_a_prespawned_child(tmp_path):
    prime_builtin(tmp_path)
    path = script(tmp_path, "import os\ndef main():\n    return os.getpid()\n", "pid.py")
    before = set(warm_run.spare_pids())
    assert len(before) == warm_run.DEPTH
    first = run_b(path)["result"]
    assert first in before
    wait_ready(warm_run.DEPTH)
    second = run_b(path)["result"]
    assert second != first and second not in {first}


@pytest.mark.parametrize("name", sorted(CASES))
def test_builtin_result_identical_to_cold(tmp_path, monkeypatch, name):
    src, params = CASES[name]
    path = script(tmp_path, src, "case.py")
    prime_builtin(tmp_path)
    warm = run_b(path, params)
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
    cold = run_b(path, params)
    assert _strip(warm) == _strip(cold)


def test_builtin_isolation(tmp_path):
    src = (
        "import os, sys\n"
        "STATE = {'n': 0}\n"
        "def main():\n"
        "    seen = [STATE['n'], os.environ.get('LEAK'), 'sentinel' in sys.modules,\n"
        "            os.path.samefile(sys.path[0], os.path.dirname(__file__)),\n"
        "            os.path.samefile(os.getcwd(), os.path.dirname(__file__))]\n"
        "    STATE['n'] += 1\n"
        "    os.environ['LEAK'] = 'x'\n"
        "    sys.modules['sentinel'] = sys\n"
        "    os.chdir('/')\n"
        "    sys.path.insert(0, '/nonexistent')\n"
        "    return seen\n"
    )
    path = script(tmp_path, src, "iso.py")
    prime_builtin(tmp_path)
    for _ in range(2):
        assert run_b(path)["result"] == [0, None, False, True, True]
        wait_ready(1)


def test_edit_after_spare_started_is_seen(tmp_path):
    helper = tmp_path / "helper_mod.py"
    helper.write_text("VALUE = 'old'\n")
    path = script(tmp_path, "import helper_mod\ndef main():\n    return helper_mod.VALUE\n", "e.py")
    prime_builtin(tmp_path)
    helper.write_text("VALUE = 'new'\n")
    assert run_b(path)["result"] == "new"


def test_env_change_after_spare_started_is_seen(tmp_path, monkeypatch):
    path = script(tmp_path, "import os\ndef main():\n    return os.environ.get('WR_X')\n", "env.py")
    prime_builtin(tmp_path)
    old = set(warm_run.spare_pids())
    monkeypatch.setenv("WR_X", "new")
    out = run_b(path)
    assert out["ok"] and out["result"] == "new"
    assert wait_until(lambda: not any(pid_alive(p) for p in old))


def test_timeout_kills_and_pool_recovers(tmp_path):
    path = script(tmp_path, "import os, time\ndef main():\n    time.sleep(30)\n", "slow.py")
    prime_builtin(tmp_path)
    spares = set(warm_run.spare_pids())
    out = run_b(path, timeout=1)
    assert out["ok"] is False and out["error"]["type"] == "TimeoutError"
    claimed = spares - set(warm_run.spare_pids())
    assert len(claimed) == 1
    assert wait_until(lambda: not any(pid_alive(p) for p in claimed), 2)
    assert run_b(script(tmp_path, TRIVIAL, "ok.py"))["ok"]


def test_spare_killed_externally_falls_back_cold(tmp_path):
    prime_builtin(tmp_path)
    with warm_run._POOL._lock:
        procs = [s.proc for e in warm_run._POOL._entries.values() for s in e.spares]
    killed = [p.pid for p in procs]
    for p in procs:
        p.kill()
    assert wait_until(lambda: all(p.poll() is not None for p in procs), 3)
    signal.alarm(10)
    try:
        assert run_b(script(tmp_path, TRIVIAL, "ok.py"))["ok"]
    finally:
        signal.alarm(0)
    assert not set(killed) & set(warm_run.spare_pids())


def test_no_orphans_on_shutdown_and_on_parent_close(tmp_path):
    prime_builtin(tmp_path)
    pids = list(warm_run.spare_pids())
    warm_run.shutdown()
    assert wait_until(lambda: not any(pid_alive(p) for p in pids), 2)
    # The server-death case: stdin EOF alone must end the spare.
    warm_run.reset()
    proc = warm_run._spawn([sys.executable, executor.CHILD], None)
    try:
        proc.stdin.close()
        assert proc.wait(timeout=5) is not None
    finally:
        warm_run._reap(proc)
        if proc.poll() is None:
            proc.kill()


def test_sequential_burst_never_queues(tmp_path):
    path = script(tmp_path, TRIVIAL, "b.py")
    prime_builtin(tmp_path)
    t0 = time.monotonic()
    for _ in range(20):
        assert run_b(path)["ok"]
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_b(path)["ok"]))
               for _ in range(6)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert results == [True] * 6
    assert time.monotonic() - t0 < 15
    assert wait_until(lambda: warm_run._POOL.spare_count() <= warm_run.DEPTH, 3)
    time.sleep(0.5)
    assert warm_run._POOL.spare_count() <= warm_run.DEPTH


def test_disabled_switch_never_spawns_spares(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
    assert run_b(script(tmp_path, TRIVIAL))["ok"]
    time.sleep(0.2)
    assert warm_run.spare_pids() == []


# ---- fused kind ----------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CASES))
def test_fused_warm_equals_cold(tmp_path, monkeypatch, name):
    src, params = CASES[name]
    path = script(tmp_path, src, "case.py")
    prime_fused(tmp_path)
    warm = run_f(path, params)
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
    cold = run_f(path, params)
    assert _strip(warm) == _strip(cold)


def test_fused_udf_and_dataframe_equal_cold(tmp_path, monkeypatch):
    srcs = {
        "udf": ("import fused\n@fused.udf\ndef udf(x: int = 1):\n    return {'x': x}\n", {"x": "7"}),
        "result": ("result = {'a': 1}\n", {}),
    }
    try:
        import pandas  # noqa: F401
        srcs["df"] = ("import pandas as pd\ndef main():\n    return pd.DataFrame({'a': [1, 2]})\n", {})
    except ImportError:
        pass
    prime_fused(tmp_path)
    for name, (src, params) in srcs.items():
        path = script(tmp_path, src, f"{name}.py")
        warm = run_f(path, params)
        monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
        cold = run_f(path, params)
        monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "1")
        assert _strip(warm) == _strip(cold), name
        wait_ready(1)


def test_fused_runner_is_a_fresh_prespawned_process(tmp_path):
    prime_fused(tmp_path)
    path = script(tmp_path, "import os\ndef main():\n    return [os.getpid(), os.getppid()]\n", "p.py")
    a_pids = set(warm_run.spare_pids())
    b_pids = children_of(a_pids)
    assert len(a_pids) == warm_run.DEPTH and len(b_pids) == warm_run.DEPTH
    pid1, ppid1 = run_f(path)["result"]
    assert pid1 in b_pids and ppid1 in a_pids
    wait_ready(warm_run.DEPTH)
    pid2, _ = run_f(path)["result"]
    assert pid2 != pid1


def test_fused_timeout_kills_the_whole_group(tmp_path):
    prime_fused(tmp_path)
    backend = engine.get_backend()
    backend._timeout_seconds = 2
    path = script(tmp_path, (
        "import subprocess, time\n"
        "subprocess.Popen(['sleep', '59.7'])\n"
        "def main():\n    time.sleep(60)\n"), "hang.py")
    a_pids = set(warm_run.spare_pids())
    parent_of = {c: a for a in a_pids for c in children_of([a])}
    out = run_f(path)
    assert out["ok"] is False
    assert "Execution timed out after 2s" in out["error"]["message"]
    claimed_a = a_pids - set(warm_run.spare_pids())
    assert claimed_a, "one A' must have been claimed"
    doomed = claimed_a | {b for b, a in parent_of.items() if a in claimed_a}
    assert wait_until(lambda: subprocess.run(["pgrep", "-f", "sleep 59.7"],
                                             capture_output=True).returncode != 0, 3)
    assert wait_until(lambda: not any(pid_alive(p) for p in doomed), 3)
    backend._timeout_seconds = 60
    wait_ready(1)
    assert run_f(script(tmp_path, TRIVIAL, "ok.py"))["ok"]


def test_fused_success_does_not_kill_script_daemons(tmp_path):
    prime_fused(tmp_path)
    path = script(tmp_path, (
        "import subprocess\n"
        "def main():\n"
        "    p = subprocess.Popen(['sleep', '30.3'], start_new_session=True,\n"
        "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    return p.pid\n"), "d.py")
    out = run_f(path)
    assert out["ok"], out
    pid = out["result"]
    try:
        time.sleep(0.3)
        assert pid_alive(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_fused_site_stamp_discards_spare(tmp_path):
    prime_fused(tmp_path)
    site = warm_run._site_dirs(engine.app_interpreter())
    assert site, "dev venv should expose a site-packages dir"
    old = set(warm_run.spare_pids())
    pth = os.path.join(site[0], "zz_warm_run_probe.pth")
    open(pth, "w").close()
    os.remove(pth)
    out = run_f(script(tmp_path, TRIVIAL, "ok.py"))
    assert out["ok"]
    assert wait_until(lambda: not any(pid_alive(p) for p in old), 3)


def test_discard_all_hooks(tmp_path):
    prime_builtin(tmp_path)
    assert warm_run.spare_pids()
    engine.reset_app_interpreter_cache()
    assert wait_until(lambda: not warm_run.spare_pids(), 3)
    prime_builtin(tmp_path)
    envinstall.reset_venv_validation_cache()
    assert wait_until(lambda: not warm_run.spare_pids(), 3)
    prime_builtin(tmp_path)
    engine.reset_app_packages_cache()
    assert wait_until(lambda: not warm_run.spare_pids(), 3)


# ---- interpreter start-up settings must match the cold path ----------------------

@pytest.mark.parametrize("var,value", [("PYTHONPATH", "/wr-extra-path"), ("PYTHONSAFEPATH", "1")])
def test_fused_startup_env_difference_falls_back_to_cold(tmp_path, monkeypatch, var, value):
    """The handler hands the runner an env that differs from the spare's in a
    setting Python reads at start-up: the warm B' (already booted) cannot honour
    it, so the call must take the cold runner and see the same sys.path."""
    from fused_render.compute import backend as python_compute
    monkeypatch.setattr(
        python_compute, "_RUN_SCRIPT",
        f"import os\nos.environ[{var!r}] = {value!r}\n" + python_compute._RUN_SCRIPT)
    path = script(tmp_path, "import sys\ndef main():\n    return [p for p in sys.path if 'wr-extra' in p] + [len(sys.path)]\n", "sp.py")
    prime_fused(tmp_path)
    warm = run_f(path)
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
    cold = run_f(path)
    assert warm["ok"] and cold["ok"], (warm, cold)
    assert warm["result"] == cold["result"]


def test_builtin_startup_env_change_matches_cold(tmp_path, monkeypatch):
    path = script(tmp_path, "import sys\ndef main():\n    return [p for p in sys.path if 'wr-extra' in p]\n", "sp.py")
    prime_builtin(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/wr-extra-path")
    warm = run_b(path)
    monkeypatch.setenv("FUSED_RENDER_WARM_RUN", "0")
    cold = run_b(path)
    assert warm["ok"] and warm["result"] == cold["result"] == ["/wr-extra-path"]
