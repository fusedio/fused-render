"""Tests for the Mounts backend (shell/mounts.py): the persisted
mount store, the rclone rcd client (against a stub rc server — real
rclone is never invoked), and the /api/mounts endpoints.

FUSED_RENDER_HOME is redirected per test so no test touches the real
~/.fused-render or a real mount.
"""
import json
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fused_render.shell.gcssign as gcssign_mod
import fused_render.shell.mounts as mounts_mod


@pytest.fixture(autouse=True)
def _warm_https_opener():
    """Build urllib's global HTTPS opener (and its SSL context) on the REAL
    platform before any test runs. The win32 mount tests patch sys.platform to
    "win32"; if the opener is first constructed while that patch is live,
    ssl.load_default_certs takes the Windows cert-store branch
    (enum_certificates), which doesn't exist off-Windows, and every rc call over
    plain-HTTP loopback dies with a NameError. Pre-warming sidesteps the
    artifact — real Windows has enum_certificates."""
    import urllib.request as _u
    if _u._opener is None:
        _u._opener = _u.build_opener()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_ambient_aws_creds(tmp_path, monkeypatch):
    """Sign mode's credential resolver reads AWS_* env vars and
    ~/.aws/credentials; neutralize both so a developer's real AWS credentials
    can't make an env_auth remote resolve as signable and perturb these tests.
    Tests that exercise sign mode put explicit keys in the remote config."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE",
                       str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    # The optional botocore rung (s3sign) is consulted for an env_auth/profile
    # remote once the static ladder finds nothing; disable the IMDS probe so it
    # resolves to None immediately (deterministic, and fast) instead of
    # resolving an EC2/CI role or stalling ~1s on metadata.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


class StubRcd:
    """Minimal rclone rcd stand-in: answers the rc methods the module uses
    and records every call. Responses are per-method canned JSON that a test
    may override; mount/unmount default to success."""

    def __init__(self):
        self.calls = []
        self.delay = {}  # method -> seconds to sleep before responding (timeouts)
        self.jobs = {}   # jobid -> job dict (for the _async / job/* path)
        self._next_jobid = 1
        # Faithfully model the kernel mount table: a successful mount/mount
        # adds its mountPoint here and mount/unmount drops it, so the fixture
        # can back os.path.ismount off this set. attach_mount now verifies the
        # kernel mount actually attached (_await_ismount), so a stub that
        # answers mount/mount 200 but leaves ismount False would (correctly)
        # fail every attach — this keeps the stub's two views consistent.
        self.mounted = set()
        self.responses = {
            "core/pid": {"pid": 4242},
            "mount/mount": {},
            "mount/unmount": {},
            "mount/listmounts": {"mountPoints": []},
            "serve/list": {"list": []},
            "serve/start": {"addr": "127.0.0.1:59999", "id": "http-stub"},
            "serve/stop": {},
        }
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _resolve(self, method):
                """The (code, payload) this stub would answer `method` with,
                from the canned per-method responses."""
                resp = stub.responses.get(method)
                if isinstance(resp, list):  # per-call sequence; last repeats
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if resp is None:
                    return 404, {"error": f"unknown method {method}"}
                if isinstance(resp, tuple):
                    return resp
                return 200, resp

            def _reply(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = self.path.lstrip("/")

                # job/status and job/stop drive the _async cancellation path.
                if method == "job/status":
                    stub.calls.append((method, body))
                    job = stub.jobs.get(body.get("jobid"))
                    if job is None:
                        return self._reply(500, {"error": "job not found"})
                    finished = (job["ready_at"] is not None
                                and time.monotonic() >= job["ready_at"])
                    out = {"id": body["jobid"], "finished": finished,
                           "success": finished and not job["error"],
                           "error": job["error"] if finished else ""}
                    if finished and not job["error"]:
                        out["output"] = job["output"]
                    return self._reply(200, out)
                if method == "job/stop":
                    stub.calls.append((method, body))
                    job = stub.jobs.get(body.get("jobid"))
                    if job is None:
                        return self._reply(500, {"error": "job not found"})
                    job["stopped"] = True
                    return self._reply(200, {})

                # Async submit: record the logical call (minus the _async control
                # key), stash the eventual result as a job keyed by delay, and
                # hand back a jobid immediately — exactly like rclone rcd.
                if body.pop("_async", None):
                    stub.calls.append((method, body))
                    code, payload = self._resolve(method)
                    jobid = stub._next_jobid
                    stub._next_jobid += 1
                    # A never-ending job: delay is None => ready_at never arrives.
                    d = stub.delay.get(method)
                    stub.jobs[jobid] = {
                        "ready_at": (None if d == float("inf")
                                     else time.monotonic() + (d or 0)),
                        "output": payload if code == 200 else {},
                        "error": "" if code == 200 else str(payload.get("error", "err")),
                        "stopped": False,
                    }
                    return self._reply(200, {"jobid": jobid})

                # Synchronous path (core/pid, mount/*, and any non-async caller).
                stub.calls.append((method, body))
                if method in stub.delay:
                    time.sleep(stub.delay[method])
                code, payload = self._resolve(method)
                # Track the kernel mount table so the fixture's os.path.ismount
                # sees a mount appear/disappear exactly when rcd reports success.
                if code == 200 and body.get("mountPoint"):
                    if method == "mount/mount":
                        stub.mounted.add(body["mountPoint"])
                    elif method == "mount/unmount":
                        stub.mounted.discard(body["mountPoint"])
                self._reply(code, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def rcd(home, monkeypatch):
    """A live stub rcd whose port is recorded in the state file, so
    _ensure_rcd reuses it instead of spawning real rclone.

    os.path.ismount is backed by the stub's simulated kernel mount table so
    attach_mount's post-mount verify (_await_ismount) passes on the happy path
    exactly when the stub reports mount/mount success. A test that needs a
    different ismount view (stale/disconnected, adopt-an-existing-mount) still
    monkeypatches os.path.ismount itself in its body — that runs after this and
    wins."""
    stub = StubRcd()
    mounts_mod.write_rcd_state(stub.port, 4242)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p in stub.mounted)
    yield stub
    stub.close()


# -- rclone_bin ------------------------------------------------------------


def test_rclone_bin_prefers_bundled_when_packaged(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "rclone"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_rclone_bin_falls_back_when_packaged_bundle_missing(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


def test_rclone_bin_uses_path_when_unpackaged(monkeypatch):
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


# -- store ---------------------------------------------------------------------


def test_store_roundtrip(home):
    assert mounts_mod.list_mounts() == []
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    assert c["name"] == "data" and c["remote"] == "remote:bucket/prefix"
    [stored] = mounts_mod.list_mounts()
    assert stored["id"] == c["id"]
    mounts_mod.remove_mount(c["id"])
    assert mounts_mod.list_mounts() == []


def test_add_rejects_bad_names_and_remotes(home):
    for bad in ("", "a/b", "a\\b", "a:b", ".hidden"):
        with pytest.raises(ValueError):
            mounts_mod.add_mount(bad, "remote:x")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("ok", "no-colon-spec")


def test_add_rejects_duplicates(home):
    mounts_mod.add_mount("data", "remote:bucket")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("data", "remote:other")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("other", "remote:bucket")


def test_mountpoint_derives_from_branch_aware_home(home):
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    assert mp.startswith(str(home))
    assert mp.endswith("/mounts/data")



# -- rcd client ----------------------------------------------------------------


def test_ensure_rcd_reuses_live_daemon(home, rcd):
    port = mounts_mod.ensure_rcd()
    assert port == rcd.port
    assert ("core/pid", {}) in rcd.calls


def test_mount_calls_rc_with_vfs_options(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    err = mounts_mod.attach_mount(c)
    assert err is None
    [(method, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["fs"] == "remote:bucket/prefix"
    assert body["mountPoint"] == mounts_mod.mountpoint(c)
    assert body["vfsOpt"]["CacheMode"] == "full"
    assert body["vfsOpt"]["CacheMaxAge"] == "24h"
    # The mount carries the same on-disk cache cap as the serve — both must
    # hold every vfs option or rcd splits them into two VFS instances.
    assert body["vfsOpt"]["CacheMaxSize"] == "20Gi"


def test_serve_vfs_opt_derived_from_mount_vfs_opt():
    # The mount (vfsOpt object) and the serve (flat vfs_* params) must describe
    # the SAME option set or rcd builds two independent VFS instances that don't
    # share cached ranges — the whole reason this bug existed. SERVE_VFS_OPT is
    # DERIVED from VFS_OPT to guarantee it; assert every key maps and the values
    # agree (bools -> "true"/"false", ints -> str, as serve/list echoes them).
    # The map also carries the per-mount ReadOnly key (added by _vfs_opt_for and
    # layered on by _serve_vfs_opt_for), so VFS_OPT's keys are a SUBSET of it.
    assert set(mounts_mod.VFS_OPT) <= set(mounts_mod._VFS_OPT_TO_SERVE_PARAM)
    assert set(mounts_mod._VFS_OPT_TO_SERVE_PARAM) - set(mounts_mod.VFS_OPT) == {"ReadOnly"}
    for obj_key in mounts_mod.VFS_OPT:
        flat_key = mounts_mod._VFS_OPT_TO_SERVE_PARAM[obj_key]
        v = mounts_mod.VFS_OPT[obj_key]
        expected = ("true" if v else "false") if isinstance(v, bool) else str(v)
        assert mounts_mod.SERVE_VFS_OPT[flat_key] == expected

    # _serve_vfs_opt_for is derived (not hand-written): it layers each mount's
    # read_only onto the shared serve params via the same table.
    assert mounts_mod._serve_vfs_opt_for({"read_only": True})["read_only"] == "true"
    assert mounts_mod._serve_vfs_opt_for({"read_only": False})["read_only"] == "false"
    assert mounts_mod._serve_vfs_opt_for({})["read_only"] == "false"


@pytest.mark.skipif(mounts_mod.sys.platform != "darwin",
                    reason="nfsmount timeo override is macOS-only")
def test_mount_raises_nfs_timeout_on_macos(home, rcd):
    # The loopback NFS client's low default timeout drops the whole mount on a
    # slow chunk fetch; attach must pass a raised timeo so local-path reads are
    # slow, not fatal. mountOpt is NFS transport, not a vfs option, so it does
    # not affect VFS sharing with the serve.
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["mountType"] == "nfsmount"
    assert body["mountOpt"]["ExtraOptions"] == mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]
    assert any(o.startswith("timeo=") for o in body["mountOpt"]["ExtraOptions"])


def test_mount_surfaces_rc_error(home, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "mount helper failed"})
    c = mounts_mod.add_mount("data", "remote:bucket")
    err = mounts_mod.attach_mount(c)
    assert err is not None and "mount helper failed" in err


# -- Windows (WinFsp) mount semantics ------------------------------------------
# The whole win32 mount path is exercised here with sys.platform patched to
# "win32" (real rclone/WinFsp is never invoked). WinFsp differs from the POSIX
# backends in two ways the code must special-case: the mountpoint leaf must NOT
# pre-exist, and there is no `umount` to force a detach.


def test_attach_win32_does_not_create_leaf_mountpoint(home, rcd, monkeypatch):
    # WinFsp creates the mountpoint itself; pre-creating the leaf makes
    # mount/mount fail. So on win32 attach_mount must never makedirs the leaf
    # (it still ensures the mounts ROOT, which carries the Spotlight marker).
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True, raising=False)
    made = []
    real_makedirs = mounts_mod.os.makedirs

    def spy_makedirs(path, *a, **k):
        made.append(path)
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(mounts_mod.os, "makedirs", spy_makedirs)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    assert mounts_mod.attach_mount(c) is None
    assert mp not in made               # leaf never pre-created (WinFsp does it)
    assert mounts_mod.mounts_dir() in made  # but the root is still ensured


def test_attach_win32_removes_stale_empty_leaf(home, rcd, monkeypatch):
    # A previous mount can leave an empty leaf dir behind; WinFsp refuses to
    # mount over it, so attach_mount rmdir's the stale empty leaf first.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True, raising=False)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    mounts_mod.os.makedirs(mp)  # stale empty leaf
    assert mounts_mod.os.path.isdir(mp)
    assert mounts_mod.attach_mount(c) is None
    # we removed the stale leaf and did NOT recreate it (WinFsp would),
    # while the stub reports it as a live mount.
    assert not mounts_mod.os.path.exists(mp)
    assert mounts_mod.os.path.ismount(mp)


def test_attach_win32_refuses_nonempty_leaf(home, rcd, monkeypatch):
    # A non-empty leaf is a user's data (or a foreign mount's contents) — never
    # rmdir it; error out and never even reach mount/mount.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True, raising=False)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    mounts_mod.os.makedirs(mp)
    keep = mounts_mod.os.path.join(mp, "keep.txt")
    with open(keep, "w") as f:
        f.write("x")
    err = mounts_mod.attach_mount(c)
    assert err is not None
    assert mounts_mod.os.path.isdir(mp) and mounts_mod.os.path.isfile(keep)
    assert not any(x[0] == "mount/mount" for x in rcd.calls)


def test_attach_win32_stale_leaf_raced_delete_proceeds(home, rcd, monkeypatch):
    # A concurrent delete of the stale leaf (FileNotFoundError from rmdir) means
    # the leaf is already gone — exactly what we wanted — so the mount proceeds.
    import errno as _errno
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    mounts_mod.os.makedirs(mp)  # isdir(mp) True so we enter the rmdir branch

    def raise_fnf(_p):
        raise FileNotFoundError(_errno.ENOENT, "raced away")

    monkeypatch.setattr(mounts_mod.os, "rmdir", raise_fnf)
    assert mounts_mod.attach_mount(c) is None  # proceeded to a normal mount
    assert any(x[0] == "mount/mount" for x in rcd.calls)


def test_attach_win32_stale_leaf_nonempty_reports_not_empty(home, rcd, monkeypatch):
    # ENOTEMPTY/EEXIST from rmdir keeps the "not empty — remove it" guidance.
    import errno as _errno
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    mounts_mod.os.makedirs(mp)

    def raise_notempty(_p):
        raise OSError(_errno.ENOTEMPTY, "directory not empty")

    monkeypatch.setattr(mounts_mod.os, "rmdir", raise_notempty)
    err = mounts_mod.attach_mount(c)
    assert err is not None and "not empty" in err
    assert not any(x[0] == "mount/mount" for x in rcd.calls)


def test_attach_win32_stale_leaf_other_oserror_reports_exception(home, rcd, monkeypatch):
    # A permission/sharing-violation OSError must report the ACTUAL error, not
    # falsely claim the leaf is non-empty.
    import errno as _errno
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    mounts_mod.os.makedirs(mp)

    def raise_perm(_p):
        raise PermissionError(_errno.EACCES, "access is denied")

    monkeypatch.setattr(mounts_mod.os, "rmdir", raise_perm)
    err = mounts_mod.attach_mount(c)
    assert err is not None
    assert "not empty" not in err
    assert "access is denied" in err or "Errno" in err
    assert not any(x[0] == "mount/mount" for x in rcd.calls)


@pytest.mark.parametrize("plat", ["darwin", "linux"])
def test_attach_posix_creates_leaf_mountpoint(home, rcd, monkeypatch, plat):
    # Regression guard: POSIX (FUSE/NFS) mounts over an EXISTING empty dir, so
    # the leaf is still pre-created on darwin/linux.
    monkeypatch.setattr(mounts_mod.sys, "platform", plat)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    assert not mounts_mod.os.path.exists(mp)
    assert mounts_mod.attach_mount(c) is None
    assert mounts_mod.os.path.isdir(mp)


def test_force_unmount_win32_no_shellouts_returns_none_when_unmounted(monkeypatch):
    # On win32 there is no umount/diskutil; _force_unmount must not shell out and
    # returns None the moment the reparse point is gone.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    ran = []
    monkeypatch.setattr(mounts_mod.subprocess, "run",
                        lambda cmd, *a, **k: ran.append(cmd))
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: False)
    assert mounts_mod._force_unmount("/x/mnt") is None
    assert ran == []


def test_force_unmount_win32_polls_until_unmounted(monkeypatch):
    # The poll loop returns None once os.path.ismount flips False.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    states = iter([True, True, False])
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: next(states, False))
    monkeypatch.setattr(mounts_mod, "_FORCE_UNMOUNT_WIN32_BUDGET_S", 5.0)
    assert mounts_mod._force_unmount("/x/mnt") is None


def test_force_unmount_win32_errors_when_still_mounted(monkeypatch):
    # Still mounted after the budget expires -> an error string, and never a
    # umount/diskutil shell-out.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    ran = []
    monkeypatch.setattr(mounts_mod.subprocess, "run",
                        lambda cmd, *a, **k: ran.append(cmd))
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(mounts_mod, "_FORCE_UNMOUNT_WIN32_BUDGET_S", 0.2)
    err = mounts_mod._force_unmount("/x/mnt")
    assert err is not None
    assert ran == []
    assert not any(c and c[0] in ("umount", "diskutil") for c in ran)


def test_winfsp_available_true_off_win32(monkeypatch):
    monkeypatch.setattr(mounts_mod.sys, "platform", "darwin")
    assert mounts_mod._winfsp_available() is True


def test_winfsp_available_finds_arm64_dll(tmp_path, monkeypatch):
    # WinFsp ships winfsp-a64.dll (not winfsp-x64.dll) on ARM64 Windows; the
    # detector must accept it via the ProgramFiles probe.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        monkeypatch.delenv(env, raising=False)
    binp = tmp_path / "WinFsp" / "bin"
    binp.mkdir(parents=True)
    (binp / "winfsp-a64.dll").write_bytes(b"")  # ARM64 DLL only, no x64
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert mounts_mod._winfsp_available() is True


def test_attach_win32_missing_winfsp_returns_friendly_error(home, monkeypatch):
    # Missing WinFsp fails fast with an install hint, before any rcd work.
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: False)
    called = []
    monkeypatch.setattr(mounts_mod, "ensure_rcd",
                        lambda *a, **k: called.append(True) or 1)
    c = mounts_mod.add_mount("data", "remote:bucket")
    err = mounts_mod.attach_mount(c)
    assert err is not None
    assert "WinFsp" in err and "https://winfsp.dev/rel/" in err
    assert called == []  # bailed before ensure_rcd


def test_attach_win32_with_winfsp_proceeds(home, rcd, monkeypatch):
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True)
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.attach_mount(c) is None
    assert any(x[0] == "mount/mount" for x in rcd.calls)


def test_attach_win32_adopt_path_not_gated_by_winfsp(home, rcd, monkeypatch):
    # A mount that survived a restart (already a LIVE kernel mount) must take the
    # adopt/reconcile branch on win32 even when the WinFsp detector reports False
    # — the driver false-negatives on a non-default install location, or WinFsp
    # was removed while rcd + the mount stay alive under the Job Object. The gate
    # exists to fail NEW mounts fast, not to declare a healthy adopted mount
    # broken (run_automount would otherwise report every survivor as failed).
    monkeypatch.setattr(mounts_mod.sys, "platform", "win32")
    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: False)
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    err = mounts_mod.attach_mount(c)
    assert err is None  # adopted, not rejected with the WinFsp install message
    assert not any(x[0] == "mount/mount" for x in rcd.calls)  # no new mount made


def test_unmount_calls_rc(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.detach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/unmount"]
    assert body["mountPoint"] == mounts_mod.mountpoint(c)


def test_rc_mtime_for_answers_from_rc_api_not_kernel(home, rcd, monkeypatch):
    # The fs/events poller must learn a mount-backed file's mtime from the rcd
    # rc API (operations/stat), never a kernel os.stat — that GETATTR is what
    # killed a mount in the stat-storm incident. Verify it calls operations/stat
    # with the mount's remote + relative path, returns ModTime, and never stats.
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["operations/stat"] = {"item": {"ModTime": "2024-01-02T03:04:05Z"}}

    import os as _os
    calls = []
    real = _os.stat
    monkeypatch.setattr(mounts_mod.os, "stat",
                        lambda p, *a, **k: (calls.append(_os.fspath(p)), real(p, *a, **k))[1])

    path = _os.path.join(mp, "sub", "world.zarr")
    assert mounts_mod.rc_mtime_for(path) == "2024-01-02T03:04:05Z"
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/stat"]
    assert body == {"fs": "remote:bucket/prefix", "remote": "sub/world.zarr"}
    assert path not in calls


def test_rc_mtime_for_normalizes_mountpoint_root_to_empty_remote(home, rcd):
    # The mount ROOT watch (Listing on the mountpoint itself) must send remote
    # "" to operations/stat: _mount_for returns "." for the mountpoint, and
    # remote "." returns {"item": null} so the root watch would never prime.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/stat"] = {"item": {"ModTime": "2024-01-02T03:04:05Z"}}
    assert mounts_mod.rc_mtime_for(mounts_mod.mountpoint(c)) == "2024-01-02T03:04:05Z"
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/stat"]
    assert body["remote"] == ""  # "." normalized to the fs root


def test_rc_mtime_for_none_when_rcd_down(home):
    # rcd unreachable -> None ("unchanged"). Callers MUST NOT fall back to
    # os.stat here (that reintroduces the mount-killing hazard).
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"
    assert mounts_mod.rc_mtime_for(path) is None


def test_rc_mtime_for_none_on_rc_error_or_missing(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"
    # rc error (stub returns 404 for unset methods) -> None.
    assert mounts_mod.rc_mtime_for(path) is None
    # item present but no ModTime, or item null -> None.
    rcd.responses["operations/stat"] = {"item": {"Size": 1}}
    assert mounts_mod.rc_mtime_for(path) is None
    rcd.responses["operations/stat"] = {"item": None}
    assert mounts_mod.rc_mtime_for(path) is None


def test_rc_mtime_for_none_outside_any_mount(home, rcd):
    assert mounts_mod.rc_mtime_for("/tmp/not/a/mount/f.parquet") is None


def test_rc_stat_for_tri_state(home, rcd):
    # rc_stat_for distinguishes the three outcomes rc_mtime_for collapses to
    # None, so a caller can filter a genuinely-deleted mount file while still
    # failing open on anything indeterminate.
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"

    # Healthy rcd, item is a dict -> file exists (ModTime irrelevant to stat).
    rcd.responses["operations/stat"] = {"item": {"ModTime": "2024-01-02T03:04:05Z"}}
    assert mounts_mod.rc_stat_for(path) == "exists"
    rcd.responses["operations/stat"] = {"item": {"Size": 1}}  # exists, no ModTime
    assert mounts_mod.rc_stat_for(path) == "exists"

    # Healthy rcd, item is null -> a TRUSTWORTHY "the file is gone".
    rcd.responses["operations/stat"] = {"item": None}
    assert mounts_mod.rc_stat_for(path) == "missing"

    # Malformed answer (missing 'item' key, non-dict resp) -> indeterminate.
    rcd.responses["operations/stat"] = {"nope": 1}
    assert mounts_mod.rc_stat_for(path) == "indeterminate"


def test_rc_stat_for_indeterminate_on_error_or_no_rcd_or_no_mount(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"
    # rc error (stub 404s unset methods) -> indeterminate, NOT missing.
    assert mounts_mod.rc_stat_for(path) == "indeterminate"
    # Path under no mount record -> indeterminate.
    assert mounts_mod.rc_stat_for("/tmp/not/a/mount/f.parquet") == "indeterminate"


def test_rc_stat_for_indeterminate_when_rcd_down(home):
    # No live rcd port at all -> indeterminate (fail open), never "missing".
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"
    assert mounts_mod.rc_stat_for(path) == "indeterminate"


def test_rc_mtime_for_and_rc_stat_for_share_one_rc_call_semantics(home, rcd):
    # rc_mtime_for's documented None contract is preserved after being
    # reimplemented on the shared stat: exists-but-no-ModTime and item-null
    # both still yield None.
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/f.parquet"
    rcd.responses["operations/stat"] = {"item": {"Size": 1}}
    assert mounts_mod.rc_mtime_for(path) is None
    assert mounts_mod.rc_stat_for(path) == "exists"
    rcd.responses["operations/stat"] = {"item": None}
    assert mounts_mod.rc_mtime_for(path) is None
    assert mounts_mod.rc_stat_for(path) == "missing"


def test_rc_kind_for_four_state(home, rcd):
    # rc_kind_for extends rc_stat_for with the IsDir bit operations/stat already
    # carries, so a condition-gate shim can tell os.path.isfile from isdir over a
    # mount WITHOUT the cold negative kernel LOOKUP that lists the whole S3 prefix
    # and wedges the mount.
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/store"

    rcd.responses["operations/stat"] = {"item": {"IsDir": True}}
    assert mounts_mod.rc_kind_for(path) == "dir"
    rcd.responses["operations/stat"] = {"item": {"IsDir": False, "Size": 12}}
    assert mounts_mod.rc_kind_for(path) == "file"
    rcd.responses["operations/stat"] = {"item": {"Size": 12}}  # IsDir absent -> file
    assert mounts_mod.rc_kind_for(path) == "file"
    rcd.responses["operations/stat"] = {"item": None}  # healthy rcd: gone
    assert mounts_mod.rc_kind_for(path) == "missing"
    rcd.responses["operations/stat"] = {"nope": 1}  # malformed -> indeterminate
    assert mounts_mod.rc_kind_for(path) == "indeterminate"


def test_rc_kind_for_indeterminate_on_error_or_no_rcd_or_no_mount(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/store"
    # rc error (stub 404s unset methods) and a path under no mount -> indeterminate.
    assert mounts_mod.rc_kind_for(path) == "indeterminate"
    assert mounts_mod.rc_kind_for("/tmp/not/a/mount/store") == "indeterminate"


def test_rc_stat_result_synthesizes_from_operations_stat(home, rcd):
    # A mount stat is answered off the kernel: st_mode's dir/file bit, st_size,
    # and st_mtime come from operations/stat, never a kernel GETATTR.
    import stat as _stat

    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/store"

    rcd.responses["operations/stat"] = {
        "item": {"IsDir": True, "ModTime": "2024-01-02T03:04:05Z"}}
    st = mounts_mod.rc_stat_result(path)
    assert _stat.S_ISDIR(st.st_mode)
    assert st.st_mtime == mounts_mod.rc_modtime_epoch("2024-01-02T03:04:05Z")

    rcd.responses["operations/stat"] = {
        "item": {"IsDir": False, "Size": 99, "ModTime": "2024-01-02T03:04:05Z"}}
    st = mounts_mod.rc_stat_result(path)
    assert _stat.S_ISREG(st.st_mode)
    assert st.st_size == 99


def test_rc_stat_result_raises_like_kernel_on_missing_and_indeterminate(home, rcd):
    # Fail exactly like the kernel os.stat it replaces so callers' 404 handling
    # holds: FileNotFoundError when the rcd confirms the item gone, OSError on any
    # indeterminate outcome — and NEVER fall back to a kernel GETATTR.
    c = mounts_mod.add_mount("data", "remote:bucket")
    path = mounts_mod.mountpoint(c) + "/store"

    rcd.responses["operations/stat"] = {"item": None}
    with pytest.raises(FileNotFoundError):
        mounts_mod.rc_stat_result(path)

    rcd.responses["operations/stat"] = {"nope": 1}  # malformed -> indeterminate
    with pytest.raises(OSError):
        mounts_mod.rc_stat_result(path)


def test_rc_read_bounded_reads_over_http_serve(home, monkeypatch):
    # The condition-gate shim's open() reads the one bounded zarr.json over the
    # mount's localhost HTTP serve (serve_url_for), never a kernel open/read.
    import io as _io

    calls = []

    def _fake_serve_url_for(p):
        return "http://127.0.0.1:59999/store/zarr.json"

    def _fake_urlopen(req, timeout=None):
        calls.append((req.full_url, dict(req.headers), timeout))
        return _io.BytesIO(b'{"node_type": "group"}')

    monkeypatch.setattr(mounts_mod, "serve_url_for", _fake_serve_url_for)
    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", _fake_urlopen)

    data = mounts_mod.rc_read_bounded("/mnt/store/zarr.json")
    assert data == b'{"node_type": "group"}'
    # Bounded: a Range header caps the read.
    assert "Range" in calls[0][1] or "range" in {k.lower() for k in calls[0][1]}


def test_rc_read_bounded_raises_oserror_without_serve_or_on_transport_error(home, monkeypatch):
    # No live serve, or any transport failure -> OSError so the gate fails closed.
    monkeypatch.setattr(mounts_mod, "serve_url_for", lambda p: None)
    with pytest.raises(OSError):
        mounts_mod.rc_read_bounded("/mnt/store/zarr.json")

    monkeypatch.setattr(mounts_mod, "serve_url_for", lambda p: "http://127.0.0.1:1/x")

    def _boom(req, timeout=None):
        raise mounts_mod.urllib.error.URLError("refused")

    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", _boom)
    with pytest.raises(OSError):
        mounts_mod.rc_read_bounded("/mnt/store/zarr.json")


def test_is_mount_backed_follows_symlink_into_mounts(home, tmp_path):
    # A symlink whose target is inside the mounts dir must classify as
    # mount-backed (else it lands on the kernel os.stat ticker — the GETATTR
    # storm). A pure abspath string check misses it; realpath resolution catches
    # it. Direct mount paths and genuine local paths keep classifying by string.
    import os as _os
    mounts_root = mounts_mod.mounts_dir()
    _os.makedirs(_os.path.join(mounts_root, "s3demo"), exist_ok=True)
    real_target = _os.path.join(mounts_root, "s3demo", "world.zarr")
    _os.makedirs(real_target, exist_ok=True)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    link = local_dir / "shortcut"
    _os.symlink(real_target, link)

    assert mounts_mod.is_mount_backed(str(link)) is True          # symlink -> mount
    assert mounts_mod.is_mount_backed(real_target) is True        # direct mount path
    assert mounts_mod.is_mount_backed(str(local_dir)) is False    # genuine local dir


def test_broken_mount_error_normalizes_non_abspath_input(home, rcd, monkeypatch):
    # A request path carrying ".." must be abspath-normalized before the
    # mounts-root prefix check, or a broken mount misclassifies as a plain 400
    # instead of the 503 "reconnect".
    c = mounts_mod.add_mount("s3demo", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    # Mount is recorded but not actually mounted -> broken.
    messy = _os.path.join(mp, "sub", "..", "data")  # normalizes to <mp>/data
    err = mounts_mod.broken_mount_error(messy)
    assert err is not None and "reconnect" in err.lower()


# -- rc_list_dir -----------------------------------------------------------
#
# A mount-backed directory listing must come from the rcd rc API
# (operations/list), never a kernel os.scandir: a READDIR on a flat S3 prefix
# with millions of keys forces rclone's VFS to enumerate the whole directory
# before the kernel gets a single entry, blowing past the macOS NFS deadman
# and killing the mount (the mur-sst incident).


def test_rc_list_dir_calls_operations_list_with_fs_and_remote(home, rcd, monkeypatch):
    import os as _os

    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["operations/list"] = {"list": [
        {"Name": "a", "IsDir": True, "Size": -1, "ModTime": "2024-01-02T03:04:05Z"},
        {"Name": "b.txt", "IsDir": False, "Size": 7, "ModTime": "2024-01-02T03:04:05Z"},
    ]}
    # Never touch the mount path through the kernel.
    monkeypatch.setattr(mounts_mod.os, "scandir",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scandir!")))

    entries = mounts_mod.rc_list_dir(_os.path.join(mp, "sub"))
    assert [e["Name"] for e in entries] == ["a", "b.txt"]
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body == {"fs": "remote:bucket/prefix", "remote": "sub",
                    "opt": {"noMimeType": True}}


def test_rc_list_dir_normalizes_mountpoint_root_to_empty_remote(home, rcd):
    # _mount_for returns "." for the mountpoint itself; operations/list wants
    # "" for the fs root ("." yields a nonsense result).
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    mounts_mod.rc_list_dir(mounts_mod.mountpoint(c))
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/list"]
    assert body["remote"] == ""


def test_rc_list_dir_unavailable_when_rcd_down(home):
    c = mounts_mod.add_mount("data", "remote:bucket")
    with pytest.raises(mounts_mod.RcListUnavailable):
        mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/sub")


def test_rc_list_dir_unavailable_outside_any_mount(home, rcd):
    with pytest.raises(mounts_mod.RcListUnavailable):
        mounts_mod.rc_list_dir("/tmp/not/a/mount")


def test_rc_list_dir_error_when_rcd_rejects_listing(home, rcd):
    # rcd is up but operations/list errors (stub 404s the unset method): the
    # remote path is not a listable directory (it's a file). Distinct from a
    # timeout / a down rcd so the caller can answer 400 "not a directory".
    c = mounts_mod.add_mount("data", "remote:bucket")
    with pytest.raises(mounts_mod.RcListError) as exc:
        mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/file.parquet")
    assert not isinstance(exc.value, (mounts_mod.RcListTimeout,
                                      mounts_mod.RcListUnavailable))


def test_rc_list_dir_timeout_raises_rc_list_timeout(home, rcd):
    # A directory too large to enumerate hits the hard timeout; the request
    # fails rather than the kernel readdir wedging the mount.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    rcd.delay["operations/list"] = 1.0
    with pytest.raises(mounts_mod.RcListTimeout):
        mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/huge", timeout=0.2)


def test_rc_modtime_epoch_parses_rfc3339_and_passes_sentinel():
    import datetime as _dt

    # 'Z', fractional seconds, and sub-microsecond precision all parse.
    assert mounts_mod.rc_modtime_epoch("1970-01-01T00:00:01Z") == 1.0
    assert mounts_mod.rc_modtime_epoch(
        "1970-01-01T00:00:01.123456789Z") == pytest.approx(1.123456, abs=1e-6)
    # The synthetic-dir sentinel (2000-01-01) is passed through like any stamp.
    sentinel = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc).timestamp()
    assert mounts_mod.rc_modtime_epoch("2000-01-01T00:00:00Z") == sentinel
    assert mounts_mod.rc_modtime_epoch(None) is None
    assert mounts_mod.rc_modtime_epoch("not-a-date") is None


def test_rc_modtime_epoch_normalizes_any_fractional_digit_count():
    # rclone emits 1-9 fractional digits, but py3.10's fromisoformat accepts
    # only 3 or 6 (7+ never parse anywhere); an off count used to silently drop
    # the mtime. Every digit count must now parse, on 'Z' and on a numeric
    # offset alike. Base instant is 2024-01-02T03:04:05 UTC.
    import datetime as _dt
    base = _dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=_dt.timezone.utc).timestamp()
    for n in range(1, 10):
        digits = "123456789"[:n]
        # The value the parser should see is 0.<digits> truncated to microseconds.
        expected_frac = float("0." + digits)
        got_z = mounts_mod.rc_modtime_epoch(f"2024-01-02T03:04:05.{digits}Z")
        assert got_z == pytest.approx(base + expected_frac, abs=1e-6), n
        got_off = mounts_mod.rc_modtime_epoch(f"2024-01-02T03:04:05.{digits}+00:00")
        assert got_off == pytest.approx(base + expected_frac, abs=1e-6), n


def test_mounted_paths_merges_listmounts(home, rcd, monkeypatch):
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    rcd.responses["mount/listmounts"] = {"mountPoints": [{"Fs": c["remote"], "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mp in mounts_mod.mounted_paths()
    view = mounts_mod.mount_view(c)
    assert view["mounted"] is True and view["mountpoint"] == mp
    assert view["state"] == "mounted"


def test_mount_rejects_mountpoint_serving_other_remote(home, rcd, monkeypatch):
    # A stale mount from a deleted same-name mount must not pass for the
    # new remote: rcd lists the old fs at the mountpoint -> mount errors.
    c = mounts_mod.add_mount("data", "remote:new")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:old", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    err = mounts_mod.attach_mount(c)
    assert err is not None and "remote:old" in err
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mount_adopts_matching_existing_mount(home, rcd, monkeypatch):
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.attach_mount(c) is None
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mounted_paths_empty_when_rcd_down(home):
    # No state file, no daemon: status degrades to unmounted, never raises.
    # (ensure_rcd would spawn; mounted_paths must NOT spawn just to read.)
    assert mounts_mod.mounted_paths() == set()


# -- endpoints -------------------------------------------------------------------

FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


@pytest.fixture()
def client(home):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    return TestClient(create_app(start_dir=str(home)))


def test_get_shape(client, rcd):
    data = client.get("/api/mounts").json()
    assert data["mounts"] == []
    assert set(data["rclone"]) == {"available", "version", "remotes", "suggested"}


def test_writes_require_fused_header(client):
    assert client.post("/api/mounts", json={"name": "x", "remote": "r:"}).status_code == 403
    assert client.delete("/api/mounts/nope").status_code == 403


def test_create_validates_before_mounting(client, rcd):
    r = client.post("/api/mounts", json={"name": "a/b", "remote": "r:"}, headers=FUSED)
    assert r.status_code == 400
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_create_mounts_and_persists(client, rcd):
    r = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "data"
    assert any(m == "mount/mount" for m, _ in rcd.calls)
    assert len(client.get("/api/mounts").json()["mounts"]) == 1


def test_create_rolls_back_store_on_mount_failure(client, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "boom"})
    r = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 502
    assert client.get("/api/mounts").json()["mounts"] == []


def test_mount_unmount_delete_unknown_id(client, rcd):
    assert client.post("/api/mounts/nope/mount", headers=FUSED).status_code == 404
    assert client.post("/api/mounts/nope/unmount", headers=FUSED).status_code == 404
    assert client.delete("/api/mounts/nope", headers=FUSED).status_code == 404


def test_mount_view_has_no_automount_field(client, rcd):
    m = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()
    # automount is implicit for every mount now — the field is gone.
    assert "automount" not in m
    assert set(m) == {"id", "name", "remote", "mountpoint", "mounted", "state",
                      "read_only", "builtin", "restart_reason"}


def test_delete_unmounts_and_removes(client, rcd):
    cid = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    assert client.delete(f"/api/mounts/{cid}", headers=FUSED).status_code == 200
    assert client.get("/api/mounts").json()["mounts"] == []
    assert any(m == "mount/unmount" for m, _ in rcd.calls)


def test_delete_rejects_builtin_mount(client, rcd, tmp_path, monkeypatch):
    # BUGBOT: nothing stopped the shipped Learn mount from being deleted like
    # any other mount — the record only reappears at the next full SERVER
    # restart, while the already-open Sidebar's learnMountReady state never
    # rechecks once true, leaving a dead Learn link for the rest of the
    # session. Bundled read-only content shouldn't be removable by a user
    # action in the first place.
    zp = tmp_path / "learn.zip"
    zp.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty-zip EOCD; content unused
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(zp))
    mounts_mod.ensure_learn_mount()
    builtin = next(m for m in mounts_mod.list_mounts() if m.get("builtin"))
    r = client.delete(f"/api/mounts/{builtin['id']}", headers=FUSED)
    assert r.status_code == 400
    assert "bundled" in r.json()["error"].lower()
    assert any(m["id"] == builtin["id"] for m in mounts_mod.list_mounts())


def test_create_s3_remote_builds_rclone_argv(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    r = client.post("/api/mounts/remotes", json={
        "name": "mys3",
        "params": {"access_key_id": "AK", "secret_access_key": "SK",
                   "endpoint": "https://e.example", "region": "us-east-1"},
    }, headers=FUSED)
    assert r.status_code == 200
    cmd = seen["cmd"]
    assert cmd[:4] == ["/usr/bin/rclone", "config", "create", "mys3"]
    assert "s3" in cmd and "AK" in cmd and "https://e.example" in cmd


def test_create_remote_rejects_bad_name(client):
    r = client.post("/api/mounts/remotes", json={"name": "a:b"}, headers=FUSED)
    assert r.status_code == 400


# -- credential auto-detection (keyless env_auth remotes) ------------------------


def test_aws_profiles_and_suggestions_from_dotfiles(tmp_path, monkeypatch):
    (tmp_path / "credentials").write_text(
        "[default]\naws_access_key_id = AK\n\n[work]\naws_access_key_id = WK\n")
    (tmp_path / "config").write_text(
        "[default]\nregion = us-east-1\n\n[profile prod]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    assert mounts_mod._aws_profiles() == ["default", "prod", "work"]
    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    # "[profile prod]" in config is unwrapped; default keeps the bare "aws" name
    assert by_id["aws-profile:default"]["remote_name"] == "aws"
    assert by_id["aws-profile:work"]["remote_name"] == "aws-work"
    assert by_id["aws-profile:prod"]["params"] == {
        "provider": "AWS", "env_auth": "true", "profile": "prod"}


def test_suggestions_view_hides_already_materialized(monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "backend": "s3", "params": {"provider": "AWS", "env_auth": "true"}},
    ])
    # kind defaults to "detected" for entries that don't set it.
    assert mounts_mod._suggestions_view([]) == [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "kind": "detected"}]
    assert mounts_mod._suggestions_view(["aws-env:"]) == []


def test_public_bucket_suggestion_always_present(monkeypatch):
    """The anonymous public-bucket remote is offered even with no AWS/gcloud
    credentials at all — it's what lets a user mount open data without keys."""
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)

    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    pub = by_id["aws-open-public"]
    assert pub["remote_name"] == "aws-open"
    assert pub["kind"] == "public"
    # anonymous: env_auth off, no key material anywhere in the spec
    assert pub["params"]["env_auth"] == "false"
    assert not any("secret" in k or "key" in k for k in pub["params"])


def test_public_suggestion_hidden_once_materialized(monkeypatch):
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)
    # not yet created → shown under kind="public"
    assert any(s["id"] == "aws-open-public" and s["kind"] == "public"
               for s in mounts_mod._suggestions_view([]))
    # once aws-open: exists it drops out (shows under Remotes instead)
    assert not any(s["id"] == "aws-open-public"
                   for s in mounts_mod._suggestions_view(["aws-open:"]))


_AWS_OPEN_SUGG = {
    "id": "aws-open-public",
    "label": "AWS S3 — public buckets (no credentials)",
    "remote_name": "aws-open", "backend": "s3", "kind": "public",
    "params": {"provider": "AWS", "env_auth": "false"},
}


def test_remote_label_matches_suggestion():
    """A remote whose stored config matches a suggestion's backend+params reuses
    that suggestion's friendly label — one stable human name across its lifecycle."""
    configs = {"aws-open": {"type": "s3", "provider": "AWS", "env_auth": "false"}}
    assert (mounts_mod._remote_label("aws-open:", [_AWS_OPEN_SUGG], configs)
            == "AWS S3 — public buckets (no credentials)")


def test_remote_label_falls_back_to_bare_name():
    """An unknown/custom remote (no matching suggestion) keeps its bare rclone
    name as the label."""
    assert mounts_mod._remote_label("myminio:", [], {}) == "myminio:"


def test_remote_label_ignores_name_collision():
    """Provenance, not name: a user's own remote merely named `aws` whose config
    differs from the default-profile suggestion must NOT inherit that label —
    the dropdown would otherwise claim the wrong credential source for the mount."""
    sugg = {"id": "aws-profile:default", "label": "AWS S3 — default profile",
            "remote_name": "aws", "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true", "profile": "default"}}
    # a custom MinIO remote that just happens to be named "aws"
    configs = {"aws": {"type": "s3", "provider": "Minio",
                       "endpoint": "http://localhost:9000"}}
    assert mounts_mod._remote_label("aws:", [sugg], configs) == "aws:"


def test_rclone_state_labels_materialized_remote(monkeypatch):
    """_rclone_state exposes remotes as {name,label}: a materialized suggestion
    (aws-open:, config matches) carries its friendly label, a custom remote
    (myminio:) its bare name. The name stays the verbatim rclone mount base."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_AWS_OPEN_SUGG])
    _fake_rclone(monkeypatch, existing_remotes=("aws-open:", "myminio:"),
                 configs={"aws-open": {"type": "s3", "provider": "AWS",
                                       "env_auth": "false"},
                          "myminio": {"type": "s3", "provider": "Minio"}})

    state = mounts_mod._rclone_state()
    assert state["remotes"] == [
        {"name": "aws-open:", "label": "AWS S3 — public buckets (no credentials)"},
        {"name": "myminio:", "label": "myminio:"},
    ]
    # the materialized aws-open drops out of the suggestions (shown under Remotes)
    assert not any(s["id"] == "aws-open-public" for s in state["suggested"])


def test_detect_materializes_public_anonymous_remote(client, monkeypatch):
    """Selecting the built-in public option creates an anonymous S3 remote —
    no secret material, env_auth=false — so unsigned requests reach open data."""
    created = []
    _fake_rclone(monkeypatch, existing_remotes=(), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-open-public"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "aws-open:"
    [cmd] = created
    assert cmd[:5] == ["/usr/bin/rclone", "config", "create", "aws-open", "s3"]
    assert "env_auth" in cmd and "false" in cmd
    assert not any("secret" in str(x).lower() for x in cmd)


def test_gcs_public_bucket_suggestion_always_present(monkeypatch):
    """The anonymous GCS public-bucket remote is offered alongside aws-open,
    even with no gcloud credentials — anonymous=true needs no key material."""
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)

    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    pub = by_id["gcs-open-public"]
    assert pub["remote_name"] == "gcs-open"
    assert pub["kind"] == "public"
    assert pub["backend"] == "google cloud storage"
    assert pub["params"] == {"anonymous": "true"}


def test_gcs_env_credentials_suggestion_detected(monkeypatch):
    """GOOGLE_APPLICATION_CREDENTIALS (a service-account JSON path) is detected
    symmetrically with AWS_ACCESS_KEY_ID → a keyless env_auth=true GCS
    suggestion; absent, it isn't offered. No key material is copied."""
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/creds/sa.json")
    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    sug = by_id["gcs-env"]
    assert sug["remote_name"] == "gcs-env"
    assert sug["backend"] == "google cloud storage"
    assert sug["params"] == {"env_auth": "true"}
    assert not any("secret" in k or "key" in k for k in sug["params"])

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    assert "gcs-env" not in by_id


def test_detect_materializes_gcs_public_anonymous_remote(client, monkeypatch):
    """Selecting the built-in GCS public option creates an anonymous GCS remote
    — no key material, anonymous=true — reaching public buckets unsigned."""
    created = []
    _fake_rclone(monkeypatch, existing_remotes=(), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "gcs-open-public"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "gcs-open:"
    [cmd] = created
    assert cmd[:5] == ["/usr/bin/rclone", "config", "create", "gcs-open",
                       "google cloud storage"]
    assert "anonymous" in cmd and "true" in cmd
    assert not any("secret" in str(x).lower() for x in cmd)


def test_gcs_anonymous_detected_read_only():
    """anonymous=true GCS is the gcs-open shape — unauthenticated requests can
    never take a write, so it must read-only detect like anonymous S3 does."""
    assert mounts_mod._gcs_anonymous(
        {"type": "google cloud storage", "anonymous": "true"})
    assert not mounts_mod._gcs_anonymous({"type": "google cloud storage"})
    assert not mounts_mod._gcs_anonymous(
        {"type": "s3", "anonymous": "true"})


_DETECTED_SUGG = {
    "id": "aws-env", "label": "AWS S3 — environment credentials",
    "remote_name": "aws-env", "backend": "s3",
    "params": {"provider": "AWS", "env_auth": "true"},
}


def _fake_rclone_probe(monkeypatch, lsd_rc=0, lsd_stderr="", record=None,
                       existing_remotes=(), delete_rc=0):
    """Like _fake_rclone but the `lsd` credential probe can be made to fail
    with a given stderr; records EVERY argv (create, lsd, delete). Pass
    `existing_remotes` to make `listremotes` report already-created remotes
    (exercises the idempotent re-entry path) and `delete_rc` to make the
    rollback `config delete` exit non-zero."""
    def fake_run(cmd, **kw):
        if record is not None:
            record.append(cmd)

        class R:
            returncode = (lsd_rc if "lsd" in cmd
                          else delete_rc if "delete" in cmd else 0)
            stderr = lsd_stderr if "lsd" in cmd else ""
            stdout = ("rclone v1.2\n" if "version" in cmd
                      else "{}" if "dump" in cmd
                      else "".join(f"{r}\n" for r in existing_remotes)
                      if "listremotes" in cmd else "")
        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")


def test_detect_rejects_expired_credentials(client, monkeypatch):
    """Materializing a detected credential source whose keys are stale (expired
    STS/SSO token) fails with an actionable message and rolls the half-created
    remote back — a broken remote must not linger inviting doomed mounts."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    calls = []
    _fake_rclone_probe(
        monkeypatch, lsd_rc=1, record=calls,
        lsd_stderr="ERROR: ExpiredToken: The provided token has expired.")

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 502
    assert "expired" in r.json()["error"].lower()
    assert any(c[:3] == ["/usr/bin/rclone", "config", "delete"] for c in calls)


def test_detect_rejects_google_expired_or_revoked_token(client, monkeypatch):
    """Google ADC/OAuth refresh failures surface as "Token has been expired or
    revoked." — no invalid_grant, and it matches neither "has expired" nor "is
    expired". _BAD_CRED_MARKERS must still classify it as expired creds, or
    stale GCS creds slip through to the opaque reconnect path this replaces."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    calls = []
    _fake_rclone_probe(
        monkeypatch, lsd_rc=1, record=calls,
        lsd_stderr="Token has been expired or revoked.")

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 502
    assert "expired" in r.json()["error"].lower()
    assert any(c[:3] == ["/usr/bin/rclone", "config", "delete"] for c in calls)


def test_detect_accepts_access_denied_probe(client, monkeypatch):
    """AccessDenied means valid keys without ListBuckets permission — the probe
    must not reject those; only credential-shaped failures do."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    _fake_rclone_probe(monkeypatch, lsd_rc=1,
                       lsd_stderr="ERROR: AccessDenied: Access Denied")

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "aws-env:"


def test_detect_skips_probe_for_public_remotes(client, monkeypatch):
    """Anonymous public remotes carry no credentials to go stale — no lsd
    probe runs for them (it would only add latency and network flake)."""
    calls = []
    _fake_rclone_probe(monkeypatch, record=calls)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-open-public"}, headers=FUSED)
    assert r.status_code == 200
    assert not any("lsd" in c for c in calls)


def test_detect_reports_failed_rollback(client, monkeypatch):
    """When creds are expired the half-created remote is rolled back — but if
    the `config delete` itself fails (non-zero), the remote may still exist, so
    the 502 must say so rather than returning the bare cred error as if cleanup
    succeeded (a lingering remote would be reported ok on the next detect)."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    calls = []
    _fake_rclone_probe(
        monkeypatch, lsd_rc=1, record=calls, delete_rc=1,
        lsd_stderr="ERROR: ExpiredToken: The provided token has expired.")

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 502
    err = r.json()["error"].lower()
    assert "expired" in err  # keep the cred-error wording
    assert "could not be removed" in err or "manually" in err
    assert any(c[:3] == ["/usr/bin/rclone", "config", "delete"] for c in calls)


def test_detect_reprobes_existing_detected_remote_now_expired(client, monkeypatch):
    """Idempotent re-entry must not report an already-existing detected remote
    healthy on faith: if its creds have since expired, re-detect returns the
    502 cred error, NOT {"ok": True} — otherwise the stale remote invites a
    doomed mount just like a freshly created one would."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    calls = []
    _fake_rclone_probe(
        monkeypatch, lsd_rc=1, record=calls, existing_remotes=("aws-env:",),
        lsd_stderr="ERROR: ExpiredToken: The provided token has expired.")

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 502
    assert "expired" in r.json()["error"].lower()
    # re-entry re-probed (lsd) but did NOT re-create or delete the remote
    assert any("lsd" in c for c in calls)
    assert not any("create" in c for c in calls)
    assert not any("delete" in c for c in calls)


def test_detect_existing_detected_remote_still_valid_returns_ok(client, monkeypatch):
    """Regression: an already-existing detected remote whose creds are still
    valid re-probes cleanly and returns {"ok": True} — the re-probe closes the
    stale-cred hole without breaking the healthy idempotent path."""
    monkeypatch.setattr(mounts_mod, "_credential_suggestions",
                        lambda: [_DETECTED_SUGG])
    calls = []
    _fake_rclone_probe(monkeypatch, lsd_rc=0, record=calls,
                       existing_remotes=("aws-env:",))

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["name"] == "aws-env:"
    assert not any("create" in c for c in calls)  # not re-created


def _fake_rclone(monkeypatch, existing_remotes=(), record=None, configs=None):
    """Stub rclone_bin + subprocess.run: version/listremotes/config-dump canned,
    every other argv appended to `record` and reported as success. `configs` is
    the {bare_name: cfg} map `rclone config dump` returns (JSON-encoded)."""
    def fake_run(cmd, **kw):
        if record is not None and "create" in cmd:
            record.append(cmd)

        class R:
            returncode = 0
            stderr = ""
            stdout = ("rclone v1.2\n" if "version" in cmd
                      else json.dumps(configs or {}) if "dump" in cmd
                      else "".join(f"{r}\n" for r in existing_remotes)
                      if "listremotes" in cmd else "")
        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")


def test_detect_materializes_keyless_remote(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-profile:work", "label": "AWS S3 — work profile",
         "remote_name": "aws-work", "backend": "s3",
         "params": {"provider": "AWS", "env_auth": "true", "profile": "work"}},
    ])
    created = []
    _fake_rclone(monkeypatch, existing_remotes=(), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-profile:work"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "aws-work:"
    [cmd] = created
    assert cmd[:5] == ["/usr/bin/rclone", "config", "create", "aws-work", "s3"]
    assert "env_auth" in cmd and "true" in cmd and "work" in cmd
    # keyless — no secret material is ever passed
    assert not any("secret" in str(x).lower() for x in cmd)


def test_detect_is_idempotent_when_remote_exists(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "backend": "s3", "params": {"provider": "AWS", "env_auth": "true"}},
    ])
    created = []
    _fake_rclone(monkeypatch, existing_remotes=("aws-env:",), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["name"] == "aws-env:"
    assert created == []  # already present → no create attempted


def test_detect_unknown_source_is_404(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [])
    _fake_rclone(monkeypatch)
    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "nope"}, headers=FUSED)
    assert r.status_code == 404


def test_detect_requires_fused_header(client):
    assert client.post("/api/mounts/remotes/detect",
                       json={"id": "x"}).status_code == 403


# -- unmount-busy: release tile daemons, retry once -------------------------------


@pytest.fixture()
def tile_daemon(tmp_path, monkeypatch):
    """A stub tile-server daemon (records /quit) plus a state file pointing at
    it, wired in as one of the module's DAEMON_STATE_FILES."""
    quits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            quits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"bye")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / "daemon.json"
    state.write_text(json.dumps(
        {"port": server.server_address[1], "pid": 1, "token": "tok-test"}))
    missing = tmp_path / "absent" / "daemon.json"  # the parallel file, absent
    monkeypatch.setattr(mounts_mod, "DAEMON_STATE_FILES", (str(state), str(missing)))
    yield quits
    server.shutdown()


def test_unmount_busy_quits_daemons_and_retries(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = [(500, {"error": "device busy"}), {}]
    assert mounts_mod.detach_mount(c) is None
    assert tile_daemon == ["/quit?t=tok-test"]   # token forwarded (D122 gate)
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 2


def test_unmount_success_never_touches_daemons(home, rcd, tile_daemon):
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.detach_mount(c) is None
    assert tile_daemon == []


def test_unmount_non_busy_error_skips_daemons(home, rcd, tile_daemon):
    # Only a busy error means a daemon holds a file open; on any other
    # failure quitting tile servers would kill unrelated local previews.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "some rc failure"})
    err = mounts_mod.detach_mount(c)
    assert err is not None and "some rc failure" in err
    assert tile_daemon == []
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 1


def test_unmount_still_busy_after_release_reports_error(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    err = mounts_mod.detach_mount(c)
    assert err is not None and "hold a file open" in err
    assert tile_daemon == ["/quit?t=tok-test"]


def test_delete_blocked_while_still_mounted(client, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    cid = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    mp = mounts_mod.mountpoint(mounts_mod.get_mount(cid))
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    r = client.delete(f"/api/mounts/{cid}", headers=FUSED)
    assert r.status_code == 502 and "not deleted" in r.json()["error"]
    assert len(client.get("/api/mounts").json()["mounts"]) == 1


# -- health detection + reconnect (dead/wedged mounts) ----------------------------
#
# The failure these cover: the rclone daemon (or its NFS serve) dies while the
# kernel mount entry survives. os.path.ismount() still says True, listings
# return stale/empty data, and a plain unmount fails. The mount must report
# "disconnected" (not a green "mounted") and reconnect must force-clear the
# mountpoint before remounting.

import os as _os


def _make_mount(home, rcd, name="data", remote="remote:bucket", served=True):
    c = mounts_mod.add_mount(name, remote)
    mp = mounts_mod.mountpoint(c)
    _os.makedirs(mp, exist_ok=True)
    if served:
        rcd.responses["mount/listmounts"] = {
            "mountPoints": [{"Fs": remote, "MountPoint": mp}]}
    return c, mp


def test_state_mounted_when_served_and_listable(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "mounted"


def test_state_disconnected_when_kernel_mount_has_no_daemon(home, rcd, monkeypatch):
    # ismount True but rcd doesn't list it: the daemon that served it is gone.
    c, mp = _make_mount(home, rcd, served=False)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "disconnected"


def test_state_stale_when_rcd_tracks_a_dropped_kernel_mount(home, rcd):
    # rcd still lists the mount but the kernel mount is gone: the mountpoint
    # is a plain local dir masquerading as remote data. This is the 2026-07-16
    # split-brain (user hit "Disconnect" on the macOS dialog; kernel unmounted
    # but mount/listmounts still showed the mount) — reported as the distinct
    # "stale" state, not "disconnected".
    c, mp = _make_mount(home, rcd)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "stale"


def test_state_disconnected_when_listing_hangs(home, rcd, monkeypatch):
    # A wedged NFS mount blocks listdir forever; the probe times out instead.
    c, mp = _make_mount(home, rcd)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    ev = threading.Event()

    def hang(p):
        ev.wait(5)
        return []

    monkeypatch.setattr(mounts_mod.os, "listdir", hang)
    try:
        state = mounts_mod.mount_state(c, mounts_mod.mounted_paths(), timeout=0.2)
    finally:
        ev.set()  # release the probe thread
    assert state == "disconnected"


def test_state_unmounted_when_nothing_there(home, rcd):
    c, mp = _make_mount(home, rcd, served=False)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "unmounted"


# -- broken_mount_error: expired detected credentials ----------------------
#
# A mount backed by detected (env_auth) credentials keeps mounting fine, then
# stops flowing when the SSO/STS token expires — the kernel raises an opaque
# I/O error, so mount_state reads "disconnected" exactly like a dead daemon.
# But "reconnect" can't fix an expired token: broken_mount_error probes the
# remote and, when the failure is credential-shaped, tells the user to re-auth.


def _stub_lsd(monkeypatch, rc=0, lsd_stderr="", record=None):
    """Stub rclone_bin + subprocess.run so the credential probe's `lsd` returns
    a chosen returncode/stderr; config/get still routes to the stub rcd."""
    def fake_run(cmd, **kw):
        if record is not None:
            record.append(cmd)

        class R:
            returncode = rc
            stderr = lsd_stderr
            stdout = ""
        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")


def _disconnected_mount(home, rcd, monkeypatch, remote="corp:bucket"):
    c, mp = _make_mount(home, rcd, remote=remote, served=False)
    # ismount True + not served -> "disconnected" (kernel mount, daemon gone).
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    return c, mp


def test_broken_mount_error_reports_expired_credentials(
        home, rcd, monkeypatch, fresh_upstream):
    c, mp = _disconnected_mount(home, rcd, monkeypatch)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=1,
              lsd_stderr="ERROR: ExpiredToken: The provided token has expired.")

    err = mounts_mod.broken_mount_error(_os.path.join(mp, "data"))
    assert err is not None
    assert "expired" in err.lower() and "refresh" in err.lower()
    # Must NOT send the user to the reconnect button — that won't re-auth.
    assert "reconnect" not in err.lower()


def test_broken_mount_error_routes_reauthed_creds_to_restart(
        home, rcd, monkeypatch, fresh_upstream):
    # env_auth remote, disconnected, but the creds probe SUCCEEDS: the user
    # re-authed and the long-lived daemon is still holding the stale keys.
    # Reconnect can't fix that — only a restart re-reads the refreshed creds.
    # Regression guard for the exact credential-expiry bug being fixed.
    c, mp = _disconnected_mount(home, rcd, monkeypatch)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=0)

    err = mounts_mod.broken_mount_error(_os.path.join(mp, "data"))
    assert err is not None
    assert "restart" in err.lower()
    # Must NOT send the user to Reconnect — it hits the same stale daemon.
    assert "reconnect" not in err.lower()


def test_broken_mount_error_skips_cred_probe_for_non_env_auth(
        home, rcd, monkeypatch, fresh_upstream):
    # A remote that isn't env_auth-backed can't have detected creds expire —
    # no lsd probe runs (it would only add latency), and the generic message
    # stands.
    c, mp = _disconnected_mount(home, rcd, monkeypatch)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false",
                                   "access_key_id": "AKIA"}
    calls = []
    _stub_lsd(monkeypatch, rc=1, lsd_stderr="ExpiredToken", record=calls)

    err = mounts_mod.broken_mount_error(_os.path.join(mp, "data"))
    assert err is not None and "reconnect" in err.lower()
    assert not any("lsd" in cmd for cmd in calls)


def test_broken_mount_error_no_cred_probe_when_never_mounted(
        home, rcd, monkeypatch, fresh_upstream):
    # "unmounted" (never mounted / cleanly disconnected) is a user action, not
    # a credential failure — the probe must not run for it.
    c, mp = _make_mount(home, rcd, remote="corp:bucket", served=False)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    calls = []
    _stub_lsd(monkeypatch, rc=1, lsd_stderr="ExpiredToken", record=calls)

    err = mounts_mod.broken_mount_error(_os.path.join(mp, "data"))
    assert err is not None and "not mounted" in err.lower()
    assert not any("lsd" in cmd for cmd in calls)


def test_reconnect_force_unmounts_dead_mount_then_remounts(home, rcd, monkeypatch):
    # rcd's own unmount fails (the wedged-NFS case) -> force umount -> remount.
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["mount/unmount"] = (500, {"error": "failed to umount the NFS volume"})
    still_mounted = {"v": True}
    # Wedged at entry (still_mounted); once the force umount clears it, the
    # remount re-attaches via the stub's mount table (mount/mount -> mounted),
    # which is what attach_mount's post-mount verify now checks.
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mp and (still_mounted["v"] or p in rcd.mounted))
    forced = []

    def fake_run(cmd, **kw):
        forced.append(cmd)
        still_mounted["v"] = False  # umount succeeded

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    assert mounts_mod.reconnect_mount(c) is None
    assert forced and forced[0][:1] == ["umount"]
    assert any(m == "mount/mount" for m, _ in rcd.calls)


def test_reconnect_stops_serve_so_it_rebinds_to_fresh_vfs(home, rcd):
    # rcd shares one VFS between a mount and its serve; unmounting tears that
    # VFS down and leaves the serve wedged on uncached reads (verified against
    # real rclone). Reconnect must stop the serve so the following sync_serves
    # starts a fresh one bound to the remounted VFS. The serve's options match
    # SERVE_VFS_OPT, so the ONLY serve/stop here is reconnect's, not a drift.
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-live", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   **mounts_mod.SERVE_VFS_OPT}}]}
    assert mounts_mod.reconnect_mount(c) is None
    assert ("serve/stop", {"id": "http-live"}) in rcd.calls
    assert any(m == "mount/mount" for m, _ in rcd.calls)


def test_reconnect_reports_force_unmount_failure(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd, served=False)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "umount: busy"

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    err = mounts_mod.reconnect_mount(c)
    assert err is not None and "force unmount" in err
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_forced_detach_escalates_on_rc_failure(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["mount/unmount"] = (500, {"error": "failed to umount the NFS volume"})
    still_mounted = {"v": True}
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mp and still_mounted["v"])

    def fake_run(cmd, **kw):
        still_mounted["v"] = False

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    assert mounts_mod.detach_mount(c, force=True) is None
    # unforced stays loud: same rc failure, no force fallback
    still_mounted["v"] = True
    err = mounts_mod.detach_mount(c)
    assert err is not None and "unmount failed" in err


def test_reconnect_endpoint(client, rcd, monkeypatch):
    m = client.post("/api/mounts", json={"name": "data", "remote": "r:bucket"},
                    headers=FUSED).json()
    assert client.post(f"/api/mounts/{m['id']}/reconnect").status_code == 403
    r = client.post(f"/api/mounts/{m['id']}/reconnect", headers=FUSED)
    assert r.status_code == 200
    assert client.post("/api/mounts/nope/reconnect", headers=FUSED).status_code == 404


def test_fs_list_errors_for_dead_mount_instead_of_empty(client, rcd, home):
    # The user-visible bug: a dead mount leaves a plain empty dir at the
    # mountpoint, and the folder view rendered it as an ordinary empty
    # folder. It must 503 with a pointer to the Mounts page instead.
    m = client.post("/api/mounts", json={"name": "data", "remote": "r:bucket"},
                    headers=FUSED).json()
    mp = m["mountpoint"]
    _os.makedirs(mp, exist_ok=True)
    # rcd tracks the mount (create succeeded) but nothing is kernel-mounted:
    # state != "mounted" -> the empty listing is not trustworthy.
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "r:bucket", "MountPoint": mp}]}
    # Creating the mount kernel-mounted it (mount/mount -> stub.mounted); now
    # simulate the kernel dropping it while rcd keeps listing it — the "stale"
    # split-brain, where ismount is False but the listing is not trustworthy.
    rcd.mounted.discard(mp)
    r = client.get("/api/fs/list", params={"path": mp})
    assert r.status_code == 503
    assert "Mounts page" in r.json()["error"]


def test_fs_list_normal_empty_dir_still_lists(client, home, tmp_path):
    d = tmp_path / "plain-empty"
    d.mkdir()
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 200 and r.json()["entries"] == []


# -- automount at startup --------------------------------------------------------


def test_run_automount_mounts_every_mount(home, rcd):
    mounts_mod.add_mount("one", "r:one")
    mounts_mod.add_mount("two", "r:two")
    mounts_mod.run_automount()
    mounted = sorted(b["fs"] for m, b in rcd.calls if m == "mount/mount")
    # No per-mount opt-in: every mount is remounted at startup.
    assert mounted == ["r:one", "r:two"]


def test_run_automount_skips_already_mounted(home, rcd):
    c = mounts_mod.add_mount("auto", "r:one")
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "r:one", "MountPoint": mounts_mod.mountpoint(c)}]}
    mounts_mod.run_automount()
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_run_automount_syncs_serves_after_learn_removal(home, rcd, tmp_path, monkeypatch):
    # BUGBOT: when the learn zip disappears and it was the only mount,
    # ensure_learn_mount removes the record (and stops its rc serve
    # directly via _force_detach_learn_mount), but serves.json on disk is
    # ONLY ever rewritten by sync_serves — an early return before it (the
    # old run_automount behavior, taken because list_mounts() is now empty)
    # would leave a stale {mountpoint: dead_url} entry that serve_url_for
    # keeps resolving forever.
    zp = tmp_path / "learn.zip"
    zp.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty-zip EOCD; content unused
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(zp))
    mounts_mod.run_automount()
    learn_mp = mounts_mod.mountpoint({"name": "learn"})
    with open(mounts_mod.serves_path()) as f:
        assert learn_mp in json.load(f)

    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP")
    mounts_mod.run_automount()  # learn was the only mount -> list_mounts() is now empty
    with open(mounts_mod.serves_path()) as f:
        assert learn_mp not in json.load(f)


# -- http serves (the duckdb reader's mounted-parquet fast path) -----------------


def test_attach_starts_http_serve_and_writes_map(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert body["type"] == "http"
    assert body["fs"] == "remote:bucket/prefix"
    # Serve-side vfs cache is a capped on-disk range cache: unlike DuckDB's
    # in-RAM external file cache it survives reconnects and server restarts.
    # serve/start takes vfs options as FLAT vfs_* rc params — a `vfsOpt`
    # object is mount/mount-only and gets silently ignored (cache stays off).
    assert "vfsOpt" not in body
    assert body["vfs_cache_mode"] == "full"
    assert body["vfs_cache_max_size"] == "20Gi"
    # The serve is started with the FULL derived option set (not just cache
    # mode/size) so it shares the mount's VFS — every SERVE_VFS_OPT key present.
    for k, v in mounts_mod.SERVE_VFS_OPT.items():
        assert body[k] == v
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


# -- read-only enforcement at the rclone layer (INCIDENT 2026-07-16) -------------
#
# read_only was purely an app-level guard; the rclone VFS still cached writes
# and looped forever on S3 PutObject 403s. These assert the flag now reaches
# both layers that accept bytes: the VFS (mount vfsOpt.ReadOnly + serve
# read_only, which must agree or the shared VFS splits) and, on macOS, the
# kernel NFS mount ("rdonly" ExtraOption).


def test_read_only_mount_sets_vfs_readonly_and_serve_flag(home, rcd):
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["vfsOpt"]["ReadOnly"] is True
    # The serve carries the matching flat flag so mount and serve share one VFS.
    [(_, serve)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert serve["read_only"] == "true"


def test_read_write_mount_has_readonly_false_and_no_serve_flag(home, rcd):
    c = mounts_mod.add_mount("rw", "remote:bucket", read_only=False)
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    # Explicit False, not omission, so it reads back matching the serve.
    assert body["vfsOpt"]["ReadOnly"] is False
    [(_, serve)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert serve["read_only"] == "false"


@pytest.mark.skipif(mounts_mod.sys.platform != "darwin",
                    reason="rdonly rides macOS nfsmount ExtraOptions; Linux FUSE differs")
def test_read_only_mount_adds_rdonly_extraoption_on_macos(home, rcd):
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert "rdonly" in body["mountOpt"]["ExtraOptions"]
    # The transport tuning is still there — rdonly is additive.
    assert "timeo=600" in body["mountOpt"]["ExtraOptions"]


@pytest.mark.skipif(mounts_mod.sys.platform != "darwin",
                    reason="rdonly rides macOS nfsmount ExtraOptions; Linux FUSE differs")
def test_read_write_mount_has_no_rdonly_on_macos(home, rcd):
    c = mounts_mod.add_mount("rw", "remote:bucket", read_only=False)
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert "rdonly" not in body["mountOpt"]["ExtraOptions"]


# rcd never echoes a live mount's vfsOpt back, so the only way to notice that
# an adopted mount predates a read_only change is to record what was baked in
# (mounted_read_only) and remount on mismatch — otherwise a legacy writable
# VFS survives every restart no matter what the record says (Bugbot, PR #157).


def test_attach_records_mounted_read_only(home, rcd):
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    assert mounts_mod.attach_mount(c) is None
    assert mounts_mod.get_mount(c["id"])["mounted_read_only"] is True


# attach_mount verifies the kernel mount actually attached after mount/mount:
# on a flap-prone loopback NFS mount rcd can report mount/mount success while
# the kernel mount silently never takes (or drops within seconds), leaving
# rcd's serve alive over an empty mountpoint — the "stale" split-brain that a
# /reconnect used to report as OK. See _await_ismount / attach_mount.


def test_attach_errors_when_kernel_mount_never_attaches(home, rcd, monkeypatch):
    c = mounts_mod.add_mount("data", "remote:bucket")
    # rcd answers mount/mount 200, but the kernel mount never appears.
    monkeypatch.setattr(mounts_mod, "_await_ismount", lambda mp: False)
    err = mounts_mod.attach_mount(c)
    assert err is not None and "did not attach" in err
    # It still issued the mount/mount — the failure is detected after, not by
    # skipping the attempt.
    assert any(m == "mount/mount" for m, _ in rcd.calls)


def test_await_ismount_true_promptly_when_mounted(monkeypatch):
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: True)
    assert mounts_mod._await_ismount("/x", deadline=5.0) is True


def test_await_ismount_false_after_deadline_when_absent(monkeypatch):
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: False)
    start = time.monotonic()
    assert mounts_mod._await_ismount("/x", deadline=0.15) is False
    assert time.monotonic() - start >= 0.15


def test_adopted_mount_remounts_when_vfs_predates_read_only(home, rcd, monkeypatch):
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    # Live rcd mount, but the record has no mounted_read_only: it was created
    # before the flag reached the rclone layer, so its VFS is writable.
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    # Kernel-mounted at entry (adopt check); reconnect's mount/unmount drops it
    # from the stub table, then the remount re-adds it — os.path.ismount tracks
    # that lifecycle so attach_mount's post-mount verify passes.
    rcd.mounted.add(mp)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p in rcd.mounted)
    assert mounts_mod.attach_mount(c) is None
    methods = [m for m, _ in rcd.calls]
    assert methods.index("mount/unmount") < methods.index("mount/mount")
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["vfsOpt"]["ReadOnly"] is True
    assert mounts_mod.get_mount(c["id"])["mounted_read_only"] is True


def test_adopted_mount_left_alone_when_vfs_matches_flag(home, rcd, monkeypatch):
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    c["mounted_read_only"] = True
    mounts_mod._update_mount(c)
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.attach_mount(c) is None
    assert not any(m in ("mount/mount", "mount/unmount") for m, _ in rcd.calls)


def test_run_automount_reapplies_read_only_to_adopted_mounts(home, rcd, monkeypatch):
    # The startup adopt path must go through attach_mount's already-mounted
    # branch, or a mount that survived the restart keeps its pre-flag
    # writable VFS forever (the incident's doomed-upload loop, reborn).
    c = mounts_mod.add_mount("ro", "remote:bucket", read_only=True)
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    seq = iter([True, True])  # live for automount + adopt checks, then unmounted
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: next(seq, False))
    mounts_mod.run_automount()
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["vfsOpt"]["ReadOnly"] is True


# -- split-brain (stale) detection + reconnect healing (INCIDENT 2026-07-16) -----


def test_reconnect_stale_unmounts_rcd_entry_before_remounting(home, rcd, monkeypatch):
    # Split-brain: rcd lists the mount, the kernel has already dropped it. rcd
    # would refuse to remount over its stale entry, so reconnect must issue
    # mount/unmount (clearing the entry) BEFORE mount/mount.
    c, mp = _make_mount(home, rcd)  # served=True
    # Stale: rcd lists it but the kernel dropped it (not in the stub table), so
    # ismount is False until the reconnect's remount re-adds it (mount/mount).
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p in rcd.mounted)
    assert mounts_mod.reconnect_mount(c) is None
    methods = [m for m, _ in rcd.calls]
    assert "mount/unmount" in methods and "mount/mount" in methods
    assert methods.index("mount/unmount") < methods.index("mount/mount")


# -- rcd log file + rotation (INCIDENT 2026-07-16: daemon had no --log-file) -----


def test_ensure_rcd_spawn_has_log_file_and_records_path(home, monkeypatch):
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)  # force a spawn
    argvs = []

    class FakePopen:
        def __init__(self, argv, **kw):
            argvs.append(argv)

    monkeypatch.setattr(mounts_mod.subprocess, "Popen", FakePopen)
    # Stand in for the post-spawn core/pid poll so no real rclone is needed.
    monkeypatch.setattr(mounts_mod, "_rc", lambda *a, **k: {"pid": 999})

    mounts_mod.ensure_rcd()
    [argv] = argvs
    log_flags = [a for a in argv if a.startswith("--log-file=")]
    assert log_flags and log_flags[0].endswith("rcd.log")
    assert "--log-level" in argv
    assert argv[argv.index("--log-level") + 1] == "INFO"
    # The log path is recorded in rcd.json alongside port/pid.
    state = json.load(open(mounts_mod._rcd_state_path()))
    assert state["log"] == mounts_mod._rcd_log_path()


def test_rcd_log_rotates_past_cap(home):
    log = mounts_mod._rcd_log_path()
    _os.makedirs(_os.path.dirname(log), exist_ok=True)
    with open(log, "wb") as f:
        f.write(b"x" * (mounts_mod.RCD_LOG_MAX_BYTES + 1))
    assert mounts_mod._rotate_rcd_log() == log
    assert _os.path.exists(log + ".1")  # rolled aside
    assert not _os.path.exists(log)     # rclone will recreate it fresh


def test_rcd_log_not_rotated_below_cap(home):
    log = mounts_mod._rcd_log_path()
    _os.makedirs(_os.path.dirname(log), exist_ok=True)
    with open(log, "wb") as f:
        f.write(b"small")
    mounts_mod._rotate_rcd_log()
    assert not _os.path.exists(log + ".1")


def test_copytruncate_rcd_log_caps_a_live_daemons_log(home):
    # The detached rcd outlives server restarts and holds the log inode open, so
    # os.replace can't rotate it — copytruncate copies the contents aside and
    # truncates the live file IN PLACE (same inode, so rclone keeps appending).
    log = mounts_mod._rcd_log_path()
    _os.makedirs(_os.path.dirname(log), exist_ok=True)
    with open(log, "wb") as f:
        f.write(b"y" * (mounts_mod.RCD_LOG_MAX_BYTES + 1))
    ino_before = _os.stat(log).st_ino
    mounts_mod._copytruncate_rcd_log()
    assert _os.path.exists(log)                       # live file kept in place
    assert _os.stat(log).st_ino == ino_before         # SAME inode (truncated, not replaced)
    assert _os.path.getsize(log) == 0                 # emptied
    assert _os.path.getsize(log + ".1") == mounts_mod.RCD_LOG_MAX_BYTES + 1  # contents saved


def test_copytruncate_rcd_log_noop_below_cap(home):
    log = mounts_mod._rcd_log_path()
    _os.makedirs(_os.path.dirname(log), exist_ok=True)
    with open(log, "wb") as f:
        f.write(b"small")
    mounts_mod._copytruncate_rcd_log()
    assert not _os.path.exists(log + ".1")
    assert _os.path.getsize(log) == len(b"small")


def test_copytruncate_rcd_log_swallows_errors(home):
    # Best-effort: a missing log (or any OSError) must never raise into startup.
    mounts_mod._copytruncate_rcd_log()  # no log file at all -> no exception


def test_attach_syncs_serves_when_already_mounted(home, rcd, monkeypatch):
    # The already-a-kernel-mount early return must still reconcile serves:
    # without one, /api/fs/raw falls back to reads through the kernel mount.
    c = mounts_mod.add_mount("data", "remote:bucket")
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mounts_mod.mountpoint(c))
    assert mounts_mod.attach_mount(c) is None
    assert not any(m == "mount/mount" for m, _ in rcd.calls)
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert body["fs"] == "remote:bucket"
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


def test_sync_serves_reuses_existing_serve(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    # A serve whose flat params exactly match this (read_write) mount's expected
    # option set — SERVE_VFS_OPT plus read_only=false — is reused, not
    # restarted. (An adopted serve missing read_only reads as drift and is
    # restarted; that rollout is covered by the stale-opts test below.)
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-live", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   **mounts_mod._serve_vfs_opt_for(c)}}]}
    mounts_mod.sync_serves()
    assert not any(m == "serve/start" for m, _ in rcd.calls)
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:41000"}


def test_sync_serves_restarts_serve_with_stale_vfs_opts(home, rcd):
    # Serves outlive server runs, so a SERVE_VFS_OPT change would never reach
    # an adopted serve unless the sync notices the drift and restarts it.
    # A serve started by the old vfsOpt-object code has NO flat vfs_* params
    # (rcd ignored the object) — exactly this drift, so the fix self-deploys.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-stale", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   "vfsOpt": {"CacheMode": "full", "CacheMaxSize": "5Gi"}}}]}
    mounts_mod.sync_serves()
    [(_, stop)] = [x for x in rcd.calls if x[0] == "serve/stop"]
    assert stop == {"id": "http-stale"}
    [(_, start)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert start["vfs_cache_mode"] == "full"
    assert start["vfs_cache_max_size"] == "20Gi"
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


def test_sync_serves_stops_orphaned_serve(home, rcd):
    # A serve whose mount record is gone (deleted mount) gets stopped.
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-orphan", "addr": "127.0.0.1:41001",
        "params": {"type": "http", "fs": "remote:deleted"}}]}
    mounts_mod.sync_serves()
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/stop"]
    assert body == {"id": "http-orphan"}
    assert json.load(open(mounts_mod.serves_path())) == {}


def test_sync_serves_without_daemon_clears_map(home):
    # No rcd: nothing can be served, so a stale map must not send the duckdb
    # reader to dead URLs (it would fall back, but why leave the trap).
    mounts_mod.add_mount("data", "remote:bucket")
    mounts_mod.sync_serves()
    assert json.load(open(mounts_mod.serves_path())) == {}


def test_sync_serves_survives_serve_start_failure(home, rcd):
    c = mounts_mod.add_mount("a", "remote:a")
    c2 = mounts_mod.add_mount("b", "remote:b")
    rcd.responses["serve/start"] = [
        (500, {"error": "boom"}),
        {"addr": "127.0.0.1:42000", "id": "http-b"},
    ]
    mounts_mod.sync_serves()
    serves = json.load(open(mounts_mod.serves_path()))
    # The failed remote is just absent; the other one still gets served.
    assert list(serves.values()) == ["http://127.0.0.1:42000"]


def test_serve_url_for_maps_and_quotes(home, rcd):
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mounts_mod.sync_serves()  # stub serve at 127.0.0.1:59999
    mp = mounts_mod.mountpoint(c)
    url = mounts_mod.serve_url_for(os.path.join(mp, "year=2022", "a b.parquet"))
    assert url == "http://127.0.0.1:59999/year%3D2022/a%20b.parquet"
    assert mounts_mod.serve_url_for("/somewhere/else.parquet") is None


def test_stat_marks_mount_backed_files_remote(client, home, rcd):
    import os

    # fs/stat routes a mount stat through the rc API, so the stub rcd answers
    # operations/stat for the mount-backed file (the local path still stats via
    # the kernel).
    rcd.responses["operations/stat"] = {"item": {"Size": 2}}
    c = mounts_mod.add_mount("data", "remote:bucket")  # a real record for _mount_for
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.parquet")
    open(f, "wb").write(b"pq")
    assert client.get("/api/fs/stat", params={"path": f}).json()["remote"] is True
    plain = os.path.join(str(home), "y.parquet")
    open(plain, "wb").write(b"pq")
    assert client.get("/api/fs/stat", params={"path": plain}).json()["remote"] is False


def test_fs_raw_proxies_range_from_mount_serve(client, home, rcd):
    """/api/fs/raw for a mount-backed path streams GETs from the mount's
    HTTP serve, not from the local filesystem. HEAD is answered from the
    rclone rcd (operations/list), NOT a kernel os.stat: a missing-sidecar HEAD
    is the cold-negative probe that would enumerate the whole remote prefix and
    wedge the mount. Here the rc size (77) intentionally differs from both the
    local file (11) and the serve body (12) to prove HEAD is rc-sourced."""
    import functools
    import http.server
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")  # what a (dead-mount) local read would see
    rcd.responses["operations/list"] = {"list": [
        {"Name": "x.bin", "IsDir": False, "Size": 77,
         "ModTime": "2024-01-02T03:04:05Z"}]}

    served = home / "served"
    served.mkdir()
    (served / "x.bin").write_bytes(b"REMOTE-BYTES")

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(H, directory=str(served)))
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import fused_render.shell.storage as storage
        storage.write_json(mounts_mod.serves_path(),
                           {mp: f"http://127.0.0.1:{srv.server_address[1]}"})
        r = client.get("/api/fs/raw", params={"path": f})
        assert r.status_code == 200 and r.content == b"REMOTE-BYTES"
        r = client.head("/api/fs/raw", params={"path": f})
        assert r.status_code == 200
        assert r.headers["content-length"] == "77"  # from the rcd, not kernel
        assert r.headers["accept-ranges"] == "bytes"
        assert "last-modified" in r.headers
        assert r.content == b""
    finally:
        srv.shutdown()


def test_fs_raw_directory_404s_not_listing(client, home, rcd):
    """A directory path under a served mount 404s on GET and HEAD. The serve
    (like rclone serve http) answers a directory with a 200 HTML listing, so
    without a guard the proxy would hand that listing back as raw bytes; a
    directory has no bytes to serve, so both verbs must 404. GET's guard is the
    proxy-path stat; HEAD detects the directory via the rclone rcd (IsDir), the
    mount-safe replacement for the kernel os.stat."""
    import functools
    import http.server
    import os
    import threading

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    d = os.path.join(mp, "subdir")
    os.makedirs(d)
    open(os.path.join(d, "x.bin"), "wb").write(b"CHUNK")
    # rcd reports `subdir` as a directory, so the HEAD probe 404s without a
    # kernel stat.
    rcd.responses["operations/list"] = {"list": [
        {"Name": "subdir", "IsDir": True, "Size": -1,
         "ModTime": "2024-01-02T03:04:05Z"}]}

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(H, directory=str(mp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import fused_render.shell.storage as storage
        storage.write_json(mounts_mod.serves_path(),
                           {mp: f"http://127.0.0.1:{srv.server_address[1]}"})
        # Browser-style request (Sec-Fetch-Mode present) skips the redirect
        # branch and reaches the proxy path, which is where a directory would
        # otherwise leak a 200 listing.
        h = {"sec-fetch-mode": "navigate"}
        r = client.get("/api/fs/raw", params={"path": d}, headers=h)
        assert r.status_code == 404
        r = client.head("/api/fs/raw", params={"path": d}, headers=h)
        assert r.status_code == 404
    finally:
        srv.shutdown()


def test_fs_raw_serve_dead_returns_503_never_kernel_reads_mount(client, home):
    """When the mount's HTTP serve is unreachable, /api/fs/raw must 503 rather
    than fall back to a local-file kernel read: reading through the kernel NFS
    mount is the wedge this whole subsystem exists to avoid, so even a file that
    happens to be cached locally is refused while the serve is down/respawning."""
    import os
    import socket

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    import fused_render.shell.storage as storage
    storage.write_json(mounts_mod.serves_path(), {mp: f"http://127.0.0.1:{dead}"})
    r = client.get("/api/fs/raw", params={"path": f})
    assert r.status_code == 503


def test_fs_raw_proxy_error_keeps_range_headers(client, home, monkeypatch):
    """An HTTP error from a live serve passes through WITH its protocol
    headers — a 416's `Content-Range: bytes */<size>` is how a range client
    (DuckDB httpfs) learns the file length."""
    import http.server
    import os
    import threading

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")
    # The warm-read fallthrough resolves a mount-backed path's shape through the
    # rcd (_mount_probe), never a kernel stat — stub the parent listing so the
    # file reads as a present regular object and the proxy is reached.
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda p, timeout=None: [{"Name": "x.bin", "IsDir": False,
                                                  "Size": 11,
                                                  "ModTime": "2024-01-02T03:04:05Z"}])

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(416)
            self.send_header("Content-Range", "bytes */12345")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import fused_render.shell.storage as storage
        storage.write_json(mounts_mod.serves_path(),
                           {mp: f"http://127.0.0.1:{srv.server_address[1]}"})
        r = client.get("/api/fs/raw", params={"path": f},
                       headers={"Range": "bytes=99999999-"})
        assert r.status_code == 416
        assert r.headers["content-range"] == "bytes */12345"
    finally:
        srv.shutdown()


# -- upstream_url_for (direct-to-store bypass) -------------------------------


@pytest.fixture()
def fresh_upstream():
    """The bypass memoizes per-remote capability, config, credentials and per-
    object links in module globals; clear around each test so results don't leak.
    Delegates to the production invalidator so every cache (incl. the gsign
    bearer-fallback marks and single-flight locks) is dropped via one registry."""
    mounts_mod._invalidate_upstream_caches()
    yield
    mounts_mod._invalidate_upstream_caches()


def test_upstream_url_prefers_presigned_link(home, rcd, fresh_upstream):
    import os

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a", "b.parquet")
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/publiclink"]
    assert body["fs"] == "remote:bucket" and body["remote"] == "a/b.parquet"
    # Second ask answers from the link cache — no extra rc round-trip.
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    assert len([x for x in rcd.calls if x[0] == "operations/publiclink"]) == 1


def test_upstream_url_public_s3_when_link_unsupported(home, rcd, fresh_upstream):
    import os

    # Anonymous S3 remotes can't presign ("unsupported signer type noAuth")
    # but don't need to — the plain public object URL works. The config makes
    # that knowable up front, so publiclink is never even attempted, and the
    # config is memoized: minting URLs for later objects on the same remote
    # (a zarr store touches thousands) is pure string building, no rc call.
    rcd.responses["operations/publiclink"] = (
        500, {"error": 'unsupported signer type "smithy.api#noAuth"'})
    rcd.responses["config/get"] = {
        "type": "s3", "provider": "AWS", "env_auth": "false",
        "region": "us-west-2"}
    c = mounts_mod.add_mount("data", "aws-open:bucket/pre fix")
    f = os.path.join(mounts_mod.mountpoint(c), "k ey.parquet")
    assert mounts_mod.upstream_url_for(f) == (
        "https://bucket.s3.us-west-2.amazonaws.com/pre%20fix/k%20ey.parquet")
    f2 = os.path.join(mounts_mod.mountpoint(c), "other.parquet")
    assert mounts_mod.upstream_url_for(f2) == (
        "https://bucket.s3.us-west-2.amazonaws.com/pre%20fix/other.parquet")
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []
    assert len([x for x in rcd.calls if x[0] == "config/get"]) == 1


def test_gcs_public_object_url_builds_unsigned_url(home, rcd, fresh_upstream):
    # Anonymous GCS objects are reachable by a plain path-style storage URL —
    # no region, no dotted-bucket rule. The key is percent-encoded exactly as
    # the S3 builder encodes it.
    mounts_mod._upstream_cfg["gcs-open"] = {
        "type": "google cloud storage", "anonymous": "true"}
    assert mounts_mod._gcs_public_object_url(
        "gcs-open:bucket/pre fix", "k ey.parquet") == (
        "https://storage.googleapis.com/bucket/pre%20fix/k%20ey.parquet")
    # A non-anonymous GCS remote has no reachable unsigned URL.
    mounts_mod._upstream_cfg["gcp"] = {"type": "google cloud storage"}
    assert mounts_mod._gcs_public_object_url("gcp:bucket", "x.parquet") is None


def test_upstream_url_public_gcs_when_anonymous(home, rcd, fresh_upstream):
    import os

    # Anonymous GCS remotes can never presign either — the config makes that
    # knowable up front, so publiclink is never attempted and the plain public
    # object URL is minted directly, config memoized for later objects.
    rcd.responses["config/get"] = {
        "type": "google cloud storage", "anonymous": "true"}
    c = mounts_mod.add_mount("open", "gcs-open:bucket/pre fix")
    f = os.path.join(mounts_mod.mountpoint(c), "k ey.parquet")
    assert mounts_mod.upstream_url_for(f) == (
        "https://storage.googleapis.com/bucket/pre%20fix/k%20ey.parquet")
    f2 = os.path.join(mounts_mod.mountpoint(c), "other.parquet")
    assert mounts_mod.upstream_url_for(f2) == (
        "https://storage.googleapis.com/bucket/pre%20fix/other.parquet")
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []
    assert len([x for x in rcd.calls if x[0] == "config/get"]) == 1


def test_upstream_url_non_anonymous_gcs_gets_no_public_url(home, rcd, fresh_upstream):
    import os

    # A non-anonymous GCS remote has no reachable unsigned URL. With no SA key
    # configured it is token-only-SHAPED, so it pins the bearer mode WITHOUT ever
    # calling publiclink (which always fails for GCS, PublicLink: False); with no
    # resolvable token (google-auth absent) upstream_url_for stays None and the
    # read falls through to the serve.
    rcd.responses["operations/publiclink"] = (500, {"error": "boom"})
    rcd.responses["config/get"] = {"type": "google cloud storage"}
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "x.parquet")
    assert mounts_mod.upstream_url_for(f) is None
    assert mounts_mod.upstream_url_for(f) is None
    assert mounts_mod._upstream_mode["gcp:bucket"] == "bearer"
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []


def test_upstream_url_none_is_remembered(home, rcd, fresh_upstream):
    import os

    # A credentialed remote that can't presign has no reachable direct URL;
    # the verdict is cached so the raw hot path doesn't re-ask rcd per read.
    rcd.responses["operations/publiclink"] = (500, {"error": "boom"})
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    c = mounts_mod.add_mount("data", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "x.parquet")
    assert mounts_mod.upstream_url_for(f) is None
    assert mounts_mod.upstream_url_for(f) is None
    assert len([x for x in rcd.calls if x[0] == "operations/publiclink"]) == 1


def test_upstream_url_none_outside_mounts(home, rcd, fresh_upstream):
    assert mounts_mod.upstream_url_for("/somewhere/else.parquet") is None


# -- sign mode: locally presigned URLs for credentialed plain-AWS S3 ---------

_CRED_S3_CFG = {"type": "s3", "provider": "AWS",
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "region": "us-east-1"}


def _query_of(url):
    import urllib.parse as up
    return dict(up.parse_qsl(up.urlsplit(url).query))


def test_upstream_sign_mode_presigns_credentialed_s3(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_S3_CFG
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (206, None))
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a", "b.parquet")
    url = mounts_mod.upstream_url_for(f)
    q = _query_of(url)
    assert url.startswith(
        "https://bucket.s3.us-east-1.amazonaws.com/pre/a/b.parquet?")
    assert q["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert "X-Amz-Signature" in q and "X-Amz-Expires" in q
    # publiclink is never consulted, and sign mode caches no per-object link.
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []
    assert mounts_mod._upstream_mode["corp:bucket/pre"] == "sign"
    assert mounts_mod._upstream_links == {}
    # Validation is one-time per fs: a second object does not re-validate.
    seen = []
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: seen.append(url) or (206, None))
    mounts_mod.upstream_url_for(
        os.path.join(mounts_mod.mountpoint(c), "c.parquet"))
    assert seen == []


def test_upstream_sign_dotted_bucket_is_path_style_and_signed(home, rcd, fresh_upstream, monkeypatch):
    # The regression for the dropped-URL gap: a dotted bucket can't be
    # virtual-hosted, and a SIGNED publiclink URL can't be rewritten path-style
    # (SigV4 covers Host) so today it's dropped. Sign mode builds path-style
    # from the start, so the signature is valid.
    import os

    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-west-2"}
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (200, None))
    c = mounts_mod.add_mount("corp", "corp:us-west-2.opendata.source.coop/foo")
    f = os.path.join(mounts_mod.mountpoint(c), "z.parquet")
    url = mounts_mod.upstream_url_for(f)
    assert url.startswith(
        "https://s3.us-west-2.amazonaws.com/"
        "us-west-2.opendata.source.coop/foo/z.parquet?")
    assert "X-Amz-Signature" in url


def test_upstream_sign_falls_back_to_publiclink_on_403(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_S3_CFG
    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (403, None))
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    assert mounts_mod._upstream_mode["corp:bucket/pre"] == "link"
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"]
    # A wrong/guessed region isn't left cached after a failed validation.
    assert "corp:bucket/pre" not in mounts_mod._upstream_region


def test_upstream_custom_endpoint_never_signs(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = {**_CRED_S3_CFG, "endpoint": "https://r2.example.com"}
    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    validated = []
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: validated.append(url) or (200, None))
    c = mounts_mod.add_mount("r2", "r2:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    assert validated == []  # never entered sign mode, so never validated
    assert mounts_mod._upstream_mode["r2:bucket"] == "link"


def test_upstream_sign_includes_session_token(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = {**_CRED_S3_CFG, "session_token": "FQoTOK//"}
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (200, None))
    c = mounts_mod.add_mount("corp", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    url = mounts_mod.upstream_url_for(f)
    assert "X-Amz-Security-Token=FQoTOK%2F%2F" in url


def test_upstream_sign_region_self_corrects(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-east-1"}
    seq = [(301, "eu-west-1"), (206, None)]
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: seq.pop(0))
    c = mounts_mod.add_mount("corp", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    url = mounts_mod.upstream_url_for(f)
    assert url.startswith("https://bucket.s3.eu-west-1.amazonaws.com/")
    assert mounts_mod._upstream_region["corp:bucket"] == "eu-west-1"
    assert seq == []  # exactly two probes: initial + one retry after correction
    # Later objects reuse the corrected region without re-validating.
    url2 = mounts_mod.upstream_url_for(
        os.path.join(mounts_mod.mountpoint(c), "b.parquet"))
    assert url2.startswith("https://bucket.s3.eu-west-1.amazonaws.com/")


def test_adopt_region_retries_when_this_request_signed_stale(fresh_upstream):
    # A sibling listing/probe already adopted the corrected region into the
    # shared map, but THIS request signed with the stale region — it must still
    # re-sign and retry once, not skip the retry because the map is already
    # right (which would fail it into the slow rc path).
    headers = {"x-amz-bucket-region": "eu-west-1"}
    mounts_mod._upstream_region["corp:bucket"] = "eu-west-1"
    assert mounts_mod._adopt_region_on_301(
        "corp:bucket", 301, headers, "us-east-1") is True
    # A request that already signed with the corrected region does not retry.
    assert mounts_mod._adopt_region_on_301(
        "corp:bucket", 301, headers, "eu-west-1") is False
    # No hint / non-correctable status -> never retries.
    assert mounts_mod._adopt_region_on_301(
        "corp:bucket", 301, {}, "us-east-1") is False
    assert mounts_mod._adopt_region_on_301(
        "corp:bucket", 403, headers, "us-east-1") is False


def test_adopt_region_on_307_temporary_redirect(fresh_upstream):
    # S3 answers a newly created bucket with 307 (Temporary Redirect) carrying
    # x-amz-bucket-region — it must be treated as correctable exactly like 301.
    headers = {"x-amz-bucket-region": "eu-west-1"}
    assert mounts_mod._adopt_region_on_301(
        "corp:bucket", 307, headers, "us-east-1") is True
    assert mounts_mod._upstream_region["corp:bucket"] == "eu-west-1"


def test_upstream_sign_region_self_corrects_on_307(home, rcd, fresh_upstream, monkeypatch):
    # sign-mode validation must adopt the region from a 307 (new-bucket
    # Temporary Redirect), not only a 301.
    import os

    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-east-1"}
    seq = [(307, "eu-west-1"), (206, None)]
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: seq.pop(0))
    c = mounts_mod.add_mount("corp", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    url = mounts_mod.upstream_url_for(f)
    assert url.startswith("https://bucket.s3.eu-west-1.amazonaws.com/")
    assert mounts_mod._upstream_region["corp:bucket"] == "eu-west-1"
    assert seq == []  # initial + one retry after the 307 correction


def test_upstream_anonymous_s3_unchanged_and_never_resolves(home, rcd, fresh_upstream, monkeypatch):
    # INVARIANT: an anonymous S3 mount is byte-identical to today — the
    # resolver is never called, no validation GET is issued, the URL carries no
    # X-Amz-* params, and the mode is "public".
    import os

    rcd.responses["config/get"] = {**_ANON_S3_CFG, "region": "us-west-2"}
    # Spy the REAL resolver entry points production uses (resolve_credentials is
    # no longer called on this path — repointed per finding 8). None must fire on
    # the anonymous path.
    resolver_calls = []

    def _spy(fnname):
        def f(*a, **k):
            resolver_calls.append(fnname)
        return f

    for fnname in ("resolve_static_credentials", "needs_botocore",
                   "resolve_botocore_chain", "resolve_credentials"):
        monkeypatch.setattr(mounts_mod.s3sign, fnname, _spy(fnname))
    validated = []
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: validated.append(url) or (200, None))
    c = mounts_mod.add_mount("open", "aws-open:bucket/pre fix")
    f = os.path.join(mounts_mod.mountpoint(c), "k ey.parquet")
    got = mounts_mod.upstream_url_for(f)
    assert got == (
        "https://bucket.s3.us-west-2.amazonaws.com/pre%20fix/k%20ey.parquet")
    assert "X-Amz" not in got
    assert resolver_calls == []
    assert validated == []
    assert mounts_mod._upstream_mode["aws-open:bucket/pre fix"] == "public"


def test_upstream_sign_demotes_to_link_when_creds_vanish(home, rcd, fresh_upstream, monkeypatch):
    # Finding 4: once sign mode is active, if creds stop resolving (rotated-away
    # key) the request must fall through to the publiclink ladder AND the mode
    # demote to "link" — not return None per-request forever.
    import os

    rcd.responses["config/get"] = _CRED_S3_CFG
    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (206, None))
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    first = mounts_mod.upstream_url_for(
        os.path.join(mounts_mod.mountpoint(c), "a.parquet"))
    assert first.startswith("https://bucket.s3.")  # presigned
    assert mounts_mod._upstream_mode["corp:bucket/pre"] == "sign"
    # Creds rotate away: the resolver now returns None and the cache expires.
    monkeypatch.setattr(mounts_mod.s3sign, "resolve_static_credentials",
                        lambda cfg: None)
    mounts_mod._cred_cache.clear()
    got = mounts_mod.upstream_url_for(
        os.path.join(mounts_mod.mountpoint(c), "b.parquet"))
    assert got == "https://signed.example/x?sig=1"  # fell back to publiclink
    assert mounts_mod._upstream_mode["corp:bucket/pre"] == "link"


def test_upstream_sign_negative_cache_skips_revalidation(home, rcd, fresh_upstream, monkeypatch):
    # Finding 5: when validation fails and no fallback can commit (publiclink
    # also failing), the failed validation is negative-cached so a second read
    # in the window doesn't re-run the blocking validation GET.
    import os

    rcd.responses["config/get"] = _CRED_S3_CFG
    rcd.responses["operations/publiclink"] = (500, {"error": "boom"})
    calls = []
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: calls.append(url) or (0, None))  # network fail
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    mp = mounts_mod.mountpoint(c)
    assert mounts_mod.upstream_url_for(os.path.join(mp, "a.parquet")) is None
    assert len(calls) == 1
    # Second request within the negative-cache window: no re-validation.
    assert mounts_mod.upstream_url_for(os.path.join(mp, "b.parquet")) is None
    assert len(calls) == 1
    # Mode was NOT permanently pinned to link on the inconclusive failure.
    assert mounts_mod._upstream_mode.get("corp:bucket/pre") != "sign"


def test_upstream_sign_single_flight_validates_once(home, rcd, fresh_upstream, monkeypatch):
    # Finding 2: N concurrent first reads must issue ONE validation, not N, and
    # the losing racer's fallback must never erase the winner's adopted region.
    import os

    rcd.responses["config/get"] = _CRED_S3_CFG
    rcd.responses["operations/publiclink"] = {"url": "https://pl.example/x"}
    calls = []
    calls_lock = threading.Lock()

    def slow_validate(url):
        with calls_lock:
            calls.append(url)
        time.sleep(0.25)  # hold the flight so siblings pile up
        return (206, None)

    monkeypatch.setattr(mounts_mod, "_sign_validation_status", slow_validate)
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    mp = mounts_mod.mountpoint(c)
    results = []
    res_lock = threading.Lock()

    def worker(i):
        u = mounts_mod.upstream_url_for(os.path.join(mp, f"obj{i}.parquet"))
        with res_lock:
            results.append(u)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1  # exactly one validation across all threads
    assert mounts_mod._upstream_mode["corp:bucket/pre"] == "sign"
    # Every request got a usable URL — signed (winner + latecomers) or the
    # publiclink fallback (racers that didn't win the flight).
    assert all(r is not None for r in results)


# -- upstream cache hygiene (Task 4) -----------------------------------------


def test_create_remote_invalidates_upstream_caches(client, fresh_upstream, monkeypatch):
    # The stale-_upstream_cfg bug: a changed key must be picked up without a
    # restart. Creating a remote clears every memoized upstream fact.
    mounts_mod._upstream_cfg["mys3"] = {"type": "s3", "stale": True}
    mounts_mod._upstream_mode["mys3:bucket"] = "link"
    mounts_mod._upstream_links[("mys3:bucket", "x")] = ("u", 9e18)
    mounts_mod._cred_cache["mys3"] = (None, 9e18)
    mounts_mod._upstream_region["mys3:bucket"] = "us-east-1"

    class _R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(mounts_mod.subprocess, "run", lambda cmd, **kw: _R())
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    r = client.post("/api/mounts/remotes", json={
        "name": "mys3",
        "params": {"access_key_id": "AK", "secret_access_key": "SK"}},
        headers=FUSED)
    assert r.status_code == 200
    assert mounts_mod._upstream_cfg == {}
    assert mounts_mod._upstream_mode == {}
    assert mounts_mod._upstream_links == {}
    assert mounts_mod._cred_cache == {}
    assert mounts_mod._upstream_region == {}


def test_delete_mount_invalidates_upstream_caches(client, rcd, fresh_upstream, monkeypatch):
    c = mounts_mod.add_mount("data", "remote:bucket")
    mounts_mod._upstream_cfg["remote"] = {"type": "s3"}
    mounts_mod._upstream_mode["remote:bucket"] = "link"
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: False)
    r = client.delete(f"/api/mounts/{c['id']}", headers=FUSED)
    assert r.status_code == 200
    assert mounts_mod._upstream_cfg == {}
    assert mounts_mod._upstream_mode == {}


def test_link_cache_cap_holds_under_many_inserts(home, fresh_upstream):
    now = time.monotonic()
    with mounts_mod._upstream_lock:
        for i in range(5000):
            mounts_mod._store_upstream_link((f"fs{i}", "r"), f"u{i}",
                                            now + 1800, now)
    assert len(mounts_mod._upstream_links) <= mounts_mod._UPSTREAM_LINKS_CAP


def test_link_cache_evicts_expired_before_oldest(home, fresh_upstream):
    now = time.monotonic()
    cap = mounts_mod._UPSTREAM_LINKS_CAP
    with mounts_mod._upstream_lock:
        for i in range(cap):  # fill to the cap with already-expired entries
            mounts_mod._store_upstream_link((f"old{i}", "r"), "u", now - 1, now)
        # One insert past the cap drops the expired ones, keeping the fresh one.
        mounts_mod._store_upstream_link(("fresh", "r"), "keep", now + 1800, now)
    assert ("fresh", "r") in mounts_mod._upstream_links
    assert len(mounts_mod._upstream_links) < cap


def test_session_token_remote_gets_short_link_ttl(home, rcd, fresh_upstream):
    import os

    # A custom-endpoint S3 remote carrying an STS session_token: not signable
    # (endpoint excluded), so it takes the publiclink path — and its link is
    # cached for the 5-minute clamp, not the 30-minute default.
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false",
                                   "endpoint": "https://r2.example.com",
                                   "session_token": "TOK"}
    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    c = mounts_mod.add_mount("corp", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    before = time.monotonic()
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    _url, expiry = mounts_mod._upstream_links[("corp:bucket", "a.parquet")]
    # Clamped to ~5 min, far below the 30-min default (small slack: `before` is
    # captured just before the function's own monotonic `now`).
    assert 240 < expiry - before < mounts_mod._SESSION_TOKEN_LINK_TTL_S + 60


def test_env_session_token_remote_gets_short_link_ttl(home, rcd, fresh_upstream, monkeypatch):
    import os

    # An STS token arriving via AWS_SESSION_TOKEN (not a config session_token)
    # on a non-signable custom-endpoint remote must clamp the link TTL too — the
    # resolved credentials carry the token even though the config doesn't.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY",
                       "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FQoGZ//envtoken")
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true",
                                   "endpoint": "https://r2.example.com"}
    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    c = mounts_mod.add_mount("corp", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    before = time.monotonic()
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    _url, expiry = mounts_mod._upstream_links[("corp:bucket", "a.parquet")]
    assert 240 < expiry - before < mounts_mod._SESSION_TOKEN_LINK_TTL_S + 60


def test_no_session_token_remote_keeps_default_link_ttl(home, rcd, fresh_upstream):
    import os

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    before = time.monotonic()
    mounts_mod.upstream_url_for(f)
    _url, expiry = mounts_mod._upstream_links[("remote:bucket", "a.parquet")]
    assert expiry - before > mounts_mod._SESSION_TOKEN_LINK_TTL_S


def test_fs_raw_redirects_cold_native_range_reads(client, home, rcd, fresh_upstream):
    """A GET from a native client (no Sec-Fetch-Mode) on a cold mount-backed
    file 307s to the store's direct URL — ranged or whole-file alike (zarr
    reads its many tiny metadata files whole); browser requests and HEADs
    keep today's serve proxy (a redirect would die on CORS / fail a
    GET-signed link). No stat gates the redirect: a getattr on a never-listed
    mount object is a full remote round trip, and the store 404s a missing
    object itself."""
    import os

    import fused_render.shell.storage as storage

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")
    # A live-looking serve entry; the serve itself is never reached by the
    # redirect path, and browser/HEAD fall back to the file when it's dead.
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})

    r = client.get("/api/fs/raw", params={"path": f},
                   headers={"Range": "bytes=0-3"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://signed.example/x"

    # Whole-file native GET: redirected too, and without any local stat —
    # a path that doesn't exist under the (fake) mount still redirects, so
    # the daemon's read never pays the VFS getattr round trip.
    r = client.get("/api/fs/raw", params={"path": f}, follow_redirects=False)
    assert r.status_code == 307
    ghost = os.path.join(mp, "not-listed-yet.bin")
    r = client.get("/api/fs/raw", params={"path": ghost},
                   follow_redirects=False)
    assert r.status_code == 307

    r = client.get("/api/fs/raw", params={"path": f},
                   headers={"Range": "bytes=0-3", "Sec-Fetch-Mode": "cors"},
                   follow_redirects=False)
    assert r.status_code != 307
    r = client.head("/api/fs/raw", params={"path": f}, follow_redirects=False)
    assert r.status_code != 307
    # HEAD still answers from the stat: missing files 404 locally.
    r = client.head("/api/fs/raw", params={"path": ghost},
                    follow_redirects=False)
    assert r.status_code == 404


def test_api_run_rewrites_raw_source_url_to_direct(
        client, home, rcd, fresh_upstream, tmp_path):
    """/api/run swaps a raw-proxy source_url for the store's direct URL on a
    cold mount-backed file (redirects defeat httpfs connection pooling — the
    reader must get the direct URL up front), and leaves everything else
    alone: non-mount paths, warm (prefetched) files, and foreign URLs."""
    import os
    from urllib.parse import quote

    import fused_render.shell.prefetch as prefetch_mod
    import fused_render.shell.storage as storage

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.parquet")
    open(f, "wb").write(b"PAR1")
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})

    echo = tmp_path / "echo.py"
    echo.write_text("def main(source_url=None):\n"
                    "    return {'source_url': source_url}\n",
                    encoding="utf-8")

    def run(src):
        r = client.post("/api/run", json={
            "py": str(echo), "params": {"source_url": src}},
            headers={"X-Fused": "1"})
        return r.json()["result"]["source_url"]

    raw = f"http://testserver/api/fs/raw?path={quote(f)}"
    # Cold mount-backed file: rewritten to the store URL.
    assert run(raw) == "https://signed.example/x"
    # Warm file (prefetch landed): the raw proxy stays, so reads replay from
    # the serve's local cache.
    with prefetch_mod._lock:
        prefetch_mod._jobs[f] = {"status": "done", "size": 4, "done": 4,
                                 "at": 0.0}
    try:
        assert run(raw) == raw
    finally:
        with prefetch_mod._lock:
            prefetch_mod._jobs.pop(f, None)
    # Non-mount path and foreign URLs: untouched.
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"x")
    unmounted = f"http://testserver/api/fs/raw?path={quote(str(plain))}"
    assert run(unmounted) == unmounted
    assert run("https://elsewhere.example/data.parquet") == \
        "https://elsewhere.example/data.parquet"


# -- read-only remotes -----------------------------------------------------
# A mount's writability is a property of the REMOTE (anonymous S3 can never
# take a write; an :http: backend has no write verbs at all), but the kernel
# mount can't say so: with CacheMode=full a write "succeeds" into the local
# VFS cache and only fails at async upload. So every attach re-detects
# read-onlyness non-mutatingly via rc (fsinfo features + remote config) and
# persists it on the mount record — unless the user chose the flag at create
# (read_only_user), which detection never overrides. Inconclusive probes
# persist nothing. server._writable consults the flag via mount_read_only().

FSINFO_RW = {"Features": {"Put": True, "PutStream": True, "Copy": True}}
FSINFO_RO = {"Features": {"Put": False, "PutStream": False, "Copy": False}}


def _attached(name, remote):
    m = mounts_mod.add_mount(name, remote)
    assert mounts_mod.attach_mount(m) is None
    return mounts_mod.get_mount(m["id"])


def test_attach_marks_anonymous_s3_read_only(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "provider": "AWS",
                                   "env_auth": "false"}
    assert _attached("pub", "aws-open:bucket")["read_only"] is True


def test_attach_marks_credentialed_s3_writable(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "provider": "AWS",
                                   "env_auth": "true", "profile": "default"}
    assert _attached("data", "aws:bucket")["read_only"] is False


def test_attach_marks_putless_backend_read_only(home, rcd):
    # An :http:-style backend advertises no write features at all — read-only
    # regardless of its config.
    rcd.responses["operations/fsinfo"] = FSINFO_RO
    rcd.responses["config/get"] = {"type": "http"}
    assert _attached("web", "web:")["read_only"] is True


def test_attach_persists_nothing_when_probe_inconclusive(home, rcd):
    # Neither probe answers (stub 404s both) — persist NO verdict: the mount
    # stays rw (the pre-flag behavior) and the next attach re-probes, so a
    # transient rcd hiccup at first attach can't freeze a wrong answer.
    m = _attached("data", "remote:bucket")
    assert "read_only" not in m
    # The probes come back — the same mount converges on the next attach.
    rcd.responses["operations/fsinfo"] = FSINFO_RO
    rcd.responses["config/get"] = {"type": "http"}
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is True


def test_attach_treats_missing_features_as_inconclusive(home, rcd):
    # fsinfo answering WITHOUT a Features map is version skew, not evidence
    # of read-onlyness — a writable remote must not get locked by it.
    rcd.responses["operations/fsinfo"] = {"Name": "remote"}
    rcd.responses["config/get"] = (404, {"error": "config not found"})
    assert "read_only" not in _attached("data", "remote:bucket")


def test_reattach_redetects_when_credentials_change(home, rcd):
    # Detected (not user-set) flags follow the remote: an anonymous S3 mount
    # flagged read-only flips back once credentials appear in its config.
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false"}
    m = _attached("pub", "aws-open:bucket")
    assert m["read_only"] is True
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is False


def test_explicit_read_only_flag_never_redetected(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RO  # would detect read-only
    rcd.responses["config/get"] = {"type": "http"}
    m = mounts_mod.add_mount("web", "web:", read_only=False)
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is False


def test_add_mount_rejects_non_bool_read_only(home):
    # Straight off a JSON body: bool("false") is True, so anything but a real
    # boolean (or None) must be refused, not coerced.
    with pytest.raises(ValueError):
        mounts_mod.add_mount("pub", "aws-open:bucket", read_only="false")


def test_mount_read_only_path_lookup(home):
    ro = mounts_mod.add_mount("pub", "aws-open:bucket", read_only=True)
    rw = mounts_mod.add_mount("data", "aws:bucket", read_only=False)
    legacy = mounts_mod.add_mount("old", "old:bucket")  # pre-flag record
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(ro), "f.parquet")) is True
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(rw), "f.parquet")) is False
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(legacy), "f.parquet")) is False
    assert mounts_mod.mount_read_only("/somewhere/else.parquet") is False


def test_mount_view_exposes_read_only(home):
    m = mounts_mod.add_mount("pub", "aws-open:bucket", read_only=True)
    assert mounts_mod.mount_view(m, rcd_mounts=set())["read_only"] is True
    m2 = mounts_mod.add_mount("data", "aws:bucket")
    assert mounts_mod.mount_view(m2, rcd_mounts=set())["read_only"] is False


def test_fix_dotted_bucket_url():
    fix = mounts_mod._fix_dotted_bucket_url
    # dotted bucket in the TLS hostname can never pass the wildcard cert:
    # unsigned URLs are rewritten to path-style
    assert fix("https://us-west-2.opendata.source.coop.s3.us-west-2"
               ".amazonaws.com/mindearth/a%20b/zarr.json") == \
        ("https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop"
         "/mindearth/a%20b/zarr.json")
    # a signed one can't be (SigV4 covers Host) — dropped, caller stays on
    # the serve proxy
    assert fix("https://buck.et.s3.us-east-1.amazonaws.com/k"
               "?X-Amz-Signature=abc") is None
    # dot-free buckets and non-AWS hosts pass through untouched
    for u in ("https://plain.s3.us-west-2.amazonaws.com/key",
              "https://plain.s3.us-west-2.amazonaws.com/key?X-Amz-Sig=x",
              "https://minio.example.com/bucket/key"):
        assert fix(u) == u


# -- s3_list_page / s3_direct_capable ---------------------------------------
#
# rclone can't paginate a listing at any layer (see rc_list_dir), so a flat S3
# prefix with millions of keys times out. For anonymous plain AWS S3 — the
# backend class that dominates our mounts — a single ListObjectsV2 page is
# fetched straight from S3 unsigned, off the kernel mount. Real S3 is never
# hit: urllib.request.urlopen is monkeypatched with canned XML.

_ANON_S3_CFG = {"type": "s3", "provider": "AWS", "env_auth": "false"}


def _s3_list_xml(*, prefixes=(), contents=(), truncated=False, next_token=None):
    """A ListObjectsV2 response body. `contents` are (key, size, lastmod)."""
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>',
             f'<ListBucketResult xmlns="{ns}">',
             f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>']
    if next_token:
        parts.append(f'<NextContinuationToken>{next_token}</NextContinuationToken>')
    for key, size, lastmod in contents:
        parts.append(f'<Contents><Key>{key}</Key><Size>{size}</Size>'
                     f'<LastModified>{lastmod}</LastModified></Contents>')
    for p in prefixes:
        parts.append(f'<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>')
    parts.append('</ListBucketResult>')
    return "".join(parts).encode()


class _FakeS3Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def s3_urlopen(monkeypatch):
    """Capture the ListObjectsV2 URL and hand back canned XML. `box["body"]`
    is the reply; set `box["raise"]` to an exception to fail the GET."""
    calls = []
    box = {"body": _s3_list_xml()}
    real = mounts_mod.urllib.request.urlopen

    def fake(url, timeout=None):
        # rc calls (config/get etc.) go to the loopback stub rcd over http —
        # only intercept the S3 ListObjectsV2 GET, delegate the rest.
        target = url if isinstance(url, str) else url.get_full_url()
        if "amazonaws.com" not in target:
            return real(url, timeout=timeout)
        calls.append((target, timeout))
        if box.get("raise") is not None:
            raise box["raise"]
        return _FakeS3Resp(box["body"])

    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", fake)
    # Signable listings go through the non-redirect opener (so a wrong-region
    # 301 is observable); intercept it too so it never hits real S3.
    monkeypatch.setattr(mounts_mod._NO_REDIRECT_OPENER, "open", fake)
    return calls, box


def test_s3_direct_capable_true_for_anonymous_s3(home, rcd, fresh_upstream):
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    assert mounts_mod.s3_direct_capable(mounts_mod.mountpoint(c) + "/analysed_sst")


def test_s3_direct_capable_true_for_credentialed_shape_no_resolution(
        home, rcd, fresh_upstream, monkeypatch):
    # FINDING 12: a credentialed-SHAPED S3 remote (ambient auth opted in) is
    # capable by config shape alone — the predicate resolves NO credentials (that
    # unbudgeted network walk stalled the conditions/stat callers).
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    resolved = []

    def _spy(fnname):
        def f(*a, **k):
            resolved.append(fnname)
        return f

    for fnname in ("resolve_static_credentials", "resolve_botocore_chain",
                   "resolve_credentials"):
        monkeypatch.setattr(mounts_mod.s3sign, fnname, _spy(fnname))
    c = mounts_mod.add_mount("corp", "corp:bucket")
    assert mounts_mod.s3_direct_capable(mounts_mod.mountpoint(c) + "/x")
    assert resolved == []  # no credential resolution from the predicate


def test_s3_direct_capable_false_for_custom_endpoint(home, rcd, fresh_upstream):
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false",
                                   "endpoint": "https://r2.example.com"}
    c = mounts_mod.add_mount("r2", "r2:bucket")
    assert not mounts_mod.s3_direct_capable(mounts_mod.mountpoint(c) + "/x")


def test_s3_direct_capable_false_outside_mount(home, rcd, fresh_upstream):
    assert not mounts_mod.s3_direct_capable("/tmp/not/a/mount")


def test_s3_list_page_url_and_query_nested_dir(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, _box = s3_urlopen
    rcd.responses["config/get"] = {**_ANON_S3_CFG, "region": "us-west-2"}
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mounts_mod.s3_list_page(
        mounts_mod.mountpoint(c) + "/analysed_sst", max_keys=1000)
    [(url, timeout)] = calls
    assert url.startswith("https://mur-sst.s3.us-west-2.amazonaws.com/?")
    q = up.parse_qs(up.urlsplit(url).query)
    assert q["list-type"] == ["2"]
    assert q["delimiter"] == ["/"]
    assert q["prefix"] == ["zarr-v1/analysed_sst/"]
    assert q["max-keys"] == ["1000"]
    assert "continuation-token" not in q
    assert timeout == mounts_mod.S3_LIST_TIMEOUT_S


def test_s3_list_page_mountpoint_uses_store_prefix(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, _box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=500)
    q = up.parse_qs(up.urlsplit(calls[0][0]).query)
    assert q["prefix"] == ["zarr-v1/"]


def test_s3_list_page_bucket_root_mountpoint_empty_prefix(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, _box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=500)
    q = up.parse_qs(up.urlsplit(calls[0][0]).query, keep_blank_values=True)
    assert q["prefix"] == [""]


def test_s3_list_page_continuation_token_urlencoded(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, _box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000,
                            continuation="a b/c+d=")
    q = up.parse_qs(up.urlsplit(calls[0][0]).query)
    assert q["continuation-token"] == ["a b/c+d="]


def test_s3_list_page_dotted_bucket_path_style(home, rcd, fresh_upstream, s3_urlopen):
    calls, _box = s3_urlopen
    rcd.responses["config/get"] = {**_ANON_S3_CFG, "region": "us-west-2"}
    c = mounts_mod.add_mount("open", "aws-open:us-west-2.opendata.source.coop/foo")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert calls[0][0].startswith(
        "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop?")


def test_s3_list_page_parses_prefixes_files_and_token(home, rcd, fresh_upstream, s3_urlopen):
    calls, box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    box["body"] = _s3_list_xml(
        prefixes=["zarr-v1/analysed_sst/2020/", "zarr-v1/analysed_sst/2021/"],
        contents=[
            # placeholder object whose key IS the prefix -> skipped
            ("zarr-v1/analysed_sst/", 0, "2000-01-01T00:00:00.000Z"),
            ("zarr-v1/analysed_sst/.zattrs", 42, "2024-01-02T03:04:05.000Z"),
        ],
        truncated=True, next_token="TOKEN123")
    entries, token = mounts_mod.s3_list_page(
        mounts_mod.mountpoint(c) + "/analysed_sst", max_keys=1000)
    by = {e["Name"]: e for e in entries}
    assert set(by) == {"2020", "2021", ".zattrs"}
    assert by["2020"]["IsDir"] is True and by["2020"]["Size"] is None
    assert by["2020"]["ModTime"] is None
    assert by[".zattrs"]["IsDir"] is False and by[".zattrs"]["Size"] == 42
    assert by[".zattrs"]["ModTime"] == "2024-01-02T03:04:05.000Z"
    assert token == "TOKEN123"


def test_s3_list_page_not_truncated_returns_none_token(home, rcd, fresh_upstream, s3_urlopen):
    calls, box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    box["body"] = _s3_list_xml(contents=[("zarr-v1/f.txt", 1, "2024-01-02T03:04:05Z")])
    _entries, token = mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert token is None


def test_s3_list_page_http_403_raises_s3listerror(home, rcd, fresh_upstream, s3_urlopen):
    calls, box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    box["raise"] = mounts_mod.urllib.error.HTTPError(
        "https://x", 403, "Forbidden", {}, None)
    with pytest.raises(mounts_mod.S3ListError):
        mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)


def test_s3_list_page_non_anonymous_raises_s3listerror(home, rcd, fresh_upstream, s3_urlopen):
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    with pytest.raises(mounts_mod.S3ListError):
        mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)


# -- signable S3: direct listing for credentialed remotes -------------------


def test_s3_direct_capable_true_for_signable(home, rcd, fresh_upstream):
    rcd.responses["config/get"] = _CRED_S3_CFG
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    assert mounts_mod.s3_direct_capable(mounts_mod.mountpoint(c) + "/x")


def test_s3_list_page_signable_presigns(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, box = s3_urlopen
    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-west-2"}
    box["body"] = _s3_list_xml(
        contents=[("pre/f.txt", 5, "2024-01-02T03:04:05Z")])
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    entries, token = mounts_mod.s3_list_page(
        mounts_mod.mountpoint(c), max_keys=1000)
    [(url, _t)] = calls
    q = up.parse_qs(up.urlsplit(url).query)
    # ListObjectsV2 params AND the SigV4 signature are both present — the list
    # params ride through the presigner, canonicalized in one place.
    assert q["list-type"] == ["2"] and q["prefix"] == ["pre/"]
    assert q["max-keys"] == ["1000"] and q["delimiter"] == ["/"]
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "X-Amz-Signature" in q
    assert url.startswith("https://bucket.s3.us-west-2.amazonaws.com/?")
    assert [e["Name"] for e in entries] == ["f.txt"]
    assert token is None


def test_s3_list_page_signable_continuation_roundtrips(home, rcd, fresh_upstream, s3_urlopen):
    import urllib.parse as up

    calls, _box = s3_urlopen
    rcd.responses["config/get"] = _CRED_S3_CFG
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000,
                            continuation="a b/c+d=")
    q = up.parse_qs(up.urlsplit(calls[0][0]).query)
    assert q["continuation-token"] == ["a b/c+d="]
    assert "X-Amz-Signature" in q


def test_s3_list_page_signable_403_raises(home, rcd, fresh_upstream, s3_urlopen):
    _calls, box = s3_urlopen
    rcd.responses["config/get"] = _CRED_S3_CFG
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    box["raise"] = mounts_mod.urllib.error.HTTPError(
        "https://x", 403, "Forbidden", {}, None)
    with pytest.raises(mounts_mod.S3ListError):
        mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)


def test_s3_list_page_signable_region_self_corrects(home, rcd, fresh_upstream, monkeypatch):
    # A signable listing on a wrong/unset-region remote must observe the 301
    # (non-redirect opener), adopt x-amz-bucket-region, re-sign and retry — so a
    # direct list/probe (and the watcher) works instead of a followed 301 -> 403.
    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-east-1"}
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    body = _s3_list_xml(contents=[("pre/f.txt", 5, "2024-01-02T03:04:05Z")])
    seen = []

    def fake_open(req, timeout=None):
        url = req if isinstance(req, str) else req.get_full_url()
        seen.append(url)
        if len(seen) == 1:  # first try in the (wrong) config region -> 301
            raise mounts_mod.urllib.error.HTTPError(
                url, 301, "Moved", {"x-amz-bucket-region": "eu-west-1"}, None)
        return _FakeS3Resp(body)

    monkeypatch.setattr(mounts_mod._NO_REDIRECT_OPENER, "open", fake_open)
    entries, _tok = mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert [e["Name"] for e in entries] == ["f.txt"]
    assert len(seen) == 2  # one wrong-region 301 + one retry
    assert seen[0].startswith("https://bucket.s3.us-east-1.amazonaws.com/")
    assert seen[1].startswith("https://bucket.s3.eu-west-1.amazonaws.com/")
    assert mounts_mod._upstream_region["corp:bucket/pre"] == "eu-west-1"


def test_s3_list_page_signable_region_self_corrects_on_307(home, rcd, fresh_upstream, monkeypatch):
    # Same as the 301 case but S3 returns a 307 Temporary Redirect (new bucket):
    # the direct listing must observe it, adopt x-amz-bucket-region and retry.
    rcd.responses["config/get"] = {**_CRED_S3_CFG, "region": "us-east-1"}
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    body = _s3_list_xml(contents=[("pre/f.txt", 5, "2024-01-02T03:04:05Z")])
    seen = []

    def fake_open(req, timeout=None):
        url = req if isinstance(req, str) else req.get_full_url()
        seen.append(url)
        if len(seen) == 1:  # first try in the (wrong) config region -> 307
            raise mounts_mod.urllib.error.HTTPError(
                url, 307, "Temporary Redirect",
                {"x-amz-bucket-region": "eu-west-1"}, None)
        return _FakeS3Resp(body)

    monkeypatch.setattr(mounts_mod._NO_REDIRECT_OPENER, "open", fake_open)
    entries, _tok = mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert [e["Name"] for e in entries] == ["f.txt"]
    assert len(seen) == 2  # one wrong-region 307 + one retry
    assert seen[1].startswith("https://bucket.s3.eu-west-1.amazonaws.com/")
    assert mounts_mod._upstream_region["corp:bucket/pre"] == "eu-west-1"


def test_s3_list_page_anonymous_url_has_no_xamz(home, rcd, fresh_upstream, s3_urlopen):
    # INVARIANT: an anonymous listing URL is byte-identical to today — no
    # X-Amz-* parameters, resolver never consulted (only _anonymous_s3 matches).
    calls, _box = s3_urlopen
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    mounts_mod.s3_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert "X-Amz" not in calls[0][0]


# -- gcs_list_page / gcs_direct_capable -------------------------------------
#
# Anonymous GCS (the gcs-open shape) is the GCS analog of anonymous plain AWS
# S3: rclone can't paginate a listing at any layer, so a flat prefix with
# hundreds of thousands of children times out. For anonymous GCS a single page
# is fetched straight from the GCS JSON API (objects.list) unsigned, off the
# kernel mount. Real GCS is never hit: urllib.request.urlopen is monkeypatched
# with canned JSON.

_ANON_GCS_CFG = {"type": "google cloud storage", "anonymous": "true"}


def _gcs_list_json(*, prefixes=(), items=(), next_token=None):
    """A GCS objects.list response body. `items` are (name, size, updated);
    `size` is emitted as a STRING, exactly as the JSON API returns it."""
    body: dict = {}
    if prefixes:
        body["prefixes"] = list(prefixes)
    if items:
        body["items"] = [{"name": n, "size": str(sz), "updated": upd}
                         for n, sz, upd in items]
    if next_token:
        body["nextPageToken"] = next_token
    return json.dumps(body).encode()


@pytest.fixture()
def gcs_urlopen(monkeypatch):
    """Capture the objects.list URL and hand back canned JSON. `box["body"]` is
    the reply; set `box["raise"]` to an exception to fail the GET."""
    calls = []
    box = {"body": _gcs_list_json()}
    real = mounts_mod.urllib.request.urlopen

    def fake(url, timeout=None):
        # rc calls (config/get etc.) go to the loopback stub rcd over http —
        # only intercept the GCS objects.list GET, delegate the rest.
        target = url if isinstance(url, str) else url.get_full_url()
        if "storage.googleapis.com" not in target:
            return real(url, timeout=timeout)
        calls.append((target, timeout))
        if box.get("raise") is not None:
            raise box["raise"]
        return _FakeS3Resp(box["body"])

    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", fake)
    return calls, box


def test_gcs_direct_capable_true_for_anonymous_gcs(home, rcd, fresh_upstream):
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    assert mounts_mod.gcs_direct_capable(mounts_mod.mountpoint(c) + "/analysed_sst")


def test_gcs_direct_capable_true_for_non_anonymous_shape(home, rcd, fresh_upstream):
    # FINDING 12: any non-anonymous GCS remote is capable by shape (bearer), even
    # a bare one with no config markers (ADC-only) — the predicate resolves no
    # token; a failed bearer read falls back to rc inside the budgeted fetch path.
    rcd.responses["config/get"] = {"type": "google cloud storage"}
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    assert mounts_mod.gcs_direct_capable(mounts_mod.mountpoint(c) + "/x")


def test_gcs_direct_capable_false_outside_mount(home, rcd, fresh_upstream):
    assert not mounts_mod.gcs_direct_capable("/tmp/not/a/mount")


def test_gcs_list_page_url_and_query_nested_dir(home, rcd, fresh_upstream, gcs_urlopen):
    import urllib.parse as up

    calls, _box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    mounts_mod.gcs_list_page(
        mounts_mod.mountpoint(c) + "/analysed_sst", max_keys=1000)
    [(url, timeout)] = calls
    assert url.startswith(
        "https://storage.googleapis.com/storage/v1/b/mur-sst/o?")
    q = up.parse_qs(up.urlsplit(url).query)
    assert q["delimiter"] == ["/"]
    assert q["prefix"] == ["zarr-v1/analysed_sst/"]
    assert q["maxResults"] == ["1000"]
    assert "pageToken" not in q
    assert timeout == mounts_mod.GCS_LIST_TIMEOUT_S


def test_gcs_list_page_mountpoint_uses_store_prefix(home, rcd, fresh_upstream, gcs_urlopen):
    import urllib.parse as up

    calls, _box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=500)
    q = up.parse_qs(up.urlsplit(calls[0][0]).query)
    assert q["prefix"] == ["zarr-v1/"]


def test_gcs_list_page_bucket_root_mountpoint_empty_prefix(home, rcd, fresh_upstream, gcs_urlopen):
    import urllib.parse as up

    calls, _box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst")
    mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=500)
    q = up.parse_qs(up.urlsplit(calls[0][0]).query, keep_blank_values=True)
    assert q["prefix"] == [""]


def test_gcs_list_page_continuation_token_urlencoded(home, rcd, fresh_upstream, gcs_urlopen):
    import urllib.parse as up

    calls, _box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000,
                             continuation="a b/c+d=")
    q = up.parse_qs(up.urlsplit(calls[0][0]).query)
    assert q["pageToken"] == ["a b/c+d="]


def test_gcs_list_page_parses_prefixes_files_and_token(home, rcd, fresh_upstream, gcs_urlopen):
    calls, box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    box["body"] = _gcs_list_json(
        prefixes=["zarr-v1/analysed_sst/2020/", "zarr-v1/analysed_sst/2021/"],
        items=[
            # placeholder object whose name IS the prefix -> skipped
            ("zarr-v1/analysed_sst/", 0, "2000-01-01T00:00:00.000Z"),
            ("zarr-v1/analysed_sst/.zattrs", 42, "2024-01-02T03:04:05.000Z"),
        ],
        next_token="TOKEN123")
    entries, token = mounts_mod.gcs_list_page(
        mounts_mod.mountpoint(c) + "/analysed_sst", max_keys=1000)
    by = {e["Name"]: e for e in entries}
    assert set(by) == {"2020", "2021", ".zattrs"}
    assert by["2020"]["IsDir"] is True and by["2020"]["Size"] is None
    assert by["2020"]["ModTime"] is None
    assert by[".zattrs"]["IsDir"] is False and by[".zattrs"]["Size"] == 42
    assert by[".zattrs"]["ModTime"] == "2024-01-02T03:04:05.000Z"
    assert token == "TOKEN123"


def test_gcs_list_page_not_truncated_returns_none_token(home, rcd, fresh_upstream, gcs_urlopen):
    calls, box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    box["body"] = _gcs_list_json(items=[("zarr-v1/f.txt", 1, "2024-01-02T03:04:05Z")])
    _entries, token = mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert token is None


def test_gcs_list_page_http_403_raises_directlisterror(home, rcd, fresh_upstream, gcs_urlopen):
    calls, box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    box["raise"] = mounts_mod.urllib.error.HTTPError(
        "https://x", 403, "Forbidden", {}, None)
    with pytest.raises(mounts_mod.DirectListError):
        mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)


def test_gcs_list_page_non_anonymous_raises_directlisterror(home, rcd, fresh_upstream, gcs_urlopen):
    rcd.responses["config/get"] = {"type": "google cloud storage"}
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    with pytest.raises(mounts_mod.DirectListError):
        mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)


# -- credentialed GCS: bearer-authorized direct listings and probes ---------
#
# A non-anonymous GCS remote whose bearer token resolves (gcssign.resolve_token)
# joins the direct fast path: the same JSON-API pager and HEAD/list probes, now
# with an `Authorization: Bearer` header. Anonymous GCS stays byte-identical (no
# header, resolver never consulted). resolve_token is stubbed so the tests need
# no google-auth and pin the degrade contract.

_CRED_GCS_CFG = {"type": "google cloud storage",
                 "service_account_file": "/keys/sa.json"}


def _stub_gcs_token(monkeypatch, *, token="TOKENVAL", expiry_offset=3600.0,
                    calls=None):
    """Stub the two-step GCS credential resolution (build the credential object,
    then extract a Token). `calls` records each credential-OBJECT build — i.e.
    each creds-cache miss — so a test can assert re-resolution behaviour."""
    marker = object()

    def fake_resolve_credentials(cfg):
        if calls is not None:
            calls.append(cfg)
        return marker if token is not None else None

    def fake_token_from_credentials(creds):
        if creds is None or token is None:
            return None
        return gcssign_mod.Token(token, time.time() + expiry_offset)

    monkeypatch.setattr(mounts_mod.gcssign, "resolve_credentials",
                        fake_resolve_credentials)
    monkeypatch.setattr(mounts_mod.gcssign, "token_from_credentials",
                        fake_token_from_credentials)


@pytest.fixture()
def gcs_bearer_urlopen(monkeypatch):
    """Capture (url, Authorization-header) for GCS API GETs and hand back canned
    JSON. box['body'] is the reply; box['raises'] a per-call list of exceptions
    (None entry = succeed). rc calls (loopback rcd) delegate to real urlopen."""
    calls = []
    box = {"body": _gcs_list_json()}
    real = mounts_mod.urllib.request.urlopen

    def fake(req, timeout=None):
        target = req if isinstance(req, str) else req.get_full_url()
        if "storage.googleapis.com" not in target:
            return real(req, timeout=timeout)
        auth = None if isinstance(req, str) else req.get_header("Authorization")
        calls.append((target, auth))
        seq = box.get("raises")
        if seq:
            exc = seq.pop(0)
            if exc is not None:
                raise exc
        return _FakeS3Resp(box["body"])

    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", fake)
    return calls, box


def test_gcs_direct_capable_true_for_credentialed(home, rcd, fresh_upstream, monkeypatch):
    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_token(monkeypatch, token="TOK")
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    assert mounts_mod.gcs_direct_capable(mounts_mod.mountpoint(c) + "/x")


def test_gcs_direct_capable_true_regardless_of_token(home, rcd, fresh_upstream, monkeypatch):
    # FINDING 12: capability is shape-based, so it holds even when no token
    # resolves — the failed bearer read falls back to rc in the fetch path, not
    # the predicate. The token resolver must not even be consulted here.
    rcd.responses["config/get"] = _CRED_GCS_CFG
    resolver_calls = []
    _stub_gcs_token(monkeypatch, token=None, calls=resolver_calls)
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    assert mounts_mod.gcs_direct_capable(mounts_mod.mountpoint(c) + "/x")
    assert resolver_calls == []  # pure shape check, no token resolution


def test_gcs_list_page_credentialed_carries_bearer(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    calls, _box = gcs_bearer_urlopen
    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_token(monkeypatch, token="TOKENVAL")
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    [(url, auth)] = calls
    assert url.startswith(
        "https://storage.googleapis.com/storage/v1/b/bucket/o?")
    assert auth == "Bearer TOKENVAL"


def test_gcs_head_credentialed_carries_bearer(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    calls, box = gcs_bearer_urlopen
    box["body"] = b'{"size": "42", "updated": "2024-01-02T03:04:05Z"}'
    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_token(monkeypatch, token="TOKENVAL")
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    got = mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/x.json")
    assert got.exists is True and got.size == 42
    assert calls[-1][1] == "Bearer TOKENVAL"


def test_gcs_has_children_credentialed_carries_bearer(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    calls, box = gcs_bearer_urlopen
    box["body"] = _gcs_list_json(items=[("pre/x", 1, "2024-01-01T00:00:00Z")])
    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_token(monkeypatch, token="TOKENVAL")
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    assert mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/sub") is True
    assert calls[-1][1] == "Bearer TOKENVAL"


def test_gcs_anonymous_never_carries_bearer_or_consults_resolver(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    # INVARIANT: anonymous GCS is byte-identical — no Authorization header, and
    # the token resolver is never consulted (mirrors the S3 anonymous guard).
    calls, _box = gcs_bearer_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    resolver_calls = []
    _stub_gcs_token(monkeypatch, token="TOK", calls=resolver_calls)
    c = mounts_mod.add_mount("open", "gcs-open:bucket/pre")
    mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    [(_url, auth)] = calls
    assert auth is None
    assert resolver_calls == []


def test_gcs_list_page_401_reresolves_then_retries(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    calls, box = gcs_bearer_urlopen
    rcd.responses["config/get"] = _CRED_GCS_CFG
    resolver_calls = []
    _stub_gcs_token(monkeypatch, token="TOK", calls=resolver_calls)
    box["raises"] = [
        mounts_mod.urllib.error.HTTPError("https://x", 401, "no", {}, None),
        None]  # first 401, retry succeeds
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    entries, _tok = mounts_mod.gcs_list_page(
        mounts_mod.mountpoint(c), max_keys=1000)
    assert len(calls) == 2  # one 401 + one retry
    assert calls[1][1] == "Bearer TOK"
    # capability probe cached the token; the 401 dropped it and forced one
    # re-resolve for the retry.
    assert len(resolver_calls) == 2


def test_gcs_list_page_second_401_raises(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    calls, box = gcs_bearer_urlopen
    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_token(monkeypatch, token="TOK")
    box["raises"] = [
        mounts_mod.urllib.error.HTTPError("https://x", 401, "no", {}, None),
        mounts_mod.urllib.error.HTTPError("https://x", 401, "no", {}, None)]
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    with pytest.raises(mounts_mod.DirectListError):
        mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert len(calls) == 2  # retried exactly once


def test_gcs_list_page_403_propagates_without_retry(home, rcd, fresh_upstream, monkeypatch, gcs_bearer_urlopen):
    # 403 = permission denial with a VALID token (per-object IAM); re-resolving
    # wouldn't help, so it propagates immediately — no retry, no token churn.
    calls, box = gcs_bearer_urlopen
    rcd.responses["config/get"] = _CRED_GCS_CFG
    resolver_calls = []
    _stub_gcs_token(monkeypatch, token="TOK", calls=resolver_calls)
    box["raises"] = [
        mounts_mod.urllib.error.HTTPError("https://x", 403, "no", {}, None),
        None]  # a retry (if it happened) would succeed — it must NOT happen
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    with pytest.raises(mounts_mod.DirectListError):
        mounts_mod.gcs_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert len(calls) == 1  # no retry on 403
    assert len(resolver_calls) == 1  # token not invalidated / re-resolved


def test_invalidate_upstream_caches_clears_gcs_token_and_creds_caches(fresh_upstream):
    mounts_mod._gcs_token_cache["gcp"] = (
        gcssign_mod.Token("T", time.time() + 999), time.time() + 999)
    mounts_mod._gcs_creds_cache["gcp"] = (object(), time.time() + 999)
    mounts_mod._invalidate_upstream_caches()
    assert mounts_mod._gcs_token_cache == {}
    assert mounts_mod._gcs_creds_cache == {}


def test_gcs_bearer_token_ttl_tracks_expiry(fresh_upstream, monkeypatch):
    # The token cache runs to expiry-minus-slack — NOT clamped to _CRED_TTL_S —
    # so a live ~1h token is reused for its whole life (no per-minute OAuth).
    _stub_gcs_token(monkeypatch, token="T", expiry_offset=65)
    mounts_mod._gcs_bearer_token("gcp", _CRED_GCS_CFG)
    _tok, exp = mounts_mod._gcs_token_cache["gcp"]
    assert 0 < exp - time.monotonic() <= 10  # ~5s (65s - 60s slack)

    mounts_mod._gcs_token_cache.clear()
    _stub_gcs_token(monkeypatch, token="T", expiry_offset=3600)
    mounts_mod._gcs_bearer_token("gcp2", _CRED_GCS_CFG)
    _tok2, exp2 = mounts_mod._gcs_token_cache["gcp2"]
    # ~1h - 60s slack, far beyond the 60s _CRED_TTL_S window it used to clamp to.
    assert abs((exp2 - time.monotonic()) - (3600 - 60)) < 3


def test_gcs_bearer_token_caches_within_ttl(fresh_upstream, monkeypatch):
    calls = []
    _stub_gcs_token(monkeypatch, token="TOK", calls=calls)
    mounts_mod._gcs_bearer_token("gcp", _CRED_GCS_CFG)
    mounts_mod._gcs_bearer_token("gcp", _CRED_GCS_CFG)
    assert len(calls) == 1  # second call served from cache


def test_gcs_credentials_object_cached_across_token_reresolves(fresh_upstream, monkeypatch):
    # The credential OBJECT is built at most once per _CRED_TTL_S window even
    # when the Token is re-derived from it multiple times (fix 6).
    calls = []
    _stub_gcs_token(monkeypatch, token="TOK", expiry_offset=1, calls=calls)
    mounts_mod._gcs_bearer_token("gcp", _CRED_GCS_CFG)
    # Force the short-lived (1s) token cache to lapse, then re-derive.
    mounts_mod._gcs_token_cache.clear()
    mounts_mod._gcs_bearer_token("gcp", _CRED_GCS_CFG)
    assert len(calls) == 1  # creds object reused from the 60s creds cache


class _FakeBotocoreChain:
    """A self-refreshing botocore credentials object: get_frozen_credentials
    always answers, so re-freezing is cheap and needs no re-walk."""

    def __init__(self, token=None):
        self._token = token

    def get_frozen_credentials(self):
        class _F:
            access_key = "AKIACHAIN"
            secret_key = "CHAINSEC"
        _F.token = self._token
        return _F


def test_signable_credentials_caches_botocore_chain_object(fresh_upstream, monkeypatch):
    # The (IMDS-slow) provider-chain walk runs at most once per its own window;
    # get_frozen_credentials is re-run per _CRED_TTL_S on the cached object.
    builds = []

    def fake_chain(cfg):
        builds.append(cfg)
        return _FakeBotocoreChain(token="STSTOK")

    monkeypatch.setattr(mounts_mod.s3sign, "resolve_botocore_chain", fake_chain)
    cfg = {"type": "s3", "env_auth": "true"}  # opts into ambient auth, no static
    c1 = mounts_mod._signable_credentials("corp", cfg)
    assert c1 == mounts_mod.s3sign.Credentials("AKIACHAIN", "CHAINSEC", "STSTOK")
    # The 60s frozen cache lapses but the chain OBJECT is reused (not re-walked).
    mounts_mod._cred_cache.clear()
    c2 = mounts_mod._signable_credentials("corp", cfg)
    assert c2 == c1
    assert len(builds) == 1  # chain walked once; frozen re-extracted each window


def test_signable_credentials_static_keys_skip_botocore(fresh_upstream, monkeypatch):
    # A remote with static keys never triggers the chain walk.
    builds = []
    monkeypatch.setattr(mounts_mod.s3sign, "resolve_botocore_chain",
                        lambda cfg: builds.append(cfg))
    cfg = {"type": "s3", "access_key_id": "AKIACFG", "secret_access_key": "SEC"}
    assert mounts_mod._signable_credentials("corp", cfg) == \
        mounts_mod.s3sign.Credentials("AKIACFG", "SEC", None)
    assert builds == []


def test_invalidate_upstream_caches_clears_botocore_chain_cache(fresh_upstream):
    mounts_mod._botocore_creds_cache["corp"] = (object(), time.time() + 999)
    mounts_mod._invalidate_upstream_caches()
    assert mounts_mod._botocore_creds_cache == {}


# -- tiered private-GCS raw reads: gsign 307 / bearer proxy -----------------


class _StubGcsSigner:
    """A signer object (gcssign.Signer.signer) that returns fixed bytes, so a
    signed URL carries a real X-Goog-Signature without any crypto."""

    def sign(self, data):
        return b"\xab\xcd\xef"


def _stub_gcs_signer(monkeypatch, present=True):
    signer = (gcssign_mod.Signer(_StubGcsSigner(), "svc@p.iam.gserviceaccount.com")
              if present else None)
    monkeypatch.setattr(mounts_mod.gcssign, "resolve_signer", lambda cfg: signer)


def test_upstream_gsign_signs_sa_key_gcs(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch)
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (206, None))
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a", "b.parquet")
    url = mounts_mod.upstream_url_for(f)
    assert url.startswith(
        "https://storage.googleapis.com/bucket/pre/a/b.parquet?")
    assert "X-Goog-Signature=" in url and "X-Goog-Algorithm=GOOG4-RSA-SHA256" in url
    assert mounts_mod._upstream_mode["gcp:bucket/pre"] == "gsign"
    assert mounts_mod._upstream_links == {}  # gsign never caches a link
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []
    # Validation is one-time per fs: a second object does not re-validate.
    seen = []
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: seen.append(url) or (206, None))
    mounts_mod.upstream_url_for(
        os.path.join(mounts_mod.mountpoint(c), "c.parquet"))
    assert seen == []


def test_upstream_gsign_403_falls_to_bearer_and_bearer_serves(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch, present=True)    # SA key parses...
    _stub_gcs_token(monkeypatch, token="TOKENVAL")  # ...and a token resolves
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (403, None))
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) is None  # signature rejected, no 307
    assert mounts_mod._upstream_mode["gcp:bucket/pre"] == "bearer"
    # FINDING 1: a remote pinned to bearer serves the token even though the SA
    # key still parses (_gcs_signable True) — no dead-end back to the serve.
    url, headers = mounts_mod.bearer_upstream_for(f)
    assert url == "https://storage.googleapis.com/bucket/pre/a.parquet"
    assert headers == {"Authorization": "Bearer TOKENVAL"}
    # publiclink is never attempted for a credentialed GCS remote.
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []


def test_upstream_gsign_inconclusive_not_pinned(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch)
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (0, None))  # network error -> inconclusive
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) is None
    assert "gcp:bucket" not in mounts_mod._upstream_mode  # not pinned -> retried
    assert mounts_mod._sign_neg_cache.get("gcp:bucket", 0.0) > 0.0


def test_upstream_gsign_retry_window_serves_bearer_without_pinning(
        home, rcd, fresh_upstream, monkeypatch):
    # FINDING 1: while gsign validation is inconclusive (its retry window), a raw
    # read must still be served via the bearer proxy in the SAME request even
    # though the SA key parses (_gcs_signable True) — not dead-end at the serve —
    # AND bearer must NOT be permanently pinned, so gsign is retried after the
    # window.
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch, present=True)      # SA key parses (signable)
    _stub_gcs_token(monkeypatch, token="TOKENVAL")   # ...and a token resolves
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (0, None))  # network error -> inconclusive
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) is None  # no 307 URL this window
    assert "gcp:bucket/pre" not in mounts_mod._upstream_mode  # NOT pinned bearer
    # ...but the bearer proxy serves the token in the same window (not a dead-end)
    # even though the SA key still parses.
    assert mounts_mod._gcs_signable("gcp", _CRED_GCS_CFG) is True
    url, headers = mounts_mod.bearer_upstream_for(f)
    assert url == "https://storage.googleapis.com/bucket/pre/a.parquet"
    assert headers == {"Authorization": "Bearer TOKENVAL"}
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []


def test_upstream_gsign_transient_signer_none_not_pinned_bearer(
        home, rcd, fresh_upstream, monkeypatch):
    # FINDING 2: a signable-SHAPED remote (SA key configured) whose signer
    # momentarily resolves None must NOT be pinned to bearer permanently — once
    # the signer comes back the remote ends up in gsign mode, not stuck bearer.
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG  # carries service_account_file
    _stub_gcs_token(monkeypatch, token="TOKENVAL")
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (206, None))
    _stub_gcs_signer(monkeypatch, present=False)  # SA file momentarily unreadable
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) is None            # served via bearer
    assert "gcp:bucket/pre" not in mounts_mod._upstream_mode  # NOT pinned bearer
    # Signer comes back and its cache expires: gsign now validates and pins.
    _stub_gcs_signer(monkeypatch, present=True)
    mounts_mod._gcs_signer_cache.clear()
    url = mounts_mod.upstream_url_for(f)
    assert url is not None and "X-Goog-Signature=" in url
    assert mounts_mod._upstream_mode["gcp:bucket/pre"] == "gsign"


def test_upstream_gsign_unpins_when_signer_vanishes(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch, present=True)
    monkeypatch.setattr(mounts_mod, "_sign_validation_status",
                        lambda url: (206, None))
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.upstream_url_for(f) is not None
    assert mounts_mod._upstream_mode["gcp:bucket"] == "gsign"
    # SA key rotated away (and its cached signer expires): gsign mints nothing.
    _stub_gcs_signer(monkeypatch, present=False)
    mounts_mod._gcs_signer_cache.clear()
    assert mounts_mod.upstream_url_for(f) is None
    # FINDING 5: the demotion is NON-permanent — the mode is un-pinned, so the
    # next request re-derives (gsign again once the signer returns) rather than
    # being stuck on a wrong mode forever.
    assert "gcp:bucket" not in mounts_mod._upstream_mode


def test_upstream_bearer_token_only_gcs(home, rcd, fresh_upstream, monkeypatch):
    import os

    # Token-only SHAPE: a non-anonymous GCS remote with no SA key configured
    # (oauth / ADC). It pins bearer straight away (never signable) and a token
    # resolves, so the bearer proxy serves.
    rcd.responses["config/get"] = {"type": "google cloud storage"}
    _stub_gcs_token(monkeypatch, token="TOKENVAL")
    c = mounts_mod.add_mount("gcp", "gcp:bucket/pre")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    # No URL may carry the token -> upstream_url_for returns None, mode bearer.
    assert mounts_mod.upstream_url_for(f) is None
    assert mounts_mod._upstream_mode["gcp:bucket/pre"] == "bearer"
    url, headers = mounts_mod.bearer_upstream_for(f)
    assert url == "https://storage.googleapis.com/bucket/pre/a.parquet"
    assert headers == {"Authorization": "Bearer TOKENVAL"}
    # publiclink (PublicLink: False for GCS) is never called.
    assert [x for x in rcd.calls if x[0] == "operations/publiclink"] == []


def test_bearer_upstream_none_for_anonymous_gcs(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _ANON_GCS_CFG
    resolver_calls = []
    _stub_gcs_token(monkeypatch, token="X", calls=resolver_calls)
    c = mounts_mod.add_mount("open", "gcs-open:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.bearer_upstream_for(f) is None
    assert resolver_calls == []  # anonymous never consults the token resolver


def test_bearer_upstream_none_for_sa_key_gcs(home, rcd, fresh_upstream, monkeypatch):
    import os

    rcd.responses["config/get"] = _CRED_GCS_CFG
    _stub_gcs_signer(monkeypatch, present=True)  # signable -> 307 path, not bearer
    _stub_gcs_token(monkeypatch, token="X")
    c = mounts_mod.add_mount("gcp", "gcp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a.parquet")
    assert mounts_mod.bearer_upstream_for(f) is None


def test_bearer_upstream_none_outside_mounts(fresh_upstream):
    assert mounts_mod.bearer_upstream_for("/somewhere/else.parquet") is None


# -- unified direct-listing dispatchers (S3 + GCS) --------------------------


def test_direct_list_capable_resolves_no_credentials(home, rcd, fresh_upstream, monkeypatch):
    # FINDING 12: direct_list_capable is a pure config-shape check — it must walk
    # NONE of the credential/token resolvers, so the unbudgeted conditions/stat
    # callers can't be stalled by a black-holed metadata endpoint.
    resolved = []

    def _spy(fnname):
        def f(*a, **k):
            resolved.append(fnname)
        return f

    for fnname in ("resolve_static_credentials", "resolve_botocore_chain",
                   "resolve_credentials"):
        monkeypatch.setattr(mounts_mod.s3sign, fnname, _spy("s3:" + fnname))
    for fnname in ("resolve_credentials", "resolve_token", "resolve_signer"):
        monkeypatch.setattr(mounts_mod.gcssign, fnname, _spy("gcs:" + fnname))
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    s = mounts_mod.add_mount("corp", "corp:bucket")
    assert mounts_mod.direct_list_capable(mounts_mod.mountpoint(s))
    mounts_mod._upstream_cfg.clear()
    rcd.responses["config/get"] = _CRED_GCS_CFG
    g = mounts_mod.add_mount("gcp", "gcp:bucket")
    assert mounts_mod.direct_list_capable(mounts_mod.mountpoint(g))
    assert resolved == []  # no credential/token resolution from the predicate


def test_direct_list_capable_true_for_both_backends(home, rcd, fresh_upstream):
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("s3open", "aws-open:mur-sst/zarr-v1")
    assert mounts_mod.direct_list_capable(mounts_mod.mountpoint(c))
    mounts_mod._upstream_cfg.clear()
    rcd.responses["config/get"] = _ANON_GCS_CFG
    g = mounts_mod.add_mount("gcsopen", "gcs-open:mur-sst/zarr-v1")
    assert mounts_mod.direct_list_capable(mounts_mod.mountpoint(g))


def test_direct_list_page_routes_gcs_to_googleapis(home, rcd, fresh_upstream, gcs_urlopen):
    calls, _box = gcs_urlopen
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    mounts_mod.direct_list_page(mounts_mod.mountpoint(c), max_keys=1000)
    assert calls and "storage.googleapis.com" in calls[0][0]


def test_s3listerror_is_directlisterror_alias():
    assert mounts_mod.S3ListError is mounts_mod.DirectListError


# -- direct_head / direct_is_dir (point probes) -----------------------------
#
# operations/stat has no S3 point lookup — rclone answers a negative or a dir
# probe with an UNBOUNDED ListObjectsV2 of the whole parent prefix, so on a flat
# world-scale prefix every probe burns the full rc timeout. HeadObject and a
# max-keys=1 list are the true point lookups. These drive direct_head /
# direct_is_dir against a LOCAL stub HTTP server (real HTTP HEAD/GET, real 404 ->
# HTTPError, real header parsing); a urlopen redirector rewrites the object-store
# host to the stub so the real path-style URL building still runs.


class DirectObjStub:
    """A stand-in for anonymous S3 / GCS object endpoints. `head` drives the
    HEAD (or GCS objects.get GET) reply; `listing` drives the GET list reply."""

    def __init__(self):
        self.calls = []  # (method, path, query)
        self.delay = {}  # method -> seconds to sleep before responding (timeouts)
        # (status, headers-or-body). 200 head -> send those headers, no body.
        self.head = (200, {"Content-Length": "123",
                           "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.head_body = b'{"size": "123", "updated": "2015-10-21T07:28:00Z"}'
        self.listing = (200, b"")  # GET list/objects.get body
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                p, _, q = self.path.partition("?")
                stub.calls.append((self.command, p, q))
                if self.command in stub.delay:
                    time.sleep(stub.delay[self.command])

            def do_HEAD(self):
                self._record()
                status, headers = stub.head
                if status != 200:
                    self.send_error(status)
                    return
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()

            def do_GET(self):
                self._record()
                # GCS objects.get lands here too (path has no query); the
                # objects.list has a query. Both use the `listing`/`head_body`.
                p = self.path
                if "/storage/v1/b/" in p and "?" not in p:
                    status, body = 200, stub.head_body  # objects.get metadata
                    if stub.head[0] != 200:
                        self.send_error(stub.head[0])
                        return
                else:
                    status, body = stub.listing
                if status != 200:
                    self.send_error(status)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def direct_stub(monkeypatch):
    """A live object-store stub + a urlopen redirector: object-store URLs
    (amazonaws / googleapis) are rewritten to the local stub host, preserving
    path/query/method; rc calls (loopback rcd) delegate to the real urlopen."""
    import urllib.parse as _up

    stub = DirectObjStub()
    real = mounts_mod.urllib.request.urlopen

    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.get_full_url()
        parts = _up.urlsplit(url)
        if ("amazonaws.com" not in (parts.hostname or "")
                and "googleapis.com" not in (parts.hostname or "")):
            return real(req, timeout=timeout)
        local = _up.urlunsplit(("http", f"127.0.0.1:{stub.port}",
                               parts.path, parts.query, ""))
        method = "GET" if isinstance(req, str) else req.get_method()
        return real(mounts_mod.urllib.request.Request(local, method=method),
                    timeout=timeout)

    monkeypatch.setattr(mounts_mod.urllib.request, "urlopen", fake)
    # Signable probes go through the non-redirect opener; redirect it too.
    monkeypatch.setattr(mounts_mod._NO_REDIRECT_OPENER, "open", fake)
    yield stub
    stub.close()


def test_direct_head_s3_exists_returns_size_and_mtime(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    got = mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/analysed_sst/zarr.json")
    assert got.exists is True and got.size == 123
    assert got.mtime.startswith("2015-10-21T07:28:00")
    # A HEAD, not a prefix list — one point round trip.
    assert direct_stub.calls and direct_stub.calls[-1][0] == "HEAD"


def test_direct_head_s3_missing_is_definitive_not_error(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.head = (404, {})
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    got = mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/nope.json")
    assert got.exists is False  # a 404 is a trustworthy negative, not a raise


def test_direct_head_s3_http_error_raises(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.head = (403, {})  # needs auth -> indeterminate
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    with pytest.raises(mounts_mod.DirectProbeError):
        mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/x.json")


def test_direct_head_not_capable_raises(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}  # credentialed
    c = mounts_mod.add_mount("corp", "corp:bucket")
    with pytest.raises(mounts_mod.DirectProbeError):
        mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/x.json")


def test_direct_is_dir_s3_true_when_children(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.listing = (200, _s3_list_xml(contents=[("zarr-v1/analysed_sst/x", 1, "2024-01-01T00:00:00Z")]))
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    assert mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/analysed_sst") is True
    # max-keys=1 list against the prefix, off the kernel.
    assert any(q for _m, _p, q in direct_stub.calls if "max-keys=1" in q)


def test_direct_is_dir_s3_false_when_empty(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.listing = (200, _s3_list_xml())  # no Contents
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    assert mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/ghost") is False


def test_direct_is_dir_s3_http_error_raises(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.listing = (500, b"")
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    with pytest.raises(mounts_mod.DirectProbeError):
        mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/x")


def test_direct_head_gcs_exists_and_dir(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_GCS_CFG
    c = mounts_mod.add_mount("open", "gcs-open:mur-sst/zarr-v1")
    got = mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/analysed_sst/zarr.json")
    assert got.exists is True and got.size == 123
    assert got.mtime == "2015-10-21T07:28:00Z"
    direct_stub.listing = (200, _gcs_list_json(items=[("zarr-v1/analysed_sst/x", 1, "2024-01-01T00:00:00Z")]))
    assert mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/analysed_sst") is True


def test_direct_head_signable_signs_head_method(home, rcd, fresh_upstream, direct_stub):
    # A signable (credentialed) remote probes via a presigned HEAD — signed as
    # HEAD, not GET (a presigned GET rejects a HEAD request).
    rcd.responses["config/get"] = _CRED_S3_CFG
    direct_stub.head = (200, {"Content-Length": "10",
                              "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"})
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    got = mounts_mod.direct_head(mounts_mod.mountpoint(c) + "/x.json")
    assert got.exists is True and got.size == 10
    method, _p, query = direct_stub.calls[-1]
    assert method == "HEAD"
    assert "X-Amz-Signature" in query


def test_direct_head_signable_404_is_definitive(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _CRED_S3_CFG
    direct_stub.head = (404, {})
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    assert mounts_mod.direct_head(
        mounts_mod.mountpoint(c) + "/nope.json").exists is False


def test_direct_is_dir_signable_presigns_list(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _CRED_S3_CFG
    direct_stub.listing = (200, _s3_list_xml(
        contents=[("pre/sub/x", 1, "2024-01-01T00:00:00Z")]))
    c = mounts_mod.add_mount("corp", "corp:bucket/pre")
    assert mounts_mod.direct_is_dir(mounts_mod.mountpoint(c) + "/sub") is True
    _method, _p, query = direct_stub.calls[-1]
    assert "max-keys=1" in query and "X-Amz-Signature" in query


# -- rc stat helpers route direct-first (no operations/stat when capable) ----


def test_rc_kind_for_uses_direct_probe_not_operations_stat(home, rcd, fresh_upstream, direct_stub):
    # A direct-capable mount must answer rc_kind_for via HeadObject, NEVER via
    # operations/stat (which lists the whole prefix). The stub rcd would fail the
    # test if operations/stat were called.
    rcd.responses["config/get"] = _ANON_S3_CFG
    rcd.responses["operations/stat"] = (500, {"error": "must not be called"})
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    kind = mounts_mod.rc_kind_for(mounts_mod.mountpoint(c) + "/analysed_sst/zarr.json")
    assert kind == "file"
    assert not any(x[0] == "operations/stat" for x in rcd.calls)


def test_rc_kind_for_direct_dir_then_missing(home, rcd, fresh_upstream, direct_stub):
    rcd.responses["config/get"] = _ANON_S3_CFG
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    base = mounts_mod.mountpoint(c)
    # HEAD 404 + a non-empty prefix list -> "dir".
    direct_stub.head = (404, {})
    direct_stub.listing = (200, _s3_list_xml(contents=[("zarr-v1/d/x", 1, "2024-01-01T00:00:00Z")]))
    assert mounts_mod.rc_kind_for(base + "/d") == "dir"
    # HEAD 404 + empty prefix list -> "missing" (trustworthy negative).
    direct_stub.listing = (200, _s3_list_xml())
    assert mounts_mod.rc_kind_for(base + "/gone") == "missing"
    assert not any(x[0] == "operations/stat" for x in rcd.calls)


def test_direct_stat_pair_shares_one_timeout_budget(home, rcd, fresh_upstream, direct_stub):
    # A dir/miss resolution runs direct_head THEN direct_is_dir back-to-back. If
    # each got a fresh full `timeout`, one logical rc_kind_for could burn up to
    # 2x the caller's budget (the "direct stat doubles probe timeout" bug). The
    # two probes must SHARE one deadline, so a slow head leaves the list only the
    # remaining budget — a single stat never exceeds ~timeout.
    rcd.responses["config/get"] = _ANON_S3_CFG
    # Both point probes are slow (0.8s); together they'd be ~1.6s if unbounded.
    direct_stub.head = (404, {})                 # HEAD -> not a file (proceeds to dir probe)
    direct_stub.listing = (200, _s3_list_xml())  # empty prefix
    direct_stub.delay = {"HEAD": 0.8, "GET": 0.8}
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    t0 = time.monotonic()
    mounts_mod.rc_kind_for(mounts_mod.mountpoint(c) + "/slow", timeout=1.0)
    elapsed = time.monotonic() - t0
    # With the shared budget the head eats most of the 1.0s and the list gets no
    # meaningful budget, so the whole call stays well under 2x. Without the fix
    # this is ~1.6s.
    assert elapsed < 1.3, f"one stat took {elapsed:.2f}s, ~2x the 1.0s budget"


def test_rc_stat_result_falls_back_to_rc_when_direct_errors(home, rcd, fresh_upstream, direct_stub):
    # Direct probe indeterminate (403) -> fall back to operations/stat, which
    # here reports a healthy directory item. Proves the ladder direct -> rc.
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.head = (403, {})       # HEAD indeterminate
    direct_stub.listing = (403, b"")   # is_dir indeterminate too
    rcd.responses["operations/stat"] = {"item": {"IsDir": True, "Size": -1,
                                                 "ModTime": "2024-01-02T03:04:05Z"}}
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    st = mounts_mod.rc_stat_result(mounts_mod.mountpoint(c) + "/d")
    assert stat.S_ISDIR(st.st_mode)
    assert any(x[0] == "operations/stat" for x in rcd.calls)


def test_probe_floor_never_exceeds_caller_timeout(home, rcd, fresh_upstream, direct_stub):
    # _DIRECT_PROBE_MIN_S is a bail-out threshold, NOT a grant: when the
    # caller's remaining budget is already below the floor, the stat must
    # report indeterminate immediately — a max(floor, remaining) clamp would
    # instead hand a 0.5s head probe PLUS a 0.5s rc fallback to a caller who
    # asked for 0.2s, stacking one logical stat past the gate budget (the
    # "probe floor exceeds caller timeout" bug).
    rcd.responses["config/get"] = _ANON_S3_CFG
    direct_stub.head = (404, {})
    direct_stub.listing = (200, _s3_list_xml())
    direct_stub.delay = {"HEAD": 2.0, "GET": 2.0}  # any granted probe shows up as wall time
    c = mounts_mod.add_mount("open", "aws-open:mur-sst/zarr-v1")
    t0 = time.monotonic()
    kind = mounts_mod.rc_kind_for(mounts_mod.mountpoint(c) + "/x", timeout=0.2)
    elapsed = time.monotonic() - t0
    assert kind == "indeterminate"
    assert elapsed < 0.19, (
        f"sub-floor budget still ran probes for {elapsed:.2f}s (floor granted, not bailed)")


# -- restart_rcd / _kill_current_rcd: clean daemon restart -----------------
#
# The global "Restart rclone" recovery: tear down every kernel mount, kill the
# confirmed daemon, spawn a fresh one (which re-reads ~/.aws/credentials — the
# credential-expiry fix), then re-mount everything. _kill_current_rcd carries
# the critical safety invariant: it must NEVER signal a pid it can't confirm is
# our rcd.


def test_restart_rcd_teardown_then_respawn_then_automount(home, rcd, monkeypatch):
    mounts_mod.add_mount("a", "r:one")
    mounts_mod.add_mount("b", "r:two")
    events = []
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: events.append(("detach", m["name"], force)))
    monkeypatch.setattr(mounts_mod, "_kill_current_rcd",
                        lambda: events.append(("kill",)))
    monkeypatch.setattr(mounts_mod, "_ensure_rcd_locked",
                        lambda: events.append(("spawn",)))
    monkeypatch.setattr(mounts_mod, "run_automount",
                        lambda: events.append(("automount",)))

    mounts_mod.restart_rcd()

    # Every mount is force-detached FIRST, then kill, then spawn, then automount.
    assert events == [
        ("detach", "a", True),
        ("detach", "b", True),
        ("kill",),
        ("spawn",),
        ("automount",),
    ]


def test_restart_rcd_propagates_spawn_failure_after_teardown(home, rcd, monkeypatch):
    mounts_mod.add_mount("a", "r:one")
    events = []
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: events.append("detach"))
    monkeypatch.setattr(mounts_mod, "_kill_current_rcd",
                        lambda: events.append("kill"))

    def boom():
        events.append("spawn")
        raise RuntimeError("rclone rcd did not come up within 10s")

    monkeypatch.setattr(mounts_mod, "_ensure_rcd_locked", boom)
    monkeypatch.setattr(mounts_mod, "run_automount",
                        lambda: events.append("automount"))

    with pytest.raises(RuntimeError, match="did not come up"):
        mounts_mod.restart_rcd()
    # Teardown + kill + failed spawn ran; automount did NOT (honest half-state).
    assert events == ["detach", "kill", "spawn"]


def test_restart_rcd_detach_failure_is_best_effort(home, rcd, monkeypatch):
    # A wedged mount whose detach raises must not abort the restart — the whole
    # point is to recover from wedged mounts.
    mounts_mod.add_mount("a", "r:one")
    events = []

    def bad_detach(m, force=False):
        raise OSError("wedged")

    monkeypatch.setattr(mounts_mod, "detach_mount", bad_detach)
    monkeypatch.setattr(mounts_mod, "_kill_current_rcd",
                        lambda: events.append("kill"))
    monkeypatch.setattr(mounts_mod, "_ensure_rcd_locked",
                        lambda: events.append("spawn"))
    monkeypatch.setattr(mounts_mod, "run_automount",
                        lambda: events.append("automount"))

    mounts_mod.restart_rcd()
    assert events == ["kill", "spawn", "automount"]


def test_kill_current_rcd_refuses_unconfirmed_pid(home, monkeypatch):
    # THE safety invariant: an alive pid we can't confirm is our rcd is NEVER
    # signalled — raise instead of murdering an unrelated process.
    mounts_mod.write_rcd_state(12345, 999)
    monkeypatch.setattr(mounts_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd", lambda e: False)
    killed = []
    monkeypatch.setattr(mounts_mod.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(RuntimeError):
        mounts_mod._kill_current_rcd()
    assert killed == []


def test_kill_current_rcd_noops_without_state(home, monkeypatch):
    # No rcd.json at all -> nothing to kill (the fresh spawn will just start one).
    killed = []
    monkeypatch.setattr(mounts_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    mounts_mod._kill_current_rcd()
    assert killed == []


def test_kill_current_rcd_noops_when_pid_already_dead(home, monkeypatch):
    # Dead pid short-circuits BEFORE the confirm check: already gone, drop it.
    mounts_mod.write_rcd_state(12345, 999)
    monkeypatch.setattr(mounts_mod, "_pid_alive", lambda pid: False)
    confirmed = []
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd",
                        lambda e: confirmed.append(e) or True)
    killed = []
    monkeypatch.setattr(mounts_mod.os, "kill",
                        lambda pid, sig: killed.append(pid))
    mounts_mod._kill_current_rcd()
    assert killed == []
    assert confirmed == []


def test_kill_current_rcd_signals_confirmed_pid(home, monkeypatch):
    mounts_mod.write_rcd_state(12345, 999)
    alive = {"v": True}
    monkeypatch.setattr(mounts_mod, "_pid_alive", lambda pid: alive["v"])
    monkeypatch.setattr(mounts_mod, "_confirmed_our_rcd", lambda e: True)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)
    sigs = []

    def fake_kill(pid, sig):
        sigs.append((pid, sig))
        alive["v"] = False  # dies on the first (graceful) signal

    monkeypatch.setattr(mounts_mod.os, "kill", fake_kill)
    mounts_mod._kill_current_rcd()
    # One SIGTERM was enough; no SIGKILL escalation.
    assert sigs == [(999, mounts_mod.signal.SIGTERM)]


# -- POST /api/mounts/restart ----------------------------------------------


def test_restart_endpoint_requires_fused_and_returns_mounts(client, rcd, monkeypatch):
    # Write guard: no X-Fused -> 403, and restart_rcd is never invoked.
    called = []
    monkeypatch.setattr(mounts_mod, "restart_rcd", lambda: called.append(True))
    assert client.post("/api/mounts/restart").status_code == 403
    assert called == []

    r = client.post("/api/mounts/restart", headers=FUSED)
    assert r.status_code == 200
    assert called == [True]
    # Same payload as GET /api/mounts, so the client refreshes in one shot.
    assert set(r.json()) == {"rclone", "mounts"}


def test_restart_endpoint_500_on_failure(client, rcd, monkeypatch):
    def boom():
        raise RuntimeError("rclone rcd did not come up within 10s")

    monkeypatch.setattr(mounts_mod, "restart_rcd", boom)
    r = client.post("/api/mounts/restart", headers=FUSED)
    assert r.status_code == 500
    assert "did not come up" in r.json()["error"]


# -- mount_restart_reason: params drift + credential-refresh ----------------
#
# A REASON STRING surfaced on mount_view so the UI can prompt a restart:
#   "params"      — live+mounted but the running mount was baked with different
#                   params than the record now wants (conservative subset:
#                   read_only, the one param the UI changes).
#   "credentials" — a disconnected/stale env_auth mount whose creds probe VALID
#                   again: the daemon still holds the pre-refresh keys, so only
#                   a restart re-reads them (Reconnect can't).


def test_mount_restart_reason_params_on_read_only_drift(home, rcd):
    m = {"id": "x", "name": "data", "remote": "r:bucket",
         "read_only": True, "mounted_read_only": False}
    # mounted + the live VFS was baked read_write but the record now wants
    # read_only -> a restart is needed to apply it.
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="mounted") == "params"
    # in agreement -> no drift
    m["mounted_read_only"] = True
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="mounted") is None
    # params drift is only meaningful for a live mount
    m["mounted_read_only"] = False
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") is None


def test_mount_restart_reason_credentials_when_reauthed(
        home, rcd, monkeypatch, fresh_upstream):
    m = mounts_mod.add_mount("data", "corp:bucket")
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=0)  # creds probe VALID again (user re-authed)
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") == "credentials"
    # stale is healed the same way
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="stale") == "credentials"


def test_mount_restart_reason_none_when_creds_still_expired(
        home, rcd, monkeypatch, fresh_upstream):
    m = mounts_mod.add_mount("data", "corp:bucket")
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=1, lsd_stderr="ExpiredToken")  # genuinely expired
    # Genuinely-expired creds: a restart won't help, so no prompt.
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") is None


def test_mount_restart_reason_none_for_non_env_auth(
        home, rcd, monkeypatch, fresh_upstream):
    m = mounts_mod.add_mount("data", "corp:bucket")
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false",
                                   "access_key_id": "AKIA"}
    calls = []
    _stub_lsd(monkeypatch, rc=0, record=calls)
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") is None
    # A non-env_auth remote never gets the (latency-costing) lsd probe.
    assert not any("lsd" in cmd for cmd in calls)


def test_mount_view_includes_restart_reason(home, rcd):
    c = mounts_mod.add_mount("data", "r:bucket")
    view = mounts_mod.mount_view(c)
    assert "restart_reason" in view
    assert view["restart_reason"] is None  # healthy/unmounted -> nothing to prompt


# -- review fixes: tri-state credential probe, drift + perf guards ----------


def test_mount_restart_reason_inconclusive_probe_is_not_credentials(
        home, rcd, monkeypatch, fresh_upstream):
    # A non-credential failure (AccessDenied = valid keys, no list perm) is
    # INCONCLUSIVE, not proof the creds work — no false "credentials" prompt.
    m = mounts_mod.add_mount("data", "corp:bucket")
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=1, lsd_stderr="AccessDenied: not authorized")
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") is None

    # A timeout is likewise inconclusive (config/get is already memoized).
    def timeout_run(cmd, **kw):
        raise mounts_mod.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(mounts_mod.subprocess, "run", timeout_run)
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="disconnected") is None


def test_broken_mount_error_inconclusive_creds_falls_through_to_reconnect(
        home, rcd, monkeypatch, fresh_upstream):
    # env_auth + a transient/non-credential failure: neither "refresh" nor
    # "restart" — fall through to the plain reconnect guidance.
    c, mp = _disconnected_mount(home, rcd, monkeypatch)
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    _stub_lsd(monkeypatch, rc=1, lsd_stderr="AccessDenied: not authorized")
    err = mounts_mod.broken_mount_error(_os.path.join(mp, "data"))
    assert err is not None
    assert "reconnect" in err.lower()
    assert "restart" not in err.lower()
    assert "refresh" not in err.lower()


def test_mount_restart_reason_no_params_for_adopted_legacy_read_only(home, rcd):
    # A read_only mount adopted via listmounts (no mounted_read_only key, so we
    # can't know what the live VFS was baked with) must NOT show a false
    # "params changed" prompt.
    m = {"id": "x", "name": "data", "remote": "r:bucket", "read_only": True}
    assert mounts_mod.mount_restart_reason(
        m, rcd_mounts=set(), state="mounted") is None


def test_mount_restart_reason_skips_mounted_paths_when_state_supplied(
        home, monkeypatch):
    # The error path passes state, so the rc round-trip mounted_paths() must be
    # skipped entirely.
    def boom():
        raise AssertionError("mounted_paths() called on the state-supplied path")

    monkeypatch.setattr(mounts_mod, "mounted_paths", boom)
    m = {"id": "x", "name": "data", "remote": "r:bucket",
         "read_only": False, "mounted_read_only": False}
    assert mounts_mod.mount_restart_reason(m, state="mounted") is None


def test_get_mounts_probes_credentials_off_serial_path(
        client, rcd, home, monkeypatch):
    # Several broken env_auth mounts must NOT stall the polled Mounts page: the
    # credential probe runs once per mount, in PARALLEL with the state probes,
    # off the serial view-building path.
    mps = []
    for i in range(4):
        _c, mp = _make_mount(home, rcd, name=f"m{i}",
                             remote=f"corp{i}:bucket", served=False)
        mps.append(mp)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p in mps)
    calls = []

    def slow_status(m, bin_=None):
        calls.append(m["name"])
        time.sleep(0.5)
        return "valid"

    monkeypatch.setattr(mounts_mod, "_mount_credential_status", slow_status)
    t0 = time.monotonic()
    data = client.get("/api/mounts").json()
    elapsed = time.monotonic() - t0

    # Exactly one probe per broken mount — no second probe from mount_view.
    assert sorted(calls) == ["m0", "m1", "m2", "m3"]
    # 4 x 0.5s SERIALLY would be >= 2s; parallel is ~0.5s.
    assert elapsed < 1.3, f"credential probes ran serially ({elapsed:.2f}s)"
    assert all(mv["restart_reason"] == "credentials" for mv in data["mounts"])


# --------------------------------------------------- health monitor: episode dedup


def test_health_monitor_notifies_once_per_episode(monkeypatch):
    """poll_once is DETECTION-ONLY: it emits exactly one "disconnected" event
    per broken episode and NEVER calls reconnect_mount (auto-reconnect is off —
    it churned on flap-prone mounts). A healthy->broken transition notifies once,
    staying broken emits nothing more, a return to "mounted" re-arms, and
    "unmounted" (user-detached) never notifies. Classification is I/O-free
    (probe_io=False)."""
    # Isolate module-level monitor state so ids/episodes don't leak across tests.
    monkeypatch.setattr(mounts_mod, "_health_episodes", {})
    monkeypatch.setattr(mounts_mod, "_health_events",
                        __import__("collections").deque(maxlen=100))
    monkeypatch.setattr(mounts_mod, "_health_event_seq", 0)

    m = {"id": "m1", "name": "n1", "remote": "r:bucket"}
    monkeypatch.setattr(mounts_mod, "list_mounts", lambda: [m])
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: set())

    current = {"state": "mounted"}
    # The monitor calls mount_state(..., probe_io=False); accept the kwarg.
    monkeypatch.setattr(
        mounts_mod, "mount_state",
        lambda mm, live, timeout=None, probe_io=True: current["state"])

    # Auto-reconnect is disabled: the monitor must NEVER call reconnect_mount.
    reconnects = []
    monkeypatch.setattr(mounts_mod, "reconnect_mount",
                        lambda mm: reconnects.append(mm["id"]))

    def kinds():
        return [e["kind"] for e in mounts_mod._health_events]

    # 1. Healthy tick: nothing.
    mounts_mod.poll_once()
    assert kinds() == []

    # 2. healthy -> disconnected: exactly one "disconnected" event.
    current["state"] = "disconnected"
    mounts_mod.poll_once()
    assert kinds() == ["disconnected"]

    # 3. still broken across ticks: no repeat (episode already notified).
    mounts_mod.poll_once()
    mounts_mod.poll_once()
    assert kinds() == ["disconnected"]

    # 4. mounted again -> re-arm; a fresh drop (the other broken state) notifies.
    current["state"] = "mounted"
    mounts_mod.poll_once()
    current["state"] = "stale"
    mounts_mod.poll_once()
    assert kinds() == ["disconnected", "disconnected"]

    # 5. "unmounted" (user-detached) never notifies, even from healthy.
    current["state"] = "mounted"
    mounts_mod.poll_once()
    current["state"] = "unmounted"
    mounts_mod.poll_once()
    assert kinds() == ["disconnected", "disconnected"]

    # Monotonic, distinct ids; and NOT ONCE did the monitor auto-reconnect.
    assert [e["id"] for e in mounts_mod._health_events] == [1, 2]
    assert reconnects == []
