"""Heads-up notes: a side check that reads a running chat and, rarely, offers
ONE short card the user should see.

The check is a FORK of the live session (`--resume <sid> --fork-session
--no-session-persistence`), launched with the same argv the main session got
from `agent._claude_argv` so the system prompt, tool list and MCP roster match
byte for byte — that is what makes the fork a prompt-cache hit instead of a
cold read of the whole conversation (measured: a changed `--tools` or any
`--disallowedTools` is a full miss). Tools are denied at call time by
`--permission-mode dontAsk`, not by trimming the roster.

Driven by the page's polling (`poll`), never by a server-side timer: a closed
tab means no checks.
"""
from __future__ import annotations

import collections
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import typing
import uuid

logger = logging.getLogger(__name__)

HEADSUP_TIMEOUT = 120.0
#: A check fires every this many main-thread tool calls within one turn.
STEPS_PER_CHECK = 6
_MAX_RUNS = 64
_MAX_BULLETS = 3
_MAX_TITLE = 120
_MAX_BULLET = 400
_TAGS = ("You should know", "Heads up")

HEADSUP_PROMPT = """\
You are checking the work in the conversation above on the user's behalf. Do not use any tools; just reply in text.

The main agent is not interrupted: it keeps working while you read, so you may be reading in the middle of its work. It is very capable and will very possibly reach the same point you do, possibly in the reply it is writing right now. Only offer something if you are confident it has been, or will be, glossed over. Do not do the agent's job: do not investigate the user's question yourself, and do not explain or pre-announce a finding, root cause or conclusion the agent is working toward. Avoid topics the user is already discussing with the agent: if the user asked the agent to explain or find X, do not offer X.

Look for at most ONE thing the user should really know about this session and very likely does not understand yet. Good candidates: a decision or trade-off the agent made on the user's behalf, an assumption it is acting on, a constraint it hit, a non-obvious edge case, a result from a step it already finished that may be wrong, or a decision that carries a real cost in money, time, wasted work or a wrong result. A decision or detail the agent mentioned only in passing, inside a long answer or in the middle of a long run of tool calls, still counts as possibly missed, because people do not read everything.

Progress is never a topic: never say something is missing, unfinished, not built, not pushed, or that the session or the agent stopped or ended. The user can see whether the agent is still running.

Interesting is not the same as important. If the best reason you can give is that their understanding would be more complete, answer `learn: none`. Skip anything the user has already shown they understand, anything the agent has already explained clearly, and anything you are not confident is true. Default to `learn: none`. The bar is very high, and most checks should find nothing.

Write for a tired reader who switched away and forgot the details. Use plain words, name the real thing instead of saying "this" or "it", define any technical term, and do not end with a question. Be brief: the whole note should read in five seconds.

Pick the tag "Heads up" for the work in this session: a decision the agent made, something it did not highlight, or a result that may be off, with an immediate cost if missed. Pick "You should know" for how something works when it deeply matters to their work.

Reply with exactly `learn: none`, or exactly this shape:

learn: <one plain line>
tag: <You should know | Heads up>
explain:
**<a title of two to six words>**
* <bullet>
* <bullet>

Use 1 to 3 bullets. Each bullet is one short sentence of at most 20 words. Do not recap what the agent did, and do not list file paths unless the path is the point. The last bullet may be one concrete thing to check or do. Write in the user's language, but keep the labels `learn:`, `tag:` and `explain:` and the two tag texts in English.
"""


# ---------------------------------------------------------------- parsing

def parse_note(text) -> dict | None:
    """The card out of the model's reply, or None for "nothing to say" and for
    anything malformed (no learn line, no title, no bullets)."""
    if not isinstance(text, str):
        return None
    learn = ""
    tag = _TAGS[0]
    title = ""
    bullets: list = []
    in_explain = False
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower()
        if not in_explain:
            if low.startswith("learn:"):
                learn = line[6:].strip()
                continue
            elif low.startswith("tag:"):
                value = line[4:].strip().strip("*_` ").rstrip(".").lower()
                for t in _TAGS:
                    if value == t.lower():
                        tag = t
                continue
            elif low.startswith("explain:"):
                in_explain = True
                line = line[8:].strip()
            else:
                continue
        if not line:
            continue
        if line.startswith(("* ", "- ", "• ")):
            if title:
                bullet = line[2:].strip()
                if bullet:
                    bullets.append(bullet[:_MAX_BULLET])
        elif not title and not bullets:
            cleaned = line.replace("**", "").strip().strip("#").strip()
            if cleaned:
                title = cleaned[:_MAX_TITLE]
    if not learn or learn.lower().rstrip(".!, ") == "none":
        return None
    if not title or not bullets:
        return None
    return {"learn": learn, "tag": tag, "title": title,
            "bullets": bullets[:_MAX_BULLETS]}


def _result_text(stdout: str) -> str:
    """The `result` string of a clean-success terminal event in a
    `--output-format stream-json` stream, else "". Scans from the end: a
    trailing housekeeping row after `result` is ordinary. Not
    `claude_sessions._recap_result`: that one ends in the recap's own
    plain-text and length rules, which would mangle a structured note."""
    for line in reversed((stdout or "").splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("type") == "result":
            if row.get("is_error") or row.get("subtype") != "success":
                return ""
            text = row.get("result")
            return text if isinstance(text, str) else ""
    return ""


# ------------------------------------------------------------ transcript

_steps_lock = threading.Lock()
#: run_dir -> incremental scan state of its `out.jsonl`. LRU-bounded like `_state`.
_steps_cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_TAIL = 64


def main_steps(run_dir: str, agent=None) -> tuple:
    """`(turn_index, steps, turn_open)` read off the run's `out.jsonl`.

    `steps` is the count of main-thread `tool_use` blocks in the CURRENT turn,
    or in the turn that just ended when the last row is a `result`.
    `turn_index` is the 0-based index of THAT turn (results seen before it),
    so the closing `result` does not move a turn to a new index mid-dedupe.
    Same row rules as `agent._turn_state`: subagent rows and
    non-turn rows are skipped.

    Incremental: a per-run cache keeps the byte offset of the last fully
    consumed line plus the running counters, so a poll reads only new bytes.
    A file that shrank, or whose bytes before the offset changed, is rescanned
    from the start. A partial trailing line is left unconsumed."""
    not_a_turn = getattr(agent, "_not_a_turn_row", None) or _fallback_not_a_turn
    with _steps_lock:
        c = _steps_cache.get(run_dir)
        if c is None:
            c = {"offset": 0, "tail": b"", "results": 0, "steps": 0,
                 "last_steps": 0, "idle": False, "pending": set()}
            _steps_cache[run_dir] = c
        _steps_cache.move_to_end(run_dir)
        while len(_steps_cache) > _MAX_RUNS:
            _steps_cache.popitem(last=False)
        try:
            with open(os.path.join(run_dir, "out.jsonl"), "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                ok = size >= c["offset"]
                if ok and c["offset"]:
                    start = max(c["offset"] - len(c["tail"]), 0)
                    fh.seek(start)
                    ok = fh.read(c["offset"] - start) == c["tail"]
                if not ok:
                    c.update(offset=0, tail=b"", results=0, steps=0,
                             last_steps=0, idle=False, pending=set())
                fh.seek(c["offset"])
                data = fh.read()
        except OSError:
            c.update(offset=0, tail=b"", results=0, steps=0, last_steps=0, idle=False, pending=set())
            data = b""
        end = data.rfind(b"\n") + 1
        if end:
            consumed = data[:end]
            for raw in consumed.split(b"\n"):
                _consume(c, raw, not_a_turn)
            c["offset"] += end
            c["tail"] = (c["tail"] + consumed)[-_TAIL:]
        if c["idle"]:
            return max(c["results"] - 1, 0), c["last_steps"], False
        return c["results"], c["steps"], True


def pending_tools(run_dir: str, agent=None) -> int:
    """How many main-thread tool calls of the current turn have no
    `tool_result` yet. Non-zero means the main agent is mid-step, so a fork of
    the on-disk session would end in an unanswered call. Reads the same cache
    as `main_steps` (refreshing it first)."""
    main_steps(run_dir, agent)
    with _steps_lock:
        c = _steps_cache.get(run_dir)
        return len(c["pending"]) if c else 0


def _consume(c: dict, raw: bytes, not_a_turn) -> None:
    if not raw.strip():
        return
    try:
        row = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return
    if not isinstance(row, dict) or row.get("parent_tool_use_id"):
        return
    t = row.get("type")
    if not_a_turn(row):
        return
    c["idle"] = t == "result"
    if t == "result":
        c["results"] += 1
        c["last_steps"] = c["steps"]
        c["steps"] = 0
        c["pending"].clear()
    elif t in ("assistant", "user"):
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if t == "assistant" and b.get("type") == "tool_use":
                    c["steps"] += 1
                    if b.get("id"):
                        c["pending"].add(b["id"])
                elif t == "user" and b.get("type") == "tool_result":
                    c["pending"].discard(b.get("tool_use_id"))


def _fallback_not_a_turn(row: dict) -> bool:
    return row.get("type") in ("rate_limit_event", "stream_event")


# ------------------------------------------------------------------- fork

class Fork(typing.NamedTuple):
    argv: list
    cwd: str
    dir: str
    session_id: str


def build_fork_argv(agent, run_dir: str):
    """A `Fork(argv, cwd, dir, session_id)` for a fork of this run's session, or None when
    the run cannot be forked (no launch.json, a non-Claude run, no session)."""
    try:
        with open(os.path.join(run_dir, "launch.json"), encoding="utf-8") as fh:
            launch = json.load(fh)
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(launch, dict) or launch.get("backend", "claude") != "claude":
        return None
    session_id = agent._run_own_session(run_dir, meta if isinstance(meta, dict) else {})
    if not session_id:
        return None
    # A dir of its own: the fork's MCP config and permission server must never
    # write into the live run's directory.
    # set_model rewrites only host.json, so the live model/effort win over launch.json.
    model, effort = launch.get("model") or "", launch.get("effort") or ""
    try:
        with open(os.path.join(run_dir, "host.json"), encoding="utf-8") as fh:
            host = json.load(fh)
    except (OSError, ValueError):
        host = None
    if isinstance(host, dict):
        if isinstance(host.get("model"), str) and host["model"]:
            model = host["model"]
        if isinstance(host.get("effort"), str) and host["effort"]:
            effort = host["effort"]
    # `--resume` resolves the session from the cwd's project: with no real cwd
    # the fork could not resume, so there is no check (no tempdir fallback).
    cwd = launch.get("cwd") or ""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return None
    fork_dir = tempfile.mkdtemp(prefix="headsup-")
    try:
        argv = agent._claude_argv(
            fork_dir, bool(launch.get("pane")), None, session_id,
            model, effort,
            launch.get("extra_read_dirs") or [], launch.get("file") or "",
            strict_mcp=bool(launch.get("strict_mcp")))
    except Exception:
        shutil.rmtree(fork_dir, ignore_errors=True)
        raise
    # The fork must not fire the user's SessionStart/Stop hooks; measured to
    # keep a full cache hit (134k read, 0 written).
    argv += ["--fork-session", "--no-session-persistence",
             "--permission-mode", "dontAsk", "--max-turns", "1",
             "--settings", '{"disableAllHooks": true}']
    return Fork(argv, cwd, fork_dir, session_id)


_SETSID_EXEC = ("import os,sys; os.setsid(); os.chdir(sys.argv[1]); "
                "os.execvp(sys.argv[2], sys.argv[2:])")


def _spawn_argv(argv: list, cwd: str) -> tuple:
    """`(argv, cwd_kwarg)` that runs `argv` in `cwd` WITHOUT `Popen(cwd=)`.

    `--resume` finds the session under the cwd's project dir, so the fork has
    to start in the main session's cwd. But a `cwd=` (or `start_new_session=`
    / `preexec_fn=`) makes CPython fork()+exec instead of posix_spawn, and a
    fork with libproj resident dies SIGSEGV (D277/D292). So on POSIX the
    interpreter (absolute `sys.executable`, `-B -I`: the bundled app's python
    must never write .pyc) is the wrapper: the child itself calls setsid (so
    the whole fork tree shares one process group, which `generate` kills on
    timeout), chdirs and execs. Without an absolute interpreter, a `/bin/sh`
    does only the `cd`. Windows has no fork path to dodge, so it passes cwd."""
    if os.name == "nt":
        return argv, cwd
    if os.path.isabs(sys.executable or ""):
        return ([sys.executable, "-B", "-I", "-c", _SETSID_EXEC, cwd, *argv], None)
    return (["/bin/sh", "-c", 'cd "$1" || exit 1; shift; exec "$@"', "sh", cwd, *argv],
            None)


def _kill_tree(proc) -> None:
    """SIGKILL the fork's whole process group (MCP children included); the
    wrapper made the child a group leader. Falls back to the parent alone."""
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    proc.kill()


_OPEN_TURN = ("\nThe main agent is still working on this turn right now. The "
              "conversation above stops partway through its work and may not show "
              "its latest step, so only judge steps that already finished. Never "
              "report anything as missing, unfinished or stopped.\n")


def _build_prompt(seen: list, known: list, turn_open: bool = False) -> str:
    prompt = HEADSUP_PROMPT
    if turn_open:
        prompt += _OPEN_TURN
    if seen:
        prompt += ("\nThese notes were already shown to the user recently. "
                   "Do not offer them again:\n"
                   + "".join(f"- {x}\n" for x in seen))
    if known:
        prompt += ("\nThe user said they already understood these topics. "
                   "Do not offer them:\n"
                   + "".join(f"- {x}\n" for x in known))
    return prompt


def generate(agent, run_dir: str, turn_open: bool = False) -> dict | None:
    """Run the fork once and return the parsed note, or None."""
    built = build_fork_argv(agent, run_dir)
    if built is None:
        return None
    argv, cwd, fork_dir, session = built
    seen, known = _merged(_mem_read(agent.RUNS))
    try:
        message = json.dumps({"type": "user", "message": {
            "role": "user",
            "content": [{"type": "text", "text": _build_prompt(seen, known, turn_open)}]}})
        spawn, kw_cwd = _spawn_argv(argv, cwd)
        kwargs = {"cwd": kw_cwd} if kw_cwd else {}
        proc = subprocess.Popen(
            spawn, env=agent._spawn_env(session), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            close_fds=False, **kwargs)
        try:
            stdout, _ = proc.communicate(input=message + "\n",
                                         timeout=HEADSUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("a killed heads-up fork (pid %s) did not exit", proc.pid)
            return None
        if proc.returncode != 0:
            return None
        note = parse_note(_result_text(stdout))
        if note and _norm(note["learn"]) in {
                _norm(x) for x in seen + known}:
            return None
        return note
    finally:
        shutil.rmtree(fork_dir, ignore_errors=True)


# --------------------------------------------------------- feedback memory

_MEM_FILE = "headsup_memory.json"
_MEM_KEEP = 50
_MAX_SKIP = 16


def _norm(line) -> str:
    return str(line).strip().lower().rstrip(".!")


def _mem_path(root: str | None) -> str | None:
    """One memory file for every run, next to the runs directory."""
    return os.path.join(os.path.dirname(root), _MEM_FILE) if root else None


def _mem_read(root: str | None) -> dict:
    """The feedback memory, or empty defaults for a missing/corrupt file."""
    mem: dict = {"seen": [], "known": [], "ignored_in_a_row": 0, "skip_checks": 0}
    path = _mem_path(root)
    if not path:
        return mem
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return mem
    if not isinstance(data, dict):
        return mem
    for key in ("seen", "known"):
        if isinstance(data.get(key), list):
            mem[key] = [x for x in data[key] if isinstance(x, str)][-_MEM_KEEP:]
    for key in ("ignored_in_a_row", "skip_checks"):
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            mem[key] = v
    return mem


def _mem_save(root: str | None, mem: dict) -> None:
    """Atomic write. Callers hold `_lock`. A failed write loses only learning."""
    path = _mem_path(root)
    if not path:
        return
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(mem, fh)
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not save %s", path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _claude_store_glob() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    return os.path.join(base, "plugins", "store",
                        "cc-plugin-you-should-know_builtin-*.json")


def _claude_lists() -> tuple:
    """`(seen, known)` from Claude Code's own heads-up plugin store, unioned
    over every matching file. READ-ONLY: Claude Code rewrites these itself.
    Any error means empty lists."""
    seen: list = []
    known: list = []
    try:
        for path in sorted(glob.glob(_claude_store_glob())):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for key, out in (("seen", seen), ("known", known)):
                if isinstance(data.get(key), list):
                    out.extend(x for x in data[key] if isinstance(x, str))
    except Exception:  # noqa: BLE001 — a hint source, never a fault
        return [], []
    return seen, known


def _merged(mem: dict) -> tuple:
    """Our lists plus Claude Code's, `_norm`-deduped, last 50 of each."""
    cs, ck = _claude_lists()
    out = []
    for ours, theirs in ((mem["seen"], cs), (mem["known"], ck)):
        by: dict = {}
        for x in ours + theirs:
            by.pop(_norm(x), None)
            by[_norm(x)] = x
        out.append(list(by.values())[-_MEM_KEEP:])
    return out[0], out[1]


def _remember(mem: dict, key: str, line: str) -> None:
    """Append `line` to a list, case-insensitively deduped, last 50 kept."""
    n = _norm(line)
    mem[key] = [x for x in mem[key] if _norm(x) != n] + [line]
    mem[key] = mem[key][-_MEM_KEEP:]


# -------------------------------------------------------------- scheduler

_lock = threading.Lock()
_state: "collections.OrderedDict[str, dict]" = collections.OrderedDict()


#: Where run dirs live; set by the first poll, so `dismiss` (which gets no
#: agent) can find a run's `headsup.json` after a restart.
_runs_root: str | None = None
_FILE = "headsup.json"
_MAX_CHECKED = 200


def _state_path(run_id: str, run_dir: str | None) -> str | None:
    if run_dir:
        return os.path.join(run_dir, _FILE)
    if _runs_root and run_id:
        return os.path.join(_runs_root, run_id, _FILE)
    return None


def _load(path: str | None) -> dict:
    """The persisted part of a run's state, or {} for a missing/corrupt file."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    note = data.get("note")
    if isinstance(note, dict):
        out["note"] = note
        nid = data.get("note_id")
        out["note_id"] = nid if isinstance(nid, str) else ""
        turn = data.get("offered_turn")
        out["offered_turn"] = turn if isinstance(turn, int) and not isinstance(turn, bool) else None
        out["note_open"] = data.get("note_open") is True
    wt = data.get("withdrawn_turn")
    if isinstance(wt, int) and not isinstance(wt, bool):
        out["withdrawn_turn"] = wt
    dismissed = data.get("dismissed")
    if isinstance(dismissed, list):
        out["dismissed"] = {d for d in dismissed if isinstance(d, str)}
    checked = data.get("checked")
    if isinstance(checked, list):
        out["checked"] = {
            (p[0], p[1]) for p in checked
            if isinstance(p, list) and len(p) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in p)}
    return out


def _save(st: dict) -> None:
    """Persist the note and dismissed ids atomically. Callers hold `_lock`.
    A failed write is only a lost restart-survival, never a fault."""
    path = st.get("path")
    if not path:
        return
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"note": st["note"], "note_id": st["note_id"],
                       "offered_turn": st["offered_turn"],
                       "note_open": st["note_open"],
                       "withdrawn_turn": st["withdrawn_turn"],
                       "dismissed": sorted(st["dismissed"]),
                       # a restart must not re-run a fork (~134k tokens) for a
                       # turn that was already checked
                       "checked": [list(k) for k in
                                   sorted(st["checked"])[-_MAX_CHECKED:]]}, fh)
        os.replace(tmp, path)
    except OSError:
        logger.warning("could not save %s", path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _run_state(run_id: str, run_dir: str | None = None) -> dict:
    """The run's state record, LRU-bounded, seeded from `headsup.json` the
    first time it is built. Callers hold `_lock`."""
    st = _state.get(run_id)
    if st is None:
        path = _state_path(run_id, run_dir)
        st = {"checked": set(), "offered_turn": None, "inflight": False,
              "note": None, "note_id": "", "note_open": False,
              "withdrawn_turn": None, "dismissed": set(), "path": path}
        st.update(_load(path))
        _state[run_id] = st
    _state.move_to_end(run_id)
    while len(_state) > _MAX_RUNS:
        _state.popitem(last=False)
    return st


def _worker(agent, run_id: str, run_dir: str, turn_index: int,
            turn_open: bool = False) -> None:
    note = None
    try:
        note = generate(agent, run_dir, turn_open)
    except Exception:  # noqa: BLE001 — a decoration, never a fault
        logger.exception("heads-up check failed for run %s", run_id)
    # A new user turn began while the check ran: the note is about the past.
    stale = False
    if note:
        cur, _, cur_open = main_steps(run_dir, agent)
        stale = cur > turn_index and cur_open
    with _lock:
        st = _run_state(run_id, run_dir)
        if note and not stale:
            st["note"] = note
            st["note_id"] = uuid.uuid4().hex[:12]
            st["offered_turn"] = turn_index
            # A note written while its turn was still running is withdrawn
            # when that turn ends (see `_poll`).
            st["note_open"] = turn_open
            _save(st)
            mem = _mem_read(_runs_root)
            _remember(mem, "seen", note["learn"])
            _mem_save(_runs_root, mem)
        st["inflight"] = False


def poll(agent, run_id: str) -> dict:
    """What the page should show for this run now. Never raises."""
    try:
        return _poll(agent, run_id)
    except Exception:  # noqa: BLE001
        logger.exception("heads-up poll failed for run %s", run_id)
        return {"state": "idle"}


def _poll(agent, run_id: str) -> dict:
    if agent._bad_id(run_id):
        return {"state": "idle"}
    global _runs_root
    _runs_root = agent.RUNS
    run_dir = os.path.join(agent.RUNS, run_id)
    if not os.path.isdir(run_dir):
        return {"state": "idle"}
    with _lock:
        st = _run_state(run_id, run_dir)
        has_note = st["note"] is not None
        offered = st["offered_turn"]
    if has_note and offered is not None and main_steps(run_dir, agent)[0] >= offered + 2:
        with _lock:
            st = _run_state(run_id)
            if st["offered_turn"] == offered and st["note"] is not None:
                if st["note_id"] not in st["dismissed"]:
                    # Expired unanswered: back off the checks.
                    mem = _mem_read(_runs_root)
                    n = mem["ignored_in_a_row"] + 1
                    mem["ignored_in_a_row"] = n
                    mem["skip_checks"] = 0 if n <= 2 else min(_MAX_SKIP, 2 ** (n - 3))
                    _mem_save(_runs_root, mem)
                st["note"] = None
                st["note_open"] = False
                _save(st)
    with _lock:
        st = _run_state(run_id)
        if st["note"] is not None and st["note_open"] and st["offered_turn"] is not None:
            # A mid-turn note is about work in progress: once its turn is over
            # the turn-end review replaces it. Not ignored, not dismissed.
            cur, _, cur_open = main_steps(run_dir, agent)
            if cur > st["offered_turn"] or not cur_open:
                st["withdrawn_turn"] = st["offered_turn"]
                st["note"] = None
                st["note_open"] = False
                _save(st)
        if st["note"] is not None and st["note_id"] not in st["dismissed"]:
            return {"state": "note", "id": st["note_id"], "note": st["note"]}
        if st["inflight"]:
            return {"state": "checking"}
    turn_index, steps, turn_open = main_steps(run_dir, agent)
    if steps < STEPS_PER_CHECK:
        return {"state": "idle"}
    if turn_open and pending_tools(run_dir, agent):
        # Mid-step: a fork now would end in an unanswered tool call. Leave the
        # bucket unrecorded so it fires on a later poll, at a step boundary.
        return {"state": "idle"}
    with _lock:
        st = _run_state(run_id)
        if st["inflight"]:
            return {"state": "checking"}
        withdrawn = st["withdrawn_turn"] == turn_index and not turn_open
        if st["offered_turn"] == turn_index and not withdrawn:
            return {"state": "idle"}
        bucket = steps // STEPS_PER_CHECK
        key = (turn_index, bucket)
        if not withdrawn and key in st["checked"]:
            return {"state": "idle"}
        if not turn_open:
            # The turn just closed: one final look, but only when no check ran
            # in this turn at all, or the only note it produced was withdrawn.
            if not withdrawn and any(t == turn_index for t, _ in st["checked"]):
                return {"state": "idle"}
            key = (turn_index, -1)
            if key in st["checked"]:
                return {"state": "idle"}
        mem = _mem_read(_runs_root)
        if mem["skip_checks"] > 0:
            mem["skip_checks"] -= 1
            _mem_save(_runs_root, mem)
            st["checked"].add(key)
            _save(st)
            return {"state": "idle"}
        st["checked"].add(key)
        _save(st)
        st["inflight"] = True
    threading.Thread(target=_worker, args=(agent, run_id, run_dir, turn_index, turn_open),
                     daemon=True).start()
    return {"state": "checking"}


_ANSWERS = ("got_it", "dismiss", "chat")


def dismiss(run_id: str, note_id: str, answer: str = "dismiss") -> None:
    if not run_id or run_id.startswith(".") or any(c in run_id for c in "/\\:"):
        return
    with _lock:
        st = _state.get(run_id)
        if st is None and not (_runs_root and os.path.isdir(os.path.join(_runs_root, run_id))):
            return
        st = _run_state(run_id)
        if answer not in _ANSWERS:
            answer = "dismiss"
        mem = _mem_read(_runs_root)
        if answer == "got_it" and st["note_id"] == note_id and st["note"]:
            _remember(mem, "known", st["note"].get("learn") or "")
        mem["ignored_in_a_row"] = 0
        mem["skip_checks"] = 0
        _mem_save(_runs_root, mem)
        st["dismissed"].add(note_id)
        if st["note_id"] == note_id:
            st["note"] = None
            st["note_open"] = False
        _save(st)
