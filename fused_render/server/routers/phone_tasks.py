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

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import time

from fastapi import APIRouter, Body, Header, HTTPException, Query
from fastapi.responses import Response

from fused_render import schedule, tasks_store
from fused_render.server.common import _error, _require_fused

router = APIRouter()
logger = logging.getLogger(__name__)

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
TEXT_CAP = 20000      # a prompt or a reply: the phone draws it whole, but never a megabyte
_NO_AGENT = object()  # `_turns(agent=…)`: "look the module up" is the default; None is "there is none"


def _tasks():
    from fused_render.server.routers import tasks
    return tasks


def _flat(text, n=TEXT_MAX) -> str:
    return " ".join(str(text or "").split())[:n]


def _cut(text, n=DETAIL_MAX) -> str:
    """A cut that keeps line breaks (a tool's output, a prompt): CRs dropped,
    runs of blank lines folded, trailing space off each line, then `n` chars."""
    lines = [ln.rstrip() for ln in str(text or "").replace("\r", "").split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()[:n]


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
    # The folder a row is filed under is its project, else its target: the facet
    # (`phone_tasks`) and the phone's folder filter both read `project`, so the
    # same fallback is applied here and nowhere else.
    project = str(row.get("project") or row.get("target") or "")
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
        # The folder opens as an app: the grid's own door to its entry page, "" otherwise.
        "entry_url": ("/render?path=" + _q(str(row.get("entry")))) if row.get("entry") else "",
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


def _token(rows: list[dict], gen: int, archived: bool) -> str:
    """The list's change token: the snapshot generation AND a digest of what
    the rows say. The generation alone misses the builder's floor rebuilds
    (every `REBUILD_FLOOR_SEC`, same generation), which are what move the
    time-based fields — `live` off a transcript's age, `next_run`, queue
    positions (tasks.py `_GZ_CACHE` keys on the rows object for the same
    reason). `archived` is in it so a switch of the view is never "unchanged"."""
    h = hashlib.blake2b(digest_size=8)
    h.update(b"a" if archived else b"-")
    for r in rows:
        h.update(f"{r.get('key')}|{r.get('row_hash')}|{r.get('status')}|{int(bool(r.get('live')))}|"
                 f"{r.get('next_run')}|{r.get('queue_position')}|{r.get('unread')}\n".encode())
    return f"{gen}.{h.hexdigest()}"


def _shown(rows: list[dict], archived: bool) -> list[dict]:
    """The rows the phone lists (drafts dropped, archived on request), newest
    activity first."""
    out = [r for r in rows if not _is_draft(r) and (archived or r.get("status") != "archived")]
    out.sort(key=_activity, reverse=True)
    return out


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
    """The list: `{tasks, total, generation, projects, attention, ts}`, or
    `{unchanged: true, generation}` when `gen` is the token the last answer
    carried (`_token`: the snapshot generation plus a digest of the rows, so a
    floor rebuild that moved `live` or `next_run` is a change too). The
    unchanged answer costs the digest and nothing else: no filter, no sort, no
    row trimming. `projects` is the folder facet with counts over the rows
    shown; `attention` the needs-you count for the badge."""
    try:
        lim = min(LIMIT_MAX, max(1, _int(limit, LIMIT_DEFAULT, "limit")))
    except ValueError as exc:
        return _error(str(exc), 400)
    show_archived = _truthy(archived)
    all_rows, snapshot_gen = _tasks()._listing()
    generation = _token(all_rows, snapshot_gen, show_archived)
    if gen and gen == generation:
        return {"unchanged": True, "generation": generation}
    rows = _shown(all_rows, show_archived)
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
def phone_task_detail(key: str, since: str = Query(default=""), last: str = Query(default=""),
                      before: str = Query(default=""), limit: str = Query(default="")):
    """One task and its conversation: `{task, turns, cursor, first, total,
    reset, live, run_id, cards, card, scheduled, ts}`.

    `turns` are the transcript's (`_turns`), each with its index `i` and a
    stable `id`. With no `since`, the last `limit` turns. With
    `since=<cursor>&last=<id>`, every turn from `cursor - 1` on, capped at
    `limit` — the last turn the phone holds is re-sent so a reply still
    streaming grows in place (the phone replaces by `i`) — UNLESS the turn at
    `cursor - 1` is not the one the phone named (`_history` drops a resolved
    error turn, a "Stopped" mark moves on, the list may be shorter): then
    `reset: true` and the last `limit` turns, and the phone starts over. With
    `before=<i>`, the `limit` turns before that index ("Load earlier").
    `cursor` is the index after the last turn sent; `total` the count.

    `cards` are the live run's permission and question records, every poll
    (they carry their own verdicts, and they are few); `card` is the one whose
    buttons are live — the oldest undecided one the queue is not holding.
    `scheduled` are this session's messages not yet run. A `pending:` key has
    the scheduled message and no conversation."""
    try:
        lim = min(TURNS_MAX, max(1, _int(limit, TURNS_DEFAULT, "limit")))
        cur = _int(since, -1, "since")
        upto = _int(before, -1, "before")
    except ValueError as exc:
        return _error(str(exc), 400)
    row = _row(key)
    if row is None:
        return _error(f"no task with key {key!r}", 404)
    out = {"task": phone_task(row), "ts": time.time(), "live": False, "run_id": "",
           "cards": [], "card": None, "scheduled": [], "first": 0, "cursor": 0, "total": 0,
           "reset": False, "turns": []}
    tasks = _tasks()
    out["scheduled"] = [_scheduled(e) for e in tasks._entries_for({key}).get(key, [])
                        if str(e.get("state") or "") == schedule.PENDING]
    if tasks_store.pending_entry(key):
        return out
    session = str(row.get("session_id") or key)
    target = str(row.get("target") or row.get("project") or "")
    agent = tasks._agent_module()
    if agent is None or not target:
        return out
    try:
        # `_history` ends with `_history_live`'s own keys (live_run, permissions): one read, not two.
        hist = agent._history(target, session, inbox=False)
    except Exception:  # noqa: BLE001 — a transcript we cannot read is an empty conversation, not a 500
        logger.debug("phone task %s: history failed", key, exc_info=True)
        hist = {}
    if not isinstance(hist, dict):
        hist = {}
    turns = _turns(hist.get("turns") or [], agent=agent)
    n = len(turns)
    if upto >= 0:
        start, end = max(0, min(upto, n) - lim), min(upto, n)
    elif cur >= 0:
        start = max(0, cur - 1)
        stale = cur > n or (last and (start >= n or turns[start]["id"] != last))
        if stale:
            out["reset"] = True
            start = max(0, n - lim)
        end = min(n, start + lim)
    else:
        start, end = max(0, n - lim), n
    out["turns"] = turns[start:end]
    out["first"] = start
    out["cursor"] = n if upto >= 0 else end  # Load earlier never moves the forward cursor back
    out["total"] = n
    run_id = str(hist.get("live_run") or "")
    out["live"] = bool(run_id)
    out["run_id"] = run_id
    if run_id:
        cards = [_card(agent, p) for p in (hist.get("permissions") or []) if isinstance(p, dict)]
        out["cards"] = cards
        out["card"] = next((c for c in cards if c["verdict"] == "pending"), None)
    return out


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
    return f"{name}: {detail}" if detail and not detail.startswith(name + ":") else (detail or name)


def _seg_text(seg: dict) -> str:
    t = seg.get("text")
    if isinstance(t, list):
        t = "".join(str(x) for x in t)
    return str(t or "")


def _turns(hist_turns: list, agent=_NO_AGENT) -> list[dict]:
    """The history's turns reduced to the phone's vocabulary (PhoneTurn), each
    stamped with its index `i` and a stable `id` (the transcript record's uuid
    and the segment's place in it; an error turn, which has no uuid, by its
    text):

    user       a prompt — `text`, `at`
    assistant  reply prose (markdown-lite on the phone) — `text`, `at`
    tool       one call and its result — `summary`, `name`, `status`, `detail` (600 chars)
    system     an error, an interruption, a compaction, a notice — `text`, `tone`

    An assistant turn whose segments interleave prose and tool calls becomes
    one phone turn per run of prose and one per tool call, in order. Thinking,
    subagent children, images and context meters are dropped. `agent` is the
    agent module for the desktop's tool summaries (looked up when not given);
    None means there is none, and the fallback summaries answer."""
    out: list[dict] = []
    if agent is _NO_AGENT:
        try:
            agent = _tasks()._agent_module()
        except Exception:  # noqa: BLE001
            agent = None

    def push(kind: str, uid: str, **fields):
        out.append({"i": len(out), "id": uid or f"n{len(out)}", "kind": kind, **fields})

    for t in hist_turns:
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        ts = t.get("ts")
        uid = str(t.get("uuid") or "")
        if role == "user":
            push("user", uid, text=_cut(t.get("text"), TEXT_CAP), at=ts)
        elif role == "assistant":
            segs = t.get("segments") if isinstance(t.get("segments"), list) else None
            if not segs:
                text = _cut(t.get("text"), TEXT_CAP)
                if text:
                    push("assistant", uid, text=text, at=ts)
                continue
            prose: list[str] = []
            prose_at = None
            k = 0  # the segment-run counter inside this record: part of the id

            def flush():
                nonlocal prose, prose_at, k
                text = _cut("".join(prose), TEXT_CAP)
                if text:
                    push("assistant", f"{uid}#{k}", text=text, at=prose_at)
                    k += 1
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
                    push("tool", f"{uid}#{k}", name=name, summary=_tool_summary(agent, name, inp),
                         status=status if status in ("running", "ok", "error") else "ok",
                         detail=_cut(output) if isinstance(output, str) else "",
                         at=seg.get("ts") or ts)
                    k += 1
                elif kind == "notice":
                    flush()
                    push("system", f"{uid}#{k}", text=_flat(seg.get("text"), DETAIL_MAX),
                         tone=str(seg.get("status") or "info"), at=seg.get("ts") or ts)
                    k += 1
                # thinking and anything newer: dropped
            flush()
            if t.get("stopped"):
                push("system", f"{uid}#stop", text="Stopped", tone="info", at=ts)
        elif role == "error":
            text = _flat(t.get("text"), DETAIL_MAX)
            push("system", "err:" + hashlib.blake2b(text.encode(), digest_size=6).hexdigest(),
                 text=text, tone="error", at=ts)
        elif role == "compact":
            push("system", uid or "compact", text="Context compacted", tone="info", at=ts)
    return out


def _card(agent, perm: dict) -> dict:
    """A permission record as a phone card (PhoneCard). `kind` is "question" for
    AskUserQuestion (its `questions` ride along), "plan" for ExitPlanMode (the
    plan is the detail), else "permission". `verdict` is allow, deny, expired,
    held or pending: a held card is ANSWERED (the project queue is holding the
    answer until the folder frees), so it is never the live one."""
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
    if decision in ("allow", "deny"):
        verdict = decision
    elif decision:
        verdict = "expired"
    else:
        verdict = "held" if perm.get("held") else "pending"
    detail = ""
    if kind == "plan":
        detail = _cut(inp.get("plan"))
    elif tool == "Bash":
        detail = _cut(inp.get("command"))
    elif tool in _FILE_TOOLS:
        detail = _flat(_tilde(inp.get("file_path")), DETAIL_MAX)
    questions = None
    if kind == "question":
        questions = []
        for q in inp.get("questions") or []:
            if not isinstance(q, dict):
                continue
            # The question and label strings go out EXACTLY as asked: an answer
            # is matched to them by the agent (`_answers_from`), and a cut or
            # a folded space would make every answer a deny.
            questions.append({
                "header": _flat(q.get("header"), 60),
                "question": str(q.get("question") or ""),
                "multiSelect": bool(q.get("multiSelect")),
                "options": [{"label": str(o.get("label") or ""), "description": _flat(o.get("description"))}
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
        # Keyed by the exact question text and carrying the exact joined labels, as
        # `questions` above: the phone looks an answer up by the question it drew.
        "answers": ({str(k): str(v) for k, v in list(perm.get("answers").items())[:20]}
                    if isinstance(perm.get("answers"), dict) else {}),
    }


# --------------------------------------------------------------- writes ---
#: The naming prompt the desktop hero sends to the relay (apps/builder/HomeHero.tsx
#: NAME_SYSTEM_PROMPT), here so the phone makes one call. Keep the two the same.
NAME_NONE = "none"
NAME_MODEL = "haiku"
NAME_SYSTEM_PROMPT = (
    "You name software projects. Given a description of an app, reply with a "
    "short folder name for it in kebab-case: 2-3 lowercase words joined by "
    "hyphens, letters and digits only, at most 4 words when 3 cannot say it.\n"
    "Rules:\n"
    "- Lead with the subject (what it is about), then what it does: "
    "\"expense-tracker\", not \"track-expenses\".\n"
    "- Use words from the description or plain words for what the app does. "
    "Never invent brand words or puns.\n"
    "- No filler: never app, tool, my, simple, new, basic, project, helper, "
    "manager (unless the thing IS a manager), or the user's name.\n"
    "- If the description names the app itself, use that name, kebab-cased.\n"
    "- If the text is not a description of an app to build, reply with "
    "exactly: " + NAME_NONE + "\n"
    "Examples:\n"
    "\"a page that shows my monthly spending by category\" -> spending-by-category\n"
    "\"something to track which plants I watered and when\" -> plant-watering-log\n"
    "\"a kanban board for my reading list, call it Shelf\" -> shelf\n"
    "\"hello, what can you do?\" -> " + NAME_NONE + "\n"
    "Reply with ONLY the name — no quotes, no prose, no explanation."
)
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_TIMEOUT_S = 12.0


def _guarded(fn):
    """Run a desktop handler for the phone: its `HTTPException` (the tasks
    router's own 400/404/409 and their sentences) becomes the door's
    `{"error": sentence}` with the same status, and anything else is handed
    through as it came (a dict, or a JSONResponse carrying its own error)."""
    try:
        return fn()
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("error") or json.dumps(detail)
        return _error(str(detail), exc.status_code)


def _running(row: dict) -> bool:
    return bool(row.get("live"))


RUN_BLOCKED_HINT = "Stop the run first"  # platform/lib/task-actions.ts, the same words


def _kebab(text: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-").split("-") if w]
    return "-".join(words[:4])


async def _suggest_name(prompt: str) -> str:
    """A kebab-case folder name for the app `prompt` describes, through the AI
    relay on haiku (the hero's own call, server-side), or "" when the relay is
    down, slow, or answers prose. The caller then falls back to a dated slug."""
    try:
        from fused_render.server import ai as ai_mod
        resp = await asyncio.wait_for(
            ai_mod._ai_relay({"prompt": prompt, "systemPrompt": NAME_SYSTEM_PROMPT, "model": NAME_MODEL}),
            _NAME_TIMEOUT_S)
        status, data = _body(resp)
        if status != 200:
            return ""
        text = str(((data.get("result") or {}).get("text")) or "").strip().lower().rstrip(". ")
    except Exception:  # noqa: BLE001 — naming is best effort; the slug below is the answer then
        logger.debug("phone new-app: naming failed", exc_info=True)
        return ""
    if not text or text == NAME_NONE or not _KEBAB_RE.match(text):
        return ""
    return _kebab(text)


def _dated_slug() -> str:
    return "app-" + _dt.datetime.now().strftime("%Y%m%d-%H%M")


def _free_name(name: str) -> str:
    """`name`, or `name-2`, `name-3`, … — the first the workspace does not hold.
    Only for a name the phone did NOT type: a typed one that is taken is a 409
    with the desktop's sentence, so the person can pick another."""
    from fused_render.shell.seed import fused_dir
    root = os.path.join(fused_dir(), "app")
    if not os.path.exists(os.path.join(root, name)):
        return name
    for i in range(2, 1000):
        if not os.path.exists(os.path.join(root, f"{name}-{i}")):
            return f"{name}-{i}"
    return f"{name}-{os.urandom(3).hex()}"


def _known_folder(target: str) -> str | None:
    """The folder `target` names if the phone may start a task there: a project
    of a row the listing knows (its own spelling, so the task joins that
    folder's queue — `project_queue` keys on the path as written, never its
    realpath), or a folder strictly under the workspace (`fused_dir()`) whose
    real location stays under it (a symlink out of the workspace is refused).
    Never the workspace root, the home dir, `/` or a dot-directory; never a
    path that is not a directory. Tilde'd paths are expanded; anything else is
    None. The phone picks from a list, it never types a path."""
    from fused_render.shell.seed import fused_dir
    t = str(target or "").strip()
    if not t:
        return None
    path = os.path.abspath(os.path.expanduser(t))
    home = os.path.abspath(os.path.expanduser("~"))
    if path in ("/", home) or not os.path.isdir(path):
        return None
    if any(seg.startswith(".") for seg in path.split(os.sep) if seg):
        return None
    root = os.path.abspath(fused_dir())
    if path.startswith(root + os.sep):
        real_root, real = os.path.realpath(root), os.path.realpath(path)
        if real == real_root or real.startswith(real_root + os.sep):
            return path
        return None
    rows, _gen = _tasks()._listing()
    known = {os.path.abspath(str(r.get("project"))) for r in rows if r.get("project")}
    known.discard(home)
    return path if path in known else None


@router.post(PREFIX)
def phone_create(body: dict = Body(...), x_fused: str | None = Header(default=None),
                 x_fused_device: str | None = Header(default=None)):
    """Start a task: `{prompt, target}` -> `{key, entry_id, task_id}`. `target`
    is a folder the listing knows or one under the workspace (`_known_folder`);
    anything else is 400. Stored `origin: "phone"`, in the asking mode ("prompt"),
    the Preferences default agent, no model or effort. The key is
    `pending:<entry>` until the run mints a session; the phone follows
    `entry_id` across the rekey."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("prompt: required", 400)
    folder = _known_folder(body.get("target"))
    if folder is None:
        return _error("target: pick a folder from the list", 400)
    tasks = _tasks()
    resp = _guarded(lambda: tasks._create_task(
        {"prompt": prompt, "target": folder, "permission_mode": "default"}, None, origin="phone"))
    status, data = _body(resp)
    if status != 200:
        return resp
    key = str(data.get("key") or "")
    row = _row(key)
    logger.info("phone task %s created from device %r", key, _device(x_fused_device))
    return {"key": key, "entry_id": str(data.get("entry_id") or ""),
            "task_id": str((row or {}).get("task_id") or "")}


@router.post(PREFIX + "/new-app")
async def phone_new_app(body: dict = Body(...), x_fused: str | None = Header(default=None),
                        x_fused_device: str | None = Header(default=None)):
    """Describe an app: `{prompt, name?}` -> `{key, entry_id, task_id, app: {name,
    path, url}}`. With no `name` the relay names it (`_suggest_name`), else a
    dated slug. Then the desktop's own `api_new_app`: copy the starter, git
    init, stamp the app id, start the build task on the entry html with the
    default agent. 409 with the desktop's sentence when the name is taken."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("prompt: required", 400)
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _error("name must be a string", 400)
    typed = (name or "").strip()
    suggested = "" if typed else (await _suggest_name(prompt) or _dated_slug())
    # Everything below touches the disk and the listing, and `_new_app` waits
    # for the task's send to resolve (up to 20 s): all of it off the event loop.
    return await asyncio.to_thread(_new_app_sync, typed or suggested, bool(typed), prompt, _device(x_fused_device))


def _new_app_sync(name: str, typed: bool, prompt: str, device: str):
    from fused_render.server.routers import apps
    base = name
    if not typed:
        name = _free_name(base)
    err = apps._app_name_error(name)
    if err is not None:
        return _error(err, 400)
    # The build task asks before anything risky ("prompt"), whatever the
    # scheduler's default for a scheduled message is, and is stamped phone.
    body = {"name": name, "prompt": prompt, "backend": ""}
    resp = _guarded(lambda: apps._new_app(body, permission_mode="prompt", origin="phone"))
    status, data = _body(resp)
    if status == 409 and not typed:
        # A name nobody typed was taken between the check and the copy (two
        # quick taps in one minute): the next free one, once.
        body["name"] = name = _free_name(base)
        resp = _guarded(lambda: apps._new_app(body, permission_mode="prompt", origin="phone"))
        status, data = _body(resp)
    if status != 200:
        return resp
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    entry_id = str(task.get("id") or "")
    session = str(task.get("claude_session_id") or "")
    key = session or (tasks_store.pending_key(entry_id) if entry_id else "")
    row = _row(key) if key else None
    path = str(data.get("path") or "")
    out = {"key": key, "entry_id": entry_id, "task_id": str((row or {}).get("task_id") or ""),
           "app": {"name": name, "path": _tilde(path),
                   "url": "/render?path=" + _q(str(data.get("entry_html") or ""))}}
    if data.get("task_error"):
        out["task_error"] = str(data["task_error"])
    logger.info("phone new app %r (%s) from device %r", name, key, device)
    return out


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="/")


def _send(key, body, _device, _row_):
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text: required")
    tasks = _tasks()
    return tasks.api_task_send(key, {"text": text}, x_fused="1")  # no model, effort or backend from the phone


def _cancel(key, _body, _device, _row_):
    return _tasks().api_task_cancel(key, x_fused="1")


NOT_WAITING = "that card is not waiting on an answer"


def _live_cards(row: dict) -> tuple[str, str, dict[str, dict]]:
    """`(run_id, session_id, {request id: permission row})` of the row's live
    run, the rows being the cards a phone may answer: undecided AND not held
    (a held card is answered; the queue delivers that answer when the folder
    frees, and a second answer from here would race it onto the disk first)."""
    key = str(row.get("key") or "")
    session = str(row.get("session_id") or key)
    target = str(row.get("target") or row.get("project") or "")
    agent = _tasks()._agent_module()
    if agent is None or not target:
        raise ValueError(NOT_WAITING)
    live = agent._history_live(target, session, inbox=False) or {}
    run_id = str(live.get("live_run") or "")
    pending = {str(p.get("id") or ""): p for p in (live.get("permissions") or [])
               if isinstance(p, dict) and not p.get("decision") and not p.get("held")}
    return run_id, session, pending


def _decide_with(row, body, decision, question: bool, answers=None, custom=None):
    request_id = str(body.get("request_id") or "")
    run_id, session, pending = _live_cards(row)
    perm = pending.get(request_id) if run_id else None
    if perm is None:
        raise ValueError(NOT_WAITING)
    agent = _tasks()._agent_module()
    answerable = str(perm.get("tool") or "") == getattr(agent, "ANSWERABLE_TOOL", "AskUserQuestion")
    if question and not answerable:
        raise ValueError("that card is a permission: allow or deny it")
    if not question and answerable:
        raise ValueError("that card is a question: answer it")
    raw = {"run_id": run_id, "request_id": request_id, "session_id": session,
           "decision": decision, "scope": "once", "note": str(body.get("note") or "")}
    if answers is not None:
        raw["answers"] = json.dumps(_answer_values(perm, answers, custom))
    if custom is not None:
        raw["custom"] = json.dumps(custom)
    return _tasks().api_queue_decide(raw, x_fused="1")


def _answer_values(perm: dict, answers: dict, custom) -> dict:
    """The answers as `agent._answers_from` wants them: one STRING per question
    — a label, or for a multi-select the ", "-join of the chosen labels in the
    card's option order with a typed "Other" last. The phone may send a string
    or a list of labels; anything else is a 400 here, not a silent deny there."""
    inp = perm.get("input") if isinstance(perm.get("input"), dict) else {}
    order: dict[str, list[str]] = {}
    for q in inp.get("questions") or []:
        if isinstance(q, dict) and isinstance(q.get("question"), str):
            order[q["question"]] = [o["label"] for o in (q.get("options") or [])
                                    if isinstance(o, dict) and isinstance(o.get("label"), str)]
    out = {}
    for text, value in answers.items():
        if isinstance(value, str):
            out[text] = value
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            labels = order.get(text, [])
            picked = [l for l in labels if l in value]
            typed = str((custom or {}).get(text) or "") if isinstance(custom, dict) else ""
            if typed and typed in value and typed not in picked:
                picked.append(typed)
            out[text] = ", ".join(picked) if picked else ", ".join(value)
        else:
            raise ValueError("answers: each value is a label or a list of labels")
    return out


def _decide(key, body, _device, row):
    """Allow once or deny the permission card the detail served. `scope` is
    forced to `once`: "for this session" and "always" are desktop decisions."""
    decision = body.get("decision")
    if decision not in ("allow", "deny"):
        raise ValueError("decision must be allow or deny")
    return _decide_with(row, body, decision, question=False)


def _answer(key, body, _device, row):
    """Answer a question card: `{request_id, answers: {question: label | [labels]},
    custom?: {question: typed}}` — the desktop composer's payload, joined into
    the strings the agent latches (`_answer_values`)."""
    answers = body.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ValueError("answers: required")
    custom = body.get("custom")
    if custom is not None and not isinstance(custom, dict):
        raise ValueError("custom must be an object")
    return _decide_with(row, body, "allow", question=True, answers=answers, custom=custom)


def _archive(key, _body, _device, row):
    if _running(row):
        return _error(RUN_BLOCKED_HINT, 409)
    tasks = _tasks()
    return tasks.api_task_archive(tasks.ArchivePatch(key=key), x_fused="1")


def _unarchive(key, _body, _device, _row_):
    tasks = _tasks()
    return tasks.api_task_unarchive(tasks.UnarchivePatch(key=key), x_fused="1")


def _rename(key, body, _device, _row_):
    tasks = _tasks()
    title = body.get("title", "")
    return tasks.api_task_rename(tasks.RenamePatch(key=key, title=title), x_fused="1")


def _favourite(key, body, _device, _row_):
    on = body.get("on")
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    tasks = _tasks()
    return tasks.api_task_favourite(tasks.FavouritePatch(key=key, on=on), x_fused="1")


def _delete(key, _body, _device, _row_):
    tasks = _tasks()
    return tasks.api_task_delete(tasks.DeletePatch(key=key), x_fused="1")  # 409 while running, its own words


def _read(key, _body, _device, _row_):
    tasks = _tasks()
    return tasks.api_task_read(tasks.ReadPatch(key=key, all=True), x_fused="1")


def _skip(key, _body, _device, _row_):
    return _tasks().api_queue_skip({"key": key}, x_fused="1")


#: Everything the phone may do to a task. Not here on purpose (docs/phone-tasks.md):
#: erase, export, worktrees, ports, holds, running/idle marks, settings, queue admit
#: and force, pr-status, seen, ui, snapshots, the terminal.
_OPS = {"send": _send, "cancel": _cancel, "decide": _decide, "answer": _answer,
        "archive": _archive, "unarchive": _unarchive, "rename": _rename,
        "favourite": _favourite, "delete": _delete, "read": _read, "skip": _skip}


@router.post(PREFIX + "/{key}/{op}")
def phone_op(key: str, op: str, body: dict = Body(default=None), x_fused: str | None = Header(default=None),
             x_fused_device: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    fn = _OPS.get(op)
    if fn is None:
        return _error(f"unknown task action {op!r}", 404)
    row = _row(key)
    if row is None:
        return _error(f"no task with key {key!r}", 404)
    try:
        return _guarded(lambda: fn(key, body if isinstance(body, dict) else {}, _device(x_fused_device), row))
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc) or exc.__class__.__name__, 400)
