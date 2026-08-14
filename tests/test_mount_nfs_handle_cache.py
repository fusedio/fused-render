"""The macOS nfsmount NFS handle cache, and the kernel NFS client options.

Background — the defect these pin down. rclone serves the macOS mount over NFSv3
using go-nfs, whose default in-memory handle cache
(helpers/cachinghandler.go, v0.0.4 = the version rclone v1.74.4 pins) scans EVERY
cached handle on every handle resolution, i.e. on essentially every NFS RPC:

    if f, ok := c.activeHandles.Get(id); ok {
        for _, k := range c.activeHandles.Keys() {        // O(N)
            candidate, _ := c.activeHandles.Peek(k)
            if hasPrefix(f.p, candidate.p) { ... }

ToHandle mints one handle per directory entry and READDIRPLUS calls it per entry,
so ANY recursive walk of a mount — a global search across the home dir, ripgrep,
Spotlight, or one flat million-key S3 prefix — drives N toward
--nfs-cache-handle-limit (default 1_000_000). The LRU has no TTL and shrinks only
by eviction at the limit, so the mount never recovers: the long-standing "one
global search in ~/.fused-render kills the mount, and only a force-unmount heals
it" failure.

Measured on a LOCAL 50k-file tree, so no S3 latency was involved and the growth
is attributable to the handle cache alone:

    handles minted     memory ms/stat     disk ms/stat
                 0              0.123            0.169
             5_000              0.398            0.124
            15_000              1.127            0.124
            50_000              3.576            0.120

and re-listing one unchanged 500-entry directory went 80ms -> 1801ms under
"memory" while staying ~63ms under "disk". Extrapolated to the 1M-handle default
limit, "memory" reaches ~70ms per stat — a single 500-entry listing takes ~36s.

So the daemon must run the on-disk handler, whose FromHandle is a sha256 of the
path plus one small file read, with no scan at all.
"""
import os

import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


class _FakePopen:
    calls: list = []

    def __init__(self, argv, **kw):
        type(self).calls.append((argv, kw))


@pytest.fixture
def spawn(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda *a, **k: None)
    monkeypatch.setattr(mounts_mod.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(mounts_mod, "_rc",
                        lambda *a, **k: {"pid": 999})
    return _FakePopen.calls


# ---- the handle cache -------------------------------------------------------


def test_daemon_runs_the_on_disk_handle_cache(home, spawn):
    """The whole point: not the "memory" default, whose per-RPC cost grows
    without bound and never recovers."""
    mounts_mod.ensure_rcd()
    [(_argv, kw)] = spawn
    assert kw["env"]["RCLONE_NFS_CACHE_TYPE"] == "disk"


def test_handle_cache_options_go_in_the_env_not_argv(home, spawn):
    """Regression guard with teeth: --nfs-cache-type/--nfs-cache-dir are
    registered on the `serve nfs`/`nfsmount` COMMAND flag sets, not globally, so
    `rcd` rejects them with "unknown flag" and REFUSES TO START — mounts would
    stop working entirely. They must travel as RCLONE_NFS_* env vars, which
    rclone derives for every registered option (verified against v1.74.4 via
    `rc options/get` reporting {"HandleCache":"disk", ...})."""
    mounts_mod.ensure_rcd()
    [(argv, kw)] = spawn
    assert not [a for a in argv if "nfs-cache" in a], (
        f"rcd would refuse to start with these on argv: {argv}")
    assert kw["env"]["RCLONE_NFS_CACHE_TYPE"] == "disk"
    assert kw["env"]["RCLONE_NFS_CACHE_DIR"]


def test_handle_cache_dir_is_home_scoped_and_exists(home, spawn):
    """Under home_dir() so a pytest temp home or a deleted worktree takes it
    with them, and created up front — rclone is handed a usable path."""
    mounts_mod.ensure_rcd()
    [(_argv, kw)] = spawn
    d = kw["env"]["RCLONE_NFS_CACHE_DIR"]
    assert d == mounts_mod._nfs_handle_cache_dir()
    assert os.path.isdir(d)
    assert os.path.realpath(d).startswith(os.path.realpath(str(home)))


def test_handle_cache_is_reset_on_spawn(home, spawn):
    """The disk handler never evicts — one small file per path ever handled — so
    without a reset a long-lived home accumulates them without bound. A fresh
    daemon cannot honour handles it did not mint anyway (the VFS they referred to
    is gone), so the spawn is the right moment."""
    d = mounts_mod._nfs_handle_cache_dir()
    os.makedirs(d, exist_ok=True)
    stale = os.path.join(d, "deadbeef")
    with open(stale, "w") as fh:
        fh.write("stale handle from a previous daemon")

    mounts_mod.ensure_rcd()

    assert not os.path.exists(stale)
    assert os.path.isdir(d)  # cleared, not merely deleted


def test_reset_survives_an_unwritable_cache_dir(home, monkeypatch):
    """Best-effort: a stale or unremovable cache is a disk-space nuisance, never
    a reason to fail a mount. The path is still returned so the spawn proceeds
    and rclone can create it itself."""
    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(mounts_mod.os, "makedirs", boom)
    assert mounts_mod._reset_nfs_handle_cache() == mounts_mod._nfs_handle_cache_dir()


def test_rc_auth_still_locked_down_alongside_the_nfs_vars(home, spawn):
    """The NFS vars are added to the same env that REPLACES the RCLONE_RC_*
    namespace; adding keys must not have disturbed that lock-down."""
    mounts_mod.ensure_rcd()
    [(_argv, kw)] = spawn
    env = kw["env"]
    assert env["RCLONE_RC_NO_AUTH"] == "false"
    assert env["RCLONE_RC_USER"] == mounts_mod._RCD_RC_USER


# ---- kernel NFS client options ----------------------------------------------


def test_wedged_processes_are_killable():
    """"intr" lets a call stuck on an unresponsive server fail with EINTR when a
    termination signal is posted. Without it (macOS defaults to nointr) a process
    that trips a slow mount is UNKILLABLE — the beachballed ripgrep or editor
    search that previously could only be cleared by force-unmounting."""
    assert "intr" in mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]


def test_locks_are_disabled_because_the_server_has_none():
    """rclone serves NFSv3 through go-nfs, which registers NO NLM program, yet
    macOS mounts with remote locks enabled by default. The mismatch stays latent
    until something takes a real lock — and DuckDB/SQLite opening a file on a
    mount does exactly that. "nolocks" makes those fail fast instead of hanging
    on an absent lockd."""
    assert "nolocks" in mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]


def test_no_retrans_and_no_soft():
    """Two traps, both previously tempting.

    "retrans" is defined by mount_nfs(8) as the retransmit count "for soft
    mounts"; this mount is hard (the macOS default), so retrans=2 was inert and
    read as tuning that was doing nothing.

    "soft" is worse than inert. It converts a stall into a mid-read EIO, which
    DuckDB/rasterio surface as file corruption on a read that was merely slow —
    and on a read-only mount it silently implies deadtimeout=60, force-unmounting
    the mount out from under the app. The stalls it appeared to mitigate were
    caused by the handle cache above, which is fixed at the source."""
    opts = mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]
    assert not [o for o in opts if o.startswith("retrans")]
    assert "soft" not in opts


def test_timeo_is_tenths_of_a_second():
    """600 is 60 seconds, not 600. Pinned so nobody "fixes" the number by
    reading it as seconds."""
    assert "timeo=600" in mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]


@pytest.mark.parametrize("read_only", [True, False])
def test_per_mount_options_keep_the_client_tuning(read_only):
    """_nfs_mount_opt appends "rdonly" for a read-only record; it must not drop
    the transport tuning while doing so."""
    opts = mounts_mod._nfs_mount_opt({"read_only": read_only})["ExtraOptions"]
    for expected in ("timeo=600", "intr", "nolocks", "nobrowse"):
        assert expected in opts
    assert ("rdonly" in opts) is read_only
