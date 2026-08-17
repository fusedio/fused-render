"""Canvases sub-app backend (fused_render/canvases.py).

Same harness idea as test_account.py: the fused CLI is a stub script wired in
through FUSED_RENDER_FUSED_BIN, its behavior driven by a scenario file, its
invocations logged — so the tests exercise the real subprocess seam without a
network or a fused install. The `fused login` credential store is pointed at a
tmp file via FUSED_RENDER_FUSED_CREDENTIALS.
"""
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
from fused_render.server import create_app

GUARD = {"X-Fused": "1"}

# The stub speaks just enough of the fused CLI for canvases.py: whoami, canvas
# list/pull/push. Scenario file keys: fail (nonzero exit + stderr), whoami,
# canvases. Every call is appended to FUSED_STUB_LOG as one JSON line.
STUB = r"""
import json, os, sys

def main():
    args = sys.argv[1:]
    with open(os.environ["FUSED_STUB_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
    scenario = {}
    path = os.environ.get("FUSED_STUB_SCENARIO")
    if path and os.path.exists(path):
        with open(path) as f:
            scenario = json.load(f)
    # The legacy SDK CLI is nested under `fused workbench` (fused >= 2.x
    # agent-toolkit layout); the stub strips the nesting like option tokens.
    plain = [a for a in args if a not in ("workbench", "--format", "json")]
    if scenario.get("fail"):
        sys.stderr.write("Error: " + scenario["fail"] + "\n")
        sys.exit(1)
    if plain[:1] == ["whoami"]:
        json.dump(scenario.get("whoami", {"handle": "tester"}), sys.stdout)
        return
    if plain[:2] == ["canvas", "list"]:
        json.dump(scenario.get("canvases", ["alpha", "beta"]), sys.stdout)
        return
    if plain[:2] == ["canvas", "pull"]:
        out = plain[plain.index("-o") + 1]
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "canvas.toml"), "w") as f:
            f.write('type = "canvas"\n')
        return
    if plain[:2] == ["canvas", "push"]:
        return
    sys.stderr.write("Error: unknown stub command\n")
    sys.exit(2)

main()
"""


@pytest.fixture(autouse=True)
def _reset_module_state():
    yield
    login = canvases_mod._active_login
    if login is not None and login.proc.poll() is None:
        login.proc.kill()
        login.proc.wait()
    canvases_mod._active_login = None
    with canvases_mod._SYNC_LOCK:
        managers = list(canvases_mod._syncs.values())
        canvases_mod._syncs.clear()
    for manager in managers:
        manager.stop()


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        stub = tmp_path / "fused_stub.py"
        stub.write_text(STUB, encoding="utf-8")
        monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", f"{sys.executable} {stub}")

        self.creds = tmp_path / "fused-credentials.json"
        monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(self.creds))
        self.root = tmp_path / "canvases"
        monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(self.root))

        self.log = tmp_path / "stub-log.jsonl"
        monkeypatch.setenv("FUSED_STUB_LOG", str(self.log))
        self.scenario_file = tmp_path / "scenario.json"
        self.set_scenario({})
        monkeypatch.setenv("FUSED_STUB_SCENARIO", str(self.scenario_file))

        self.client = TestClient(create_app(start_dir=str(tmp_path)))

    def set_scenario(self, scenario: dict) -> None:
        self.scenario_file.write_text(json.dumps(scenario), encoding="utf-8")

    def log_in(self) -> None:
        self.creds.write_text(json.dumps({"access_token": "tok123"}), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_status_reports_signed_out(harness):
    res = harness.client.get("/api/canvases/status")
    assert res.status_code == 200
    body = res.json()
    assert body["cli_found"] is True
    assert body["logged_in"] is False
    assert body["login_in_flight"] is False
    assert body["workbench_base_url"]


def test_guarded_endpoints_403_without_header(harness):
    harness.log_in()
    assert harness.client.get("/api/canvases/list").status_code == 403
    assert harness.client.get("/api/canvases/whoami").status_code == 403
    assert harness.client.get("/api/canvases/token").status_code == 403
    assert harness.client.post("/api/canvases/clone", json={"name": "alpha"}).status_code == 403


def test_list_requires_login(harness):
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 409


def test_list_normalizes_names_and_marks_cloned(harness):
    harness.log_in()
    (harness.root / "beta").mkdir(parents=True)
    (harness.root / "beta" / "canvas.toml").write_text("", encoding="utf-8")
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 200
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["cloned"] is False
    assert canvases["beta"]["cloned"] is True


def test_stale_credentials_map_to_401(harness):
    # A present-but-unrefreshable store: the CLI dies with its own
    # re-authenticate message, and the client needs a 401 to fall back to the
    # sign-in flow instead of a dead 502.
    harness.log_in()
    harness.set_scenario(
        {"fail": "Auth0 refused to refresh your Fused credentials. Re-authenticate by running `fused login`."}
    )
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 401
    assert "Re-authenticate" in res.json()["error"]


def test_whoami_extracts_handle(harness):
    harness.set_scenario({"whoami": {"username": "vasu"}})
    res = harness.client.get("/api/canvases/whoami", headers=GUARD)
    assert res.status_code == 200
    assert res.json()["handle"] == "vasu"


def test_clone_rejects_bad_names(harness):
    harness.log_in()
    for bad in ("../x", "a b", "a-b", ""):
        res = harness.client.post("/api/canvases/clone", json={"name": bad}, headers=GUARD)
        assert res.status_code == 400, bad


def test_clone_pulls_into_canvases_dir(harness):
    harness.log_in()
    res = harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200
    assert os.path.isfile(harness.root / "alpha" / "canvas.toml")
    pulls = [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "pull"]]
    assert pulls and "--force" in pulls[0]


def test_clone_cli_failure_surfaces_message(harness):
    harness.log_in()
    harness.set_scenario({"fail": "no such canvas"})
    res = harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 502
    assert "no such canvas" in res.json()["error"]


def test_sync_start_requires_a_clone(harness):
    harness.log_in()
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409


def test_sync_watches_debounces_and_pushes(harness, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200
    assert res.json()["watching"] is True

    # The clone's own files are the baseline — no push without a change.
    time.sleep(0.4)
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]

    (harness.root / "alpha" / "udf.py").write_text("print('hi')\n", encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status["push_seq"] >= 1, status
    pushes = [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    assert pushes and pushes[0][-2:] == ["--canvas", "alpha"]

    stop = harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)
    assert stop.json()["stopped"] is True


def test_sync_push_failure_reports_error(harness, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    harness.set_scenario({"fail": "push exploded"})
    (harness.root / "alpha" / "udf.py").write_text("x = 1\n", encoding="utf-8")
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_state"] == "error":
            break
        time.sleep(0.05)
    assert status and status["push_state"] == "error"
    assert "push exploded" in status["error"]


def test_token_external_cli_reads_store(harness):
    # FUSED_RENDER_FUSED_BIN is an external override in this harness, so the
    # token endpoint falls back to the raw on-disk store.
    harness.log_in()
    res = harness.client.get("/api/canvases/token", headers=GUARD)
    assert res.status_code == 200
    assert res.json()["access_token"] == "tok123"


def test_token_requires_login(harness):
    res = harness.client.get("/api/canvases/token", headers=GUARD)
    assert res.status_code == 409
