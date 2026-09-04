"""A session host owns the CLAUDE CLI's stdin for the life of a chat session.

`_start` (agent.py) used to Popen the CLI directly, once, for a single turn:
argv in, one message on stdin, EOF, `-p` exits. That made every follow-up a
NEW process — no way for the CLI to keep background work running between
turns, and no way for a mid-turn message to reach a turn already in flight.

This module is the process that changes that. `_start` detaches ONE of these
per session (not per turn) and hands it a JSON request on stdin (never argv —
the request may carry the user's own text-bearing paths, and argv is visible
to every local user via `ps`). This host then:

  1. Spawns the CLAUDE CLI itself, using the SAME argv `_start` used to build
     inline (now `agent._claude_argv`), with its OWN stdin held open as a
     pipe — so the CLI is never told to expect EOF after one message.
  2. Overwrites `run_dir/pid` with the CLI's own pid (not this host's) —
     `_cancel`'s `killpg` needs the CLI's process-group root, not this
     wrapper's, to reach the whole tree it may spawn.
  3. Drains `run_dir/inbox/*.json` into the CLI's stdin pipe in name order,
     moving each to `inbox/done/` once written — the first entry is the
     turn's own opening message (written by `_start`), every later one a
     follow-up (written by `_send`, a later task in this plan).
  4. Watches `agent._turn_state(run_dir)` — the SAME read `_live_run` and
     `_poll` use, off `out.jsonl` alone — and reaps (closes the CLI's stdin,
     lets both processes exit) once that has read `(turn_open=False,
     tasks_pending=False)` continuously for `_IDLE_REAP_SECONDS`. Nothing
     here re-derives that answer differently; a second implementation of
     "is this session still doing something" is a second answer.

Like every other file in this package (D-whatever governs
`templates/claude/`), this module is stdlib-only and never imports
`fused_render` — it is loaded by file path (`importlib.util
.spec_from_file_location`), the same way `claude_spawn.load_agent` loads
`agent.py` itself, and it must keep working if this whole directory is
copied out and run standalone.

Fork-safety (see `claude_spawn.py`'s own comment on this): this process is
ALREADY a bare Python interpreter with no `libproj` ever loaded — it exists
BECAUSE the server process cannot safely `fork()` (PROJ's atfork handler
SIGSEGVs on a post-fork sqlite handle). `_start` reaches this file only from
inside that same already-isolated subprocess (the `python -c` helper spawned
by `claude_spawn.spawn_helper`), so every `fork()` this module's own Popen
calls trigger (detaching, or spawning the CLI with a `cwd`) is happening in a
process that was never going to load `libproj` in the first place.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

# Both overridable by environment variable — not by editing this file at
# import time — so a test can shrink them to run fast without needing to
# monkeypatch a constant across a process boundary this module always runs
# in as a real subprocess (that's the whole point of it).
_IDLE_REAP_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS", "30"))
_DRAIN_INTERVAL_SECONDS = float(
    os.environ.get("FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS", "0.2"))


def _load_agent(path: str):
    """Load agent.py by file path — mirrors `claude_spawn.load_agent`
    exactly (same pattern, different caller): this host is spawned from a
    request that names the agent module's own path, not an import the
    Python path is guaranteed to resolve (this file may be running from a
    copy of the template directory, not the installed package)."""
    spec = importlib.util.spec_from_file_location("claude_agent_host", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _append_private(path: str):
    """Open `path` for binary append, creating it 0600 if new. Not
    `agent._private_open` — that one opens text-mode `"w"` (truncate), which
    is right for the one-shot files `_start` writes but wrong here: the
    CLI's own stdout/stderr must APPEND across the whole session, and
    `subprocess.Popen(stdout=...)` wants a file object it can write raw
    bytes to directly."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")


def _drain_inbox(agent, run_dir: str, cli_stdin) -> None:
    """Write every queued `run_dir/inbox/*.json` entry to the CLI's stdin
    pipe, oldest name first, moving each to `inbox/done/` once written.
    Entries appear here from two callers that never coordinate directly with
    each other: `_start` (the turn's opening message) and `_send` (every
    later follow-up) — this loop is the only thing that knows the order
    they should reach the CLI in, which is exactly filename order."""
    inbox = os.path.join(run_dir, "inbox")
    done = os.path.join(inbox, "done")
    try:
        names = sorted(n for n in os.listdir(inbox) if n.endswith(".json"))
    except FileNotFoundError:
        return
    if names and not os.path.isdir(done):
        # `_private_dir`'s leaf create is exclusive — right for the first
        # entry this session ever drains, wrong for every one after: this
        # loop calls in here every `_DRAIN_INTERVAL_SECONDS`, and `done`
        # already existing from the first pass is the ordinary case, not a
        # collision.
        agent._private_dir(done)
    for name in names:
        src = os.path.join(inbox, name)
        try:
            with open(src, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            continue  # raced with something else draining it; not our job
        cli_stdin.write(data)
        cli_stdin.flush()
        os.replace(src, os.path.join(done, name))


def _cli_popen_command(argv: list):
    """The command to hand `subprocess.Popen` for the CLI spawn — `argv`
    unchanged everywhere except when `argv[0]` (`agent._claude_bin()`'s
    resolution) is a Windows `.bat`/`.cmd`.

    A `.bat`/`.cmd` can only be started through cmd.exe — CreateProcess
    talks to real executables and cannot launch one at all. Python's own hop
    to cmd.exe is `shell=True`, which wraps the command line as
    `%ComSpec% /c "<our string>"` — so cmd.exe, not the batch file, is what
    actually receives argv, as one command-line string. cmd.exe's `/c`
    parser then scans that WHOLE string for its own
    operators (`< > & | ^`) wherever they fall, quotes or no — double quotes
    protect a token from being split on whitespace, but do NOT stop cmd.exe
    from reading a `<`/`>` inside one as real redirection. `_claude_argv`'s
    `--append-system-prompt` text is not test-only content that could be
    written around this — it is the product's own boilerplate (the
    `<live-app-state>` tag mentioned in every prompt, D239) — so any `.bat`/
    `.cmd`-resolved `claude` (an npm install on Windows installs exactly one
    of these two, `_WINDOWS_CANDIDATES` still lists `.cmd` after `.exe`) hits
    a cmd.exe parse of `<live-app-state>` as "redirect stdin from a file
    named live-app-state", which does not exist — cmd.exe errors out and
    never reaches the batch file's own body, so the CLI never launches at
    all, on every single turn.

    The fix is `cmd.exe /c`'s own documented escape hatch (`cmd /?`, the
    fallback rule below its "no special characters between the quotes"
    case): when the argument to `/c` begins and ends with a double quote,
    cmd strips exactly that one outer pair and runs the interior literally,
    without re-scanning it for redirection. That outer pair is exactly what
    `shell=True` adds, so this returns the INTERIOR — every element quoted,
    joined by spaces — and the caller spawns a `str` with `shell=True`. The
    interior then reaches `claude.bat`/`claude.cmd`'s own CreateProcess call
    untouched, `<live-app-state>` included.

    EVERY element is quoted, not just the ones `subprocess.list2cmdline`
    would quote for having whitespace in them: the outer pair stops cmd
    re-parsing QUOTES, not metacharacters (`& | > < ^`), and a quoted run is
    the only place those stay literal. An unquoted `<live-app-state>` inside
    an otherwise correct line is still read as "redirect stdin from a file
    named live-app-state". Nothing here can contain a `"` — Windows paths
    cannot, and the rest is the product's own boilerplate — so a stray one
    raises rather than silently producing a line that means something else.

    This is the same shape `fused_render/server/ai.py` (`_popen_cmd` /
    `_spawn_claude_stream`) already runs in production for the same `.cmd`
    shim, arrived at the same way; the two are deliberately identical.

    Left as `argv` (the list form) whenever `argv[0]` is NOT a `.bat`/`.cmd`
    — including every POSIX launch, and a native `.exe` `claude` on Windows
    — since CreateProcess talks directly to a real executable there and
    never involves cmd.exe or its redirection scan."""
    if os.name == "nt" and os.path.splitext(argv[0])[1].lower() in (".bat", ".cmd"):
        for arg in argv:
            if '"' in arg:
                raise ValueError(
                    "argument may not contain a double quote: %r" % (arg,))
        return " ".join('"%s"' % arg for arg in argv)
    return argv


def _turn_state_if_grown(agent, run_dir: str, cache: dict) -> tuple:
    """`agent._turn_state(run_dir)`, but only actually re-parses `out.jsonl`
    when the file has grown since the last call — the reap loop below calls
    this every `_DRAIN_INTERVAL_SECONDS` (0.2s) for the entire life of a
    session, and `_turn_state` re-reads and re-`json.loads`s the WHOLE file
    every time. A multi-hour session with a transcript in the tens of
    megabytes burned a core continuously on a file that had not changed a
    single byte since the previous tick. The turn/task state cannot have
    changed if no new bytes landed, so a cheap `os.path.getsize` stands in
    for the real read on every tick that would have been wasted work; `cache`
    (a plain dict the caller owns, `{"size": ..., "state": ...}`) is mutated
    in place so this needs no state of its own between calls."""
    try:
        size = os.path.getsize(os.path.join(run_dir, "out.jsonl"))
    except OSError:
        size = 0
    if size != cache.get("size") or "state" not in cache:
        cache["size"] = size
        cache["state"] = agent._turn_state(run_dir)
    return cache["state"]


def main() -> None:
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    agent = _load_agent(req["agent"])
    run_dir = req["run_dir"]

    argv = agent._claude_argv(
        run_dir, req["pane"], req["cli_mode"] or None, req["session_id"],
        req["model"], req["effort"], req["extra_read_dirs"], req["file"])

    out_fh = _append_private(os.path.join(run_dir, "out.jsonl"))
    err_fh = _append_private(os.path.join(run_dir, "err.log"))
    try:
        # Same _DETACH discipline as the CLI spawn this replaces: the CLI
        # becomes its own session leader (POSIX) / process-group root
        # (Windows), independent of THIS host's own detachment from
        # `_start`'s caller. Nested detachment is fine — each process is its
        # own leader, and `_cancel`'s killpg only ever needs the CLI's.
        command = _cli_popen_command(argv)
        cli = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=out_fh,
            stderr=err_fh, cwd=req["cwd"], env=agent._spawn_env(),
            # A `str` is the Windows `.bat`/`.cmd` case and ONLY that (see
            # `_cli_popen_command`): the cmd.exe hop is what can launch a
            # batch file at all, and `shell=True` is what adds the single
            # outer quote pair cmd parses deterministically. Not an
            # injection surface — the payload is ours and fully quoted, and
            # the user's message never appears in it (it rides the inbox).
            # `_cancel` already kills with `taskkill /T`, which walks past
            # the cmd.exe wrapper to the CLI underneath it.
            shell=isinstance(command, str),
            **agent._DETACH)
    except Exception as exc:
        # No CLI process ever existed — write the failure where `_poll`'s
        # abnormal-exit fallback already knows to look (the tail of
        # err.log), same file `_start` pre-created empty for exactly this
        # case. One extra poll cycle of latency versus the old synchronous
        # failure, in exchange for `_start` no longer having to wait on a
        # second interpreter before it can answer the caller.
        err_fh.write(str(exc).encode("utf-8", "replace"))
        return
    finally:
        out_fh.close()
        err_fh.close()

    # Overwrite the transient host-pid `_start` left behind: `_cancel`'s
    # killpg must reach the CLI's own process group, not this wrapper's.
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(cli.pid))

    host_json = os.path.join(run_dir, "host.json")
    with agent._private_open(host_json) as f:
        json.dump({
            "pid": os.getpid(), "session_id": req["session_id"],
            "file": req["file"], "mode": req["cli_mode"],
            "model": req["model"], "effort": req["effort"],
            "read_dirs": req["extra_read_dirs"],
        }, f)

    _reap_loop(agent, run_dir, cli, host_json)


def _reap_loop(agent, run_dir: str, cli, host_json: str) -> None:
    """Drain `run_dir/inbox` into `cli`'s stdin and watch
    `agent._turn_state` until idle-with-nothing-pending has held for
    `_IDLE_REAP_SECONDS`, then tear the session down. Extracted out of
    `main()` so a test can drive it directly against a fake `cli` — the
    race this function's `finally` block closes cannot be reproduced
    deterministically through a real subprocess and wall-clock sleeps.

    A `_send` writes straight into `run_dir/inbox` with no coordination
    with this loop at all, and can land in the gap between this loop's own
    last regular `_drain_inbox` call (top of the iteration that goes on to
    `break`) and the loop actually exiting — silently orphaning that
    message even though `_send` already told its caller `{"sent": True}`.
    One more `_drain_inbox` pass runs in `finally`, after every way this
    loop can end (the idle break, an out.jsonl read failure, or the CLI
    already dead), before `cli.stdin` is closed — and `host_json` is
    removed right after that, as early as this function can safely do it,
    so a `_send` racing past this point starts failing cleanly (no live
    `host.json` to find) instead of queuing into an inbox nothing will
    ever drain again."""
    idle_since = None
    turn_state_cache = {}
    try:
        while cli.poll() is None:
            _drain_inbox(agent, run_dir, cli.stdin)
            turn_open, tasks_pending = _turn_state_if_grown(
                agent, run_dir, turn_state_cache)
            if turn_open or tasks_pending:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= _IDLE_REAP_SECONDS:
                    break
            time.sleep(_DRAIN_INTERVAL_SECONDS)
    except Exception as exc:
        # Any read failure inside this loop favors REAPING, not hanging —
        # a host stuck alive forever because one `os.listdir` raised is
        # worse than a session that ends a poll cycle early. Nothing here
        # is worth crashing over; the CLI's own exit code (if any) is what
        # `_poll` reports. Logged (not silent) so a real bug here is still
        # findable — appended, since out.jsonl/err.log already exist and a
        # `_private_open` truncate would erase the CLI's own transcript.
        try:
            with _append_private(os.path.join(run_dir, "err.log")) as f:
                f.write(("\nsession_host: %r\n" % (exc,)).encode("utf-8"))
        except OSError:
            pass
    finally:
        try:
            _drain_inbox(agent, run_dir, cli.stdin)
        except Exception:
            pass  # the CLI may already be gone; nothing left to hand it
        try:
            cli.stdin.close()
        except Exception:
            pass
        try:
            os.remove(host_json)
        except OSError:
            pass
        if cli.poll() is None:
            try:
                cli.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


if __name__ == "__main__":
    main()
