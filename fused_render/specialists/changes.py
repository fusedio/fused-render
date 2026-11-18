"""What each nightly update changed, and Revert (PRODUCT §6; PLAN A.2 "Change list", B.6; CONTRACT C4).

    <home>/.specialist/changes/<run>.json
    {"run","at","summary","status":"done|interrupted|error","commit","row","fallback":bool,"drafts":[ids],
     "items":[{"id":"i1","kind","text","entry","file","from":[...],"undo","commit",
               "before"?, "after"?,    memory entry line before / after the run (None: absent), from the run's diff
               "remove"?,              a file or skill folder the run created (Revert deletes it)
               "diff"?}]}              the run's diff of `file` (a non-entry change), capped
    <home>/.specialist/reverts.json   {"<run>/<item>": {"session_id","at","state":"requested"|"done"}}

The change list is applied already when it is written: there is no approval step. It is reconciled with the run's
diff when it is saved (`build_items`): every memory entry the run changed has an item (one the job wrote, or a
`Changed` item the app adds), and each entry item carries the exact line before and after.

**Revert** on an item is a message the page sends into the specialist's main chat (`revert` returns it, prefix
`REVERT_PREFIX`) asking it to undo THAT change only: for a memory entry it gives the exact line to put back, for a
new skill or knowledge file the path to delete. The item reads `reverting` until the main-chat turn that carried the
message ends (`after_turn`, the Stop hook) and `reverted` after. Every item is compared with the home whenever the
list is read (`_with_state`): an entry whose line no longer reads what the run wrote, or a created file that is
gone, is `changed` ("Changed since"). Revert is refused on `reverted` and `changed` items. The notes the change came
from stay digested, so it is not learned again tomorrow: the next nightly reads the main chat where it was reverted.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from fused_render.specialists.counts import read_json, write_json

logger = logging.getLogger(__name__)

KINDS = ("Learned", "Corrected", "Retired", "Moved", "New skill", "New check", "People", "Trigger")
FALLBACK_KIND = "Changed"
ITEMS_MAX = 50
TEXT_MAX = 500
SENTENCE_MAX = 160
DIFF_MAX = 3000
RUN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
ITEM_RE = re.compile(r"^i[0-9]{1,3}$")
ENTRY_LINE_RE = re.compile(r"^- \[(m-[0-9a-f]{4,8})\] ")
ENTRY_ID_RE = re.compile(r"\[?\bm-[0-9a-f]{4,8}\b\]?")
MEMORY = "memory.md"
SKILLS = ".claude/skills/"

# C3: the frontend recognises the revert request by this prefix; tests pin it.
REVERT_PREFIX = "Undo this change from the latest update: "
REVERT_TAIL = ("Change only this. Leave every other line and every other file as it is, never restore whole files "
               "from git and do not run git at all (the app saves your change). When you answer, say in one plain "
               "sentence what you changed; do not mention ids or commits.")


class Gone(ValueError):
    """Revert asked for an item that is already reverted or was changed since (the route answers 409)."""


_LOCK = threading.RLock()


def _app(spec_id: str) -> str:
    from fused_render.specialists import paths
    return paths.app_dir(spec_id)


def path(spec_id: str, run: str) -> str:
    return os.path.join(_app(spec_id), "changes", run + ".json")


def _reverts_path(spec_id: str) -> str:
    return os.path.join(_app(spec_id), "reverts.json")


# ------------------------------------------------------------------ validation ---
def _s(v, cap: int) -> str:
    return str(v).strip()[:cap] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def sentence(text, cap: int = SENTENCE_MAX) -> str:
    """`text` on one line, at most `cap` characters: a longer one is cut at a word and ends with an ellipsis."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(t) <= cap:
        return t
    cut = t[:cap - 1]
    if " " in cut[cap // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def _note_id(v) -> str:
    v = _s(v, 80)
    return v if re.match(r"^n-[a-z0-9-]{1,64}$", v) else ""


def validate(raw) -> dict | None:
    """The job's changes.json (B.6, C4) cleaned: unknown keys dropped, item text one short sentence, at most
    `ITEMS_MAX` items, an item without text dropped, an unknown kind read as `Changed`. `ignored` is the notes the
    job weighed and deliberately left out (`[{"note","why"}]`). `digested` is computed: the note ids in some item's
    `from` or in `ignored` (a note in neither stays in the inbox). None when it is not a JSON object with an `items`
    list (the caller then falls back to the files the run changed)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return None
    items = []
    for it in raw["items"]:
        if not isinstance(it, dict) or not _s(it.get("text"), TEXT_MAX):
            continue
        kind = _s(it.get("kind"), 40)
        entry = _s(it.get("entry"), 20)
        frm = it.get("from") if isinstance(it.get("from"), list) else []
        items.append({"kind": kind if kind in KINDS else FALLBACK_KIND, "text": sentence(_s(it.get("text"), TEXT_MAX)),
                      "entry": entry if re.match(r"^m-[0-9a-f]{4,8}$", entry) else None,
                      "file": _s(it.get("file"), 300) or None,
                      "from": [_s(x, 60) for x in frm if _s(x, 60)][:20],
                      "undo": _s(it.get("undo"), TEXT_MAX)})
        if len(items) >= ITEMS_MAX:
            break
    ignored = []
    for g in raw.get("ignored") if isinstance(raw.get("ignored"), list) else []:
        nid = _note_id(g.get("note")) if isinstance(g, dict) else _note_id(g)
        if nid and nid not in {x["note"] for x in ignored}:
            ignored.append({"note": nid, "why": sentence(_s(g.get("why"), TEXT_MAX)) if isinstance(g, dict) else ""})
    drafts = []
    for d in raw.get("drafts") if isinstance(raw.get("drafts"), list) else []:
        if not isinstance(d, dict) or not _s(d.get("text"), 8000):
            continue
        to = d.get("to") if isinstance(d.get("to"), dict) else {}
        drafts.append({"to": {k: _s(to.get(k), 200) for k in ("name", "service", "handle")},
                       "subject": _s(d.get("subject"), 300), "text": _s(d.get("text"), 8000),
                       "why": _s(d.get("why"), TEXT_MAX)})
    closed = raw.get("closed_reports") if isinstance(raw.get("closed_reports"), list) else []
    rb = raw.get("repeat_breakouts")
    return {"summary": _s(raw.get("summary"), TEXT_MAX), "items": items, "drafts": drafts[:10],
            "ignored": ignored[:200], "digested": digested_of(items, ignored),
            "closed_reports": list(dict.fromkeys(x for x in closed if isinstance(x, str)
                                                 and re.match(r"^d-[0-9a-f]{12}$", x)))[:50],
            "repeat_breakouts": rb if isinstance(rb, int) and not isinstance(rb, bool) and 0 <= rb <= 100 else 0}


def digested_of(items: list, ignored: list) -> list[str]:
    """C4: the notes a run accounted for: named in an item's `from`, or in `ignored`."""
    out = [_note_id(x) for it in items or [] for x in it.get("from") or []]
    out += [g.get("note") for g in ignored or [] if isinstance(g, dict)]
    return list(dict.fromkeys(x for x in out if x))


def merge(first: dict | None, second: dict | None) -> dict | None:
    """The first pass's validated list with a later pass's (the compaction) added to it: both item lists (an
    item the second repeats is kept once), both drafts, the union of `ignored`, `digested` and `closed_reports`. The
    first pass's summary and repeat count win; either alone is returned as it is."""
    if first is None or second is None:
        return first if second is None else second

    def key(it):
        return (it.get("kind"), it.get("text"), it.get("entry"), it.get("file"))

    seen = {key(it) for it in first["items"]}
    items = first["items"] + [it for it in second["items"] if key(it) not in seen]
    dseen = {(d["subject"], d["text"]) for d in first["drafts"]}
    gseen = {g["note"] for g in first.get("ignored", [])}
    ignored = first.get("ignored", []) + [g for g in second.get("ignored", []) if g["note"] not in gseen]
    return {"summary": first["summary"] or second["summary"], "items": items[:ITEMS_MAX],
            "drafts": (first["drafts"] + [d for d in second["drafts"] if (d["subject"], d["text"]) not in dseen])[:10],
            "ignored": ignored,
            "digested": list(dict.fromkeys(first["digested"] + second["digested"])),
            "closed_reports": list(dict.fromkeys(first.get("closed_reports", []) + second.get("closed_reports", []))),
            "repeat_breakouts": first["repeat_breakouts"] or second["repeat_breakouts"]}


def fallback_items(files, before: str = "") -> list[dict]:
    """One `Changed` item per changed tracked file (the job left no valid change list, or left files out)."""
    return [{"kind": FALLBACK_KIND, "text": sentence(f"Changed the file {f}."), "entry": None, "file": f, "from": [],
             "undo": f"Put back what this update changed in {f}."} for f in files][:ITEMS_MAX]


# ----------------------------------------------------------- reconcile with diff ---
def entries(text: str) -> dict[str, str]:
    """memory.md's entry lines by id (`- [m-xxxx] …`); the first line wins for a repeated id."""
    out: dict[str, str] = {}
    for ln in str(text or "").splitlines():
        m = ENTRY_LINE_RE.match(ln)
        if m and m.group(1) not in out:
            out[m.group(1)] = ln.rstrip()
    return out


def entry_text(line: str | None) -> str:
    """The words of an entry line: no `- [m-id] ` head, no `{…}` provenance."""
    t = ENTRY_LINE_RE.sub("", str(line or ""), count=1)
    return re.sub(r"\s*\{[^{}]*\}\s*$", "", t).strip()


def _plain(text: str, before: str | None, after: str | None) -> str:
    """Item text with no entry id in it (R1): an id is replaced by what the entry says."""
    if not ENTRY_ID_RE.search(text or ""):
        return text
    words = entry_text(after) or entry_text(before)
    rest = sentence(re.sub(r"\s+", " ", ENTRY_ID_RE.sub("", text)).strip(" :,-"))
    if words:
        return sentence(f"{rest}: {words}" if rest else words)
    return rest or text


def reconcile(items: list, before_text: str, after_text: str) -> list[dict]:
    """C4: the change list made to match memory.md's diff. Each item naming an entry gets that entry's exact line
    `before` and `after` the run (None: absent); every entry the run added, changed or removed that no item names
    gets a `Changed` item of its own. Items keep their order; added items follow, in memory.md's order."""
    b, a = entries(before_text), entries(after_text)
    out = []
    for it in items or []:
        it = dict(it)
        e = it.get("entry")
        if e and (e in b or e in a):
            it["before"], it["after"] = b.get(e), a.get(e)
            it["text"] = _plain(str(it.get("text") or ""), it["before"], it["after"])
        out.append(it)
    covered = {it.get("entry") for it in out if it.get("entry")}
    changed = [e for e in list(a) + [e for e in b if e not in a] if b.get(e) != a.get(e) and e not in covered]
    for e in changed:
        old, new = b.get(e), a.get(e)
        if old is None:
            text = f"Added: {entry_text(new)}"
        elif new is None:
            text = f"Removed: {entry_text(old)}"
        elif entry_text(old) == entry_text(new):
            text = f"Updated how often this was seen or confirmed: {entry_text(new)}"
        else:
            text = f"Changed: {entry_text(new)}"
        out.append({"kind": FALLBACK_KIND, "text": sentence(text), "entry": e, "file": MEMORY, "from": [],
                    "undo": "", "before": old, "after": new})
    return out[:ITEMS_MAX]


def _skill_dir(p: str) -> str | None:
    if not p.startswith(SKILLS):
        return None
    name = p[len(SKILLS):].split("/", 1)[0]
    return f"{SKILLS}{name}/" if name else None


def _git_out(spec_id: str, *args: str) -> str | None:
    try:
        from fused_render.specialists import history, paths
        if not history.available():
            return None
        res = history._git(paths.home(spec_id), *args)
        return res.stdout if res.returncode == 0 else None
    except Exception:  # noqa: BLE001
        logger.debug("changes: git %s failed", args[:1], exc_info=True)
        return None


def file_at(spec_id: str, sha: str, rel: str) -> str:
    """`rel` as commit `sha` has it ("" when absent)."""
    return _git_out(spec_id, "show", f"{sha}:{rel}") or "" if sha else ""


def name_status(spec_id: str, sha: str) -> dict[str, str]:
    """{path: A|M|D|…} of commit `sha`."""
    out = {}
    for ln in (_git_out(spec_id, "show", "--name-status", "--format=", "--no-renames", sha) or "").splitlines():
        st, _, p = ln.partition("\t")
        if p.strip():
            out[p.strip()] = st.strip()[:1]
    return out


def file_diff(spec_id: str, sha: str, rel: str) -> str:
    text = _git_out(spec_id, "show", "--format=", "--unified=1", "--no-color", sha, "--", rel) or ""
    body = [ln for ln in text.splitlines() if not ln.startswith(("diff --git", "index ", "--- ", "+++ "))]
    out = "\n".join(body).strip()
    return out if len(out) <= DIFF_MAX else out[:DIFF_MAX] + "\n…(cut)"


def _rel(spec_id: str, f) -> str:
    """An item's `file` relative to the home ("" when none)."""
    f = str(f or "").strip()
    if not f:
        return ""
    try:
        from fused_render.specialists import paths
        home = paths.home(spec_id).rstrip("/") + "/"
        if f.startswith(home):
            f = f[len(home):]
    except Exception:  # noqa: BLE001
        pass
    while f.startswith("./"):
        f = f[2:]
    return f


def build_items(spec_id: str, items: list, sha: str) -> list[dict]:
    """The change list of the run committed as `sha`, reconciled with that commit's diff (C4): memory entries
    (`reconcile`), a file or skill folder the run created (`remove`), the diff of any other file an item names
    (`diff`), and one `Changed` item per changed file no item covers."""
    items = list(items or [])
    if not sha:
        return items[:ITEMS_MAX]
    status = name_status(spec_id, sha)
    if MEMORY in status:
        items = reconcile(items, file_at(spec_id, f"{sha}^", MEMORY), file_at(spec_id, sha, MEMORY))
    added = {p for p, st in status.items() if st == "A"}
    covered = set()
    for it in items:
        f = _rel(spec_id, it.get("file"))
        if it.get("entry") and ("before" in it or "after" in it):
            covered.add(MEMORY)
            continue
        if not f or f == MEMORY:
            continue  # a memory item with no entry in the diff: never the whole file's diff
        hits = [p for p in status if p == f or p.startswith(f.rstrip("/") + "/")]
        covered.update(hits)
        skill = _skill_dir(f)
        if skill and skill + "SKILL.md" in added:
            it["remove"] = skill
            covered.update(p for p in status if p.startswith(skill))
        elif hits and all(p in added for p in hits):
            it["remove"] = f.rstrip("/") + "/" if len(hits) > 1 or hits[0] != f else f
        elif len(hits) == 1:
            it["diff"] = file_diff(spec_id, sha, hits[0])
    rest, skills = [], set()
    for p in status:
        if p in covered:
            continue
        skill = _skill_dir(p)
        if skill and skill + "SKILL.md" in added:
            if skill not in skills:
                skills.add(skill)
                rest.append(skill)
            continue
        rest.append(p)
    for fb in fallback_items(rest):
        if fb["file"] in skills:
            name = fb["file"].rstrip("/").rsplit("/", 1)[-1]
            fb.update(remove=fb["file"], text=sentence(f"Added the skill {name}."))
            items.append(fb)
            continue
        if fb["file"] in added:
            fb.update(remove=fb["file"], text=sentence(f"Added the file {fb['file']}."))
        else:
            fb["diff"] = file_diff(spec_id, sha, fb["file"])
        items.append(fb)
    return items[:ITEMS_MAX]


# ---------------------------------------------------------------------- records ---
def _home_ok(spec_id: str) -> bool:
    from fused_render.specialists import paths
    return os.path.isfile(os.path.join(paths.home(spec_id), "specialist.json"))


def save(spec_id: str, run: str, *, summary: str, items: list, commit: str | None, row: str, status: str,
         fallback: bool, drafts: list) -> dict | None:
    """Write the change list of `run`; None when the specialist was deleted meanwhile (nothing is recreated)."""
    if not _home_ok(spec_id):
        return None
    rec = {"run": run, "at": time.time(), "summary": summary, "status": status, "commit": commit or "",
           "row": row, "fallback": fallback, "drafts": list(drafts),
           "items": [{**it, "id": f"i{n}", "commit": commit or ""} for n, it in enumerate(items, 1)]}
    write_json(path(spec_id, run), rec)
    return rec


def get(spec_id: str, run: str) -> dict | None:
    if not isinstance(run, str) or not RUN_RE.match(run):
        return None
    rec = read_json(path(spec_id, run))
    return rec if isinstance(rec, dict) and rec.get("run") == run else None


def _reverts(spec_id: str) -> dict:
    r = read_json(_reverts_path(spec_id), {})
    return r if isinstance(r, dict) else {}


def _home_now(spec_id: str) -> tuple[str, dict[str, str]]:
    """(home, memory.md's entry lines now)."""
    from fused_render.specialists import paths
    home = paths.home(spec_id)
    try:
        with open(os.path.join(home, MEMORY), encoding="utf-8", errors="replace") as fh:
            return home, entries(fh.read())
    except OSError:
        return home, {}


def item_state(it: dict, revert: dict | None, home: str, now: dict[str, str]) -> str:
    """`reverting` / `reverted` (a Revert was asked / its turn ended), `changed` (the home no longer holds what
    the run wrote: the entry line differs, or a file it created is gone), else `active`."""
    if isinstance(revert, dict):
        return "reverted" if revert.get("state") == "done" else "reverting"
    if it.get("entry") and ("before" in it or "after" in it):
        if now.get(str(it["entry"])) != it.get("after"):
            return "changed"
    elif it.get("remove") and not os.path.exists(os.path.join(home, str(it["remove"]))):
        return "changed"
    return "active"


def _with_state(spec_id: str, rec: dict, reverts: dict | None = None, now: tuple | None = None) -> dict:
    reverts = _reverts(spec_id) if reverts is None else reverts
    home, current = _home_now(spec_id) if now is None else now
    items = [{**it, "state": item_state(it, reverts.get(f"{rec['run']}/{it.get('id')}"), home, current)}
             for it in rec.get("items") or []]
    return {**rec, "items": items}


def runs(spec_id: str, limit: int = 5) -> list[dict]:
    """The latest change lists with each item's state, newest first."""
    try:
        names = sorted((n[:-5] for n in os.listdir(os.path.join(_app(spec_id), "changes"))
                        if n.endswith(".json") and RUN_RE.match(n[:-5])), reverse=True)
    except OSError:
        return []
    reverts = _reverts(spec_id)
    now = _home_now(spec_id)
    out = []
    for run in names:
        rec = get(spec_id, run)
        if rec is not None:
            out.append(_with_state(spec_id, rec, reverts, now))
        if len(out) >= max(1, int(limit)):
            break
    return out


# ------------------------------------------------------------------------ revert ---
def _current(spec_id: str) -> str:
    try:
        from fused_render.specialists import chat
        return str((chat.state(spec_id) or {}).get("current") or "")
    except Exception:  # noqa: BLE001
        return ""


def _instruction(item: dict) -> str:
    """What exactly to undo, from the run's diff (C4)."""
    before, after = item.get("before"), item.get("after")
    if item.get("entry") and ("before" in item or "after" in item) and (before or after):
        if before and after:
            return f"In memory.md, replace this exact line:\n{after}\nwith this exact line:\n{before}"
        if after:
            return f"In memory.md, delete this exact line:\n{after}"
        moved = str(item.get("file") or "")
        extra = (f"\nThis update moved it into {moved}; take it out of there too." if moved and moved != MEMORY
                 else "")
        return f"In memory.md, add this exact line back among the entries:\n{before}{extra}"
    if item.get("remove"):
        rm = str(item["remove"])
        if rm.startswith(SKILLS) and rm.endswith("/"):
            return f"Delete the folder {rm} (this update created that skill)."
        return f"Delete {rm} (this update created it)."
    if item.get("diff") and item.get("file"):
        return (f"In {item['file']}, this update made the change below (lines starting with - were removed, + were "
                f"added). Put back only the part that is this change:\n{item['diff']}")
    how = sentence(item.get("undo") or "", 300)
    where = f" in {item['file']}" if item.get("file") else ""
    return f"Undo only this change{where}" + (f": {how}" if how else ", as it was before this update.")


def message(rec: dict, item: dict) -> str:
    """The REVERT request (C3 prefix, C4). Its first line names the change; the app finds it by that line."""
    text = sentence(str(item.get("text") or item.get("kind") or "this change")).replace('"', "'")
    return f'{REVERT_PREFIX}"{text}"\n\n{_instruction(item)}\n\n{REVERT_TAIL}'


def _marker(rec: dict, item: dict) -> str:
    return message(rec, item).split("\n", 1)[0]


def revert(spec_id: str, run: str, item_id: str) -> dict:
    """Request a Revert: `{"message","session_id"}` (the main chat's current session, or None when it has none
    yet). Raises LookupError for an unknown run or item, `Gone` for one already reverted or changed since."""
    rec = get(spec_id, run)
    item = next((it for it in (rec or {}).get("items") or [] if it.get("id") == item_id), None)
    if rec is None or item is None or not ITEM_RE.match(str(item_id)):
        raise LookupError("no such change")
    sid = _current(spec_id)
    with _LOCK:
        reverts = _reverts(spec_id)
        home, now = _home_now(spec_id)
        state = item_state(item, reverts.get(f"{run}/{item_id}"), home, now)
        if state == "reverted":
            raise Gone("This change was already undone.")
        if state == "changed":
            raise Gone("This has changed since that update, so it can no longer be undone from here.")
        if _home_ok(spec_id):
            reverts[f"{run}/{item_id}"] = {"session_id": sid, "at": time.time(), "state": "requested"}
            write_json(_reverts_path(spec_id), reverts)
    return {"message": message(rec, item), "session_id": sid or None}


def _turns(spec_id: str, session_id: str) -> list:
    from fused_render.claude_agent import agent_module
    from fused_render.specialists import paths
    hist = agent_module()._history(paths.home(spec_id), session_id)
    return list(hist.get("turns") or []) if isinstance(hist, dict) else []


def _answered(turns: list, marker: str) -> bool:
    """A user turn carrying `marker` with an assistant turn after it."""
    seen = False
    for t in turns or []:
        if not isinstance(t, dict):
            continue
        if t.get("role") == "user" and marker in str(t.get("text") or ""):
            seen = True
        elif seen and t.get("role") == "assistant" and str(t.get("text") or "").strip():
            return True
    return False


def after_turn(spec_id, session_id) -> None:
    """A main-chat turn ended (Stop hook): every Revert requested for this session whose message the
    conversation now holds, answered, reads `reverted`. Never raises."""
    try:
        sid = str(session_id or "")
        if not sid:
            return
        reverts = _reverts(spec_id)
        mine = {k: v for k, v in reverts.items() if isinstance(v, dict) and v.get("state") == "requested"
                and (v.get("session_id") == sid or (not v.get("session_id") and sid == _current(spec_id)))}
        if not mine:
            return
        turns = _turns(spec_id, sid)
        done = []
        for key in mine:
            run, _, item_id = key.partition("/")
            rec = get(spec_id, run)
            item = next((it for it in (rec or {}).get("items") or [] if it.get("id") == item_id), None)
            if rec is not None and item is not None and _answered(turns, _marker(rec, item)):
                done.append(key)
        if not done:
            return
        with _LOCK:
            if not _home_ok(spec_id):
                return
            reverts = _reverts(spec_id)
            for key in done:
                if isinstance(reverts.get(key), dict):
                    reverts[key] = {**reverts[key], "state": "done", "session_id": sid, "done_at": time.time()}
            write_json(_reverts_path(spec_id), reverts)
    except Exception:  # noqa: BLE001
        logger.warning("specialist %s: could not settle reverts", spec_id, exc_info=True)
