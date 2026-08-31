"""The fused runner's hardcoded per-call scratch root, `/tmp/exec` (D626).

`fused`'s handler — the same file the local backend spawns — opens every call
with `os.makedirs(f"/tmp/exec/{uuid4()}")`. That path is a literal with no env
var and no argument, and on a desktop it is machine-wide: whichever account runs
first creates it under its own umask, and every other account on the box then
gets `PermissionError: [Errno 13] Permission denied: '/tmp/exec/<uuid>'` — raised
in the CHILD, so it arrives as the run's error and the page's overlay shows it as
if the user's own script raised it (reported from a fresh macOS profile).

`engine.exec_root_blocked()` is the guard: it creates/repairs the root so no
account can lock the others out, and reports the cases it cannot fix so
`prefs.effective_engine` degrades to builtin instead of the app showing a
traceback where the page should be.

EXEC_ROOT is redirected to a tmp dir for every test here — these assert on
modes and owners, and a test that touched the real /tmp/exec would either fight
the developer's own app or leave a directory behind.
"""
import asyncio
import os
import stat

import pytest

from fused_render import engine
from fused_render.shell import prefs as prefs_mod

# os.access always says yes for root, so a chmod-based gate cannot trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="permission bits are ignored when running as root")

requires_posix = pytest.mark.skipif(
    os.name == "nt", reason="POSIX owner/mode model (Windows takes the other branch)")

#: The shipped constant, captured before the autouse fixture below redirects it
#: for every test. Only the parity test at the bottom wants this — it is about
#: the value the app actually runs with, not the redirect.
REAL_EXEC_ROOT = engine.EXEC_ROOT


@pytest.fixture(autouse=True)
def _redirected_exec_root(tmp_path, monkeypatch):
    """Point the guard at a throwaway root and clear its log-transition state."""
    root = tmp_path / "exec"
    monkeypatch.setattr(engine, "EXEC_ROOT", str(root))
    # `""` is the module's "nothing logged yet" — a previous test's reason left
    # here would suppress the warning this one is asserting on.
    monkeypatch.setattr(engine, "_exec_root_logged", "")
    return root


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# -- creating and repairing the root -------------------------------------------


@requires_posix
def test_a_missing_root_is_created_world_writable_and_sticky(_redirected_exec_root):
    """The prevention half: the account that gets there first must not leave a
    root the next account cannot use. 1777 is /tmp's own mode — everyone may
    create their own call dir, nobody may remove anyone else's."""
    assert engine.exec_root_blocked() is None
    assert _mode(_redirected_exec_root) == 0o1777


@requires_posix
def test_a_root_we_own_is_widened_in_place(_redirected_exec_root, caplog):
    """The half that fixes the OTHER account, and the reason the check is not
    cached: a `/tmp/exec` this app created under an ordinary umask before D626
    (or after a /tmp sweep) is repaired on the next resolve, with no action from
    the account that was locked out."""
    _redirected_exec_root.mkdir()
    os.chmod(_redirected_exec_root, 0o755)
    with caplog.at_level("INFO"):
        assert engine.exec_root_blocked() is None
    assert _mode(_redirected_exec_root) == 0o1777
    assert "widened" in caplog.text


@requires_posix
def test_a_root_already_at_the_target_mode_is_not_rewritten(_redirected_exec_root,
                                                            caplog):
    """Idempotent, which is what makes it safe on the per-request dispatch path:
    the healthy case does no write and logs nothing."""
    _redirected_exec_root.mkdir(mode=0o1777)
    os.chmod(_redirected_exec_root, 0o1777)  # mkdir's mode is umask-masked
    with caplog.at_level("INFO"):
        assert engine.exec_root_blocked() is None
    assert "widened" not in caplog.text


@requires_posix
def test_repeated_calls_do_not_repeat_the_warning(_redirected_exec_root, monkeypatch,
                                                  caplog):
    """Logged on transitions only — `effective_engine` calls this per request, and
    a warning per /api/run would bury the log it is trying to be found in."""
    monkeypatch.setattr(engine, "_exec_root_reason", lambda: "nope")
    with caplog.at_level("WARNING"):
        assert engine.exec_root_blocked() == "nope"
        assert engine.exec_root_blocked() == "nope"
    assert caplog.text.count("cannot run here") == 1


@requires_posix
def test_a_root_that_becomes_usable_again_is_noticed(_redirected_exec_root,
                                                     monkeypatch, caplog):
    """Uncached in both directions: a blocking directory that goes away restores
    the engine without a server restart."""
    reasons = iter(["nope", None])
    monkeypatch.setattr(engine, "_exec_root_reason", lambda: next(reasons))
    with caplog.at_level("INFO"):
        assert engine.exec_root_blocked() == "nope"
        assert engine.exec_root_blocked() is None
    assert "usable again" in caplog.text


# -- the cases it cannot fix ---------------------------------------------------


@requires_posix
def test_a_symlink_is_refused_and_never_followed(tmp_path, _redirected_exec_root):
    """The security property, not a convenience: /tmp is world-writable, so any
    local account can plant `/tmp/exec` as a symlink. A path-based chmod would
    then apply 1777 to its TARGET — `/tmp/exec -> ~/.ssh` — and the owner check
    would not catch it, because the target being ours is the attack. The guard
    opens O_NOFOLLOW and refuses instead."""
    target = tmp_path / "private"
    target.mkdir()
    os.chmod(target, 0o700)
    os.symlink(target, _redirected_exec_root)

    reason = engine.exec_root_blocked()
    assert reason is not None and "symbolic link" in reason
    assert _mode(target) == 0o700


@requires_posix
def test_a_file_where_the_root_should_be_is_reported_not_crashed(_redirected_exec_root):
    _redirected_exec_root.write_text("not a directory")
    reason = engine.exec_root_blocked()
    assert reason is not None and "not a directory" in reason


@requires_posix
@skip_root
def test_another_accounts_unwritable_root_blocks_the_engine(_redirected_exec_root,
                                                            monkeypatch, caplog):
    """The reported bug, from the locked-out account's side: the root exists, it
    is not ours, and we cannot create our call dir in it. There is nothing to
    fix — chmod and rmdir both need to be the owner — so it must be reported,
    not attempted."""
    _redirected_exec_root.mkdir()
    os.chmod(_redirected_exec_root, 0o555)
    # Somebody else's: the widen must not fire, and `os.access` then answers for
    # real. Faked rather than staged with a second account, which a test cannot
    # create.
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(_redirected_exec_root).st_uid + 1)

    with caplog.at_level("WARNING"):
        reason = engine.exec_root_blocked()
    assert reason is not None
    assert "cannot create a directory inside" in reason
    assert "mode 0555" in reason              # names what is wrong
    assert _mode(_redirected_exec_root) == 0o555   # and changed nothing
    # The log carries the remedy, since the reason alone reads as a dead end.
    assert "reboot clears /tmp" in caplog.text


@requires_posix
def test_an_unwritable_root_is_reported_whoever_is_running(_redirected_exec_root,
                                                           monkeypatch, caplog):
    """The same verdict as the test above, reached without depending on the
    permission bits actually biting — as root they never do, and CI runs as root,
    so the branch this entire fix hangs on would otherwise be covered nowhere."""
    _redirected_exec_root.mkdir()
    os.chmod(_redirected_exec_root, 0o555)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(_redirected_exec_root).st_uid + 1)
    monkeypatch.setattr(os, "access", lambda *a, **kw: False)

    with caplog.at_level("WARNING"):
        reason = engine.exec_root_blocked()
    assert reason is not None
    assert "cannot create a directory inside" in reason
    assert "mode 0555" in reason
    assert _mode(_redirected_exec_root) == 0o555
    assert "reboot clears /tmp" in caplog.text


# -- what the app does about it ------------------------------------------------


def test_dispatch_degrades_to_builtin_while_the_root_is_blocked(monkeypatch):
    """The fallback happens at DISPATCH, not inside a run, so the engine the
    Preferences page and the Calls log name stays the engine that actually
    executed the page (SPEC §20.2's one-resolver rule)."""
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    monkeypatch.setattr(engine, "exec_root_blocked", lambda: None)
    assert prefs_mod.effective_engine() == "fused"

    monkeypatch.setattr(engine, "exec_root_blocked", lambda: "/tmp/exec is not ours")
    assert prefs_mod.effective_engine() == "builtin"


def test_an_override_degrades_too(monkeypatch):
    """`=fused` and `=auto` both go through the same resolve: the override picks
    the engine, it does not make an unusable one work."""
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    monkeypatch.setattr(engine, "exec_root_blocked", lambda: "blocked")
    for value in ("fused", "auto"):
        monkeypatch.setenv("FUSED_RENDER_ENGINE", value)
        assert prefs_mod.effective_engine() == "builtin"


def test_prefs_report_the_reason_separately_from_availability(monkeypatch):
    """Two fields, one cause each: an installed-but-blocked engine must not be
    reported as a missing package, which is the only thing `fused_available`
    ever meant."""
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: True)
    monkeypatch.setattr(engine, "exec_root_blocked", lambda: "blocked because reasons")
    state = prefs_mod.engine_state()
    assert state["fused_available"] is True
    assert state["effective"] == "builtin"
    assert state["blocked_reason"] == "blocked because reasons"


def test_no_reason_is_reported_when_the_package_itself_is_missing(monkeypatch):
    """`blocked_reason` answers "why is an INSTALLED engine not running"; with no
    package there is nothing to block, and probing the filesystem to say so
    would create the root on a machine that will never use it."""
    monkeypatch.setattr(prefs_mod, "fused_engine_available", lambda: False)
    monkeypatch.setattr(engine, "exec_root_blocked",
                        lambda: pytest.fail("must not be probed"))
    assert prefs_mod.engine_state()["blocked_reason"] is None


def test_warm_repairs_the_root_off_the_request_path(_redirected_exec_root,
                                                    monkeypatch):
    """The repair is what unblocks a second account, so it runs at startup rather
    than waiting for that account to open a page."""
    monkeypatch.setattr(engine, "available", lambda: True)
    engine.warm()
    assert _redirected_exec_root.is_dir()


class _Result:
    """The one field `run_python` reads before it decides (see engine._execute)."""

    def __init__(self, error):
        self.error = error
        self.return_value = None
        self.stdout = ""
        self.stderr = ""
        self.duration_ms = 1
        self.response = None


def test_a_child_side_denial_is_translated_not_shown_as_the_scripts(
        tmp_path, monkeypatch, _redirected_exec_root):
    """The residual race the pre-flight cannot close (the root was fine at
    dispatch and gone by the child's makedirs). Untranslated, `_split_error`
    reads `PermissionError` off the child's stderr and the overlay presents it as
    the script's own failure — which is exactly the report."""
    target = tmp_path / "sine.py"
    target.write_text("def main():\n    return 1\n")
    raw = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path}/run.py", line 13, in <module>\n'
        "    result = lambda_handler(event, None)\n"
        f'  File "{tmp_path}/handler.py", line 650, in lambda_handler\n'
        "    os.makedirs(call_dir, exist_ok=True)\n"
        f"PermissionError: [Errno 13] Permission denied: "
        f"'{_redirected_exec_root}/6f1e2d3c'\n"
    )

    async def _execute(*a, **kw):
        return _Result(raw)

    monkeypatch.setattr(engine, "_execute", _execute)
    out = asyncio.run(engine.run_python(str(target), {}))

    assert out["ok"] is False
    assert out["error"]["type"] == "EngineError"
    assert "Nothing is wrong with your script" in out["error"]["message"]
    assert "sine.py" in out["error"]["message"]
    # The child's own traceback is kept, so the cause is still inspectable.
    assert "os.makedirs" in out["error"]["traceback"]


def test_an_ordinary_script_error_is_still_the_scripts(tmp_path, monkeypatch):
    """The translation is keyed on the scratch root AND a denial: a script that
    legitimately raises PermissionError on its own file must keep its error."""
    target = tmp_path / "t.py"
    target.write_text("def main():\n    return 1\n")
    raw = (
        "Traceback (most recent call last):\n"
        f'  File "{target}", line 2, in main\n'
        "    open('/etc/shadow')\n"
        "PermissionError: [Errno 13] Permission denied: '/etc/shadow'\n"
    )

    async def _execute(*a, **kw):
        return _Result(raw)

    monkeypatch.setattr(engine, "_execute", _execute)
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["error"]["type"] == "PermissionError"
    assert "/etc/shadow" in out["error"]["message"]


# -- the literal this whole file is guarding -----------------------------------


def test_exec_root_matches_the_path_the_installed_handler_actually_uses():
    """The gate is only a gate while it names the same directory the runner does.
    Upstream moving that literal would leave every check here passing about a
    directory no run touches — silently, since a working machine looks identical
    either way. Skipped (not passed) when `fused` is absent: there is nothing to
    compare against."""
    pytest.importorskip("fused")
    import re
    from pathlib import Path

    import fused.agent_core.backends.aws.handler.handler as handler_mod

    source = Path(handler_mod.__file__).read_text(encoding="utf-8")
    found = re.findall(r'call_dir\s*=\s*f?"([^"{]*)/?\{?', source)
    assert found, "handler.py no longer assigns call_dir from a literal path"
    assert found[0].rstrip("/") == REAL_EXEC_ROOT, (
        f"the handler now writes call dirs under {found[0]!r}, but "
        f"engine.EXEC_ROOT still guards {REAL_EXEC_ROOT!r}"
    )
