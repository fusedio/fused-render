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
import threading
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
        lines = scenario.get("push_fail_lines")
        if lines:
            for ln in lines:
                sys.stderr.write(ln + "\n")
            sys.exit(1)
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


# In-interpreter list shim (the preferred path when the CLI isn't an external
# binary): stub scripts standing in for _fused_canvases_list.py.
def _wire_list_shim(tmp_path, monkeypatch, body: str) -> None:
    shim = tmp_path / "list_shim.py"
    shim.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        canvases_mod, "_shim_list_command", lambda cli: [sys.executable, str(shim)]
    )


def test_list_shim_reports_previews_and_updated_at(harness, tmp_path, monkeypatch):
    harness.log_in()
    beta = harness.root / "beta"
    beta.mkdir(parents=True)
    (beta / "canvas.toml").write_text("", encoding="utf-8")
    _wire_list_shim(
        tmp_path,
        monkeypatch,
        "import json, sys\n"
        "json.dump([\n"
        "  {'name': 'alpha', 'id': 'id-a', 'preview_url': 'https://s3/signed.png',\n"
        "   'last_updated': '2026-08-18T06:10:17.419215Z'},\n"
        "  {'name': 'beta', 'id': 'id-b', 'preview_url': None, 'last_updated': None},\n"
        "], sys.stdout)\n",
    )
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 200
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["preview_url"] == "https://s3/signed.png"
    assert canvases["alpha"]["id"] == "id-a"
    assert isinstance(canvases["alpha"]["updated_at"], float)
    assert canvases["alpha"]["cloned"] is False
    assert canvases["beta"]["preview_url"] is None
    assert canvases["beta"]["cloned"] is True


def test_list_shim_expired_credentials_map_to_401(harness, tmp_path, monkeypatch):
    harness.log_in()
    _wire_list_shim(
        tmp_path,
        monkeypatch,
        "import sys\n"
        "sys.stderr.write('Error: please re-authenticate with fused login\\n')\n"
        "sys.exit(1)\n",
    )
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 401


def test_list_external_cli_entries_have_null_preview_fields(harness):
    # The stub CLI is an external FUSED_RENDER_FUSED_BIN, so the fallback
    # `canvas list` path runs: bare names, no previews or updated_at.
    harness.log_in()
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["preview_url"] is None
    assert canvases["alpha"]["updated_at"] is None


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


_VALIDATION_LINES = [
    "error: node 'buffer' has no source file (buffer.py missing)",
    "error: edge references unknown node 'join'",
    "error: widget_map.json: {{udf.buffer}} does not resolve",
    "error: widget_map.json: param 'radius' not in UDF signature",
    "Error: Canvas validation failed with 4 error(s). "
    "Fix the errors or use --no-validate to push anyway.",
]


def _fail_push_with_validation(harness, monkeypatch) -> dict:
    """Drive the watcher into a failed push whose stderr is a full
    validation report; return the error status."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    harness.set_scenario({"push_fail_lines": _VALIDATION_LINES})
    (harness.root / "alpha" / "udf.py").write_text("x = 1\n", encoding="utf-8")
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_state"] == "error":
            break
        time.sleep(0.05)
    assert status and status["push_state"] == "error", status
    return status


def test_push_validation_failure_keeps_the_full_transcript(harness, monkeypatch):
    # cli_error keeps ONE line — right for the pill, wrong for validation,
    # where the lines that name the broken files are all above the summary.
    # error_detail carries the verbatim transcript; a later good push clears it.
    status = _fail_push_with_validation(harness, monkeypatch)
    assert status["error_detail"] == _VALIDATION_LINES
    assert "Canvas validation failed with 4 error(s)" in status["error"]

    harness.set_scenario({})
    (harness.root / "alpha" / "udf.py").write_text("x = 2\n", encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["push_state"] == "idle" and status["push_seq"] >= 1:
            break
        time.sleep(0.05)
    assert status["push_state"] == "idle", status
    assert status["error_detail"] == []
    assert status["error"] is None
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_fix_endpoint_requires_a_failing_push(harness, monkeypatch):
    harness.log_in()
    # No watcher at all → nothing to fix.
    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409
    # A healthy watcher → still nothing to fix.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409
    # And the endpoint is write-guarded like every other canvases POST.
    assert harness.client.post("/api/canvases/fix", json={"name": "alpha"}).status_code == 403
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_fix_endpoint_spawns_a_primed_claude_session(harness, monkeypatch):
    from fused_render import claude_spawn

    _fail_push_with_validation(harness, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        claude_spawn, "spawn_helper",
        lambda target, prompt, mode, session_id="": (
            seen.update(target=target, prompt=prompt, mode=mode),
            {"run_id": "run-77"})[1])
    monkeypatch.setattr(claude_spawn, "record_session_when_ready",
                        lambda agent, run_id, on_tick=None: None)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: object())

    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "run_id": "run-77"}
    # Session lands on the clone dir, unattended-capable, primed with the
    # verbatim transcript plus the guard rails (validate loop, never push).
    assert seen["target"] == str(harness.root / "alpha")
    assert seen["mode"] == "auto"
    for line in _VALIDATION_LINES:
        assert line in seen["prompt"]
    assert "fused workbench canvas validate" in seen["prompt"]
    assert "Do NOT run `fused workbench canvas push`" in seen["prompt"]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_fix_active_blocks_a_concurrent_fixer_until_the_recorder_exits(harness, monkeypatch):
    # fix_active is set the instant a fix spawns and is what a second click
    # gets 409'd against — never a guess from transcript activity, because
    # that has a grace window a slow tool call could hide inside (the bug
    # this replaced). It clears when the recorder thread exits, whatever the
    # reason — here simulated by blocking record_session_when_ready on an
    # Event so the test controls exactly when that is.
    from fused_render import claude_spawn

    _fail_push_with_validation(harness, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(
        claude_spawn, "spawn_helper",
        lambda target, prompt, mode, session_id="": {"run_id": "run-1"})
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: object())
    monkeypatch.setattr(
        claude_spawn, "record_session_when_ready",
        lambda agent, run_id: release.wait(5))

    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200
    status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert status["fix_active"] is True

    # A second click while the first fixer is still working is refused, not
    # raced onto the same clone.
    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409

    # The recorder thread exiting (for ANY reason — here, the fake unblocks)
    # is what clears the lock, not a "done" observed mid-run.
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["fix_active"] is False:
            break
        time.sleep(0.05)
    assert status["fix_active"] is False

    # A push still in error and no active fixer → a new fix is allowed again.
    monkeypatch.setattr(
        claude_spawn, "spawn_helper",
        lambda target, prompt, mode, session_id="": {"run_id": "run-2"})
    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200
    assert res.json()["run_id"] == "run-2"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_fix_active_clears_even_when_the_recorder_never_sees_done(harness, monkeypatch):
    # record_session_when_ready can return without ever observing "done" (its
    # own poll cap, an exception, or — here — load_agent() itself blowing up
    # inside the recorder thread). The lock must still clear: the fallback is
    # the recorder thread exiting at all, not a specific outcome inside it.
    from fused_render import claude_spawn

    _fail_push_with_validation(harness, monkeypatch)
    monkeypatch.setattr(
        claude_spawn, "spawn_helper",
        lambda target, prompt, mode, session_id="": {"run_id": "run-1"})

    def _boom():
        raise RuntimeError("agent.py failed to load")

    monkeypatch.setattr(claude_spawn, "load_agent", _boom)

    res = harness.client.post("/api/canvases/fix", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200

    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if status["fix_active"] is False:
            break
        time.sleep(0.05)
    assert status["fix_active"] is False, status
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_fix_lock_serializes_concurrent_spawn_attempts(harness, monkeypatch):
    # The check-then-spawn-then-set sequence used to run outside any lock, so
    # two nearly-simultaneous clicks (two workspace tabs) could both read "no
    # active fix" before either had set it, and both spawn a session onto the
    # same clone. fix_lock makes that sequence atomic; this drives two REAL
    # concurrent calls at the underlying endpoint function (bypassing the
    # HTTP layer, same pattern as test_claude_permission_bridge's concurrent
    # first-run test) to prove the second one blocks on the lock rather than
    # racing through.
    from fused_render import claude_spawn

    _fail_push_with_validation(harness, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    # Held open until after both calls have returned: record_session_when_ready
    # clears active_fix_run_id with no lock of its own (it doesn't need one —
    # clearing isn't a compound check-then-act), so a stub that returns
    # instantly can race the second thread's read of active_fix_run_id and
    # make THIS TEST flaky, independent of whether fix_lock itself is correct.
    # A real recorder never returns this fast (it polls every 2s), so holding
    # it open here just removes a timing accident the stub introduced.
    recorder_release = threading.Event()
    calls = []

    def _slow_spawn(target, prompt, mode, session_id=""):
        calls.append(1)
        entered.set()
        release.wait(5)
        return {"run_id": f"run-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", _slow_spawn)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: object())
    monkeypatch.setattr(
        claude_spawn, "record_session_when_ready",
        lambda agent, run_id: recorder_release.wait(5))

    results = {}

    def _call(key):
        results[key] = canvases_mod.api_canvases_fix(body={"name": "alpha"}, x_fused="1")

    t1 = threading.Thread(target=_call, args=("a",))
    t1.start()
    assert entered.wait(5), "first call never reached spawn_helper"
    t2 = threading.Thread(target=_call, args=("b",))
    t2.start()
    t2.join(2)
    # Still blocked on fix_lock — spawn_helper only ran once so far.
    assert "b" not in results
    assert len(calls) == 1
    release.set()
    t1.join(5)
    t2.join(5)
    assert len(calls) == 1, "a second spawn ran before the first released the lock"
    assert isinstance(results["a"], dict) and results["a"]["ok"] is True
    assert results["b"].status_code == 409
    recorder_release.set()
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


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


# -- shim-backed sync: manifest probe + per-file three-way merge ---------------
#
# The manifest/zip shims are stub scripts wired in through monkeypatch (same
# pattern as _wire_list_shim). The manifest stub prints the FAKE_MANIFEST file;
# the zip stub bundles FAKE_REMOTE_DIR — mutating those two simulates the
# remote moving.

_MANIFEST_SHIM = """
import os, sys
with open(os.environ["FAKE_MANIFEST"]) as f:
    sys.stdout.write(f.read())
"""

_ZIP_SHIM = """
import io, os, sys, zipfile
src = os.environ["FAKE_REMOTE_DIR"]
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    for root, dirs, files in os.walk(src):
        for name in files:
            path = os.path.join(root, name)
            zf.write(path, os.path.relpath(path, src))
sys.stdout.buffer.write(buf.getvalue())
"""


def _md5(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


class SyncShims:
    """Wires the manifest/zip shims and owns the fake remote state."""

    def __init__(self, harness, tmp_path, monkeypatch):
        manifest_shim = tmp_path / "manifest_shim.py"
        manifest_shim.write_text(_MANIFEST_SHIM, encoding="utf-8")
        zip_shim = tmp_path / "zip_shim.py"
        zip_shim.write_text(_ZIP_SHIM, encoding="utf-8")
        monkeypatch.setattr(
            canvases_mod,
            "_shim_manifest_command",
            lambda cli: [sys.executable, str(manifest_shim)],
        )
        monkeypatch.setattr(
            canvases_mod,
            "_shim_zip_command",
            lambda cli: [sys.executable, str(zip_shim)],
        )
        self.manifest_file = tmp_path / "fake_manifest.json"
        self.remote_dir = tmp_path / "fake_remote"
        self.remote_dir.mkdir()
        monkeypatch.setenv("FAKE_MANIFEST", str(self.manifest_file))
        monkeypatch.setenv("FAKE_REMOTE_DIR", str(self.remote_dir))
        self.harness = harness

    def set_manifest(self, last_updated: str) -> None:
        self.manifest_file.write_text(
            json.dumps({"id": "c1", "last_updated": last_updated, "udfs": {}}),
            encoding="utf-8",
        )

    def set_remote_files(self, files: dict) -> None:
        for old in self.remote_dir.rglob("*"):
            if old.is_file():
                old.unlink()
        for rel, content in files.items():
            path = self.remote_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def seed_base(self, name: str, files: dict, last_updated: str) -> None:
        """The persisted sync-point state a manager loads at construction."""
        sync_dir = self.harness.root / ".sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "files": {rel: _md5(content) for rel, content in files.items()},
                    "remote": {"id": "c1", "last_updated": last_updated, "udfs": {}},
                }
            ),
            encoding="utf-8",
        )


_BASE_FILES = {
    "canvas.toml": 'type = "canvas"\n',
    "a.py": "a1\n",
    "b.py": "b1\n",
}


def _cloned_shim_harness(harness, tmp_path, monkeypatch) -> SyncShims:
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    shims = SyncShims(harness, tmp_path, monkeypatch)
    shims.seed_base("alpha", _BASE_FILES, "t1")
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    return shims


def _wait_status(harness, predicate, timeout=8):
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
        if predicate(status):
            return status
        time.sleep(0.05)
    return status


def test_sync_merges_remote_changes_while_dirty(harness, tmp_path, monkeypatch):
    # Remote changed b.py while the local clone had an unpushed edit to a.py:
    # the merge applies b.py (local untouched) and keeps a.py (local wins),
    # then the debounced push publishes the merged state.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.3)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")
    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")

    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1 and s["push_seq"] >= 1)
    assert status and status["merge_seq"] >= 1 and status["push_seq"] >= 1, status
    assert (harness.root / "alpha" / "a.py").read_text() == "a-local\n"
    assert (harness.root / "alpha" / "b.py").read_text() == "b2-remote\n"
    # The sync-point state lives OUTSIDE the clone dir (a CLI `pull --force`
    # removes any in-dir file that isn't in the bundle).
    assert (harness.root / ".sync" / "alpha.json").exists()
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_merge_keeps_local_delete(harness, tmp_path, monkeypatch):
    # Local deleted b.py; the bundle still carries it. The merge must NOT
    # recreate it — the push propagates the delete.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.3)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    (harness.root / "alpha" / "b.py").unlink()
    shims.set_remote_files({**_BASE_FILES, "a.py": "a2-remote\n"})
    shims.set_manifest("t2")

    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1 and s["push_seq"] >= 1)
    assert status and status["merge_seq"] >= 1, status
    assert (harness.root / "alpha" / "a.py").read_text() == "a2-remote\n"
    assert not (harness.root / "alpha" / "b.py").exists()
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_merge_applies_remote_delete_when_untouched(harness, tmp_path, monkeypatch):
    # Remote deleted b.py; local never touched it since the sync point → the
    # merge removes it locally. The locally-edited canvas.toml stays.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.3)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    (harness.root / "alpha" / "canvas.toml").write_text(
        'type = "canvas"\nlocal = true\n', encoding="utf-8"
    )
    remote = dict(_BASE_FILES)
    del remote["b.py"]
    shims.set_remote_files(remote)
    shims.set_manifest("t2")

    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1 and s["push_seq"] >= 1)
    assert status and status["merge_seq"] >= 1, status
    assert not (harness.root / "alpha" / "b.py").exists()
    assert (
        harness.root / "alpha" / "canvas.toml"
    ).read_text() == 'type = "canvas"\nlocal = true\n'
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_push_probes_and_merges_first(harness, tmp_path, monkeypatch):
    # Poll effectively disabled: the ONLY probe that can see the remote move
    # is the one _push runs before replacing the remote set.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 1000.0)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")
    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")

    status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert status and status["push_seq"] >= 1, status
    assert status["merge_seq"] >= 1, status
    assert (harness.root / "alpha" / "a.py").read_text() == "a-local\n"
    assert (harness.root / "alpha" / "b.py").read_text() == "b2-remote\n"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_shim_poll_pulls_clean_via_cli_force(harness, tmp_path, monkeypatch):
    # No seeded base: the first poll adopts the manifest as baseline; the
    # next manifest change with a CLEAN clone goes through the CLI force
    # pull (wholesale, as before), not the merge.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    shims = SyncShims(harness, tmp_path, monkeypatch)
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    time.sleep(0.4)  # first poll adopts the baseline
    harness.set_scenario(
        {"pull_files": {**_BASE_FILES, "remote_udf.py": "print('from workbench')\n"}}
    )
    shims.set_manifest("t2")

    status = _wait_status(harness, lambda s: s["pull_seq"] >= 1)
    assert status and status["pull_seq"] >= 1, status
    assert (harness.root / "alpha" / "remote_udf.py").exists()
    assert status["merge_seq"] == 0
    # The pull's writes are baseline, not local changes — no echo push.
    time.sleep(0.4)
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)
