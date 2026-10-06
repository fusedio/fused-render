"""The index crawler never descends into a fused-render home tree.

The ignore list is user-editable, so these tests run with an EMPTY ignore list:
what they police is the structural guard (MountGuard).
"""
import json
import os

import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard, norm
from fused_render.index.runner import canonical_root
from fused_render.index.scan import run_scan
from fused_render.index.store import read_manifest


def _run(cfg, root):
    """Write and run a spec exactly as `runner.start` would.

    `run_scan` trusts `spec["root"]` is already canonical — `runner.start`
    guarantees that before this path is ever reached in production
    (platform.md §1) — and this helper calls `run_scan` directly, so it has
    to make the same guarantee itself or the run's own top-level row lands
    under the native (backslash, on Windows) spelling while everything
    `scan_dir_once` discovers underneath it is `norm`ed to forward slashes."""
    run_dir = os.path.join(cfg.runs_dir, "run")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": canonical_root(root), "full": False, "started": 0,
                   "config": cfg.to_dict()}, f)
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


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def test_a_scan_over_the_home_skips_the_app_state_dir(home, tmp_path):
    state = home / "cache"
    state.mkdir()
    (state / "remote.parquet").write_text("x")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "local.txt").write_text("hi", encoding="utf-8")

    cfg = IndexConfig(dir=str(tmp_path / "index"), ignore=[])
    events = _run(cfg, str(tmp_path))

    end = [e for e in events if e.get("type") == "run_end"][-1]
    assert end["msg"] == "complete", end.get("error")
    indexed = _paths(cfg)
    assert norm(str(project / "local.txt")) in indexed
    assert not [p for p in indexed if p.startswith(norm(str(home)) + "/")]


def test_the_home_tree_is_never_descended(home, tmp_path):
    guard = MountGuard(home_dirs=[str(home)])
    assert guard.blocks(str(home))
    assert guard.blocks(str(home / "cache" / "deep" / "deeper"))
    assert not guard.blocks(str(tmp_path / "proj"))


def test_a_symlinked_scan_root_pointing_into_the_home_is_refused(home, tmp_path):
    link = tmp_path / "shortcut"
    os.symlink(home, link)
    guard = MountGuard(home_dirs=[str(home)])
    # a pure string check cannot see through the symlink; blocks_root can,
    # because a root arrives from a user rather than from the walk
    assert not guard.blocks(str(link))
    assert guard.blocks_root(str(link))


def test_the_guard_blocks_every_fused_render_home_not_just_the_current_one(
        home, tmp_path):
    default_home = tmp_path / "default-home"
    (default_home / "branches" / "b").mkdir(parents=True)
    guard = MountGuard(home_dirs=[str(default_home), str(home)])
    assert guard.blocks(str(default_home / "branches" / "b" / "cache"))
    assert guard.blocks(str(home / "x"))
    assert not guard.blocks(str(tmp_path / "Documents"))


def test_the_default_guard_covers_the_default_home_even_when_home_is_redirected(
        home, tmp_path, monkeypatch):
    fake_default = tmp_path / "userhome" / ".fused-render"
    fake_default.mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path / "userhome"), 1)
                        if p.startswith("~") else p)
    assert MountGuard().blocks(str(fake_default / "cache"))


def test_the_walk_never_crosses_onto_another_filesystem(tmp_path, monkeypatch):
    """The general form of the same failure. A mount — rclone, iCloud, SMB,
    an external disk — is always its own device, so refusing to descend into
    one costs nothing (the stat is already taken) and covers every mount the
    guard has no name for."""
    from fused_render.index.ignore import IgnoreRules
    from fused_render.index.scan import scan_dir_once

    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "f.txt").write_text("x", encoding="utf-8")
    real_stat = os.stat

    def fake_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if str(path).endswith("elsewhere"):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1)
                                  + tuple(st)[3:])
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    guard = MountGuard(home_dirs=[])
    root_dev = os.stat(tmp_path).st_dev
    kind, payload, subs = scan_dir_once(
        str(tmp_path / "elsewhere"), {}, IgnoreRules([]), guard,
        root_dev=root_dev)
    assert (kind, payload, subs) == (None, None, [])
    # the same directory on the SAME device is scanned normally
    kind, _p, _s = scan_dir_once(str(tmp_path), {}, IgnoreRules([]), guard,
                                 root_dev=root_dev)
    assert kind == "s"
