"""The index crawler must never touch a mount with a kernel FS syscall.

A kernel scandir/stat on an rclone NFS mount path can wedge the mount
permanently — a single READDIR on a flat million-key S3 prefix has killed
mounts in production — and a background crawler nobody is watching is more
dangerous than an interactive walk, not less. The ignore list names the mounts
dir by default, but that list is user-editable, so these tests run with an
EMPTY ignore list: what they police is the structural guard.

Same shape as the other mount-safety suites (see tests/_mount_safe_helpers).
"""
import json
import os

import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard, default_ignore, IgnoreRules
from fused_render.index.scan import run_scan
from fused_render.index.store import read_manifest
from tests._mount_safe_helpers import _mount, _no_kernel_on_mount, home  # noqa: F401


def _run(cfg, root, mounts_dir):
    run_dir = os.path.join(cfg.runs_dir, "run")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": root, "full": False, "started": 0,
                   "config": cfg.to_dict(), "mounts_dir": mounts_dir}, f)
    run_scan(run_dir)
    with open(os.path.join(run_dir, "events.jsonl")) as f:
        return [json.loads(line) for line in f if line.strip()]


def _paths(cfg):
    import pyarrow.parquet as pq
    m = read_manifest(cfg)
    out = []
    for part in m["partitions"]:
        out += pq.read_table(os.path.join(cfg.files_dir, part["file"])
                             ).column("path").to_pylist()
    return out


def test_a_scan_over_the_home_never_kernel_touches_a_mount(home, monkeypatch):
    mp = _mount("m1", on_disk=True)
    with open(os.path.join(mp, "remote.parquet"), "w") as f:
        f.write("x")
    project = home / "proj"
    project.mkdir()
    (project / "local.txt").write_text("hi", encoding="utf-8")

    # Empty ignore list on purpose: the ONLY thing standing between the
    # crawler and the mount here is the structural guard.
    cfg = IndexConfig(dir=str(home / "index"), ignore=[])
    _no_kernel_on_mount(monkeypatch, mp)
    events = _run(cfg, str(home), str(home / "mounts"))

    end = [e for e in events if e.get("type") == "run_end"][-1]
    assert end["msg"] == "complete", end.get("error")
    indexed = _paths(cfg)
    assert str(project / "local.txt") in indexed
    assert not [p for p in indexed if p.startswith(str(mp))]


def test_the_mounts_container_itself_is_never_descended(home, monkeypatch):
    mp = _mount("m2", on_disk=True)
    guard = MountGuard(mounts_dir=str(home / "mounts"))
    assert guard.blocks(str(home / "mounts"))
    assert guard.blocks(mp)
    assert guard.blocks(os.path.join(mp, "deep", "deeper"))
    assert not guard.blocks(str(home / "proj"))


def test_a_symlinked_scan_root_pointing_into_the_mounts_is_refused(home):
    mp = _mount("m3", on_disk=True)
    link = home / "shortcut"
    os.symlink(mp, link)
    guard = MountGuard(mounts_dir=str(home / "mounts"))
    # a pure string check cannot see through the symlink; blocks_root can,
    # because a root arrives from a user rather than from the walk
    assert not guard.blocks(str(link))
    assert guard.blocks_root(str(link))


def test_the_default_ignore_list_also_names_the_mounts_dir(home):
    """Defense in depth, not redundancy: the ignore entry keeps mount paths
    out of the index even for a caller that bypasses the walk (cached rows,
    a replayed FSEvents journal)."""
    rules = IgnoreRules(default_ignore())
    assert rules.is_ignored(str(home / "mounts"))
    assert rules.is_ignored_tree(str(home / "mounts" / "m1" / "deep"))
