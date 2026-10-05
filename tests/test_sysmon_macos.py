"""The macOS readers against this very process — sane ranges only."""
import getpass
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def test_proc_sample_and_host_memory_are_sane():
    from fused_render.sysmon import macos

    s = macos.proc_sample(os.getpid())
    assert s is not None
    assert 1 << 20 < s.footprint < 64 << 30
    assert s.cpu_ns > 0

    mem = macos.host_memory()
    assert mem is not None
    assert 1 << 30 <= mem["total"] <= 4 << 40
    assert 0 < mem["used"] <= mem["total"]

    assert abs(macos.start_time(os.getpid()) - time.time()) < 86400 * 30
    assert macos.parent_pid(os.getpid()) == os.getppid()
    assert macos.proc_args(os.getpid())[0]
    ticks = macos.host_cpu_ticks()
    assert ticks is not None and ticks[2] >= ticks[0] + ticks[1]


def test_list_procs_sees_this_process_and_other_users():
    from fused_render.sysmon import macos
    from fused_render.sysmon.common import user_name

    procs = {p.pid: p for p in macos.list_procs()}
    assert os.getpid() in macos.list_pids()
    me = procs[os.getpid()]
    assert me.ppid == os.getppid()
    assert user_name(me.uid) == getpass.getuser()
    assert abs(me.start - macos.start_time(os.getpid())) < 0.01
    assert me.name and not me.zombie
    # launchd is root's, and still listed.
    assert user_name(procs[1].uid) == "root"
    assert macos.proc_ident(os.getpid()) == me
    assert macos.proc_ident(1).uid == 0
