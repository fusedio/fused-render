"""macOS in-app updater (fused_render/update/mac.py) and its API surface.

The signed-manifest crypto path is shared with the Windows updater and covered
by tests/test_win_supervisor_update.py; these tests cover what's new on mac:
the brew/dmg method decision, the manager's state machine (including the
no-DMG-fallback-on-brew-failure rule), and the /api/update endpoints' guards.
"""
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from fused_render.server.app import create_app
from fused_render.update import common, mac


# ---- detect_method -----------------------------------------------------------


def _run_stub(returncode: int, stdout: str = ""):
    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return run


def test_detect_method_none_when_unbundled():
    assert mac.detect_method(None) == "none"


def test_detect_method_dmg_when_brew_probe_missing(monkeypatch):
    monkeypatch.setattr(mac, "find_brew", lambda: None)
    assert mac.detect_method("/Applications/FusedRender.app") == "dmg"


def test_detect_method_dmg_when_cask_not_installed():
    method = mac.detect_method("/Applications/FusedRender.app", brew="/fake/brew",
                               run=_run_stub(1))
    assert method == "dmg"


def test_detect_method_brew_when_listed_artifact_matches(tmp_path):
    bundle = tmp_path / "FusedRender.app"
    bundle.mkdir()
    method = mac.detect_method(str(bundle), brew="/fake/brew",
                               run=_run_stub(0, f"==> App\n{bundle}\n"))
    assert method == "brew"


def test_detect_method_dmg_when_listed_artifact_elsewhere(tmp_path):
    bundle = tmp_path / "FusedRender.app"
    bundle.mkdir()
    other = tmp_path / "Other.app"
    other.mkdir()
    method = mac.detect_method(str(bundle), brew="/fake/brew",
                               run=_run_stub(0, f"{other}\n"))
    assert method == "dmg"


def test_detect_method_dmg_when_brew_errors(tmp_path):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    bundle = tmp_path / "FusedRender.app"
    bundle.mkdir()
    assert mac.detect_method(str(bundle), brew="/fake/brew", run=run) == "dmg"


# ---- UpdateManager state machine ----------------------------------------------


def _manager(monkeypatch, *, method="dmg", available=None, current="0.4.10"):
    manager = mac.UpdateManager(bundle="/Applications/FusedRender.app", method=method)
    monkeypatch.setattr(mac, "__version__", current)
    if available is not None:
        manifest = {"schema": 1, "version": available, "url": "https://x/y.dmg",
                    "sha256": "s", "signature": "g"}
        monkeypatch.setattr(common, "fetch_manifest",
                            lambda url, **kwargs: dict(manifest))
    return manager


def test_check_finds_newer(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["latest_version"] == "9.9.9"


def test_check_up_to_date(monkeypatch):
    manager = _manager(monkeypatch, available="0.0.1")
    status = manager.check()
    assert status["state"] == "idle"
    assert status["latest_version"] is None


def test_check_failure_keeps_available(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    assert manager.check()["state"] == "available"

    def boom(url, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(common, "fetch_manifest", boom)
    status = manager.check()
    assert status["state"] == "available"
    assert status["latest_version"] == "9.9.9"


def test_check_failure_without_prior_update_is_idle(monkeypatch):
    manager = _manager(monkeypatch)

    def boom(url, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(common, "fetch_manifest", boom)
    assert manager.check()["state"] == "idle"


def test_install_requires_available_state(monkeypatch):
    manager = _manager(monkeypatch)
    assert manager.install()["state"] == "idle"


def test_install_runs_method_and_lands_installed(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    manager.check()
    done = []
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: done.append(manifest))
    manager.install()
    manager._install_thread.join(timeout=5)
    assert done and done[0]["version"] == "9.9.9"
    assert manager.status()["state"] == "installed"


def test_install_failure_surfaces_error(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    manager.check()

    def boom(manifest):
        raise RuntimeError("disk full")

    monkeypatch.setattr(manager, "_install_dmg", boom)
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert "disk full" in status["error"]


def test_install_retry_allowed_from_error(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    manager.check()
    monkeypatch.setattr(manager, "_install_dmg",
                        lambda manifest: (_ for _ in ()).throw(RuntimeError("x")))
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "error"
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: None)
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "installed"


# ---- brew path: failure gives the command, never a DMG fallback ---------------


def test_brew_failure_sets_manual_command_and_no_dmg(monkeypatch):
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    manager.check()
    monkeypatch.setattr(mac, "find_brew", lambda: "/fake/brew")

    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Error: no sudo")

    monkeypatch.setattr(mac.subprocess, "run", run)
    dmg_calls = []
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: dmg_calls.append(1))
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert status["manual_command"] == "brew upgrade --cask fused-render"
    assert "no sudo" in status["error"]
    assert not dmg_calls


def test_brew_missing_at_install_sets_manual_command(monkeypatch):
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    manager.check()
    monkeypatch.setattr(mac, "find_brew", lambda: None)
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert status["manual_command"] == "brew upgrade --cask fused-render"


def test_brew_success_lands_installed(monkeypatch):
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    manager.check()
    monkeypatch.setattr(mac, "find_brew", lambda: "/fake/brew")
    monkeypatch.setattr(
        mac.subprocess, "run",
        lambda cmd, **kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "installed"


# ---- dmg helpers ---------------------------------------------------------------


def test_find_app(tmp_path):
    (tmp_path / "FusedRender.app").mkdir()
    manager = mac.UpdateManager(bundle="/x.app", method="dmg")
    assert manager._find_app(str(tmp_path)).endswith("FusedRender.app")
    with pytest.raises(RuntimeError):
        manager._find_app(str(tmp_path / "FusedRender.app"))  # empty dir: no .app


def test_verify_app_version(tmp_path):
    import plistlib

    app = tmp_path / "FusedRender.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "1.2.3"}, f)
    manager = mac.UpdateManager(bundle="/x.app", method="dmg")
    manager._verify_app_version(str(app), "1.2.3")
    with pytest.raises(RuntimeError):
        manager._verify_app_version(str(app), "9.9.9")


# ---- API surface ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_config_omits_update_without_manager(client):
    assert mac.manager() is None
    assert "update" not in client.get("/api/config").json()


def test_update_endpoints_404_without_manager(client):
    assert client.post("/api/update/check", headers={"X-Fused": "1"}).status_code == 404
    assert client.post("/api/update/install", headers={"X-Fused": "1"}).status_code == 404


def test_update_endpoints_require_x_fused(client, monkeypatch):
    monkeypatch.setattr(mac, "_manager",
                        mac.UpdateManager(bundle="/x.app", method="dmg"))
    assert client.post("/api/update/check").status_code == 403
    assert client.post("/api/update/install").status_code == 403


def test_config_carries_update_with_manager(client, monkeypatch):
    monkeypatch.setattr(mac, "_manager",
                        mac.UpdateManager(bundle="/x.app", method="dmg"))
    body = client.get("/api/config").json()
    assert body["update"]["state"] == "idle"
    assert body["update"]["method"] == "dmg"


def test_start_noop_when_unbundled(monkeypatch):
    monkeypatch.setattr(mac, "_manager", None)
    monkeypatch.setattr(mac, "bundle_path", lambda: None)
    assert mac.start() is None
    assert mac.manager() is None
