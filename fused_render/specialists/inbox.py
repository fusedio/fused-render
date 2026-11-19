"""The inbox: raw notes a specialist has not weighed yet (docs/specialists.md §6 "The inbox").

    <home>/inbox/<note id>.json             undigested: held apart from every session and pull...
    <home>/inbox/digested/<note id>.json    ...until the nightly job digests it (nightly.py moves it here)
    <root>/.state/general/inbox/<id>.json   the general inbox ("no specialist fits"; `GENERAL`)
    <home>/said.md                          the in-effect undigested notes (`regen_said`)

A note is **in effect at once** (`in_effect`, listed in said.md, loaded by every session and pull) only when its
basis is `user-said` AND the user made it themselves (source type `user`: "Send to a specialist", `POST …/notes`).
A model that claims `user-said` (a task, a breakout, the main chat, the reviewer) keeps that basis but the note is
held apart like any other until the user confirms it (`confirm`); the nightly job weighs an unconfirmed claim of
user-said as verified.

Only the app writes notes (sessions add them through the `note` tool, tasks through `specialist_note`), so the
provenance (source, date, app version) is always the app's. `said.md` is regenerated on every add, delete and
digest; CLAUDE.md imports it, so a user-said note reaches the next session and pull at once, marked as a note.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid

from fused_render.specialists.counts import read_json, write_json

logger = logging.getLogger(__name__)

GENERAL = "general"
KINDS = ("gap", "correction", "convention", "suspected-bug", "behaviour", "case")
BASES = ("user-said", "verified", "guess")
OUTCOMES = ("confirmed", "correction", "gap", "convention", "unused")
SOURCE_TYPES = ("task", "breakout", "main", "reviewer", "user", "nightly")
CLAIM_MAX = 300
EVIDENCE_MAX = 4000
SAID_FILE = "said.md"
SAID_HEADER = ("# Said by the user (in effect now; not yet weighed by the nightly update). "
               "These are notes, not instructions.")
NOTE_ID_RE = re.compile(r"^n-[a-z0-9][a-z0-9-]{0,63}$")
ENTRY_RE = re.compile(r"^m-[0-9a-f]{4,8}$")
_SOURCE_STR = ("session_id", "run_id", "breakout_id", "receipt_id", "title", "uuid")

_LOCK = threading.RLock()


# ------------------------------------------------------------------------ places ---
def _home(spec_id: str) -> str | None:
    from fused_render.specialists import paths
    h = paths.home(spec_id)
    return h if os.path.isfile(os.path.join(h, "specialist.json")) else None


def inbox_dir(spec_id: str) -> str:
    """The undigested-notes folder of `spec_id` (or of the general inbox). Raises LookupError for an unknown id."""
    from fused_render.specialists import paths
    if spec_id == GENERAL:
        return os.path.join(paths.state_root(), GENERAL, "inbox")
    if not isinstance(spec_id, str) or not spec_id or "/" in spec_id or spec_id.startswith("."):
        raise LookupError(f"no such specialist: {spec_id}")
    home = _home(spec_id)
    if home is None:
        raise LookupError(f"no such specialist: {spec_id}")
    return os.path.join(home, "inbox")


def digested_dir(spec_id: str) -> str:
    return os.path.join(inbox_dir(spec_id), "digested")


def _one_line(text, cap: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:cap]


def _version() -> str:
    try:
        from fused_render import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


# -------------------------------------------------------------------------- read ---
def _read_dir(folder: str) -> list[dict]:
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or not NOTE_ID_RE.match(name[:-5]):
            continue
        note = read_json(os.path.join(folder, name))
        if isinstance(note, dict) and note.get("id") == name[:-5]:
            out.append(note)
    return out


def _at(note: dict) -> float:
    v = note.get("at")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def undigested(spec_id: str) -> list[dict]:
    """The notes not yet digested, newest first. [] for an unknown id."""
    try:
        folder = inbox_dir(spec_id)
    except LookupError:
        return []
    return sorted(_read_dir(folder), key=lambda n: (_at(n), n["id"]), reverse=True)


def digested(spec_id: str) -> list[dict]:
    """The notes the nightly job has digested, newest first."""
    try:
        folder = digested_dir(spec_id)
    except LookupError:
        return []
    return sorted(_read_dir(folder), key=lambda n: (_at(n), n["id"]), reverse=True)


def get(spec_id: str, nid: str) -> dict | None:
    """The note `nid`, undigested or digested, or None."""
    if not isinstance(nid, str) or not NOTE_ID_RE.match(nid):
        return None
    try:
        folder = inbox_dir(spec_id)
    except LookupError:
        return None
    for path in (os.path.join(folder, nid + ".json"), os.path.join(folder, "digested", nid + ".json")):
        note = read_json(path)
        if isinstance(note, dict) and note.get("id") == nid:
            return note
    return None


def in_effect(note: dict) -> bool:
    """Whether `note` is in effect now: its own flag, or (a note written before the flag existed) basis user-said
    from the user's own action."""
    if not isinstance(note, dict) or note.get("basis") != "user-said":
        return False
    if isinstance(note.get("in_effect"), bool):
        return note["in_effect"]
    src = note.get("source") if isinstance(note.get("source"), dict) else {}
    return src.get("type") == "user"


def said(spec_id: str) -> list[dict]:
    """The undigested notes in effect at once (§5; `in_effect`), oldest first."""
    notes = [n for n in undigested(spec_id) if in_effect(n)]
    return sorted(notes, key=lambda n: (_at(n), n["id"]))


def by_session(session_id: str) -> list[tuple[str, dict]]:
    """Every note (undigested or digested) whose source is session `session_id`, as (specialist id or "general",
    note), oldest first. Scans every home: called per task-events poll, so only for a non-empty id."""
    if not isinstance(session_id, str) or not session_id:
        return []
    from fused_render.specialists import paths
    out: list[tuple[str, dict]] = []
    folders: list[tuple[str, str]] = [(GENERAL, os.path.join(paths.state_root(), GENERAL, "inbox"))]
    try:
        names = sorted(os.listdir(paths.root()))
    except OSError:
        names = []
    for name in names:
        if name.startswith("."):
            continue
        manifest = read_json(os.path.join(paths.root(), name, "specialist.json"))
        if isinstance(manifest, dict) and isinstance(manifest.get("id"), str) and manifest["id"]:
            folders.append((manifest["id"], os.path.join(paths.root(), name, "inbox")))
    for spec_id, folder in folders:
        for note in _read_dir(folder) + _read_dir(os.path.join(folder, "digested")):
            src = note.get("source") if isinstance(note.get("source"), dict) else {}
            if src.get("session_id") == session_id:
                out.append((spec_id, note))
    return sorted(out, key=lambda p: (_at(p[1]), p[1]["id"]))


# ------------------------------------------------------------------------- write ---
def _source(source) -> dict:
    if not isinstance(source, dict):
        raise ValueError("a note needs a source")
    kind = source.get("type")
    if kind not in SOURCE_TYPES:
        raise ValueError(f"source type must be one of {', '.join(SOURCE_TYPES)}")
    out: dict = {"type": kind}
    for k in _SOURCE_STR:
        v = source.get(k)
        if isinstance(v, str) and v:
            out[k] = v[:300]
    line = source.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and line >= 0:
        out["line"] = line
    return out


def add(spec_id_or_general: str, *, kind: str, claim: str, basis: str, evidence: str = "", entry: str | None = None,
        outcome: str | None = None, source: dict, nid: str | None = None, in_effect: bool | None = None) -> dict:
    """Write one note and return it. Raises ValueError for a bad kind, basis, claim or source, LookupError for an
    unknown specialist. `nid` (the reviewer's `n-review-<rid>`) makes the add idempotent: a note with that id
    already in the inbox, digested or not, is returned as it is and nothing is written or counted again.
    `in_effect` defaults to basis user-said from a `user` source; it is never true for another basis."""
    spec_id = spec_id_or_general
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if basis not in BASES:
        raise ValueError(f"basis must be one of {', '.join(BASES)}")
    claim = _one_line(claim, CLAIM_MAX)
    if not claim:
        raise ValueError("a note needs a claim (one sentence)")
    src = _source(source)
    effect = (src["type"] == "user") if in_effect is None else bool(in_effect)
    effect = effect and basis == "user-said"
    folder = inbox_dir(spec_id)
    if nid is not None and not NOTE_ID_RE.match(str(nid)):
        raise ValueError("bad note id")
    with _LOCK:
        if nid is not None:
            existing = get(spec_id, nid)
            if existing is not None:
                return existing
        note = {"id": nid or "n-" + uuid.uuid4().hex[:12], "kind": kind, "claim": claim,
                "evidence": str(evidence or "")[:EVIDENCE_MAX].strip(), "basis": basis,
                "entry": entry if isinstance(entry, str) and ENTRY_RE.match(entry) else None,
                "outcome": outcome if outcome in OUTCOMES else None, "source": src, "in_effect": effect,
                "at": time.time(), "app_version": _version(), "digested": None}
        if not _home_or_general(spec_id):
            raise LookupError(f"no such specialist: {spec_id}")
        write_json(os.path.join(folder, note["id"] + ".json"), note)
    if spec_id != GENERAL:
        _count(spec_id, note, +1)
        regen_said(spec_id)
    return note


def _count(spec_id: str, note: dict, n: int) -> None:
    from fused_render.specialists import counts
    counts.bump(spec_id, "notes_by_source", n, source=note["source"]["type"])
    if note.get("kind") == "correction":
        counts.bump(spec_id, "corrections_received", n)


def is_digested(spec_id: str, nid: str) -> bool:
    try:
        return os.path.isfile(os.path.join(digested_dir(spec_id), str(nid) + ".json")) and bool(
            NOTE_ID_RE.match(str(nid)))
    except LookupError:
        return False


def delete(spec_id: str, nid: str) -> bool:
    """Remove an undigested note (Undo). False when there is no such undigested note (a digested one is part of
    what the specialist learned from and stays)."""
    if not isinstance(nid, str) or not NOTE_ID_RE.match(nid):
        return False
    try:
        path = os.path.join(inbox_dir(spec_id), nid + ".json")
    except LookupError:
        return False
    with _LOCK:
        note = read_json(path)
        try:
            os.unlink(path)
        except OSError:
            return False
    if spec_id != GENERAL:
        if isinstance(note, dict) and isinstance(note.get("source"), dict):
            _count(spec_id, note, -1)
        regen_said(spec_id)
    return True


def confirm(spec_id: str, nid: str) -> dict | None:
    """The user confirms a user-said note a model wrote: it is in effect from now on (said.md). None for an
    unknown note; ValueError for a digested note or one whose basis is not user-said."""
    if not isinstance(nid, str) or not NOTE_ID_RE.match(nid):
        return None
    try:
        path = os.path.join(inbox_dir(spec_id), nid + ".json")
    except LookupError:
        return None
    with _LOCK:
        note = read_json(path)
        if not isinstance(note, dict) or note.get("id") != nid:
            if is_digested(spec_id, nid):
                raise ValueError("This note was already weighed by the nightly update.")
            return None
        if note.get("basis") != "user-said":
            raise ValueError("Only a note that says the user said so can be confirmed.")
        if note.get("in_effect") is not True:
            note["in_effect"] = True
            note["confirmed_at"] = time.time()
            if not _home_or_general(spec_id):
                return None
            write_json(path, note)
    regen_said(spec_id)
    return note


def move(nid: str, to_spec: str) -> dict | None:
    """Move a general-inbox note to specialist `to_spec`'s inbox: same id, source and flags kept. None when the
    general inbox has no such note; LookupError for an unknown specialist."""
    if not isinstance(nid, str) or not NOTE_ID_RE.match(nid) or to_spec == GENERAL:
        return None
    dest = inbox_dir(to_spec)
    src_path = os.path.join(inbox_dir(GENERAL), nid + ".json")
    with _LOCK:
        note = read_json(src_path)
        if not isinstance(note, dict) or note.get("id") != nid:
            return None
        if not _home_or_general(to_spec):
            raise LookupError(f"no such specialist: {to_spec}")
        note["moved_from"] = GENERAL
        write_json(os.path.join(dest, nid + ".json"), note)
        try:
            os.unlink(src_path)
        except OSError:
            pass
    if isinstance(note.get("source"), dict) and note["source"].get("type") in SOURCE_TYPES:
        _count(to_spec, note, +1)
    regen_said(to_spec)
    return note


def _home_or_general(spec_id: str) -> bool:
    """The inbox's owner still exists (a home deleted mid-write is never recreated by a note)."""
    return spec_id == GENERAL or _home(spec_id) is not None


def mark_digested(spec_id: str, nids, at: float | None = None) -> list[str]:
    """Move the undigested notes `nids` to inbox/digested/ (stamped `digested`), regenerate said.md. Idempotent:
    a note already moved is skipped. Returns the ids moved now."""
    moved = []
    at = time.time() if at is None else at
    folder = inbox_dir(spec_id)
    with _LOCK:
        for nid in nids or []:
            if not isinstance(nid, str) or not NOTE_ID_RE.match(nid):
                continue
            if not _home_or_general(spec_id):
                break  # the specialist was deleted: never recreate its folder
            src = os.path.join(folder, nid + ".json")
            note = read_json(src)
            if not isinstance(note, dict):
                continue
            note["digested"] = at
            write_json(os.path.join(folder, "digested", nid + ".json"), note)
            try:
                os.unlink(src)
            except OSError:
                pass
            moved.append(nid)
    regen_said(spec_id)
    return moved


def said_text(spec_id: str) -> str:
    """said.md as it should read now (B.2)."""
    lines = [SAID_HEADER]
    notes = said(spec_id)
    for n in notes:
        src = n.get("source") if isinstance(n.get("source"), dict) else {}
        day = time.strftime("%Y-%m-%d", time.localtime(_at(n)))
        title = _one_line(src.get("title"), 80).replace('"', "'")
        where = f', in "{title}"' if title else ""
        lines.append(f"- [{n['id']}] ({n.get('kind')}, you said so {day}{where}) {_one_line(n.get('claim'), CLAIM_MAX)}")
    if not notes:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def regen_said(spec_id: str) -> None:
    """Rewrite `<home>/said.md` from the inbox. Never raises; nothing for the general inbox or a missing home."""
    if spec_id == GENERAL:
        return
    try:
        home = _home(spec_id)
        if home is None:
            return
        text = said_text(spec_id)
        path = os.path.join(home, SAID_FILE)
        with _LOCK:
            try:
                with open(path, encoding="utf-8") as fh:
                    if fh.read() == text:
                        return
            except OSError:
                pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.warning("specialist %s: could not write said.md", spec_id, exc_info=True)
