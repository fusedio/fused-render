"""Tests for the deploy surface (fused_render/deploy.py): /api/deploy/config,
/api/deploy, /api/deploy/revoke, /api/deploy/status, /api/deploy/shares.

The fused CLI is substituted with a stub script through the same seam the
runtime uses (FUSED_RENDER_FUSED_BIN — a compound "python stub.py" command):
the stub records every invocation (argv, OPENFUSED_ENV, the bundle's files at
call time) to a log and answers each `share <verb>` from a scenario JSON, so
the tests assert both the orchestration (which verbs ran, against which env,
with which flags) and the parsed results — without any real fused install or
network. FUSED_RENDER_HOME / OPENFUSED_ENVS_FILE are redirected to tmp dirs so
nothing touches the real stores.
"""
import json
import sys

from fastapi.testclient import TestClient

import fused_render.deploy as deploy_mod
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes

# Answers each `fused share <verb>` from the FUSED_STUB_SCENARIO json file and
# appends {argv, env, bundle_files} to FUSED_STUB_LOG. A verb missing from the
# scenario exits 1 with a click-style "Error: ..." on stderr — how the tests
# simulate an unreachable env / CLI failure.
STUB = """\
import json, os, sys

with open(os.environ["FUSED_STUB_SCENARIO"], "r") as f:
    scenario = json.load(f)
verb = sys.argv[2] if len(sys.argv) > 2 else ""
bundle = next((a for a in sys.argv[3:] if os.path.isdir(a)), None)
entry = {
    "argv": sys.argv[1:],
    "env": os.environ.get("OPENFUSED_ENV"),
    "bundle_files": sorted(os.listdir(bundle)) if bundle else None,
}
with open(os.environ["FUSED_STUB_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if verb not in scenario:
    sys.stderr.write("Error: stub has no answer for %r\\n" % verb)
    sys.exit(1)
print(json.dumps(scenario[verb]))
"""

ENVS = {
    "default": "prod",
    "envs": {
        "cloud": {"name": "cloud", "backend": "fused", "org": "acme", "env": "e1"},
        "prod": {"name": "prod", "backend": "aws", "region": "us-west-2"},
        "dev": {"name": "dev", "backend": "local"},
    },
}


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "home"
        monkeypatch.setenv("FUSED_RENDER_HOME", str(self.home))
        monkeypatch.delenv("OPENFUSED_ENV", raising=False)

        envs_file = tmp_path / "envs.json"
        envs_file.write_text(json.dumps(ENVS), encoding="utf-8")
        monkeypatch.setenv("OPENFUSED_ENVS_FILE", str(envs_file))

        stub = tmp_path / "fused_stub.py"
        stub.write_text(STUB, encoding="utf-8")
        monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", f"{sys.executable} {stub}")

        self.log = tmp_path / "stub-log.jsonl"
        monkeypatch.setenv("FUSED_STUB_LOG", str(self.log))
        self.scenario_file = tmp_path / "scenario.json"
        self.set_scenario({})
        monkeypatch.setenv("FUSED_STUB_SCENARIO", str(self.scenario_file))

        # A deployable page with one runPython dependency beside it.
        self.page = tmp_path / "view.html"
        self.page.write_text(
            "<html><head></head><body><script>"
            "fused.runPython('./sine.py', {});"
            "</script></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "sine.py").write_text("def main():\n    return 1\n", encoding="utf-8")

        self.client = TestClient(create_app(start_dir=str(tmp_path)))

    def set_scenario(self, scenario: dict) -> None:
        self.scenario_file.write_text(json.dumps(scenario), encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def pointer(self) -> dict | None:
        path = self.home / "deployments.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(str(self.page))


def _harness(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# -- config -------------------------------------------------------------------


def test_config_lists_hosted_envs_and_defaults_to_fused_backend(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    cfg = h.client.get("/api/deploy/config").json()
    # local envs are never eligible; the fused-backend env wins the default
    # even though the store default is the aws env.
    assert [(e["name"], e["backend"]) for e in cfg["envs"]] == [
        ("cloud", "fused"),
        ("prod", "aws"),
    ]
    assert cfg["default_env"] == "cloud"
    assert cfg["cli"]["found"] is True


def test_config_default_honors_ambient_openfused_env(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENFUSED_ENV", "prod")
    assert h.client.get("/api/deploy/config").json()["default_env"] == "prod"


def test_config_reports_missing_cli(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy_mod, "fused_command", lambda: None)
    cli = h.client.get("/api/deploy/config").json()["cli"]
    assert cli["found"] is False
    assert "fused-render[fused]" in cli["install_hint"]


def test_config_with_no_envs_file(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENFUSED_ENVS_FILE", str(tmp_path / "missing.json"))
    cfg = h.client.get("/api/deploy/config").json()
    assert cfg["envs"] == []
    assert cfg["default_env"] is None


# -- deploy -------------------------------------------------------------------


def test_deploy_creates_public_share_and_stores_pointer(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    record = resp.json()
    assert record["token"] == "abc123"
    assert record["url"] == "https://serve.example/abc123"
    assert record["status"] == "active"
    assert record["env"] == "cloud"
    assert record["backend"] == "fused"
    assert record["entrypoints"] == ["sine"]

    (call,) = h.calls()
    assert call["argv"][0] == "share" and call["argv"][1] == "create"
    assert "--public" in call["argv"]
    assert call["env"] == "cloud"
    # The bundle handed to the CLI was a real export at call time.
    assert call["bundle_files"] == ["code", "manifest.json", "page.html"]

    assert h.pointer()["token"] == "abc123"


def test_redeploy_active_mount_repoints_same_token(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # AWS-style repoint output: token but no url — the last-known URL survives.
    h.set_scenario(
        {
            "list": [{"token": "abc123", "status": "active"}],
            "repoint": {"token": "abc123", "status": "active"},
        }
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"] == "abc123"
    assert resp.json()["url"] == "https://serve.example/abc123"

    verbs = [c["argv"][1] for c in h.calls()]
    assert verbs == ["create", "list", "repoint"]
    repoint = h.calls()[-1]
    assert repoint["argv"][2] == "abc123"
    assert "--all" in h.calls()[1]["argv"]


def test_redeploy_revoked_tombstone_revives_same_token(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario(
        {
            "list": [{"token": "abc123", "status": "revoked"}],
            "recreate": {"token": "abc123", "status": "active"},
            "repoint": {"token": "abc123", "status": "active"},
        }
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    calls = h.calls()
    verbs = [c["argv"][1] for c in calls]
    assert verbs == ["create", "list", "recreate", "repoint"]
    assert calls[2]["argv"][2:4] == ["abc123", "--same-token"]


def test_redeploy_absent_mount_falls_back_to_fresh_create(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # e.g. after an infra teardown: the token is gone entirely — no tombstone
    # to revive, so a fresh create mints a new link.
    h.set_scenario(
        {
            "list": [],
            "create": {"token": "new456", "url": "https://serve.example/new456", "status": "active"},
        }
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"] == "new456"
    assert h.pointer()["url"] == "https://serve.example/new456"


def test_deploy_to_different_env_creates_fresh_and_repoints_pointer(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # A different env never repoints the other env's mount — fresh create, and
    # the AWS backend returns no url, so the pointer's url resets to null
    # (the old link belongs to the other env's mount).
    h.set_scenario({"create": {"token": "aws789", "status": "active"}})
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "prod"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"] == "aws789"
    assert resp.json()["url"] is None
    assert resp.json()["backend"] == "aws"
    assert [c["argv"][1] for c in h.calls()] == ["create", "create"]
    assert h.calls()[-1]["env"] == "prod"


def test_deploy_rejects_non_hosted_env(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "dev"}, headers=FUSED)
    assert resp.status_code == 400
    assert "hosted" in resp.json()["error"]
    assert h.calls() == []


def test_deploy_export_error_is_400(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.page.write_text(
        "<html><body><script>fused.writeFile('/x', 'y');</script></body></html>",
        encoding="utf-8",
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 400
    assert "writeFile" in resp.json()["error"]
    assert h.calls() == []  # nothing was shelled out for an unexportable page


def test_deploy_surfaces_cli_error_verbatim(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario({})  # stub answers nothing -> exit 1 with "Error: ..." stderr
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 400
    # click's "Error: " prefix is stripped; the CLI's own message reaches the UI.
    assert resp.json()["error"].startswith("stub has no answer")


def test_deploy_requires_fused_header(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"})
    assert resp.status_code == 403


# -- status / revoke / shares ---------------------------------------------------


def test_status_without_reconcile_never_shells_out(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    before = len(h.calls())

    resp = h.client.get("/api/deploy/status", params={"path": str(h.page)})
    assert resp.status_code == 200
    assert resp.json()["deployment"]["token"] == "abc123"
    assert resp.json()["reconciled"] is False
    assert len(h.calls()) == before  # opening a preview must not spawn the CLI


def test_status_for_undeployed_page(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.get("/api/deploy/status", params={"path": str(h.page)})
    assert resp.json() == {"deployment": None, "reconciled": True}


def test_status_reconcile_flips_to_revoked(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # Revoked out-of-band via the CLI: `share list` is truth.
    h.set_scenario({"list": [{"token": "abc123", "status": "revoked"}]})
    resp = h.client.get("/api/deploy/status", params={"path": str(h.page), "reconcile": "1"})
    assert resp.json()["deployment"]["status"] == "revoked"
    assert resp.json()["reconciled"] is True
    assert h.pointer()["status"] == "revoked"


def test_status_reconcile_unreachable_env_keeps_last_known(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario({})  # list fails -> env unreachable
    resp = h.client.get("/api/deploy/status", params={"path": str(h.page), "reconcile": "1"})
    assert resp.json()["deployment"]["status"] == "active"
    assert resp.json()["reconciled"] is False


def test_revoke_flips_pointer(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario({"revoke": {"token": "abc123", "status": "revoked"}})
    resp = h.client.post("/api/deploy/revoke", json={"page": str(h.page)}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"
    # The pointer is kept (not cleared) so a later deploy revives the same URL.
    assert h.pointer()["token"] == "abc123"
    assert h.pointer()["status"] == "revoked"
    assert h.calls()[-1]["argv"][1:3] == ["revoke", "abc123"]


def test_revoke_without_deployment_is_400(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.post("/api/deploy/revoke", json={"page": str(h.page)}, headers=FUSED)
    assert resp.status_code == 400


def test_shares_joins_mounts_to_local_pages(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # Neither backend's `share list` carries a URL — ours comes back from the
    # pointer; the foreign mount (CLI-created / another machine) has none.
    h.set_scenario(
        {
            "list": [
                {"token": "zzz999", "status": "active"},
                {"token": "abc123", "status": "active"},
                {"token": "old111", "status": "revoked"},
            ]
        }
    )
    resp = h.client.get("/api/deploy/shares", params={"env": "cloud"})
    assert resp.status_code == 200, resp.text
    mounts = resp.json()["mounts"]
    # Local pages first, live before revoked.
    assert [(m["token"], m["page"]) for m in mounts] == [
        ("abc123", str(h.page)),
        ("zzz999", None),
        ("old111", None),
    ]
    assert mounts[0]["url"] == "https://serve.example/abc123"
    assert mounts[1]["url"] is None


def test_shares_cli_failure_is_400(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario({})
    resp = h.client.get("/api/deploy/shares", params={"env": "cloud"})
    assert resp.status_code == 400
