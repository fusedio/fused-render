"""Live CPU and memory of the processes fused-render runs (fused_render/sysmon),
and on request of every process on the machine.

`GET /api/system/activity` is an unguarded same-origin read, like
`GET /api/engines/running`; reading it is also what keeps the sampler awake
(it idles 15 s after the last read). `?scope=all` lists every process on the
machine instead of fused-render's own (the shell's /monitor page), and
`GET /api/system/activity/history?pids=1,2` returns those pids' last minute.

Two mutations, both behind the D3 X-Fused guard:
`POST /api/system/activity/stop` ends one of fused-render's OWN processes
through its owner's API (engine, model, Claude run, terminal);
`POST /api/system/activity/kill` signals any process the last whole-machine
sample listed (SIGTERM, or SIGKILL with `force: true`). Both take
`{pid, startedAt}` — the process as the client saw it — and answer 404
"process changed" if that pid has since become a different process.
Command lines in the GET have credential-looking values redacted.
"""
import re

from fastapi import APIRouter, Body, Header

from fused_render.server.common import _error, _require_fused
from fused_render.sysmon import sampler as sysmon_sampler

router = APIRouter()

_MAX_HISTORY_PIDS = 20

# Command lines are shown to anyone who can open the page, and other programs
# put credentials in theirs. A value after one of these flags, or in a
# `name=value` pair whose name says secret, is replaced; the flag stays.
_SECRET_FLAGS = {"--token", "--key", "--password", "--passwd", "--secret",
                 "--api-key", "--apikey", "--access-token", "--auth-token"}
_SECRET_PAIR = re.compile(
    r"(?i)(^|[\s&?])([\w.-]*(?:token|key|secret|password|passwd)[\w.-]*=)([^\s&]+)")
REDACTED = "•••"


def redact_args(args: str) -> str:
    """`args` with credential-looking values replaced by `•••`."""
    if not args:
        return args
    parts = args.split(" ")
    for i in range(1, len(parts)):
        if parts[i - 1].lower() in _SECRET_FLAGS and parts[i]:
            parts[i] = REDACTED
    return _SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + REDACTED, " ".join(parts))


@router.get("/api/system/activity")
def api_system_activity(scope: str = "fused"):
    body = sysmon_sampler.get_sampler().poll("all" if scope == "all" else "fused")
    for row in body["procs"]:
        if "args" in row:
            row["args"] = redact_args(row["args"])
    return body


@router.get("/api/system/activity/history")
def api_system_activity_history(pids: str = ""):
    try:
        wanted = [int(p) for p in pids.split(",") if p.strip()][:_MAX_HISTORY_PIDS]
    except ValueError:
        return _error("pids must be a comma-separated list of integers", status=400)
    return sysmon_sampler.get_sampler().proc_history(wanted)


def _pid_of(payload: dict):
    try:
        return int(payload.get("pid")), None
    except (TypeError, ValueError):
        return None, _error("pid must be an integer", status=400)


@router.post("/api/system/activity/stop")
def api_system_activity_stop(payload: dict = Body(...),
                             x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    pid, error = _pid_of(payload)
    if error is not None:
        return error
    try:
        how = sysmon_sampler.stop(sysmon_sampler.get_sampler(), pid,
                                  payload.get("startedAt"))
    except sysmon_sampler.StopRefused as refused:
        return _error(str(refused), status=refused.status)
    except OSError as e:
        return _error(f"could not stop process: {e}", status=400)
    return {"ok": True, "via": how}


@router.post("/api/system/activity/kill")
def api_system_activity_kill(payload: dict = Body(...),
                             x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    pid, error = _pid_of(payload)
    if error is not None:
        return error
    try:
        return sysmon_sampler.kill(sysmon_sampler.get_sampler(), pid,
                                   force=payload.get("force") is True,
                                   started_at=payload.get("startedAt"))
    except sysmon_sampler.StopRefused as refused:
        return _error(str(refused), status=refused.status)
    except OSError as e:
        return _error(f"could not signal process: {e}", status=400)
