import os

from fused_render import login_path


def _fake_shell(tmp_path, body):
    shell = tmp_path / "fake-shell"
    shell.write_text("#!/bin/sh\n" + body)
    shell.chmod(0o755)
    return str(shell)


def test_merge_order_and_idempotent():
    login = "/Users/me/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    merged = login_path.merge("/app/bin:/usr/bin:/bin:/usr/sbin:/sbin", login)
    assert merged == f"/app/bin:{login}"
    assert login_path.merge(merged, login) == merged
    assert os.path.expanduser("~/.local/bin") in login_path.merge("/usr/bin", "").split(os.pathsep)


def test_probe_reads_the_path_after_rc_noise_without_bundle_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/bundle/python")
    shell = _fake_shell(tmp_path, "echo 'welcome back'\necho __FUSED_RENDER_LOGIN_PATH__\n"
                                  'echo "/x/bin${PYTHONHOME:+:leaked}"\n')
    assert login_path.probe(shell) == "/x/bin"


def test_probe_keeps_the_path_printed_before_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(login_path, "_TIMEOUT_S", 1)
    shell = _fake_shell(tmp_path, "echo __FUSED_RENDER_LOGIN_PATH__\necho /x/bin\nsleep 30\n")
    assert login_path.probe(shell) == "/x/bin"
