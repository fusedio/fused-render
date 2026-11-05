"""connect/applescript.py: the `local-app` kind's runner and consent probe, against a
fake `osascript` (a shell script on a temp PATH whose behaviour an env var picks).
Nothing here needs a Mac: `sys.platform` is patched to "darwin" for the runs."""
import json
import os
import stat
import subprocess

import pytest

from fused_render.connect import applescript

FAKE = r'''#!/bin/sh
# argv: -l JavaScript - <args...>; the script arrives on stdin.
script=$(cat)
shift 3
case "$FAKE_OSA" in
  ok)      printf '{"name":"Notes","account":"iCloud","argv":%s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")" ;;
  denied)  echo "execution error: Not authorized to send Apple events to Notes. (-1743)" >&2; exit 1 ;;
  gone)    echo "execution error: Notes got an error: Application isn't running. (-600)" >&2; exit 1 ;;
  junk)    echo "not json" ;;
  errobj)  echo '{"error":"no folder named X"}' ;;
  errmore) echo '{"error":"2 notes match title Milk; pass the id of one","candidates":[{"id":"p1"},{"id":"p2"}]}' ;;
  slow)    sleep 5; echo '{}' ;;
  *)       echo "execution error: boom (-1700)" >&2; exit 1 ;;
esac
'''


@pytest.fixture()
def mac(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    osa = bindir / "osascript"
    osa.write_text(FAKE)
    osa.chmod(osa.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setenv("FAKE_OSA", "ok")
    return monkeypatch


def test_available_is_a_mac_with_osascript(monkeypatch, tmp_path):
    monkeypatch.setattr(applescript.sys, "platform", "linux")
    ok, why = applescript.available()
    assert not ok and "Mac" in why
    assert applescript.available("win32")[0] is False
    monkeypatch.setattr(applescript.sys, "platform", "darwin")
    monkeypatch.setenv("PATH", str(tmp_path))   # no osascript here
    ok, why = applescript.available()
    assert not ok and "osascript" in why


def test_run_jxa_passes_args_on_argv_and_parses_json(mac):
    out = applescript.run_jxa("function run(argv) {}", [json.dumps({"q": "x"}), 7], app="Notes")
    assert out["name"] == "Notes" and out["argv"] == ['{"q": "x"}', "7"]


def test_run_jxa_off_a_mac_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(applescript.sys, "platform", "linux")
    with pytest.raises(applescript.AutomationError, match="Mac"):
        applescript.run_jxa("x", [], app="Notes")


@pytest.mark.parametrize("mode, exc, text", [
    ("denied", applescript.AutomationDenied, "Privacy & Security > Automation"),
    ("gone", applescript.AutomationError, "could not be launched"),
    ("junk", applescript.AutomationError, "not JSON"),
    ("errobj", applescript.AutomationError, "no folder named X"),
    ("other", applescript.AutomationError, "boom"),
])
def test_run_jxa_classifies_failures(mac, mode, exc, text):
    mac.setenv("FAKE_OSA", mode)
    with pytest.raises(exc, match=text):
        applescript.run_jxa("x", [], app="Notes")


def test_an_error_with_candidates_is_the_answer_not_a_raise(mac):
    """Notes' append on an ambiguous title answers error + candidates: the ids the
    message tells the caller to pass must reach them."""
    mac.setenv("FAKE_OSA", "errmore")
    out = applescript.run_jxa("x", [], app="Notes")
    assert out["error"].startswith("2 notes match") and [c["id"] for c in out["candidates"]] == ["p1", "p2"]


def test_run_jxa_timeout_names_the_app(mac):
    mac.setenv("FAKE_OSA", "slow")
    with pytest.raises(applescript.AutomationError, match="Notes did not answer within 1 s"):
        applescript.run_jxa("x", [], app="Notes", timeout=1)


def test_probe_returns_name_and_account(mac):
    p = applescript.Probe("Notes", "app.defaultAccount().name()")
    p.start()
    out = p.result(timeout=10)
    assert out["name"] == "Notes" and out["account"] == "iCloud"
    assert out["argv"] == ["Notes", "app.defaultAccount().name()"]   # the expression rides on argv, never in the script


def test_probe_denied_says_where_to_allow(mac):
    mac.setenv("FAKE_OSA", "denied")
    p = applescript.Probe("Calendar")
    p.start()
    with pytest.raises(applescript.AutomationDenied, match="control Calendar"):
        p.result(timeout=10)


def test_probe_cancel_kills_the_process(mac):
    mac.setenv("FAKE_OSA", "slow")
    p = applescript.Probe("Notes")
    p.start()
    p.cancel()
    with pytest.raises(applescript.AutomationError):
        p.result(timeout=10)
    assert p._proc.poll() is not None


def test_probe_timeout_mentions_the_prompt(mac):
    mac.setenv("FAKE_OSA", "slow")
    p = applescript.Probe("Notes")
    p.start()
    with pytest.raises(applescript.AutomationError, match="click OK"):
        p.result(timeout=1)


def test_probe_off_a_mac_refuses_to_start(monkeypatch):
    monkeypatch.setattr(applescript.sys, "platform", "linux")
    with pytest.raises(applescript.AutomationError):
        applescript.Probe("Notes").start()
    assert not isinstance(subprocess, int)
