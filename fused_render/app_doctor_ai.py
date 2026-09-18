"""The App Doctor's one MODEL-BACKED row: `cross-browser`, answered by a
one-shot Claude call rather than a file read or a regex.

Every other row of the checklist (`app_doctor.py`) is deterministic and runs
on every GET — the header dot fetches the report each time an app opens.
This row cannot: it spends the user's tokens and takes seconds, so it is

* ON DEMAND — nothing here runs unless a person presses the row's own Check
  button (`POST /api/apps/doctor/run`, routers/apps.py). `report()` and
  `report_one()` only ever READ the cache below; they never spawn `claude`.
* CACHED ON THE APP'S CONTENT — the verdict is stored beside a sha256 over
  exactly the bytes the model was shown (`checksum`), under
  `.fused/cache/app-doctor/cross-browser.json` (SPEC §47: `cache/` is for
  bytes that can be rebuilt, which a verdict is — press Check again). A later
  GET recomputes the checksum and, if the view files changed since, the row
  reads UNRUN again with "the app changed since it was last checked" — the
  cache invalidates itself on content, never on time.
* FIXED AT SONNET + LOW EFFORT — the question is bounded (a rubric plus a
  handful of view files), the reader is waiting on a button, and the rubric
  does the heavy lifting. The FIX session that follows a failing verdict is
  an ordinary App Doctor fix task and still takes the user's own model and
  effort pickers, exactly like every other row.

THE RUBRIC IS THE SKILL. The prompt embeds `skills/fused-render-cross-browser/
SKILL.md`, read from disk at run time through `skill_sources()` the same way
`app_doctor.engine()` loads `ci/app_check.py` by path — so the check judges
by the same table the fix session later fixes by, and there is one copy of
what "compatible" means. Files are handed over with numbered lines so the
verdict's findings cite `path:line` and land in the modal in the same
`{rule, path, line, excerpt}` shape every other row's findings use.

THE SPAWN COPIES `claude_sessions._recap_generate`'S SHAPE, not `ai.py`'s
persistent process: `-p --no-session-persistence` (no phantom `.jsonl` in the
sessions list), `--input-format stream-json` with the prompt over STDIN —
file contents are full of newlines, and on the Windows `.cmd` shim a newline
inside an argv element ends the command line as far as `cmd.exe` is
concerned — `--tools=`/`--setting-sources=` in equals form (the shim drops an
empty argv element, see `ai._ai_cmd`), `--max-turns 1`, `--verbose` (the CLI
demands it with stream-json output), and `--json-schema`, which makes the
terminal `result` event carry a parsed `structured_output` — verified live
against the installed CLI on 2026-09-18 with `--model sonnet --effort low`:
~4s, two findings on a two-line fixture, both real.

The verdict is a `kind="candidate"` row (`app_doctor._CHECK_META`): a
model's reading of a rubric is not a fact (a file exists or it does not) and
its findings are worth a second look before an edit, which is exactly what
the candidate prompt (`_triage_ask`) asks the fix session to do.

NOTHING HERE RAISES INTO A REPORT. A cache that will not read is "not run
yet"; a checksum that cannot be computed reads as UNRUN with the reason; a
run that fails comes back as an error string the endpoint turns into a 502,
and the previous cached verdict — if any — is left where it was.
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

from fused_render import app_fused_dir
from fused_render.skill_sources import skill_sources

logger = logging.getLogger(__name__)

CHECK_ID = "cross-browser"
LABEL = "Renders and works alike in Chrome, Firefox and Safari"

# The skill whose SKILL.md is the rubric — and the one the fix session is
# routed to by the app-doctor skill's `cross-browser` section.
RUBRIC_SKILL = "fused-render-cross-browser"

# The model and effort the CHECK runs at. Fixed on purpose — see the module
# docstring. `sonnet` is the CLI alias, the CLI resolves it.
MODEL = "sonnet"
EFFORT = "low"
# One shot, no tools, a bounded prompt: a minute is generous, and a run that
# has not answered by then is killed rather than left burning tokens.
TIMEOUT = 120.0

# What the model is shown: the view files a browser actually parses. `.py`
# never reaches a browser, images are bytes, `.md` is prose.
_VIEW_SUFFIXES = (".html", ".htm", ".css", ".js", ".mjs", ".svg")
# Bounds on the prompt — past these the rest is listed by name only, so a
# huge app gets a partial verdict that SAYS it is partial rather than a
# request too big to send.
_MAX_FILES = 40
_MAX_FILE_BYTES = 64 * 1024
_MAX_TOTAL_BYTES = 256 * 1024

_CACHE_REL = os.path.join("app-doctor", f"{CHECK_ID}.json")


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


def _read_bounded(path: str) -> tuple[str, bool]:
    """`(text, truncated)` — at most `_MAX_FILE_BYTES`, decoded leniently."""
    with open(path, "rb") as fh:
        raw = fh.read(_MAX_FILE_BYTES + 1)
    truncated = len(raw) > _MAX_FILE_BYTES
    return raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace"), truncated


def gather(app_dir: str) -> dict:
    """The exact input one run is judged on: `{"files": [{"rel", "text",
    "truncated"}], "omitted": [rel...], "checksum": sha256-hex}`.

    The checksum covers rel path + the bytes actually sent (post-truncation)
    for every included file, plus the names of omitted ones — so it is a hash
    of what the model SAW, not of the folder: a cache hit means "same input",
    which is the only thing that makes reusing the verdict honest. Raises
    OSError only for a folder that cannot be walked at all; a single file that
    vanished mid-walk is skipped."""
    files = []
    omitted = []
    total = 0
    h = hashlib.sha256()
    for full, rel in _view_files(app_dir):
        if len(files) >= _MAX_FILES or total >= _MAX_TOTAL_BYTES:
            omitted.append(rel)
            continue
        try:
            text, truncated = _read_bounded(full)
        except OSError:
            continue
        total += len(text)
        files.append({"rel": rel, "text": text, "truncated": truncated})
        h.update(rel.encode("utf-8") + b"\0")
        h.update(text.encode("utf-8", errors="replace") + b"\0")
    for rel in omitted:
        h.update(b"omitted:" + rel.encode("utf-8") + b"\0")
    return {"files": files, "omitted": omitted, "checksum": h.hexdigest()}


# ----------------------------------------------------------------- the cache


def cache_path(app_dir: str) -> str:
    return os.path.join(app_fused_dir.cache_dir(app_dir), _CACHE_REL)


def read_cache(app_dir: str) -> dict | None:
    """The stored verdict, or None — absent, unreadable, malformed or from an
    unfamiliar writer all read the same: nothing to reuse."""
    try:
        with open(cache_path(app_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("checksum"), str):
        return None
    if not isinstance(data.get("ok"), bool) or not isinstance(data.get("findings"), list):
        return None
    return data


def write_cache(app_dir: str, verdict: dict) -> bool:
    """Best-effort: a read-only `.fused` clone (`appfile._make_read_only`) or
    a mount-backed folder `app_fused_dir.ensure` refuses just means the
    verdict is returned to the caller uncached. Temp file + `os.replace` so a
    half-written file never reads as a verdict."""
    path = cache_path(app_dir)
    try:
        if not app_fused_dir.ensure(app_dir):
            # A mount-backed or unwritable folder: `ensure` declining is the
            # signal to leave it alone, not to makedirs around it.
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(verdict, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.debug("could not cache the %s verdict for %s", CHECK_ID, app_dir, exc_info=True)
        return False


# ------------------------------------------------------------------- the row

# The shape `app_doctor._check` builds, minus what only `app_doctor` knows
# (section/severity/kind) — it wraps this with `_check(...)`.
def row_state(app_dir: str) -> tuple[str, str, list[dict]]:
    """`(state, detail, findings)` for the row as a GET should draw it —
    from the cache alone. Never spawns anything.

    No cache at all → UNRUN without even walking the folder (the common case
    for an app nobody has pressed Check on, and the header dot's GET runs on
    every app open). A cache whose checksum no longer matches the folder →
    UNRUN, saying so. A matching cache → the stored verdict."""
    from fused_render.app_doctor import FAIL, PASS, UNRUN

    cached = read_cache(app_dir)
    if cached is None:
        return UNRUN, ("not checked yet — Check asks Claude (Sonnet) to read the app's "
                       "view files against the cross-browser rubric"), []
    try:
        current = gather(app_dir)["checksum"]
    except OSError as exc:
        return UNRUN, f"could not read the app's view files: {exc}", []
    if current != cached["checksum"]:
        return UNRUN, "the app changed since it was last checked — press Check to re-run", []
    findings = [f for f in cached["findings"] if isinstance(f, dict)]
    summary = str(cached.get("summary") or "").strip()
    if cached["ok"]:
        return PASS, "nothing will look or behave differently in another browser", []
    n = len(findings)
    # The summary alone — no date, no jargon. The findings under it carry
    # the where/what/fix; the detail line only has to say how bad it is.
    return FAIL, summary or f"{n} thing{'' if n == 1 else 's'} will look different in another browser", findings


# ------------------------------------------------------------------- the run

# THE VERDICT IS WRITTEN FOR THE APP'S AUTHOR, NOT FOR A BROWSER ENGINEER.
# The first live run came back as a 90-word paragraph of rubric jargon with
# raw source lines under it (owner, 2026-09-18: "intimidating"). So the shape
# the model fills in is three plain sentences per finding — WHERE (file:line),
# WHAT will go wrong and in which browser, and the FIX in one line — plus one
# short summary, and the schema's `description`s carry the length and
# plain-language limits so they are enforced at generation, not trimmed
# after. The modal draws `excerpt` (the WHAT) as the finding's sentence and
# `fix` under it; the raw source line is never shown — the fix session has
# the file.
_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean",
               "description": "true only when there is nothing to fix"},
        "summary": {
            "type": "string",
            "description": "One plain sentence, at most 15 words, no code and no "
                           "jargon, saying how many things will look or behave "
                           "differently and in which browser(s). Example: 'Two "
                           "things will look different in Safari.' Empty when ok.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "the file, as given"},
                    "line": {"type": "integer", "description": "the numbered line"},
                    "rule": {"type": "string",
                             "description": "3-6 word name of the trap, e.g. "
                                            "'Safari disclosure triangle'"},
                    "what": {
                        "type": "string",
                        "description": "What a visitor will see go wrong, and in "
                                       "which browser — one sentence, at most 20 "
                                       "words, written for someone who does not "
                                       "know CSS. No code, no backticks.",
                    },
                    "fix": {
                        "type": "string",
                        "description": "What to change, one sentence, at most 20 "
                                       "words. May name one CSS property or "
                                       "attribute; no code blocks.",
                    },
                },
                "required": ["path", "line", "rule", "what", "fix"],
            },
        },
    },
    "required": ["ok", "summary", "findings"],
}

_SYSTEM = (
    "You review fused-render app views for cross-browser compatibility: they are "
    "opened in whatever the user's default browser is (Chrome/Edge, Firefox, Safari) "
    "and inside WKWebView, by people who are not the author. Judge ONLY by the rubric "
    "you are given — do not invent rules beyond it, and do not comment on logic, "
    "style or performance. A finding is a concrete line that will look or behave "
    "differently in one of those engines, or a feature the rubric says not to use "
    "without a fallback. Prefer few, certain findings over many doubtful ones; the "
    "same problem repeated across lines is ONE finding pointing at the first "
    "occurrence. Write every sentence for the app's author, who may not know CSS: "
    "say what a visitor would notice and in which browser, then what to change. "
    "Short sentences, plain words, no code in `summary` or `what`. Skip anything "
    "cosmetic that the rubric does not name (font weights, minor spacing)."
)


# The schema, as prose, for the Windows shim path where `--json-schema`
# cannot ride in argv (see `_run_locked`). `_parse_verdict` reads the JSON
# back out of the result text there.
_SCHEMA_IN_PROMPT = (
    "Answer with ONLY a JSON object, no prose around it: "
    '{"ok": bool, "summary": str, "findings": [{"path": str, "line": int, '
    '"rule": str, "what": str, "fix": str}]}. `summary`: one plain sentence, at '
    "most 15 words, no code, saying how many things will look different and in "
    "which browser(s) — empty when ok. Per finding: `rule` a 3-6 word name of the "
    "trap; `what` one sentence (<= 20 words) saying what a visitor sees go wrong and "
    "in which browser, no code; `fix` one sentence (<= 20 words) saying what to "
    "change, may name one CSS property."
)


def _rubric() -> str | None:
    src = skill_sources().get(RUBRIC_SKILL)
    if not src:
        return None
    try:
        with open(os.path.join(src, "SKILL.md"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _numbered(text: str) -> str:
    return "\n".join(f"{i}: {ln}" for i, ln in enumerate(text.splitlines(), 1))


def build_prompt(gathered: dict, rubric: str) -> str:
    parts = ["# Rubric\n", rubric.strip(), "\n\n# App view files\n"]
    if not gathered["files"]:
        parts.append("(no .html/.css/.js/.svg files found)\n")
    for f in gathered["files"]:
        parts.append(f"\n## {f['rel']}" + (" (truncated)" if f["truncated"] else "") + "\n")
        parts.append(_numbered(f["text"]) + "\n")
    if gathered["omitted"]:
        parts.append("\n## Not shown (past the size budget — say the verdict is partial)\n")
        parts.extend(f"- {rel}\n" for rel in gathered["omitted"])
    parts.append("\nAnswer with the JSON object described by the schema.")
    return "".join(parts)


def _result_event(stdout: str) -> dict | None:
    result = None
    for ln in stdout.splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    return result


def _parse_verdict(result: dict) -> dict | None:
    """`{"ok", "summary", "findings"}` off the terminal result event —
    `structured_output` when the CLI honoured the schema, else the `result`
    text parsed as JSON (fences stripped). None when neither reads."""
    data = result.get("structured_output")
    if not isinstance(data, dict):
        text = str(result.get("result") or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):text.rfind("}") + 1]
        try:
            data = json.loads(text)
        except ValueError:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        # `excerpt` is the WHAT sentence (the modal's finding line); `fix` is
        # the extra line under it. Older-shaped answers (`excerpt` only) are
        # still read so a cached verdict from a previous build draws.
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
    # `.strip('"')` because the structured answer has arrived with a stray
    # trailing quote inside the string (seen live) — not worth a re-run over.
    return {"ok": not findings,
            "summary": str(data.get("summary") or "").strip().strip('"').strip()[:500],
            "findings": findings}


def _agent():
    """The claude template's agent.py (`_claude_bin`, `_spawn_env`), loaded
    and cached by `project_queue.agent_module` — the same one copy
    `routers/tasks._agent_module` wraps, reached directly so this module
    never imports the router stack."""
    from fused_render import project_queue

    return project_queue.agent_module()


_inflight: dict[str, threading.Lock] = {}
_inflight_guard = threading.Lock()


def run(app_dir: str, force: bool = False) -> tuple[dict | None, str | None]:
    """Run the check now and cache the verdict. `(row_tuple, error)` — exactly
    one set: `row_tuple` is `row_state`'s `(state, detail, findings)` for the
    fresh verdict, `error` is one sentence for a 502.

    Single-flighted per folder: two Check presses on one app (two tabs) run
    the CLI once; the second waits and reads the first's verdict. `force` is
    the Re-check button: skip that reuse and ask the model again even though
    the cache still matches — the one case the checksum cannot see is the
    rubric itself having moved on.
    """
    app_dir = os.path.abspath(app_dir)
    with _inflight_guard:
        lock = _inflight.setdefault(app_dir, threading.Lock())
    with lock:
        if not force:
            # Someone else may have just finished — a matching cache is the answer.
            state, detail, findings = row_state(app_dir)
            from fused_render.app_doctor import UNRUN

            if state != UNRUN:
                return (state, detail, findings), None
        return _run_locked(app_dir)


def _run_locked(app_dir: str) -> tuple[dict | None, str | None]:
    agent = _agent()
    if agent is None:
        return None, "the Claude session host is not available on this server"
    rubric = _rubric()
    if rubric is None:
        return None, f"the {RUBRIC_SKILL} skill is not installed"
    try:
        gathered = gather(app_dir)
    except OSError as exc:
        return None, f"could not read the app's view files: {exc}"

    message = json.dumps({"type": "user", "message": {
        "role": "user",
        "content": [{"type": "text", "text": build_prompt(gathered, rubric)}]}})
    workdir = app_dir if os.path.isdir(app_dir) else tempfile.gettempdir()
    # NO FREE TEXT IN ARGV — the same rule `ai._ai_cmd` spells out: behind the
    # Windows `.cmd` shim cmd.exe re-parses the whole line and `_cmd_quote`
    # refuses any element holding a `"`. The system prompt goes through a
    # file (`--system-prompt-file`, as ai.py does), the schema is compact
    # JSON with its quotes intact — so it also has to travel as a FILE, not
    # argv... except the CLI has no `--json-schema-file`. So the schema is
    # written with single-quoted-safe content: `json.dumps` output holds `"`,
    # which `_cmd_quote` rejects. On the shim path we therefore fall back to
    # asking for JSON in the prompt (`_parse_verdict` reads the result text)
    # and pass no `--json-schema` at all. `_popen_cmd` turns the list into
    # the shim's single command string where needed.
    from fused_render.server.ai import _kill_process_tree, _needs_cmd_shim, _popen_cmd

    bin_path = agent._claude_bin()
    shim = _needs_cmd_shim(bin_path)
    fd, sp_file = tempfile.mkstemp(prefix="fused-doctor-sp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_SYSTEM if not shim else _SYSTEM + "\n\n" + _SCHEMA_IN_PROMPT)
        args = ["-p", "--no-session-persistence",
                "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--max-turns", "1", "--tools=", "--setting-sources=",
                "--model", MODEL, "--effort", EFFORT,
                "--system-prompt-file", sp_file]
        if not shim:
            args += ["--json-schema", json.dumps(_SCHEMA, separators=(",", ":"))]
        cmd = _popen_cmd(bin_path, args)
        try:
            proc = subprocess.Popen(
                cmd,
                # A STRING is the shim's command line and only means anything
                # through cmd.exe — `shell=True` is the sync twin of
                # `ai._spawn_claude_stream`'s create_subprocess_shell, and
                # just as there it is no injection surface: the payload is
                # ours, fully quoted, and every free text rides stdin or a
                # file. A list is exec'd directly.
                shell=isinstance(cmd, str),
                cwd=workdir, env=agent._spawn_env(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0))
        except (OSError, ValueError) as exc:
            return None, f"could not start claude: {exc}"
        try:
            stdout, stderr = proc.communicate(input=message + "\n", timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            # The tree, not just the top process: behind the shim `proc` is
            # cmd.exe, and a bare kill would orphan the node child that is
            # still burning tokens (`ai._kill_process_tree`).
            _kill_process_tree(proc)
            proc.communicate()
            return None, f"the check did not answer within {int(TIMEOUT)}s"
    finally:
        try:
            os.unlink(sp_file)
        except OSError:
            pass
    if proc.returncode != 0:
        tail = (stderr or "").strip().splitlines()
        return None, "claude exited " + str(proc.returncode) + (f": {tail[-1]}" if tail else "")
    result = _result_event(stdout or "")
    if result is None or result.get("is_error"):
        why = str((result or {}).get("result") or "no result event").strip()
        return None, f"the check failed: {why[:200]}"
    verdict = _parse_verdict(result)
    if verdict is None:
        return None, "the model's answer was not the JSON verdict asked for"

    stored = {
        "checksum": gathered["checksum"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "effort": EFFORT,
        "files": [f["rel"] for f in gathered["files"]],
        "omitted": gathered["omitted"],
        **verdict,
    }
    written = write_cache(app_dir, stored)
    # Read back through the same path a GET uses, so the row the button
    # returns is byte-for-byte what the next open will draw. Two ways that
    # read can still come back UNRUN, told apart rather than blamed on one:
    # the cache could not be written (read-only clone, mount-backed folder),
    # or the app changed WHILE the model was reading it, so the verdict that
    # was just written already describes a folder that is gone.
    state, detail, findings = row_state(app_dir)
    from fused_render.app_doctor import FAIL, PASS, UNRUN

    if state == UNRUN:
        state = PASS if verdict["ok"] else FAIL
        detail = verdict["summary"] or (
            "nothing will look or behave differently in another browser" if verdict["ok"]
            else f"{len(verdict['findings'])} thing"
                 f"{'' if len(verdict['findings']) == 1 else 's'} will look different in another browser")
        detail += (" (not cached: this folder is read-only)" if not written
                   else " (the app changed while it was being checked — Check again)")
        findings = verdict["findings"]
    return (state, detail, findings), None
