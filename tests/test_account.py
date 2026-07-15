"""Tests for the account surface (fused_render/account.py):
/api/account/status, /api/account/login, /api/account/login/cancel,
/api/account/logout.

The fused CLI is substituted with a stub script through the same seam the
runtime uses (FUSED_RENDER_FUSED_BIN — a compound "python stub.py" command),
mirroring tests/test_deploy.py. The stub records every invocation (argv plus
the child-env details the login flow depends on) to a log; its `cloud login`
behavior is driven by FUSED_STUB_LOGIN_MODE:

  complete — print the authorize URL, then (as if the user finished the
             browser round-trip) write the credentials file and exit 0
  hang     — print the URL and block (the CLI waiting on its localhost
             callback); only cancel/logout/SIGTERM ends it
  silent   — print nothing and block (URL-capture timeout path)
  fail     — exit 1 with a click-style error before any URL

`cloud logout` deletes the credentials file; `cloud orgs` answers from the
FUSED_STUB_SCENARIO json (exit 1 when scenario has no "orgs" — the
unreachable-control-plane path) and requires the credentials file, like the
real CLI. FUSED_RENDER_HOME / OPENFUSED_* are redirected to tmp paths so
nothing touches the real stores.
"""
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.account as account_mod
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on writes

STUB = """\
import json, os, sys, time

entry = {
    "argv": sys.argv[1:],
    "return_url": os.environ.get("OPENFUSED_LOGIN_RETURN_URL"),
    "unbuffered": os.environ.get("PYTHONUNBUFFERED"),
    "openfused_env": os.environ.get("OPENFUSED_ENV"),
}
with open(os.environ["FUSED_STUB_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")

group = sys.argv[1] if len(sys.argv) > 1 else ""
verb = sys.argv[2] if len(sys.argv) > 2 else ""
creds = os.environ["OPENFUSED_FUSED_CLOUD_CREDENTIALS"]
mode = os.environ.get("FUSED_STUB_LOGIN_MODE", "complete")

if group == "cloud" and verb == "login":
    if mode == "fail":
        sys.stderr.write("Error: Not admitted to the Fused beta.\\n")
        sys.exit(1)
    if mode == "silent":
        time.sleep(30)
        sys.exit(1)
    print("Open this URL in your browser to log in to Fused:")
    print("  https://auth.example.test/authorize?client_id=abc&code_challenge=xyz")
    sys.stdout.flush()
    if mode == "hang":
        time.sleep(30)
        sys.exit(1)
    time.sleep(float(os.environ.get("FUSED_STUB_LOGIN_DELAY", "0.2")))
    with open(creds, "w") as f:
        f.write("{}")
    print("Logged in to Fused. Control-plane credentials saved to %s." % creds)
    sys.exit(0)
if group == "cloud" and verb == "logout":
    if os.path.exists(creds):
        os.unlink(creds)
    print("Logged out. Removed control-plane credentials at %s." % creds)
    sys.exit(0)
if group == "cloud" and verb == "orgs":
    if not os.path.exists(creds):
        sys.stderr.write("Error: Not logged in to Fused. Run `fused cloud login` first.\\n")
        sys.exit(1)
    with open(os.environ["FUSED_STUB_SCENARIO"], "r") as f:
        scenario = json.load(f)
    if "orgs" not in scenario:
        sys.stderr.write("Error: control plane unreachable\\n")
        sys.exit(1)
    print(json.dumps(scenario["orgs"]))
    sys.exit(0)
sys.stderr.write("Error: stub has no handler for %r\\n" % (sys.argv[1:],))
sys.exit(1)
"""

AUTHORIZE_URL = "https://auth.example.test/authorize?client_id=abc&code_challenge=xyz"

ENVS = {
    "default": "cloud",
    "envs": {
        "cloud": {"name": "cloud", "backend": "fused", "org": "acme", "env": "e1"},
        "dev": {"name": "dev", "backend": "local"},
    },
}


@pytest.fixture(autouse=True)
def _reset_active_login():
    # The module holds the one in-flight login as process-global state; a
    # hang-mode child left by one test must never leak into the next.
    yield
    account_mod._cancel_active_login(wait=2.0)


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "home"
        monkeypatch.setenv("FUSED_RENDER_HOME", str(self.home))
        monkeypatch.delenv("OPENFUSED_ENV", raising=False)
        monkeypatch.delenv("OPENFUSED_LOGIN_RETURN_URL", raising=False)

        envs_file = tmp_path / "envs.json"
        envs_file.write_text(json.dumps(ENVS), encoding="utf-8")
        monkeypatch.setenv("OPENFUSED_ENVS_FILE", str(envs_file))

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

        self.client = TestClient(create_app(start_dir=str(tmp_path)))

    def set_scenario(self, scenario: dict) -> None:
        self.scenario_file.write_text(json.dumps(scenario), encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def login_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][:2] == ["cloud", "login"]]

    def status(self, probe: bool = False) -> dict:
        url = "/api/account/status" + ("?probe=1" if probe else "")
        resp = self.client.get(url)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def wait_status(self, predicate, deadline: float = 5.0) -> dict:
        """Poll /api/account/status until `predicate(status)` — the client's
        own completion model (there is no push channel from the CLI)."""
        end = time.monotonic() + deadline
        while True:
            status = self.status()
            if predicate(status):
                return status
            assert time.monotonic() < end, f"timed out waiting; last status: {status}"
            time.sleep(0.05)


def _harness(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# -- status ---------------------------------------------------------------------


def test_status_logged_out(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    status = h.status()
    assert status["logged_in"] is False
    assert status["login_in_flight"] is False
    assert status["cli"]["found"] is True
    assert status["setup_cli"] == "fused"
    assert [e["name"] for e in status["envs"]] == ["cloud"]  # hosted only
    assert status["default_env"] == "cloud"
    assert status["probe"] is None


def test_status_logged_in_via_credentials_presence(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.creds.write_text("{}", encoding="utf-8")
    assert h.status()["logged_in"] is True


def test_status_probe_skipped_when_logged_out(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    assert h.status(probe=True)["probe"] is None
    assert h.calls() == []  # no pointless `cloud orgs` child


def test_status_probe_parses_orgs(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.creds.write_text("{}", encoding="utf-8")
    h.set_scenario(
        {
            "orgs": {
                "admitted": True,
                "orgs": [
                    {"org": "acme", "env": "default", "provision_state": "ready", "role": "admin"},
                    {"org": "acme", "env": "staging", "provision_state": "provisioning"},
                ],
            }
        }
    )
    probe = h.status(probe=True)["probe"]
    assert probe["ok"] is True
    assert probe["admitted"] is True
    assert probe["error"] is None
    assert probe["orgs"] == [
        {"org": "acme", "env": "default", "provision_state": "ready", "role": "admin"},
        {"org": "acme", "env": "staging", "provision_state": "provisioning", "role": None},
    ]


def test_status_probe_surfaces_cli_failure(tmp_path, monkeypatch):
    # Scenario without "orgs" = the stub's unreachable-control-plane answer.
    # The presence-only logged_in stays True (last-known), the probe says why
    # the deeper check failed — a stale/revoked credential reads the same way.
    h = _harness(tmp_path, monkeypatch)
    h.creds.write_text("{}", encoding="utf-8")
    status = h.status(probe=True)
    assert status["logged_in"] is True
    assert status["probe"]["ok"] is False
    assert status["probe"]["admitted"] is None
    assert "control plane unreachable" in status["probe"]["error"]


# -- login ----------------------------------------------------------------------


def test_login_returns_authorize_url_then_polling_sees_completion(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    return_url = "http://127.0.0.1:1777/view/_account"
    resp = h.client.post("/api/account/login", json={"return_url": return_url}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"authorize_url": AUTHORIZE_URL}

    # The child got the two load-bearing env vars: unbuffered stdout (the URL
    # must reach the pipe) and the return URL (the browser lands back in the app).
    (call,) = h.login_calls()
    assert call["argv"] == ["cloud", "login", "--no-browser"]
    assert call["unbuffered"] == "1"
    assert call["return_url"] == return_url
    assert call["openfused_env"] is None  # account runs are not env-targeted

    # Completion is polled: the stub finishes the "browser round-trip" and
    # writes the credentials file; status flips and the child is reaped.
    status = h.wait_status(lambda s: s["logged_in"] and not s["login_in_flight"])
    assert status["logged_in"] is True


def test_login_requires_guard_header(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    assert h.client.post("/api/account/login", json={}).status_code == 403
    assert h.client.post("/api/account/login/cancel", json={}).status_code == 403
    assert h.client.post("/api/account/logout", json={}).status_code == 403


def test_login_rejects_non_loopback_return_url(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    for bad in ("https://evil.example/phish", "ftp://127.0.0.1/x", "not a url", 7):
        resp = h.client.post("/api/account/login", json={"return_url": bad}, headers=FUSED)
        assert resp.status_code == 400, bad
    assert h.calls() == []  # rejected before any child was spawned


def test_login_is_single_flight(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_STUB_LOGIN_MODE", "hang")
    first = h.client.post("/api/account/login", json={}, headers=FUSED)
    second = h.client.post("/api/account/login", json={}, headers=FUSED)
    assert first.json() == second.json() == {"authorize_url": AUTHORIZE_URL}
    assert len(h.login_calls()) == 1  # the second call joined, no second child
    assert h.status()["login_in_flight"] is True


def test_login_cancel_kills_the_child(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_STUB_LOGIN_MODE", "hang")
    assert h.client.post("/api/account/login", json={}, headers=FUSED).status_code == 200
    resp = h.client.post("/api/account/login/cancel", headers=FUSED)
    assert resp.json() == {"ok": True, "canceled": True}
    status = h.wait_status(lambda s: not s["login_in_flight"])
    assert status["logged_in"] is False  # the abandoned login never completed

    # Cancel with nothing in flight is a clean no-op.
    resp = h.client.post("/api/account/login/cancel", headers=FUSED)
    assert resp.json() == {"ok": True, "canceled": False}


def test_login_failure_surfaces_cli_error(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_STUB_LOGIN_MODE", "fail")
    resp = h.client.post("/api/account/login", json={}, headers=FUSED)
    assert resp.status_code == 502
    assert "Not admitted" in resp.json()["error"]
    assert h.status()["login_in_flight"] is False


def test_login_url_capture_timeout(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_STUB_LOGIN_MODE", "silent")
    monkeypatch.setattr(account_mod, "URL_CAPTURE_TIMEOUT", 0.5)
    resp = h.client.post("/api/account/login", json={}, headers=FUSED)
    assert resp.status_code == 502
    assert "did not print a sign-in URL" in resp.json()["error"]
    status = h.wait_status(lambda s: not s["login_in_flight"])
    assert status["logged_in"] is False


def test_login_without_cli_is_a_400(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(account_mod, "fused_cli", lambda: None)
    resp = h.client.post("/api/account/login", json={}, headers=FUSED)
    assert resp.status_code == 400
    assert "FUSED_RENDER_FUSED_BIN" in resp.json()["error"]


# -- logout ---------------------------------------------------------------------


def test_logout_deletes_credentials_and_returns_fresh_status(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.creds.write_text("{}", encoding="utf-8")
    resp = h.client.post("/api/account/logout", headers=FUSED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["logged_in"] is False
    assert not h.creds.exists()
    (call,) = h.calls()
    assert call["argv"] == ["cloud", "logout", "--no-browser"]


def test_logout_forwards_env_for_a_full_signout(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.creds.write_text("{}", encoding="utf-8")
    resp = h.client.post("/api/account/logout", json={"env": "cloud"}, headers=FUSED)
    assert resp.status_code == 200, resp.text
    (call,) = h.calls()
    assert call["argv"] == ["cloud", "logout", "--no-browser", "--env", "cloud"]


def test_logout_kills_an_inflight_login_first(tmp_path, monkeypatch):
    # A login child that outlived a logout could complete its browser callback
    # LATER and silently re-write the JWT — logout must kill it (and wait)
    # before the CLI deletes credentials.
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("FUSED_STUB_LOGIN_MODE", "hang")
    assert h.client.post("/api/account/login", json={}, headers=FUSED).status_code == 200
    assert h.status()["login_in_flight"] is True

    resp = h.client.post("/api/account/logout", headers=FUSED)
    assert resp.status_code == 200, resp.text
    status = resp.json()
    assert status["login_in_flight"] is False
    assert status["logged_in"] is False
    verbs = [c["argv"][:2] for c in h.calls()]
    assert verbs == [["cloud", "login"], ["cloud", "logout"]]
    assert not h.creds.exists()
