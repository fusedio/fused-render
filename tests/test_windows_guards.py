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
    monkeypatch.delattr(os, "geteuid")
    assert _platform.euid() == 0


def test_claude_cmd_log_root_needs_no_geteuid(monkeypatch):
    from fused_render import claude_cmd_log
    monkeypatch.delattr(os, "geteuid")
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


def test_terminal_sessions_are_refused_on_windows(monkeypatch):
    from fused_render import _platform, pty_session
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    with pytest.raises(RuntimeError, match="not supported on Windows"):
        pty_session.PtySessionRegistry().spawn(None)


def test_legacy_mount_sweep_is_skipped_on_windows(monkeypatch):
    from fused_render import _platform, legacy_mounts_cleanup
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(legacy_mounts_cleanup.threading, "Thread",
                        lambda *a, **k: pytest.fail("started the sweep on Windows"))
    legacy_mounts_cleanup.start_background()
