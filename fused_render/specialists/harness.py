"""Side sessions: one-shot `claude -p` runs of a specialist (check, reviewer, nightly; PLAN A.1, C.1).

A side session has nobody to answer a permission prompt, so it runs in the `default` permission mode with no
prompt tool: whatever no allow rule covers is refused by Claude Code itself. The fence is the argv (`argv`):

- `--setting-sources=project` (NEVER `--setting-sources=`, which stops CLAUDE.md and the project skills from
  loading, verified on 2.1.327): the home's CLAUDE.md (charter + `@memory.md` + `@said.md`) and its skills load
  natively, the user's own settings and MCP servers do not.
- `--tools Read,Glob,Grep` (+ `Edit,Write` only when the caller names write globs, i.e. the nightly job) and
  `--allowedTools` rules: `Read(//<dir>/**)` per read dir, `Edit(//<home>/<glob>)` per write glob, and
  `mcp__specialist_services` for a nightly with services.
- `--strict-mcp-config --mcp-config <inline>`: no servers for check/reviewer; the read-only service roster for
  a nightly with `services=True`.
- env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: memory is ours (one writer), not Claude Code's auto memory.

Each run leaves a row `<home>/.specialist/runs/<row id>.json` (the experts row shape: id, kind, started, ended,
status, trigger, transcript, answer, error?, cost_usd?). A row still `running` that no thread of this process
runs was cut short by a restart and reads `interrupted` (scenario X); callers decide whether to try again.

Concurrency: checks and reviewers share a semaphore (`PARALLEL`); a nightly run is serialised by stream 3's
per-home `nightly.lock` (flock), which this module does not take. `_spawn(call)` is the one function that runs a
process: tests replace it (`tests/_specialists_conftest.stub_side`).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Literal, Sequence

from fused_render.specialists import home as hm
from fused_render.specialists import paths

logger = logging.getLogger(__name__)

KINDS = ("check", "reviewer", "nightly")
PARALLEL = 3
SLOT_WAIT_S = 120.0
ROWS_KEPT = 300
LINE_MAX = 4000
BUSY = "the specialist is busy; try again in a minute"
CLOSED = "the server is shutting down; no new specialist session starts"
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "low"

_SLOTS = threading.BoundedSemaphore(PARALLEL)
_GUARD = threading.Lock()
_RUNNING: set = set()     # row ids this process is running
_PROCS: set = set()       # their processes (shutdown)
_STATE = {"closed": False}
_GLOB_CHARS = frozenset("*?[]{}")


def _env_float(name: str, default: float, floor: float = 0.0) -> float:
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return max(floor, v) if v > 0 and v != float("inf") else default


def timeout_for(kind: str) -> float:
    if kind == "nightly":
        return _env_float("FUSED_RENDER_SPECIALIST_NIGHTLY_TIMEOUT_S", 1800.0)
    return _env_float("FUSED_RENDER_SPECIALIST_TIMEOUT_S", 600.0)


# ---------------------------------------------------------------------- fence ---
def rule_path(path: str) -> str:
    """A path as a permission rule spells it: `//<absolute>` (one slash would be relative to the settings file)."""
    return "//" + path.replace(os.sep, "/").lstrip("/")


def spellings(path: str) -> list[str]:
    """The path and its realpath when they differ: the CLI matches a rule's text, and a tool may be handed either."""
    out = [os.path.abspath(path)]
    real = os.path.realpath(path)
    if real not in out:
        out.append(real)
    return out


def read_rules(dirs: Sequence[str]) -> list[str]:
    rules = []
    for d in dirs:
        if not d:
            continue
        for p in spellings(d):
            if _GLOB_CHARS & set(p):
                # The CLI has no escape for glob characters: such a folder is not granted rather than widened.
                logger.warning("specialists: folder %r has glob characters; not granted", d)
                break
            r = f"Read({rule_path(p.rstrip(os.sep) or os.sep)}/**)"
            if r not in rules:
                rules.append(r)
    return rules


def edit_rules(home: str, globs: Sequence[str]) -> list[str]:
    rules = []
    for g in globs:
        g = str(g or "").lstrip("/")
        if not g or ".." in g.split("/"):
            continue
        for h in spellings(home):
            r = f"Edit({rule_path(os.path.join(h, g))})"
            if r not in rules:
                rules.append(r)
    return rules


def claude_bin() -> str:
    explicit = os.environ.get("FUSED_RENDER_SPECIALIST_CLAUDE")
    if explicit:
        return explicit
    try:
        from fused_render.bots import claude_cli

        return claude_cli.runnable() or ""
    except Exception:  # noqa: BLE001
        return ""


def argv(spec, kind: str, *, read_dirs: Sequence[str] = (), write_globs: Sequence[str] = (),
         services: bool = False, servers: dict | None = None, binary: str = "claude") -> list[str]:
    """The `claude -p` argv of a side session (PLAN C.1 notes). `servers`: the MCP servers to list (only the
    nightly's services server today)."""
    tools = "Read,Glob,Grep" + (",Edit,Write" if write_globs else "")
    rules = read_rules(read_dirs) + edit_rules(spec.home, write_globs)
    if services and kind == "nightly" and servers:
        rules.append("mcp__specialist_services")
    out = [binary, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--setting-sources=project",
           "--model", spec.model or DEFAULT_MODEL, "--effort", spec.effort or DEFAULT_EFFORT,
           "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers or {}}, separators=(",", ":")),
           "--disable-slash-commands", "--tools", tools, "--permission-mode", "default"]
    if rules:
        out += ["--allowedTools", *rules]
    return out


def side_env() -> dict:
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


# -------------------------------------------------------------------- process ---
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _signal(proc, sig) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (AttributeError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _wait_all(procs, deadline: float) -> list:
    left = []
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            left.append(p)
    return left


def _kill(proc) -> None:
    for sig in (signal.SIGTERM, _SIGKILL):
        _signal(proc, sig)
        if not _wait_all([proc], time.monotonic() + 2):
            return


def _tool_line(blk: dict) -> str:
    inp = blk.get("input") if isinstance(blk.get("input"), dict) else {}
    what = (inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or "")
    return f"{blk.get('name') or 'tool'} {what}".strip()


def parse_stream(raw: str, out: dict) -> dict | None:
    """Fold a stream-json stdout into `out["transcript"]`; return the `result` event (None when there is none)."""
    result = None
    for ln in raw.splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for blk in (ev.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text" and (blk.get("text") or "").strip():
                    out["transcript"].append({"role": "specialist", "text": blk["text"].strip()[:LINE_MAX]})
                elif blk.get("type") == "tool_use":
                    out["transcript"].append({"role": "tool", "text": _tool_line(blk)[:300]})
        elif ev.get("type") == "result":
            result = ev
    return result


def _spawn(call: dict) -> dict:
    """Run ONE side-session process: `call` = {argv, cwd, env, prompt, timeout_s, kind, spec_id, row_id}.
    Returns `{"ok","answer","error","cost_usd","transcript"}`. The test seam."""
    out = {"ok": False, "answer": "", "error": "", "cost_usd": None,
           "transcript": [{"role": "prompt", "text": call["prompt"][:LINE_MAX]}]}
    proc = None
    try:
        with _GUARD:
            if _STATE["closed"]:
                out["error"] = CLOSED
                return out
            proc = subprocess.Popen(call["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=call["cwd"], env=call["env"],
                                    start_new_session=True)
            _PROCS.add(proc)
        try:
            raw, err = proc.communicate(call["prompt"].encode("utf-8"), timeout=call["timeout_s"])
        except subprocess.TimeoutExpired:
            _kill(proc)
            out["error"] = f"no answer after {int(call['timeout_s'])} s"
            return out
        result = parse_stream(raw.decode("utf-8", "replace"), out)
        if result is None and closed():
            out["error"] = CLOSED
            return out
        if result is None:
            tail = err.decode("utf-8", "replace").strip()[-400:]
            out["error"] = (f"Claude Code exited (code {proc.returncode}) without an answer"
                            + (f": {tail}" if tail else ""))
            return out
        if isinstance(result.get("total_cost_usd"), (int, float)):
            out["cost_usd"] = float(result["total_cost_usd"])
        for d in result.get("permission_denials") or []:
            if isinstance(d, dict):
                out["transcript"].append({"role": "refused", "text": _tool_line(
                    {"name": d.get("tool_name"), "input": d.get("tool_input")})[:300]})
        if result.get("subtype") == "success" and not result.get("is_error"):
            out["ok"], out["answer"] = True, str(result.get("result") or "").strip()
        else:
            out["error"] = str(result.get("result") or result.get("subtype") or "error")[:500]
        return out
    except OSError as e:
        out["error"] = f"could not start Claude Code: {e}"
        return out
    finally:
        if proc is not None:
            with _GUARD:
                _PROCS.discard(proc)


# ----------------------------------------------------------------------- rows ---
def _rows_dir(spec_id: str) -> str:
    return os.path.join(paths.app_dir(spec_id), "runs")


def new_row_id(started: float | None = None) -> str:
    started = started or time.time()
    return f"{int(started * 1000):013d}-{uuid.uuid4().hex[:6]}"


def save_row(spec_id: str, row: dict) -> dict:
    """Write a row (filling `id` when missing). Used here and for app rows such as history's `guard`."""
    if not row.get("id"):
        row["id"] = new_row_id(row.get("started"))
    hm.write_json(os.path.join(_rows_dir(spec_id), os.path.basename(row["id"]) + ".json"), row)
    return row


def _shown(row: dict) -> dict:
    if row.get("status") == "running":
        with _GUARD:
            live = row.get("id") in _RUNNING
        if not live:
            row = {**row, "status": "interrupted"}
    return row


def rows(spec_id: str, limit: int = 100) -> list[dict]:
    """Newest first, without transcripts; trims the folder to `ROWS_KEPT`."""
    d = _rows_dir(spec_id)
    try:
        names = sorted((n for n in os.listdir(d) if n.endswith(".json")), reverse=True)
    except OSError:
        return []
    out = []
    for n in names[:max(0, int(limit))]:
        r = hm.read_json(os.path.join(d, n), None)
        if isinstance(r, dict) and r.get("id"):
            out.append({k: v for k, v in _shown(r).items() if k != "transcript"})
    for n in names[ROWS_KEPT:]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass
    return out


def row(spec_id: str, rid: str) -> dict | None:
    rid = os.path.basename(str(rid or ""))
    r = hm.read_json(os.path.join(_rows_dir(spec_id), rid + ".json"), None) if rid else None
    return _shown(r) if isinstance(r, dict) else None


def extract_block(text: str, name: str) -> str | None:
    """The text between `<<<NAME` and `NAME>>>` (the LAST such block, trimmed of surrounding newlines)."""
    if not text or not name:
        return None
    opener, closer = "<<<" + name, name + ">>>"
    start = text.rfind(opener)
    if start < 0:
        return None
    body_start = start + len(opener)
    end = text.find(closer, body_start)
    if end < 0:
        return None
    return text[body_start:end].strip("\n").strip()


# ------------------------------------------------------------------------ run ---
def _services_servers(spec, kind: str) -> dict:
    try:
        from fused_render.specialists import launch

        return launch.session_servers(spec, kind, only=("specialist_services",))
    except Exception:  # noqa: BLE001
        logger.warning("specialists: no services server for %s", spec.id, exc_info=True)
        return {}


def run_side(spec, kind: Literal["check", "reviewer", "nightly"], prompt: str, *, trigger: dict,
             read_dirs: Sequence[str] = (), write_globs: Sequence[str] = (), services: bool = False,
             timeout_s: float | None = None) -> dict:
    """One side session, start to saved row. Never raises: a failure is a row with status `error`."""
    started = time.time()
    rec = {"id": new_row_id(started), "kind": kind, "started": started, "ended": None, "status": "running",
           "trigger": trigger if isinstance(trigger, dict) else {}, "transcript": [], "answer": ""}
    if kind not in KINDS:
        rec.update(status="error", ended=time.time(), error=f"unknown side-session kind {kind!r}")
        return rec
    gated = kind != "nightly"
    if closed() or (gated and not _SLOTS.acquire(timeout=SLOT_WAIT_S)):
        rec.update(status="error", ended=time.time(), error=CLOSED if closed() else BUSY)
        try:
            save_row(spec.id, rec)
        except OSError:
            pass
        return rec
    with _GUARD:
        _RUNNING.add(rec["id"])
    try:
        save_row(spec.id, rec)
        try:
            servers = _services_servers(spec, kind) if (services and kind == "nightly") else {}
            binary = claude_bin()
            if not binary:
                res = {"ok": False, "error": "the Claude Code CLI (`claude`) was not found", "answer": "",
                       "transcript": []}
            else:
                call = {"argv": argv(spec, kind, read_dirs=read_dirs, write_globs=write_globs, services=services,
                                     servers=servers, binary=binary),
                        "cwd": spec.home, "env": side_env(), "prompt": prompt,
                        "timeout_s": float(timeout_s) if timeout_s else timeout_for(kind),
                        "kind": kind, "spec_id": spec.id, "row_id": rec["id"]}
                res = _spawn(call)
        except Exception as e:  # noqa: BLE001
            logger.warning("specialists: %s session failed for %s", kind, spec.id, exc_info=True)
            res = {"ok": False, "error": f"{type(e).__name__}: {e}", "answer": "", "transcript": []}
        rec.update(ended=time.time(), status="done" if res.get("ok") else "error",
                   transcript=res.get("transcript") or [], answer=res.get("answer") or "")
        if res.get("error"):
            rec["error"] = res["error"]
        if res.get("cost_usd") is not None:
            rec["cost_usd"] = res["cost_usd"]
    finally:
        try:
            save_row(spec.id, rec)
        except OSError:
            logger.warning("specialists: could not save row %s", rec["id"], exc_info=True)
        with _GUARD:
            _RUNNING.discard(rec["id"])
        if gated:
            _SLOTS.release()
    return rec


def closed() -> bool:
    with _GUARD:
        return _STATE["closed"]


def reopen() -> None:
    with _GUARD:
        _STATE["closed"] = False


def shutdown(budget_s: float = 2.0) -> None:
    """No new side session starts; SIGTERM every running one, wait up to `budget_s` together, SIGKILL the rest."""
    with _GUARD:
        _STATE["closed"] = True
        procs = list(_PROCS)
    for p in procs:
        _signal(p, signal.SIGTERM)
    for p in _wait_all(procs, time.monotonic() + max(0.0, budget_s)):
        _signal(p, _SIGKILL)
