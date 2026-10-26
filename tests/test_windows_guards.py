"""POSIX-only calls on the serve/open path degrade instead of raising when the
platform lacks them (Windows): each test removes the call or patches
`sys.platform` and checks the code takes its fallback."""
import os
import subprocess
import sys

import pytest

from fused_render import claude_health, copilot_health
from fused_render.claude_config import lib as claude_lib


@pytest.mark.parametrize("mod", [claude_health, copilot_health])
def test_login_shell_probe_is_skipped_on_windows(monkeypatch, mod):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("spawned a login shell on Windows"))
    assert mod._shell_probe() is None


def test_reveal_uses_startfile_on_windows(monkeypatch):
    opened = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    assert claude_lib.reveal("C:\\x") is True
    assert opened == ["C:\\x"]


def test_user_name_falls_back_to_the_uid_without_pwd(monkeypatch):
    from fused_render.sysmon import common
    monkeypatch.setitem(sys.modules, "pwd", None)
    monkeypatch.setattr(common, "_USERS", {})
    assert common.user_name(4242) == "4242"


def test_euid_is_zero_without_geteuid(monkeypatch):
    from fused_render import _platform
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert _platform.euid() == 0


def test_claude_cmd_log_root_needs_no_geteuid(monkeypatch):
    from fused_render import claude_cmd_log
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert claude_cmd_log.root().endswith(os.path.join("fused_render_claude-0", "claude-cmds"))


def test_disk_space_check_reads_shutil_not_statvfs(monkeypatch, tmp_path):
    import shutil
    from collections import namedtuple
    from fused_render.update import _manager
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10, 10, 0))
    monkeypatch.delattr(os, "statvfs", raising=False)
    mgr = _manager.UpdateManager.__new__(_manager.UpdateManager)
    with pytest.raises(RuntimeError, match="not enough free disk space"):
        mgr._check_disk_space(str(tmp_path))


def test_terminal_sessions_need_pywinpty_on_windows(monkeypatch):
    from fused_render import _platform, pty_session
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "winpty", None)
    with pytest.raises(RuntimeError, match="needs pywinpty"):
        pty_session.PtySessionRegistry().spawn(None)


def test_legacy_mount_sweep_is_skipped_on_windows(monkeypatch):
    from fused_render import _platform, legacy_mounts_cleanup
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(legacy_mounts_cleanup.threading, "Thread",
                        lambda *a, **k: pytest.fail("started the sweep on Windows"))
    legacy_mounts_cleanup.start_background()


def test_venv_python_follows_the_platform_layout(monkeypatch):
    from fused_render import _platform
    monkeypatch.setattr(_platform._mod, "IS_WINDOWS", False)
    assert _platform.venv_python("/v") == os.path.join("/v", "bin", "python")
    monkeypatch.setattr(_platform._mod, "IS_WINDOWS", True)
    assert _platform.venv_python("/v") == os.path.join("/v", "Scripts", "python.exe")


def test_envinstall_venv_python_uses_the_platform_layout(monkeypatch):
    from fused_render import _platform, envinstall
    monkeypatch.setattr(_platform._mod, "IS_WINDOWS", True)
    assert envinstall._venv_python("/v") == os.path.join("/v", "Scripts", "python.exe")


def test_env_install_worker_venv_python_uses_the_platform_layout(monkeypatch):
    from fused_render import _env_install_worker as worker
    monkeypatch.setattr(sys, "platform", "win32")
    assert worker._venv_python("/v") == os.path.join("/v", "Scripts", "python.exe")
    monkeypatch.setattr(sys, "platform", "linux")
    assert worker._venv_python("/v") == os.path.join("/v", "bin", "python")


def test_map_drops_root_needs_no_geteuid(monkeypatch):
    from fused_render.templates.map import discover
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert "fused_render_map-0" in discover._drops_root()


def test_require_private_skips_the_ownership_check_without_geteuid(monkeypatch, tmp_path):
    from fused_render.templates.shared import private_dir
    d = tmp_path / "p"
    d.mkdir(mode=0o700)
    monkeypatch.delattr(os, "geteuid", raising=False)
    private_dir.require_private(str(d))


def _fake_windows_lstat(monkeypatch):
    real = os.lstat

    class _St:
        def __init__(self, st):
            self.st_mode = 0o40777
            self.st_uid = 0

    monkeypatch.delattr(os, "geteuid", raising=False)
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: _St(real(p, *a, **k)))


def test_require_private_ignores_mode_bits_on_windows(monkeypatch, tmp_path):
    from fused_render.templates.shared import private_dir
    d = tmp_path / "p"
    d.mkdir()
    _fake_windows_lstat(monkeypatch)
    private_dir.require_private(str(d))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_require_private_still_refuses_a_world_writable_dir_on_posix(tmp_path):
    from fused_render.templates.shared import private_dir
    d = tmp_path / "p"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(PermissionError):
        private_dir.require_private(str(d))


def test_chat_run_dir_and_perm_dir_pass_on_windows(monkeypatch, tmp_path):
    from fused_render.claude_agent import agent
    root = tmp_path / "fused_render_claude-0"
    monkeypatch.setattr(agent, "RUNS", str(root / "runs"))
    run_dir = os.path.join(agent.RUNS, "abc")
    agent._private_dir(run_dir)
    _fake_windows_lstat(monkeypatch)
    agent._private_dir(agent._perm_dir(run_dir))


def test_imessage_channel_imports_without_fcntl(monkeypatch):
    import importlib

    monkeypatch.setitem(sys.modules, "fcntl", None)
    sys.modules.pop("fused_render.bots.channels.imessage", None)
    try:
        mod = importlib.import_module("fused_render.bots.channels.imessage")
        assert hasattr(mod, "ImessageChannel")
    finally:
        sys.modules.pop("fused_render.bots.channels.imessage", None)
