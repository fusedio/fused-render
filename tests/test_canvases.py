"""Canvases sub-app backend (fused_render/canvases.py).

Same harness idea as test_account.py: the fused CLI is a stub script wired in
through FUSED_RENDER_FUSED_BIN, its behavior driven by a scenario file, its
invocations logged — so the tests exercise the real subprocess seam without a
network or a fused install. The `fused login` credential store is pointed at a
tmp file via FUSED_RENDER_FUSED_CREDENTIALS.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
import fused_render.fusedcli as fusedcli_mod
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
        delay = scenario.get("pull_delay")
        if delay:
            import time as _t
            _t.sleep(delay)
        out = plain[plain.index("-o") + 1]
        files = scenario.get("pull_files", {"canvas.toml": 'type = "canvas"\n'})
        if "--dry-run" in plain:
            # canvas_pull.py prints this sentinel when local == remote;
            # anything else means the pull would change files. An explicit
            # override (used by tests of the genuine-diff case) always wins;
            # otherwise this mirrors the real CLI's _pull_plan_and_conflicts:
            # any on-disk file not in the bundle (besides its one exempt
            # basename, _shared.fused) is a diff too, same as a content
            # mismatch — so a clone carrying our own seeded CLAUDE.md /
            # .fusedignore reports a diff exactly like the real CLI would.
            if "pull_dry" in scenario:
                sys.stdout.write(scenario["pull_dry"])
                return
            diff = False
            for rel, content in files.items():
                p = os.path.join(out, rel)
                try:
                    with open(p) as f:
                        diff = f.read() != content
                except OSError:
                    diff = True
                if diff:
                    break
            if not diff:
                for root, _dirs, names in os.walk(out):
                    for name in names:
                        if name == "_shared.fused":
                            continue
                        rel = os.path.relpath(os.path.join(root, name), out)
                        if rel.replace(os.sep, "/") not in files:
                            diff = True
                            break
                    if diff:
                        break
            sys.stdout.write(
                "Would write files (local differs from the canvas)."
                if diff else
                "Nothing to do: already up to date (local files match the canvas)."
            )
            return
        os.makedirs(out, exist_ok=True)
        # --force replaces the bundle wholesale: any on-disk file not in the
        # bundle is deleted too (besides _shared.fused), matching the real
        # CLI's plan.deletes.
        if "--force" in plain:
            for root, _dirs, names in os.walk(out):
                for name in names:
                    if name == "_shared.fused":
                        continue
                    p = os.path.join(root, name)
                    rel = os.path.relpath(p, out).replace(os.sep, "/")
                    if rel not in files:
                        os.remove(p)
        for rel, content in files.items():
            full = os.path.join(out, rel)
            os.makedirs(os.path.dirname(full) or out, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        return
    if plain[:2] == ["canvas", "push"]:
        lines = scenario.get("push_fail_lines")
        if lines:
            for ln in lines:
                sys.stderr.write(ln + "\n")
            sys.exit(1)
        return
    if plain[:2] == ["canvas", "validate"]:
        if scenario.get("validate_fail"):
            sys.stderr.write("error: node 'x' has no source file\n")
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
def _load_list_shim():
    """Import _fused_canvases_list.py with its `fused` imports stubbed.

    The shim runs as a script inside the CLI's own interpreter, where `fused` is
    always importable; the count filter it carries is pure, so a test of that
    filter should not need the compute-engine wheel installed.
    """
    import importlib.util
    import types

    path = os.path.join(
        os.path.dirname(canvases_mod.__file__), "_fused_canvases_list.py"
    )
    fake = types.ModuleType("fused")
    fake._env = lambda name: None
    global_api = types.ModuleType("fused._global_api")
    global_api.get_api = lambda: None
    saved = {k: sys.modules.get(k) for k in ("fused", "fused._global_api")}
    sys.modules["fused"] = fake
    sys.modules["fused._global_api"] = global_api
    try:
        spec = importlib.util.spec_from_file_location("_list_shim_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, prev in saved.items():
            if prev is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = prev
    return module


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


def test_list_shim_code_udf_count_passes_through(harness, tmp_path, monkeypatch):
    # The count exists for every canvas the listing names, cloned or not — it is
    # computed from the node list the lite payload already carried, so a card can
    # show "N UDFs" (and tile N map thumbnails) before anything is cloned.
    harness.log_in()
    _wire_list_shim(
        tmp_path,
        monkeypatch,
        "import json, sys\n"
        "json.dump([\n"
        "  {'name': 'alpha', 'id': 'id-a', 'n_code_udfs': 6, 'last_updated': None},\n"
        "  {'name': 'beta', 'id': 'id-b', 'n_code_udfs': 0, 'last_updated': None},\n"
        # A shim from an older install, or one whose payload had no node list.
        "  {'name': 'gamma', 'id': 'id-c', 'last_updated': None},\n"
        "  {'name': 'delta', 'id': 'id-d', 'n_code_udfs': 'six', 'last_updated': None},\n"
        "], sys.stdout)\n",
    )
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 200
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["n_code_udfs"] == 6
    # Zero is a real answer (an empty canvas), NOT a missing one — the card
    # shows "No UDFs present in the canvas" for it rather than a map tile.
    assert canvases["beta"]["n_code_udfs"] == 0
    assert canvases["gamma"]["n_code_udfs"] is None
    assert canvases["delta"]["n_code_udfs"] is None


def test_list_external_cli_entries_carry_a_null_code_udf_count(harness):
    # The bare-name `canvas list` fallback has no node list to count, but the key
    # is still present so the client does not have to special-case its absence.
    harness.log_in()
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["n_code_udfs"] is None


def test_shim_code_udf_count_excludes_notes_widgets_and_apps():
    # Mirrors the workbench client's getCodeUdfCount: sticky notes and widgets
    # are nodes but not code, and an `app` node is a published app.
    shim = _load_list_shim()
    assert (
        shim._code_udf_count(
            {
                "udf_ids": [
                    {"slug": "airbnb_data", "udf_type": "auto"},
                    {"slug": "note_3", "udf_type": "auto"},
                    {"slug": "widget_1", "udf_type": "auto"},
                    {"slug": "my_app", "udf_type": "app"},
                    {"slug": "square_numbers", "udf_type": "auto"},
                    "not-a-dict",
                ]
            }
        )
        == 2
    )
    assert shim._code_udf_count({"udf_ids": []}) == 0
    assert shim._code_udf_count({}) is None


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
    assert canvases["alpha"]["preview_pending"] is False


# Preview signing off the listing's critical path (D364): the list shim reports
# an uploaded preview as pending, and POST /api/canvases/previews signs the
# whole batch afterwards.
def _wire_previews_shim(tmp_path, monkeypatch, body: str) -> None:
    shim = tmp_path / "previews_shim.py"
    shim.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        canvases_mod, "_shim_previews_command", lambda cli: [sys.executable, str(shim)]
    )


def test_list_reports_a_pending_preview_without_signing_it(harness, tmp_path, monkeypatch):
    harness.log_in()
    _wire_list_shim(
        tmp_path,
        monkeypatch,
        "import json, sys\n"
        "json.dump([\n"
        "  {'name': 'alpha', 'id': 'id-a', 'preview_url': None,\n"
        "   'preview_pending': True, 'last_updated': None},\n"
        "  {'name': 'beta', 'id': None, 'preview_url': None,\n"
        "   'preview_pending': True, 'last_updated': None},\n"
        "], sys.stdout)\n",
    )
    res = harness.client.get("/api/canvases/list", headers=GUARD)
    assert res.status_code == 200
    canvases = {c["name"]: c for c in res.json()["canvases"]}
    assert canvases["alpha"]["preview_pending"] is True
    assert canvases["alpha"]["preview_url"] is None
    # No id means nothing to sign, so it is not advertised as pending.
    assert canvases["beta"]["preview_pending"] is False


def test_previews_endpoint_signs_the_batch_it_is_given(harness, tmp_path, monkeypatch):
    harness.log_in()
    # The shim reads the ids as JSON on stdin — assert that, not just the output.
    _wire_previews_shim(
        tmp_path,
        monkeypatch,
        "import json, sys\n"
        "ids = json.load(sys.stdin)\n"
        "json.dump({i: None if i == 'id-none' else 'https://s3/%s.png' % i for i in ids},\n"
        "          sys.stdout)\n",
    )
    res = harness.client.post(
        "/api/canvases/previews", json={"ids": ["id-a", "id-none"]}, headers=GUARD
    )
    assert res.status_code == 200
    assert res.json()["previews"] == {
        "id-a": "https://s3/id-a.png",
        "id-none": None,
    }


def test_previews_endpoint_is_guarded_and_validates_its_input(harness):
    harness.log_in()
    assert harness.client.post("/api/canvases/previews", json={"ids": []}).status_code == 403
    bad = harness.client.post("/api/canvases/previews", json={"ids": "id-a"}, headers=GUARD)
    assert bad.status_code == 400
    # An empty batch is answered without running anything at all.
    empty = harness.client.post("/api/canvases/previews", json={"ids": []}, headers=GUARD)
    assert empty.status_code == 200
    assert empty.json() == {"previews": {}}


def test_previews_endpoint_caps_the_batch_size(harness, tmp_path, monkeypatch):
    harness.log_in()
    _wire_previews_shim(
        tmp_path,
        monkeypatch,
        "import json, sys\n"
        "json.dump({'n': str(len(json.load(sys.stdin)))}, sys.stdout)\n",
    )
    over = canvases_mod.PREVIEWS_MAX_IDS + 10
    res = harness.client.post(
        "/api/canvases/previews",
        json={"ids": [f"id-{i}" for i in range(over)]},
        headers=GUARD,
    )
    assert res.status_code == 200
    assert res.json()["previews"]["n"] == str(canvases_mod.PREVIEWS_MAX_IDS)


def test_previews_endpoint_on_an_external_cli_answers_empty(harness):
    # The stub CLI is an external FUSED_RENDER_FUSED_BIN: that path never
    # reports a pending preview, so there is nothing to sign — and no error.
    harness.log_in()
    res = harness.client.post(
        "/api/canvases/previews", json={"ids": ["id-a"]}, headers=GUARD
    )
    assert res.status_code == 200
    assert res.json() == {"previews": {}}


def test_previews_endpoint_maps_expired_credentials_to_401(harness, tmp_path, monkeypatch):
    harness.log_in()
    _wire_previews_shim(
        tmp_path,
        monkeypatch,
        "import sys\n"
        "sys.stderr.write('Error: please re-authenticate with fused login\\n')\n"
        "sys.exit(1)\n",
    )
    res = harness.client.post(
        "/api/canvases/previews", json={"ids": ["id-a"]}, headers=GUARD
    )
    assert res.status_code == 401


def test_previews_endpoint_requires_a_login(harness, tmp_path, monkeypatch):
    _wire_previews_shim(tmp_path, monkeypatch, "import sys\nsys.exit(1)\n")
    res = harness.client.post(
        "/api/canvases/previews", json={"ids": ["id-a"]}, headers=GUARD
    )
    assert res.status_code == 409


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
    # verbatim transcript plus what to do: validate, then publish. (It used to
    # be told NOT to push; nothing races it now, and the push is how it
    # confirms the fix landed — see _fix_prompt.)
    assert seen["target"] == str(harness.root / "alpha")
    assert seen["mode"] == "auto"
    for line in _VALIDATION_LINES:
        assert line in seen["prompt"]
    assert "fused workbench canvas validate" in seen["prompt"]
    assert "fused workbench canvas push ." in seen["prompt"]
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
import io, os, sys, time, zipfile
delay = os.environ.get("FAKE_ZIP_DELAY")
if delay:
    time.sleep(float(delay))
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

    def set_manifest(self, last_updated: str, udfs: dict | None = None) -> None:
        self.manifest_file.write_text(
            json.dumps(
                {"id": "c1", "last_updated": last_updated, "udfs": udfs or {}}
            ),
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

    def seed_base(
        self,
        name: str,
        files: dict,
        last_updated: str,
        udfs: dict | None = None,
        history: list | None = None,
    ) -> None:
        """The persisted sync-point state a manager loads at construction."""
        sync_dir = self.harness.root / ".sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "files": {rel: _md5(content) for rel, content in files.items()},
                    "remote": {
                        "id": "c1",
                        "last_updated": last_updated,
                        "udfs": udfs or {},
                    },
                    "history": [
                        {"udfs": h, "at": time.time()} for h in (history or [])
                    ],
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
    # Shims wired BEFORE the clone: CLAUDE.md seeding is gated on shim
    # availability (external-CLI installs never seed).
    shims = SyncShims(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    shims.seed_base("alpha", _BASE_FILES, "t1")
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    return shims


def _manager(name="alpha"):
    """The live watcher for a canvas — for waiting on internal sync state that
    no status field exposes."""
    return canvases_mod._syncs[name]


def _wait_for(predicate, timeout=8):
    """Spin until `predicate()` or the deadline. Returns whether it held."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except (KeyError, AttributeError):
            pass
        time.sleep(0.02)
    return False


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
    # The overwritten a.py's pre-merge bytes landed in the trash safety net.
    trashed = list((harness.root / ".sync" / "trash" / "alpha").glob("*/a.py"))
    assert trashed and trashed[0].read_text() == "a1\n"
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
    # The remote-deleted file's bytes landed in the trash safety net.
    trashed = list((harness.root / ".sync" / "trash" / "alpha").glob("*/b.py"))
    assert trashed and trashed[0].read_text() == "b1\n"
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
    # The pull's writes are baseline, not local changes — no echo push. This
    # also pins the seeded-files regression: CLAUDE.md/.fusedignore are
    # rewritten into the clone right after this force pull, and if that
    # happened BEFORE the post-pull dry-run recheck (rather than after), the
    # recheck would see them as an un-bundled diff on every single poll and
    # push_state would be stuck "pending" forever.
    time.sleep(0.4)
    status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert status["push_state"] == "idle", status
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_shim_force_pull_rechecks_before_adopting_clean(harness, tmp_path, monkeypatch):
    """A3: an edit landing DURING the force pull must not be adopted as clean.

    The legacy leg (_pull_if_remote_changed) re-runs a `--dry-run` after
    applying and re-arms the dirty flag, precisely because that window cannot be
    closed with fingerprints — the pull's own writes and a concurrent local edit
    both just look like "the file changed". The shim leg re-baselined
    unconditionally instead, so a file an active session wrote mid-pull was
    overwritten AND recorded as the sync point: a silently lost edit.

    `pull_dry` makes the post-pull recheck report a diff, which is the CLI
    saying "local is not what I just pulled". Local wins: go dirty and push,
    rather than clean and silent.
    """
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    shims = SyncShims(harness, tmp_path, monkeypatch)
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    # Wait for the FIRST poll to adopt the manifest as baseline, rather than
    # sleeping a guessed interval: if t2 lands before that poll, the baseline
    # adopted is t2 itself and no force pull ever happens — the test would then
    # fail for a reason that has nothing to do with the recheck. Under xdist
    # load a fixed sleep loses that race often enough to matter.
    _wait_for(lambda: getattr(_manager(), "_remote", None) is not None)
    # The remote moved, the clone is clean → the CLI force-pull branch. The
    # recheck that follows it reports a diff: something moved local away from
    # what was just pulled.
    harness.set_scenario({
        "pull_files": {**_BASE_FILES, "remote_udf.py": "print('from workbench')\n"},
        "pull_dry": "Would write 1 file (local differs from the canvas).",
    })
    shims.set_manifest("t2")

    status = _wait_status(harness, lambda s: s["pull_seq"] >= 1)
    assert status and status["pull_seq"] >= 1, status
    # The recheck ran at all — without it there is nothing to notice the edit.
    # Waited for, not asserted outright: pull_seq is bumped before the recheck
    # subprocess is spawned, so the status the loop above saw does not yet
    # imply the call has been logged.
    assert _wait_for(lambda: [c for c in harness.calls()
                              if c[:3] == ["workbench", "canvas", "pull"]
                              and "--dry-run" in c]), \
        "no post-pull --dry-run recheck was issued"
    # Local wins: the clone is dirty and pushes, instead of being adopted clean.
    pushed = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert pushed and pushed["push_seq"] >= 1, pushed
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


_UDFS_OLD = {"a": {"hash": "h1", "last_updated": "t0"}}
_UDFS_NEW = {"a": {"hash": "h2", "last_updated": "t1"}}


def test_sync_stale_echo_is_repushed_not_pulled(harness, tmp_path, monkeypatch):
    # A stale browser tab autosaved pre-push state over a fresh push: the
    # probed remote matches a sync point this watcher already superseded
    # (in history, inside ECHO_WINDOW_S). The guard must NOT pull it down
    # (that's how a fresh local file gets deleted) — it queues a push that
    # re-asserts local instead.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    shims.seed_base(
        "alpha", _BASE_FILES, "t1", udfs=_UDFS_NEW, history=[_UDFS_OLD]
    )
    # The remote now shows the SUPERSEDED udf BODIES again — but a stale
    # save always writes fresh timestamps (collection and per-UDF), so the
    # guard must match on the hashes alone.
    shims.set_manifest("t9", udfs={"a": {"hash": "h1", "last_updated": "t8"}})
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert status and status["push_seq"] >= 1, status
    assert status["echo_seq"] >= 1, status
    assert status["pull_seq"] == 0 and status["merge_seq"] == 0, status
    # Nothing was pulled down: the clone still holds all its files.
    assert (harness.root / "alpha" / "b.py").read_text() == "b1\n"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_sync_merge_rolls_back_when_it_breaks_validation(harness, tmp_path, monkeypatch):
    # Per-file merge can mix canvas.toml from one side with source files
    # from the other and produce an unpushable clone. When post-merge
    # validation fails, the merge is rolled back: local files restored,
    # clone stays dirty, the push re-asserts local wholesale.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.3)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")
    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")
    harness.set_scenario({"pull_files": _BASE_FILES, "validate_fail": True})

    status = _wait_status(
        harness, lambda s: s["merge_rollback_seq"] >= 1 and s["push_seq"] >= 1
    )
    assert status and status["merge_rollback_seq"] >= 1, status
    assert status["merge_seq"] == 0, status
    # The merge's write to b.py was undone; local edits untouched.
    assert (harness.root / "alpha" / "b.py").read_text() == "b1\n"
    assert (harness.root / "alpha" / "a.py").read_text() == "a-local\n"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_push_aborts_when_remote_moved_and_zip_unavailable(harness, tmp_path, monkeypatch):
    # The pre-push probe sees the remote moved, but the zip download fails —
    # pushing anyway would wholesale-replace edits we haven't seen. The push
    # must abort and retry; once the zip works, merge + push proceed.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 1000.0)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    broken_zip = tmp_path / "broken_zip.py"
    broken_zip.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    working_zip_cmd = canvases_mod._shim_zip_command(None)  # monkeypatched: ignores cli
    monkeypatch.setattr(
        canvases_mod, "_shim_zip_command", lambda cli: [sys.executable, str(broken_zip)]
    )
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")
    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")

    time.sleep(1.0)
    status = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert status["push_seq"] == 0, status
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]

    # Zip recovers → merge folds b.py in, push publishes.
    monkeypatch.setattr(canvases_mod, "_shim_zip_command", lambda cli: working_zip_cmd)
    status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert status and status["push_seq"] >= 1 and status["merge_seq"] >= 1, status
    assert (harness.root / "alpha" / "b.py").read_text() == "b2-remote\n"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_merge_abort_clears_a_stale_error_from_an_earlier_failed_push(
        harness, tmp_path, monkeypatch):
    """A merge-abort (remote moved, zip download failed) is a benign,
    retryable deferral — push_state goes "pending", not "error". But if an
    EARLIER push failed with a real validation error, last_error/error_detail
    stayed set (they only clear on a successful push), so a later merge-abort
    would report last time's validation errors verbatim even though this
    attempt never got far enough to see them. `_fix_prompt` and the CLI
    interception's error_detail passthrough would then send a Claude session
    to fix a problem it may have already fixed."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 1000.0)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    # First, a real validation failure: last_error/error_detail get set.
    harness.set_scenario({"push_fail_lines": _VALIDATION_LINES})
    (harness.root / "alpha" / "a.py").write_text("a-broken\n", encoding="utf-8")
    status = _wait_status(harness, lambda s: s["push_state"] == "error")
    assert status and status["push_state"] == "error", status
    assert status["error_detail"], status

    # Now: remote moved, zip download fails → the NEXT push attempt aborts
    # via merge, not via a real push failure.
    harness.set_scenario({"push_fail_lines": None})
    broken_zip = tmp_path / "broken_zip2.py"
    broken_zip.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(
        canvases_mod, "_shim_zip_command", lambda cli: [sys.executable, str(broken_zip)]
    )
    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")
    (harness.root / "alpha" / "a.py").write_text("a-fixed\n", encoding="utf-8")

    status = _wait_status(harness, lambda s: s["push_state"] == "pending")
    assert status and status["push_state"] == "pending", status
    assert status["error"] is None, status
    assert status["error_detail"] == [], status
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_clone_seeds_claude_md_and_fusedignore(harness, tmp_path, monkeypatch):
    # A clone gets a CLAUDE.md pointing the session at the workbench:* skills
    # and a .fusedignore keeping both files out of every push. Seeding never
    # dirties the sync.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    claude_md = harness.root / "alpha" / "CLAUDE.md"
    assert claude_md.exists()
    text = claude_md.read_text()
    assert "workbench:canvas-toml" in text
    ignore = (harness.root / "alpha" / ".fusedignore").read_text()
    assert "CLAUDE.md" in ignore and ".fusedignore" in ignore
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    time.sleep(0.4)
    # The seeded files are invisible to the watcher — no push fired.
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_opening_a_canvas_retires_the_legacy_fused_plugin(harness, tmp_path,
                                                          monkeypatch):
    """The other half of the stale-skills fix, wired to the same hook as the
    fetch. Supplying the current `workbench:*` skills achieves nothing while the
    pre-rename `fused` plugin is still enabled globally and shadowing them under
    the very prefix an old seeded CLAUDE.md names — so the canvas paths do both.

    Asserted through the real function rather than a spy on the name, because a
    spy would keep passing if the call were moved somewhere it never runs.
    """
    from fused_render import skill_plugin
    from fused_render.claude_config import lib

    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    with open(lib.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"enabledPlugins": {skill_plugin.LEGACY_PLUGIN_ID: True,
                                      "keep-me@mkt": True}}, f)

    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"},
                        headers=GUARD)
    # The kick runs off-thread — wait for it rather than sleeping a guess.
    deadline = time.time() + 5
    enabled = {}
    while time.time() < deadline:
        with open(lib.SETTINGS_PATH, encoding="utf-8") as f:
            enabled = (json.load(f).get("enabledPlugins") or {})
        if enabled.get(skill_plugin.LEGACY_PLUGIN_ID) is False:
            break
        time.sleep(0.05)
    assert enabled.get(skill_plugin.LEGACY_PLUGIN_ID) is False, enabled
    # And nothing else was touched on the way past.
    assert enabled.get("keep-me@mkt") is True, enabled
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"},
                        headers=GUARD)


def test_sync_start_reseeds_a_stale_claude_md(harness, tmp_path, monkeypatch):
    """Opening a canvas rewrites CLAUDE.md, so a clone made before a text change
    stops carrying the old one. This is the field bug: clones from before D360
    name the pre-rename `fused:*` skills, and a user who once installed that
    plugin globally has a stale copy the session happily loads instead of the
    `workbench:*` root the app hands it — silently, because stale skills load
    fine. /clone and the force-pull legs cannot reach an existing clone; open
    can, and does it on every open (and every watcher re-arm)."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    claude_md = harness.root / "alpha" / "CLAUDE.md"
    claude_md.write_text("stale: load fused:canvas-toml\n", encoding="utf-8")

    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    text = claude_md.read_text()
    assert "workbench:canvas-toml" in text
    assert "fused:canvas-toml" not in text
    time.sleep(0.4)
    # Rewriting it is still invisible to the sync — no push fired.
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_clean_pull_reseeds_claude_md(harness, tmp_path, monkeypatch):
    # The CLI's `pull --force` deletes the seeded files (not in the bundle);
    # the pull leg puts them back.
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "CLAUDE.md").unlink()

    shims.set_remote_files({**_BASE_FILES, "remote_udf.py": "print('x')\n"})
    harness.set_scenario(
        {"pull_files": {**_BASE_FILES, "remote_udf.py": "print('x')\n"}}
    )
    shims.set_manifest("t2")
    status = _wait_status(harness, lambda s: s["pull_seq"] >= 1)
    assert status and status["pull_seq"] >= 1, status
    # Waited for, not asserted outright — the same reason as the recheck test
    # above: `pull_seq` is bumped BEFORE the post-pull recheck subprocess, and
    # the reseed deliberately follows that recheck (seeding first makes the
    # never-bundled helper files look like a permanent diff). So a status that
    # reports pull_seq >= 1 does not yet imply the files are back, and under
    # load (-n auto) that gap is wide enough to fail ~1 run in 10.
    assert _wait_for(lambda: (harness.root / "alpha" / "CLAUDE.md").exists()), \
        "the pull leg never re-seeded CLAUDE.md"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


# -- the workbench skills reach an EXISTING canvas, not just a fresh clone -----
#
# Field bug: the fetch hung off POST /api/canvases/clone alone, so a canvas that
# already existed (i.e. nearly every real canvas) never got one. The plugin root
# stayed absent, `_plugin_argv` passed no second --plugin-dir, and the session
# looked up the `workbench:canvas-toml` skills its own seeded CLAUDE.md names,
# found nothing, and went hunting for the format elsewhere — `find /` included,
# which wedged every rclone mount on the machine. So the fetch has to fire on the
# path that OPENS a canvas too, which is the watcher start the page does on boot.


def _stub_skills_git(monkeypatch, calls):
    """The git seam, materialising a loadable clone the way a real one would."""
    from fused_render import skill_plugin

    def run(args, timeout):
        calls.append(list(args))
        if args[0] == "clone":
            root = os.path.join(args[-1], skill_plugin.WORKBENCH_PLUGIN_SUBDIR)
            os.makedirs(os.path.join(root, skill_plugin.MANIFEST_DIR), exist_ok=True)
            with open(os.path.join(root, skill_plugin.MANIFEST_DIR,
                                   skill_plugin.MANIFEST_NAME), "w",
                      encoding="utf-8") as fh:
                fh.write('{"name": "workbench"}')
            for skill in skill_plugin.WORKBENCH_SKILLS:
                d = os.path.join(root, skill_plugin.SKILLS_SUBDIR, skill)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
                    fh.write("# %s\n" % skill)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(skill_plugin, "_git", run)


def test_opening_an_existing_canvas_fetches_the_workbench_skills(
        harness, tmp_path, monkeypatch):
    """The page's own open path: POST /api/canvases/sync/start on a canvas dir
    that is already there. No /clone is involved and none can be — the canvas
    predates this feature — so this is the only hook that can put the skills on
    disk for it."""
    from fused_render import skill_plugin

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "skillhome"))
    monkeypatch.delenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, raising=False)
    calls = []
    _stub_skills_git(monkeypatch, calls)

    # An EXISTING canvas: made by hand, exactly as a pre-branch clone looks.
    (harness.root / "canvas_1").mkdir(parents=True)
    (harness.root / "canvas_1" / "canvas.toml").write_text(
        'type = "canvas"\n', encoding="utf-8")
    assert skill_plugin.workbench_plugin_root() is None, "precondition: no clone"

    res = harness.client.post("/api/canvases/sync/start",
                              json={"name": "canvas_1"}, headers=GUARD)
    assert res.status_code == 200, res.text

    assert _wait_for(lambda: skill_plugin.workbench_plugin_root() is not None), (
        "opening an existing canvas never fetched the workbench skills", calls)
    root = skill_plugin.workbench_plugin_root()
    # Published for the sessions this canvas spawns, which is the whole point.
    assert _wait_for(
        lambda: os.environ.get(skill_plugin.WORKBENCH_PLUGIN_DIR_ENV) == root)
    assert os.path.isfile(os.path.join(root, skill_plugin.SKILLS_SUBDIR,
                                       "canvas-toml", "SKILL.md"))
    assert [c[0] for c in calls] == ["clone"], calls
    harness.client.post("/api/canvases/sync/stop",
                        json={"name": "canvas_1"}, headers=GUARD)


def test_the_open_hook_never_blocks_the_request_and_never_fails_it(
        harness, tmp_path, monkeypatch):
    """Bounded on the latency side too: a shallow clone over a dead network sits
    for up to _CLONE_TIMEOUT_S, and the canvas page AWAITS this request before it
    renders anything. So the fetch runs off-thread, and a fetch that explodes
    still leaves a started watcher behind."""
    from fused_render import skill_plugin

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "skillhome"))
    monkeypatch.delenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, raising=False)
    released = threading.Event()

    def slow_and_broken(args, timeout):
        released.wait(5)
        raise OSError("git is not installed")

    monkeypatch.setattr(skill_plugin, "_git", slow_and_broken)
    (harness.root / "canvas_1").mkdir(parents=True)
    (harness.root / "canvas_1" / "canvas.toml").write_text(
        'type = "canvas"\n', encoding="utf-8")

    started = time.time()
    res = harness.client.post("/api/canvases/sync/start",
                              json={"name": "canvas_1"}, headers=GUARD)
    elapsed = time.time() - started
    assert res.status_code == 200, res.text
    assert res.json()["watching"] is True, res.json()
    assert elapsed < 2.0, ("the open request waited for the skills fetch", elapsed)
    released.set()
    harness.client.post("/api/canvases/sync/stop",
                        json={"name": "canvas_1"}, headers=GUARD)


def test_repeated_opens_do_not_refetch(harness, tmp_path, monkeypatch):
    """The page re-arms the watcher whenever a poll finds it dropped, so this
    hook fires far more often than a canvas is opened. The rate limit is what
    keeps that from being a git invocation per re-arm."""
    from fused_render import skill_plugin

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "skillhome"))
    monkeypatch.delenv(skill_plugin.WORKBENCH_PLUGIN_SRC_ENV, raising=False)
    calls = []
    _stub_skills_git(monkeypatch, calls)
    (harness.root / "canvas_1").mkdir(parents=True)
    (harness.root / "canvas_1" / "canvas.toml").write_text(
        'type = "canvas"\n', encoding="utf-8")

    for _ in range(4):
        harness.client.post("/api/canvases/sync/start",
                            json={"name": "canvas_1"}, headers=GUARD)
    assert _wait_for(lambda: skill_plugin.workbench_plugin_root() is not None)
    time.sleep(0.2)  # let any further kicks land
    assert [c[0] for c in calls] == ["clone"], (
        "an open re-fetched instead of honouring the rate limit", calls)
    harness.client.post("/api/canvases/sync/stop",
                        json={"name": "canvas_1"}, headers=GUARD)


# -- the sanctioned manual push (B1) -------------------------------------------
#
# A Claude session working in the clone needs a way to publish a coherent change
# set on purpose. The ONLY safe way is the watcher's own _push(), under its
# _op_lock: that is where the probe+merge+abort guard lives, and it is also what
# keeps the remote from moving behind the watcher's back. The endpoint exists so
# the agent never has a reason to run the raw CLI, whose hazard the last test
# here pins.


def test_manual_push_endpoint_pushes_and_the_next_poll_stays_quiet(
        harness, tmp_path, monkeypatch):
    """The whole point of routing through _push: the sync point moves WITH the
    push, so the remote never looks like it moved on its own. A raw CLI push
    leaves the watcher to discover a changed remote and force-pull the agent's
    own work back down (phantom pull), or to read it as a stale echo."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    # Long debounce: the push under test must be the ENDPOINT's, not the
    # watcher's debounced one firing on the same edit.
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("a-edited-by-claude\n")
    _wait_status(harness, lambda s: s["push_state"] == "pending")

    res = harness.client.post("/api/canvases/sync/push",
                              json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True, body
    assert body["push_state"] == "idle", body
    assert body["push_seq"] == 1, body
    assert body["error"] is None and body["error_detail"] == [], body
    assert [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]

    # The remote did not move on its own, and the push re-baselined — so the
    # polls that follow must do nothing at all.
    time.sleep(0.5)
    after = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert after["pull_seq"] == 0, after
    assert after["echo_seq"] == 0, after
    assert after["merge_seq"] == 0, after
    assert after["push_seq"] == 1, ("the watcher pushed again — the endpoint's "
                                    "push did not become the sync point")
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)
    _ = shims


def test_manual_push_endpoint_returns_the_validation_transcript_verbatim(
        harness, tmp_path, monkeypatch):
    """The reason the agent calls this instead of being told "it failed": the
    CLI prints one line per broken node, and those lines have to land in the
    agent's own transcript so it can iterate without a human relaying them."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    harness.set_scenario({"pull_files": _BASE_FILES,
                          "push_fail_lines": _VALIDATION_LINES})
    (harness.root / "alpha" / "a.py").write_text("broken\n")

    res = harness.client.post("/api/canvases/sync/push",
                              json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False, body
    assert body["push_state"] == "error", body
    for line in _VALIDATION_LINES:
        assert line in body["error_detail"], body
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_manual_push_endpoint_is_refused_while_a_sync_op_holds_the_lock(
        harness, tmp_path, monkeypatch):
    """Push serialization is the module's stated invariant. The endpoint must
    not become a second pusher: with _op_lock held it says so and returns,
    rather than running a concurrent CLI push over the same folder."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    monkeypatch.setattr(canvases_mod, "MANUAL_PUSH_LOCK_WAIT_S", 0.2)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    manager = _manager()
    with manager._op_lock:
        res = harness.client.post("/api/canvases/sync/push",
                                  json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409, res.text
    assert "in flight" in res.json()["error"], res.json()
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_manual_push_endpoint_is_refused_while_the_watcher_is_paused(
        harness, tmp_path, monkeypatch):
    """A pause means someone else owns this folder right now (a re-pull, a
    logout). Pushing into that is exactly the race pause() exists to stop."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    manager = _manager()
    manager.pause()
    try:
        res = harness.client.post("/api/canvases/sync/push",
                                  json={"name": "alpha"}, headers=GUARD)
        assert res.status_code == 409, res.text
        assert "paused" in res.json()["error"], res.json()
        assert not [c for c in harness.calls()
                    if c[:3] == ["workbench", "canvas", "push"]]
    finally:
        manager.resume(rebaseline=False)
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_manual_push_endpoint_validates_its_inputs(harness, tmp_path, monkeypatch):
    """Same guard and same name rule as every sibling route, and a clear 409
    when there is no watcher to push through (rather than quietly starting one:
    a canvas nobody is syncing has no merge base, so a push would be the
    wholesale replace this endpoint exists to avoid)."""
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    assert harness.client.post("/api/canvases/sync/push",
                               json={"name": "alpha"}).status_code == 403
    assert harness.client.post("/api/canvases/sync/push",
                               json={"name": "../etc"}, headers=GUARD).status_code == 400
    res = harness.client.post("/api/canvases/sync/push",
                              json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409, res.text
    assert "not being synced" in res.json()["error"], res.json()
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]


def test_an_out_of_band_cli_push_is_force_pulled_back_by_the_watcher(
        harness, tmp_path, monkeypatch):
    """The hazard the endpoint exists to remove — and the case nothing in this
    suite simulated, which is why none of it was caught.

    A raw `fused workbench canvas push` from inside the clone moves the remote
    and touches no local file, so the watcher cannot tell it from a workbench
    edit. With a clean clone it takes the wholesale force-pull branch: a full
    trash snapshot, every unignored file the push did not publish deleted, and a
    phantom "pulled from workbench" in the UI. Nothing here asserts that is
    GOOD — it pins the behaviour so the reason for the endpoint stays visible.
    """
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    # Wait for the first poll to adopt t1 as the baseline, with the clone clean.
    # A blind sleep here would race: if the out-of-band push lands before that
    # poll, t9 becomes the FIRST baseline and there is no move to react to.
    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t1")

    # Now the out-of-band push: the remote moves with content the watcher never
    # produced a sync point for, while the clone is clean.
    pushed = {**_BASE_FILES, "a.py": "a-pushed-out-of-band\n"}
    shims.set_remote_files(pushed)
    harness.set_scenario({"pull_files": pushed})
    shims.set_manifest("t9")

    status = _wait_status(harness, lambda s: s["pull_seq"] >= 1)
    assert status and status["pull_seq"] >= 1, (
        "an out-of-band push did NOT provoke a force pull — if this ever "
        "becomes true, re-read the endpoint's rationale", status)
    # The force pull ran against the whole clone, so the sync counted a
    # downstream pull that no workbench edit caused.
    assert [c for c in harness.calls()
            if c[:3] == ["workbench", "canvas", "pull"] and "--force" in c]
    assert status["merge_seq"] == 0, "the wholesale branch, not the per-file merge"
    # Two further consequences are the real CLI's, not reproducible here: that
    # `pull --force` DELETES every in-dir file not in the bundle (the stub only
    # writes pull_files), so unpublished agent scratch files are lost; and that
    # each pass takes a trash snapshot, so ~_TRASH_MAX out-of-band pushes evict
    # the entire recoverable history.
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


# -- the server's own push must never re-enter the interception -----------------


_REAL_SHIM = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "_fused_cli.py")


def _stub_fused_package(tmp_path, log):
    """A `fused` package whose `_cli.main` records the argv and succeeds — so a
    push that reaches the REAL CLI is observable, and one that never gets there
    is too."""
    pkg = tmp_path / "fusedstub"
    (pkg / "fused").mkdir(parents=True)
    (pkg / "fused" / "__init__.py").write_text("")
    (pkg / "fused" / "_cli.py").write_text(
        "import json, os, sys\n"
        "def main():\n"
        "    with open(os.environ['REAL_CLI_LOG'], 'a') as f:\n"
        "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "    sys.exit(0)\n")
    _ = log
    return pkg


def test_the_managers_own_push_does_not_route_back_through_the_endpoint(
        harness, tmp_path, monkeypatch):
    """Regression for a live outage on this branch.

    `_push` runs `[*cli.command, "workbench", "canvas", "push", …]`, and on the
    shim path `fused_cli()` resolves cli.command to
    `[sys.executable, _fused_cli.py]` — the file that performs the interception.
    So the manager's own push POSTed back to /api/canvases/sync/push, was
    refused because a push was already running (itself), and recorded that
    refusal as a CLI failure: push_state "error", push_seq stuck at 0, canvas
    sync dead with a Fix-with-Claude button offering to fix nothing.

    The rest of this file cannot catch it: the harness substitutes its stub
    through FUSED_RENDER_FUSED_BIN, so cli.command is never the shim. Here it
    IS, with a stub `fused` package behind it — and FUSED_RENDER_ORIGIN points
    at a server that answers, so without the guard the interception really does
    fire and really does refuse.
    """
    import http.server
    import threading as _threading

    seen_posts = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen_posts.append(self.path)
            out = json.dumps({"error": "a push is already running for this canvas",
                              "code": "busy"}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        real_cli_log = tmp_path / "real-cli.jsonl"
        pkg = _stub_fused_package(tmp_path, real_cli_log)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(pkg), repo]))
        monkeypatch.setenv("REAL_CLI_LOG", str(real_cli_log))
        monkeypatch.setenv("FUSED_RENDER_ORIGIN",
                           "http://127.0.0.1:%d" % server.server_port)
        # The shim path, not the external stub: this is the whole point.
        monkeypatch.delenv("FUSED_RENDER_FUSED_BIN", raising=False)
        monkeypatch.setattr(
            canvases_mod, "fused_cli",
            lambda: fusedcli_mod.FusedCli(command=[sys.executable, _REAL_SHIM],
                                          external=False))
        monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
        monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
        monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)

        harness.log_in()
        (harness.root / "alpha").mkdir(parents=True)
        (harness.root / "alpha" / "canvas.toml").write_text('type = "canvas"\n')
        (harness.root / "alpha" / "a.py").write_text("a1\n")
        # Shims present, so this is the manifest-backed path; the manifest probe
        # is irrelevant here (PULL_POLL_S is long) but availability is what
        # gates seeding and the shim path generally.
        SyncShims(harness, tmp_path, monkeypatch)
        harness.client.post("/api/canvases/sync/start", json={"name": "alpha"},
                            headers=GUARD)
        (harness.root / "alpha" / "a.py").write_text("a2-edited\n")

        status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
        assert status and status["push_seq"] >= 1, (
            "the manager's own push never succeeded", status)
        assert status["push_state"] == "idle", status
        assert status["error"] is None, status
        assert status["error_detail"] == [], status
        assert status["fix_active"] is False, status
        # It reached the real CLI...
        assert real_cli_log.exists(), "the push never reached the fused CLI"
        pushes = [json.loads(ln) for ln in
                  real_cli_log.read_text().splitlines()]
        assert any(a[:3] == ["workbench", "canvas", "push"] for a in pushes), pushes
        # ...and never asked the server to push on its behalf.
        assert seen_posts == [], (
            "the manager's own push re-entered the interception", seen_posts)
        harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"},
                            headers=GUARD)
    finally:
        server.shutdown()
        server.server_close()


def test_a_busy_refusal_is_not_recorded_as_a_push_failure(
        harness, tmp_path, monkeypatch):
    """A genuine double-push (two sessions, or a session racing the watcher) is
    a timing conflict, not a broken canvas. If it landed in push_state "error"
    it would wedge the canvas — `_run` only re-arms `pending` on a fresh change
    and the remote-poll leg is gated on idle — and it would light up the
    Fix-with-Claude button with nothing for Claude to fix."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    monkeypatch.setattr(canvases_mod, "MANUAL_PUSH_LOCK_WAIT_S", 0.2)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    manager = _manager()
    (harness.root / "alpha" / "a.py").write_text("a-edited\n")
    _wait_status(harness, lambda s: s["push_state"] == "pending")

    with manager._op_lock:  # someone else owns the folder this instant
        res = harness.client.post("/api/canvases/sync/push",
                                  json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "busy", res.json()

    after = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert after["push_state"] != "error", after
    assert after["error"] is None, after
    assert after["error_detail"] == [], after
    assert after["fix_active"] is False, after
    # And the Fix endpoint stays unavailable — there is nothing to fix.
    fix = harness.client.post("/api/canvases/fix", json={"name": "alpha"},
                              headers=GUARD)
    assert fix.status_code == 409, fix.text

    # The change is still publishable: a retry works.
    ok = harness.client.post("/api/canvases/sync/push",
                             json={"name": "alpha"}, headers=GUARD)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True, ok.json()
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


# -- what the clone tells the session ------------------------------------------


def test_the_claude_md_names_exactly_the_skills_the_app_hands_over():
    """skill_plugin.WORKBENCH_SKILLS is what validates a candidate plugin root;
    this file is what asks the session to load them. If the two drift, the app
    either rejects a good plugin or seeds a CLAUDE.md naming a skill it never
    handed over."""
    from fused_render import skill_plugin

    text = canvases_mod._CLONE_CLAUDE_MD
    for skill in skill_plugin.WORKBENCH_SKILLS:
        assert "workbench:%s" % skill in text, skill


def test_the_missing_skills_fallback_confines_the_session_to_its_folder():
    """The fallback has to be a BOUNDARY, not just "carry on".

    What actually happened in the field, with the skills absent: the session
    accepted the fallback, went looking for the edge format in the app's own
    internals, and ran `find / -iname "pipeline.md"` and a recursive walk of
    `~/.fused-render` — which permanently wedged every rclone NFS mount on the
    user's machine (a known failure mode in this repo: a recursive walk over
    ~/.fused-render/mounts is the documented mount-killer). Its own summary was
    "I got sidetracked digging through internal app files", i.e. it recognised
    the detour only afterwards — so the text must PREVENT the walk, not nudge.
    """
    text = canvases_mod._CLONE_CLAUDE_MD
    tail = text[text.index("## Skills"):]
    low = tail.lower()
    # Stay in the folder, in as many words.
    assert "do not search outside this folder" in low
    # The three destinations it actually went to, named.
    assert "find" in low and "recursive" in low
    assert "~/.fused-render/mounts" in tail
    assert "fused-render" in low and "internal" in low
    # And why: the mounts are network mounts a walk destroys.
    assert "wedge" in low or "wedges" in low
    # The positive instruction survives — the folder itself is the reference.
    assert "canvas.toml" in tail and "conventions" in low


def test_the_seeded_claude_md_never_hands_the_user_a_shell_command():
    """The reader of this file is a Claude session in a chat pane. It cannot run
    an install command, and the user reading it there is the wrong person to
    hand one to — so the escape hatch is gone entirely rather than softened. The
    app supplies the skills itself (--plugin-dir), and when it cannot, this file
    degrades to the folder's own conventions."""
    text = canvases_mod._CLONE_CLAUDE_MD
    assert "plugin add" not in text
    assert "plugin marketplace" not in text
    assert "STOP" not in text
    assert "fused:canvas-toml" not in text, "the plugin was renamed to workbench"
    # It says what to do instead.
    assert "canvas.toml" in text and "conventions" in text


def test_the_seeded_claude_md_sanctions_the_standard_push():
    """A1/B3. It used to call a manual push "usually redundant", which
    understated it in both directions: the raw CLI push can destroy a concurrent
    workbench edit, and now that the auto-push is held while a session works,
    NOT pushing leaves the work unpublished until the session ends."""
    text = canvases_mod._CLONE_CLAUDE_MD
    assert "fused workbench canvas push ." in text
    assert "usually redundant" not in text
    assert "--no-validate" in text  # says it is refused
    # And the bundled-CLI rule (A4d).
    assert "pip install fused" in text
    assert "python -m fused" in text


def test_the_fix_prompt_tells_the_session_to_push():
    """B5. The old "Do NOT run canvas push" is now wrong: nothing races it, and
    the push is what confirms the fix landed."""
    prompt = canvases_mod._fix_prompt("alpha", ["error: node 'x' has no source"],
                                      "validation failed")
    assert "error: node 'x' has no source" in prompt, "verbatim, per D328"
    assert "fused workbench canvas push ." in prompt
    assert "Do NOT run" not in prompt
    assert "validate" in prompt


def test_the_cli_prompt_note_no_longer_forbids_pushing_in_a_clone():
    """The system-prompt disclosure said "rather than running `canvas push`
    yourself", i.e. the exact opposite of the new contract. A session that
    believes that finishes blind."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_for_cli_note", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    note = agent._fused_cli_note.__doc__ + (
        agent._fused_cli_note() if agent._fused_cli_dir() else "")
    # The docstring keeps the history; the emitted text must not carry the old
    # instruction. Check the emitted text alone.
    os.environ["FUSED_RENDER_FUSED_CLI_DIR"] = "/tmp/fused-bin"
    try:
        text = agent._fused_cli_note()
    finally:
        os.environ.pop("FUSED_RENDER_FUSED_CLI_DIR", None)
    assert text, "the note must render when a wrapper exists"
    assert "rather than running `canvas push` yourself" not in text
    assert "canvas push ." in text
    assert "pip install fused" in text
    assert "python -m fused" in text
    _ = note


# -- a live Claude session holds the auto-push, and shows up in status ----------


class _FakeAgent:
    """Stands in for the claude template's agent module. `live` is the run id
    `_live_run` reports for the folder — "" for "nobody is editing"."""

    def __init__(self, live=""):
        self.live = live
        self.calls = []

    def _live_run(self, file, session_id="", limit=None):
        self.calls.append((file, limit))
        return {"run_id": self.live}


@pytest.fixture()
def fake_agent(monkeypatch):
    agent = _FakeAgent()
    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: agent)
    # No cross-test cache: the module-level loader memoizes, and the per-manager
    # answer is cached for AGENT_LIVE_CACHE_S.
    monkeypatch.setattr(canvases_mod, "AGENT_LIVE_CACHE_S", 0.0)
    return agent


def test_the_auto_push_waits_for_a_live_session_and_then_fires(
        harness, tmp_path, monkeypatch, fake_agent):
    """B4. The debounce measures file quiet, and a session goes quiet for much
    longer than DEBOUNCE_S mid-change-set — thinking, reading, waiting on a
    tool. Pushing then ships a half-done rename. But the watcher must stay the
    BACKSTOP: once the run ends, a still-dirty clone pushes on the next tick, so
    a session that never pushes degrades to today's behaviour, not to a lost
    change set."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    fake_agent.live = "run-abc"
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("half of a rename\n")

    # Dirty and past the debounce, yet no push — held for the live session.
    status = _wait_status(harness, lambda s: s["push_state"] == "pending")
    assert status["push_state"] == "pending", status
    time.sleep(0.5)
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]], \
        "the watcher pushed a change set a live session was still writing"
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["agent_active"] is True

    # The session ends. The clone is still dirty, so the backstop takes over.
    fake_agent.live = ""
    pushed = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert pushed and pushed["push_seq"] >= 1, (
        "the watcher did not resume pushing after the session ended", pushed)
    assert pushed["agent_active"] is False, pushed
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_the_remote_poll_is_held_while_a_session_is_live(
        harness, tmp_path, monkeypatch, fake_agent):
    """Both downstream legs are now held for the length of a session, not just
    the push.

    The earlier rule (D354's note in the watcher) was that the remote poll must
    keep running through a chat or workbench edits would stop arriving. It must
    not: the clone's files moving under a session mid-change-set is what the
    seeded CLAUDE.md and the workbench lock both exist to avoid, and every pull
    was also a `pulling` window the lock engaged on — so a chat of any length
    flickered the embedded workbench read-only every PULL_POLL_S. The accepted
    cost is timing only: a workbench edit made during the session arrives at the
    next pull (which is also step 1 of the push), where the per-file merge folds
    it in with local winning ties.
    """
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    fake_agent.live = "run-abc"
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    # Dirty, so the merge leg is the one that would fire.
    (harness.root / "alpha" / "a.py").write_text("a-local-edit\n")
    _wait_status(harness, lambda s: s["push_state"] == "pending")
    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t1")

    # A workbench edit lands. It must NOT arrive while the session is live.
    shims.set_remote_files({**_BASE_FILES, "b.py": "b-from-workbench\n"})
    shims.set_manifest("t2", {"b": {"hash": "h2", "last_updated": "t2"}})
    seen = []
    for _ in range(20):
        seen.append(harness.client.get(
            "/api/canvases/sync/status?name=alpha").json())
        time.sleep(0.05)
    assert all(s["merge_seq"] == 0 for s in seen), \
        "the remote poll merged into a clone a live session was working in"
    assert (harness.root / "alpha" / "b.py").read_text() == "b1\n"
    # And no lock engagement at all: `pulling` never went true, so the embedded
    # workbench was never flickered read-only by the held leg.
    assert all(s["pulling"] is False for s in seen), seen
    # `_remote` was not rotated past the move either — forgetting it would mean
    # the edit is never applied at all.
    assert (_manager()._remote or {}).get("last_updated") == "t1"

    # Session ends → the held edit arrives.
    fake_agent.live = ""
    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1)
    assert status and status["merge_seq"] >= 1, (
        "the remote poll never resumed after the session ended", status)
    assert (harness.root / "alpha" / "b.py").read_text() == "b-from-workbench\n"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_the_held_poll_resumes_on_the_very_next_tick(
        harness, tmp_path, monkeypatch, fake_agent):
    """`_last_pull_poll` is deliberately NOT stamped while the leg is held. If it
    were, a session ending one moment after a skipped poll would leave the
    workbench's edits waiting up to a whole PULL_POLL_S — with the user watching
    a canvas that visibly does not update."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 3.0)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)  # keep it dirty
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    fake_agent.live = "run-abc"
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("a-local-edit\n")
    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t1")

    shims.set_remote_files({**_BASE_FILES, "b.py": "b-from-workbench\n"})
    shims.set_manifest("t2", {"b": {"hash": "h2", "last_updated": "t2"}})
    # Sit in the held state until well past a full PULL_POLL_S window, so the
    # leg has been due-and-skipped for a while.
    time.sleep(3.5)
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["merge_seq"] == 0

    # Release, and give it far less than PULL_POLL_S to act: a skip that stamped
    # `_last_pull_poll` would push the next poll a fresh 3s out and time out here.
    fake_agent.live = ""
    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1, timeout=1.2)
    assert status and status["merge_seq"] >= 1, (
        "the held leg waited for a fresh PULL_POLL_S instead of the next tick",
        status)
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_the_first_look_baseline_is_adopted_even_while_a_session_is_live(
        harness, tmp_path, monkeypatch, fake_agent):
    """The one exemption from the hold. The first look writes NOTHING to the
    clone — it adopts `_remote` and hashes the disk as the merge base. Gate it
    too and `_base_files` stays None for the whole session, which silently
    degrades every later merge to local-wins wholesale: a workbench edit to a
    file Claude never touched would be discarded instead of folded in."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    fake_agent.live = "run-abc"  # live BEFORE the watcher's first tick
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    assert _wait_for(
        lambda: (_manager()._remote or {}).get("last_updated") == "t1", timeout=3
    ), "the first-look baseline was gated by the live session"
    assert _manager()._base_files is not None, \
        "no merge base adopted — every later merge degrades to local-wins"
    # Adopting is not pulling: nothing was written, so the lock stayed off.
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["pulling"] is False
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_a_poll_that_finds_nothing_never_reports_pulling(
        harness, tmp_path, monkeypatch):
    """`pulling` marks real WRITES only. It used to wrap probe-and-decide, so
    every 10s poll of an unchanged remote registered a full lock engagement that
    the 2s status poll could sample — the workbench flickering read-only for a
    pull that never happened."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.05)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t1")

    # Remote never moves; the leg polls repeatedly and must decide "nothing to
    # do" without ever engaging the lock.
    for _ in range(20):
        assert _manager()._pulling is False
        time.sleep(0.02)
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["pulling"] is False
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_agent_active_tracks_the_live_run(harness, tmp_path, monkeypatch, fake_agent):
    """C1. The lock's signal, straight from the pid-based lookup — and asked
    UNBOUNDED, because the capped scan can miss a live run on a busy machine and
    a missed run means the workbench is silently left editable."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["agent_active"] is False
    fake_agent.live = "run-xyz"
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["agent_active"] is True
    fake_agent.live = ""
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["agent_active"] is False
    # The clone dir is the identity, and the cap is off.
    assert all(call[0] == str(harness.root / "alpha") for call in fake_agent.calls)
    assert all(call[1] is None for call in fake_agent.calls), \
        "the lock must not use the capped scan"
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_a_clean_clone_still_refreshes_the_live_run_cache_from_the_watcher(
        harness, tmp_path, monkeypatch):
    """A clean clone (no pending push, nothing to debounce) used to never call
    agent_run_id() from the watcher thread at all — that only happened inside
    the dirty+debounce branch. So the unbounded RUNS scan (a meta.json read
    per run dir, deliberately not result-capped) ran on the REQUEST thread
    instead, roughly every other `/api/canvases/sync/status` poll (the cache's
    short TTL is close to the poll interval). The watcher must refresh it on
    every tick regardless of dirty state, so the request thread almost always
    finds a warm cache."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    agent = _FakeAgent(live="run-xyz")
    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: agent)
    monkeypatch.setattr(canvases_mod, "AGENT_LIVE_CACHE_S", 0.3)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    # The clone is clean (freshly seeded) — nothing to debounce or push, yet
    # the watcher's own ticks must still be calling into the agent module.
    assert _wait_for(lambda: len(agent.calls) >= 2, timeout=3), (
        "the watcher never refreshed the live-run cache on a clean clone", agent.calls)

    # A status poll right after must find a warm cache, not trigger its own
    # scan: the call count settles rather than growing 1:1 with polls.
    before = len(agent.calls)
    for _ in range(5):
        harness.client.get("/api/canvases/sync/status?name=alpha").json()
    time.sleep(0.05)
    assert len(agent.calls) - before <= 1, (
        "status() polling itself is paying for the scan instead of reading "
        "the watcher-refreshed cache", agent.calls)
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_agent_active_is_false_when_nothing_is_syncing(harness, tmp_path, monkeypatch):
    """A page that polls a canvas with no watcher must still be told the lock is
    off — otherwise a dropped watcher or a server restart mid-lock leaves the
    workbench read-only with nothing left to release it."""
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    body = harness.client.get("/api/canvases/sync/status?name=alpha").json()
    assert body["watching"] is False
    assert body["agent_active"] is False
    assert body["pulling"] is False


# -- `pulling`: the lock's OTHER signal (task C) --------------------------------
#
# The frontend lock no longer engages on a live Claude run at all (a "hi" with
# no edits must never lock the workbench) — only on an actual sync op moving
# the clone's files: a push in flight (push_state pending/pushing, unchanged)
# or a pull/merge in flight, which needed a new signal since nothing in
# status() reported it before. `pulling` is that signal: true for exactly the
# duration of _poll_remote's force-pull/merge leg or the legacy
# _pull_if_remote_changed leg, both already serialized under _op_lock.


def test_status_reports_pulling_during_a_clean_force_pull(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    harness.log_in()
    harness.set_scenario({"pull_files": _BASE_FILES})
    harness.client.post("/api/canvases/clone", json={"name": "alpha"}, headers=GUARD)
    shims = SyncShims(harness, tmp_path, monkeypatch)
    shims.set_manifest("t1")
    shims.set_remote_files(_BASE_FILES)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    _wait_for(lambda: getattr(_manager(), "_remote", None) is not None)

    # Remote moves, clean clone → the force-pull branch. A slow pull (real
    # canvas_pull.py can legitimately take a moment) gives a window to observe
    # `pulling` go True while it runs.
    harness.set_scenario({
        "pull_files": {**_BASE_FILES, "remote_udf.py": "print('x')\n"},
        "pull_delay": 0.6,
    })
    shims.set_manifest("t2")

    assert _wait_for(
        lambda: harness.client.get(
            "/api/canvases/sync/status?name=alpha").json()["pulling"] is True,
        timeout=3,
    ), "status never reported pulling during the force pull"
    # Widen PULL_POLL_S NOW, with the pull already in flight (`_last_pull_poll`
    # was just stamped for THIS cycle) — so the very next cycle, which would
    # otherwise probe again within 0.1s and race the post-pull assertion
    # below, is pushed well out of the way instead.
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)
    # pull_seq increments as soon as the force-pull itself lands, but the leg
    # keeps `pulling` True through its own post-pull recheck dry-run too — so
    # wait for `pulling` to clear, not just for pull_seq, or this races the
    # tail end of the same leg.
    status = _wait_status(harness, lambda s: s["pull_seq"] >= 1 and s["pulling"] is False)
    assert status and status["pull_seq"] >= 1, status
    assert status["pulling"] is False, status
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_pulling_covers_the_merges_writes_but_not_its_zip_download(
        harness, tmp_path, monkeypatch):
    """`pulling` marks writes, and the merge's probe-and-decide reaches deeper
    than the leg boundary: the bundle download is a network op that can fail, and
    it precedes every write. Marking it held the embedded workbench read-only for
    the whole download and for merges that then wrote nothing — the same flicker
    the window was introduced to remove, one layer in."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)  # keep the clone dirty
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)

    # Observed from INSIDE the merge, so neither assertion depends on catching a
    # window with an HTTP poll.
    during_download = []
    during_write = []
    real_zip = canvases_mod._SyncManager._download_zip
    real_backup = canvases_mod._SyncManager._backup_to

    def watched_zip(self, revision_id):
        during_download.append(self._pulling)
        return real_zip(self, revision_id)

    def slow_backup(self, trash, rel, data):
        during_write.append(self._pulling)
        time.sleep(0.4)  # hold the write open past one status poll
        return real_backup(self, trash, rel, data)

    monkeypatch.setattr(canvases_mod._SyncManager, "_download_zip", watched_zip)
    monkeypatch.setattr(canvases_mod._SyncManager, "_backup_to", slow_backup)

    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")
    shims.set_remote_files({**_BASE_FILES, "b.py": "b2-remote\n"})
    shims.set_manifest("t2")

    assert _wait_for(
        lambda: harness.client.get(
            "/api/canvases/sync/status?name=alpha").json()["pulling"] is True,
        timeout=3,
    ), "status never reported pulling while the merge was writing"
    status = _wait_status(harness, lambda s: s["merge_seq"] >= 1)
    assert status and status["merge_seq"] >= 1, status
    assert status["pulling"] is False, status
    assert during_download and all(v is False for v in during_download), \
        ("the lock engaged for the bundle download, before any write",
         during_download)
    assert during_write and all(v is True for v in during_write), during_write
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_a_merge_that_writes_nothing_never_reports_pulling(
        harness, tmp_path, monkeypatch):
    """The common shape while a session works: the remote moved the same file the
    session is editing, so every per-file decision goes to local. The merge
    reconciles `_remote` and touches not one byte — it must cost no lock
    engagement, or a flaky remote flickers the workbench every PULL_POLL_S."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)  # keep the clone dirty
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    seen = []
    real_zip = canvases_mod._SyncManager._download_zip

    def watched_zip(self, revision_id):
        out = real_zip(self, revision_id)
        seen.append(self._pulling)
        return out

    monkeypatch.setattr(canvases_mod._SyncManager, "_download_zip", watched_zip)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)

    # Local edit to a.py, and the remote moved a.py too — local wins, nothing to
    # apply; b.py is identical on both sides, so it is a base refresh, not a write.
    (harness.root / "alpha" / "a.py").write_text("a-local\n", encoding="utf-8")
    _wait_status(harness, lambda s: s["push_state"] == "pending")
    shims.set_remote_files({**_BASE_FILES, "a.py": "a-from-workbench\n"})
    shims.set_manifest("t2")

    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t2",
                     timeout=3), "the merge never reconciled the remote"
    assert seen, "the merge never ran"
    assert all(v is False for v in seen), \
        ("the lock engaged for a merge that wrote nothing", seen)
    assert (harness.root / "alpha" / "a.py").read_text() == "a-local\n"
    assert _manager()._pulling is False
    assert harness.client.get(
        "/api/canvases/sync/status?name=alpha").json()["pulling"] is False
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_a_liveness_lookup_failure_reads_as_not_live(harness, tmp_path, monkeypatch):
    """"Cannot tell" has to mean "not live". The alternative is a lock that
    never releases, which is worse than one that never engages — the user can
    always stop editing, but they cannot un-stick a read-only pane."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(canvases_mod, "AGENT_LIVE_CACHE_S", 0.0)

    class _Broken:
        def _live_run(self, file, session_id="", limit=None):
            raise RuntimeError("RUNS is gone")

    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: _Broken())
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("edited\n")
    # Not live → the watcher still pushes, i.e. the sync did not seize up.
    status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert status and status["push_seq"] >= 1, status
    assert status["agent_active"] is False
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_no_agent_module_at_all_does_not_stop_the_sync(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: None)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    (harness.root / "alpha" / "a.py").write_text("edited\n")
    status = _wait_status(harness, lambda s: s["push_seq"] >= 1)
    assert status and status["push_seq"] >= 1, status
    assert status["agent_active"] is False
    harness.client.post("/api/canvases/sync/stop", json={"name": "alpha"}, headers=GUARD)


def test_the_real_agent_module_answers_for_a_folder(harness, tmp_path, monkeypatch):
    """The fakes above pin canvases.py's logic; this pins the WIRING — that
    `claude_spawn.load_agent()` really resolves and its `_live_run` really takes
    the unbounded `limit`. A signature drift in the template would otherwise
    only show up as a lock that never engages, in production."""
    canvases_mod._AGENT_MOD = None
    canvases_mod._AGENT_MOD_TRIED = False
    try:
        agent = canvases_mod._agent_module()
        assert agent is not None, "the claude agent module did not load"
        assert agent._live_run(str(tmp_path), limit=None) == {"run_id": ""}
    finally:
        canvases_mod._AGENT_MOD = None
        canvases_mod._AGENT_MOD_TRIED = False
