"""The diagnostics bundle (SPEC §50): one zip a user can send when the app
misbehaved, built from files on disk alone.

`build_bundle()` writes `fused-render-diagnostics-<YYYYmmdd-HHMMSS>.zip` to
the Desktop (or `<home>/downloads` when there is no Desktop) and returns its
path. It must work with NO server running — the menubar item, the
`fused-render diagnostics` CLI and a wedged server all call it the same way —
so nothing here talks HTTP; every source is a file or a short subprocess.

**Never raises on a source.** A missing directory, an unreadable file, a
command that is not installed or times out: each becomes a `skipped` row in
`manifest.json` with a reason, and the bundle still gets written. A missing
optional file inside an otherwise-present collection (a run dir with no
`host.err.log`) is simply absent — a row per non-event would drown the rows
that matter.

**The window.** A file is collected when its mtime is at or after
`window_start = since_s or min(health.started_at(), now - 24 h)`: the whole
current server session or the last day, whichever reaches further back. From
the CLI `health.started_at()` is the CLI's own process start, so the default
there is always the last 24 h. Crash files are NOT windowed (they are tiny,
and a leftover empty one from a week ago is still evidence); DiagnosticReports
are.

**Caps.** `TOTAL_CAP` bounds the sum of SOURCE bytes collected (uncompressed —
predictable, and what a reader unpacks). Unbounded append logs are cut to
their last `TAIL_CAP` bytes. Sources are added in priority order — state and
app logs first, `calls/` last and newest-first — so when the cap bites it
bites the oldest call records.

**Privacy.** Logs are copied verbatim: file paths in them are the evidence.
Only `state/server.json` (tokens dropped), `state/prefs.json` (secret-looking
keys dropped, emails scrubbed) and each claude run's `meta.json` (the typed
`message` replaced by its length) are rewritten. Nothing user-authored leaves
a claude run dir (`out.jsonl` is the transcript). `EXCLUDED_NAMES` is the last
line of defence: `_add` refuses any source or arcname with a matching path
component, whatever the plan says.

`summarize_plan()` is the dry run behind the confirm dialog: it enumerates
the same file plan without running any command or writing anything.
"""
from __future__ import annotations

import json
import logging
import math
import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import zipfile

logger = logging.getLogger(__name__)

SCHEMA = 1
TOTAL_CAP = 64 * 1024 * 1024
TAIL_CAP = 512 * 1024
#: Session logs always collected regardless of the window: the live one, the
#: one before it (a crashed session) and one more for a restart loop.
ALWAYS_SESSIONS = 3
LOG_SHOW_CAP = 8 * 1024 * 1024
LOG_SHOW_TIMEOUT_S = 45
#: `log show` scans at a fixed cost per hour; past this span `resources.jsonl`
#: is the cheaper witness for memory pressure.
LOG_SHOW_MAX_SPAN_S = 2 * 3600
#: Slice size for the newest-first read; one slice is ~8 s on a busy machine.
LOG_SHOW_CHUNK_S = 30 * 60
CMD_TIMEOUT_S = 5
WINDOW_S = 24 * 3600
INDEX_RUNS = 5

#: Path components that never enter a bundle. Matched against every component
#: of both the source path and the arcname; an entry ending in `*` is a prefix.
EXCLUDED_NAMES = (
    "secrets.json",       # openfused/secrets.json
    "credentials*",       # openfused/credentials*
    # Retired mount feature: nothing writes this any more, but an upgraded
    # install still carries the user's remote credentials (the cleanup shim
    # deliberately leaves them alone), so it must never enter a bundle.
    "rclone.conf",        # rclone/rclone.conf
    "lan_tls",            # LAN TLS keys
    "claude-config",      # claude CLI config + auth
    "drafts.json",        # claude-sessions/drafts.json — half-typed messages
    "held_answers.json",
    "out.jsonl",          # claude run transcript
)

#: Process names whose crash reports / ps lines are ours.
_PS_NEEDLES = ("FusedRender", "fused_render", "fused-render", "fused-apple-ai",
               "claude")
_REDACT_KEY = re.compile(r"token|secret|key|password|credential", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ---- paths -----------------------------------------------------------------

def _home() -> str:
    from fused_render.shell.storage import home_dir
    return home_dir()


def _base_home() -> str:
    from fused_render.shell.storage import base_home_dir
    return base_home_dir()


def _default_out_dir() -> str:
    desktop = os.path.expanduser("~/Desktop")
    if os.path.isdir(desktop):
        return desktop
    return os.path.join(_home(), "downloads")


def _bundle_executable() -> str:
    """`CFBundleExecutable` of the bundle we run from, else "FusedRender".
    Read here rather than via `app.bundle_executable` — app.py imports
    uvicorn and AppKit-adjacent modules, and the CLI path must stay light."""
    exe = os.path.abspath(sys.executable)
    marker = ".app/Contents/MacOS/"
    if marker in exe:
        contents = exe[: exe.index(marker) + len(".app/Contents")]
        try:
            with open(os.path.join(contents, "Info.plist"), "rb") as f:
                name = plistlib.load(f).get("CFBundleExecutable")
            if isinstance(name, str) and name:
                return name
        except (OSError, plistlib.InvalidFileException, ValueError):
            pass
    return "FusedRender"


def _window_start(since_s: float | None, now: float) -> float:
    if since_s:
        return float(since_s)
    try:
        from fused_render import health
        started = health.started_at()
    except Exception:  # noqa: BLE001
        started = now
    return min(started, now - WINDOW_S)


def _excluded(path: str) -> bool:
    parts = re.split(r"[\\/]+", path)
    for part in parts:
        for name in EXCLUDED_NAMES:
            if name.endswith("*"):
                if part.startswith(name[:-1]):
                    return True
            elif part == name:
                return True
    return False


def _scandir(path: str):
    """`os.scandir` as a list; raises OSError for the caller to record."""
    with os.scandir(path) as it:
        return list(it)


def _mtime(entry_or_path) -> float:
    try:
        if isinstance(entry_or_path, os.DirEntry):
            return entry_or_path.stat().st_mtime
        return os.stat(entry_or_path).st_mtime
    except OSError:
        return 0.0


# ---- the plan --------------------------------------------------------------

class _Plan:
    """Ordered file items plus skipped rows. An item is
    `{arc, src, tail, transform}` where `transform` (optional) maps the
    source bytes to the bytes written (the redacting copies)."""

    def __init__(self, window_start: float):
        self.window_start = window_start
        self.items: list[dict] = []
        self.skipped: list[dict] = []
        self.crash_reports = 0
        self.boot_ids: list[str] = []

    def add(self, arc: str, src: str, *, tail: int | None = None, transform=None):
        self.items.append({"arc": arc, "src": src, "tail": tail, "transform": transform})

    def skip(self, source: str, reason: str):
        self.skipped.append({"source": source, "reason": reason})

    def listdir(self, path: str, label: str) -> list:
        """Entries of a collection root; a missing/unreadable root is one
        skipped row and an empty list."""
        try:
            return _scandir(path)
        except FileNotFoundError:
            self.skip(path, f"{label}: directory does not exist")
        except OSError as e:
            self.skip(path, f"{label}: {e.strerror or e}")
        return []

    def in_window(self, entry) -> bool:
        return _mtime(entry) >= self.window_start


def _redact_json(obj):
    if isinstance(obj, dict):
        return {k: _redact_json(v) for k, v in obj.items() if not _REDACT_KEY.search(str(k))}
    if isinstance(obj, list):
        return [_redact_json(v) for v in obj]
    if isinstance(obj, str):
        return _EMAIL.sub("<email>", obj)
    return obj


def _redact_prefs(data: bytes) -> bytes:
    return json.dumps(_redact_json(json.loads(data)), indent=2).encode()


def _redact_server_json(data: bytes) -> bytes:
    obj = json.loads(data)
    if isinstance(obj, dict):
        for k in ("token", "desktop_token"):
            obj.pop(k, None)
    return json.dumps(obj, indent=2).encode()


def _redact_claude_meta(data: bytes) -> bytes:
    """Keep the run's bookkeeping, drop the user's typed message."""
    obj = json.loads(data)
    if isinstance(obj, dict) and "message" in obj:
        msg = obj.pop("message")
        obj["message_chars"] = len(msg) if isinstance(msg, str) else None
    return json.dumps(obj, indent=2).encode()


def _plan_state(plan: _Plan) -> None:
    home = _home()
    srv = os.path.join(home, "server.json")
    if os.path.exists(srv):
        plan.add("state/server.json", srv, transform=_redact_server_json)
        try:
            with open(srv, encoding="utf-8") as f:
                bid = json.load(f).get("boot_id")
            if isinstance(bid, str) and bid:
                plan.boot_ids.append(bid)
        except (OSError, ValueError, AttributeError):
            pass
    else:
        plan.skip(srv, "server.json absent (no server running or clean shutdown)")
    for name in ("current_apps.json", "background_apps.json", "registered_apps.json",
                 "claude-health.json"):
        p = os.path.join(home, name)
        if os.path.exists(p):
            plan.add(f"state/{name}", p)
    prefs = os.path.join(home, "prefs.json")
    if os.path.exists(prefs):
        plan.add("state/prefs.json", prefs, transform=_redact_prefs)


def _plan_app_logs(plan: _Plan) -> None:
    from fused_render import logs

    prefix = logs.LOG_NAME_PREFIX + "-"
    seen: set[str] = set()
    dirs = [(logs.log_dir(), "log dir"), (logs.temp_log_dir(), "legacy temp log dir")]
    for directory, label in dirs:
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        legacy = label.startswith("legacy")
        entries = [e for e in plan.listdir(directory, label)
                   if e.name.startswith(prefix) and ".log" in e.name and e.is_file()]
        # THE NEWEST SESSIONS ARE ALWAYS IN, whatever the window. The window
        # trims the bulky collectors; session logs are the join key the
        # reading recipe greps first, and the one that matters most after a
        # crash-and-relaunch is the DEAD session's — whose mtime is before
        # "since the app started" by definition (bugbot, PR #1399).
        always = set()
        if not legacy:
            by_mtime = sorted(entries, key=_mtime, reverse=True)
            sessions_seen: list[str] = []
            for e in by_mtime:
                sid = e.name.split(".log", 1)[0].rsplit("-", 1)[-1]
                if sid not in sessions_seen:
                    sessions_seen.append(sid)
                if len(sessions_seen) > ALWAYS_SESSIONS:
                    break
                always.add(e.path)
        for e in entries:
            if e.path not in always and not plan.in_window(e):
                continue
            arc = f"app/legacy/{e.name}" if legacy else f"app/{e.name}"
            plan.add(arc, e.path, tail=TAIL_CAP)

    try:
        from fused_render import crashlog
        cdir = crashlog.crash_dir()
    except Exception as e:  # noqa: BLE001
        plan.skip("crash dir", f"could not resolve: {e}")
        cdir = None
    if cdir:
        for e in plan.listdir(cdir, "crash dir"):
            if e.name.endswith(".log") and e.is_file():
                plan.add(f"app/crash/{e.name}", e.path, tail=TAIL_CAP)

    try:
        from fused_render import health
        for arc, p in (("app/outages.jsonl", health.outages_path()),
                       ("app/resources.jsonl", health.resources_path())):
            if os.path.exists(p):
                plan.add(arc, p, tail=TAIL_CAP * 8)
            else:
                plan.skip(p, "absent")
    except Exception as e:  # noqa: BLE001
        plan.skip("health files", f"could not resolve: {e}")


def _plan_diagnostic_reports(plan: _Plan) -> None:
    if sys.platform != "darwin":
        return
    prefixes = (_bundle_executable(), "python", "Python", "fused-apple-ai")
    root = os.path.expanduser("~/Library/Logs/DiagnosticReports")
    for sub, arc_sub in ((root, ""), (os.path.join(root, "Retired"), "Retired/")):
        try:
            entries = _scandir(sub)
        except FileNotFoundError:
            continue
        except OSError as e:
            plan.skip(sub, f"DiagnosticReports: {e.strerror or e}")
            continue
        for e in entries:
            if not e.name.startswith(prefixes) or not e.is_file():
                continue
            if not plan.in_window(e):
                continue
            plan.add(f"os/DiagnosticReports/{arc_sub}{e.name}", e.path)
            plan.crash_reports += 1


def _plan_index(plan: _Plan) -> None:
    try:
        from fused_render.index.config import load_config
        runs = load_config().runs_dir
    except Exception as e:  # noqa: BLE001
        plan.skip("index runs", f"could not resolve: {e}")
        return
    dirs = [e for e in plan.listdir(runs, "index runs") if e.is_dir()]
    dirs.sort(key=_mtime, reverse=True)
    for d in dirs[:INDEX_RUNS]:
        for name, tail in (("worker.log", TAIL_CAP), ("events.jsonl", TAIL_CAP),
                           ("spec.json", None)):
            p = os.path.join(d.path, name)
            if os.path.isfile(p):
                plan.add(f"index/runs/{d.name}/{name}", p, tail=tail)


def _plan_ai_workers(plan: _Plan) -> None:
    # Reconstructed, not `ai.supervisor._worker_dir()`, which creates the dir.
    wdir = os.path.join(_home(), "ai", "workers")
    for e in plan.listdir(wdir, "ai workers"):
        if not e.is_file():
            continue
        if e.name.endswith(".json"):
            plan.add(f"ai/workers/{e.name}", e.path)
        elif e.name.endswith(".log"):
            plan.add(f"ai/workers/{e.name}", e.path, tail=TAIL_CAP)


def _walk_daemon_logs(root: str, depth: int):
    """Yield `daemon.log` paths under root, at most `depth` dirs down."""
    try:
        entries = _scandir(root)
    except OSError:
        return
    for e in entries:
        try:
            if e.is_file() and e.name == "daemon.log":
                yield e.path
            elif depth > 0 and e.is_dir(follow_symlinks=False) and e.name != "_env_install":
                yield from _walk_daemon_logs(e.path, depth - 1)
        except OSError:
            continue


def _plan_engines(plan: _Plan) -> None:
    home = _home()
    seen: set[str] = set()
    # Background apps: <home>/apps/<engine_id>/daemon.log
    for e in plan.listdir(os.path.join(home, "apps"), "background app dirs"):
        p = os.path.join(e.path, "daemon.log")
        if e.is_dir() and os.path.isfile(p):
            seen.add(os.path.realpath(p))
            plan.add(f"engines/{e.name}/daemon.log", p, tail=TAIL_CAP)
    # Template engine caches. Most hardcode `~/.fused-render/cache/<name>`
    # (not the branch-resolved home), so scan both cache roots.
    roots = []
    for r in (os.path.join(home, "cache"), os.path.join(_base_home(), "cache")):
        if os.path.realpath(r) not in {os.path.realpath(x) for x in roots}:
            roots.append(r)
    for root in roots:
        for p in _walk_daemon_logs(root, 3):
            real = os.path.realpath(p)
            if real in seen:
                continue
            seen.add(real)
            rel = os.path.relpath(os.path.dirname(p), root)
            eid = "cache-" + re.sub(r"[\\/]+", "-", rel)
            plan.add(f"engines/{eid}/daemon.log", p, tail=TAIL_CAP)


def _plan_envinstall(plan: _Plan) -> None:
    # Reconstructed, not `envinstall.progress_dir` (heavy import).
    root = os.path.join(_home(), "cache", "_env_install")
    for e in plan.listdir(root, "env installs"):
        if not e.is_dir() or not plan.in_window(e):
            continue
        for name, tail in (("worker.log", TAIL_CAP), ("progress.json", None)):
            p = os.path.join(e.path, name)
            if os.path.isfile(p):
                plan.add(f"envinstall/{e.name}/{name}", p, tail=tail)


def _claude_runs_root() -> str:
    """Mirror of `claude_agent/agent.py::_runs_root`. A mirror rather than an
    import on purpose: the diagnostics bundle must still collect the runs'
    logs when the agent module itself will not import, which is one of the
    failures a bundle exists to explain."""
    geteuid = getattr(os, "geteuid", None)
    suffix = "-%d" % geteuid() if geteuid is not None else ""
    return os.path.join(tempfile.gettempdir(), "fused_render_claude" + suffix, "runs")


def _plan_claude(plan: _Plan) -> None:
    for e in plan.listdir(_claude_runs_root(), "claude runs"):
        if not e.is_dir() or not plan.in_window(e):
            continue
        # Allow-list only: anything else in a run dir is user content.
        for name in ("err.log", "host.err.log"):
            p = os.path.join(e.path, name)
            if os.path.isfile(p):
                plan.add(f"claude/{e.name}/{name}", p, tail=TAIL_CAP)
        meta = os.path.join(e.path, "meta.json")
        if os.path.isfile(meta):
            plan.add(f"claude/{e.name}/meta.json", meta, transform=_redact_claude_meta)


def _plan_calls(plan: _Plan) -> None:
    try:
        from fused_render.calls import SUFFIX, store_dir
        root = store_dir()
    except Exception as e:  # noqa: BLE001
        plan.skip("calls store", f"could not resolve: {e}")
        return
    found = []
    # Exactly two levels (<root>/<partition>/<file>), suffix-filtered: on
    # Linux/Windows the app log dir is <root>/app and must not land twice.
    for part in plan.listdir(root, "calls store"):
        if not part.is_dir():
            continue
        try:
            entries = _scandir(part.path)
        except OSError as e:
            plan.skip(part.path, f"calls partition: {e.strerror or e}")
            continue
        for e in entries:
            if e.name.endswith(SUFFIX) and e.is_file() and plan.in_window(e):
                found.append((_mtime(e), part.name, e))
    found.sort(key=lambda t: t[0], reverse=True)  # newest first: cap bites oldest
    for _, pname, e in found:
        plan.add(f"calls/{pname}/{e.name}", e.path)


def _plan_desktop(plan: _Plan) -> None:
    if sys.platform == "darwin":
        return
    try:
        from fused_render.supervisor.paths import DesktopPaths
        logs_dir = str(DesktopPaths.discover().logs)
    except Exception as e:  # noqa: BLE001
        plan.skip("supervisor logs", f"could not resolve: {e}")
        return
    for name in ("server-console.log", "supervisor.log"):
        p = os.path.join(logs_dir, name)
        if os.path.isfile(p):
            plan.add(f"desktop/{name}", p, tail=TAIL_CAP)


def _build_plan(since_s: float | None, now: float) -> _Plan:
    plan = _Plan(_window_start(since_s, now))
    # Priority order: the cap bites whatever comes last.
    for step in (_plan_state, _plan_app_logs, _plan_diagnostic_reports, _plan_desktop,
                 _plan_index, _plan_ai_workers, _plan_engines, _plan_envinstall,
                 _plan_claude, _plan_calls):
        try:
            step(plan)
        except Exception as e:  # noqa: BLE001 — one broken source never sinks the bundle
            logger.debug("diagnostics plan step %s failed", step.__name__, exc_info=True)
            plan.skip(step.__name__.removeprefix("_plan_"), f"enumeration failed: {e}")
    return plan


def _cost(item: dict) -> int:
    try:
        size = os.path.getsize(item["src"])
    except OSError:
        return 0
    return min(size, item["tail"]) if item["tail"] else size


def summarize_plan(since_s: float | None = None) -> dict:
    """Dry run: what `build_bundle` would collect from files. Runs no command
    and writes nothing; `bytes` is pre-cap, post-tail source bytes."""
    plan = _build_plan(since_s, time.time())
    return {
        "files": len(plan.items),
        "bytes": sum(_cost(i) for i in plan.items),
        "window_start": plan.window_start,
        "crash_reports": plan.crash_reports,
    }


# ---- writing ---------------------------------------------------------------

class _Writer:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.total = 0
        self.collected: list[dict] = []
        self.skipped: list[dict] = []

    def _zinfo(self, arcname: str, mtime: float | None = None) -> zipfile.ZipInfo:
        t = time.localtime(mtime if mtime else time.time())
        date_time = max((1980, 1, 1, 0, 0, 0), tuple(t[:6]))
        info = zipfile.ZipInfo(arcname, date_time=date_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        return info

    def _guard(self, arcname: str, source: str) -> bool:
        if _excluded(source) or _excluded(arcname):
            self.skipped.append({"source": source, "reason": "excluded by EXCLUDED_NAMES"})
            return False
        return True

    def _room(self, n: int, source: str) -> bool:
        if self.total + n > TOTAL_CAP:
            self.skipped.append({"source": source,
                                 "reason": f"total cap {TOTAL_CAP} bytes reached"})
            return False
        return True

    def add(self, arcname: str, src: str, tail_bytes: int | None = None,
            transform=None) -> bool:
        """Copy `src` (its last `tail_bytes`, if set) into the zip."""
        if not self._guard(arcname, src):
            return False
        try:
            st = os.stat(src)
        except OSError as e:
            self.skipped.append({"source": src, "reason": e.strerror or str(e)})
            return False
        truncated = bool(tail_bytes) and st.st_size > tail_bytes
        n = min(st.st_size, tail_bytes) if tail_bytes else st.st_size
        if not self._room(n, src):
            return False
        try:
            with open(src, "rb") as f:
                if truncated:
                    f.seek(st.st_size - tail_bytes)
                    data = f.read(tail_bytes)
                    # Start at a line boundary so the first line is whole.
                    nl = data.find(b"\n")
                    if 0 <= nl < 4096:
                        data = data[nl + 1:]
                else:
                    data = f.read()
            if transform is not None:
                try:
                    data = transform(data)
                except Exception as e:  # noqa: BLE001
                    self.skipped.append({"source": src, "reason": f"redaction failed: {e}"})
                    return False
            self.zf.writestr(self._zinfo(arcname, st.st_mtime), data)
        except OSError as e:
            self.skipped.append({"source": src, "reason": e.strerror or str(e)})
            return False
        self.total += len(data)
        self.collected.append({"path_in_zip": arcname, "source": src,
                               "bytes": len(data), "truncated": truncated})
        return True

    def add_text(self, arcname: str, text: str, source: str = "generated") -> bool:
        if not self._guard(arcname, source):
            return False
        data = text.encode("utf-8", "replace")
        if not self._room(len(data), source):
            return False
        self.zf.writestr(self._zinfo(arcname), data)
        self.total += len(data)
        self.collected.append({"path_in_zip": arcname, "source": source,
                               "bytes": len(data), "truncated": False})
        return True


def _add(zf_writer: _Writer, arcname: str, src_path: str, tail_bytes: int | None = None,
         transform=None) -> bool:
    return zf_writer.add(arcname, src_path, tail_bytes, transform)


def _add_text(zf_writer: _Writer, arcname: str, text: str, source: str = "generated") -> bool:
    return zf_writer.add_text(arcname, text, source)


def _run(argv: list[str], timeout: float = CMD_TIMEOUT_S) -> str:
    """One command's output as a text section; failure is described inline."""
    head = "$ " + " ".join(argv) + "\n"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, check=False)
    except FileNotFoundError:
        return head + "(not found)\n"
    except subprocess.TimeoutExpired:
        return head + f"(timed out after {timeout:g}s)\n"
    except OSError as e:
        return head + f"({e})\n"
    out = r.stdout
    if r.stderr:
        out += "\n[stderr]\n" + r.stderr
    if r.returncode:
        out += f"\n[exit {r.returncode}]\n"
    return head + out + "\n"


def _memory_text() -> str | None:
    if sys.platform == "darwin":
        cmds = [["sysctl", "hw.memsize", "vm.swapusage"], ["memory_pressure"], ["vm_stat"]]
        return "".join(_run(c) for c in cmds)
    if sys.platform.startswith("linux"):
        text = _run(["free", "-b"])
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                text += "$ cat /proc/meminfo\n" + f.read()
        except OSError as e:
            text += f"/proc/meminfo: {e}\n"
        return text
    return None


def _ps_text() -> str | None:
    if sys.platform.startswith("win"):
        return None
    try:
        r = subprocess.run(["ps", "-axo", "pid,ppid,user,%cpu,%mem,rss,lstart,command"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=CMD_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f"ps failed: {e}\n"
    lines = r.stdout.splitlines()
    if not lines:
        return "ps produced no output\n" + r.stderr
    keep = [lines[0]] + [ln for ln in lines[1:] if any(n in ln for n in _PS_NEEDLES)]
    return "\n".join(keep) + "\n"


def _unified_log(w: _Writer, window_start: float, now: float, tmp_dir: str) -> None:
    """`log show` for memory kills, App Nap and our native helper.
    Streamed to a temp file beside the zip (never held in memory), tail-capped.

    THIS IS THE SLOW STEP, and the only one: every other collector together
    takes well under a second, while `log show` scans the unified log store
    at a fixed ~15 s per hour of window on a busy machine (measured
    2026-10-05: 17 s for 2 h, 35 s for 24 h). Three choices follow from that:

    * The span is capped at `LOG_SHOW_MAX_SPAN_S` regardless of the bundle's
      window — jetsam evidence older than that is in `resources.jsonl` anyway.
    * The predicate names only what the app log cannot know: the kernel's
      `memorystatus:` lines (jetsam kills — kernel sender, so `CONTAINS` is
      unavoidable), the memorystatus subsystem, RunningBoard assertions for
      the app (App Nap), and the Swift helper. NOT `process == "python"` or a
      bare `process == "FusedRender"`: measured at 7 MB and 27 MB per 2 h of
      WebKit chatter that buried the dozen lines that matter.
    * The window is read in `LOG_SHOW_CHUNK_S` slices, NEWEST FIRST, against
      one shared deadline. `log show` emits oldest-first, so a single query
      cut off by a timeout kept the start of the window and lost the recent
      lines the bundle exists for (bugbot, PR #1399). Chunking the other way
      round means a deadline costs the OLDEST slice. The file is still
      written in chronological order.
    """
    source = "log show"
    predicate = ('(sender == "kernel" AND eventMessage CONTAINS "memorystatus") OR '
                 'subsystem == "com.apple.memorystatus" OR '
                 '(process == "FusedRender" AND subsystem == "com.apple.runningboard") OR '
                 'process == "fused-apple-ai"')
    span = min(now - window_start, LOG_SHOW_MAX_SPAN_S)
    deadline = time.monotonic() + LOG_SHOW_TIMEOUT_S

    def fmt(t: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))

    chunks: list[bytes] = []  # newest first
    covered_from = now
    timed_out = False
    failed = False
    end = now
    while end > now - span:
        start = max(end - LOG_SHOW_CHUNK_S, now - span)
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            timed_out = True
            break
        argv = ["log", "show", "--start", fmt(start), "--end", fmt(end),
                "--style", "compact", "--predicate", predicate]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=remaining, check=False)
        except FileNotFoundError:
            w.skipped.append({"source": source, "reason": "`log` not found"})
            return
        except subprocess.TimeoutExpired as e:
            # Keep what this slice produced — within one slice the loss is the
            # slice's tail, bounded by LOG_SHOW_CHUNK_S.
            if e.stdout:
                chunks.append(e.stdout)
            timed_out = True
            break
        except OSError as e:
            w.skipped.append({"source": source, "reason": str(e)})
            return
        if proc.returncode != 0:
            # A failed slice must not read as "no memory events": record the
            # error and stop here — `covered_from` stays at the last slice
            # that actually answered, so the header and manifest say so.
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
            w.skipped.append({"source": f"{source} {fmt(start)}..{fmt(end)}",
                              "reason": f"exit {proc.returncode}: {err or 'no stderr'}"})
            failed = True
            break
        chunks.append(proc.stdout or b"")
        covered_from = start
        end = start

    if not chunks:
        if not failed:
            w.skipped.append({"source": source,
                              "reason": f"timed out after {LOG_SHOW_TIMEOUT_S}s "
                                        "before the first slice finished"})
        return
    fd, tmp = tempfile.mkstemp(prefix=".fused-render-logshow-", suffix=".txt", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            header = (f"# log show, {fmt(covered_from)} -> {fmt(now)}"
                      + (" (deadline hit: older slices not read)" if timed_out else "")
                      + (" (an older slice failed: see manifest skipped)" if failed else "")
                      + "\n").encode()
            out.write(header)
            for body in reversed(chunks):  # chronological
                out.write(body)
        if w.add("os/unified-log.txt", tmp, LOG_SHOW_CAP):
            w.collected[-1]["source"] = "log show --start ... --end ... '<predicate>'"
            w.collected[-1]["covered_from"] = covered_from
            if timed_out or failed:
                w.collected[-1]["truncated"] = True
                w.collected[-1]["note"] = (
                    (f"deadline {LOG_SHOW_TIMEOUT_S}s hit" if timed_out else "a slice failed")
                    + f"; covers {fmt(covered_from)} onward only")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _machine() -> dict:
    ram = None
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=CMD_TIMEOUT_S, check=False)
            ram = int(r.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram = int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError):
            pass
    return {"ram_bytes": ram, "cpu_count": os.cpu_count()}


def _install_method() -> str:
    """dmg / brew / appimage / pip / dev / unknown. Best effort."""
    try:
        if getattr(sys, "frozen", None) == "macosx_app":
            from fused_render.update import mac
            bundle = mac.bundle_path()
            if bundle is None:
                return "unknown"
            method = mac.detect_method(bundle)
            return method if method in ("brew", "dmg") else "unknown"
        if sys.platform.startswith("linux") and os.environ.get("APPIMAGE"):
            return "appimage"
        pkg = os.path.dirname(os.path.abspath(__file__))
        if os.path.exists(os.path.join(os.path.dirname(pkg), ".git")):
            return "dev"
        if "site-packages" in pkg or "dist-packages" in pkg:
            return "pip"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _installed_version() -> str | None:
    try:
        from fused_render.installed import installed_version
        return installed_version()
    except Exception:  # noqa: BLE001
        return None


def build_bundle(since_s: float | None = None, out_dir: str | None = None, *,
                 progress=None, reveal: bool = False, system_log: bool = True) -> str:
    """Write the diagnostics zip and return its path. See the module doc.

    `system_log=False` skips `log show` (macOS), the one step that costs
    more than a second: the Preferences button defaults it off and offers a
    checkbox, the menu-bar item and the CLI keep it on because a reporter's
    bundle is the one that must carry the jetsam evidence."""
    def say(msg: str) -> None:
        if progress is not None:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    now = time.time()
    out_dir = out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    name = "fused-render-diagnostics-" + time.strftime("%Y%m%d-%H%M%S",
                                                       time.localtime(now)) + ".zip"
    path = os.path.join(out_dir, name)
    part = os.path.join(out_dir, "." + name + ".part")

    say("Planning…")
    plan = _build_plan(since_s, now)

    try:
        from fused_render import __version__ as app_version
    except Exception:  # noqa: BLE001
        app_version = None
    try:
        from fused_render import health
        my_boot = health.boot_id()
    except Exception:  # noqa: BLE001
        my_boot = None

    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
            w = _Writer(zf)
            w.skipped.extend(plan.skipped)
            say(f"Collecting {len(plan.items)} files…")
            for item in plan.items:
                _add(w, item["arc"], item["src"], item["tail"], item["transform"])

            say("Sampling memory and processes…")
            mem = _memory_text()
            if mem is None:
                w.skipped.append({"source": "os/memory.txt", "reason": "not supported"})
            else:
                _add_text(w, "os/memory.txt", mem, "memory commands")
            ps = _ps_text()
            if ps is None:
                w.skipped.append({"source": "os/ps-tree.txt", "reason": "not supported"})
            else:
                _add_text(w, "os/ps-tree.txt", ps, "ps -axo …")

            if sys.platform == "darwin" and system_log:
                say("Reading the system log (15-45 s)…")
                _unified_log(w, plan.window_start, now, out_dir)
            elif sys.platform == "darwin":
                w.skipped.append({"source": "log show", "reason": "system log not requested"})

            say("Writing manifest…")
            manifest = {
                "schema": SCHEMA,
                "generated_at": now,
                "app_version": app_version,
                "installed_version": _installed_version(),
                "install_method": _install_method(),
                "platform": platform.platform(),
                "python": sys.version,
                "machine": _machine(),
                "boot_id": my_boot,
                "server_boot_ids": plan.boot_ids,
                "window": {"start": plan.window_start, "end": now},
                "crash_reports": plan.crash_reports,
                "total_bytes": w.total,
                "collected": w.collected,
                "skipped": w.skipped,
            }
            # The manifest is always written, outside the cap.
            zf.writestr(w._zinfo("manifest.json"), json.dumps(manifest, indent=2, default=str))
        os.replace(part, path)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise

    say(f"Saved {path}")
    if reveal and sys.platform == "darwin":
        try:
            subprocess.run(["open", "-R", path], timeout=CMD_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return path
