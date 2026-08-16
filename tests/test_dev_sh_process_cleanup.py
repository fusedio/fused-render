"""Guards on scripts/dev.sh's process cleanup: kill_tree, the pidfile, the traps.

dev.sh used to leak whole process trees. Three separate holes, each observed on
a real machine (eleven orphans hand-killed, some 10-20 days old):

  * the traps killed `$!` of a *subshell* — `(cd frontend && npm run watch) &` —
    so `npm` and the `vite build --watch` node under it reparented to init and
    kept rebuilding into shell-dist/ forever;
  * the watchfiles-supervised server ran in the FOREGROUND and was in no trap at
    all, and bash defers a trap handler until the current foreground command
    returns, so `kill <dev.sh>` was queued and did nothing;
  * nothing detected an already-running dev.sh for the same worktree, so one
    worktree accumulated two complete dev.sh trees writing the same shell-dist/.

The shell is the only source of truth for all three, so these tests drive the
real functions out of the script: `FUSED_RENDER_DEV_SH_LIB=1 source dev.sh`
defines the helpers and returns before dev.sh does any work, which is what makes
`kill_tree` and the stale-pidfile decision testable without booting a server.
"""
import os
import re
import subprocess
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEV_SH = os.path.join(_ROOT, "scripts", "dev.sh")


def _script():
    with open(_DEV_SH, encoding="utf-8") as f:
        return f.read()


def _run_lib(snippet, env=None, timeout=60):
    """Run `snippet` with dev.sh's helper functions sourced (library mode)."""
    full_env = dict(os.environ)
    full_env["FUSED_RENDER_DEV_SH_LIB"] = "1"
    # Deterministic ref unless the test asks for another one: an unset var would
    # make dev.sh read the checkout's real branch and the assertions would move
    # with whatever branch the suite runs on.
    full_env.setdefault("FUSED_RENDER_BRANCH", "test-branch")
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source {_DEV_SH!r}\n{snippet}'],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
        close_fds=False,
    )


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie still answers signal 0; ps is what distinguishes "gone" from
    # "waiting to be reaped", and only the former is what kill_tree promises.
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


# --------------------------------------------------------------------------
# kill_tree
# --------------------------------------------------------------------------

def test_kill_tree_reaps_every_descendant_not_just_the_direct_child():
    """The orphan-vite bug in one assertion.

    `(...) &` gives back the SUBSHELL's pid; the real work (npm -> node vite)
    hangs two levels below it. Killing only the pid bash handed you leaves that
    work running under init. kill_tree must walk down with `pgrep -P` and kill
    depth-first, so nothing reparents mid-kill.
    """
    # bash -c 'sleep 300 & sleep 300' -> a subshell whose children are two sleeps.
    proc = subprocess.Popen(
        ["bash", "-c", "bash -c 'sleep 300 & sleep 300' & wait"], close_fds=False
    )
    try:
        # Let the whole tree materialize before snapshotting it.
        descendants = []
        for _ in range(50):
            descendants = _descendants(proc.pid)
            if len(descendants) >= 3:
                break
            time.sleep(0.1)
        assert len(descendants) >= 3, f"tree never formed: {descendants}"

        res = _run_lib(f"kill_tree_hard {proc.pid}")
        assert res.returncode == 0, res.stderr

        leftover = list(descendants)
        deadline = time.time() + 10
        while time.time() < deadline:
            leftover = [p for p in descendants if _alive(p)]
            if not leftover:
                break
            time.sleep(0.1)
        proc.wait(timeout=10)
        assert not leftover, f"descendants survived kill_tree: {leftover}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_kill_tree_escalates_to_sigkill_for_a_term_ignoring_process():
    """A child that traps TERM must not be able to outlive dev.sh.

    Bounded grace, then KILL — never an unbounded wait, which would hang the
    Ctrl-C the developer just pressed.
    """
    proc = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; sleep 300"], close_fds=False
    )
    try:
        time.sleep(0.3)
        res = _run_lib(f"kill_tree_hard {proc.pid}", timeout=30)
        assert res.returncode == 0, res.stderr
        proc.wait(timeout=10)
        assert not _alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _descendants(pid):
    out = []
    stack = [pid]
    while stack:
        cur = stack.pop()
        kids = subprocess.run(
            ["pgrep", "-P", str(cur)], capture_output=True, text=True
        ).stdout.split()
        for k in kids:
            out.append(int(k))
            stack.append(int(k))
    return out


# --------------------------------------------------------------------------
# pidfile path derivation
# --------------------------------------------------------------------------

def test_pidfile_is_per_worktree_and_keyed_on_the_sanitized_branch():
    """Same key the port and state dir already use (_branch.sanitize).

    A second sanitizer would let the pidfile name and the port drift apart, and
    the whole point of keying on the branch is that one worktree's entry can
    never be read as another's.
    """
    res = _run_lib(
        'dev_pidfile_path', env={"FUSED_RENDER_BRANCH": "feature/Some_Branch"}
    )
    assert res.returncode == 0, res.stderr
    path = res.stdout.strip()
    assert path == os.path.join(_ROOT, ".dev-pids", "feature-some"), path


def test_pidfile_baseline_ref_still_gets_a_filename():
    """main/master sanitize to "" (baseline); an empty filename is not a path."""
    res = _run_lib("dev_pidfile_path", env={"FUSED_RENDER_BRANCH": "main"})
    assert res.returncode == 0, res.stderr
    path = res.stdout.strip()
    assert os.path.dirname(path) == os.path.join(_ROOT, ".dev-pids")
    assert os.path.basename(path), "baseline produced an empty pidfile name"


# --------------------------------------------------------------------------
# stale vs live
# --------------------------------------------------------------------------

def test_a_dead_pid_is_stale():
    """The common case: the machine rebooted, or dev.sh was SIGKILLed."""
    proc = subprocess.Popen(["sleep", "0.01"], close_fds=False)
    proc.wait(timeout=10)
    res = _run_lib(
        f'dev_pidfile_is_ours {proc.pid} "" {_ROOT!r} && echo OURS || echo STALE'
    )
    assert res.stdout.strip() == "STALE", res.stdout + res.stderr


def test_a_live_but_unrelated_pid_is_stale():
    """PID reuse must never turn cleanup into killing someone else's process.

    Other worktrees on this machine run their own servers, and at least one
    known orphan is deliberately left alone; a recycled pid landing on any of
    them must read as stale, not as "our dev.sh".
    """
    proc = subprocess.Popen(["sleep", "60"], close_fds=False)
    try:
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} "" {_ROOT!r} && echo OURS || echo STALE'
        )
        assert res.stdout.strip() == "STALE", res.stdout + res.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_live_dev_sh_for_this_worktree_is_recognized(tmp_path):
    """The positive case, or the reap would silently never fire.

    Stands in a throwaway script *named* dev.sh for the real thing — booting a
    server here would be both slow and a way to leave processes behind. What is
    under test is the identification (command line + start time + recorded
    root), and that sees only what `ps` reports.
    """
    stub = tmp_path / "dev.sh"
    stub.write_text("#!/usr/bin/env bash\nsleep 60\n")
    proc = subprocess.Popen(["bash", str(stub)], close_fds=False)
    try:
        time.sleep(0.5)
        start = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(proc.pid)],
            capture_output=True,
            text=True,
        ).stdout.strip()
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {start!r} {_ROOT!r} && echo OURS || echo STALE'
        )
        assert res.stdout.strip() == "OURS", res.stdout + res.stderr

        # ...and the same pid with a start time from a different process (i.e. a
        # recycled pid) must not be.
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} "Mon Jan  1 00:00:00 2001" {_ROOT!r}'
            " && echo OURS || echo STALE"
        )
        assert res.stdout.strip() == "STALE", res.stdout + res.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_pidfile_from_another_worktree_root_is_stale():
    """Defence in depth behind the per-worktree path: the root is recorded too."""
    proc = subprocess.Popen(["sleep", "60"], close_fds=False)
    try:
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} "" "/some/other/worktree"'
            " && echo OURS || echo STALE"
        )
        assert res.stdout.strip() == "STALE", res.stdout + res.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------
# structural guards
# --------------------------------------------------------------------------

def test_cleanup_flag_is_consumed_and_never_forwarded_to_the_server():
    """`--cleanup` is dev.sh's own flag; cli.py would reject it.

    It has to be stripped from `$@` before the passthrough loops build the
    watchfiles command / the single-launch argv.
    """
    src = _script()
    assert "--cleanup" in src
    # The strip has to happen before either passthrough site uses "$@".
    strip_at = src.index("--cleanup")
    for marker in ('for a in "$@"; do CMD+=', '-m fused_render.cli "$@"'):
        assert strip_at < src.index(marker), f"--cleanup stripped after {marker}"


def test_no_broad_pattern_kill():
    """Never `pkill -f vite` / `pkill -f dev.sh`.

    Other worktrees on this machine run their own dev servers; a pattern kill
    would take them out along with ours. Only the pidfile's own tree is fair
    game.
    """
    src = _script()
    assert not re.search(r"pkill\s+-f", src)
    assert not re.search(r"killall", src)


def test_shutdown_is_a_single_handler_reaping_every_background_pid():
    """One handler, so the two trap sites cannot drift apart again.

    The old script had two `trap '...' EXIT INT TERM` lines with different pid
    lists, and neither mentioned the server at all.
    """
    src = _script()
    assert "dev_shutdown" in src
    handler = src[src.index("dev_shutdown() {") : src.index("dev_shutdown() {") + 2500]
    for var in ("WATCH_PID", "CORE_WATCH_PID", "OPENER_PID", "SERVER_PID"):
        assert var in handler, f"{var} is not reaped by dev_shutdown"
    # Every trap installs the same handler.
    traps = re.findall(r"^\s*trap\s+'([^']*)'\s+EXIT", src, re.M)
    assert traps, "no EXIT trap"
    for body in traps:
        assert "dev_shutdown" in body, body


def test_the_server_is_backgrounded_and_waited_on_in_both_branches():
    """A FOREGROUND child defers the trap; `wait` is interruptible.

    Both the reload path (watchfiles) and FUSED_RENDER_NO_RELOAD=1 (plain cli)
    must background + wait, or `kill <dev.sh>` queues a handler that only runs
    once the child happens to exit on its own.
    """
    src = _script()
    for launch in ("-m watchfiles", "-m fused_render.cli"):
        idx = src.rindex(launch)
        tail = src[idx : idx + 400]
        assert "SERVER_PID=$!" in tail, f"{launch} is not backgrounded"
        assert 'wait "$SERVER_PID"' in tail, f"{launch} is not waited on"


def test_dev_pids_dir_is_gitignored():
    with open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8") as f:
        assert ".dev-pids/" in f.read().split("\n")
