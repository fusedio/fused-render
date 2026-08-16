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

def _lstart(pid):
    return subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()


def _argv(pid):
    return subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()


# A stand-in for dev.sh: a script whose *argv* is what the identity check reads.
#
# The trailing `exit 0` and the trap are not decoration. Bash replaces itself
# with the last command of a script when nothing can follow it — verified here:
# `bash -c 'sleep 30'` already shows up as `sleep 30`, with no shell in argv, on
# bash 3.2 — and bash 5.x (every Linux runner) applies that optimization to
# script files too. A stub that was only `sleep 60` therefore became a bare
# `sleep` on Linux, its argv lost the "dev.sh" the check looks for, and two
# tests that should read OURS read STALE. The real dev.sh installs traps and has
# hundreds of commands after any given one, so it is never exec-replaced; the
# stub has to be equally unoptimizable or it is not standing in for anything.
_STUB = "#!/usr/bin/env bash\ntrap 'exit 0' TERM\nsleep 60\nexit 0\n"


def _spawn_stub(path):
    """Start `path` (named dev.sh) and assert it really is identifiable.

    Checking the argv here rather than letting the OURS assertion fail turns a
    platform quirk in the *fixture* into a message that names it, instead of a
    bare "STALE" that looks identical to the production bug.
    """
    path.write_text(_STUB)
    proc = subprocess.Popen(["bash", str(path)], close_fds=False)
    time.sleep(0.5)
    argv = _argv(proc.pid)
    if "dev.sh" not in argv:
        proc.kill()
        proc.wait(timeout=10)
        raise AssertionError(
            f"stub was exec-replaced by its own last command; argv={argv!r}. "
            "The fixture, not dev.sh, is what needs fixing."
        )
    return proc


def test_a_dead_pid_is_stale():
    """The common case: the machine rebooted, or dev.sh was SIGKILLed.

    The recorded start time is the REAL one, captured while the process was
    alive, so the verdict has to come from the liveness check. Passing "" would
    make this pass for the wrong reason — and worse, would leave the assertion
    resting on the command-line check, which under `-n auto` races every
    concurrent `bash -c "source …/dev.sh"` in this very file: those all have
    dev.sh in their argv, so a recycled pid would flip the answer to OURS.
    """
    proc = subprocess.Popen(["sleep", "30"], close_fds=False)
    start = _lstart(proc.pid)
    assert start, "ps -o lstart= gave nothing; the rest of this test is vacuous"
    proc.kill()
    proc.wait(timeout=10)
    res = _run_lib(
        f'dev_pidfile_is_ours {proc.pid} {start!r} {_ROOT!r} && echo OURS || echo STALE'
    )
    assert res.stdout.strip() == "STALE", res.stdout + res.stderr


def test_a_live_but_unrelated_pid_is_stale():
    """PID reuse must never turn cleanup into killing someone else's process.

    Other worktrees on this machine run their own servers, and at least one
    known orphan is deliberately left alone; a recycled pid landing on any of
    them must read as stale, not as "our dev.sh". Its own real start time is
    passed, so only the command-line check can reject it.
    """
    proc = subprocess.Popen(["sleep", "60"], close_fds=False)
    try:
        start = _lstart(proc.pid)
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {start!r} {_ROOT!r} && echo OURS || echo STALE'
        )
        assert res.stdout.strip() == "STALE", res.stdout + res.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_missing_start_time_fails_closed(tmp_path):
    """No recorded start time -> stale, even for a live process named dev.sh.

    `ps -o lstart=` can come back empty at write time, and the start time is the
    ONLY check that rules out pid reuse — alive + "argv mentions dev.sh" +
    recorded root are all satisfied by another worktree's dev.sh. Treating the
    empty record as "skip that check" would make a recycled pid landing on a
    colleague's dev.sh reapable. Failing closed costs one missed auto-restart.
    """
    proc = _spawn_stub(tmp_path / "dev.sh")
    try:
        # Same process that IS recognized when the start time is supplied.
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {_lstart(proc.pid)!r} {_ROOT!r}'
            " && echo OURS || echo STALE"
        )
        assert res.stdout.strip() == "OURS", (
            f"{res.stdout}{res.stderr} argv={_argv(proc.pid)!r} "
            f"lstart={_lstart(proc.pid)!r}"
        )
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
    proc = _spawn_stub(tmp_path / "dev.sh")
    try:
        start = _lstart(proc.pid)
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {start!r} {_ROOT!r} && echo OURS || echo STALE'
        )
        assert res.stdout.strip() == "OURS", (
            f"{res.stdout}{res.stderr} argv={_argv(proc.pid)!r} start={start!r}"
        )

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


def test_a_pidfile_from_another_worktree_root_is_stale(tmp_path):
    """Defence in depth behind the per-worktree path: the root is recorded too.

    Uses a live process that passes every OTHER check (named dev.sh, real start
    time), so the recorded root is the only thing that can reject it.
    """
    proc = _spawn_stub(tmp_path / "dev.sh")
    try:
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {_lstart(proc.pid)!r} "/some/other/worktree"'
            " && echo OURS || echo STALE"
        )
        assert res.stdout.strip() == "STALE", res.stdout + res.stderr
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_long_command_line_is_still_recognized(tmp_path):
    """`ps` must not truncate dev.sh out of its own command line.

    GNU procps caps the command column at 80 chars when stdout is not a
    terminal, and dev.sh sits at the END of the line — so on Linux a checkout
    deeper than ~74 chars (any git worktree under
    <repo>/.claude/worktrees/<name>/) would make dev_pidfile_is_ours reject its
    own pidfile forever: no error, no reap, just the duplicate trees this PR
    exists to prevent. `ps -ww` lifts the cap.

    This can only go red on Linux — BSD ps never truncated a piped `-o` — but it
    is the check that pins the flag in place, and it is cheap to keep here
    rather than in a Linux-only file nobody runs locally.
    """
    deep = tmp_path
    for part in ("a-very-deeply-nested", "worktree-style-path", "that-exceeds-eighty"):
        deep = deep / part
    deep.mkdir(parents=True)
    stub = deep / "dev.sh"
    proc = _spawn_stub(stub)
    try:
        argv = _argv(proc.pid)
        assert len(argv) > 80, f"path too short to exercise the cap: {len(argv)}"
        res = _run_lib(
            f'dev_pidfile_is_ours {proc.pid} {_lstart(proc.pid)!r} {_ROOT!r}'
            " && echo OURS || echo STALE"
        )
        assert res.stdout.strip() == "OURS", (
            f"{res.stdout}{res.stderr} argv={argv!r} (len {len(argv)})"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------
# --port extraction
# --------------------------------------------------------------------------

def test_effective_port_reads_both_port_spellings():
    """`--port N` and `--port=N` both have to win over the per-branch default.

    This is what the post-reap port wait blocks on; getting it wrong means
    waiting on the wrong port and racing cli.py's port guard.
    """
    for args, want in (
        ("--port 9000", "9000"),
        ("--port=9001", "9001"),
        ("serve --no-browser --port 9002", "9002"),
    ):
        res = _run_lib(f"dev_effective_port {args}")
        assert res.stdout.strip() == want, (args, res.stdout, res.stderr)


def test_effective_port_falls_back_to_the_branch_port():
    """No --port: the same number the server itself derives from the branch."""
    import sys

    sys.path.insert(0, _ROOT)
    from fused_render._branch import branch_port

    res = _run_lib("dev_effective_port --no-browser")
    assert res.stdout.strip() == str(branch_port("test-branch")), (
        res.stdout,
        res.stderr,
    )


# --------------------------------------------------------------------------
# structural guards
# --------------------------------------------------------------------------

def test_cleanup_flag_is_consumed_and_never_forwarded_to_the_server():
    """`--cleanup` is dev.sh's own flag; cli.py would reject it.

    It has to be stripped from `$@`, and `$@` REBUILT from the filtered array,
    before the passthrough loops build the watchfiles command / the
    single-launch argv.

    Anchored on the strip statement itself, never on the bare string
    "--cleanup": that string's first occurrence is the header documentation
    bullet ~600 chars above, so `src.index("--cleanup")` made this assertion
    "the header comment precedes line 700" — trivially true, and green even with
    the whole strip loop deleted.
    """
    src = _script()
    strip = 'if [[ "$_a" == "--cleanup" ]]'
    rebuild = 'set -- ${_ARGS[@]+"${_ARGS[@]}"}'
    assert src.count(strip) == 1, "the strip statement moved or was duplicated"
    assert src.count(rebuild) == 1, "the argv rebuild moved or was duplicated"
    strip_at = src.index(strip)
    rebuild_at = src.index(rebuild)
    assert strip_at < rebuild_at, "$@ is rebuilt before --cleanup is filtered out"
    # EVERY occurrence of each passthrough site must follow the rebuild — using
    # index()/rindex() here would reintroduce exactly the first-vs-last trap
    # this test was rewritten to escape.
    for marker in ('for a in "$@"; do CMD+=', '-m fused_render.cli "$@"'):
        hits = [m.start() for m in re.finditer(re.escape(marker), src)]
        assert hits, f"passthrough site vanished: {marker}"
        for at in hits:
            assert rebuild_at < at, f"--cleanup still reaches {marker}"


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
    start = src.index("dev_shutdown() {")
    # Bound the slice at the function's own closing brace rather than by a
    # character count: a fixed window spills into whatever follows, so the
    # assertions below could be satisfied by unrelated code further down.
    body = src[start : src.index("\n}\n", start)]
    for var in ("WATCH_PID", "CORE_WATCH_PID", "OPENER_PID", "SERVER_PID"):
        assert var in body, f"{var} is not reaped by dev_shutdown"
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
    # Matched as code, not by index()/rindex() on a substring that also occurs
    # in prose: each launch line must literally end in `&` and be followed by
    # the SERVER_PID capture and the wait.
    for launch in ("watchfiles", "fused_render\\.cli"):
        pattern = (
            r'^\s*"\$PY" -m ' + launch + r'\b[^\n]*&\n'
            r'(?:\s*#[^\n]*\n)*'
            r"\s*SERVER_PID=\$!\n"
        )
        assert re.search(pattern, src, re.M), f"{launch} is not backgrounded"
    assert src.count('wait "$SERVER_PID"') == 2, "both launches must be waited on"


def test_command_line_reads_are_width_unlimited():
    """Every `ps … command=` in dev.sh must pass -ww.

    The behavioural test above cannot fail on macOS, where BSD ps does not
    truncate; this one can, and it is the guard that keeps the flag from being
    dropped as noise by someone reading the script on a Mac.
    """
    src = _script()
    reads = re.findall(r"ps\s+([^\n|]*?)-o\s+command=", src)
    assert reads, "the command-line read vanished"
    for flags in reads:
        assert "-ww" in flags, f"`ps -o command=` without -ww: {flags!r}"


def test_dev_pids_dir_is_gitignored():
    with open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8") as f:
        assert ".dev-pids/" in f.read().split("\n")
