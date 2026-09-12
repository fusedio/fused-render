"""Drafts over HTTP — the composer's unsent text, and the New task modal's
half-filled form.

The model is `fused_render/drafts.py`; this is the HTTP skin over it, and it is
deliberately thin. Every rule about what a draft is — empty is a delete, an
unknown field is dropped, a key is one of two shapes — lives in the store,
because the badge join in `routers/tasks.py` and the schedule router's
delete-on-create read the same store and must not be able to disagree with this
file about any of them.

**No D3 write guard**, unlike `POST /api/schedule`. That guard is on the two
endpoints that start and stop an unattended agent turn; a draft executes
nothing, and the POSTs in `routers/tasks.py` (read, archive, delete) are
unguarded for the same reason — this is the same weight of change as marking a
message read.

**Every mutation announces itself** (`tasks_watch.notify`). A draft is a listed
fact now: a chat draft puts a `✎ Draft` chip on its session's row, and a task
draft IS a row. Both have to appear and vanish without a reload, and the
long-poll in `/api/tasks/changes` is how every other change to a row already
travels. The key announced is the key the listing files the row under — the
session id for a chat draft, `draft:<id>` for a task draft — so the changes
endpoint rebuilds exactly that row and nothing else.

`new:<file>` announces itself too, as of round 2. It used to be the one key
that did not — no session meant no task row meant nothing for a listening page
to repaint — but an unsent chat IS a row now, filed under the folder it was
opened on (`routers/tasks.py::_new_chat_draft_row`), so it has to appear and
vanish live like every other (design.md, "Round 2"; Akshil, 2026-09-11).

Routes take `{key:path}` rather than `{key}` for one reason: a `new:<file>` key
carries a file path, separators and all, and a plain path parameter stops at
the first one.
"""
from fastapi import APIRouter, Body, HTTPException

from fused_render import drafts, tasks_store, tasks_watch

router = APIRouter()


def _chat_key(raw: str) -> str:
    key = drafts.chat_key(raw)
    if not key:
        raise HTTPException(
            status_code=400,
            detail="chat draft key: expected a session id or `new:<file>`")
    return key


def _draft_id(raw: str) -> str:
    ident = drafts.draft_id(raw)
    if not ident:
        raise HTTPException(
            status_code=400,
            detail="draft id: expected 8-64 characters of [A-Za-z0-9_-]")
    return ident


def _announce(*keys: str) -> None:
    """Tell the Tasks page's long-poll that these rows moved. Best-effort by
    construction — `notify` is an in-memory bump and cannot fail.

    Nothing is silent any more: all three shapes this file handles — a session
    id, `draft:<id>` and `new:<file>` — are keys the listing files a row under
    (module docstring)."""
    named = {key for key in keys if key}
    if named:
        tasks_watch.notify(named)


@router.get("/api/drafts")
def api_drafts():
    """Everything, both kinds. The modal's "resume" affordance reads the task
    half; the composer reads its own key out of the chat half rather than
    paying for a request per conversation."""
    task, chat = drafts.list_all()  # one file, one read
    return {"chat": chat, "task": task}


@router.post("/api/drafts/chat/rekey")
def api_draft_chat_rekey(body: dict = Body(default={})):
    """`{from, to}` — the first send has learned the session id.

    A chat opened on a folder is drafted under `new:<file>` and listed under
    that key, TASK number and all. The moment its first message is sent Claude
    Code mints a session, and the conversation the user is looking at is now
    filed somewhere else. Without this the number would be stranded on a row
    nobody can reach and the session would allocate a second one — the same
    break `pending:<entry-id>` → session id already has an answer for, which is
    why this is that answer: one `tasks_store.rekey`, same file, same
    allocate-once promise, same refusal to overwrite a number `to` already has
    (design.md, "Round 2").

    A no-op that still answers 200 when `from` has no number — a chat sent
    before the debounce ever saved a draft never had a row to number — because
    the client fires this on every first send and cannot know which it was.

    THE TEXT MOVES WITH THE NUMBER, when there is any. Normally there is none:
    the send is what cleared the composer, so `from` is already empty and this
    is purely a delete. When the user typed on after sending, the leftover
    text belongs to the conversation that send created. A draft already stored
    under `to` is left alone — it is the newer of the two and the one the
    composer is actually showing.
    """
    src = _chat_key(body.get("from"))
    dst = drafts.session_key(body.get("to"))
    if not dst:
        raise HTTPException(
            status_code=400, detail="to: expected a session id")
    if src == dst:
        raise HTTPException(
            status_code=400, detail="from and to: expected two different keys")

    number = ""
    try:
        number = tasks_store.rekey(src, dst)
    except OSError:
        # The message IS sent and the conversation IS open; a read-only state
        # dir must not turn that into a 500. The session simply allocates its
        # own number on the next listing. Same posture as the rekey in
        # `routers/schedule.py`.
        pass

    record = drafts.get_chat(src)
    moved = False
    if record and drafts.get_chat(dst) is None:
        moved = drafts.put_chat(dst, record.get("text"),
                                record.get("attachments")) is not None
    drafts.delete_chat(src)
    _announce(src, dst)
    return {"ok": True, "from": src, "to": dst,
            "task_id": number, "moved": moved}


@router.put("/api/drafts/chat/{key:path}")
def api_draft_chat_put(key: str, body: dict = Body(default={})):
    """Upsert one chat draft. An empty one is a DELETE, and the answer says so
    (`draft: null`) rather than making the caller infer it — the composer's
    autosave fires on every pause including the one after the send cleared the
    box, and it must be allowed to keep sending what it now holds."""
    chat = _chat_key(key)
    record = drafts.put_chat(chat, body.get("text"), body.get("attachments"))
    _announce(chat)
    return {"ok": True, "key": chat, "draft": record}


@router.delete("/api/drafts/chat/{key:path}")
def api_draft_chat_delete(key: str):
    """Drop one chat draft — on send, or on an explicit clear. Answers whether
    there was one, and is not a 404 when there was not: the composer clears
    after a send whether or not the debounce ever got round to a first save,
    and a red line in the console over that would be noise about nothing."""
    chat = _chat_key(key)
    removed = drafts.delete_chat(chat)
    _announce(chat)
    return {"ok": True, "key": chat, "removed": removed}


@router.put("/api/drafts/task/{draft_id:path}")
def api_draft_task_put(draft_id: str, body: dict = Body(default={})):
    """Upsert one task draft from the modal's form fields.

    The body IS the form — `title`, `description`, `target`, `when`, `repeat`,
    `custom_rule`, `model`, `effort`, `permission`, `attachments`,
    `new_task_each_run`, `from_chat_key` — and
    anything else in it is dropped by the store rather than refused here, so
    the modal may grow a field without this endpoint learning about it. An
    all-empty form is a delete, the same bargain the chat half makes."""
    ident = _draft_id(draft_id)
    record = drafts.put_task(ident, body)
    # A DRAFT MOVES, IT NEVER DUPLICATES. The composer → New task hop mints the
    # task draft out of what was in the chat box, so for one instant the same
    # unfinished sentence is two drafts and two rows. `from_chat_key` is the
    # client naming the one it came from, and this is the same request that
    # made the new one — a client that deleted the old one afterwards would
    # leave both listed through any failure between the two calls, which is the
    # one outcome the rule forbids (design.md, "Round 2"; Akshil, 2026-09-11).
    #
    # Only on SUCCESS: an all-empty form is a delete (the store says so by
    # answering None), and dropping the chat draft over a write that stored
    # nothing would lose the text outright. Optional and silently ignored when
    # absent or malformed — the modal's ordinary autosave sends no such key,
    # and a hop is not worth a 400.
    #
    # IT IS ALSO A STORED FIELD, not only a side effect (`drafts.TASK_FIELDS`),
    # and every later save that omits it keeps what was stored. That is what
    # rides down in the draft row's `form`, and it is what lets a draft REOPENED
    # from the List still know which conversation to put its words back into:
    # such a card has no `?back=` in its URL and no sessionStorage stash left,
    # so without a stored key "Back to chat" had nothing to aim at and the move
    # was one-way the moment the modal closed (Akshil, 2026-09-11).
    from_chat = drafts.chat_key(body.get("from_chat_key")) if record else ""
    if from_chat:
        drafts.delete_chat(from_chat)
    _announce(drafts.task_key(ident), from_chat)
    return {"ok": True, "draft_id": ident, "draft": record,
            "from_chat_key": from_chat}


@router.delete("/api/drafts/task/{draft_id:path}")
def api_draft_task_delete(draft_id: str):
    """Discard one task draft — the modal's Discard button, and the tidy-up
    after a draft has been scheduled for real (which `POST /api/schedule` does
    for itself, given a `draft_id`)."""
    ident = _draft_id(draft_id)
    removed = drafts.delete_task(ident)
    _announce(drafts.task_key(ident))
    return {"ok": True, "draft_id": ident, "removed": removed}
