"""macOS in-app updater (fused_render/update/mac.py) and its API surface.

The signed-manifest crypto path is shared with the Windows updater and covered
by tests/test_win_supervisor_update.py; these tests cover what's new on mac:
the brew/dmg method decision, the manager's state machine (including that a
brew-managed bundle installs down the same DMG path while ALSO carrying the
`brew upgrade` command as a secondary way), and the /api/update endpoints'
guards.
"""
import os
import subprocess
import time
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


# ---- brew path: one install path, and never a brew command anywhere ----------


def test_brew_available_has_no_manual_command_either(monkeypatch):
    """The method is informational only (D742): a brew-managed bundle gets the
    same status shape as a dmg one, `manual_command` included — the app never
    offers a brew command because it never runs brew on itself."""
    manager = _manager(monkeypatch, method="brew", available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["method"] == "brew"
    assert status["manual_command"] is None


def test_dmg_available_has_no_manual_command(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["manual_command"] is None


def test_brew_install_takes_the_dmg_path(monkeypatch, tmp_path):
    """A brew-managed install used to be a no-op on POST /install (the user ran
    the brew command). It now installs exactly like a DMG one — one install
    path for every install type (D742) — and carries no command with it."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    manager._method = "brew"
    manager.check()
    assert manager.status()["manual_command"] is None

    # Recorded, not real: `_install_dmg` would otherwise download the manifest's
    # URL. The assertion is that it RAN — a brew install used to be refused
    # before it got here.
    ran = []
    monkeypatch.setattr(manager, "_install_dmg", lambda manifest: ran.append(manifest))

    manager.install()
    assert manager._install_thread is not None
    manager._install_thread.join(timeout=5)
    # The recorder returns instantly, so `install()`'s own return may already
    # say "installed" — the state to assert on is the settled one.
    assert len(ran) == 1
    assert ran[0]["version"] == "9.9.9"
    assert manager.status()["state"] == "installed"


def test_a_failed_brew_install_has_no_terminal_command(monkeypatch, tmp_path):
    """A failed install on a brew-managed bundle offers no way out but "Try
    again": the app will not hand the user a brew command it would not run
    itself (the cask's `uninstall quit:` would quit the app mid-upgrade)."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    manager._method = "brew"
    manager.check()
    monkeypatch.setattr(manager, "_install_dmg",
                        lambda manifest: (_ for _ in ()).throw(RuntimeError("boom")))
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert status["manual_command"] is None


def test_a_failed_dmg_install_has_no_terminal_command(monkeypatch, tmp_path):
    """Same failure on a dmg-managed bundle: the badge shows the raw error and
    a "Try again", with no command of any kind."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "_install_dmg",
                        lambda manifest: (_ for _ in ()).throw(RuntimeError("boom")))
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert status["manual_command"] is None


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
    on disk and lands on "installed"."""
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
    # The phase word lives in `detail`, not `message`: `jobTypeLabel` (the
    # dock/StatusBar chip) reads its leading verb off `detail` and the title
    # ("Update to v9.9.9") has no verb in it, while `jobStatusLine` joins
    # `message` and `detail` for a running row — the same word in both would
    # render as "Downloading · Downloading".
    assert row["detail"] == "Downloading"
    assert row["message"] == ""
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
    # The installing row has to be read from INSIDE the swap — by the time the
    # install thread is joinable the row is terminal and says "Cancelled" or an
    # error, which is not the phase this test is about. `_attach` is the first
    # thing the swap does, so it is the seam: capture there, then stop (mounting
    # a DMG that was never downloaded is not what this test is about either).
    def no_mount(dmg):
        seen["installing"] = _row()
        raise RuntimeError("stop after the phase flip")

    monkeypatch.setattr(manager, "_attach", no_mount)
    manager.install()
    manager._install_thread.join(timeout=5)

    downloading = seen["downloading"]
    assert downloading["done"] == float(1024 * 1024)
    assert downloading["total"] == float(4 * 1024 * 1024)
    assert downloading["unit"] == "bytes"
    assert downloading["detail"] == mac.PHASE_DOWNLOADING
    assert downloading["cancellable"] is True
    # The row during the swap: the phase in `detail` (so the chip reads
    # "Installing", not the download verb it would infer from `kind`), no
    # honest total for a copy-and-swap, and no ✕ either — there is no safe
    # point to stop at once the bundle is moving.
    installing = seen["installing"]
    assert installing["state"] == "running"
    assert installing["detail"] == mac.PHASE_INSTALLING
    assert installing["message"] == ""
    assert installing["done"] is None
    assert installing["total"] is None
    assert installing["cancellable"] is False
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


def test_a_cancel_after_the_last_byte_tick_still_stops_before_the_swap(
        monkeypatch, tmp_path):
    """`should_abort` runs BEFORE each chunk and a cancel only ever arrives on
    the reply to a tick — so a ✕ pressed AFTER the final tick (during the
    sha256 compare, seconds on a large DMG) has neither left to ride on
    (bugbot, PR #1058). The manager refreshes the flag with an id-only report
    once the download returns — the bundle is still untouched there — and
    cancels, discarding the finished DMG."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    updates = tmp_path / "updates"

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        assert not should_abort()
        progress(4, 8)
        progress(8, 8)
        os.makedirs(dir, exist_ok=True)
        path = os.path.join(dir, prefix + "done" + suffix)
        with open(path, "wb") as f:
            f.write(b"x" * 8)
        # The ✕ lands after the last tick was reported and after the loop's
        # last `should_abort` — nothing is left to carry it back except the
        # manager's own re-read.
        jobs.request_cancel("sys:update:9.9.9")
        return path

    monkeypatch.setattr(common, "download_verified", fake_download)
    monkeypatch.setattr(manager, "_attach",
                        lambda dmg: pytest.fail("a cancelled update must not mount"))
    manager.install()
    manager._install_thread.join(timeout=5)

    status = manager.status()
    assert status["state"] == "available"
    assert status["error"] is None
    row = _row()
    assert row["state"] == "cancelled"
    assert row["cancellable"] is False
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
        manager._job_report(done=1.0, total=None, detail=mac.PHASE_DOWNLOADING)
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


def test_the_swap_keeps_reporting_so_the_row_is_never_dropped_as_stale(
        monkeypatch, tmp_path):
    """`jobs.is_stalled` shows a running row as "No longer reporting" 30s after
    its last report and `_forget` drops it entirely after ten minutes — after
    which the final `done` upsert lands on a dismissed id and the "Installed —
    restart to finish" line is never drawn at all. The swap has no progress to
    report, so a watchdog re-sends the phase while it runs."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(mac, "INSTALL_HEARTBEAT_S", 0.02)
    installing = []
    real_upsert = jobs.upsert

    def spy(body, **kwargs):
        if body.get("detail") == mac.PHASE_INSTALLING:
            installing.append(dict(body))
        return real_upsert(body, **kwargs)

    monkeypatch.setattr(jobs, "upsert", spy)

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        os.makedirs(dir, exist_ok=True)
        path = os.path.join(dir, prefix + "done" + suffix)
        with open(path, "wb") as f:
            f.write(b"x" * 8)
        return path

    monkeypatch.setattr(common, "download_verified", fake_download)

    def slow_attach(dmg):
        time.sleep(0.3)  # a `ditto` of a whole .app bundle, in miniature
        raise RuntimeError("stop after the phase flip")

    monkeypatch.setattr(manager, "_attach", slow_attach)
    manager.install()
    manager._install_thread.join(timeout=10)

    # The flip itself, plus at least one beat from the watchdog while `_attach`
    # was busy — the row's clock moved without its content changing.
    assert len(installing) >= 2, installing
    assert all(body.get("cancellable") is False for body in installing)
    assert manager.status()["state"] == "error"


def test_a_row_that_cannot_be_opened_warns_once_and_the_install_carries_on(
        monkeypatch, tmp_path, caplog):
    """Reporting is never load-bearing: an install whose row cannot be drawn
    still installs. And it says so ONCE — there is a report per megabyte behind
    the first one, so an unlatched failure would print the same traceback
    hundreds of times per download."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    calls = []

    def broken_upsert(body, **kwargs):
        calls.append(dict(body))
        raise RuntimeError("registry is wedged")

    monkeypatch.setattr(jobs, "upsert", broken_upsert)

    def fake_install(manifest):
        for done in range(5):
            manager._job_report(done=float(done), total=4.0,
                                detail=mac.PHASE_DOWNLOADING, cancellable=True)

    monkeypatch.setattr(manager, "_install_dmg", fake_install)
    with caplog.at_level("WARNING", logger="fused_render.update"):
        manager.install()
        manager._install_thread.join(timeout=5)

    assert manager.status()["state"] == "installed"
    # Exactly one attempt (the opening report) and exactly one log line: every
    # later report is a no-op behind the latch.
    assert len(calls) == 1, calls
    records = [r for r in caplog.records if "could not report update job" in r.message]
    assert len(records) == 1, [r.message for r in caplog.records]


def test_the_heartbeat_never_overwrites_the_finished_row(monkeypatch, tmp_path):
    """The beat is joined before the terminal row is written and re-checks its
    stop flag after every wait (bugbot, PR #1058): with a beat firing every
    millisecond through a slow swap, no beat report is ever written AFTER the
    terminal one, so the row ends on the line the install wrote."""
    manager = _dmg_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(mac, "INSTALL_HEARTBEAT_S", 0.001)
    reports = []
    real_upsert = jobs.upsert

    def spy(body, **kwargs):
        reports.append(dict(body))
        return real_upsert(body, **kwargs)

    monkeypatch.setattr(jobs, "upsert", spy)

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        os.makedirs(dir, exist_ok=True)
        path = os.path.join(dir, prefix + "done" + suffix)
        with open(path, "wb") as f:
            f.write(b"x" * 8)
        return path

    monkeypatch.setattr(common, "download_verified", fake_download)

    def slow_attach(dmg):
        time.sleep(0.05)
        raise RuntimeError("swap ended")

    monkeypatch.setattr(manager, "_attach", slow_attach)
    manager.install()
    manager._install_thread.join(timeout=10)
    time.sleep(0.05)  # any beat still alive gets its chance to misfire

    terminal = [i for i, b in enumerate(reports) if b.get("state") == "error"]
    assert terminal, reports
    beats_after = [b for b in reports[terminal[0] + 1:] if b.get("detail") == mac.PHASE_INSTALLING]
    assert beats_after == [], beats_after
    row = _row()
    assert row["state"] == "error" and "swap ended" in row["message"], row
