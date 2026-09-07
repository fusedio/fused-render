"""macOS in-app updater (fused_render/update/mac.py) and its API surface.

The signed-manifest crypto path is shared with the Windows updater and covered
by tests/test_win_supervisor_update.py; these tests cover what's new on mac:
the brew/dmg method decision, the manager's state machine (including the rule
that brew-managed installs are never updated by the app — the user runs the
surfaced `brew upgrade` command themselves), and the /api/update endpoints'
guards.
"""
import os
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
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
    assert status["manual_command"] == "brew update && brew upgrade --cask fused-render"


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


def test_status_notices_external_upgrade_without_a_check(monkeypatch):
    """The badge polls status() every minute; a terminal `brew upgrade` must
    flip it to "installed" then, not after the next multi-hour check tick."""
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    assert manager.check()["state"] == "available"
    monkeypatch.setattr(manager, "_disk_version", lambda: "9.9.9")
    status = manager.status()
    assert status["state"] == "installed"
    assert status["manual_command"] is None


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


# ---- the Activity row (sys:update:<version>) ----------------------------------
#
# The install mirrors itself into the job registry the same way an index rescan
# does (`server/routers/index.py::_mirror_one_run_job`), so the dock — not the
# sidebar badge's own panel — is where the bytes, the phase and the ✕ live. The
# tests below are the contract that row makes: one id per version, bytes while
# downloading, indeterminate and uncancellable once the swap starts, and a
# cancel that puts the manager back exactly where the ✕ was pressed from.


@pytest.fixture(autouse=True)
def _reset_jobs():
    jobs.reset()
    yield
    jobs.reset()


def _dmg_manager(monkeypatch, tmp_path, *, available="9.9.9", current="0.4.10"):
    """A dmg-method manager whose bundle, updates dir and disk check are all
    inside tmp_path — so `_install_dmg` can be run for real without touching
    the developer's own /Applications install or ~/Library."""
    bundle = tmp_path / "FusedRender.app"
    bundle.mkdir()
    updates = tmp_path / "updates"
    updates.mkdir()
    manager = mac.UpdateManager(bundle=str(bundle), method="dmg")
    monkeypatch.setattr(mac, "__version__", current)
    manifest = {"schema": 1, "version": available,
                "url": "https://example.invalid/FusedRender.dmg",
                "sha256": "s", "signature": "g"}
    monkeypatch.setattr(common, "fetch_manifest", lambda url, **kwargs: dict(manifest))
    monkeypatch.setattr(manager, "_updates_dir", lambda: str(updates))
    monkeypatch.setattr(manager, "_check_disk_space", lambda updates: None)
    manager.check()
    assert manager.status()["state"] == "available"
    return manager


def _row():
    rows = jobs.list_jobs()
    assert len(rows) == 1, rows
    return rows[0]


def test_install_opens_a_cancellable_download_row_for_the_version(monkeypatch, tmp_path):
    import threading

    manager = _dmg_manager(monkeypatch, tmp_path)
    gate = threading.Event()
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: gate.wait(5))
    manager.install()
    row = _row()
    assert row["id"] == "sys:update:9.9.9"
    assert row["title"] == "Update to v9.9.9"
    assert row["owner"] == "server"
    assert row["kind"] == "download"
    assert row["unit"] == "bytes"
    assert row["state"] == "running"
    assert row["message"] == "Downloading"
    assert row["cancellable"] is True
    gate.set()
    manager._install_thread.join(timeout=5)


def test_a_successful_install_finishes_the_row_with_the_restart_line(monkeypatch,
                                                                    tmp_path):
    manager = _dmg_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: None)
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "installed"
    row = _row()
    assert row["state"] == "done"
    # `detail` as well as `message`: jobStatusLine reads `detail` for a done
    # row, and this line is also the completion NOTICE (terminalNotifications).
    assert row["detail"] == "Installed — restart to finish"
    assert row["message"] == "Installed — restart to finish"
    assert row["cancellable"] is False


def test_a_failed_install_fails_the_row_with_the_error_text(monkeypatch, tmp_path):
    manager = _dmg_manager(monkeypatch, tmp_path)

    def boom(manifest):
        raise RuntimeError("disk full")

    monkeypatch.setattr(manager, "_install_dmg", boom)
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "error"
    row = _row()
    assert row["state"] == "error"
    assert row["message"] == "disk full"
    assert row["cancellable"] is False


def test_the_row_mirrors_bytes_then_flips_to_an_uncancellable_installing_phase(
        monkeypatch, tmp_path):
    manager = _dmg_manager(monkeypatch, tmp_path)
    seen = {}

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        progress(1024 * 1024, 4 * 1024 * 1024)
        seen["downloading"] = _row()
        return os.path.join(dir, "FusedRender.dmg")

    monkeypatch.setattr(mac.common, "download_verified", fake_download)
    # Stops the install right after the phase flip — mounting a DMG that was
    # never downloaded is not what this test is about.
    def no_mount(dmg):
        raise RuntimeError("stop after the phase flip")

    monkeypatch.setattr(manager, "_attach", no_mount)
    manager.install()
    manager._install_thread.join(timeout=5)

    downloading = seen["downloading"]
    assert downloading["done"] == float(1024 * 1024)
    assert downloading["total"] == float(4 * 1024 * 1024)
    assert downloading["unit"] == "bytes"
    assert downloading["message"] == "Downloading"
    assert downloading["cancellable"] is True
    # The row after the download: no honest total for a copy-and-swap, and no
    # ✕ either — there is no safe point to stop at once the bundle is moving.
    row = _row()
    assert row["state"] == "error"
    assert row["cancellable"] is False


class _ChunkedResponse:
    """A response that streams several chunks, with a hook fired after the
    first one — the seam a "the user pressed ✕ mid-download" test needs."""

    def __init__(self, chunk: bytes, count: int, on_first_chunk=None):
        self._chunk = chunk
        self._left = count
        self._on_first_chunk = on_first_chunk
        self._served = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if self._left <= 0:
            return b""
        self._left -= 1
        self._served += 1
        if self._served == 1 and self._on_first_chunk is not None:
            self._on_first_chunk()
        return self._chunk

    def getheader(self, name, default=None):
        return default


def test_cancelling_mid_download_reverts_to_available_and_discards_the_partial(
        monkeypatch, tmp_path):
    """End to end through the REAL download loop: the ✕ sets the registry's
    flag, the manager learns it from the reply to its next byte tick, and
    `download_verified` aborts on the following chunk."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    updates = tmp_path / "updates"

    def press_cancel():
        assert jobs.request_cancel("sys:update:9.9.9") is not None

    monkeypatch.setattr(
        common, "urlopen",
        lambda url, timeout: _ChunkedResponse(b"x" * 8, 50, press_cancel))
    monkeypatch.setattr(manager, "_attach",
                        lambda dmg: pytest.fail("cancelled download must not mount"))
    manager.install()
    manager._install_thread.join(timeout=5)

    status = manager.status()
    # Back exactly where the ✕ was pressed from: the update is still there to
    # install, so this is "available", not "error".
    assert status["state"] == "available"
    assert status["latest_version"] == "9.9.9"
    assert status["progress"] is None
    assert status["progress_total"] is None
    assert status["error"] is None
    row = _row()
    assert row["state"] == "cancelled"
    assert row["detail"] == "Cancelled"
    assert row["cancellable"] is False
    # The half-written DMG is gone — nothing is left to resume or sweep.
    assert os.listdir(updates) == []


def test_a_retry_after_a_cancel_starts_from_a_clean_flag(monkeypatch, tmp_path):
    """The row id is per-version, so a retry reuses the row the previous ✕ was
    pressed on — and the manager's own abort flag survives in memory. Both have
    to be clean before the second attempt reads them, or it aborts on its own
    first chunk."""
    import threading

    manager = _dmg_manager(monkeypatch, tmp_path)

    def cancelled_install(manifest):
        # The ✕, and then the byte tick that carries the flag back — the exact
        # two steps the real download loop takes.
        jobs.request_cancel("sys:update:9.9.9")
        manager._job_report(done=1.0, total=None, message=mac.PHASE_DOWNLOADING)
        assert manager._cancel_requested() is True
        raise common.UpdateCancelled("cancelled")

    monkeypatch.setattr(manager, "_install_dmg", cancelled_install)
    manager.install()
    manager._install_thread.join(timeout=5)
    assert manager.status()["state"] == "available"
    assert _row()["state"] == "cancelled"

    gate = threading.Event()
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: gate.wait(5))
    manager.install()
    row = _row()
    assert row["state"] == "running"
    assert row["cancel_requested"] is False
    assert manager._cancel_requested() is False
    gate.set()
    manager._install_thread.join(timeout=5)
