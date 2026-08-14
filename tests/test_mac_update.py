"""macOS in-app updater (fused_render/update/mac.py) and its API surface.

The signed-manifest crypto path is shared with the Windows updater and covered
by tests/test_win_supervisor_update.py; these tests cover what's new on mac:
the brew/dmg method decision, the manager's state machine (including the rule
that brew-managed installs are never updated by the app — the user runs the
surfaced `brew upgrade` command themselves), and the /api/update endpoints'
guards.
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
    # A bundle path that exists on no machine: _disk_version() must read None
    # so these tests never see the developer's real /Applications install.
    manager = mac.UpdateManager(bundle="/nonexistent/FusedRender.app", method=method)
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


# ---- brew path: the app never runs brew — the user does -----------------------


def test_brew_available_carries_manual_command(monkeypatch):
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["manual_command"] == "brew upgrade --cask fused-render"


def test_dmg_available_has_no_manual_command(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["manual_command"] is None


def test_brew_install_is_a_noop(monkeypatch):
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    manager.check()
    status = manager.install()
    assert status["state"] == "available"
    assert manager._install_thread is None


def test_brew_external_upgrade_flips_check_to_installed(monkeypatch):
    """The user runs brew in a terminal; the next check() sees the new bundle
    on disk, lands on "installed", and drops the manual command."""
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    assert manager.check()["state"] == "available"
    monkeypatch.setattr(manager, "_disk_version", lambda: "9.9.9")
    status = manager.check()
    assert status["state"] == "installed"
    assert status["manual_command"] is None


def test_failed_check_keeps_installed_when_disk_is_current(monkeypatch):
    """A network blip after a completed install must not resurface the install
    button — the error path re-derives state from the bundle on disk."""
    manager = _manager(monkeypatch, available="9.9.9")
    monkeypatch.setattr(manager, "_disk_version", lambda: "9.9.9")
    assert manager.check()["state"] == "installed"

    def boom(url, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(common, "fetch_manifest", boom)
    assert manager.check()["state"] == "installed"


def test_check_reports_installed_once_disk_has_the_update(monkeypatch):
    """After a swap (ours or a manual brew upgrade) the running __version__ is
    still old; a later auto-check must land on "installed", not flip back to
    "available" with a live install button."""
    manager = _manager(monkeypatch, available="9.9.9")
    monkeypatch.setattr(manager, "_disk_version", lambda: "9.9.9")
    status = manager.check()
    assert status["state"] == "installed"
    assert status["latest_version"] == "9.9.9"


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
