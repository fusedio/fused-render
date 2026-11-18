"""`/api/specialists/{id}/…`: the learning loop's HTTP API (PLAN B.5 "Learning").

    GET    …/{id}/panel                         the specialist page's right panel
    GET    …/{id}/learned?limit=5               the latest change lists, each item with its state
    POST   …/{id}/changes/{run}/{item}/revert   {"message","session_id"}: the page sends it into the main chat
    POST   …/{id}/nightly/run                   202 {"row"} / 409 {"error","reason":"busy"}
    GET    …/{id}/nightly                       {"last_ok","running","last_row"}
    GET    …/{id}/memory                        always-loaded entries with provenance, size against the cap, knowledge
    GET    …/{id}/inbox                         undigested notes, newest first
    POST   …/{id}/notes                         "Send to a specialist" ({id} may be `general`): a user-said note
    DELETE …/{id}/notes/{nid}                   Undo; 409 once digested
    POST   …/{id}/notes/{nid}/confirm           the user confirms a user-said note a model wrote: in effect now
    GET    /api/specialists/general/inbox       notes that fit no specialist
    POST   /api/specialists/general/notes/{nid}/move {"specialist"}  into that specialist's inbox, same id
    DELETE /api/specialists/general/notes/{nid}
    GET    …/{id}/drafts                        drafts waiting for the user
    POST   …/{id}/drafts/{did}/send             {"message","session_id"}: Review and send
    POST   …/{id}/drafts/{did}/discard          {"ok":true}
    GET    …/{id}/counts                        B.6 counts

Every route depends on `flag.require` (flag off: 404). Every non-GET needs `X-Fused: 1`. The `general` routes are
declared before the `{id}` ones. Errors are `{"error": sentence}`.
"""
from __future__ import annotations

import functools
import os
import re

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse

from fused_render.server.common import _error, _require_fused
from fused_render.specialists import flag

router = APIRouter(dependencies=[Depends(flag.require)])
PREFIX = "/api/specialists"
ENTRY_RE = re.compile(r"^- \[(m-[0-9a-f]{4,8})\] (.+?) \{(.*)\}\s*$")


class _NotFound(Exception):
    pass


def _handled(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (_NotFound, LookupError, KeyError) as e:
            return _error(str(e.args[0]) if e.args else "not found", 404)
        except ValueError as e:
            return _error(str(e) or "bad request", 400)
    return wrap


def _spec(spec_id: str):
    from fused_render.specialists import store
    s = store.get(spec_id)
    if s is None:
        raise _NotFound(f"no such specialist: {str(spec_id)[:80]}")
    return s


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("the request body must be a JSON object")
    return body


# ----------------------------------------------------------------- general inbox ---
@router.get(PREFIX + "/general/inbox")
@_handled
def general_inbox():
    from fused_render.specialists import inbox
    return {"notes": inbox.undigested(inbox.GENERAL)}


@router.post(PREFIX + "/general/notes/{nid}/move")
@_handled
def general_move(nid: str, body=Body(None), x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import inbox, store
    key = _body(body).get("specialist")
    s = store.find(key) if isinstance(key, str) and key else None
    if s is None:
        raise _NotFound(f"no such specialist: {str(key)[:80]}")
    note = inbox.move(nid, s.id)
    if note is None:
        raise _NotFound("no such note in the general inbox")
    return note


@router.delete(PREFIX + "/general/notes/{nid}")
@_handled
def general_delete(nid: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import inbox
    if not inbox.delete(inbox.GENERAL, nid):
        raise _NotFound("no such note in the general inbox")
    return {"ok": True}


# ------------------------------------------------------------------------- panel ---
@router.get(PREFIX + "/{spec_id}/panel")
@_handled
def panel(spec_id: str):
    from fused_render.specialists import changes, drafts, inbox
    _spec(spec_id)
    runs = changes.runs(spec_id, limit=1)
    learned = None
    if runs:
        r = runs[0]
        learned = {"run": r["run"], "at": r.get("at"), "summary": r.get("summary", ""),
                   "items": r["items"][:3], "total": len(r["items"])}
    helping, guided = [], 0
    try:
        from fused_render.specialists import breakouts
        for b in breakouts.open_now(spec_id) or []:
            parent = b.get("parent") if isinstance(b.get("parent"), dict) else {}
            helping.append({"session_id": parent.get("session_id", ""), "title": parent.get("title", ""),
                            "file": parent.get("file", ""), "breakout_id": b.get("id", "")})
    except Exception:  # noqa: BLE001
        helping = []
    try:
        from fused_render.specialists import receipts
        guided = int(receipts.guided_week(spec_id) or 0)
    except Exception:  # noqa: BLE001
        guided = 0
    return {"learned": learned, "waiting": drafts.waiting(spec_id), "inbox": {"count": len(inbox.undigested(spec_id))},
            "tasks": {"helping_now": helping, "guided_week": guided}}


@router.get(PREFIX + "/{spec_id}/learned")
@_handled
def learned(spec_id: str, limit: int = 5):
    from fused_render.specialists import changes
    _spec(spec_id)
    return {"runs": [{"run": r["run"], "at": r.get("at"), "summary": r.get("summary", ""), "status": r.get("status"),
                      "commit": r.get("commit", ""), "items": r["items"]}
                     for r in changes.runs(spec_id, limit=max(1, min(int(limit), 50)))]}


@router.post(PREFIX + "/{spec_id}/changes/{run}/{item}/revert")
@_handled
def revert(spec_id: str, run: str, item: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import changes
    _spec(spec_id)
    return changes.revert(spec_id, run, item)


# ----------------------------------------------------------------------- nightly ---
@router.post(PREFIX + "/{spec_id}/nightly/run")
@_handled
def nightly_run(spec_id: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import nightly
    _spec(spec_id)
    res = nightly.run_now(spec_id)
    if res.get("error"):
        return JSONResponse({"error": res["error"], "reason": res.get("reason", "busy")},
                            status_code=404 if res.get("reason") == "not_found" else 409)
    return JSONResponse({"row": res["row"]}, status_code=202)


@router.get(PREFIX + "/{spec_id}/nightly")
@_handled
def nightly_status(spec_id: str):
    from fused_render.specialists import nightly
    _spec(spec_id)
    return nightly.status(spec_id)


# ------------------------------------------------------------------------ memory ---
def parse_entry(line: str) -> dict | None:
    m = ENTRY_RE.match(line)
    if not m:
        return None
    meta = {}
    for part in m.group(3).split("; "):
        k, sep, v = part.partition("=")
        if sep:
            meta[k.strip()] = v.strip()
    seen = meta.get("seen", "")
    return {"id": m.group(1), "text": m.group(2), "basis": meta.get("basis", ""),
            "seen": int(seen) if seen.isdigit() else None, "confirmed": meta.get("confirmed", ""),
            "from": meta.get("from", ""), "retires": meta.get("retires", ""), "raw": line}


def _knowledge(home: str) -> list[dict]:
    out = []
    root = os.path.join(home, "knowledge")
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            p = os.path.join(dirpath, f)
            title = os.path.splitext(f)[0]
            try:
                with open(p, encoding="utf-8") as fh:
                    for ln in fh.read(4000).splitlines():
                        if ln.startswith("#"):
                            title = ln.lstrip("#").strip() or title
                            break
            except OSError:
                pass
            out.append({"title": title, "path": p})
    return out


@router.get(PREFIX + "/{spec_id}/memory")
@_handled
def memory(spec_id: str):
    from fused_render.specialists import nightly
    s = _spec(spec_id)
    home = getattr(s, "home", "") or ""
    from fused_render.specialists import paths
    home = home or paths.home(spec_id)
    try:
        with open(os.path.join(home, "memory.md"), "rb") as fh:
            data = fh.read()
    except OSError:
        data = b""
    raw = data.decode("utf-8", "replace")
    entries = []
    for ln in raw.splitlines():
        if ln.startswith("- "):
            e = parse_entry(ln)
            entries.append(e if e is not None else {"id": "", "text": ln[2:], "basis": "", "seen": None,
                                                    "confirmed": "", "from": "", "retires": "", "raw": ln})
    max_lines, max_bytes = nightly._limits()
    return {"entries": entries, "raw": raw, "lines": len(raw.splitlines()), "bytes": len(data),
            "limit_lines": max_lines, "limit_bytes": max_bytes, "knowledge": _knowledge(home)}


# ------------------------------------------------------------------------- inbox ---
@router.get(PREFIX + "/{spec_id}/inbox")
@_handled
def inbox_list(spec_id: str):
    from fused_render.specialists import inbox
    if spec_id != inbox.GENERAL:
        _spec(spec_id)
    return {"notes": inbox.undigested(spec_id)}


@router.post(PREFIX + "/{spec_id}/notes")
@_handled
def note_add(spec_id: str, body=Body(None), x_fused: str | None = Header(None)):
    """"Send to a specialist" on a message: the user's choice, so basis is the body's (the page sends
    user-said, PLAN D.2 #8) and the source is `user` with the message's session."""
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import inbox
    if spec_id != inbox.GENERAL:
        _spec(spec_id)
    body = _body(body)
    src = body.get("source") if isinstance(body.get("source"), dict) else {}
    source = {"type": "user", "session_id": str(src.get("session_id") or ""), "uuid": str(src.get("uuid") or ""),
              "title": str(src.get("title") or "")}
    for k in ("kind", "claim", "basis"):
        if not isinstance(body.get(k), str):
            raise ValueError(f"'{k}' is required")
    return inbox.add(spec_id, kind=body["kind"], claim=body["claim"], basis=body["basis"],
                     evidence=str(body.get("evidence") or ""), entry=body.get("entry"), source=source)


@router.delete(PREFIX + "/{spec_id}/notes/{nid}")
@_handled
def note_delete(spec_id: str, nid: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import inbox
    if spec_id != inbox.GENERAL:
        _spec(spec_id)
    if inbox.is_digested(spec_id, nid):
        return _error("This note was already weighed by the nightly update; it can no longer be undone.", 409)
    if not inbox.delete(spec_id, nid):
        raise _NotFound("no such note")
    return {"ok": True}


@router.post(PREFIX + "/{spec_id}/notes/{nid}/confirm")
@_handled
def note_confirm(spec_id: str, nid: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import inbox
    _spec(spec_id)
    try:
        note = inbox.confirm(spec_id, nid)
    except ValueError as e:
        return _error(str(e), 409)
    if note is None:
        raise _NotFound("no such note")
    return note


# ------------------------------------------------------------------------ drafts ---
@router.get(PREFIX + "/{spec_id}/drafts")
@_handled
def drafts_list(spec_id: str):
    from fused_render.specialists import drafts
    _spec(spec_id)
    return {"drafts": drafts.waiting(spec_id)}


@router.post(PREFIX + "/{spec_id}/drafts/{did}/send")
@_handled
def draft_send(spec_id: str, did: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import drafts
    _spec(spec_id)
    return drafts.send(spec_id, did)


@router.post(PREFIX + "/{spec_id}/drafts/{did}/discard")
@_handled
def draft_discard(spec_id: str, did: str, x_fused: str | None = Header(None)):
    if (bad := _require_fused(x_fused)) is not None:
        return bad
    from fused_render.specialists import drafts
    _spec(spec_id)
    drafts.discard(spec_id, did)
    return {"ok": True}


@router.get(PREFIX + "/{spec_id}/counts")
@_handled
def counts_get(spec_id: str):
    from fused_render.specialists import counts
    _spec(spec_id)
    return counts.get(spec_id)
