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
    "argv0": sys.argv[0],
    "env": os.environ.get("OPENFUSED_ENV"),
    "pythonhome": os.environ.get("PYTHONHOME"),
    "pythonpath": os.environ.get("PYTHONPATH"),
    "bundle_files": sorted(os.listdir(bundle)) if bundle else None,
}
with open(os.environ["FUSED_STUB_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if verb not in scenario:
    sys.stderr.write("Error: stub has no answer for %r\\n" % verb)
    sys.exit(1)
print(json.dumps(scenario[verb]))
"""

# The same record/answer behavior as STUB, packaged as a fake `fused` package
# whose `_cli.main()` the real fused_render/_fused_cli.py shim invokes — the
# in-interpreter autodetection path (the packaged .app's shape: importable
# package, no console script).
FAKE_FUSED_CLI = "def main():\n" + "".join(
    "    " + line + "\n" for line in STUB.splitlines()
)

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

        # Isolate the login signal from any real ~/.openfused credentials.
        self.creds = tmp_path / "fused-cloud-credentials.json"
        monkeypatch.setenv("OPENFUSED_FUSED_CLOUD_CREDENTIALS", str(self.creds))

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


def test_config_reports_missing_cli_as_installable(tmp_path, monkeypatch):
    # The pin is a code constant (never package metadata, which is absent on
    # source runs and stale on pre-extra editable installs), so a missing CLI
    # on a pip-capable 3.11+ interpreter is always one-click installable.
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy_mod, "fused_cli", lambda: None)
    cli = h.client.get("/api/deploy/config").json()["cli"]
    assert cli["found"] is False
    assert cli["installable"] is True
    assert cli["reason"] is None
    assert "fused-render[fused]" in cli["install_hint"]


def test_config_missing_cli_without_pip_names_the_workaround(tmp_path, monkeypatch):
    # An embedded/packaged interpreter (no pip) can't install into itself —
    # the reason must route the user to FUSED_RENDER_FUSED_BIN instead.
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy_mod, "fused_cli", lambda: None)
    monkeypatch.setattr(deploy_mod, "_pip_available", lambda: False)
    cli = h.client.get("/api/deploy/config").json()["cli"]
    assert cli["installable"] is False
    assert "FUSED_RENDER_FUSED_BIN" in cli["reason"]


# -- CLI resolution: one explicit override, one autodetection, nothing else ----


def test_no_override_and_not_importable_means_no_cli(tmp_path, monkeypatch):
    # With the override unset and `fused` not importable in the server's
    # interpreter, there is NO fallback — no venv-bin scan, no PATH lookup,
    # no well-known locations. A CLI runs only because the user's own
    # interpreter has the package or the user explicitly pointed at one.
    _harness(tmp_path, monkeypatch)
    monkeypatch.delenv("FUSED_RENDER_FUSED_BIN")
    real_find_spec = deploy_mod.importlib.util.find_spec
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name, *a, **k: None if name == "fused" else real_find_spec(name, *a, **k),
    )
    assert deploy_mod.fused_cli() is None


def test_importable_fused_autodetects_via_shim_and_deploys(tmp_path, monkeypatch):
    # The one autodetected source: a `fused` package importable in the
    # server's interpreter runs through [sys.executable, _fused_cli.py] —
    # the packaged .app's shape (baked-in package, no console script). The
    # child inherits the interpreter env untouched (PYTHONPATH here is what
    # lets it import the fake package — inside the .app the analog is the
    # bundle's PYTHONHOME).
    h = _harness(tmp_path, monkeypatch)
    fake_root = tmp_path / "fakepkg"
    (fake_root / "fused").mkdir(parents=True)
    (fake_root / "fused" / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "fused" / "_cli.py").write_text(FAKE_FUSED_CLI, encoding="utf-8")
    monkeypatch.delenv("FUSED_RENDER_FUSED_BIN")
    monkeypatch.syspath_prepend(str(fake_root))  # parent: find_spec sees it
    monkeypatch.setenv("PYTHONPATH", str(fake_root))  # child: import sees it

    cli = deploy_mod.fused_cli()
    assert cli is not None and cli.external is False
    assert cli.command[0] == sys.executable
    assert cli.command[1].endswith("_fused_cli.py")

    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == "https://serve.example/abc123"
    (call,) = h.calls()
    assert call["argv0"] == "fused"  # the shim renamed argv[0] for click
    assert call["argv"][:2] == ["share", "create"]
    assert call["bundle_files"] == ["code", "manifest.json", "page.html"]


def test_setup_cli_hint_names_the_bundle_wrapper_when_frozen(tmp_path, monkeypatch):
    # In the packaged .app, one-time setup guidance must point at the bundle's
    # own CLI wrapper (Contents/Resources/bin/fused — under Resources because
    # a script in Contents/MacOS breaks the codesign bundle seal), resolved
    # relative to sys.executable (Contents/MacOS/python).
    macos = tmp_path / "FusedRender.app" / "Contents" / "MacOS"
    wrapper = tmp_path / "FusedRender.app" / "Contents" / "Resources" / "bin" / "fused"
    macos.mkdir(parents=True)
    wrapper.parent.mkdir(parents=True)
    (macos / "python").write_text("", encoding="utf-8")
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(deploy_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(deploy_mod.sys, "executable", str(macos / "python"))
    assert deploy_mod._setup_cli_hint() == str(wrapper)

    # No wrapper on disk (an older .app build) -> fall back to plain "fused".
    wrapper.unlink()
    assert deploy_mod._setup_cli_hint() == "fused"


def test_setup_cli_hint_is_plain_fused_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delattr(deploy_mod.sys, "frozen", raising=False)
    assert deploy_mod._setup_cli_hint() == "fused"


def test_external_override_scrubs_interpreter_env(tmp_path, monkeypatch):
    # A FUSED_RENDER_FUSED_BIN CLI is an external interpreter: the packaged
    # app's bundle-scoped PYTHONHOME/PYTHONPATH must not leak into it (they
    # would break any other Python). The stub records what it saw.
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("PYTHONHOME", "/bundle/Contents/Resources")
    monkeypatch.setenv("PYTHONPATH", "/bundle/Contents/Resources/lib")
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    (call,) = h.calls()
    assert call["pythonhome"] is None
    assert call["pythonpath"] is None
    assert call["env"] == "cloud"  # OPENFUSED_ENV still targets the pick


def test_pinned_requirement_matches_pyproject_extra():
    # deploy.PINNED_FUSED_REQUIREMENT is the in-code source of the pin; the
    # pyproject [fused] extra must reference the SAME wheel or a wheel install
    # and the one-click install would land different builds.
    import pathlib

    import pytest

    tomllib = pytest.importorskip("tomllib")
    pyproject = pathlib.Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    (extra_spec,) = data["project"]["optional-dependencies"]["fused"]
    extra_requirement = extra_spec.split(";", 1)[0].strip()
    assert extra_requirement == deploy_mod.PINNED_FUSED_REQUIREMENT


def test_pointer_store_key_is_canonicalized(tmp_path, monkeypatch):
    # The pointer store keys on the canonical absolute path (os.path.abspath), so two
    # spellings of the same file resolve to one pointer — status/dot/redeploy never miss.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    rec = {
        "page": str(tmp_path / "d" / "p.html"),
        "env": "e",
        "backend": "aws",
        "token": "t",
        "url": None,
        "status": "active",
        "entrypoints": [],
        "updated_at": "now",
    }
    # Write under a non-canonical spelling (a `..` segment) …
    deploy_mod.set_deployment(str(tmp_path / "d" / "sub" / ".." / "p.html"), rec)
    # … read back under a different (canonical) spelling of the same file.
    assert deploy_mod.get_deployment(str(tmp_path / "d" / "p.html")) == rec
    # The on-disk key is the canonical abspath, not the raw spelling.
    store = json.loads((tmp_path / "home" / "deployments.json").read_text(encoding="utf-8"))
    assert str(tmp_path / "d" / "p.html") in store
    assert "sub" not in " ".join(store)


def test_install_invokes_pip_with_the_pinned_requirement(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    ran: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        return _Proc()

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    resp = h.client.post("/api/deploy/install", headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "requirement": deploy_mod.PINNED_FUSED_REQUIREMENT}
    (cmd,) = ran
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
    assert cmd[4] == deploy_mod.PINNED_FUSED_REQUIREMENT


def test_config_reports_fused_login_state(tmp_path, monkeypatch):
    # Presence of the CLI's own credentials file = a `fused cloud login`
    # happened; the modal warns before a doomed managed-env deploy otherwise.
    h = _harness(tmp_path, monkeypatch)
    assert h.client.get("/api/deploy/config").json()["fused_logged_in"] is False
    h.creds.write_text("{}", encoding="utf-8")
    assert h.client.get("/api/deploy/config").json()["fused_logged_in"] is True


def test_cli_login_errors_name_the_bundled_wrapper(tmp_path, monkeypatch):
    # The CLI's login errors say `fused cloud login`, which doesn't resolve
    # inside the packaged app — the wrapper's real path is appended so the
    # instruction is runnable as printed. Non-login errors stay untouched.
    monkeypatch.setattr(deploy_mod, "_setup_cli_hint", lambda: "/App/Contents/Resources/bin/fused")
    message = deploy_mod._cli_error(
        "Error: Not logged in to Fused. Run `fused cloud login` first.\n", "fallback"
    )
    assert message.endswith("(in this app: /App/Contents/Resources/bin/fused cloud login)")
    assert deploy_mod._cli_error("Error: no mount 'x'\n", "fallback") == "no mount 'x'"


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


def test_deploy_bundles_included_file_and_persists_selection(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    resp = h.client.post(
        "/api/deploy",
        json={"page": str(h.page), "env": "cloud", "include": ["data.csv"], "exclude": []},
        headers=FUSED,
    )
    assert resp.status_code == 200, resp.text
    record = resp.json()
    # The selection is persisted on the record itself (no sidecar), so a reopened
    # modal reloads it.
    assert record["include"] == ["data.csv"]
    assert record["exclude"] == []
    assert h.pointer()["include"] == ["data.csv"]

    # The included file was actually bundled as an asset alongside the auto scan.
    (call,) = h.calls()
    assert call["bundle_files"] == ["assets", "code", "manifest.json", "page.html"]


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


def test_failed_revive_with_compensation_flips_a_stale_active_pointer(tmp_path, monkeypatch):
    # Pointer reads "active" but the mount was revoked out-of-band; the
    # redeploy discovers the tombstone, revives it (recreate ok), fails to
    # publish (repoint missing from the scenario -> CLI error), and the
    # compensating revoke lands. The pointer must then read "revoked" — not
    # keep a green dot on a link that is verifiably down.
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert h.pointer()["status"] == "active"

    h.set_scenario(
        {
            "list": [{"token": "abc123", "status": "revoked"}],
            "recreate": {"token": "abc123", "status": "active"},
            # no "repoint" -> that verb fails
            "revoke": {"token": "abc123", "status": "revoked"},
        }
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 400
    verbs = [c["argv"][1] for c in h.calls()]
    assert verbs == ["create", "list", "recreate", "repoint", "revoke"]
    assert h.pointer()["status"] == "revoked"


def test_failed_revive_and_failed_compensation_persists_active_and_names_it(tmp_path, monkeypatch):
    # recreate succeeds, repoint fails, AND the compensating revoke also fails:
    # the mount is LIVE with old content. The pointer must read active (the dot
    # must match reality — not the pre-deploy revoked state), and the error must
    # name the token so the user can revoke manually.
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # Revoked tombstone; recreate ok; repoint + revoke both missing -> both fail.
    h.set_scenario(
        {
            "list": [{"token": "abc123", "status": "revoked"}],
            "recreate": {"token": "abc123", "status": "active"},
        }
    )
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 400
    assert "abc123" in resp.json()["error"]
    assert "LIVE" in resp.json()["error"] or "live" in resp.json()["error"]
    verbs = [c["argv"][1] for c in h.calls()]
    assert verbs == ["create", "list", "recreate", "repoint", "revoke"]
    # The mount is live -> the pointer reflects active, not a stale revoked.
    assert h.pointer()["status"] == "active"


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


def test_fresh_create_with_new_token_never_keeps_the_old_url(tmp_path, monkeypatch):
    # AWS-style create output carries no url. When the token CHANGED (absent
    # mount -> fresh create), the old pointer's url must be dropped, not kept —
    # copy/open would otherwise point at a link that no longer matches the
    # live mount. (The keep-last-known fallback is for same-token repoints.)
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario({"list": [], "create": {"token": "new456", "status": "active"}})
    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"] == "new456"
    assert resp.json()["url"] is None
    assert h.pointer()["url"] is None


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


def test_deploy_rejects_non_string_page_with_400_not_500(tmp_path, monkeypatch):
    # A truthy non-string page (JSON number/array) must not reach os.path.isabs
    # (which raises TypeError -> 500); it stays a clean 400.
    h = _harness(tmp_path, monkeypatch)
    for bad in (123, ["/x.html"], {"p": 1}):
        resp = h.client.post("/api/deploy", json={"page": bad, "env": "cloud"}, headers=FUSED)
        assert resp.status_code == 400, (bad, resp.status_code)
    assert h.calls() == []


def test_deploy_refuses_to_overwrite_a_corrupt_store(tmp_path, monkeypatch):
    # A corrupt deployments.json must NOT collapse to {} and get overwritten —
    # that would drop every other page's pointer, orphaning live mounts. The
    # deploy aborts with a clear error and the file is left untouched.
    h = _harness(tmp_path, monkeypatch)
    h.home.mkdir(parents=True, exist_ok=True)
    store = h.home / "deployments.json"
    store.write_text('{"other.html": {"env": "cloud", "token": "keep-me"', encoding="utf-8")  # truncated
    h.set_scenario({"create": {"token": "abc123", "url": "https://x/abc123", "status": "active"}})

    resp = h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert resp.status_code == 400
    assert "not valid JSON" in resp.json()["error"]
    # Untouched — the other page's record is not clobbered, and no CLI ran.
    assert store.read_text(encoding="utf-8").startswith('{"other.html"')
    assert h.calls() == []


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
    assert resp.json() == {"deployment": None, "reconciled": True, "live": None}


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
    assert resp.json()["live"] == "revoked"
    assert h.pointer()["status"] == "revoked"


def test_status_reconcile_reports_absent_mount_distinctly(tmp_path, monkeypatch):
    # An absent mount (e.g. after an infra teardown) persists as "revoked" (the
    # link IS down) but must be distinguishable from a revoked tombstone: a
    # tombstone redeploys to the SAME URL, an absent mount gets a NEW one — the
    # modal's "restore URL" promise branches on `live`.
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario({"list": []})
    resp = h.client.get("/api/deploy/status", params={"path": str(h.page), "reconcile": "1"})
    assert resp.json()["live"] == "absent"
    assert resp.json()["deployment"]["status"] == "revoked"
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


def test_revoke_by_token_covers_untracked_mounts(tmp_path, monkeypatch):
    # The Preferences page revokes by env+token — including mounts with no
    # local pointer (deployed by the CLI / another machine).
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario({"revoke": {"token": "zzz999", "status": "revoked"}})
    resp = h.client.post(
        "/api/deploy/revoke", json={"env": "cloud", "token": "zzz999"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"env": "cloud", "token": "zzz999", "status": "revoked"}
    assert h.calls()[-1]["argv"][1:3] == ["revoke", "zzz999"]


def test_revoke_by_alternate_id_still_flips_the_pointer(tmp_path, monkeypatch):
    # The managed backend addresses one mount by token OR id. Here the create
    # output carried only the id (so the pointer recorded it), while the share
    # list row shows the token — revoking by the token must still flip the
    # pointer, or the preview dot stays green for a link that was just taken
    # down.
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"id": "id-777", "url": "https://serve.example/tok-777", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)
    assert h.pointer()["token"] == "id-777"

    h.set_scenario(
        {
            "list": [{"token": "tok-777", "id": "id-777", "status": "active"}],
            "revoke": {"token": "tok-777", "status": "revoked"},
        }
    )
    resp = h.client.post(
        "/api/deploy/revoke", json={"env": "cloud", "token": "tok-777"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.text
    assert h.pointer()["status"] == "revoked"


def test_revoke_by_token_flips_the_matching_pointer(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    h.set_scenario({"revoke": {"token": "abc123", "status": "revoked"}})
    resp = h.client.post(
        "/api/deploy/revoke", json={"env": "cloud", "token": "abc123"}, headers=FUSED
    )
    assert resp.status_code == 200, resp.text
    # The page's pointer stays consistent: its Deploy button reads revoked now.
    assert h.pointer()["status"] == "revoked"


def test_shares_joins_mounts_to_local_pages(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario(
        {"create": {"token": "abc123", "url": "https://serve.example/abc123", "status": "active"}}
    )
    h.client.post("/api/deploy", json={"page": str(h.page), "env": "cloud"}, headers=FUSED)

    # `share list` carries no URLs on either backend — ours comes back from
    # the pointer, and the foreign mounts' are DERIVED from the env's base
    # URL (every mount on an env serves under one base as <base>/<token>,
    # share-links.md §6), which our recorded link reveals.
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
    assert mounts[1]["url"] == "https://serve.example/zzz999"


def test_shares_urls_stay_null_with_no_recorded_link_to_derive_from(tmp_path, monkeypatch):
    # No pointer on this env carries an absolute URL (e.g. every deploy so far
    # was AWS, which returns none) -> nothing to derive a base from; foreign
    # mounts honestly show no URL rather than a guessed one.
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario({"list": [{"token": "zzz999", "status": "active"}]})
    resp = h.client.get("/api/deploy/shares", params={"env": "cloud"})
    assert resp.json()["mounts"][0]["url"] is None


# -- preview -------------------------------------------------------------------


def test_preview_lists_what_would_be_published(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.post("/api/deploy/preview", json={"path": str(h.page)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"] == "view.html"
    assert body["entrypoints"] == [{"path": "./sine.py", "name": "sine"}]
    assert body["assets"] == []
    assert body["auto"] == ["./sine.py"]  # the default set, before any selection
    assert body["errors"] == []
    assert body["warnings"] == []
    assert h.calls() == []  # a preview is a pure local scan — no CLI, no files


def test_preview_applies_include_and_exclude(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    # Include a file the scan can't see; exclude the auto-detected entrypoint.
    resp = h.client.post(
        "/api/deploy/preview",
        json={"path": str(h.page), "include": ["data.csv"], "exclude": ["./sine.py"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entrypoints"] == []  # sine.py excluded
    assert [a["path"] for a in body["assets"]] == ["data.csv"]  # include added
    # `auto` is the default set and ignores the selection — sine.py stays listed
    # so the UI knows excluding it (not data.csv) belongs in "Excluded".
    assert body["auto"] == ["./sine.py"]
    assert body["errors"] == []
    # excluding a literally-referenced target warns (its call will fail when hosted)
    assert any("sine.py" in w for w in body["warnings"])


def test_preview_reports_export_blockers(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.page.write_text(
        "<html><body><script>fused.writeFile('/x', 'y');</script></body></html>",
        encoding="utf-8",
    )
    body = h.client.post("/api/deploy/preview", json={"path": str(h.page)}).json()
    assert len(body["errors"]) == 1
    assert "writeFile" in body["errors"][0]


def test_preview_rejects_non_html(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    resp = h.client.post("/api/deploy/preview", json={"path": str(tmp_path / "sine.py")})
    assert resp.status_code == 400


def test_shares_cli_failure_is_400(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.set_scenario({})
    resp = h.client.get("/api/deploy/shares", params={"env": "cloud"})
    assert resp.status_code == 400
