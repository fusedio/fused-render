"""`_rclone_state` must not report "rclone not installed" while a live rcd
daemon is actively serving mounts.

On a fresh server launch the direct probe (`shutil.which` + `rclone version`)
can transiently fail (binary momentarily unresolved on PATH, or the version
subprocess hiccups), which flipped the Mounts page to a spurious "rclone not
found" banner even though an already-running rcd was happily serving mounts —
cleared only by bouncing the process. A live daemon is itself proof rclone
works, so the state now falls back to asking the daemon (core/version +
config/listremotes) rather than reporting unavailable.
"""

import fused_render.shell.mounts as mounts_mod


def _neuter_labeling(monkeypatch):
    # _rclone_state_view does credential/label I/O we don't care about here;
    # pin it to trivial values so the test asserts only availability + shape.
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [])
    monkeypatch.setattr(mounts_mod, "_rclone_config_dump", lambda b: {})
    monkeypatch.setattr(mounts_mod, "_suggestions_view", lambda names: [])


def test_daemon_vouches_when_direct_probe_fails(monkeypatch):
    _neuter_labeling(monkeypatch)
    # Binary won't resolve on PATH (the transient failure).
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    # ...but a live daemon answers.
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 4242)

    def fake_rc(port, method, *a, **k):
        assert port == 4242
        if method == "core/version":
            return {"version": "v1.74.4"}
        if method == "config/listremotes":
            return {"remotes": ["aws-open", "aws"]}
        raise AssertionError(f"unexpected rc method {method}")

    monkeypatch.setattr(mounts_mod, "_rc", fake_rc)

    state = mounts_mod._rclone_state()
    assert state["available"] is True
    assert state["version"] == "v1.74.4"
    # rc gives bare names; the view must carry the trailing ':' mount-base form.
    assert [r["name"] for r in state["remotes"]] == ["aws-open:", "aws:"]


def test_unavailable_when_no_binary_and_no_daemon(monkeypatch):
    _neuter_labeling(monkeypatch)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)
    state = mounts_mod._rclone_state()
    assert state == {"available": False, "version": None, "remotes": [], "suggested": []}


def test_daemon_alive_but_rc_errors_still_available(monkeypatch):
    _neuter_labeling(monkeypatch)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 4242)

    def boom(port, method, *a, **k):
        raise RuntimeError("rc down mid-answer")

    monkeypatch.setattr(mounts_mod, "_rc", boom)
    state = mounts_mod._rclone_state()
    # A live daemon proves rclone works even if BOTH follow-up rc calls fail.
    assert state["available"] is True
    assert state["version"] is None
    assert state["remotes"] == []


def test_partial_rc_success_keeps_version_when_listremotes_fails(monkeypatch):
    _neuter_labeling(monkeypatch)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 4242)

    def half(port, method, *a, **k):
        if method == "core/version":
            return {"version": "v1.74.4"}
        raise RuntimeError("listremotes down")

    monkeypatch.setattr(mounts_mod, "_rc", half)
    state = mounts_mod._rclone_state()
    # version succeeded, listremotes failed — the good version must survive.
    assert state["available"] is True
    assert state["version"] == "v1.74.4"
    assert state["remotes"] == []


def test_partial_rc_success_keeps_remotes_when_version_fails(monkeypatch):
    _neuter_labeling(monkeypatch)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 4242)

    def half(port, method, *a, **k):
        if method == "config/listremotes":
            return {"remotes": ["aws-open"]}
        raise RuntimeError("core/version down")

    monkeypatch.setattr(mounts_mod, "_rc", half)
    state = mounts_mod._rclone_state()
    # version failed, listremotes succeeded — the remotes must survive.
    assert state["available"] is True
    assert state["version"] is None
    assert [r["name"] for r in state["remotes"]] == ["aws-open:"]


def test_direct_probe_success_skips_daemon(monkeypatch):
    _neuter_labeling(monkeypatch)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")

    class _Run:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(cmd, *a, **k):
        if cmd[1] == "version":
            return _Run("rclone v1.74.4\n- os/version: darwin\n")
        if cmd[1] == "listremotes":
            return _Run("aws-open:\nmyminio:\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)

    def no_daemon(*a, **k):
        raise AssertionError("_live_rcd_port must not be consulted on probe success")

    monkeypatch.setattr(mounts_mod, "_live_rcd_port", no_daemon)

    state = mounts_mod._rclone_state()
    assert state["available"] is True
    assert state["version"] == "rclone v1.74.4"
    assert [r["name"] for r in state["remotes"]] == ["aws-open:", "myminio:"]
