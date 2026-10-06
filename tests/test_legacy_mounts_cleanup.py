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
