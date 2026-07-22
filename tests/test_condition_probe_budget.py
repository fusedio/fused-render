"""Per-gate probe budget backstop (server._mount_gate_builtins / _run_condition
and the /api/fs/conditions endpoint).

operations/stat has NO S3 point lookup: on a non-direct-capable mount each probe
makes rclone list the whole parent prefix, burning the full rc timeout. A
condition gate runs its probes SERIALLY, so three of them would stack to ~3x the
timeout (20-30s on source.coop). One gate evaluation shares a single wall-clock
deadline (GATE_PROBE_BUDGET_S): each probe is bounded to the budget remaining and
once it is spent the rest fail closed instantly (SPEC CT-12), so the gate — and
the endpoint — completes within the budget rather than stacking timeouts.

These drive the REAL rc path against a hanging stub rcd (the socket-timeout
cutoff is the mechanism under test, so it must be exercised for real), mirroring
the CHECK_BUDGET_S pattern in tests/test_shell_recents.py. Real rclone is never
invoked; FUSED_RENDER_HOME is redirected per test.
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.server as server
import fused_render.shell.mounts as mounts_mod
from fused_render.server import create_app
from test_shell_mounts import StubRcd


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    # A mount is added BEFORE create_app; skip the automount startup thread so it
    # can't outlive the test and corrupt the next test's home.
    monkeypatch.setattr(mounts_mod, "startup", lambda: None)
    return h


@pytest.fixture()
def rcd(home):
    stub = StubRcd()
    mounts_mod.write_rcd_state(stub.port, 4242)
    yield stub
    stub.close()


@pytest.fixture(autouse=True)
def fresh_cfg():
    # _remote_config memoizes per remote in a module global; clear so a
    # credentialed config from one test doesn't leak into the next.
    mounts_mod._upstream_cfg.clear()
    yield
    mounts_mod._upstream_cfg.clear()


def _hanging_noncapable_mount(rcd, monkeypatch, budget=1.0):
    """A mount whose stat backend HANGS: credentialed S3 (not direct-capable, so
    the rc route is taken) with operations/stat delayed far past the budget."""
    monkeypatch.setattr(server, "GATE_PROBE_BUDGET_S", budget)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    rcd.delay["operations/stat"] = 10.0  # every rc probe would burn 10s
    c = mounts_mod.add_mount("corp", "corp:bucket")
    return os.path.join(mounts_mod.mountpoint(c), "store")


def test_gate_probe_budget_caps_serialized_hanging_probes(home, rcd, tmp_path, monkeypatch):
    store = _hanging_noncapable_mount(rcd, monkeypatch, budget=1.0)
    gate = tmp_path / "condition.py"
    gate.write_text(
        "import os\n"
        "def main(path):\n"
        "    a = os.path.isfile(path + '/a')\n"
        "    b = os.path.isfile(path + '/b')\n"
        "    c = os.path.isfile(path + '/c')\n"
        "    return a or b or c\n")

    start = time.monotonic()
    allowed, err = server._run_condition(str(gate), store)
    elapsed = time.monotonic() - start

    # Fail closed, and within roughly one budget — NOT 3 * 10s of stacked probes.
    assert allowed is False and err is None
    assert elapsed < 3.0, f"gate took {elapsed:.1f}s — budget not enforced"


def test_fs_conditions_bounded_when_stat_backend_stalls(home, rcd, tmp_path, monkeypatch):
    store = _hanging_noncapable_mount(rcd, monkeypatch, budget=1.0)

    client = TestClient(create_app(start_dir="/"))
    start = time.monotonic()
    r = client.get("/api/fs/conditions", params={"path": store})
    elapsed = time.monotonic() - start

    # Fail-closed 200 (never a spurious 404 or a hang): the endpoint dir probe
    # and every gate are budget-bounded, so all conditions resolve False fast.
    assert r.status_code == 200
    assert all(v is False for v in r.json()["conditions"].values())
    assert elapsed < 6.0, f"/api/fs/conditions took {elapsed:.1f}s — not bounded"
