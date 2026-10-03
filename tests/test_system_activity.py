"""`GET /api/system/activity` (both scopes), its per-pid history,
`POST /api/system/activity/stop` / `.../kill` and the sampler behind them (fused_render/sysmon). A fake backend stands in for the
platform readers, so nothing here touches real processes."""
import os
import signal
import sys

import pytest
from fastapi.testclient import TestClient

from fused_render.ai import supervisor
from fused_render.server import create_app, engine_host
from fused_render.sysmon import labels, sampler as sysmon_sampler
from fused_render.sysmon.common import ProcIdent, ProcSample

HDRS = {"X-Fused": "1"}
ROOT = 100


class FakeBackend:
    """pid tree: ROOT -> 201 (engine), 202 (model worker), 203 (unknown
    python) -> 204 (its child); 900 is outside the tree. The whole machine
    adds pid 1 and 950, both another user's: listed, but unreadable."""

    def __init__(self):
        self.tree = {201: ROOT, 202: ROOT, 203: ROOT, 204: 203}
        self.cpu_ns = {p: 0 for p in (ROOT, 201, 202, 203, 204, 900)}
        self.mem = {ROOT: 500, 201: 100, 202: 2000, 203: 30, 204: 10, 900: 1}
        self.args = {203: ["/usr/bin/python3", "-m", "some.tool"], 204: ["/bin/sleep", "5"]}
        self.ticks = (0, 0, 0)
        self.pstart: dict[int, int] = {}   # kernel start stamp; 1 unless a test recycles a pid
        self.uid = os.getuid()
        self.idents = {p: ProcIdent(p, self.tree.get(p, 1), self.uid, 1_700_000_000.0 + p,
                                    f"proc{p}", False) for p in self.cpu_ns}
        self.idents[1] = ProcIdent(1, 0, 0, 1_600_000_000.0, "launchd", False)
        self.idents[950] = ProcIdent(950, 1, 0, 1_700_000_950.0, "coreduetd", False)

    def list_procs(self):
        return list(self.idents.values())

    def proc_ident(self, pid):
        return self.idents.get(pid)

    def descendants(self, root):
        return dict(self.tree) if root == ROOT else {}

    def parent_pid(self, pid):
        return self.tree.get(pid, 1)

    def is_zombie(self, pid):
        return False

    def proc_sample(self, pid):
        if pid not in self.cpu_ns:
            return None
        return ProcSample(pid, self.cpu_ns[pid], self.mem[pid], self.mem[pid],
                          self.pstart.get(pid, 1))

    def proc_args(self, pid, limit=None):
        return self.args.get(pid)

    def start_time(self, pid):
        # proc_pidinfo, like the real one: refused for another user's pid.
        ident = self.idents.get(pid)
        return ident.start if ident and ident.uid == self.uid else None

    def host_memory(self):
        return {"total": 16 << 30, "used": 10 << 30, "app": 6 << 30,
                "wired": 2 << 30, "compressed": 2 << 30}

    def host_cpu_ticks(self):
        return self.ticks


def at(pid, **extra):
    """A /stop or /kill body naming `pid` the way the page does: with the
    start stamp FakeBackend gives it."""
    return {"pid": pid, "startedAt": 1_700_000_000.0 + pid, **extra}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _child(engine_id, **over):
    fields = dict(engine_id=engine_id, python=sys.executable, daemon="/tmp/daemon.py",
                  cache="c", version="v1", pid=201)
    fields.update(over)
    return engine_host.Child(**fields)


@pytest.fixture()
def registries(monkeypatch):
    monkeypatch.setattr(engine_host, "_children",
                        {"bg_abc": _child("bg_abc", folder="/w/My Widget")})
    worker = supervisor.Worker(model="org/qwen2.5-7b", capability="text-generation",
                               runner_code="llama", token="t", state="ready")
    worker.pid = 202
    monkeypatch.setattr(supervisor, "_workers", {"text-generation": worker})


@pytest.fixture()
def fake(monkeypatch, registries):
    backend = FakeBackend()
    clock = Clock()
    s = sysmon_sampler.Sampler(backend, root_pid=ROOT, roots=lambda: {},
                               clock=clock, wall=clock, threaded=False)
    monkeypatch.setattr(sysmon_sampler, "_SAMPLER", s)
    return backend, clock, s


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _second_sample(backend, clock, s):
    clock.t += 1.0
    backend.cpu_ns[202] += 500_000_000      # half a core
    backend.cpu_ns[ROOT] += 100_000_000
    backend.ticks = (30, 10, 100)
    s.tick()


def test_get_is_unguarded_and_has_the_documented_shape(client, fake):
    backend, clock, s = fake
    body = client.get("/api/system/activity").json()
    assert body["supported"] is True
    assert set(body) == {"supported", "host", "procs", "history", "totals"}
    assert set(body["host"]) == {"cpuPct", "cpuUser", "cpuSystem", "ncpu", "load",
                                 "memTotal", "memUsed", "memApp", "memWired",
                                 "memCompressed", "windowS"}
    assert body["host"]["windowS"] == sysmon_sampler.WINDOW_S
    row = body["procs"][0]
    assert set(row) == {"pid", "ppid", "kind", "label", "cpuPct", "memBytes", "startedAt"}
    # First poll after idle: one sample only, so CPU is not known yet.
    assert all(r["cpuPct"] is None for r in body["procs"])
    assert body["host"]["cpuPct"] is None
    assert body["totals"] == {"cpuPct": None, "memBytes": 2640}

    _second_sample(backend, clock, s)
    body = client.get("/api/system/activity").json()
    by_pid = {r["pid"]: r for r in body["procs"]}
    assert by_pid[202]["cpuPct"] == 50.0
    assert by_pid[ROOT]["cpuPct"] == 10.0
    assert body["totals"]["cpuPct"] == 60.0
    assert body["host"]["cpuPct"] == 40.0
    assert body["procs"][0]["pid"] == 202  # sorted by CPU
    assert body["history"][-1]["appCpuPct"] == 60.0


def test_labels_join_engine_and_model_registries(client, fake):
    procs = {r["pid"]: r for r in client.get("/api/system/activity").json()["procs"]}
    assert (procs[ROOT]["kind"], procs[ROOT]["label"]) == ("app", "fused-render")
    assert (procs[201]["kind"], procs[201]["label"]) == ("engine", "App: My Widget")
    assert (procs[202]["kind"], procs[202]["label"]) == ("model", "Model: qwen2.5-7b")
    assert (procs[203]["kind"], procs[203]["label"]) == ("other", "some.tool")
    # Unclaimed child of an unclaimed process stays `other`, named by command.
    assert (procs[204]["kind"], procs[204]["label"]) == ("other", "sleep")


def test_a_child_of_a_labelled_process_inherits_its_kind(fake):
    backend, clock, s = fake
    backend.tree[205] = 202
    backend.cpu_ns[205] = 0
    backend.mem[205] = 5
    backend.args[205] = ["/bin/llama-server"]
    s.poll()
    row = next(r for r in s.payload()["procs"] if r["pid"] == 205)
    assert row["kind"] == "model"
    assert row["label"] == "Model: qwen2.5-7b › llama-server"


def test_claude_roots_outside_the_tree_are_sampled(monkeypatch, registries):
    backend = FakeBackend()
    owner = labels.Owner("claude", "Claude: fix the bug", {"run_id": "r1"})
    clock = Clock()
    s = sysmon_sampler.Sampler(backend, root_pid=ROOT, roots=lambda: {900: owner},
                               clock=clock, wall=clock, threaded=False)
    s.poll()
    row = next(r for r in s.payload()["procs"] if r["pid"] == 900)
    assert (row["kind"], row["label"]) == ("claude", "Claude: fix the bug")


def test_command_name_reads_scripts_and_modules():
    assert labels.command_name(["/x/python3.12", "-m", "fused_render.index.worker"]) == \
        "fused-render index worker"
    assert labels.command_name(["/x/python", "/a/b/serve.py", "--port", "1"]) == "serve.py"
    assert labels.command_name(["/bin/zsh", "-l"]) == "zsh"
    assert labels.command_name(None) == "process"
    assert labels.classify(1, ["/x/python", "/pkg/fused_render/_child.py"]).kind == "run"


def test_run_title_is_first_line_trimmed():
    assert labels.run_title({"message": "\n  fix   the\tbug \nmore"}) == "fix the bug"
    assert len(labels.run_title({"message": "x" * 200})) == 48
    assert labels.run_title({}) == ""


def test_stop_requires_x_fused(client, fake):
    client.get("/api/system/activity")
    r = client.post("/api/system/activity/stop", json=at(203))
    assert r.status_code == 403


def test_stop_refuses_the_app_itself(client, fake):
    client.get("/api/system/activity")
    r = client.post("/api/system/activity/stop", json=at(ROOT), headers=HDRS)
    assert r.status_code == 400
    assert "itself" in r.json()["error"]


def test_stop_refuses_a_foreign_pid(client, fake, monkeypatch):
    killed = []
    monkeypatch.setattr(sysmon_sampler, "_kill", lambda pid, sig: killed.append(pid))
    client.get("/api/system/activity")
    r = client.post("/api/system/activity/stop", json=at(900), headers=HDRS)
    assert r.status_code == 404
    assert killed == []


def test_stop_routes_through_the_owner(client, fake, monkeypatch):
    stopped, unloaded, killed = [], [], []
    monkeypatch.setattr(engine_host, "stop", lambda eid: stopped.append(eid))
    monkeypatch.setattr(supervisor, "unload",
                        lambda **kw: unloaded.append(kw["model"]) or True)
    monkeypatch.setattr(sysmon_sampler, "_kill", lambda pid, sig: killed.append(pid))
    client.get("/api/system/activity")

    assert client.post("/api/system/activity/stop", json=at(201),
                       headers=HDRS).json() == {"ok": True, "via": "engine"}
    assert client.post("/api/system/activity/stop", json=at(202),
                       headers=HDRS).json() == {"ok": True, "via": "model"}
    assert client.post("/api/system/activity/stop", json=at(203),
                       headers=HDRS).json() == {"ok": True, "via": "signal"}
    assert (stopped, unloaded, killed) == (["bg_abc"], ["org/qwen2.5-7b"], [203])


def test_stop_rejects_a_bad_pid(client, fake):
    r = client.post("/api/system/activity/stop", json={"pid": "nope"}, headers=HDRS)
    assert r.status_code == 400


def test_sampler_goes_idle_after_15s_without_a_poll(fake):
    backend, clock, s = fake
    s.poll()
    assert s.running
    clock.t += 14.0
    assert s.tick() is True
    clock.t += 1.5
    assert s.tick() is False
    assert not s.running
    # The next poll restarts it with CPU unknown again (one fresh sample).
    s.poll()
    assert s.running
    assert all(r["cpuPct"] is None for r in s.payload()["procs"])


def test_unsupported_platform_reports_supported_false(monkeypatch, client):
    monkeypatch.setattr(sysmon_sampler, "_SAMPLER", sysmon_sampler.Sampler(backend=None))
    s = sysmon_sampler._SAMPLER
    s.backend = None
    body = client.get("/api/system/activity").json()
    assert body["supported"] is False
    assert body["procs"] == []


# ---- the whole machine (?scope=all) --------------------------------------------

ALL_ROW_KEYS = {"pid", "ppid", "user", "command", "args", "cpuPct", "memBytes",
                "startedAt", "fused", "kind", "label"}


def test_scope_all_lists_every_process_with_identity(client, fake):
    backend, clock, s = fake
    body = client.get("/api/system/activity?scope=all").json()
    assert set(body) == {"supported", "host", "procs", "history", "totals"}
    rows = {r["pid"]: r for r in body["procs"]}
    assert set(rows) == {1, ROOT, 201, 202, 203, 204, 900, 950}
    assert all(set(r) == ALL_ROW_KEYS for r in rows.values())
    # Ours keep the labeller's kind and label; everything else is "system".
    assert (rows[ROOT]["fused"], rows[ROOT]["kind"]) == (True, "app")
    assert (rows[202]["kind"], rows[202]["label"]) == ("model", "Model: qwen2.5-7b")
    assert (rows[900]["fused"], rows[900]["kind"]) == (False, "system")
    assert rows[204]["command"] == "sleep" and rows[204]["args"] == "/bin/sleep 5"
    # No argv readable: the kernel's short name stands in.
    assert rows[900]["command"] == rows[900]["label"] == rows[900]["args"] == "proc900"
    # Another user's pid: listed with its user, figures null, row kept.
    assert rows[950]["user"] == "root"
    assert (rows[950]["cpuPct"], rows[950]["memBytes"]) == (None, None)
    # The default scope is untouched by the whole-machine one.
    fused = client.get("/api/system/activity").json()["procs"]
    assert {r["pid"] for r in fused} == {ROOT, 201, 202, 203, 204}
    assert set(fused[0]) == {"pid", "ppid", "kind", "label", "cpuPct", "memBytes", "startedAt"}


def test_scope_all_measures_cpu_and_sorts_nulls_last(client, fake):
    backend, clock, s = fake
    client.get("/api/system/activity?scope=all")
    clock.t += 1.0
    backend.cpu_ns[900] += 250_000_000
    s.tick()
    procs = client.get("/api/system/activity?scope=all").json()["procs"]
    assert procs[0]["pid"] == 900 and procs[0]["cpuPct"] == 25.0
    assert {p["pid"] for p in procs if p["cpuPct"] is None} == {1, 950}
    assert [p["cpuPct"] for p in procs[-2:]] == [None, None]


def test_history_rings_fill_and_drop_exited_pids(client, fake):
    backend, clock, s = fake
    client.get("/api/system/activity?scope=all")
    for _ in range(sysmon_sampler.RING_POINTS + 5):
        clock.t += 1.0
        s.tick()
        s._all_last_poll = clock.t  # still being read
        s._last_poll = clock.t
    hist = client.get("/api/system/activity/history?pids=202,950,4242").json()
    assert set(hist) == {"202", "950", "4242"}
    assert len(hist["202"]) == sysmon_sampler.RING_POINTS
    assert set(hist["202"][-1]) == {"t", "cpuPct", "memBytes"}
    assert hist["202"][-1]["memBytes"] == 2000
    assert hist["950"] == [] and hist["4242"] == []   # unreadable / unknown
    # A pid that exits loses its ring on the next sample.
    del backend.idents[204]
    clock.t += 1.0
    s.tick()
    assert client.get("/api/system/activity/history?pids=204").json() == {"204": []}
    assert client.get("/api/system/activity/history?pids=x").status_code == 400


def test_whole_machine_state_is_dropped_15s_after_the_last_all_poll(fake):
    backend, clock, s = fake
    s.poll("all")
    assert s.stamp(950) is not None
    for _ in range(16):
        clock.t += 1.0
        s.poll()        # the status bar keeps reading the default scope
        s.tick()
    assert s.stamp(950) is None
    assert s.proc_history([202]) == {"202": []}
    assert {r["pid"] for r in s.payload("all")["procs"]} == {ROOT, 201, 202, 203, 204}


@pytest.fixture()
def killed(monkeypatch):
    sent = []
    monkeypatch.setattr(sysmon_sampler, "_kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_kill_requires_x_fused(client, fake, killed):
    client.get("/api/system/activity?scope=all")
    r = client.post("/api/system/activity/kill", json=at(900))
    assert r.status_code == 403
    assert killed == []


def test_kill_signals_term_then_kill_when_forced(client, fake, killed):
    client.get("/api/system/activity?scope=all")
    assert client.post("/api/system/activity/kill", json=at(900),
                       headers=HDRS).json() == {"ok": True, "signal": "SIGTERM"}
    assert client.post("/api/system/activity/kill", json=at(900, force=True),
                       headers=HDRS).json() == {"ok": True, "signal": "SIGKILL"}
    assert killed == [(900, signal.SIGTERM), (900, signal.SIGKILL)]


@pytest.mark.parametrize("pid", [ROOT, 1, 0, -5])
def test_kill_refuses_the_app_and_system_pids(client, fake, killed, pid):
    client.get("/api/system/activity?scope=all")
    r = client.post("/api/system/activity/kill", json=at(pid), headers=HDRS)
    assert r.status_code == 400
    assert killed == []


def test_kill_refuses_a_recycled_or_unlisted_pid(client, fake, killed):
    backend, clock, s = fake
    client.get("/api/system/activity?scope=all")
    old = backend.idents[900]
    backend.idents[900] = old._replace(start=old.start + 5)   # same pid, new process
    r = client.post("/api/system/activity/kill", json=at(900), headers=HDRS)
    assert r.status_code == 404
    r = client.post("/api/system/activity/kill", json=at(4242), headers=HDRS)
    assert r.status_code == 404
    assert killed == []


def test_kill_permission_denied_is_an_answer_not_an_error(client, fake, monkeypatch):
    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(sysmon_sampler, "_kill", deny)
    client.get("/api/system/activity?scope=all")
    r = client.post("/api/system/activity/kill", json=at(950), headers=HDRS)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "permission denied"}


# ---- one loop, 5 s window -------------------------------------------------------

def test_a_first_all_poll_does_not_start_a_second_loop(monkeypatch, registries):
    """The measured bug: polling fused, then all, started two loop threads
    that shared the "previous" readings, so every delta spanned milliseconds."""
    import threading
    import time as _time

    backend = FakeBackend()
    s = sysmon_sampler.Sampler(backend, root_pid=ROOT, roots=lambda: {},
                               interval=0.05, idle_after=0.2)
    loops = lambda: [t for t in threading.enumerate()
                     if t.name == "sysmon-sampler" and t.is_alive()]
    s.poll()
    s.poll("all")
    s.poll("all")
    _time.sleep(0.05)
    assert len(loops()) == 1
    deadline = _time.monotonic() + 2
    while loops() and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert loops() == []          # idles out on its own
    s.poll("all")                 # and a cold poll starts exactly one again
    assert len(loops()) == 1


def test_history_points_are_one_tick_apart_however_the_scopes_wake(fake):
    backend, clock, s = fake
    s.poll()
    for i in range(8):
        clock.t += 1.0
        backend.cpu_ns[202] += 500_000_000
        backend.ticks = tuple(x + d for x, d in zip(backend.ticks, (3, 1, 10)))
        if i == 3:
            clock.t += 0.004
            s.poll("all")         # wakes the whole machine mid-stride
        s.tick()
    ts = [h["t"] for h in s.payload()["history"]]
    assert len(ts) == len(set(ts))
    steps = [round(b - a, 3) for a, b in zip(ts, ts[1:])]
    assert all(abs(d - 1.0) < 0.01 for d in steps), steps
    # Host CPU is never the 0% a zero-width window used to give.
    assert all(h["cpuPct"] == 40.0 for h in s.payload()["history"])


def test_cpu_is_averaged_over_the_5s_window(fake):
    backend, clock, s = fake
    s.poll()
    readings = []
    for i in range(8):
        clock.t += 1.0
        if i == 0:
            backend.cpu_ns[202] += 1_000_000_000      # one busy second…
        s.tick()                                      # …then idle
        readings.append(next(r for r in s.payload()["procs"] if r["pid"] == 202)["cpuPct"])
    # 100% over 1 s, then that second averaged over 2..5 s, then out of the window.
    assert readings == [100.0, 50.0, 33.3, 25.0, 20.0, 0.0, 0.0, 0.0]


def test_an_inline_all_sample_adds_no_ring_point(client, fake):
    backend, clock, s = fake
    s.poll()
    clock.t += 1.0
    s.tick()
    s.poll("all")                 # inline, record=False
    assert s.proc_history([202]) == {"202": []}
    clock.t += 1.0
    s.tick()
    assert len(s.proc_history([202])["202"]) == 1


# ---- (pid, startedAt) names a process; review follow-ups ----------------------------

def test_kill_and_stop_refuse_a_pid_that_became_another_process(client, fake, killed):
    client.get("/api/system/activity?scope=all")
    stale = {"pid": 900, "startedAt": 1_700_000_000.0 + 900 - 30}
    r = client.post("/api/system/activity/kill", json=stale, headers=HDRS)
    assert (r.status_code, r.json()["error"]) == (404, "process changed")
    stale = {"pid": 203, "startedAt": 1_700_000_000.0 + 203 - 30}
    r = client.post("/api/system/activity/stop", json=stale, headers=HDRS)
    assert (r.status_code, r.json()["error"]) == (404, "process changed")
    assert killed == []


def test_kill_and_stop_require_started_at(client, fake, killed):
    client.get("/api/system/activity?scope=all")
    for path in ("kill", "stop"):
        r = client.post(f"/api/system/activity/{path}", json={"pid": 203}, headers=HDRS)
        assert r.status_code == 400
    assert killed == []


def test_stop_refuses_a_recycled_pid(client, fake, killed):
    backend, clock, s = fake
    client.get("/api/system/activity")
    backend.pstart[203] = 99          # the kernel's own stamp moved: a new process
    r = client.post("/api/system/activity/stop", json=at(203), headers=HDRS)
    assert r.status_code == 404
    assert killed == []


def test_a_process_gone_before_the_signal_is_404(client, fake, monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError(3, "No such process")
    monkeypatch.setattr(sysmon_sampler, "_kill", gone)
    client.get("/api/system/activity?scope=all")
    r = client.post("/api/system/activity/kill", json=at(900), headers=HDRS)
    assert r.status_code == 404


def test_force_must_be_literally_true(client, fake, killed):
    client.get("/api/system/activity?scope=all")
    client.post("/api/system/activity/kill", json=at(900, force="yes"), headers=HDRS)
    assert killed == [(900, signal.SIGTERM)]


def test_command_line_secrets_are_redacted(client, fake):
    backend, clock, s = fake
    backend.args[900] = ["/usr/bin/node", "srv.js", "--token", "abc123", "--port", "8",
                         "https://x/?api_key=SEKRIT&q=1"]
    row = next(r for r in client.get("/api/system/activity?scope=all").json()["procs"]
               if r["pid"] == 900)
    assert row["args"] == "/usr/bin/node srv.js --token ••• --port 8 https://x/?api_key=•••&q=1"
    assert "abc123" not in row["args"] and "SEKRIT" not in row["args"]


def test_a_claude_run_pid_file_naming_a_recycled_pid_is_not_claude(tmp_path):
    import os as _os

    class B:
        def __init__(self, args, started):
            self._args, self._started = args, started

        def proc_args(self, pid):
            return self._args

        def start_time(self, pid):
            return self._started

    (tmp_path / "pid").write_text("4242")
    written = _os.path.getmtime(tmp_path / "pid")
    run = str(tmp_path)
    assert labels._is_run_process(B(["/u/.local/bin/claude", "--session-id", "x"], written - 10), 4242, run)
    assert labels._is_run_process(B(["/py", "-m", "fused_render.session_host"], written - 10), 4242, run)
    # Same pid, but now some other program: not Claude.
    assert not labels._is_run_process(B(["/bin/zsh", "-l"], written - 10), 4242, run)
    # A claude that started after the pid file was written reused the pid.
    assert not labels._is_run_process(B(["/u/.local/bin/claude"], written + 60), 4242, run)
