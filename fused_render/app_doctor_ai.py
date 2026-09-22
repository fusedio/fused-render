"""The App Doctor's one MODEL-BACKED row: `cross-browser`, answered by an
App Doctor TASK (a Claude session on the app's entry page, the same door the
fix tasks go through) rather than by a file read or a regex.

Every other row of the checklist (`app_doctor.py`) is deterministic and runs
on every GET — the header dot fetches the report each time an app opens.
This row cannot: it spends the user's tokens and takes a while, so it is

* ON DEMAND — nothing here runs unless a person presses the row's own Check
  button (`POST /api/apps/doctor/run`, routers/apps.py). `report()` and
  `report_one()` only ever READ the cache below; they never create a task.
* A TASK, NOT A BLOCKING CALL. The first build ran this as a one-shot
  `claude -p` inside the HTTP request: the page showed nothing while it ran,
  and leaving the tab lost the "checking…" state, since it lived only in the
  component (owner, 2026-09-21: "no notification while it's working … very
  unstable wrt to UI"). Now the button CREATES A TASK (`_create_app_task`,
  the fix task's own seam) and returns at once. The task is a row on the
  app's Tasks tab, its finish raises the shell's ordinary "Task finished"
  notice and the sidebar's unread dot, and every open of the doctor reads
  "checking" off the STORE, not off component state — so a reload, a tab
  switch or a second window all draw the same thing.
* CACHED ON THE TASK AND THE APP'S CONTENT — two files under
  `.fused/cache/app-doctor/` (SPEC §47: `cache/` is for bytes that can be
  rebuilt, which a verdict is — press Check again):

    cross-browser.json          the RUN record, written by the SERVER when
                                the task is created: `{checksum, task_id,
                                started_at, model, effort, files}`
    cross-browser.verdict.json  the VERDICT `{ok, summary, findings}` — also
                                server-written, lifted off the finished
                                task's transcript (`settle_from_task`): the
                                task runs in PLAN mode and cannot write

  The row is the join of the two. Run record whose `checksum` no longer
  matches the folder → UNRUN, "the app changed" — the cache invalidates
  itself on content, never on time. Run record with no verdict yet → the
  task is still working (the router confirms against the schedule store and
  attaches it as `check_task`), or it ended without writing one, which reads
  as UNRUN with that reason. Both files present and matching → PASS/FAIL.
  The checksum is the server's alone: the session never sees it and cannot
  stamp a verdict onto content it did not read.
* FIXED AT SONNET + LOW EFFORT — the question is bounded (a rubric plus a
  handful of view files) and the rubric does the heavy lifting. The FIX
  session that follows a failing verdict is an ordinary App Doctor fix task
  and takes the user's own model and effort pickers, like every other row.

THE RUBRIC IS THE SKILL. The task's prompt (`app_doctor.check_prompt`)
embeds `fused-render-cross-browser`'s SKILL.md, read from disk (`rubric()`),
so the check judges by the same table the fix session later fixes by, and
there is one copy of what "compatible" means. Embedded rather than invoked:
plan mode gates the Skill tool too, and the first live run spent its turn
finding that out. The session answers with a fenced JSON block in the shape
`_parse_verdict` reads; a reply that does not read is "no verdict", never a
crash.

The verdict is a `kind="candidate"` row (`app_doctor._CHECK_META`): a
model's reading of a rubric is not a fact (a file exists or it does not) and
its findings are worth a second look before an edit, which is exactly what
the candidate prompt (`_triage_ask`) asks the fix session to do.

NOTHING HERE RAISES INTO A REPORT. A cache that will not read is "not run
yet"; a checksum that cannot be computed reads as UNRUN with the reason.
"""
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from fused_render import app_fused_dir

logger = logging.getLogger(__name__)

CHECK_ID = "cross-browser"
LABEL = "Renders and works alike in Chrome, Firefox and Safari"

# The skill the CHECK task invokes as its rubric — and the one the fix
# session is routed to by the app-doctor skill's `cross-browser` section.
RUBRIC_SKILL = "fused-render-cross-browser"

# The model and effort the CHECK task runs at. Fixed on purpose — see the
# module docstring. Both are the CLI's own aliases (`VALID_DEFAULT_MODELS`,
# `_VALID_SESSION_EFFORTS`).
MODEL = "sonnet"
EFFORT = "low"

# What the session is pointed at: the view files a browser actually parses.
# `.py` never reaches a browser, images are bytes, `.md` is prose.
_VIEW_SUFFIXES = (".html", ".htm", ".css", ".js", ".mjs", ".svg")

_RUN_REL = os.path.join("app-doctor", f"{CHECK_ID}.json")
_VERDICT_REL = os.path.join("app-doctor", f"{CHECK_ID}.verdict.json")


# ----------------------------------------------------------------- the files


def _view_files(app_dir: str) -> list[tuple[str, str]]:
    """`[(abs, rel)]` of the view files a stranger would receive — the same
    gitignore-aware walk the `.fused` exporter uses (`appfile._iter_app_files`:
    hidden names and `.fused/` dropped, ignored build output left home), kept
    to `_VIEW_SUFFIXES` and sorted by relative path so the checksum is stable
    across platforms and runs."""
    from fused_render import appfile

    out = []
    for full, rel in appfile._iter_app_files(app_dir):
        if rel.lower().endswith(_VIEW_SUFFIXES):
            out.append((full, rel))
    out.sort(key=lambda t: t[1])
    return out


def gather(app_dir: str) -> dict:
    """What one run is judged on: `{"files": [rel...], "checksum": sha256-hex}`.

    The checksum covers every view file's rel path and full bytes, so a cache
    hit means "the same view files" — the only thing that makes reusing the
    verdict honest. Raises OSError only for a folder that cannot be walked at
    all; a single file that vanished mid-walk is skipped."""
    files = []
    h = hashlib.sha256()
    for full, rel in _view_files(app_dir):
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        files.append(rel)
        h.update(rel.encode("utf-8") + b"\0")
        h.update(raw + b"\0")
    return {"files": files, "checksum": h.hexdigest()}


# ----------------------------------------------------------------- the cache


def run_path(app_dir: str) -> str:
    return os.path.join(app_fused_dir.cache_dir(app_dir), _RUN_REL)


def verdict_path(app_dir: str) -> str:
    return os.path.join(app_fused_dir.cache_dir(app_dir), _VERDICT_REL)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_run(app_dir: str) -> dict | None:
    """The stored run record, or None — absent, unreadable, malformed or from
    an unfamiliar writer all read the same: nothing to reuse."""
    data = _read_json(run_path(app_dir))
    if data is None or not isinstance(data.get("checksum"), str):
        return None
    # `task_id` is what tells this record apart from the previous build's
    # one-file cache at the SAME path (checksum + verdict inline, no task).
    # That older file's checksum can still match today's folder, and reading
    # it as a run record would draw a saved verdict as "ended without a
    # verdict". It reads as "not checked yet" instead — one Check rebuilds it.
    if not isinstance(data.get("task_id"), str):
        return None
    return data


def read_verdict(app_dir: str) -> dict | None:
    """The session's verdict, normalised (`_parse_verdict`), or None when
    there is none yet or what is there does not read as one."""
    data = _read_json(verdict_path(app_dir))
    if data is None:
        return None
    return _parse_verdict(data)


def _write_json(path: str, data: dict) -> bool:
    """Temp file + `os.replace` so a half-written file never reads as a
    record. False when the folder refuses (see `write_run`)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.debug("could not write %s", path, exc_info=True)
        return False


def write_run(app_dir: str, record: dict) -> bool:
    """Best-effort: a read-only `.fused` clone (`appfile._make_read_only`) or
    a mount-backed folder `app_fused_dir.ensure` refuses just means the
    check cannot be cached here — and since the session would not be able to
    write its verdict either, the router refuses to create the task."""
    if not app_fused_dir.ensure(app_dir):
        # A mount-backed or unwritable folder: `ensure` declining is the
        # signal to leave it alone, not to makedirs around it.
        return False
    return _write_json(run_path(app_dir), record)


def clear_verdict(app_dir: str) -> None:
    """Drop the previous verdict BEFORE a new task starts, so the run record
    written next can never be read alongside a verdict for older content."""
    try:
        os.unlink(verdict_path(app_dir))
    except OSError:
        pass


def clear_run(app_dir: str) -> None:
    """Undo `write_run` when the task it describes was never created."""
    try:
        os.unlink(run_path(app_dir))
    except OSError:
        pass


# ------------------------------------------------------------------- the row

# The UNRUN detail for "run record, no verdict yet". The router tells the two
# cases behind it apart (task live vs. task ended empty-handed) — it is the
# only caller that can see the schedule store — and replaces this text with
# `ENDED_DETAIL` for the second. Exported so it can compare, not copy.
CHECKING_DETAIL = "an App Doctor task is checking it — listed under the app's Tasks tab"
ENDED_DETAIL = ("the last check task ended without leaving a verdict — "
                "see the Tasks tab for what happened, then press Check again")


# The shape `app_doctor._check` builds, minus what only `app_doctor` knows
# (section/severity/kind) — it wraps this with `_check(...)`.
def row_state(app_dir: str) -> tuple[str, str, list[dict]]:
    """`(state, detail, findings)` for the row as a GET should draw it —
    from the two cache files alone. Never creates a task, never spawns.

    No run record at all → UNRUN without even walking the folder (the common
    case for an app nobody has pressed Check on, and the header dot's GET
    runs on every app open). A record whose checksum no longer matches the
    folder → UNRUN, saying so. A matching record with no verdict → UNRUN,
    `CHECKING_DETAIL` — the router (`_settle_check_row`) confirms the task is
    actually still live and attaches it, or swaps in `ENDED_DETAIL` when it
    ended without a verdict. A matching record and a readable verdict →
    PASS/FAIL."""
    from fused_render.app_doctor import FAIL, PASS, UNRUN

    record = read_run(app_dir)
    if record is None:
        return UNRUN, ("not checked yet — Check creates a task that asks Claude (Sonnet) "
                       "to read the app's view files against the cross-browser rubric"), []
    try:
        current = gather(app_dir)["checksum"]
    except OSError as exc:
        return UNRUN, f"could not read the app's view files: {exc}", []
    if current != record["checksum"]:
        return UNRUN, "the app changed since it was last checked — press Check to re-run", []
    verdict = read_verdict(app_dir)
    if verdict is None:
        return UNRUN, CHECKING_DETAIL, []
    findings = verdict["findings"]
    if verdict["ok"]:
        return PASS, "nothing will look or behave differently in another browser", []
    n = len(findings)
    # The summary alone — no date, no jargon. The findings under it carry
    # the where/what/fix; the detail line only has to say how bad it is.
    return FAIL, (verdict["summary"]
                  or f"{n} thing{'' if n == 1 else 's'} will look different in another browser"), findings


def pending_task_id(app_dir: str) -> str | None:
    """The task id of a run that has no verdict yet, or None. Cheap: two
    file reads, no folder walk. What the router joins against the store."""
    record = read_run(app_dir)
    if record is None or read_verdict(app_dir) is not None:
        return None
    task_id = str(record.get("task_id") or "")
    return task_id or None


# ------------------------------------------------------------------- the run

# THE VERDICT IS WRITTEN FOR THE APP'S AUTHOR, NOT FOR A BROWSER ENGINEER.
# The first live run came back as a 90-word paragraph of rubric jargon with
# raw source lines under it (owner, 2026-09-18: "intimidating"). So the shape
# the session fills in is three plain sentences per finding — WHERE
# (file:line), WHAT will go wrong and in which browser, and the FIX in one
# line — plus one short summary. `VERDICT_SHAPE` is the prose the prompt
# hands the session (`app_doctor.check_prompt`); `_parse_verdict` is its
# lenient reader.
VERDICT_SHAPE = (
    '{"ok": bool, "summary": str, "findings": [{"path": str, "line": int, '
    '"rule": str, "what": str, "fix": str}]}\n'
    "- `ok`: true only when there is nothing to fix.\n"
    "- `summary`: one plain sentence, at most 15 words, no code and no jargon, "
    "saying how many things will look or behave differently and in which "
    "browser(s), e.g. \"Two things will look different in Safari.\" Empty when ok.\n"
    "- per finding: `path` the file as listed, `line` its 1-based line; `rule` a "
    "3-6 word name of the trap (e.g. \"Safari disclosure triangle\"); `what` one "
    "sentence (at most 20 words) saying what a visitor will see go wrong and in "
    "which browser, written for someone who does not know CSS, no code; `fix` one "
    "sentence (at most 20 words) saying what to change, may name one CSS property "
    "or attribute, no code blocks."
)


def _parse_verdict(data: dict) -> dict | None:
    """`{"ok", "summary", "findings"}` off what the session wrote, or None
    when it does not read as a verdict at all."""
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return None
    if not isinstance(data.get("findings"), list):
        return None
    findings = []
    for f in data["findings"]:
        if not isinstance(f, dict):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        # `excerpt` is the WHAT sentence (the modal's finding line); `fix` is
        # the extra line under it. Older-shaped answers (`excerpt` only) are
        # still read so a verdict from a previous build draws.
        what = str(f.get("what") or f.get("excerpt") or "").strip()
        findings.append({
            "rule": f"{CHECK_ID}:" + str(f.get("rule") or "trap").strip()[:80],
            "path": str(f.get("path") or ".").strip()[:300],
            "line": max(line, 0),
            "excerpt": what[:200],
            "fix": str(f.get("fix") or "").strip()[:200],
        })
    # A verdict that says ok but lists findings (or the reverse) is read by
    # the findings: the list is the evidence, the flag is the summary of it.
    return {"ok": not findings,
            "summary": str(data.get("summary") or "").strip().strip('"').strip()[:500],
            "findings": findings}


# The task runs in the CLI's PLAN permission mode — read-only by the CLI's own
# enforcement (Read/Grep/Glob work; Edit/Write and mutating Bash are refused),
# so "the check never edits the app" is a guarantee, not a prompt's request.
# Plan mode also means the session cannot write the verdict file itself: it
# ends its reply with the verdict as a fenced JSON block, and the SERVER
# lifts it off the transcript (`verdict_from_transcript`) into the cache when
# the task's turn is filed ok. The verdict is server-written end to end.
PERMISSION_MODE = "plan"

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def rubric() -> str | None:
    """The cross-browser skill's SKILL.md, read from disk at prompt-build time
    through `skill_sources()` (the same path trick `app_doctor.engine()` uses),
    minus its front matter. None when the skill is not installed."""
    from fused_render.skill_sources import skill_sources

    src = skill_sources().get(RUBRIC_SKILL)
    if not src:
        return None
    try:
        with open(os.path.join(src, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _assistant_texts(transcript: str) -> list[str]:
    """Every assistant text block in the transcript, in order. Tolerates
    truncated lines and unfamiliar records the way every other reader of
    these files does."""
    out: list[str] = []
    try:
        with open(transcript, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    out.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            out.append(str(block.get("text") or ""))
    except OSError:
        pass
    return out


def verdict_from_transcript(session_id: str) -> dict | None:
    """The verdict the check session left in its reply, or None. The LAST
    fenced JSON object that parses as a verdict wins — the prompt asks for
    exactly one, at the end, but a session that quoted the shape earlier
    while thinking aloud must not have that quote read as its answer."""
    from fused_render.session_liveness import transcript_path

    path = transcript_path(session_id)
    if not path:
        return None
    found = None
    for text in _assistant_texts(path):
        for m in _FENCE_RE.finditer(text):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            parsed = _parse_verdict(data)
            if parsed is not None:
                found = parsed
    return found


def settle_from_task(app_dir: str, session_id: str) -> bool:
    """Lift the finished task's verdict off its transcript into the cache.
    True when a verdict was written; False when the reply held none (the
    row then reads `ENDED_DETAIL`) or the folder refused the write."""
    verdict = verdict_from_transcript(session_id)
    if verdict is None:
        return False
    return _write_json(verdict_path(os.path.abspath(app_dir)), verdict)


def begin(app_dir: str, force: bool = False) -> tuple[dict | None, str | None, bool]:
    """Prepare a run: `(gathered, error, reuse)`, exactly one of the first two
    set. `reuse` True means the cache already answers for the folder as it
    is now — a matching verdict, or a run record with a task on it and no
    verdict yet — and no task should be created; the caller draws the row
    instead (and, for the no-verdict case, settles whether that task is
    still live). `force` is the Re-check button: ask again although the
    cached verdict still matches — the one case the checksum cannot see is
    the rubric itself having moved on. The caller must never force while a
    check task is live; the router's one-live-task-per-app gate sees to it.

    On a fresh run the previous verdict is dropped HERE, before any task
    exists, so a run record written by `record_task` next can never be read
    alongside a verdict for older content."""
    app_dir = os.path.abspath(app_dir)
    try:
        gathered = gather(app_dir)
    except OSError as exc:
        return None, f"could not read the app's view files: {exc}", False
    record = read_run(app_dir)
    if not force and record is not None and record["checksum"] == gathered["checksum"]:
        if read_verdict(app_dir) is not None or record.get("task_id"):
            return gathered, None, True
    if not app_fused_dir.ensure(app_dir):
        return None, ("this folder cannot hold the check's verdict (read-only or "
                      "mount-backed), so there is nothing for the task to write to"), False
    clear_verdict(app_dir)
    return gathered, None, False


def record_task(app_dir: str, gathered: dict, task_id: str) -> bool:
    """Write the run record once the task exists — the join key the GET
    reads. False when the folder refused, in which case the caller has a
    task running whose verdict will never be attached; it says so."""
    return write_run(os.path.abspath(app_dir), {
        "checksum": gathered["checksum"],
        "task_id": task_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "effort": EFFORT,
        "files": gathered["files"],
    })
