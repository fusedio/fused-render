"""Name each process the sampler finds by joining fused-render's own registries.

Every registry is read defensively: an import that fails or a module whose
shape moved costs that registry's labels, never the sample. Each reader
returns `{pid: Owner}`; `Owner.ref` is what the stop endpoint needs to end the
process through its owner's own API instead of a bare signal.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Owner:
    kind: str       # app | claude | engine | model | terminal | install | run | other
    label: str
    ref: dict = field(default_factory=dict, compare=False, hash=False)
    # Claude trees only: `base` is the tree's own label ("TASK-075 · Fix the
    # bug"), `part` what this process is inside it ("MCP accesslint", "shell:
    # sleep 300"). A descendant's label is `base › part`, never a chain.
    base: str = field(default="", compare=False)
    part: str = field(default="", compare=False)


def _basename(path: str) -> str:
    return os.path.basename(str(path).rstrip("/\\")) or str(path)


def _short_model(model: str) -> str:
    return str(model).rstrip("/").rsplit("/", 1)[-1] or str(model)


def _template_roots() -> list[str]:
    """Every dir a core template can run from: the staged copy, the in-package
    source and the dev override."""
    from fused_render import core_templates
    roots = [core_templates.core_templates_dir(), core_templates.PACKAGE_TEMPLATES_DIR]
    override = os.environ.get("FUSED_RENDER_CORE_TEMPLATES")
    if override:
        roots.append(override)
    return [os.path.abspath(r) for r in roots]


def _template_of(path: str) -> str:
    """The template name when `path` is inside a core templates dir, else ""."""
    try:
        target = os.path.abspath(path)
        for root in _template_roots():
            rel = os.path.relpath(target, root)
            if not rel.startswith("..") and not os.path.isabs(rel) and rel != ".":
                return rel.split(os.sep, 1)[0]
    except Exception:  # noqa: BLE001
        pass
    return ""


def engine_label(folder: str, engine_id: str) -> str:
    if folder:
        tpl = _template_of(folder)
        return f"{tpl} template engine" if tpl else f"Engine: {_basename(folder)}"
    return f"Engine: {engine_id}"


def _engines(out: dict[int, Owner]) -> None:
    from fused_render.server import engine_host
    with engine_host._lock:
        children = list(engine_host._children.values())
    for c in children:
        pid = int(getattr(c, "pid", 0) or 0)
        if pid <= 0:
            continue
        engine_id = str(getattr(c, "engine_id", ""))
        folder = str(getattr(c, "folder", "") or "")
        if engine_id.startswith("bg_") and folder:
            label = f"App: {_basename(folder)}"
        else:
            label = engine_label(folder, engine_id)
        out[pid] = Owner("engine", label, {"engine_id": engine_id})


def _models(out: dict[int, Owner]) -> None:
    from fused_render.ai import supervisor
    with supervisor._lock:
        workers = list(supervisor._workers.values())
    for w in workers:
        pid = int(getattr(w, "pid", 0) or 0)
        if pid <= 0:
            continue
        model = str(getattr(w, "model", ""))
        out[pid] = Owner("model", f"Model: {_short_model(model)}",
                         {"model": model, "capability": getattr(w, "capability", None)})


def _apple(out: dict[int, Owner]) -> None:
    from fused_render.ai.apple import host
    with host._lock:
        procs = list(host._text_children)
    for p in procs:
        if getattr(p, "pid", None) and p.poll() is None:
            out[int(p.pid)] = Owner("model", "Model: Apple on-device", {"apple": True})


def _terminals(out: dict[int, Owner]) -> None:
    from fused_render import pty_session
    for s in pty_session.REGISTRY.list():
        proc = getattr(s, "proc", None)
        if proc is not None and getattr(s, "alive", False):
            out[int(proc.pid)] = Owner("terminal", "Terminal", {"terminal": s.id})


def _installs(out: dict[int, Owner]) -> None:
    from fused_render import envinstall
    for pid in list(envinstall._SPAWNED):
        out[int(pid)] = Owner("install", "Installing env", {})


def _runs(out: dict[int, Owner]) -> None:
    from fused_render import executor
    for pid, (folder, target) in executor.active_runs().items():
        out[int(pid)] = Owner("run", f"Run: {_basename(folder)} › {_basename(target)}",
                              {"path": target})


_READERS = (_engines, _models, _apple, _terminals, _installs, _runs)


def registry_owners() -> dict[int, Owner]:
    """pid -> Owner for every process a registry in this server tracks."""
    out: dict[int, Owner] = {}
    for reader in _READERS:
        try:
            reader(out)
        except Exception:  # noqa: BLE001 — one broken registry costs only its labels
            logger.debug("sysmon: %s failed", reader.__name__, exc_info=True)
    return out


# ---- Claude runs ------------------------------------------------------------
# A run's CLI is detached (setsid, reparented to init), so it is NOT under the
# server's process tree; the run dirs are how the sampler finds it. Both the
# session host and the CLI it spawns are reported, under the same label.

_CLAUDE_TTL_S = 5.0
_claude_cache: tuple[float, dict[int, Owner]] | None = None


def run_title(meta: dict) -> str:
    text = str(meta.get("title") or meta.get("message") or "").strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)
    if len(line) > 48:
        line = line[:47].rstrip() + "…"
    return line


def _is_claude_argv(args: list[str] | None) -> bool:
    """A Claude CLI or the session host that runs one, by its command line."""
    if not args:
        return False
    if any("session_host" in a for a in args[:4]):
        return True
    return any(_basename(a) == "claude" or _basename(a).startswith("claude-")
               for a in args[:3])


def _is_run_process(backend, pid: int, run_dir: str) -> bool:
    """Whether `pid` (read from `run_dir/pid`) is still that run's process and
    not a recycled pid: its argv must be a Claude CLI or session host, and it
    must have started no later than the pid file was written (a process that
    reuses the pid starts after the original exited, so after the write)."""
    if not _is_claude_argv(backend.proc_args(pid)):
        return False
    try:
        written = os.path.getmtime(os.path.join(run_dir, "pid"))
        started = backend.start_time(pid)
    except (OSError, AttributeError):
        return True
    return started is None or started <= written + 2.0


def _read_json(path: str) -> dict:
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bg_jobs(run_dir: str, row: dict) -> list[dict]:
    """The run's background jobs still running, each as `{id, description}` —
    what keeps a finished task's host alive. The task row's own count when
    there is a row (the Tasks page's answer, which also sees jobs the CLI has
    not written to `bg_tasks.json`), else the run dir's file."""
    held = row.get("background_jobs")
    if isinstance(held, dict):
        return [{"id": str(j.get("id") or ""), "description": str(j.get("title") or "")}
                for j in held.get("jobs") or () if isinstance(j, dict)]
    if row:
        return []
    tasks = _read_json(os.path.join(run_dir, "bg_tasks.json")).get("tasks")
    if not isinstance(tasks, dict):
        return []
    return [{"id": str(t.get("id") or k), "description": str(t.get("description") or "")}
            for k, t in tasks.items() if isinstance(t, dict) and t.get("status") == "running"]


def _claude_runs(backend) -> dict[int, Owner]:
    from fused_render import project_queue
    agent = project_queue.agent_module()
    if agent is None:
        return {}
    out: dict[int, Owner] = {}
    for run in project_queue.scan_runs(agent):
        pid_s = project_queue.run_pid(run["run_dir"])
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if backend.is_zombie(pid) or backend.proc_sample(pid) is None:
            continue
        if not _is_run_process(backend, pid, run["run_dir"]):
            continue  # the pid file outlived its process and the pid was reused
        meta = run.get("meta") or {}
        hostj = _read_json(os.path.join(run["run_dir"], "host.json"))
        sessions = _run_session_order(pid, run, hostj)
        row = _task_for(sessions)
        folder = str(meta.get("project") or meta.get("cwd") or "")
        if not folder:
            folder = str(_claude_session_row(pid).get("cwd") or "")
        label = task_label(row) if row else ""
        if not label:
            title = claude_base(run_title(meta), folder)
            label = f"Claude: {title}" if title else "Claude CLI"
        ref = {"run_id": run["run_id"], "task": _task_info(
            row, run, hostj, folder or str(meta.get("file") or ""), sessions)}
        owner = Owner("claude", label, ref, base=label)
        out[pid] = owner
        host = backend.parent_pid(pid)
        if host and host > 1:
            args = backend.proc_args(host) or []
            if any("session_host" in a for a in args):
                out[host] = owner
    return out


def _run_session_order(pid: int, run: dict, hostj: dict) -> list[str]:
    """Every session id the run answers to, most specific first: the one the
    live CLI holds, the host's, the run's own (meta / launch.json / the
    `session` file), and LAST `resumed_from` — the parent conversation, which
    can be a different task's session."""
    meta = run.get("meta") or {}
    launch = _read_json(os.path.join(run["run_dir"], "launch.json"))
    parent = str(meta.get("resumed_from") or "")
    own = [s for s in (run.get("sessions") or ()) if s and s != parent]
    order = [_claude_session_id(pid), str(hostj.get("session_id") or ""),
             str(meta.get("session_id") or ""), str(launch.get("session_id") or ""),
             str(launch.get("new_session_id") or ""), *own, parent]
    out: list[str] = []
    for sid in order:
        if sid and sid not in out:
            out.append(sid)
    return out


def _task_info(row: dict | None, run: dict, hostj: dict, folder: str,
               sessions: list[str]) -> dict:
    """What the Monitor's "Claude tasks" strip shows for one run, and the
    fields the shell's `taskHref` needs to open its task. Read once per
    `claude_roots` refresh, so it is as fresh as the labels."""
    row = row or {}
    where = str(row.get("project") or row.get("target") or folder or "")
    status = str(row.get("status") or "")
    attention = bool(row.get("attention")) or status in ("blocked", "needs_attention")
    # A matched task that is neither running nor waiting on you, with its tree
    # still up: "done" for a finished one, "idle" for any other status
    # (queued, upcoming, …). Both read as "still alive" — the kill candidates.
    return {
        "run_id": run["run_id"],
        "session_id": str(row.get("session_id") or next((s for s in sessions if s), "")),
        "key": str(row.get("key") or ""),
        "task_id": str(row.get("task_id") or ""),
        "title": _trim(str(row.get("title") or "")) or run_title(run.get("meta") or {}),
        "project": str(row.get("project") or ""),
        "target": str(row.get("target") or ""),
        "entry": str(row.get("entry") or ""),
        "entry_origin": str(row.get("entry_origin") or ""),
        "canvas": row.get("canvas") or None,
        "folder": _basename(where) if where else "",
        "model": str(hostj.get("model") or row.get("model") or ""),
        "status": status,
        "state": ("" if not row else "waiting" if attention
                  else "running" if status == "in_progress"
                  else "done" if status in ("done", "archived") else "idle"),
        "bg_jobs": _bg_jobs(run["run_dir"], row),
    }


# ---- the task a Claude process belongs to ----------------------------------------
# The Tasks listing's last published snapshot, indexed by every session id a
# row answers to (its `key` — the first session — and its current
# `session_id`). Never builds a listing: before the first build, or with no
# tasks router at all, there is simply no task to name and the label falls
# back to the run's own title.

_task_cache: tuple[float, object, dict[str, dict]] | None = None


def _task_index(now: float | None = None) -> dict[str, dict]:
    global _task_cache
    now = time.monotonic() if now is None else now
    if _task_cache is not None and now - _task_cache[0] < _CLAUDE_TTL_S:
        return _task_cache[2]
    rows = None
    try:
        from fused_render.server.routers import tasks as tasks_router
        rows = tasks_router.snapshot_rows()
    except Exception:  # noqa: BLE001 — no listing costs only the task names
        logger.debug("sysmon: task listing unreadable", exc_info=True)
    if _task_cache is not None and rows is not None and rows is _task_cache[1]:
        index = _task_cache[2]
    else:
        index = {}
        for row in rows or ():
            for sid in (row.get("key"), row.get("session_id")):
                if sid and (sid not in index or sid == row.get("session_id")):
                    index[str(sid)] = row
    _task_cache = (now, rows, index)
    return index


def _task_for(sessions) -> dict | None:
    index = _task_index()
    return next((index[s] for s in sessions if s and s in index), None)


def _trim(text: str, cap: int = 48) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    return line[:cap - 1].rstrip() + "…" if len(line) > cap else line


def task_label(row: dict) -> str:
    """`TASK-075 · Continue after usage limit` — a task's id and title, the
    label every process of its Claude tree carries."""
    task_id = str(row.get("task_id") or "")
    title = _trim(str(row.get("title") or ""))
    return " · ".join(x for x in (task_id, title) if x)


def claude_roots(backend, now: float | None = None) -> dict[int, Owner]:
    """pid -> Owner for every live Claude run (CLI + its session host),
    cached for a few seconds: it reads run dirs off disk."""
    global _claude_cache
    now = time.monotonic() if now is None else now
    if _claude_cache is not None and now - _claude_cache[0] < _CLAUDE_TTL_S:
        return _claude_cache[1]
    try:
        found = _claude_runs(backend)
    except Exception:  # noqa: BLE001
        logger.debug("sysmon: claude run scan failed", exc_info=True)
        found = {}
    _claude_cache = (now, found)
    return found


def _claude_session_id(pid: int) -> str:
    try:
        from fused_render import tasks_watch
        return tasks_watch.session_for_pid(pid) or ""
    except Exception:  # noqa: BLE001
        return ""


def _claude_session_row(pid: int) -> dict:
    try:
        from fused_render import tasks_watch
        sid = _claude_session_id(pid)
        return (tasks_watch.registry_row(sid) if sid else None) or {}
    except Exception:  # noqa: BLE001
        return {}


def _claude_session_name(pid: int) -> str:
    """The label base for a `claude` no run dir claims: its task when the
    session is one, else the session's own title or folder."""
    sid = _claude_session_id(pid)
    row = _task_for([sid]) if sid else None
    if row and task_label(row):
        return task_label(row)
    row = _claude_session_row(pid)
    name = claude_base(str(row.get("name") or ""), str(row.get("cwd") or ""))
    return f"Claude: {name}" if name else ""


# The app's own one-shot model calls (server/ai.py's relay: titles, recaps,
# fused.ai text) run a `claude -p --no-session-persistence` under the app with
# no run dir and no session — and its cwd is the app bundle, which is what the
# folder fallback used to print ("Claude: resources-6e"). `--fork-session` is
# the heads-up side question (claude_agent/headsup.py), a fork of a real one.
HELPER_LABEL = "Claude: titles & recaps helper"


def is_claude_helper(args: list[str] | None) -> bool:
    return bool(args) and "--no-session-persistence" in args and "--fork-session" not in args


_HEX_ID = re.compile(r"\s*\b[0-9a-f]{32}\b")


def claude_base(title: str, folder: str = "") -> str:
    """The readable base of a Claude process label: the session title when it is
    short human text, else the project folder's basename, else "" (callers
    then label the process "Claude CLI").
    A title that starts with `<` or `[` is a machine-injected tag, not prose."""
    t = _HEX_ID.sub("", (title or "").strip()).strip(" :")
    if t and t[0] not in "<[" and len(t) <= 40:
        return t
    return _basename(folder) if folder else ""


# ---- by command line ----------------------------------------------------------

def _is_python(exe: str) -> bool:
    return exe.lower().startswith("python")


def _venv_project(exe_path: str) -> str:
    """The project folder name when the interpreter is `<proj>/.venv|venv/bin/python`."""
    parts = os.path.normpath(exe_path).split(os.sep)
    if len(parts) >= 4 and parts[-2] == "bin" and parts[-3] in (".venv", "venv"):
        return parts[-4]
    return ""


def _in_shared_venvs(exe_path: str) -> bool:
    try:
        from fused_render import projectenv
        root = os.path.abspath(projectenv.venvs_root())
        return os.path.abspath(exe_path).startswith(root + os.sep)
    except Exception:  # noqa: BLE001
        return False


def _parent_script(script: str) -> str:
    parent = _basename(os.path.dirname(script))
    return f"{parent}/{_basename(script)}" if parent else _basename(script)


def describe(args: list[str] | None) -> tuple[str, str]:
    """(kind, label) for a command line. kind is "template" for a script that
    lives in the core templates dir, else "other"."""
    if not args:
        return "other", "process"
    exe = _basename(args[0])
    if not _is_python(exe):
        return "other", exe
    rest = args[1:]
    for i, a in enumerate(rest):
        if a == "-m" and i + 1 < len(rest):
            module = rest[i + 1]
            if module.startswith("fused_render."):
                # fused_render.index.worker -> "fused-render index worker"
                tail = module[len("fused_render."):].replace(".", " ").replace("_", " ")
                return "other", f"fused-render {tail}"
            return "other", module
        if a == "-c":
            code = re.sub(r"\s+", " ", rest[i + 1] if i + 1 < len(rest) else "").strip()
            return "other", f'python -c "{code[:40]}"'
        if not a.startswith("-"):
            tpl = _template_of(a) if os.path.isabs(a) else ""
            if tpl:
                return "template", f"{tpl} template › {os.path.splitext(_basename(a))[0]}"
            proj = _venv_project(args[0])
            if proj:
                return "other", f"{proj} › {_basename(a)}"
            return "other", _parent_script(a) if (_in_shared_venvs(args[0]) or os.sep in a) \
                else _basename(a)
    return "other", exe


def command_name(args: list[str] | None) -> str:
    """A short human name for a command line: the script or module for a
    Python process, the executable's basename otherwise."""
    return describe(args)[1]


def _warm_spares() -> set[int]:
    """Pids of the idle interpreters the warm-run pool holds (SPEC PY-20)."""
    from .. import warm_run

    return set(warm_run.spare_pids())


def classify(pid: int, args: list[str] | None) -> Owner | None:
    """An Owner for a process recognisable from its command line alone."""
    if not args:
        return None
    exe = _basename(args[0])
    if pid in _warm_spares():
        return Owner("run", "Warm Python (idle)", {})
    if exe == "claude":
        if is_claude_helper(args):
            return Owner("claude", HELPER_LABEL, {"helper": True}, base=HELPER_LABEL)
        label = _claude_session_name(pid) or "Claude CLI"
        return Owner("claude", label, {}, base=label)
    if _is_python(exe) and any(_basename(a) == "_child.py" for a in args[1:4]):
        return Owner("run", "Run: Python", {})
    kind, label = describe(args)
    if kind == "template":
        return Owner("template", label, {})
    return None


def classify_template(args: list[str] | None) -> Owner | None:
    """`classify(...)` narrowed to what a caller needing only a template daemon
    wants: the same answer for a template, None for everything else, without the
    Claude session-name lookup (a file read) `classify` does for `claude`."""
    if not args:
        return None
    exe = _basename(args[0])
    if exe == "claude" or (_is_python(exe) and any(_basename(a) == "_child.py" for a in args[1:4])):
        return None
    kind, label = describe(args)
    return Owner("template", label, {}) if kind == "template" else None


# ---- Claude CLI count ---------------------------------------------------------

def claude_summary(clis: list[dict], procs: list[dict]) -> dict:
    """Roll the live Claude CLIs up for the Monitor. `clis` are the CLI
    processes themselves, `procs` every process considered (to find each CLI's
    subtree). Rows are `{pid, ppid, label, cpu, mem, fused}`; cpu may be None."""
    children: dict[int, list[dict]] = {}
    for p in procs:
        children.setdefault(p.get("ppid") or 0, []).append(p)

    def subtree(root: dict) -> tuple[float, int]:
        cpu, mem, seen, stack = 0.0, 0, {root["pid"]}, [root]
        while stack:
            p = stack.pop()
            cpu += p.get("cpu") or 0.0
            mem += p.get("mem") or 0
            for c in children.get(p["pid"], ()):
                if c["pid"] not in seen:
                    seen.add(c["pid"])
                    stack.append(c)
        return cpu, mem

    top, cpu_sum, mem_sum = [], 0.0, 0
    for c in clis:
        cpu, mem = subtree(c)
        cpu_sum += cpu
        mem_sum += mem
        top.append({"pid": c["pid"], "label": c.get("label") or "Claude",
                    "mem": mem, "cpu": round(cpu, 1)})
    top.sort(key=lambda r: -r["mem"])
    fused = sum(1 for c in clis if c.get("fused"))
    return {"total": len(clis), "fused": fused, "other": len(clis) - fused,
            "mem_bytes": mem_sum, "cpu_pct": round(cpu_sum, 1), "top": top[:5]}


# ---- inside a Claude tree -------------------------------------------------------
# What a process under a Claude run is, in a word or two, so a tree reads
# `TASK-075 › MCP accesslint`, `TASK-075 › permissions`, `TASK-075 › shell:
# sleep 300` instead of `Claude: fused-render › npm › node`.

_SHELLS = {"sh", "bash", "zsh"}


def _mcp_name(pkg: str) -> str:
    """`@accesslint/mcp@latest` -> accesslint, `next-devtools-mcp@1` ->
    next-devtools, `@modelcontextprotocol/server-filesystem` -> filesystem."""
    if pkg.startswith("@"):
        scope, _, rest = pkg[1:].partition("/")
        rest = rest.split("@", 1)[0]
        name = scope if rest in ("", "mcp", "server", "mcp-server") else rest
    else:
        name = pkg.split("@", 1)[0]
    for pre in ("mcp-server-", "server-", "mcp-"):
        if name.startswith(pre) and len(name) > len(pre):
            name = name[len(pre):]
    for suf in ("-mcp-server", "-mcp", "-server"):
        if name.endswith(suf) and len(name) > len(suf):
            name = name[:-len(suf)]
    return name


def _mcp_of(args: list[str]) -> str:
    """The MCP server an `npm exec <pkg>` / `npx <pkg>` / `node …/.bin/<name>`
    runs, or "". npm retitles itself, so argv[0] can be the whole line."""
    words = " ".join(args).split()
    if words[:2] == ["npm", "exec"] or (words and _basename(words[0]) == "npx"):
        rest = words[2:] if words[0] == "npm" else words[1:]
        pkg = next((w for w in rest if not w.startswith("-")), "")
        return _mcp_name(pkg) if pkg else ""
    if words and _basename(words[0]) == "node":
        script = next((w for w in words[1:] if not w.startswith("-")), "")
        if "/node_modules/.bin/" in script or "/_npx/" in script:
            return _mcp_name(_basename(script))
    return ""


_BEARER = re.compile(r"(?i)\b(bearer\s+)\S+")
_USERINFO = re.compile(r"(://)[^/\s@:]+:[^/\s@]+@")


def _redact_cmd(text: str) -> str:
    """A shell command as a label may show it: the router's flag / `name=value`
    rules (the same ones `args` gets), plus `Bearer <token>` and `user:pass@`
    in a URL. Applied where the label is made, so every route that shows the
    label — the table, the detail drawer's owner and parents — gets this text."""
    try:
        from fused_render.server.routers.system import REDACTED, redact_args
    except Exception:  # noqa: BLE001 — no redactor, no command text at all
        return ""
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, redact_args(text))
    return _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", text)


def _shell_snippet(args: list[str]) -> str:
    """The command a Bash-tool wrapper is running: the text Claude Code hands
    to `eval '…'`, minus a leading `cd <dir> &&` and the wrapper's tail."""
    line = " ".join(args)
    at = line.find("eval '")
    if at < 0:
        return ""
    cmd = line[at + len("eval '"):]
    cmd = re.sub(r"^cd \S+ && ", "", cmd.strip())
    cmd = re.sub(r"'\s*<\s*/dev/null.*$", "", cmd)   # the wrapper's own tail
    return _trim(_redact_cmd(cmd), 40)


def _claude_part(args: list[str] | None, parent_part: str) -> str:
    if not args:
        return parent_part or "process"
    exe = _basename(args[0].split(" ", 1)[0])
    line = " ".join(args[:3])
    if "claude_shell_prefix" in line or (exe in _SHELLS and "-c" in args[1:2]
                                         and "shell-snapshots" in " ".join(args)):
        snippet = _shell_snippet(args)
        return f"shell: {snippet}" if snippet else "shell"
    if exe == "tee" and "claude-cmds" in line:
        return "shell"
    if parent_part.startswith("shell"):
        cmd = _trim(_redact_cmd(" ".join([exe] + list(args[1:]))), 40)
        return f"shell: {cmd}" if cmd else "shell"
    if parent_part.startswith("MCP "):
        return parent_part
    mcp = _mcp_of(args)
    if mcp:
        return f"MCP {mcp}"
    if any(_basename(a) == "permission_server.py" for a in args[1:4]):
        return "permissions"
    return command_name(args)


def claude_child(parent: Owner, args: list[str] | None) -> Owner:
    """The Owner of an unclaimed process under a Claude tree process."""
    base = parent.base or parent.label
    part = _claude_part(args, parent.part)
    return Owner("claude", f"{base} › {part}", {}, base=base, part=part)


# ---- the Monitor's "Claude tasks" strip -------------------------------------------

# A helper's entry: the same shape as a run's, naming no task.
_NO_TASK = {"run_id": "", "session_id": "", "key": "", "task_id": "", "title": "",
            "project": "", "target": "", "entry": "", "entry_origin": "", "canvas": None,
            "folder": "", "model": "", "status": "", "state": "", "bg_jobs": []}


def claude_tasks(roots: dict[int, Owner], helpers: list[int], procs: list[dict]) -> list[dict]:
    """One entry per live Claude tree: each run (its session host, CLI and
    everything under them) and each app helper CLI, with memory and CPU summed
    over the whole tree. `roots` are the run processes (`claude_roots`),
    `helpers` the helper CLIs' pids, `procs` the sampled rows
    `{pid, ppid, cpuPct, memBytes, startedAt}`. Largest first."""
    by_pid = {p["pid"]: p for p in procs}
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.get("ppid") or 0, []).append(p["pid"])

    def tree(root: int) -> list[int]:
        seen, stack = {root}, [root]
        while stack:
            for c in children.get(stack.pop(), ()):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return sorted(seen)

    groups: dict[str, list[int]] = {}
    owners: dict[str, Owner] = {}
    for pid, owner in roots.items():
        run_id = str(owner.ref.get("run_id") or "")
        if run_id and pid in by_pid:
            groups.setdefault(run_id, []).append(pid)
            owners[run_id] = owner
    out = []

    def entry(root: int, owner: Owner, info: dict, helper: bool) -> dict:
        pids = [p for p in tree(root) if p in by_pid]
        cpus = [by_pid[p].get("cpuPct") for p in pids if by_pid[p].get("cpuPct") is not None]
        state = info.get("state") or ""
        return {**info, "label": owner.base or owner.label, "helper": helper,
                "root_pid": root, "started_at": by_pid[root].get("startedAt"),
                "pids": pids,
                "mem_bytes": sum(by_pid[p].get("memBytes") or 0 for p in pids),
                "cpu_pct": round(sum(cpus), 1) if cpus else None,
                "done_alive": state in ("done", "idle")}

    for run_id, pids in groups.items():
        # The session host is the CLI's parent: the tree starts at whichever
        # of the run's processes has no parent inside the run.
        root = next((p for p in pids if by_pid[p].get("ppid") not in pids), pids[0])
        owner = owners[run_id]
        out.append(entry(root, owner, dict(owner.ref.get("task") or {}), False))
    in_runs = {p for t in out for p in t["pids"]}
    for pid in helpers:
        if pid in by_pid and pid not in in_runs:
            out.append(entry(pid, Owner("claude", HELPER_LABEL), dict(_NO_TASK), True))
    out.sort(key=lambda t: (-t["mem_bytes"], t["root_pid"]))
    return out
