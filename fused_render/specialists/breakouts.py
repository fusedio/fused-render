"""Specialist breakouts opened from a task (docs/specialists.md §5).

    <home>/.specialist/breakouts/<bid>.json     the record
    <home>/.specialist/breakouts/<bid>/work/    its workroom: the session's working folder (docs/specialists.md §3)

A breakout is a fresh task-stack session (`specialist="<id>:breakout:<bid>"`) whose working folder is its private
workroom under the home, seeded with `BREAKOUT_SEED`. Running there, its file writes cannot land on memory,
knowledge, skills or people by relative path whatever tool it uses; the charter and skills still load natively and
the app copies memory and said.md in before each start and turn (`home.sync_workroom`). A record made before
workrooms (no `work` key) keeps the home as its folder. The page drives it: create -> (offer card / Breakout menu) -> the floating card sends the seed ->
Close sends `close_prompt` -> on turn end `closed` reads the SUMMARY and NOTE blocks, drafts the summary into the
task's composer (page) and files the note in the inbox. Discard keeps the session (the nightly job reads it).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid

logger = logging.getLogger(__name__)

BID_RE = re.compile(r"^b-[0-9a-f]{12}$")
STATUSES = ("open", "closed", "discarded", "deleted")
BRIEF_KEYS = ("problem", "tried", "look_at")
BRIEF_MAX = 1000
_LOCK = threading.RLock()

BREAKOUT_SEED = """Brief from the task "{parent_title}":
Problem: {problem}
Tried: {tried}
Look at: {look_at}

The task is still running in {parent_folder} and keeps working on everything else. Its transcript is \
{transcript_path}; you may read it and the folder. Prefer advising over changing the task's files; if you change \
any, list them. Leave commits and branches to the task. When you think you are done, say so and offer to close."""

_CLOSE_BODY = """Reply with exactly two blocks and nothing else.
<<<SUMMARY
Outcome: solved | workaround | reported | not solved — one line
Found: two or three lines
Do next: what the task should do next
Files changed: paths, or None
SUMMARY>>>
<<<NOTE
kind: gap | correction | convention | suspected-bug | behaviour | case
basis: user-said | verified | guess (if the user stated it to you in this conversation, the basis is user-said)
claim: one sentence
entry: m-xxxx or none
(then the full evidence: what was tried, what happened, what worked)
NOTE>>>"""
CLOSE_OUT = "The user is closing this breakout. " + _CLOSE_BODY
CLOSE_OUT_FOLLOWUP = ("The user is closing this breakout again. Summarise only what happened since your last "
                      "summary. " + _CLOSE_BODY)
_CLOSE_MARK = "The user is closing this breakout"


def _io():
    from fused_render.specialists import counts

    return counts.read_json, counts.write_json


def _dir(spec_id: str) -> str:
    from fused_render.specialists import paths

    return os.path.join(paths.app_dir(spec_id), "breakouts")


def _agent():
    from fused_render.claude_agent import agent_module

    return agent_module()


def _save(rec: dict) -> dict:
    _, write_json = _io()
    write_json(os.path.join(_dir(rec["specialist"]), rec["id"] + ".json"), rec)
    return rec


def get(spec_id, bid) -> dict | None:
    if not isinstance(bid, str) or not BID_RE.match(bid) or not isinstance(spec_id, str) or not spec_id:
        return None
    read_json, _ = _io()
    rec = read_json(os.path.join(_dir(spec_id), bid + ".json"))
    return rec if isinstance(rec, dict) else None


def for_spec(spec_id) -> list[dict]:
    read_json, _ = _io()
    out = []
    try:
        names = sorted(os.listdir(_dir(spec_id)))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and BID_RE.match(n[:-5]):
            rec = read_json(os.path.join(_dir(spec_id), n))
            if isinstance(rec, dict):
                out.append(rec)
    return sorted(out, key=lambda r: float(r.get("opened") or 0))


def open_now(spec_id) -> list[dict]:
    return [r for r in for_spec(spec_id) if r.get("status") == "open"]


def _cwd_of(spec_id: str, rec: dict) -> str:
    from fused_render.specialists import paths

    if rec.get("work"):
        return paths.workroom(spec_id, rec["id"])
    return paths.home(spec_id)


def cwd(spec_id: str, bid: str) -> str:
    """The working folder of breakout `bid` (its workroom; the home for a record made before workrooms). For an
    unknown breakout, the workroom path it would have."""
    from fused_render.specialists import paths

    rec = get(spec_id, bid)
    return _cwd_of(spec_id, rec) if rec is not None else paths.workroom(spec_id, bid)


def session_cwds(spec_id) -> dict[str, str]:
    """session_id -> the folder its transcript is keyed on, for every breakout of `spec_id` with a session."""
    return {r["session_id"]: _cwd_of(spec_id, r) for r in for_spec(spec_id)
            if isinstance(r.get("session_id"), str) and r["session_id"]}


def sync(spec, bid: str) -> str:
    """Refresh breakout `bid`'s workroom (what does not load there natively) and answer its folder."""
    from fused_render.specialists import home as hm

    rec = get(spec.id, bid)
    folder = _cwd_of(spec.id, rec) if rec is not None else ""
    if rec is not None and rec.get("work"):
        hm.sync_workroom(spec.home, folder)
    return folder


def session_ids(spec_id) -> dict[str, str]:
    """session_id -> status, for every breakout of `spec_id` that has started a session."""
    return {r["session_id"]: str(r.get("status") or "") for r in for_spec(spec_id)
            if isinstance(r.get("session_id"), str) and r["session_id"]}


def for_parent(parent_session_id: str) -> list[dict]:
    """Every breakout opened from task session `parent_session_id`, across specialists, with `name`."""
    if not parent_session_id:
        return []
    from fused_render.specialists import store

    out = []
    for spec in store.all():
        for rec in for_spec(spec.id):
            parent = rec.get("parent") if isinstance(rec.get("parent"), dict) else {}
            if parent.get("session_id") == parent_session_id:
                out.append(dict(rec, name=spec.name))
    return sorted(out, key=lambda r: float(r.get("opened") or 0))


def clean_brief(brief) -> dict:
    """`{problem, tried, look_at}` as strings capped at 1000 characters; ValueError when not an object or no problem."""
    if not isinstance(brief, dict):
        raise ValueError("the brief must be an object with problem, tried and look_at")
    out = {}
    for k in BRIEF_KEYS:
        v = brief.get(k, "")
        if not isinstance(v, str):
            raise ValueError(f"brief.{k} must be text")
        if len(v) > BRIEF_MAX:
            raise ValueError(f"brief.{k} is longer than {BRIEF_MAX} characters")
        out[k] = v.strip()
    if not out["problem"]:
        raise ValueError("the brief needs a problem")
    return out


def _transcript_path(file: str, session_id: str) -> str:
    if not file or not session_id:
        return "(not known)"
    try:
        agent = _agent()
        return agent._session_transcript_path(agent._workdir(file), session_id)
    except Exception:  # noqa: BLE001
        return "(not known)"


def seed(rec: dict) -> str:
    parent = rec.get("parent") or {}
    brief = rec.get("brief") or {}
    file = str(parent.get("file") or "")
    folder = file if not file or os.path.isdir(file) else os.path.dirname(file)
    return BREAKOUT_SEED.format(
        parent_title=str(parent.get("title") or "untitled task").replace('"', "'"),
        problem=brief.get("problem") or "(none)", tried=brief.get("tried") or "(nothing yet)",
        look_at=brief.get("look_at") or "(not given)", parent_folder=folder or "(not known)",
        transcript_path=_transcript_path(file, str(parent.get("session_id") or "")))


def start_params(rec: dict) -> dict:
    return {"specialist": f"{rec['specialist']}:breakout:{rec['id']}"}


def create(spec, *, parent: dict, brief: dict, origin: str, request_id: str = "") -> dict:
    """Open a breakout record (status open, no session yet). Raises ValueError on a bad body. `parent` must name
    the task's `session_id` and `file`."""
    if not isinstance(parent, dict) or not isinstance(parent.get("session_id"), str) or not parent["session_id"]:
        raise ValueError("parent.session_id is required")
    if not isinstance(parent.get("file"), str) or not parent["file"]:
        raise ValueError("parent.file is required")
    if origin not in ("offer", "hand"):
        raise ValueError('origin is "offer" or "hand"')
    from fused_render.specialists import paths

    if paths.spec_of_path(parent["file"]):
        raise ValueError("a breakout cannot open a breakout")
    rec = {"id": "b-" + uuid.uuid4().hex[:12], "specialist": spec.id, "origin": origin,
           "request_id": str(request_id or "")[:80],
           "parent": {"session_id": parent["session_id"][:128], "run_id": str(parent.get("run_id") or "")[:80],
                      "file": parent["file"], "title": str(parent.get("title") or "")[:200]},
           "brief": clean_brief(brief), "session_id": "", "status": "open", "opened": time.time(),
           "closes": [], "discarded": None, "work": True}
    with _LOCK:
        _save(rec)
    sync(spec, rec["id"])
    try:
        from fused_render.specialists import counts

        counts.bump(spec.id, "breakouts_opened")
    except Exception:  # noqa: BLE001
        logger.debug("specialists: count breakouts_opened", exc_info=True)
    from fused_render.specialists import task_events

    task_events.add(rec["parent"]["session_id"], "breakout_open", spec.id, spec.name, breakout_id=rec["id"])
    return rec


def set_session(spec_id: str, bid: str, session_id: str) -> None:
    """Record the session a breakout's first start minted (router post-hook)."""
    with _LOCK:
        rec = get(spec_id, bid)
        if rec is None or not session_id or rec.get("session_id") == session_id:
            return
        if not rec.get("session_id"):
            rec["session_id"] = session_id
            _save(rec)


def close_prompt(spec_id: str, bid: str) -> str:
    rec = get(spec_id, bid)
    if rec is None:
        raise LookupError(f"no such breakout: {bid}")
    return CLOSE_OUT_FOLLOWUP if rec.get("closes") else CLOSE_OUT


def _turn_text(turn: dict) -> str:
    text = str(turn.get("text") or "")
    if text.strip():
        return text
    segs = turn.get("segments") if isinstance(turn.get("segments"), list) else []
    return "\n".join(str(s.get("text") or "") for s in segs
                     if isinstance(s, dict) and s.get("kind") in (None, "", "text"))


def last_answer(home: str, session_id: str) -> str:
    """The breakout's answer to the latest close prompt (assistant text after it), else its last assistant text."""
    try:
        hist = _agent()._history(home, session_id)
    except Exception:  # noqa: BLE001
        return ""
    turns = [t for t in (hist.get("turns") if isinstance(hist, dict) else None) or [] if isinstance(t, dict)]
    start = None
    for i, t in enumerate(turns):
        if t.get("role") == "user" and _CLOSE_MARK in str(t.get("text") or ""):
            start = i
    after = turns[start + 1:] if start is not None else turns
    answers = [_turn_text(t) for t in after if t.get("role") == "assistant"]
    if start is None:
        answers = answers[-1:]
    return "\n".join(a for a in answers if a.strip())


def _fields(block: str) -> tuple[dict, str]:
    """(`key: value` header lines, the rest) of a NOTE block."""
    head, rest, done = {}, [], False
    for ln in block.splitlines():
        m = re.match(r"^\s*(kind|basis|claim|entry)\s*:\s*(.*)$", ln, re.I)
        if m and not done:
            head[m.group(1).lower()] = m.group(2).strip()
        else:
            if ln.strip():
                done = True
            rest.append(ln)
    return head, "\n".join(rest).strip()


def _pick(value: str, allowed, default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else default


def closed(spec, bid: str, session_id: str) -> dict:
    """The breakout's close-out turn ended: file its note, record the close, answer the draft for the composer."""
    from fused_render.specialists import harness, inbox, task_events

    rec = get(spec.id, bid)
    if rec is None:
        raise LookupError(f"no such breakout: {bid}")
    sid = session_id or rec.get("session_id") or ""
    answer = last_answer(_cwd_of(spec.id, rec), sid) if sid else ""
    # One close = one close-out answer: a retry or a restart that reads the same answer gets the same draft and
    # note back, and files nothing twice.
    import hashlib

    key = hashlib.sha1(f"{sid}\0{answer}".encode("utf-8")).hexdigest()[:10]
    label = f"Draft from the {spec.name} specialist · not sent yet"
    for done in rec.get("closes") or []:
        if isinstance(done, dict) and done.get("key") == key:
            return {"draft": done.get("draft") or "", "label": label, "note_id": done.get("note_id") or ""}
    summary = harness.extract_block(answer, "SUMMARY")
    note_block = harness.extract_block(answer, "NOTE")
    title = f"{spec.name} breakout: {(rec.get('brief') or {}).get('problem', '')[:80]}"
    draft = (summary.strip() if summary and summary.strip() else "Outcome: not given") \
        + f"\nFull session: {title}"
    if note_block:
        head, evidence = _fields(note_block)
        kind = _pick(head.get("kind"), inbox.KINDS, "case")
        basis = _pick(head.get("basis"), inbox.BASES, "guess")
        claim = head.get("claim") or ""
        entry = head.get("entry") if re.match(r"^m-[0-9a-f]{4,8}$", head.get("entry") or "") else None
    else:
        kind, basis, claim, entry, evidence = "case", "guess", "", None, answer[-4000:]
    claim = claim or f"Breakout on: {(rec.get('brief') or {}).get('problem', '')}"[:300]
    note_id = ""
    try:
        note = inbox.add(spec.id, kind=kind, claim=claim, basis=basis, evidence=evidence or answer[-4000:],
                         entry=entry, nid=f"n-close-{bid[2:]}-{key}",
                         source={"type": "breakout", "breakout_id": bid, "session_id": sid, "title": title})
        note_id = note.get("id", "")
    except Exception:  # noqa: BLE001 — the draft still reaches the task
        logger.exception("specialists: could not file a breakout's note")
    with _LOCK:
        rec = get(spec.id, bid) or rec
        for done in rec.get("closes") or []:  # a concurrent twin got here first
            if isinstance(done, dict) and done.get("key") == key:
                return {"draft": done.get("draft") or "", "label": label, "note_id": done.get("note_id") or ""}
        rec.setdefault("closes", []).append({"at": time.time(), "note_id": note_id, "draft": draft, "key": key})
        rec["status"] = "closed"
        if sid and not rec.get("session_id"):
            rec["session_id"] = sid
        _save(rec)
    task_events.add(rec["parent"]["session_id"], "breakout_closed", spec.id, spec.name, breakout_id=bid,
                    note_id=note_id, session_id=rec.get("session_id") or "")
    end_hosts(spec.id, rec)
    return {"draft": draft, "label": label, "note_id": note_id}


#: How long after Close / Discard the breakout's session host is ended (its Stop hook settles first).
END_DELAY_S = 2.0


def end_hosts(spec_id: str, rec: dict, delay_s: float | None = None) -> None:
    """End the breakout's session host, in the background: a closed or discarded breakout takes no new turn
    without Reopen. Claude Code starts a turn by itself when a background job it ran finishes; that turn would
    happen in the live host, where no start is asked for, so the host itself must go (Reopen resumes the session
    in a new one, and `launch.spec` refuses any start of a breakout that is not open). Never raises."""
    if not rec.get("work"):
        return  # a record from before workrooms shares the home's folder with the main chat: never guess

    def go():
        try:
            time.sleep(END_DELAY_S if delay_s is None else delay_s)
            agent = _agent()
            folder = _cwd_of(spec_id, rec)
            for run_id in agent._hosts_in(folder):
                agent._cancel_run(run_id, interrupt_first=False)
        except Exception:  # noqa: BLE001
            logger.debug("specialists: could not end a breakout's host", exc_info=True)

    threading.Thread(target=go, daemon=True, name="specialist-breakout-end").start()


def discard(spec, bid: str) -> dict:
    from fused_render.specialists import task_events

    with _LOCK:
        rec = get(spec.id, bid)
        if rec is None:
            raise LookupError(f"no such breakout: {bid}")
        rec["status"] = "discarded"
        rec["discarded"] = time.time()
        _save(rec)
    task_events.add(rec["parent"]["session_id"], "breakout_discarded", spec.id, spec.name, breakout_id=bid,
                    session_id=rec.get("session_id") or "")
    end_hosts(spec.id, rec)
    return rec


def reopen(spec, bid: str) -> dict:
    with _LOCK:
        rec = get(spec.id, bid)
        if rec is None:
            raise LookupError(f"no such breakout: {bid}")
        if rec.get("status") == "deleted":
            raise LookupError("this specialist's breakout is gone")
        rec["status"] = "open"
        _save(rec)
    return {"session_id": rec.get("session_id") or "", "start_params": start_params(rec), "home": sync(spec, bid)}
