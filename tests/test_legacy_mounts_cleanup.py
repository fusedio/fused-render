"""Upgrade shim: leftovers of the removed rclone mount feature are cleaned up
once, best-effort, with the subprocess layer faked. Delete with the shim."""
import json
import os
import signal
import subprocess

from fused_render import legacy_mounts_cleanup as lmc


class _Runner:
    def __init__(self, ps_out="rclone rcd --rc-addr"):
        self.calls = []
        self.ps_out = ps_out

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        out = self.ps_out if os.path.basename(argv[0]) == "ps" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


def _seed(tmp_path):
    home = tmp_path / "home"
    (home / "mounts" / "s3-bucket").mkdir(parents=True)
    (home / "nfs-handle-cache").mkdir()
    (home / "nfs-handle-cache" / "x").write_text("1")
    for n in ("mounts.json", "connectors.json", "rcd.log", "serves.json"):
        (home / n).write_text("{}")
    (home / "rcd.json").write_text(json.dumps({"pid": 4242, "port": 5572}))
    (home / "rclone").mkdir()
    (home / "rclone" / "rclone.conf").write_text("[remote]\ntype = s3\n")
    (home / "keep.json").write_text("{}")
    (tmp_path / "rcd-registry.json").write_text(json.dumps(
        [{"pid": 4242, "port": 5572, "dir": str(home)}]))
    cache = tmp_path / "cache"
    (cache / "rclone" / "vfs").mkdir(parents=True)
    return home, cache


def _go(tmp_path, runner, kills, platform="darwin"):
    home, cache = _seed(tmp_path)

    def kill(pid, sig):
        kills.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    lmc.run(home=str(home), base_home=str(tmp_path),
            environ={"FUSED_RENDER_CACHE_DIR": str(cache)},
            runner=runner, kill=kill, platform=platform, sleep=lambda s: None)
    return home, cache


def test_cleans_state_and_never_deletes_rclone_conf(tmp_path):
    runner, kills = _Runner(), []
    home, cache = _go(tmp_path, runner, kills)
    for n in ("mounts.json", "connectors.json", "rcd.json", "rcd.log",
              "serves.json", "nfs-handle-cache", "mounts"):
        assert not (home / n).exists(), n
    assert not (tmp_path / "rcd-registry.json").exists()
    assert not (cache / "rclone").exists()
    assert (home / "rclone" / "rclone.conf").read_text().startswith("[remote]")
    assert (home / "keep.json").exists()
    assert (home / lmc.MARKER).exists()
    assert kills[0] == (4242, signal.SIGTERM)


def test_subprocess_rules_and_force_unmount(tmp_path):
    runner, kills = _Runner(), []
    _go(tmp_path, runner, kills)
    umounts = [c for c, _ in runner.calls if "umount" in os.path.basename(c[0])
               or "diskutil" in os.path.basename(c[0])]
    assert umounts, runner.calls
    assert any(c[-1].endswith("s3-bucket") for c in umounts)
    for argv, kw in runner.calls:
        assert os.path.isabs(argv[0])
        assert kw.get("close_fds") is False
        assert "cwd" not in kw
        assert kw.get("timeout")


def test_linux_uses_fusermount_lazy(tmp_path, monkeypatch):
    monkeypatch.setattr(lmc.shutil, "which",
                        lambda n: f"/usr/bin/{n}" if n.startswith("fusermount") else None)
    runner, kills = _Runner(), []
    _go(tmp_path, runner, kills, platform="linux")
    assert any(c[0] == "/usr/bin/fusermount3" and "-uz" in c for c, _ in runner.calls)


def test_does_not_kill_a_recycled_pid(tmp_path):
    runner, kills = _Runner(ps_out="/usr/bin/vim notes.txt"), []
    _go(tmp_path, runner, kills)
    assert kills == []


def test_never_raises_and_runs_once(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("no")
    home, _ = _seed(tmp_path)
    lmc.run(home=str(home), base_home=str(tmp_path), environ={},
            runner=boom, kill=boom, platform="darwin", sleep=lambda s: None)
    assert not (home / "mounts.json").exists()
    assert (home / "rclone" / "rclone.conf").exists()
    # second run after the marker: no subprocess at all
    runner = _Runner()
    lmc.run(home=str(home), base_home=str(tmp_path), environ={}, runner=runner,
            platform="darwin")
    assert runner.calls == []


def test_fresh_install_only_writes_marker(tmp_path):
    runner = _Runner()
    home = tmp_path / "h"
    lmc.run(home=str(home), base_home=str(tmp_path), environ={}, runner=runner)
    assert runner.calls == []
    assert (home / lmc.MARKER).exists()


# ---------------------------------------------------------------- review fixes


def test_an_inherited_rclone_cache_dir_is_never_deleted(tmp_path):
    """RCLONE_CACHE_DIR may be the user's own rclone cache; only the dirs
    fused-render itself configured (resolved per platform) are removed."""
    home, _ = _seed(tmp_path)
    theirs = tmp_path / "their-rclone-cache"
    (theirs / "vfs").mkdir(parents=True)
    user_home = tmp_path / "user"
    ours = user_home / ".cache" / "fused-render" / "rclone"
    (ours / "vfs").mkdir(parents=True)
    lmc.run(home=str(home), base_home=str(tmp_path),
            environ={"RCLONE_CACHE_DIR": str(theirs)}, runner=_Runner(),
            kill=lambda p, s: None, platform="linux", sleep=lambda s: None,
            user_home=str(user_home))
    assert (theirs / "vfs").exists()
    assert not ours.exists()  # resolved without FUSED_RENDER_CACHE_DIR


def test_cache_dirs_per_platform(tmp_path):
    uh = str(tmp_path)
    assert lmc._cache_dirs({"XDG_CACHE_HOME": "/x/c"}, "linux", uh) == [
        os.path.join("/x/c", "fused-render", "rclone")]
    assert lmc._cache_dirs({"XDG_CACHE_HOME": "rel"}, "linux", uh) == [
        os.path.join(uh, ".cache", "fused-render", "rclone")]
    assert lmc._cache_dirs({"LOCALAPPDATA": "C:/L"}, "win32", uh) == [
        os.path.join("C:/L", "FusedRender", "cache", "rclone")]
    assert lmc._cache_dirs({"RCLONE_CACHE_DIR": "/theirs"}, "darwin", uh) == []


def test_other_homes_registry_entries_survive(tmp_path):
    home, _ = _seed(tmp_path)
    other = {"pid": 77, "port": 5599, "dir": str(tmp_path / "other-home")}
    (tmp_path / "rcd-registry.json").write_text(json.dumps(
        [{"pid": 4242, "port": 5572, "dir": str(home)}, other]))
    lmc.run(home=str(home), base_home=str(tmp_path), environ={},
            runner=_Runner(), kill=lambda p, s: None, platform="darwin",
            sleep=lambda s: None)
    left = json.loads((tmp_path / "rcd-registry.json").read_text())
    assert left == [other]


def test_registry_is_deleted_when_it_empties(tmp_path):
    home, _ = _seed(tmp_path)
    lmc.run(home=str(home), base_home=str(tmp_path), environ={},
            runner=_Runner(), kill=lambda p, s: None, platform="darwin",
            sleep=lambda s: None)
    assert not (tmp_path / "rcd-registry.json").exists()


def test_a_failed_unmount_leaves_the_mountpoint_alone(tmp_path, monkeypatch):
    """rmdir stats the mount path; it only happens after an unmount returned 0."""
    home, _ = _seed(tmp_path)

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "busy")

    rmdirs = []
    real_rmdir = os.rmdir
    monkeypatch.setattr(lmc.os, "rmdir",
                        lambda p: (rmdirs.append(p), real_rmdir(p))[1])
    lmc.run(home=str(home), base_home=str(tmp_path), environ={}, runner=failing,
            kill=lambda p, s: None, platform="darwin", sleep=lambda s: None)
    assert not any(p.endswith("s3-bucket") for p in rmdirs)
    assert (home / "mounts" / "s3-bucket").exists()
    assert (home / lmc.MARKER).exists()


def test_a_successful_unmount_removes_the_empty_mountpoint(tmp_path):
    home, _ = _go(tmp_path, _Runner(), [])
    assert not (home / "mounts").exists()


def test_windows_requires_rcd_command_line_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(lmc.shutil, "which",
                        lambda n: f"C:/Win/{n}")
    calls = []

    def runner(argv, **kw):
        calls.append(list(argv))
        out = kw_out["v"] if "powershell" in argv[0] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    kw_out = {"v": "C:\\rclone.exe mount remote: Z:"}
    assert lmc._pid_is_rcd(4242, runner=runner, platform="win32") is False
    kw_out["v"] = "C:\\rclone.exe rcd --rc-addr 127.0.0.1:5572"
    assert lmc._pid_is_rcd(4242, runner=runner, platform="win32") is True
    # no PowerShell: skip rather than guess
    monkeypatch.setattr(lmc.shutil, "which", lambda n: None)
    assert lmc._pid_is_rcd(4242, runner=runner, platform="win32") is False


def test_windows_taskkill_has_no_tree_flag(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    import fused_render.legacy_mounts_cleanup as m
    orig = m.shutil.which
    m.shutil.which = lambda n: f"C:/Win/{n}"
    try:
        lmc._stop_pid(4242, runner=runner, kill=None, platform="win32",
                      sleep=None)
    finally:
        m.shutil.which = orig
    assert calls and "/T" not in calls[0] and "/F" in calls[0]


def test_a_hung_child_is_abandoned_not_waited_on(tmp_path):
    """A child that ignores the timeout kill must not hang the cleanup: the
    bounded runner raises TimeoutExpired without a second unbounded wait."""
    class _Pipe:
        def close(self):
            pass

    class _Proc:
        returncode = None
        stdout = stderr = _Pipe()
        waited = False

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            pass

        def wait(self, *a, **k):
            _Proc.waited = True
            raise AssertionError("must not wait")

    import pytest
    orig = lmc.subprocess.Popen
    lmc.subprocess.Popen = lambda *a, **k: _Proc()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            lmc._bounded_run(["/bin/umount", "-f", "/x"], timeout=0.01)
        assert lmc._run(["/bin/umount", "-f", "/x"]) is None
    finally:
        lmc.subprocess.Popen = orig
    assert _Proc.waited is False


def test_a_wedged_umount_still_writes_the_marker(tmp_path):
    home, _ = _seed(tmp_path)

    def wedged(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    lmc.run(home=str(home), base_home=str(tmp_path), environ={}, runner=wedged,
            kill=lambda p, s: None, platform="darwin", sleep=lambda s: None)
    assert (home / lmc.MARKER).exists()
    assert (home / "mounts" / "s3-bucket").exists()
