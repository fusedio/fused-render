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
        if "--dry-run" in plain:
            # canvas_pull.py prints this sentinel when local == remote;
            # anything else means the pull would change files.
            sys.stdout.write(
                scenario.get(
                    "pull_dry",
                    "Nothing to do: already up to date (local files match the canvas).",
                )
            )
            return
        out = plain[plain.index("-o") + 1]
        os.makedirs(out, exist_ok=True)
        files = scenario.get("pull_files", {"canvas.toml": 'type = "canvas"\n'})
        for rel, content in files.items():
            with open(os.path.join(out, rel), "w") as f:
                f.write(content)
        return
    if plain[:2] == ["canvas", "push"]:
        return
    if plain[:2] == ["canvas", "create"]:
        return
    if plain[:1] == ["logout"]:
        os.remove(os.environ["FUSED_RENDER_FUSED_CREDENTIALS"])
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


def test_list_reports_clone_metadata(harness):
    # Cloned canvases carry n_udfs (*.py files) and mtime from the local
    # folder; un-cloned entries stay null so the client renders no stat line.
    harness.log_in()
    beta = harness.root / "beta"
    beta.mkdir(parents=True)
    (beta / "canvas.toml").write_text("", encoding="utf-8")
    (beta / "one.py").write_text("", encoding="utf-8")
    (beta / "two.py").write_text("", encoding="utf-8")
    (beta / "widget.json").write_text("{}", encoding="utf-8")
    (beta / ".hidden.py").write_text("", encoding="utf-8")
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["beta"]["n_udfs"] == 2
    assert isinstance(canvases["beta"]["mtime"], float)
    assert canvases["alpha"]["n_udfs"] is None
    assert canvases["alpha"]["mtime"] is None


def test_create_canvas_runs_cli(harness):
    harness.log_in()
    res = harness.client.post("/api/canvases/create", json={"name": "gamma"}, headers=GUARD)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "name": "gamma"}
    assert ["workbench", "canvas", "create", "gamma"] in harness.calls()


def test_create_canvas_rejects_bad_names(harness):
    harness.log_in()
    for bad in ("a-b", "a b", ""):
        res = harness.client.post("/api/canvases/create", json={"name": bad}, headers=GUARD)
        assert res.status_code == 400, bad


def test_logout_clears_credentials_and_stops_watchers(harness):
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    manager = canvases_mod._syncs.get("alpha")
    assert manager is not None
    res = harness.client.post("/api/canvases/logout", headers=GUARD)
    assert res.status_code == 200
    assert not harness.creds.exists()
    assert manager.stop_event.is_set()
    assert canvases_mod._syncs == {}


def test_logout_failure_keeps_sync_alive_and_preserves_pending_edit(harness, monkeypatch):
    # A failed `workbench logout` must not have already torn down sync (the
    # user is still signed in) — and a local edit that was pending when the
    # attempt started must not get silently adopted as clean by the
    # pause/resume around the CLI call; it should still push once sync
    # resumes.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.5)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    manager = canvases_mod._syncs.get("alpha")
    assert manager is not None

    (harness.root / "alpha" / "udf.py").write_text("print('hi')\n", encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline and manager._dirty_since is None:
        time.sleep(0.02)
    assert manager._dirty_since is not None

    harness.set_scenario({"fail": "workbench unreachable"})
    res = harness.client.post("/api/canvases/logout", headers=GUARD)
    assert res.status_code != 200
    assert harness.creds.exists()
    assert not manager.stop_event.is_set()
    assert canvases_mod._syncs.get("alpha") is manager

    harness.set_scenario({})
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status["push_seq"] >= 1, status

    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


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


def test_sync_start_on_stale_clone_pushes_pending_edit(harness, monkeypatch):
    # Reopening an already-cloned canvas (or a server restart re-arming the
    # watcher) must not trust whatever's on disk as clean baseline — files
    # that predate the "just came out of a clone" window start dirty, so a
    # genuinely unpushed edit from before the watcher existed still pushes.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(canvases_mod, "_FRESH_WINDOW_S", 1.0)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    old = time.time() - 120
    for f in (harness.root / "alpha").iterdir():
        os.utime(f, (old, old))

    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200

    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status["push_seq"] >= 1, status
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_start_on_fresh_clone_does_not_push(harness, monkeypatch):
    # The mirror case: files just written by `clone --force` are all within
    # the fresh window, so sync/start must NOT fire a spurious push.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    time.sleep(0.4)
    status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert status["push_seq"] == 0
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


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


def test_sync_pulls_remote_changes_when_clean(harness, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    # Remote moves: the dry-run stops reporting up-to-date and the next force
    # pull delivers a new file.
    harness.set_scenario(
        {
            "pull_dry": "would update: remote_udf.py",
            "pull_files": {
                "canvas.toml": 'type = "canvas"\n',
                "remote_udf.py": "print('from workbench')\n",
            },
        }
    )
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["pull_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status and status["pull_seq"] >= 1, status
    assert (harness.root / "alpha" / "remote_udf.py").exists()
    # The pull's own writes are baseline, not local changes — no echo push.
    time.sleep(0.4)
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]

    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_pull_reprobe_diff_triggers_push(harness, monkeypatch):
    # A dry-run right after the force pull that STILL reports a diff (e.g. a
    # local edit landed while `--force` itself was running) means local
    # moved away from what was just pulled — local wins: the pull leg must
    # mark dirty instead of baselining as clean, so the normal debounced push
    # resolves the discrepancy.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    harness.set_scenario(
        {
            "pull_dry": "would update: remote_udf.py",
            "pull_files": {
                "canvas.toml": 'type = "canvas"\n',
                "remote_udf.py": "print('from workbench')\n",
            },
        }
    )
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["pull_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status and status["pull_seq"] >= 1, status

    deadline = time.time() + 5
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status["push_seq"] >= 1, status

    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_pull_poll_skips_when_up_to_date(harness, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    time.sleep(0.6)
    pulls = [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "pull"]]
    # Dry-run probes happened, but nothing was applied (no --force beyond the
    # initial clone) and pull_seq stayed 0.
    assert any("--dry-run" in c for c in pulls)
    assert len([c for c in pulls if "--force" in c]) == 1  # the clone only
    status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert status["pull_seq"] == 0
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


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
