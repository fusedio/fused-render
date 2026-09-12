"""Drafts — what the composer and the New task modal were still typing.

Two surfaces lose text today. The chat composer keeps it in `useState`, seeded
once from a sessionStorage hop that is spent on read, so it dies on tab close,
on opening another chat, on reload. The New task modal keeps it per open and
`key="new#<seq>"` remounts blank, so closing the modal is the same as never
having typed. Neither is a place unfinished work can live, and unfinished work
is most of what a person has open at any moment.

**Server, one file, global.** `<FUSED_RENDER_HOME>/claude-sessions/drafts.json`
— the same never-branch-nested directory `tasks_store` keeps `task_ids.json`
and `read.json` in, and for exactly the same reason: chat drafts key on session
ids, and `~/.claude/projects` is one machine-wide pool. A draft written from a
worktree's dev server must be there when the packaged app opens the same
conversation.

Why not the alternatives (design.md, "Where drafts live"): a draft is not a
place you can link to, so not the URL; the `✎ Draft` badge in the List and the
Board needs the server to join draft ↔ task row and `/api/tasks` is
server-rendered, so not localStorage; and `state:"draft"` inside `tasks.jsonl`
would put a row with no `due` in front of schedule.py's ticker, claim and queue,
every one of which assumes there is one. A separate store, joined at read.

Shape::

    {"chat": {"<session-id>": {"text": …, "attachments": […], "updated_at": …}},
     "task": {"<draft-id>": {"title": …, …, "created_at": …, "updated_at": …}}}

**One record, two doors.** A task draft that names a session (`session_id`) IS
that conversation's unsent message, so the composer is a second door onto it:
a session with a bound form and no chat record of its own reads and writes that
form's words from the chat half (`bound_chats`, `chat_view`, `put_chat`). Never
a copy — the words are in exactly one record, and which door the reader comes
back through does not change what they find. The bug that asked for it: hop out
of a finished session, type, press Schedule, click outside the modal, and the
row wore the `✎ Draft` chip while the composer the chip's press led to was
empty (Akshil, 2026-09-12).

**Empty is never stored.** Writing an empty draft IS deleting it — there is no
such thing as a draft with nothing in it, and a store that kept one would paint
a `✎ Draft` badge on a task whose composer is blank. `put_chat` and `put_task`
therefore both answer `None` for a write they turned into a delete, so the
caller never has to ask which of the two it just did.

**Nothing here raises for input it cannot read.** A missing store, a corrupt
store, a record of the wrong shape, an attachment row with no path — each
degrades to "no draft", never to a failed request. Same posture as tasks_store
and every other registry in this package: a draft is a convenience, and a
convenience that can break a page is worse than one that is missing.

No import of anything under `fused_render.server`, and none of
`fused_render.schedule` either — importing the schedule model to borrow its
`_attachments` validator would pull the whole ticker, its storage seams and its
event log in behind it, for one shape check. The local validator below is
deliberately the LENIENT twin of that one: `schedule._attachments` refuses an
attachment whose file has gone, because a scheduled run must not be handed a
path it cannot read, where a draft is text somebody has not sent yet and losing
it over a moved file would be the store failing at its only job (Akshil,
2026-09-11).
"""
from __future__ import annotations

import json
import os
import re
import time

try:
    import fcntl  # POSIX only — Windows falls back to no inter-process lock,
    # the same posture (and the same directory) as tasks_store._update.
except ImportError:  # pragma: no cover
    fcntl = None

# Derived from the env at import, exactly like `tasks_store.STATE_DIR` — same
# deliberate local duplication, same consequence for tests: a test that
# redirects one module's dir must redirect this one's too.
STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")

DRAFTS_FILE = "drafts.json"

#: The two kinds, and the two top-level keys of the file. A chat draft belongs
#: to a conversation; a task draft belongs to nothing yet, which is why it needs
#: an id of its own.
CHAT = "chat"
TASK = "task"

#: A chat with no session yet (the composer's first message has not been sent)
#: keys on the file it opened on instead — the same key `takeDraft(file)` uses
#: in the client.
#:
#: Round 1 said such a key never joined a listing and was never re-keyed: the
#: first send deleted the draft, and that was the end of it. Round 2 reversed
#: both halves. A `new:<file>` draft IS a row (the folder is its project, the
#: first line its title, and it carries a TASK number like any other), and the
#: first send therefore has somewhere to carry that number TO: `new:<file>` →
#: the session id, by the same `tasks_store.rekey` that moves
#: `pending:<entry-id>` forward. WHO makes that move is the SERVER, off the run
#: the send TAGGED with this key (`draft_key` in its `meta.json`,
#: `routers/tasks.py::_settle_new_chats`): a page cannot tell which session id
#: its own send created, and four rounds of trying is how we know (Bugbot, PR
#: #1118, 2026-09-12) — but it can name the draft it just spent. Nothing about
#: the SHAPE changed; what
#: changed is that the key is now worth keeping (design.md, "Round 2"; Akshil,
#: 2026-09-11).
NEW_CHAT_PREFIX = "new:"

#: A client-minted uuid, in practice. Validated as a shape rather than parsed as
#: a uuid so a client that mints `d-<random>` is not refused over a format
#: nothing in here depends on — what matters is that it is one path-free token
#: that cannot be confused with a task key.
_DRAFT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: A session id as Claude Code writes it — the same shape
#: `tasks.py::_SESSION_ID_SHAPE` accepts, for the same reason: it becomes a
#: json key and is compared against task keys, and neither wants a separator.
_SESSION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How long a `new:<file>` key may be. A path, so generous; bounded at all so a
#: request body cannot grow the store's key space without limit.
_CHAT_KEY_MAX = 512

#: How much of a chat draft the `✎ Draft` badge's tooltip carries. The listing
#: joins a preview onto every session row, so this rides on rows the Tasks page
#: polls — one line is the whole affordance, and the rest of the draft is in the
#: composer where the user left it.
PREVIEW_MAX = 120

#: Every field a task draft stores, in the order the form reads them. A key the
#: client sends that is not in here is DROPPED rather than refused (the modal
#: gains fields over time and an older server must not start 400ing a newer
#: page), and a field the client omits keeps whatever the stored draft had.
TASK_FIELDS = ("title", "description", "target", "when", "repeat", "custom_rule",
               "model", "effort", "permission", "attachments",
               "new_task_each_run", "from_chat_key", "session_id")

#: The fields that are plain text, normalised through `_text` on the way in. The
#: rest are pass-through (`when`, `repeat`, `custom_rule`), a tri-state flag
#: (`new_task_each_run`), rows (`attachments`), the chat key this draft was
#: moved out of (`from_chat_key`) or the session it is going into
#: (`session_id`) — the last two being keys rather than free text, and each
#: validated as the key it is.
#:
#: THIS IS A LIST OF SHAPES, NOT A DEFINITION OF CONTENT — see `_TASK_CONTENT`
#: below, which is the one that decides whether there is a draft here at all.
#: `from_chat_key` and `session_id` are in neither, and for the same reason
#: `from_chat_key` was always out of the second: they are provenance and
#: destination, not something a person typed. A form that arrives blank is still
#: a delete even when it names the chat it came from and the session it was
#: going to — which is exactly the bargain `an empty task put keeps the chat
#: draft` rests on (Akshil, 2026-09-11).
_TASK_TEXT = ("title", "description", "target", "model", "effort", "permission")

#: WHAT MAKES A DRAFT A DRAFT: words. Plus `attachments`, which `_empty_task`
#: asks about separately because it is rows rather than text.
#:
#: Everything else the form holds — the folder, the model, the effort, the
#: permission mode, the time, the repeat rule — is a SETTING that rides along
#: with a draft, not a reason for one to exist. They used to count, and the
#: consequence was a card that minted an "Untitled draft" row on the List the
#: moment somebody changed the folder or opened the when-row and picked a time,
#: for a form holding nothing anybody had typed. It also left the reported
#: dead-end: clear the text and the row reads "Untitled draft"; remove the last
#: attachment after that and the row still will not go, because `target` alone
#: was keeping the record alive (Akshil, 2026-09-12).
_TASK_CONTENT = ("title", "description")

#: What an attachment's `kind` may be — the same two `schedule._ATTACH_KINDS`
#: allows, and a third local copy of a list that is already spelled twice (see
#: the module docstring for why this is not an import).
_ATTACH_KINDS = ("image", "file")
_ATTACH_NAME_MAX = 255


# ------------------------------------------------------------------- the keys


def chat_key(value) -> str:
    """One chat draft's key, or `""` for anything that is not one.

    Two shapes, because a chat has two ages: a session id once the conversation
    exists, and `new:<file>` before it does. `""` rather than a raise — every
    caller here is an HTTP route that turns it into a 400, and the store itself
    must never be the thing that throws."""
    if not isinstance(value, str):
        return ""
    key = value.strip()
    if not key or len(key) > _CHAT_KEY_MAX:
        return ""
    if key.startswith(NEW_CHAT_PREFIX):
        rest = key[len(NEW_CHAT_PREFIX):]
        # A file path, so nearly anything goes — but not a control character,
        # which would be a json key no human could read back out of the store.
        if not rest.strip() or any(ch < " " for ch in rest):
            return ""
        return key
    return key if _SESSION_KEY.match(key) else ""


def is_new_chat_key(key: str) -> bool:
    """Is this the pre-session shape?

    Round 1 used this to say "no row, announce nothing". Round 2 gave such a
    draft a row of its own — a folder, a title, a TASK number — so what it now
    says is narrower and more useful: this draft is filed under a FOLDER and
    not under a conversation, which is what decides how the listing builds its
    row and what `session_id` it can print (nothing) (Akshil, 2026-09-11)."""
    return isinstance(key, str) and key.startswith(NEW_CHAT_PREFIX)


def new_chat_file(key: str) -> str:
    """The file (or folder) a `new:<file>` key was opened on, or `""`.

    The listing needs it twice — the row's `file`, and the folder its project
    and target are derived from — and neither should be re-deriving the prefix
    arithmetic. `""` for any other key, so a caller may ask without testing
    first."""
    if not is_new_chat_key(key):
        return ""
    return key[len(NEW_CHAT_PREFIX):]


def bound_session(value) -> str:
    """The session a task draft BELONGS TO, or `""` for anything that is not a
    session id.

    The narrow twin of `chat_key`: that one takes both of a chat's two ages, and
    this one takes only the older. A draft can be bound to a conversation that
    EXISTS — the composer → Schedule hop out of a session that has already run —
    and never to `new:<file>`, which names a folder somebody opened a chat on and
    no thread at all. There is nothing for a `new:` draft to schedule INTO, and a
    key of that shape in this field would make the listing hide a session row
    that does not exist (Akshil, 2026-09-12)."""
    if not isinstance(value, str):
        return ""
    key = value.strip()
    return key if _SESSION_KEY.match(key) else ""


def draft_id(value) -> str:
    """One task draft's id, or `""` for anything that is not one."""
    if not isinstance(value, str):
        return ""
    ident = value.strip()
    return ident if _DRAFT_ID.match(ident) else ""


def task_key(ident: str) -> str:
    """The task key a task draft is listed under. `draft:` rather than
    `pending:` (tasks_store's key for a scheduled message with no session yet)
    because the two are genuinely different rows: a pending message WILL run,
    and a draft will not until somebody finishes it."""
    return "draft:" + ident


# ------------------------------------------------------------------ the file


def load() -> dict:
    """The whole store, `{"chat": {}, "task": {}}` — missing or corrupt is not
    an error. Both sections always present, so no caller has to test for them."""
    data = None
    try:
        with open(os.path.join(STATE_DIR, DRAFTS_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    for section in (CHAT, TASK):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    return data


def _update(mutate):
    """Read-modify-write the store under an exclusive lock; return whatever
    `mutate` returns.

    A copy of `tasks_store._update`, sibling `.lock` file and all, and a copy
    rather than a call for the module docstring's reason: this module imports
    nothing. The lock covers the READ as well as the write for the reason
    documented there — the app runs several windows against one server, and a
    second writer holding a snapshot taken before the first one's change would
    persist the loss."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, DRAFTS_FILE)
    with open(path + ".lock", "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        result, changed = mutate(data)
        if changed:
            # Temp + rename, unlike tasks_store's plain overwrite: this file
            # holds text a person typed and has not sent, which is the one
            # thing in `claude-sessions/` that cannot be rebuilt from anywhere
            # else. A half-written read.json costs read marks; a half-written
            # drafts.json costs the draft (Akshil, 2026-09-11).
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
    return result


# ------------------------------------------------------------ the two shapes


def _attachments(value) -> list[dict]:
    """A draft's attachments as stored `{path, name, kind}` rows.

    The lenient twin of `schedule._attachments` (module docstring): a row that
    cannot be read is DROPPED, never raised over, and a file that has since
    moved is kept — the schedule validates again at create time, which is the
    moment a missing file actually matters. Containment under the task-shots
    dir is likewise the schedule's check to make: nothing in this store is ever
    handed to a run."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        kind = item.get("kind")
        if kind not in _ATTACH_KINDS:
            kind = _ATTACH_KINDS[0]
        name = item.get("name")
        # BASENAME and one line, the same two rules `schedule._attachments`
        # applies and for the same reason: this name is only ever displayed, so
        # a client that sent a path here must not have it read back as one.
        name = os.path.basename(str(name or "").strip().replace("\\", "/"))
        name = name.replace("\r", " ").replace("\n", " ").strip()
        if len(name) > _ATTACH_NAME_MAX:
            name = name[:_ATTACH_NAME_MAX]
        out.append({"path": path.strip(),
                    "name": name or os.path.basename(path.strip()),
                    "kind": kind})
    return out


def _jsonable(value):
    """`when` and `repeat` pass through whatever the form put in them — a
    datetime-local string, a cron line, or a structured `recur` rule object —
    so the modal reopens on exactly what it closed on. Only "can this be
    written back out as json" is checked; anything else becomes None, because a
    value that cannot be serialized would take the whole store down with it."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()
                if isinstance(k, str)}
    return None


def _text(value) -> str:
    """One text field. Not length-capped: this is work the user has typed and
    not sent, and silently truncating it is the one failure a draft store may
    never have (the same reasoning D615 deleted the chat's byte cap for)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _flag(value):
    """A tri-state: True, False, or "the form never said". None rather than
    False for the third, so a draft that predates the checkbox reopens with the
    form's own default rather than with it forced off."""
    return None if value is None else bool(value)


def preview(text) -> str:
    """The one line the `✎ Draft` chip's tooltip shows: the first non-empty
    line, clipped to `PREVIEW_MAX` including the ellipsis. A draft that starts
    with blank lines still has something to say about itself."""
    for line in _text(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) > PREVIEW_MAX:
            return line[:PREVIEW_MAX - 1].rstrip() + "…"
        return line
    return ""


def split_draft(text) -> tuple[str, str]:
    """One box of words as the New task form's two fields: `(title,
    description)` — the first non-empty line names the thing, the rest
    describes it.

    THE PYTHON TWIN OF `NewJobModal.splitDraft`, and it has to stay its twin.
    The hop into the form splits in the client; the composer's door onto a
    bound draft splits here (`put_chat`), and a rule that drifted would mean
    one sentence landing in two different shapes depending on which door it
    came through. Leading blank lines go before the first line is taken, which
    is what makes "the first NON-EMPTY line" true of both."""
    body = _text(text).strip()
    if not body:
        return "", ""
    head, _, rest = body.partition("\n")
    return head.strip(), rest.strip()


def join_draft(title, description) -> str:
    """The form's two fields back as one box of words — `split_draft` run
    backwards, blank line and all (`NewJobModal.joinDraft`).

    Not an inverse of every input: a title and a description written one
    newline apart come back a blank line apart, because a blank line is what
    two fields look like as one box. It IS an inverse of everything this pair
    itself produces, which is the round trip the composer actually makes."""
    head = _text(title).strip()
    body = _text(description).strip()
    if not head:
        return body
    if not body:
        return head
    return head + "\n\n" + body


# ------------------------------------------------------------- chat drafts


def _project_chat(section: dict) -> dict:
    """The chat section of an ALREADY-LOADED store, projected. Split out of
    `list_chat` so `list_all` can answer both questions off one read.

    `bound_draft` is `""` on everything that comes out of here, because a
    STORED chat record is a record of its own — the field is filled in only by
    `chat_view`, on the entries it synthesizes out of a task draft. One shape
    for both, so a reader never has to test for the key."""
    out: dict[str, dict] = {}
    for key, rec in section.items():
        if not chat_key(key) or not isinstance(rec, dict):
            continue
        out[key] = {"text": _text(rec.get("text")),
                    "attachments": _attachments(rec.get("attachments")),
                    "updated_at": _epoch(rec.get("updated_at")),
                    "bound_draft": ""}
    return out


def bound_chats(chat: dict, task: dict) -> dict[str, str]:
    """`{session-id: draft-id}` — every conversation whose unsent words are
    being kept in a New task FORM rather than in a chat record of its own.

    ONE RECORD, TWO DOORS (Akshil, 2026-09-12). The composer's Schedule hop
    moves the words out of the chat draft and into a task draft bound to the
    session (`from_chat_key` deletes the one, `session_id` binds the other), and
    the reported bug is what the session's row looked like afterwards: it wore
    the red `✎ Draft` chip, the chip's press took the reader to the chat, and
    the composer there was empty — the words were on disk the whole time, in a
    record that door could not see. A session with a bound form and no chat
    record of its own therefore SHOWS AND EDITS that form's words from the
    composer too. Two doors onto one record, never a second copy of it.

    THE JOIN ITSELF LIVES HERE and nowhere else, so `/api/drafts`, the composer
    and `routers/tasks.py`'s row chip cannot disagree about which draft a
    session's words are in.

    A STORED CHAT RECORD WINS outright: a session can have both — text left in
    its composer and a form opened out of it — and then they are two different
    unsent things, the composer's own being the one the composer is holding.
    That is the same precedence `_row` has always drawn its chip with.

    NEWEST WINS when two forms name one session, the same tie-break
    `_bound_chips` and `put_task`'s fold take, so every reader agrees on which
    draft a session is advertising."""
    out: dict[str, str] = {}
    stamps: dict[str, float] = {}
    for ident, record in task.items():
        session = bound_session(record.get("session_id"))
        if not session or session in chat:
            continue
        updated = _epoch(record.get("updated_at"))
        if session in out and stamps[session] >= updated:
            continue
        out[session], stamps[session] = ident, updated
    return out


def chat_view(chat: dict, task: dict) -> dict:
    """The chat half as a READER sees it: every stored chat draft, plus one
    synthesized entry for every session whose words live in a bound form
    (`bound_chats`).

    A synthesized entry is the form read as a composer would write it — the
    title and description joined back into one box (`join_draft`, the inverse
    of the split the hop made), the form's attachments, the form's clock — plus
    `bound_draft`, the form's id, so a client that cares can tell the two apart.
    Nothing here writes: this is a projection of one read, and the write that
    goes back through the same door is `put_chat`'s half of the bargain."""
    out = dict(chat)
    for session, ident in bound_chats(chat, task).items():
        record = task[ident]
        out[session] = {
            "text": join_draft(record.get("title"), record.get("description")),
            "attachments": _attachments(record.get("attachments")),
            "updated_at": _epoch(record.get("updated_at")),
            "bound_draft": ident,
        }
    return out


def list_chat() -> dict:
    """Every chat draft a reader can open, `{key: {text, attachments,
    updated_at, bound_draft}}` — stored records and the bound forms that read
    as one (`chat_view`), unreadable records dropped."""
    store = load()
    return chat_view(_project_chat(store[CHAT]), _project_task(store[TASK]))


def get_chat(session_id) -> dict | None:
    """One chat draft, or None. None and "an empty draft" are the same thing
    here, because the empty one is never stored."""
    key = chat_key(session_id)
    return list_chat().get(key) if key else None


def bound_chat_draft(session_id) -> str:
    """The task draft one chat key's words actually live in, or `""` — one
    read of the file, for a caller that has to name that draft's row.

    The routes' use: both halves of a chat write announce the FORM's key
    (`draft:<id>`) as well as the session's, because on a bound session the two
    doors are one record and a page holding a row under either has to hear
    about it."""
    key = chat_key(session_id)
    session = bound_session(key)
    if not session:
        return ""
    store = load()
    return bound_chats(_project_chat(store[CHAT]),
                       _project_task(store[TASK])).get(session, "")


def _put_bound(data: dict, ident: str, body: str, rows: list[dict]):
    """The composer's words written into the FORM they live in — `put_chat`'s
    other half, run inside its lock (`bound_chats` for why there is one).

    The inverse of what the reader was shown: the box is split back across
    title and description by the same rule the hop split it with
    (`split_draft`), so typing into the composer and typing into the card's two
    fields are edits to one record rather than to two copies of it.

    THE TRAY IS A REPLACEMENT AND NOT A UNION, unlike the fold `put_task` does
    between two FORMS (`_fold_into_bound`). That one reconciles a card that
    could not read the binding before it wrote, and so cannot tell "no files"
    from "did not ask"; this door always shows the whole tray, so a file
    missing from the write is a file the reader took out.

    EMPTY IS A DELETE, the same bargain the chat half has always made: the
    composer's autosave fires after the send has cleared the box, and the words
    it is clearing are the ones that were just sent. What goes is the whole
    form — there is no half of a draft left to keep."""
    title, description = split_draft(body)
    if not title and not description and not rows:
        if ident not in data[TASK]:
            return None, False
        data[TASK].pop(ident, None)
        return None, True
    record = _task_record(data[TASK].get(ident)) or {}
    now = time.time()
    record["title"] = title
    record["description"] = description
    record["attachments"] = rows
    record["created_at"] = record.get("created_at") or now
    record["updated_at"] = now
    data[TASK][ident] = record
    return {"text": join_draft(title, description), "attachments": rows,
            "updated_at": now, "bound_draft": ident}, True


def put_chat(session_id, text=None, attachments=None) -> dict | None:
    """Upsert one chat draft — and DELETE it when it comes in empty.

    "Empty" is no text and no attachments, which is the state a composer is in
    the moment its message is sent. The send path therefore does not have to
    choose between PUT and DELETE: writing what the box now holds is correct in
    both directions. Answers the stored record, or None when the write was a
    delete.

    …AND WHEN THIS SESSION'S WORDS ARE IN A BOUND FORM, the write goes THERE
    and no chat record is made (`bound_chats`, "one record, two doors"). Two
    records would be two rows, two chips and two versions of one sentence, and
    which of them the reader saw would depend on which door they came back
    through. The answer is that form read as a chat draft — the same shape a
    stored one has, `bound_draft` filled in — so the caller need not know which
    of the two it just wrote.

    The binding is looked up INSIDE the lock, with the write: a form bound (or
    emptied) between a read and a write outside one is exactly how the same
    sentence ends up in both halves of the store."""
    key = chat_key(session_id)
    if not key:
        return None
    body = _text(text)
    rows = _attachments(attachments)

    def mutate(data: dict):
        ident = ""
        session = bound_session(key)
        if session:
            ident = bound_chats(_project_chat(data[CHAT]),
                                _project_task(data[TASK])).get(session, "")
        if ident:
            return _put_bound(data, ident, body, rows)
        if not body.strip() and not rows:
            # The delete, taken here rather than through `delete_chat` so the
            # question "is this key bound?" and the answer to it are one turn of
            # the lock.
            if key not in data[CHAT]:
                return None, False
            data[CHAT].pop(key, None)
            return None, True
        record = {"text": body, "attachments": rows, "updated_at": time.time()}
        data[CHAT][key] = record
        # STORED without `bound_draft` and ANSWERED with it: the field is a fact
        # about which record these words are in, not a field of the record, and
        # writing it into the file would be a second place for it to go stale.
        return dict(record, bound_draft=""), True

    return _update(mutate)


def delete_chat(session_id) -> bool:
    """Drop one chat draft; True if there was one. Called on send, on an
    explicit clear, and by every verb that takes the task away —
    archive/delete/erase — because a draft for a conversation nobody can reach
    any more is a badge on a row that is gone."""
    key = chat_key(session_id)
    if not key:
        return False

    def mutate(data: dict):
        if key not in data[CHAT]:
            return False, False
        data[CHAT].pop(key, None)
        return True, True

    return _update(mutate)


# ------------------------------------------------------------- task drafts


def _task_record(rec) -> dict | None:
    if not isinstance(rec, dict):
        return None
    out = {field: "" for field in _TASK_TEXT}
    for field in _TASK_TEXT:
        out[field] = _text(rec.get(field))
    out["when"] = _jsonable(rec.get("when"))
    out["repeat"] = _jsonable(rec.get("repeat"))
    # THE RULE BEHIND A `custom` REPEAT (Bugbot, PR #1118). `repeat` is a preset
    # KEY, and every key but one is its own whole answer — "every day" needs no
    # second field. `custom` is a pointer at a rule the recurrence dialog built,
    # so a draft that stored the key and dropped the rule reopened saying Custom,
    # holding nothing, with Save refused and nothing on the card saying why.
    # Pass-through like `when` and `repeat`, and for the same reason: this store
    # is not the authority on what a recurrence rule looks like, and a shape it
    # validated would be a second copy of `recur`'s grammar going stale.
    out["custom_rule"] = _jsonable(rec.get("custom_rule"))
    out["attachments"] = _attachments(rec.get("attachments"))
    out["new_task_each_run"] = _flag(rec.get("new_task_each_run"))
    # WHERE THESE WORDS CAME FROM, and it is stored rather than merely acted on
    # (design.md, Round 2: "A draft moves, never duplicates"). The hop deletes
    # the chat draft as it writes this one, so without a record of the key the
    # move is irreversible the moment the modal is closed: a draft REOPENED from
    # its row has no `?back=` in the URL and no sessionStorage stash left, and
    # "Back to chat" — the only thing that puts the sentence back where it was
    # typed — had nothing to aim at. Validated through `chat_key`, so the field
    # is either a key the chat half can actually be written under or "" (Akshil,
    # 2026-09-11).
    out["from_chat_key"] = chat_key(rec.get("from_chat_key"))
    # WHICH CONVERSATION THIS DRAFT IS A MESSAGE TO, when it came out of one
    # that already exists (Akshil, 2026-09-12).
    #
    # `from_chat_key` above says where the WORDS were typed and is spent the
    # moment the chat's own copy is deleted; this says where the TASK is going,
    # and it has to outlive the modal being closed. Without it the hop worked
    # only while the page still held the session in memory: press Schedule
    # straight away and the message landed in the conversation, exit the modal
    # and the draft on disk knew nothing about it — so reopening that draft and
    # scheduling it started a NEW session under a NEW task number, and the task
    # the reader had been watching was gone.
    #
    # Validated as a session id and never as `new:<file>` (`bound_session`):
    # binding is to a thread, not to a folder.
    out["session_id"] = bound_session(rec.get("session_id"))
    out["created_at"] = _epoch(rec.get("created_at"))
    out["updated_at"] = _epoch(rec.get("updated_at"))
    return out


def _empty_task(record: dict) -> bool:
    """Is there nothing in this draft at all?

    Words or files, and nothing else — see `_TASK_CONTENT`. A form whose title
    and description are blank and whose tray is empty is not an unfinished task,
    whatever folder or model or time it happens to be carrying: those are
    settings, and settings are how a task would run if there were one. So a PUT
    that arrives in that state is a DELETE, which is the same semantics this
    store has always had for an empty draft — the change is only in what
    "empty" counts as (Akshil, 2026-09-12)."""
    if any(record[field].strip() for field in _TASK_CONTENT):
        return False
    return not record["attachments"]


def _project_task(section: dict) -> dict:
    """The task section of an ALREADY-LOADED store, projected. Split out of
    `list_task` for the same reason as `_project_chat`."""
    out: dict[str, dict] = {}
    for ident, rec in section.items():
        if not draft_id(ident):
            continue
        record = _task_record(rec)
        if record is not None:
            out[ident] = record
    return out


def list_task() -> dict:
    """Every task draft, `{draft_id: {…fields, created_at, updated_at}}`."""
    return _project_task(load()[TASK])


def list_all() -> tuple[dict, dict]:
    """Both sections off ONE read of the file: `(task_drafts, chat_drafts)`.

    `list_task()` and `list_chat()` are each a whole `load()`, and the callers
    that want drafts almost always want both — the tasks listing asks for the
    chat drafts to join onto its rows and the task drafts to build draft rows
    from, on every build, including the `/api/tasks/changes` polls. One file,
    one read. Same projections AND the same chat view — a bound form reads as
    its session's chat draft here exactly as it does through `list_chat` — so
    this is interchangeable with calling the two in turn."""
    store = load()
    task = _project_task(store[TASK])
    return task, chat_view(_project_chat(store[CHAT]), task)


def get_task(ident) -> dict | None:
    key = draft_id(ident)
    return list_task().get(key) if key else None


def _fold_into_bound(existing: dict, incoming: dict) -> dict:
    """The incoming write folded INTO the draft that already holds its session.

    What survives is everything the incoming write did not actually say. The
    caller that lands here is a card that could not read the binding before it
    wrote — `fetchDrafts` answers "unknown" on a failed lookup and the modal
    opens anyway, because words must not be lost waiting for a request — so the
    form it is saving may be a fresh one carrying only the composer's sentence,
    and the draft it is colliding with is the one holding the time, the repeat
    rule, the model and the tray somebody spent a minute choosing. Overwriting
    those with the blanks of a form that never asked about them is how the
    reported loss happened; keeping them is this function.

    NON-EMPTY IS THE WHOLE TEST, field by field. A blank title is not "clear the
    title", it is "this write has nothing to say about the title" — a draft
    store is the one place where that reading is always the safe one, since the
    alternative destroys work and the cost of being wrong is one stale setting
    the reader can see and change. Attachments are a UNION keyed on `path`, for
    the same reason read as rows: neither side's files are a statement that the
    other side's are gone.

    `session_id` is the exception and takes the incoming value outright — it is
    the very fact that brought these two records together, and it is the same on
    both sides by construction."""
    out = dict(existing)
    for field in _TASK_TEXT:
        if incoming[field].strip():
            out[field] = incoming[field]
    for field in ("when", "repeat", "custom_rule"):
        if incoming[field] not in (None, "", [], {}):
            out[field] = incoming[field]
    if incoming["new_task_each_run"] is not None:
        out["new_task_each_run"] = incoming["new_task_each_run"]
    if incoming["from_chat_key"]:
        out["from_chat_key"] = incoming["from_chat_key"]
    out["session_id"] = incoming["session_id"]
    rows: list[dict] = []
    seen: set[str] = set()
    for row in list(existing["attachments"]) + list(incoming["attachments"]):
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        rows.append(row)
    out["attachments"] = rows
    return out


def put_task(ident, fields) -> tuple[dict | None, str]:
    """Upsert one task draft — and DELETE it when every field comes in empty.

    Fields the client did not send keep the value the stored draft had, so the
    modal's autosave may send only what it knows; fields the client sent that
    this store does not know are dropped silently, so a newer page cannot 400
    against an older server (see `TASK_FIELDS`).

    `created_at` is written once and then never moves — it is the row's `at` on
    the List and the Board, and a draft that jumped to the top of Upcoming on
    every keystroke would be a row that will not sit still. Answers
    `(record, canonical_id)`: the stored record (None when the write was a
    delete) and the id it is actually stored under, which is `ident` on every
    ordinary write and somebody else's id when this write was folded into a
    draft that already held its session (below). The caller has to pass that id
    back to the page, or the next autosave, the Discard and the Schedule all aim
    at a record that is no longer there.

    ONE BOUND DRAFT PER SESSION, enforced here rather than trusted to the
    client (review, 2026-09-12). A session-bound draft stands in for its
    session's row (`_bound_chips` in routers/tasks.py) and that row wears
    exactly one `✎ Draft` chip, so two drafts naming the same session is not
    two facts, it is one fact written twice.

    IT IS A MERGE AND NOT AN EVICTION (Bugbot, PR #1126, 2026-09-12). The first
    version of this rule deleted the loser, which is correct only if the winner
    is the better-informed of the two — and the write that collides is by
    construction the one that knows LESS. `fetchDrafts` never rejects: a lookup
    that fails on the way into the hop reads as "this session has no form", so
    the card mints a NEW id and saves a fresh form over a stored draft carrying
    a time, a repeat rule, a model and a tray. Evicting there threw all of that
    away over one failed GET. Folding the incoming write into the existing
    record instead (`_fold_into_bound`) makes a failed lookup cost nothing: the
    words land, the settings stay, the id the reader keeps is the one that was
    already bound. Done inside THIS write's lock rather than as a second
    request, so nothing can observe the moment two drafts both claim one
    session."""
    key = draft_id(ident)
    if not key:
        return None, ""
    patch = fields if isinstance(fields, dict) else {}

    def mutate(data: dict):
        stored = _task_record(data[TASK].get(key)) or {}
        merged = dict(stored)
        for field in TASK_FIELDS:
            if field in patch:
                merged[field] = patch[field]
        record = _task_record(merged)
        if record is None or _empty_task(record):
            if key not in data[TASK]:
                return (None, key), False
            data[TASK].pop(key, None)
            return (None, key), True
        now = time.time()
        record["created_at"] = stored.get("created_at") or now
        record["updated_at"] = now
        session = record["session_id"]
        bound_id, bound = "", None
        if session:
            for other_id, other_rec in data[TASK].items():
                if other_id == key:
                    continue
                other = _task_record(other_rec)
                if other is None or other["session_id"] != session:
                    continue
                # NEWEST WINS if a store somehow holds several — the same
                # tie-break `_bound_chips` takes, so what this write folds into
                # is the draft the session's row is advertising.
                if bound is not None and bound["updated_at"] >= other["updated_at"]:
                    continue
                bound_id, bound = other_id, other
        if bound is not None:
            record = _fold_into_bound(bound, record)
            record["created_at"] = bound["created_at"] or now
            record["updated_at"] = now
            data[TASK].pop(key, None)
            data[TASK][bound_id] = record
            return (record, bound_id), True
        data[TASK][key] = record
        return (record, key), True

    return _update(mutate)


def unbind_session(session_id) -> list[str]:
    """Cut every task draft loose from one session; the LISTING KEYS of the ones
    that were cut (`draft:<id>`), newest-store order, and `[]` when none were.

    THE KEYS AND NOT A COUNT (bugbot, PR #1126): those rows have just changed —
    a different number, a different folder, no session — and the caller has to
    announce them (`tasks_watch.notify`) or the page goes on showing the erased
    session's number on a draft whose Schedule would send the message back into
    a conversation that no longer exists. A count cannot be announced.

    The erase gesture's share of this store (`POST /api/tasks/erase`). The words
    are NOT deleted — a draft is a task somebody is still writing, and the
    conversation it was going to be sent into is only where it was going to go.
    What goes is the binding, and with it everything the binding stood for: the
    draft stops borrowing a number that is now a reservation nobody may reissue
    (`tasks_store.forget_session` stamps the record `erased` and keeps it), stops
    standing in for a row that no longer exists, and is numbered as the ordinary
    `draft:<id>` it has become on the next listing.

    Without this the draft showed the dead TASK-nnn for ever and could never be
    allocated one of its own, because the binding was the very thing telling
    `_draft_numbers` that its task already had a number (review, 2026-09-12).

    One pass under one lock, and `[]` — no write at all — for the overwhelmingly
    common erase where nothing was bound to that session."""
    target = bound_session(session_id)
    if not target:
        return []

    def mutate(data: dict):
        cut: list[str] = []
        for ident, rec in list(data[TASK].items()):
            record = _task_record(rec)
            if record is None or record["session_id"] != target:
                continue
            # `updated_at` is NOT touched: it is the clock the row prints
            # ("drafted 5m ago") and sorts on, and nobody typed anything here.
            record["session_id"] = ""
            data[TASK][ident] = record
            cut.append(task_key(ident))
        return cut, bool(cut)

    return _update(mutate)


def delete_task(ident) -> bool:
    """Discard one task draft; True if there was one. Called by the modal's
    Discard button and by `POST /api/schedule` once the draft has become a real
    scheduled entry — the draft's whole purpose is over at that moment, and a
    row left behind would be the same task twice."""
    key = draft_id(ident)
    if not key:
        return False

    def mutate(data: dict):
        if key not in data[TASK]:
            return False, False
        data[TASK].pop(key, None)
        return True, True

    return _update(mutate)


def _epoch(value) -> float:
    """A stored stamp as a float. 0.0 for one that cannot be read, which is how
    every other absent time in this feature reads (`tasks_store`'s rows, the
    listing's `last_active`)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
