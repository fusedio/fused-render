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

    {"chat": {"<session-id>": {"text": …, "attachments": […], "updated_at": …,
                               "version": …, "form": {…}?}},
     "task": {"<draft-id>": {"title": …, …, "created_at": …, "updated_at": …,
                             "version": …}}}

**One record, one version.** Every record carries a `version` an int at a time,
stamped by `_update` on each write (`_stamp`), and every write may be
conditional on it (`if_version`, the routes' `If-Match`): a second window that
saved since the caller last read loses with a `VersionConflict` carrying the
record it lost to, instead of overwriting it. `form` is the other half of that
round — the Schedule hop's New task modal edits the CHAT record it was opened
on rather than minting a `draft:<id>` of its own, so a chat draft carries the
settings the modal set (`CHAT_FORM_FIELDS`) beside the words
(design-drafts-one-record.md).

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

#: …and a third section that is not a kind of draft: the LAST WRITE EACH PAGE
#: MADE to each key, `{key: {"client": …, "seq": …, "at": …}}`.
#:
#: A version orders two DIFFERENT writers against each other; it cannot order
#: one writer against itself, because both of its requests state the version
#: they read and the network decides which arrives first. One page's own saves
#: for one key are a SEQUENCE — the syncer client-side mints a monotonic `seq`
#: per key and sends the whole desired state each time (`platform/lib/drafts.ts`,
#: `draftSyncer`) — so a request whose `seq` is not newer than the one already
#: applied from that same page is a STRAGGLER, and the only correct thing to do
#: with it is nothing.
#:
#: IN ITS OWN SECTION AND NOT ON THE RECORD, because the case that matters most
#: is the one where there IS no record: a send DELETEs and a keepalive PUT from
#: a tenth of a second earlier arrives afterwards. A seq kept on the record would
#: have gone with it, and the sent sentence would come back as a live draft —
#: the resurrection this whole round exists to make impossible. So the note
#: outlives the record, briefly (`_SEQ_TTL_SEC`).
SEQ = "seq"

#: How long one of those notes is worth keeping. A straggler is a request that
#: is already on the wire; a minute is an eternity for one, and a quarter of an
#: hour means a suspended laptop's queued write is still judged against the page
#: that queued it. After that the note is noise and `_prune` drops it, so the
#: section cannot grow without bound on a machine that opens a thousand folders.
_SEQ_TTL_SEC = 15 * 60

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

#: How long an unsent chat with no session yet may sit in the store before
#: `_prune` forgets it. A fortnight, which is long enough to cover a holiday and
#: short enough that the Upcoming lane is not a museum. Only `new:<file>` keys
#: age — see `_prune` for why the other two kinds never do.
CHAT_TTL_SEC = 14 * 24 * 60 * 60

#: Every field a task draft stores, in the order the form reads them. A key the
#: client sends that is not in here is DROPPED rather than refused (the modal
#: gains fields over time and an older server must not start 400ing a newer
#: page), and a field the client omits keeps whatever the stored draft had.
TASK_FIELDS = ("title", "description", "target", "when", "repeat", "custom_rule",
               "model", "effort", "permission", "attachments",
               "new_task_each_run", "session_id")

#: The SETTINGS half of a draft, spelled once and read from either record.
#:
#: A hop out of the composer no longer mints a second record: the New task modal
#: edits the chat draft it was opened on, so a `new:<file>` chat record carries
#: the form's fields beside its text, and a session-bound task record answers
#: the same shape out of the fields it already stores (`_form_of_task`). One
#: vocabulary, so a reader of `/api/drafts` never has to ask which of the two
#: records it is looking at (design-drafts-one-record.md, "Server model
#: deltas").
#:
#: The WORDS are not in here. They live in `text` on a chat record and in
#: `title`/`description` on a task record, joined and split by the one rule both
#: doors use (`split_draft`). `title` is the exception and is a form field too,
#: because the modal has a title box of its own that a person may edit away from
#: the first line of what they typed.
CHAT_FORM_FIELDS = ("title", "when", "repeat", "custom_rule", "model", "effort",
                    "permission", "target", "new_task_each_run")

#: The form fields that are plain text, normalised through `_text`. The rest are
#: pass-through json (`when`, `repeat`, `custom_rule`) or a tri-state flag
#: (`new_task_each_run`) — the same split `_TASK_TEXT` draws, for the same
#: fields.
_FORM_TEXT = ("title", "model", "effort", "permission", "target")

#: The fields that are plain text, normalised through `_text` on the way in. The
#: rest are pass-through (`when`, `repeat`, `custom_rule`), a tri-state flag
#: (`new_task_each_run`), rows (`attachments`), or the session this form is a
#: message to (`session_id`) — the last being a key rather than free text, and
#: validated as one.
#:
#: THIS IS A LIST OF SHAPES, NOT A DEFINITION OF CONTENT — see `_TASK_CONTENT`
#: below, which is the one that decides whether there is a draft here at all.
#: `session_id` is in neither: it is a destination, not something a person
#: typed. A form that arrives blank is still a delete even when it names the
#: session it was going to — which is exactly the bargain `an empty task put
#: keeps the chat draft` rests on (Akshil, 2026-09-11).
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


# --------------------------------------------------------------- the version


class VersionConflict(Exception):
    """A conditional write lost — the record moved since the caller read it.

    Carries the CURRENT record and the version it is at, because a bare refusal
    is not something a client can act on: the editor that lost has to decide
    between adopting what is on disk and re-sending its own words, and either
    answer needs the state it collided with (design-drafts-one-record.md, §2).
    Raised out of the store rather than answered as a value so no caller can
    forget to look: every write path either returns a record or does not
    return."""

    def __init__(self, key: str, version: int, record=None):
        super().__init__("draft version conflict on %r" % (key,))
        self.key = key
        self.version = int(version or 0)
        self.record = record


def version_of(record) -> int:
    """One record's version, 0 for anything that has none.

    0 is "there is no record here", which is what a client that has never
    written this key holds — so `If-Match: 0` reads as "I expect nothing" and
    creates, and the two are one arithmetic rather than a special case."""
    if not isinstance(record, dict):
        return 0
    try:
        value = int(record.get("version") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


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
    return TASK_KEY_PREFIX + ident


#: What `task_key` puts in front of a task draft's id. Named so the one caller
#: that has to read such a key back — the changes endpoint, which is handed
#: listing keys and has to say which store to look each one up in — does not
#: spell the prefix a second time.
TASK_KEY_PREFIX = "draft:"


def task_draft_id(key) -> str:
    """The draft id inside a `draft:<id>` listing key, or `""`.

    `task_key` run backwards, and the tolerant twin of `draft_id`: a key of any
    other shape (a session id, `new:<file>`, a pending key) answers `""` rather
    than raising, because the caller is asking WHICH KIND of key this is."""
    if not isinstance(key, str) or not key.startswith(TASK_KEY_PREFIX):
        return ""
    return draft_id(key[len(TASK_KEY_PREFIX):])


# ------------------------------------------------------------------ the file


def load() -> dict:
    """The whole store, `{"chat": {}, "task": {}}` — missing or corrupt is not
    an error. Both sections always present, so no caller has to test for them.

    Stale `new:<file>` chat drafts are dropped on the way out — see `_prune`."""
    data = None
    try:
        with open(os.path.join(STATE_DIR, DRAFTS_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    for section in (CHAT, TASK, SEQ):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    _prune(data)
    return data


class StaleWrite(Exception):
    """This request is a straggler from a page that has already said something
    newer about this key, so it was DROPPED.

    Not an error and not a conflict: nobody lost anything and there is nothing
    for the client to resolve — the write it is being refused is one it has
    itself superseded. The routes answer 200 `{"ok": true, "dropped": true}`
    with the record as it stands, so a caller that is not looking sees a write
    that succeeded, which for its purposes it did: the state it wanted IS what
    the newer request put there.

    Carries the current record for the same reason `VersionConflict` does — the
    version on it is the one the client should be writing against next."""

    def __init__(self, key: str, record=None):
        super().__init__("stale draft write on %r" % (key,))
        self.key = key
        self.record = record


def _seq_of(value) -> int | None:
    """One request's `seq`, or None for "this client is not counting".

    None is the answer for every client written before this round, and it means
    the write is judged on its version alone — which is exactly how it behaved
    then. A bool is not an int here (it is in Python), and a negative number is
    no sequence at all."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _client_of(value) -> str:
    """The random id one PAGE mints for itself, or `""`.

    Bounded and stripped, because it becomes a json key's value in a file this
    process writes; nothing is inferred from its shape."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:64]


def _seq_fresh(data: dict, key: str, client: str, seq: int | None) -> bool:
    """Is this write NEWER than the last one this page made to this key?

    True for a client that sends no `seq` (nothing to compare), true the first
    time a page writes a key, and true for a DIFFERENT page's write however old
    its own counter is — one note per key, so the last page to write is the one
    being sequenced. That is the whole of the rule: a sequence orders a page
    against itself, and versions go on ordering pages against each other."""
    if not client or seq is None:
        return True
    row = (data.get(SEQ) or {}).get(key)
    if not isinstance(row, dict) or row.get("client") != client:
        return True
    try:
        seen = int(row.get("seq"))
    except (TypeError, ValueError):
        return True
    return seq > seen


def _seq_note(data: dict, key: str, client: str, seq: int | None) -> None:
    """Remember this write as the newest one that page made to this key.

    Only ever the LATEST `{client, seq}` — a page's whole history under a key is
    of no interest, and keeping ids for ever would make this file a record of
    every tab that ever opened a draft."""
    if not client or seq is None:
        return
    data.setdefault(SEQ, {})[key] = {"client": client, "seq": int(seq),
                                     "at": time.time()}


def _prune(data: dict) -> None:
    """Forget the `new:<file>` chat drafts nobody has touched in a fortnight.

    ONLY THAT ONE SHAPE (design.md, PR C, "TTL"). A `new:` key is a folder
    somebody opened a chat on and typed half a sentence into; it is a ROW on the
    Tasks page with a TASK number of its own, and a machine in daily use grows
    one per folder ever opened — a permanent list of month-old fragments nobody
    will finish, each holding a number. A chat draft on a REAL session is the
    next message of a conversation that is still there, and a task draft is a
    form somebody is filling in: both are things a reader can still find their
    way back to from the row they are attached to, so neither ages out.

    IN `load()` RATHER THAN IN A SWEEP, and so with no write of its own. Every
    read comes through here, `_update` reads through here INSIDE its lock, and
    `_update` writes back the dict it was handed — so the prune is observed by
    every reader at once and is persisted by the next write that changes
    anything else. A timer that took the lock to delete text nobody had asked
    about would be a risk run on nobody's behalf.

    A RECORD WITH NO READABLE STAMP IS KEPT. `_epoch` answers 0.0 for one, which
    is literally older than any cutoff — and this store's whole job is not to
    lose what somebody typed, so an unreadable clock buys a draft its life
    rather than costing it."""
    cutoff = time.time() - CHAT_TTL_SEC
    chat = data[CHAT]
    for key in [k for k in chat if isinstance(k, str) and is_new_chat_key(k)]:
        record = chat.get(key)
        stamp = _epoch(record.get("updated_at")) if isinstance(record, dict) else 0.0
        if 0.0 < stamp < cutoff:
            chat.pop(key, None)
    # …AND THE SEQUENCE NOTES NOBODY CAN STILL BE RACING (`SEQ`). A note exists
    # to drop a request that is on the wire right now; a quarter of an hour
    # later there is no such request, and keeping it would make this section
    # grow for ever. Unlike a draft, losing one of these costs nothing anybody
    # typed, so an unreadable stamp is dropped here rather than kept.
    seq_cutoff = time.time() - _SEQ_TTL_SEC
    notes = data.setdefault(SEQ, {})
    for key in list(notes):
        row = notes.get(key)
        if not isinstance(row, dict) or _epoch(row.get("at")) < seq_cutoff:
            notes.pop(key, None)


def _stamp(before: dict, data: dict) -> None:
    """Give every record this write TOUCHED the next version of itself.

    BY IDENTITY, not by comparing contents: every writer in this file replaces a
    record with a freshly built dict rather than editing the stored one in
    place, so "is this a different object than the one `load()` handed us" is
    exactly "did this write touch it" — and it is a test no writer can forget,
    which is the whole reason the stamp lives here instead of in each of them
    (design-drafts-one-record.md, §2: "`_update()` increments `version` on every
    mutation").

    A version is per KEY and monotonic: prior + 1, so a record that is deleted
    and written again starts over at 1 — a client holding version 9 of a draft
    somebody discarded collides with the new record rather than silently
    matching it, which is the resurrection this whole round exists to make
    impossible. Deletions need no stamp; the key is simply gone, and `gone` is
    what the changes endpoint says about it."""
    for section in (CHAT, TASK):
        old = before.get(section) or {}
        for key, record in data[section].items():
            if not isinstance(record, dict) or old.get(key) is record:
                continue
            record["version"] = version_of(old.get(key)) + 1


def _update(mutate):
    """Read-modify-write the store under an exclusive lock; return whatever
    `mutate` returns.

    A copy of `tasks_store._update`, sibling `.lock` file and all, and a copy
    rather than a call for the module docstring's reason: this module imports
    nothing. The lock covers the READ as well as the write for the reason
    documented there — the app runs several windows against one server, and a
    second writer holding a snapshot taken before the first one's change would
    persist the loss.

    …AND THE VERSION BUMP, AND THE `If-Match` CHECK, are both inside it. A
    compare that happened outside this lock would be a check against a store
    somebody else is already writing, which is the same lost update read as a
    guarantee. `mutate` raises `VersionConflict` for the mismatch and nothing is
    written; `_stamp` numbers whatever it did write.

    `mutate` MAY ANSWER A CALLABLE, which is called with the store after the
    stamp and whose answer is returned instead. That is how a writer returns the
    record it just wrote WITH its new version on it: the number does not exist
    until the stamp, which by construction happens after the write that earned
    it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, DRAFTS_FILE)
    with open(path + ".lock", "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        before = {section: dict(data[section]) for section in (CHAT, TASK)}
        result, changed = mutate(data)
        if changed:
            _stamp(before, data)
            # Temp + rename, unlike tasks_store's plain overwrite: this file
            # holds text a person typed and has not sent, which is the one
            # thing in `claude-sessions/` that cannot be rebuilt from anywhere
            # else. A half-written read.json costs read marks; a half-written
            # drafts.json costs the draft (Akshil, 2026-09-11).
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        if callable(result):
            result = result(data)
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


def _form(value, patch: bool = False) -> dict:
    """A draft's SETTINGS as stored — the `CHAT_FORM_FIELDS` subset of whatever
    came in, everything else dropped.

    Dropped rather than refused, the same bargain `TASK_FIELDS` makes and for
    the same reason: the modal grows a field long before an installed server
    learns about it, and a 400 there would cost the words on the page.

    `patch=True` keeps only the keys the caller ACTUALLY SENT, which is what
    makes a form a patch: a composer autosave that says nothing about the time
    must not clear the time, and `{"when": null}` must. Without it a form is
    read whole and missing fields read as empty, which is what a reader of a
    stored record wants."""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for field in CHAT_FORM_FIELDS:
        if patch and field not in value:
            continue
        if field in _FORM_TEXT:
            out[field] = _text(value.get(field))
        elif field == "new_task_each_run":
            out[field] = _flag(value.get(field))
        else:
            out[field] = _jsonable(value.get(field))
    return out


def _form_of_task(record: dict) -> dict:
    """The same settings read off a TASK record, which stores them as fields of
    its own.

    ONE VOCABULARY FOR TWO RECORDS (design-drafts-one-record.md, §1: "the
    schedule hop edits the same record"). A chat draft keys its settings under
    `form`; a session-bound task draft IS the form. A reader asking "what time
    is this draft set for" must not have to know which of the two it is holding,
    so both answer through this shape."""
    return {field: record.get(field) for field in CHAT_FORM_FIELDS}


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
    for both, so a reader never has to test for the key.

    `form` is `{}` on a record nobody has scheduled, and the settings the
    Schedule hop edited on one somebody has (`CHAT_FORM_FIELDS`). Always
    present, for the same one-shape reason."""
    out: dict[str, dict] = {}
    for key, rec in section.items():
        if not chat_key(key) or not isinstance(rec, dict):
            continue
        out[key] = {"text": _text(rec.get("text")),
                    "attachments": _attachments(rec.get("attachments")),
                    "updated_at": _epoch(rec.get("updated_at")),
                    "version": version_of(rec),
                    "bound_draft": "",
                    "form": _form(rec.get("form"))}
    return out


def bound_chats(chat: dict, task: dict) -> dict[str, str]:
    """`{session-id: draft-id}` — every conversation whose unsent words are
    being kept in a New task FORM rather than in a chat record of its own.

    ONE RECORD, TWO DOORS (Akshil, 2026-09-12). A task draft can name the
    conversation it is the next message of (`session_id`), and the reported bug
    was what the session's row looked like when one did: it wore
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


def _bound_view(ident: str, record: dict) -> dict | None:
    """One task draft read as the chat draft it is a second door onto, or None
    when there is nothing in it to read.

    The projection `chat_view` makes, split out so `put_chat` can answer the
    write it just made through the bound door in the same shape the reader gets
    — and with the version the write earned, which is the task record's."""
    text = join_draft(record.get("title"), record.get("description"))
    rows = _attachments(record.get("attachments"))
    if not text.strip() and not rows:
        return None
    return {"text": text,
            "attachments": rows,
            "updated_at": _epoch(record.get("updated_at")),
            "version": version_of(record),
            "bound_draft": ident,
            "form": _form_of_task(record)}


def chat_view(chat: dict, task: dict) -> dict:
    """The chat half as a READER sees it: every stored chat draft, plus one
    synthesized entry for every session whose words live in a bound form
    (`bound_chats`).

    A synthesized entry is the form read as a composer would write it — the
    title and description joined back into one box (`join_draft`, the inverse
    of the split the hop made), the form's attachments, the form's clock — plus
    `bound_draft`, the form's id, so a client that cares can tell the two apart.
    Nothing here writes: this is a projection of one read, and the write that
    goes back through the same door is `put_chat`'s half of the bargain.

    A WORDLESS FORM IS NOT A DRAFT HERE, even though its record survives. Since
    `_put_bound` stopped deleting the whole form when the composer empties (the
    settings the reader spent a minute choosing are not theirs to throw away),
    a bound record can legitimately hold a time, a repeat rule and a model with
    nothing typed in it — and that is not something anybody is still writing. It
    is skipped, so `get_chat` answers None, the session's row wears no `✎ Draft`
    chip, and a composer seeding off this key gets the empty box it should.

    A WORDLESS STORED RECORD IS SKIPPED FOR THE SAME REASON, and there is now
    such a thing: a chat draft that carries a `form` survives its words being
    cleared, exactly as a bound form does, because the time and the repeat rule
    somebody chose in the hop are not the composer's to throw away. One rule for
    both records, since after this round they are the same record seen through
    two doors (design-drafts-one-record.md, §1)."""
    out = {key: record for key, record in chat.items()
           if record["text"].strip() or record["attachments"]}
    for session, ident in bound_chats(chat, task).items():
        view = _bound_view(ident, task[ident])
        if view is not None:
            out[session] = view
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


def _put_bound(data: dict, ident: str, body: str, rows: list[dict], form=None):
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

    EMPTY CLEARS THE WORDS AND KEEPS THE FORM (Akshil, 2026-09-15; design.md,
    PR C). It used to delete the whole record, on the chat half's reasoning that
    there is no half of a draft to keep — true of a chat draft, which IS its
    text, and false of a form. This door is the COMPOSER, and the composer's
    autosave fires after every send; the other door is a card holding a folder, a
    time, a repeat rule, a model, an effort, a permission mode and a tray that
    somebody spent a minute choosing. Sending a message into the conversation
    threw all of that away, and the person who had been setting it up had no way
    to know that pressing Enter was the gesture that did it.

    So an emptying write clears exactly what this door can see — title,
    description, attachments — and leaves every setting where it was. The record
    is then wordless, which every reader already treats as no draft: `chat_view`
    skips it, `_bound_chips` draws no chip, and `_empty_task` says a form with no
    words and no files is nothing anybody is writing. Deleting the RECORD is
    still possible and is still what the reader's own gesture does — Discard in
    the modal, the trash on the row, `DELETE /api/drafts/chat/<key>` on the
    composer's send — all of which go through an explicit delete rather than
    through here.

    `None` is answered either way, because the question this door was asked is
    "what does the composer hold now" and the answer is still nothing.

    THE SETTINGS COME THROUGH THIS DOOR TOO, as of the one-record round. The
    Schedule hop no longer mints a second draft: the New task modal it opens
    edits THIS record, writing the time, the repeat rule and the model as
    `form` alongside the words (`CHAT_FORM_FIELDS`), and on a bound session
    those fields already ARE the task record's own. A patch, field by field, so
    the composer's ordinary autosave — which says nothing about any of them —
    cannot clear what the modal set.

    `title` is the one form field this door does not take: here the WORDS own
    it, split off the box by `split_draft`, and letting a form overwrite it
    would be the two doors disagreeing about one sentence."""
    patch = _form(form, patch=True) if isinstance(form, dict) else {}
    patch.pop("title", None)
    title, description = split_draft(body)
    words = bool(title or description or rows)
    stored = _task_record(data[TASK].get(ident))
    if stored is None and not words:
        return None, False
    record = dict(stored) if stored is not None else (_task_record({}) or {})
    had = bool(record["title"] or record["description"] or record["attachments"])
    if not words and not had and not patch:
        return None, False  # already wordless, and this write says nothing else
    record["title"] = title
    record["description"] = description
    record["attachments"] = rows
    for field, value in patch.items():
        record[field] = value
    now = time.time()
    record["created_at"] = record.get("created_at") or now
    record["updated_at"] = now
    data[TASK][ident] = record
    if not words:
        return None, True
    return (lambda d: _bound_view(ident, _task_record(d[TASK].get(ident)) or {})), True


def _has_form(form: dict) -> bool:
    """Is there anything in these settings? A form of nothing but blanks is no
    form at all, and storing one would put an empty `form` key on every chat
    draft in the file."""
    return any(value not in (None, "", [], {}) for value in form.values())


def _chat_current(data: dict, key: str, ident: str) -> dict | None:
    """What is under this chat key RIGHT NOW, read the way a client reads it —
    the body of a 409.

    Answers a WORDLESS record too, unlike `chat_view`, which is the difference
    between the two questions: the reader is asking "is there a draft here"
    (a wordless one is not), and a loser of a conditional write is asking "what
    version am I up against" (there is one, and refusing to say would leave the
    client retrying against a number it can never learn)."""
    if ident:
        record = _task_record(data[TASK].get(ident))
        if record is None:
            return None
        return _bound_view(ident, record) or {
            "text": "", "attachments": [], "updated_at": record["updated_at"],
            "version": version_of(record), "bound_draft": ident,
            "form": _form_of_task(record)}
    if key not in data[CHAT]:
        return None
    return _project_chat({key: data[CHAT][key]}).get(key)


def put_chat(session_id, text=None, attachments=None, form=None,
             if_version=None, client=None, seq=None) -> dict | None:
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
    sentence ends up in both halves of the store.

    `form` IS A PATCH AND IS OPTIONAL. The New task modal opened by the
    Schedule hop writes back through this door — same key, same record, no
    `draft:<id>` minted — so the settings it edits ride here beside the words
    (`CHAT_FORM_FIELDS`). Omitted, the stored settings stand: the composer's own
    autosave knows nothing about them and must not be able to clear them. Sent,
    only the fields it names move, so `{"when": null}` clears a time and a form
    that never mentions the model keeps it.

    EMPTYING A DRAFT THAT CARRIES SETTINGS CLEARS THE WORDS AND KEEPS THEM —
    the same bargain `_put_bound` makes on a bound session, now that the two are
    one record seen through two doors. The record then reads as no draft to
    every reader (`chat_view` skips it, no row, no chip), so nothing about that
    is visible; what survives is the time somebody chose.

    `if_version` is the caller's `If-Match`: the version it believes this key is
    at, compared inside the lock against the record this write would touch —
    the bound form's when there is one, since that is the record being written.
    A mismatch raises `VersionConflict` carrying the current state and writes
    nothing; `None` is an unconditional write, which is what every client that
    predates versions sends.

    `client`/`seq` are the page's own sequence for this key (`SEQ`): a write
    that is not newer than the last one THAT page made here raises `StaleWrite`
    and changes nothing. Checked BEFORE the version, because a straggler is not
    a conflict — nobody is losing an edit, the page has simply said something
    newer already."""
    key = chat_key(session_id)
    if not key:
        return None
    body = _text(text)
    rows = _attachments(attachments)
    page = _client_of(client)
    count = _seq_of(seq)

    def mutate(data: dict):
        ident = ""
        session = bound_session(key)
        if session:
            ident = bound_chats(_project_chat(data[CHAT]),
                                _project_task(data[TASK])).get(session, "")
        if not _seq_fresh(data, key, page, count):
            raise StaleWrite(key, _chat_current(data, key, ident))
        _seq_note(data, key, page, count)
        if if_version is not None:
            current = data[TASK].get(ident) if ident else data[CHAT].get(key)
            if version_of(current) != if_version:
                raise VersionConflict(key, version_of(current),
                                      _chat_current(data, key, ident))
        if ident:
            return _put_bound(data, ident, body, rows, form)
        stored = data[CHAT].get(key)
        settings = _form((stored or {}).get("form"))
        merged = dict(settings)
        merged.update(_form(form, patch=True) if isinstance(form, dict) else {})
        keep = _has_form(merged)
        if not body.strip() and not rows:
            # The delete, taken here rather than through `delete_chat` so the
            # question "is this key bound?" and the answer to it are one turn of
            # the lock.
            if stored is None:
                return None, False
            if not keep:
                data[CHAT].pop(key, None)
                return None, True
            was_wordless = not (_text(stored.get("text")).strip()
                                or _attachments(stored.get("attachments")))
            if was_wordless and merged == settings:
                return None, False  # already wordless, and the settings stand
            data[CHAT][key] = {"text": "", "attachments": [],
                               "updated_at": time.time(), "form": merged}
            return None, True
        record = {"text": body, "attachments": rows, "updated_at": time.time()}
        if keep:
            record["form"] = merged
        data[CHAT][key] = record
        # STORED without `bound_draft` and ANSWERED with it: the field is a fact
        # about which record these words are in, not a field of the record, and
        # writing it into the file would be a second place for it to go stale.
        # Read back AFTER `_update` has stamped it, so the answer carries the
        # version this write earned.
        return (lambda d: _project_chat({key: d[CHAT][key]}).get(key)), True

    return _update(mutate)


def delete_chat(session_id, if_version=None, client=None, seq=None) -> bool:
    """Drop one chat draft; True if there was one. Called on send, on an
    explicit clear, and by the two verbs that take the task away for good —
    delete and erase — because a draft for a conversation nobody can reach any
    more is a badge on a row that is gone. NOT by archive, which is the
    reversible verb: its drafts hide with the row and come back with it
    (Akshil, 2026-09-15).

    `if_version` is the caller's `If-Match`, compared inside the lock against
    the record this key READS as — the bound form's version on a session whose
    words live in one, exactly as `put_chat` compares — so the two doors agree
    about what the client is holding. Mismatch raises `VersionConflict` and
    deletes nothing; `None` deletes unconditionally.

    A DELETE CARRIES A `seq` TOO, and it is the one that matters most: the send
    deletes, and a keepalive PUT the page fired a moment earlier can arrive
    afterwards. The note this leaves behind outlives the record it removed
    (`SEQ`), which is what makes that straggler a no-op instead of a
    resurrection."""
    key = chat_key(session_id)
    if not key:
        return False
    page = _client_of(client)
    count = _seq_of(seq)

    def mutate(data: dict):
        ident = ""
        session = bound_session(key)
        if session:
            ident = bound_chats(_project_chat(data[CHAT]),
                                _project_task(data[TASK])).get(session, "")
        if not _seq_fresh(data, key, page, count):
            raise StaleWrite(key, _chat_current(data, key, ident))
        _seq_note(data, key, page, count)
        if if_version is not None:
            current = data[TASK].get(ident) if ident else data[CHAT].get(key)
            if version_of(current) != if_version:
                raise VersionConflict(key, version_of(current),
                                      _chat_current(data, key, ident))
        if key not in data[CHAT]:
            # NOTHING TO REMOVE, AND THE NOTE IS STILL WORTH WRITING: a send on
            # a chat whose first save never landed deletes nothing, and the PUT
            # that was in flight while it did is precisely the request this note
            # exists to drop.
            return False, bool(page and count is not None)
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
    # WHICH CONVERSATION THIS DRAFT IS A MESSAGE TO, when it came out of one
    # that already exists (Akshil, 2026-09-12).
    #
    # It says where the TASK is going, and it has to outlive the modal being
    # closed. Without it the hop worked only while the page still held the
    # session in memory: press Schedule straight away and the message landed in
    # the conversation, exit the modal and the draft on disk knew nothing about
    # it — so reopening that draft and scheduling it started a NEW session under
    # a NEW task number, and the task the reader had been watching was gone.
    #
    # ITS TWIN `from_chat_key` IS GONE (design-drafts-one-record.md, §1). It
    # said where the words were TYPED, and it only had to exist while a hop
    # copied a chat draft into a task draft — two records for one sentence, and
    # the provenance field was how the second one knew to delete the first. The
    # hop makes no copy now: the modal edits the chat record it was opened on.
    # A stored record that still carries the field loads fine and simply drops
    # it, which is what this projection does with every key it does not know.
    #
    # Validated as a session id and never as `new:<file>` (`bound_session`):
    # binding is to a thread, not to a folder.
    out["session_id"] = bound_session(rec.get("session_id"))
    out["created_at"] = _epoch(rec.get("created_at"))
    out["updated_at"] = _epoch(rec.get("updated_at"))
    # WHAT THE CALLER'S `If-Match` IS COMPARED AGAINST, stamped by `_update` on
    # every write and carried out to every reader (`GET /api/drafts`, the
    # changes endpoint, the 409 body). 0 on a record written before versions
    # existed, which reads as "nobody has a number for this yet".
    out["version"] = version_of(rec)
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
    both sides by construction.

    THE SETTINGS IT RECONCILES ARE `CHAT_FORM_FIELDS`, the same nine a chat
    record now carries as its `form` and the same nine `_put_bound` patches, so
    a hop that lands on this path and one that lands on that one leave the
    record in the same state (design-drafts-one-record.md, §1). `version` is not
    among them and is not merged: it belongs to the KEY this write lands on, and
    `_update` stamps it."""
    out = dict(existing)
    for field in _TASK_TEXT:
        if incoming[field].strip():
            out[field] = incoming[field]
    for field in ("when", "repeat", "custom_rule"):
        if incoming[field] not in (None, "", [], {}):
            out[field] = incoming[field]
    if incoming["new_task_each_run"] is not None:
        out["new_task_each_run"] = incoming["new_task_each_run"]
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


def put_task(ident, fields, if_version=None, client=None,
             seq=None) -> tuple[dict | None, str]:
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
    session.

    `if_version` is the caller's `If-Match`, compared inside the lock against
    the record under THIS id (the one the caller named and is holding a version
    of). Mismatch raises `VersionConflict` and writes nothing; `None` writes
    unconditionally, which is what every client that predates versions does.

    `client`/`seq` are the page's own sequence for `draft:<id>` — the listing's
    key for this record, and the same one the client's syncer counts under
    (`SEQ`). A write that is not newer than the last one that page made here
    raises `StaleWrite` and changes nothing."""
    key = draft_id(ident)
    if not key:
        return None, ""
    patch = fields if isinstance(fields, dict) else {}
    page = _client_of(client)
    count = _seq_of(seq)

    def mutate(data: dict):
        if not _seq_fresh(data, task_key(key), page, count):
            raise StaleWrite(task_key(key), _task_record(data[TASK].get(key)))
        _seq_note(data, task_key(key), page, count)
        if if_version is not None and version_of(data[TASK].get(key)) != if_version:
            raise VersionConflict(task_key(key), version_of(data[TASK].get(key)),
                                  _task_record(data[TASK].get(key)))
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
            # Read back after the stamp, so the answer carries the version this
            # write earned rather than the one it started from.
            return (lambda d: (_task_record(d[TASK].get(bound_id)), bound_id)), True
        data[TASK][key] = record
        return (lambda d: (_task_record(d[TASK].get(key)), key)), True

    return _update(mutate)


def delete_bound(session_id) -> list[str]:
    """Discard every task draft bound to one session; the LISTING KEYS of the
    ones that went (`draft:<id>`), and `[]` when there were none.

    THE VERB THE TASK'S OWN DELETE AND ERASE SPEND (Akshil, 2026-09-15;
    design.md, PR C). A session-bound draft is the NEXT MESSAGE of one
    conversation — it stands in for no row of its own, it borrows that
    conversation's number, and the only way back into it is the composer of the
    chat it is a message to. Take the task away and the draft is bytes nothing
    can show and nobody can reach, which is precisely what the chat draft beside
    it has always been treated as.

    THIS REPLACES THE UNBINDING THAT USED TO HAPPEN ON ERASE. That rule kept the
    words and cut only the binding, so the form came back as an ordinary
    `draft:<id>` row in its own folder — defensible in the abstract and wrong in
    practice, because what came back was half a message addressed to a
    conversation that no longer exists, in a lane the reader had just cleared.
    Delete and erase now do the same thing to both halves of "this session's
    unsent text", which is also the one thing they could never previously agree
    on (design.md, PR C: "Delete + erase drop BOTH").

    THE KEYS AND NOT A COUNT (bugbot, PR #1126): those rows have just left the
    listing and the caller has to announce them (`tasks_watch.notify`), or a
    page holding one goes on drawing a draft whose Schedule would send its
    message into a conversation that is gone. A count cannot be announced.

    ARCHIVE DOES NOT CALL THIS, and that is the decision this verb exists
    alongside (Akshil, 2026-09-15): filing a task away is reversible, so its
    drafts simply hide with it and come back on unarchive.

    One pass under one lock, and `[]` — no write at all — for the overwhelmingly
    common case where nothing was bound to that session."""
    target = bound_session(session_id)
    if not target:
        return []

    def mutate(data: dict):
        cut: list[str] = []
        for ident, rec in list(data[TASK].items()):
            record = _task_record(rec)
            if record is None or record["session_id"] != target:
                continue
            data[TASK].pop(ident, None)
            cut.append(task_key(ident))
        return cut, bool(cut)

    return _update(mutate)


def delete_task(ident, if_version=None, client=None, seq=None) -> bool:
    """Discard one task draft; True if there was one. Called by the modal's
    Discard button and by `POST /api/schedule` once the draft has become a real
    scheduled entry — the draft's whole purpose is over at that moment, and a
    row left behind would be the same task twice.

    `if_version` is the caller's `If-Match`: the trash on a row is a destructive
    gesture aimed at words somebody may have kept typing since the row was
    drawn, so it is allowed to be conditional. Mismatch raises
    `VersionConflict`; `None` deletes unconditionally.

    …and `client`/`seq` are the page's own sequence for `draft:<id>`, kept past
    the delete so a PUT that was already on the wire cannot put the record
    back (`SEQ`, `delete_chat`)."""
    key = draft_id(ident)
    if not key:
        return False
    page = _client_of(client)
    count = _seq_of(seq)

    def mutate(data: dict):
        if not _seq_fresh(data, task_key(key), page, count):
            raise StaleWrite(task_key(key), _task_record(data[TASK].get(key)))
        _seq_note(data, task_key(key), page, count)
        if if_version is not None and version_of(data[TASK].get(key)) != if_version:
            raise VersionConflict(task_key(key), version_of(data[TASK].get(key)),
                                  _task_record(data[TASK].get(key)))
        if key not in data[TASK]:
            return False, bool(page and count is not None)
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
