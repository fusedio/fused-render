"""`/api/phone/tasks/*` — the Tasks as the Fused Render iOS app sees them (docs/phone-tasks.md).

A FastAPI router included from fused_render/server/app.py beside the tasks
router. The LAN listener (lan.py) forwards this prefix, and not the desktop's
`/api/tasks/*` or `/api/claude/agent`, for a paired device, with
`X-Fused-Device: <device id>` stamped from the pairing record (a client-sent
one is dropped). On loopback the routes answer too (D3: loopback is trusted);
the header is then optional and the device is "".

Not the desktop page's routes: `GET /api/tasks` carries every row with its
three newest messages (a 1.7 MB answer on a busy machine), and the desktop's
verbs include erase, worktree removal, terminal hand-off, snapshot revert,
app-state reads and mode switches — Mac-screen and Mac-disk operations. The
door serves what a phone draws: trimmed rows (`phone_task`), a reduced
transcript (`_turns`), the one card whose buttons are live, and the verbs
below, nothing else.

Keys are the listing's: a session id, or `pending:<entry id>` for a message
that has not run yet. Errors are `{"error": "<sentence>"}`: an unknown key is
404, a refused verb 400 or 409 with the desktop handler's own sentence, a
verb the phone never gets 404, a POST without `X-Fused: 1` 403.
"""
from __future__ import annotations

import json
import os
import re
import time

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse, Response

from fused_render import schedule, tasks_store
from fused_render.server.common import _error, _require_fused

router = APIRouter()

PREFIX = "/api/phone/tasks"
TEXT_MAX = 200        # a row's title / last line
DETAIL_MAX = 600      # a tool's output or a card's body on a phone screen
LIMIT_DEFAULT = 300   # rows in the list (the owner's call: a phone list, not an archive)
LIMIT_MAX = 1000
TURNS_DEFAULT = 80    # the tail a task opens on
TURNS_MAX = 400
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # bots/phone.py's, the same pairing records
#: Row statuses that are "needs you" on the phone (the badge on the Tasks segment).
ATTENTION_STATUSES = ("needs_attention", "blocked")
#: The verbs the phone never gets. Named so the 404 is deliberate, not a typo.
DESKTOP_ONLY = frozenset({"erase", "export", "worktrees", "ports", "holds", "running", "idle",
                          "settings", "admit", "force", "pr-status", "seen", "ui", "snapshot",
                          "terminal", "revert"})


def _tasks():
    from fused_render.server.routers import tasks
    return tasks


def _flat(text, n=TEXT_MAX) -> str:
    return " ".join(str(text or "").split())[:n]


def _device(x_fused_device) -> str:
    """The paired device's id lan.py stamped; "" on loopback (or for anything that is not an id)."""
    v = str(x_fused_device or "").strip()
    return v if _DEVICE_RE.match(v) else ""


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True", "yes")


def _int(v, default, name):
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None


def _tilde(path) -> str:
    p = str(path or "")
    home = os.path.expanduser("~")
    if home and p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return "~" if p and p == home else p


def _label(path) -> str:
    return os.path.basename(str(path or "").rstrip("/\\")) or str(path or "")


def _body(resp):
    """A desktop handler's answer as a `(status, dict)` pair: a plain dict is 200, a
    JSONResponse carries its own status and JSON body."""
    if isinstance(resp, Response):
        try:
            data = json.loads(bytes(resp.body) or b"{}")
        except (TypeError, ValueError):
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {"result": data}
    return 200, resp if isinstance(resp, dict) else {"result": resp}


# ------------------------------------------------------------------- rows ---
def phone_task(row: dict) -> dict:
    """One list row (PhoneTask): the desktop `Task` cut to what the phone draws,
    every string capped. Paths are tilde'd; the labels are basenames."""
    project = str(row.get("project") or "")
    target = str(row.get("target") or "")
    lm = row.get("last_message") if isinstance(row.get("last_message"), dict) else None
    att = row.get("attention") if isinstance(row.get("attention"), dict) else None
    prs = [{"repo": str(p.get("repo") or ""), "number": p.get("number"), "url": str(p.get("url") or "")}
           for p in (row.get("prs") or []) if isinstance(p, dict)]
    return {
        "key": str(row.get("key") or ""),
        "task_id": str(row.get("task_id") or ""),
        "title": _flat(row.get("title")),
        "title_source": str(row.get("title_source") or ""),
        "status": str(row.get("status") or ""),
        "failed": bool(row.get("failed")),
        "blocked_reason": str(row.get("blocked_reason") or ""),
        "attention": ({"tool": str(att.get("tool") or ""), "summary": _flat(att.get("summary"))}
                      if att else None),
        "live": bool(row.get("live")),
        "unread": int(row.get("unread") or 0),
        "starred": bool(row.get("starred")),
        "project": _tilde(project),
        "project_label": _label(project),
        "target_label": _label(target),
        "entry": bool(row.get("entry")),  # the folder opens as an app
        "repo": str(row.get("repo") or ""),
        "prs": prs,
        "backend": str(row.get("backend") or ""),
        "model": str(row.get("model") or ""),
        "last_message": ({"role": str(lm.get("role") or ""), "text": _flat(lm.get("text")),
                          "at": lm.get("at")} if lm else None),
        "last_reply": _flat(row.get("last_reply")),
        "started": row.get("started"),
        "last_active": row.get("last_active"),
        "happened_at": row.get("happened_at"),
        "message_count": int(row.get("message_count") or 0),
        "next_run": row.get("next_run"),
        "next_run_repeats": bool(row.get("next_run_repeats")),
        "queue_position": row.get("queue_position"),
        "queue_ahead_title": _flat(row.get("queue_ahead_title")),
        "entry_id": str(row.get("entry_id") or ""),
        "bot": str(row.get("bot") or ""),
    }


def _is_draft(row: dict) -> bool:
    return row.get("kind") == "draft" or row.get("state") == "draft"


def _activity(row: dict) -> float:
    for k in ("happened_at", "last_active", "started"):
        try:
            v = float(row.get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v:
            return v
    return 0.0


def _rows(archived: bool) -> tuple[list[dict], int]:
    """The listing's rows the phone lists (drafts dropped, archived on request),
    newest activity first, and the snapshot generation they stand for."""
    rows, gen = _tasks()._listing()
    out = [r for r in rows if not _is_draft(r) and (archived or r.get("status") != "archived")]
    out.sort(key=_activity, reverse=True)
    return out, gen


def _row(key: str) -> dict | None:
    """One listing row by key (the snapshot's, or a narrowed build), or None."""
    tasks = _tasks()
    rows = tasks._task_rows(only=frozenset({key}))
    for r in rows:
        if r.get("key") == key and not _is_draft(r):
            return r
    return None


# ---------------------------------------------------------------- reads ---
@router.get(PREFIX)
def phone_tasks(gen: str = Query(default=""), archived: str = Query(default="0"),
                limit: str = Query(default="")):
    """The list: `{tasks, generation, projects, attention, ts}`, or
    `{unchanged: true, generation}` when `gen` is the snapshot's own generation
    (the poll then costs one comparison). `projects` is the folder facet with
    counts over the rows shown; `attention` the needs-you count for the badge."""
    try:
        lim = min(LIMIT_MAX, max(1, _int(limit, LIMIT_DEFAULT, "limit")))
    except ValueError as exc:
        return _error(str(exc), 400)
    rows, generation = _rows(_truthy(archived))
    if gen and gen == str(generation):
        return {"unchanged": True, "generation": generation}
    facet: dict[str, dict] = {}
    attention = 0
    for r in rows:
        if r.get("status") in ATTENTION_STATUSES:
            attention += 1
        project = str(r.get("project") or r.get("target") or "")
        if project:
            p = facet.setdefault(project, {"path": _tilde(project), "label": _label(project), "count": 0})
            p["count"] += 1
    projects = sorted(facet.values(), key=lambda p: (-p["count"], p["label"].lower()))
    return {"tasks": [phone_task(r) for r in rows[:lim]], "total": len(rows),
            "generation": generation, "projects": projects, "attention": attention,
            "ts": time.time()}


@router.get(PREFIX + "/{key}")
def phone_task_detail(key: str, since: str = Query(default=""), limit: str = Query(default="")):
    """One task and its conversation: `{task, turns, cursor, first, live, run_id,
    cards, card, scheduled, ts, deleted?}`.

    `turns` are the transcript's (`_turns`), each stamped with its index `i`.
    With no `since`, the last `limit` turns; with `since=<cursor>`, every turn
    from `cursor - 1` on — the last turn the phone holds is re-sent, so a reply
    still streaming grows in place (the phone replaces by `i`). `cards` are the
    live run's permission and question records, every poll (they carry their
    own verdicts, and they are few); `card` is the one whose buttons are live.
    `scheduled` are this session's messages not yet run. A `pending:` key has
    the scheduled message as its only turn and no conversation."""
    try:
        lim = min(TURNS_MAX, max(1, _int(limit, TURNS_DEFAULT, "limit")))
        cur = _int(since, -1, "since")
    except ValueError as exc:
        return _error(str(exc), 400)
    row = _row(key)
    if row is None:
        return _error(f"no task with key {key!r}", 404)
    out = {"task": phone_task(row), "ts": time.time(), "live": False, "run_id": "",
           "cards": [], "card": None, "scheduled": [], "first": 0, "cursor": 0, "turns": []}
    entry_id = tasks_store.pending_entry(key)
    if entry_id:
        entry = _entry(entry_id)
        if entry is not None:
            out["scheduled"] = [_scheduled(entry)]
        return out
    tasks = _tasks()
    task = tasks._collect().get(key)
    if task is None:
        return out
    session = str(task.get("session_id") or key)
    tasks._place(task)
    target = str(task.get("target") or task.get("project") or "")
    out["scheduled"] = [_scheduled(e) for e in task.get("entries") or []
                        if str(e.get("state") or "") == schedule.PENDING]
    agent = tasks._agent_module()
    if agent is None or not target:
        return out
    try:
        hist = agent._history(target, session)
    except Exception:  # noqa: BLE001 — a transcript we cannot read is an empty conversation, not a 500
        hist = {}
    if not isinstance(hist, dict):
        hist = {}
    turns = _turns(hist.get("turns") or [])
    if hist.get("deleted") or (not turns and tasks_store.erased(session)):
        out["deleted"] = True
    n = len(turns)
    start = max(0, cur - 1) if cur >= 0 else max(0, n - lim)
    out["turns"] = turns[start:]
    out["first"] = start
    out["cursor"] = n
    try:
        live = agent._history_live(target, session, inbox=False)
    except Exception:  # noqa: BLE001
        live = {}
    run_id = str((live or {}).get("live_run") or "")
    out["live"] = bool(run_id)
    out["run_id"] = run_id
    if run_id:
        cards = [_card(agent, p) for p in (live.get("permissions") or []) if isinstance(p, dict)]
        out["cards"] = cards
        out["card"] = next((c for c in cards if c["verdict"] == "pending"), None)
    return out


def _entry(entry_id: str) -> dict | None:
    for e in schedule.list_entries():
        if str(e.get("id") or "") == entry_id:
            return e
    return None


def _scheduled(entry: dict) -> dict:
    return {"entry_id": str(entry.get("id") or ""), "text": _flat(entry.get("message"), DETAIL_MAX),
            "due": entry.get("due"), "state": str(entry.get("state") or ""),
            "repeats": str(entry.get("repeats") or "")}


# ---------------------------------------------------------------- turns ---
_FILE_TOOLS = ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit")


def _tool_summary(agent, name: str, inp: dict) -> str:
    """One line for a tool call: the desktop's own (`agent._tool_detail`) where it
    has one, else the tool's name and its first argument."""
    detail = ""
    if agent is not None and hasattr(agent, "_tool_detail"):
        try:
            detail = str(agent._tool_detail(name, inp) or "")
        except Exception:  # noqa: BLE001
            detail = ""
    if not detail:
        if name == "Bash":
            detail = str(inp.get("description") or inp.get("command") or "")
        elif name in _FILE_TOOLS:
            detail = _label(inp.get("file_path") or inp.get("notebook_path") or "")
        elif name in ("Glob", "Grep"):
            detail = str(inp.get("pattern") or "")
        elif name in ("WebFetch", "WebSearch"):
            detail = str(inp.get("url") or inp.get("query") or "")
        elif name in ("Agent", "Task"):
            detail = str(inp.get("description") or inp.get("prompt") or "")
    detail = _flat(detail, 120)
    return f"{name}: {detail}" if detail and not detail.startswith(name) else (detail or name)


def _seg_text(seg: dict) -> str:
    t = seg.get("text")
    if isinstance(t, list):
        t = "".join(str(x) for x in t)
    return str(t or "")


def _turns(hist_turns: list, agent=None) -> list[dict]:
    """The history's turns reduced to the phone's vocabulary (PhoneTurn), each
    stamped with its index `i`:

    user       a prompt — `text`, `at`
    assistant  reply prose (markdown-lite on the phone) — `text`, `at`
    tool       one call and its result — `summary`, `name`, `status`, `detail` (600 chars)
    system     an error, an interruption, a compaction, a notice — `text`, `tone`

    An assistant turn whose segments interleave prose and tool calls becomes
    one phone turn per run of prose and one per tool call, in order. Thinking,
    subagent children, images and context meters are dropped. `agent` is the
    agent module for the desktop's tool summaries, None in a unit test."""
    out: list[dict] = []
    if agent is None:
        try:
            agent = _tasks()._agent_module()
        except Exception:  # noqa: BLE001
            agent = None

    def push(kind: str, **fields):
        out.append({"i": len(out), "kind": kind, **fields})

    for t in hist_turns:
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        ts = t.get("ts")
        if role == "user":
            push("user", text=str(t.get("text") or ""), at=ts)
        elif role == "assistant":
            segs = t.get("segments") if isinstance(t.get("segments"), list) else None
            if not segs:
                text = str(t.get("text") or "")
                if text.strip():
                    push("assistant", text=text, at=ts)
                continue
            prose: list[str] = []
            prose_at = None

            def flush():
                nonlocal prose, prose_at
                text = "".join(prose)
                if text.strip():
                    push("assistant", text=text, at=prose_at)
                prose, prose_at = [], None

            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                kind = seg.get("kind")
                if kind == "text":
                    if prose_at is None:
                        prose_at = seg.get("ts") or ts
                    prose.append(_seg_text(seg))
                elif kind == "tool":
                    flush()
                    name = str(seg.get("name") or "")
                    inp = seg.get("input") if isinstance(seg.get("input"), dict) else {}
                    status = str(seg.get("status") or "running")
                    output = seg.get("output")
                    push("tool", name=name, summary=_tool_summary(agent, name, inp),
                         status=status if status in ("running", "ok", "error") else "ok",
                         detail=_flat(output, DETAIL_MAX) if isinstance(output, str) else "",
                         at=seg.get("ts") or ts)
                elif kind == "notice":
                    flush()
                    push("system", text=_flat(seg.get("text"), DETAIL_MAX),
                         tone=str(seg.get("status") or "info"), at=seg.get("ts") or ts)
                # thinking and anything newer: dropped
            flush()
            if t.get("stopped"):
                push("system", text="Stopped", tone="info", at=ts)
        elif role == "error":
            push("system", text=_flat(t.get("text"), DETAIL_MAX), tone="error", at=ts)
        elif role == "compact":
            push("system", text="Context compacted", tone="info", at=ts)
    return out


def _card(agent, perm: dict) -> dict:
    """A permission record as a phone card (PhoneCard). `kind` is "question" for
    AskUserQuestion (its `questions` ride along), "plan" for ExitPlanMode (the
    plan is the detail), else "permission". `verdict` is allow, deny or pending;
    `held` means the project queue is holding the run's answer."""
    tool = str(perm.get("tool") or "")
    inp = perm.get("input") if isinstance(perm.get("input"), dict) else {}
    tasks = _tasks()
    try:
        att = tasks._attention_of(agent, perm) if agent is not None else {"tool": tool, "summary": ""}
    except Exception:  # noqa: BLE001
        att = {"tool": tool, "summary": ""}
    answerable = getattr(agent, "ANSWERABLE_TOOL", "AskUserQuestion")
    plan_tool = getattr(agent, "PLAN_TOOL", "ExitPlanMode")
    kind = "question" if tool == answerable else "plan" if tool == plan_tool else "permission"
    decision = str(perm.get("decision") or "")
    verdict = decision if decision in ("allow", "deny") else ("expired" if decision else "pending")
    detail = ""
    if kind == "plan":
        detail = _flat(inp.get("plan"), DETAIL_MAX)
    elif tool == "Bash":
        detail = _flat(inp.get("command"), DETAIL_MAX)
    elif tool in _FILE_TOOLS:
        detail = str(inp.get("file_path") or "")
    questions = None
    if kind == "question":
        questions = []
        for q in inp.get("questions") or []:
            if not isinstance(q, dict):
                continue
            questions.append({
                "header": _flat(q.get("header"), 60),
                "question": _flat(q.get("question"), DETAIL_MAX),
                "multiSelect": bool(q.get("multiSelect")),
                "options": [{"label": _flat(o.get("label"), 120), "description": _flat(o.get("description"))}
                            for o in (q.get("options") or []) if isinstance(o, dict)],
            })
    sev = str(perm.get("severity") or "")
    return {
        "request_id": str(perm.get("id") or ""),
        "kind": kind,
        "tool": tool,
        "summary": _flat(att.get("summary")),
        "detail": detail,
        "risk": {"severity": sev, "summary": _flat(perm.get("risk_summary"))} if sev else None,
        "verdict": verdict,
        "held": bool(perm.get("held")),
        "created_at": perm.get("created_at") or 0,
        "questions": questions,
        "answers": perm.get("answers") if isinstance(perm.get("answers"), dict) else {},
    }
