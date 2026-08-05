"""Files a Claude Code SESSION produced, and how everything links together.

The other two Claude sources answer "where are the app-shaped folders"
(`claude_projects`, from `~/.claude.json`) and "what did Claude Science save"
(`claude_science`). This one answers a different question: **which individual
files did my Claude Code sessions write that I might want to look at** — the
report a session saved next to the code it analysed, the figure it rendered
into a scratch folder, the page it built in a repo that is nowhere near
app-shaped. Those never satisfy a folder-shape rule, because they are files,
not folders.

The evidence is the session transcripts::

    <claude-config-dir>/projects/<cwd-slug>/<sessionId>.jsonl

Each line is one JSON record. Two kinds carry a file the session touched:

* an ``assistant`` record whose ``message.content`` holds a ``tool_use`` block
  for one of the writing tools — ``input.file_path`` is the file, verbatim and
  absolute (``NotebookEdit`` alone calls it ``notebook_path``);
* a ``file-history-delta`` record — Claude Code's own checkpoint bookkeeping,
  written whenever it is about to change a file. ``backup.realParentDir`` (an
  absolute directory) plus ``trackingPath``'s basename names the file; the
  same attribution rule ``templates/shared/file_history.py::_ghost_from``
  settled on, for the same reason — ``trackingPath`` alone is repo-relative
  and collides across checkouts.

The records also carry the session's ``cwd`` (the transcript's slug is lossy —
``-`` is both a separator and a character — so the cwd is read from the
records, never decoded from the directory name) and its first human prompt,
which is the closest thing a session has to a title.

**This module is the API; the router is a thin skin over it.** Every public
function returns plain JSON-shaped dicts so the current shell, a future UI, or
a script can consume the same calls: ``list_apps`` for the Home/hub listing,
and the related-parts pivots — ``list_sessions`` (what sessions exist),
``session_files`` (transcript → every file it touched), ``sessions_for_file``
(file → every transcript that touched it) and ``related`` (file → its sessions
AND its checkpointed versions, the git-history-like view). Versions come from
``templates/shared/file_history.py`` — the one implementation of the
checkpoint semantics (versions are checkpoints, chains are per-session, order
by mtime never by N) — loaded from its packaged path because that module is
deliberately a template-shared script, not a package member.

Transcripts are the one expensive store this app reads: real ones reach 5 MB+
and one machine holds hundreds. ``file_history``'s docstring calls reading one
per render a performance trap, so this module budgets explicitly:

* only the ``MAX_TRANSCRIPTS`` most recently modified transcripts are ever
  consulted, and anything over ``TRANSCRIPT_BYTE_CAP`` is skipped whole;
* a line is ``json.loads``-ed only when a cheap substring prefilter says it
  could matter (the same trick as ``file_history._ghosts``) — a 5 MB
  transcript is a 5 MB ``in`` scan, not 5 MB of JSON parsing;
* each parse is cached against the transcript's ``(mtime, size)``, so the
  steady-state cost of a listing is one ``stat`` per transcript. Transcripts
  are append-only journals: any growth moves both keys.

Strictly READ-ONLY with respect to the Claude config dir, like every other
`claude_*` source: nothing here writes, and every failure — an unreadable
store, a torn line from a live session, a record shaped like nothing we know —
degrades to "less listed", never to an error in the caller.
"""
import itertools
import json
import logging
import os
import stat as stat_module

from fused_render import app_listing, claude_science

logger = logging.getLogger("fused_render")

#: The `source` every app from this module carries. A FILE source, not a
#: folder one: the card's `path` is the file itself, so the shell must never
#: open it "as a project" (see the frontend's `opensAsProject` allowlist).
SOURCE = "claude-session"

#: Bounds, all logged when they bite. `MAX_TRANSCRIPTS` is the newest-first
#: window every function here works inside — a machine's transcript count
#: grows without bound, and 200 recent sessions is months of work; anything
#: older has stopped being "recent files I want back". `MAX_APPS` caps the
#: listing the same way the other sources cap theirs.
MAX_TRANSCRIPTS = 200
MAX_APPS = 500
#: Over this a transcript is skipped whole — same figure as
#: `file_history.TRANSCRIPT_BYTE_CAP`, for the same reason: a pathological
#: journal must not cost a render, even prefiltered.
TRANSCRIPT_BYTE_CAP = 64 * 1024 * 1024

#: The tools whose `tool_use` block means "this session wrote this file".
#: Read is deliberately absent: a file the session merely looked at is not the
#: session's work, and half a codebase would qualify.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

#: What earns a card. "Files I may want to SEE": pages, images the thumbnail
#: can paint, the data files this app's templates open (csv/parquet/geojson →
#: duckdb), documents. Source code is deliberately not here — a session that
#: edits twenty `.py` files is doing its job, not producing twenty things to
#: look at; those still surface through `session_files`/`related`, which list
#: everything a session touched.
VIEWABLE_SUFFIXES = (
    ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp",
    ".csv", ".parquet", ".geojson",
    ".pdf", ".md", ".txt",
)

#: Repo furniture, matched by lowercased stem. A session that touches a
#: repository almost always touches its README/CLAUDE/CHANGELOG, and every one
#: of those is a viewable `.md`/`.txt` — so without this list the listing is
#: mostly other projects' boilerplate. Canonical names only: a report the user
#: actually asked for is never called LICENSE.
REPO_DOC_STEMS = frozenset({
    "readme", "claude", "agents", "license", "licence", "notice",
    "changelog", "contributing", "code_of_conduct", "codeowners",
    "security", "requirements",
})

#: Path components that disqualify a file wherever they appear: machine
#: bookkeeping (`IGNORED_CHILDREN`) plus dependency trees. Hidden components
#: are handled separately (`_viewable`), which is also what keeps files inside
#: `~/.claude` and `~/.fused-render` out without naming them.
SKIP_COMPONENTS = frozenset(app_listing.IGNORED_CHILDREN | {"node_modules"})

#: How much of the first human prompt rides along as a session's label.
PROMPT_CAP = 140

# One parsed index per transcript, keyed by path → (mtime, size, index).
# Rebuilt whenever either key moves (transcripts are append-only journals, so
# any change moves both). Plain dict on purpose: reads/writes are atomic under
# the GIL, and the worst concurrent-request outcome is one duplicate parse.
_CACHE: dict = {}


# ------------------------------------------------------------------ locations

def config_dir() -> str:
    """Claude Code's config dir: `CLAUDE_CONFIG_DIR` when set, else `~/.claude`.

    Same resolution as `templates/shared/file_history.config_dir` (and the
    same expanduser-on-a-join reason documented there), duplicated because
    that module is a template-shared script this package loads by path — see
    `_file_history` — and a location helper is not worth the load."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.abspath(os.path.expanduser(os.path.join("~", ".claude")))


def projects_dir() -> str:
    """Where the transcripts live. The prefs page's availability probe."""
    return os.path.join(config_dir(), "projects")


_FH = None


def _file_history():
    """The template-shared `file_history` module, loaded from its packaged path.

    That file is deliberately NOT importable as `fused_render.templates.…` —
    it is a stdlib-only script that templates adopt via `sys.path` because a
    template child cannot import this package (its module docstring owns that
    story). Loading it by path here is the other direction of the same seam:
    the server reusing the ONE implementation of the checkpoint semantics
    (positional chains, mtime ordering, per-session version numbers) instead
    of growing a second one that would drift from it.
    """
    global _FH
    if _FH is None:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "shared", "file_history.py")
        spec = importlib.util.spec_from_file_location(
            "fused_render_claude_sessions_file_history", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FH = module
    return _FH


# ---------------------------------------------------------------- transcripts

def _transcripts() -> list[dict]:
    """The `MAX_TRANSCRIPTS` newest transcripts, newest first.

    Each: `{"session", "path", "mtime", "size"}`. The session id is the
    filename stem — the store's own naming — and never anything a caller sent:
    `session_files` MATCHES a requested id against this enumeration rather
    than joining it into a path, the same confinement-by-matching rule
    `file_history._resolve` documents.

    Every listing failure degrades to "fewer transcripts": an absent store is
    the normal state on a machine that never ran Claude Code.
    """
    out = []
    root = projects_dir()
    try:
        slugs = os.listdir(root)
    except OSError:
        return []
    for slug in slugs:
        slug_dir = os.path.join(root, slug)
        try:
            names = os.listdir(slug_dir)
        except (OSError, ValueError):
            continue
        for name in names:
            if not name.endswith(".jsonl") or name.startswith("."):
                continue
            path = os.path.join(slug_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat_module.S_ISREG(st.st_mode):
                continue
            if st.st_size > TRANSCRIPT_BYTE_CAP:
                logger.debug("claude-session: skipping oversized transcript %s "
                             "(%d bytes)", path, st.st_size)
                continue
            out.append({"session": name[:-len(".jsonl")], "path": path,
                        "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda t: (t["mtime"], t["path"]), reverse=True)
    if len(out) > MAX_TRANSCRIPTS:
        logger.warning("claude-session: %d transcripts on this machine; only "
                       "the %d most recent are consulted",
                       len(out), MAX_TRANSCRIPTS)
        out = out[:MAX_TRANSCRIPTS]
    return out


def _epoch(iso) -> float | None:
    """The store's ISO-8601 `Z` timestamps to epoch seconds; None unreadable."""
    if not isinstance(iso, str):
        return None
    import datetime

    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _squash(text: str) -> str:
    """One line, capped — a label, not the conversation."""
    flat = " ".join(text.split())
    return flat[:PROMPT_CAP] + ("…" if len(flat) > PROMPT_CAP else "")


def _note_file(files: dict, path, ts) -> None:
    if not isinstance(path, str) or not os.path.isabs(path):
        return  # a relative path is not attributable to anything (see _ghost_from)
    if "\0" in path:
        # No filesystem path contains NUL — but a transcript is another
        # application's journal, and os.stat raises ValueError (not OSError)
        # on one, which crashed every pivot off a single hostile line. Refuse
        # at ingestion so the index only ever holds statable paths.
        return
    path = os.path.abspath(path)
    rec = files.get(path)
    if rec is None:
        files[path] = {"first_ts": ts, "last_ts": ts, "writes": 1}
        return
    rec["writes"] += 1
    if ts is not None:
        rec["last_ts"] = ts
        if rec["first_ts"] is None:
            rec["first_ts"] = ts


def _scan_record(rec: dict, idx: dict) -> None:
    """Fold one parsed transcript record into the index. Never raises on shape:
    every field is checked before use, because this is another application's
    journal and its schema is not ours to depend on."""
    ts = _epoch(rec.get("timestamp"))
    if idx["cwd"] is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
        idx["cwd"] = rec["cwd"]
    kind = rec.get("type")
    if (idx["prompt"] is None and kind == "user"
            and not rec.get("isSidechain")):
        # The first HUMAN prompt. Sidechain user records are one agent
        # prompting another — a label the user never typed.
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            idx["prompt"] = _squash(content)
        elif isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                        and block["text"].strip()):
                    idx["prompt"] = _squash(block["text"])
                    break
    if kind == "assistant":
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") in WRITE_TOOLS):
                payload = block.get("input")
                if isinstance(payload, dict):
                    # NotebookEdit names its target `notebook_path`, not
                    # `file_path` (raised in review — with only the latter,
                    # notebook edits were invisible to every pivot unless a
                    # checkpoint delta happened to cover them). The prefilter
                    # above carries the same pair.
                    _note_file(idx["files"],
                               payload.get("file_path")
                               or payload.get("notebook_path"), ts)
    elif kind == "file-history-delta":
        # Checkpoint bookkeeping — covers a change made by ANY tool, and it is
        # attributed exactly the way file_history._ghost_from attributes a
        # ghost: absolute realParentDir + trackingPath's basename, never the
        # repo-relative trackingPath alone.
        backup = rec.get("backup")
        parent = backup.get("realParentDir") if isinstance(backup, dict) else None
        tracking = rec.get("trackingPath")
        if (isinstance(parent, str) and parent
                and isinstance(tracking, str) and tracking):
            name = os.path.basename(tracking.replace("\\", "/").rstrip("/"))
            if name:
                when = backup.get("backupTime") if isinstance(backup, dict) else None
                _note_file(idx["files"], os.path.join(parent, name),
                           _epoch(when) or ts)


def _parse(path: str) -> dict:
    """One transcript's index: `{"cwd", "prompt", "files": {path: {…}}}`.

    Line-prefiltered: `json.loads` runs only on lines that could carry a fact
    this module wants — a written file (`"file_path"` from a tool_use, or a
    checkpoint delta), or the cwd/prompt while those are still unknown. A torn
    tail from a live session, a NUL-ridden line, a record shaped like nothing
    we know: each skips one line, never the transcript.
    """
    idx = {"cwd": None, "prompt": None, "files": {}}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not ('"file_path"' in line
                        or '"notebook_path"' in line
                        or '"file-history-delta"' in line
                        or (idx["cwd"] is None and '"cwd"' in line)
                        # Both encodings: the store writes compact JSON today,
                        # but a prefilter must not silently bind this module
                        # to that formatting choice.
                        or (idx["prompt"] is None
                            and ('"type":"user"' in line
                                 or '"type": "user"' in line))):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    _scan_record(rec, idx)
    except OSError:
        logger.debug("claude-session: cannot read %s", path, exc_info=True)
    return idx


def _index(transcript: dict) -> dict:
    """`_parse`, cached against the transcript's (mtime, size)."""
    cached = _CACHE.get(transcript["path"])
    if cached is not None and cached[0] == transcript["mtime"] \
            and cached[1] == transcript["size"]:
        return cached[2]
    idx = _parse(transcript["path"])
    _CACHE[transcript["path"]] = (transcript["mtime"], transcript["size"], idx)
    return idx


def _indexes() -> list[tuple[dict, dict]]:
    """(transcript, index) for the consulted window, newest first — the one
    enumeration every public function walks. Also prunes cache entries for
    transcripts that left the window, so the cache tracks the window's size
    rather than the machine's history."""
    pairs = [(t, _index(t)) for t in _transcripts()]
    live = {t["path"] for t, _ in pairs}
    # Snapshot the keys before filtering: two requests can run this
    # concurrently (FastAPI sync handlers share a threadpool), and iterating
    # the live dict while the other thread's _index() inserts into it raises
    # RuntimeError — the one concurrent outcome the cache's "worst case is a
    # duplicate parse" claim did not cover. The del stays safe either way:
    # deleting an already-deleted key would KeyError, hence the pop.
    for stale in [p for p in list(_CACHE) if p not in live]:
        _CACHE.pop(stale, None)
    return pairs


def invalidate_cache() -> None:
    """Drop every cached parse (tests, and nothing else so far)."""
    _CACHE.clear()


# -------------------------------------------------------------- the app cards

def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + os.sep)


def _owned_roots(exclude_root: str) -> list[str]:
    """Places whose files must not become cards here: the workspace (listed by
    the workspace source), the Claude Science store (its source), the Claude
    config dir itself (a session editing `~/.claude/...` is configuring the
    tool, not making something to look at), and this app's own state dir.

    The OS temp dir is deliberately NOT here: a session that saved a report
    into a scratch folder is the exact "file I may want back" this source
    exists for, and anything genuinely ephemeral fails the exists-stat on the
    next listing anyway."""
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return [os.path.abspath(exclude_root), claude_science.claude_science_dir(),
            config_dir(), os.path.abspath(home)]


def _viewable(path: str) -> bool:
    """Whether `path` is a file a card is worth making for — by NAME alone (no
    I/O; existence is the caller's stat). Suffix must earn a preview, the stem
    must not be repo furniture, and no component may be hidden or machine
    bookkeeping — the hidden rule is also what keeps every dot-store out
    without this module naming them one by one."""
    if not path.lower().endswith(VIEWABLE_SUFFIXES):
        return False
    base = os.path.basename(path)
    stem = base[:base.rfind(".")].lower()
    if stem in REPO_DOC_STEMS:
        return False
    parts = path.replace("\\", "/").split("/")
    return not any(p.startswith(".") or p in SKIP_COMPONENTS for p in parts if p)


def _iter_apps(exclude_root: str):
    """Every card-worthy file the consulted sessions wrote, lazily, deduped.

    A generator so `MAX_APPS` caps the WORK (the islice pattern all three
    discovered sources share). Newest transcript first, so when the cap does
    bite it keeps the recent work. Dedup by path happens before the checks so
    a file fifty sessions edited costs one stat, not fifty.
    """
    seen: set[str] = set()
    owned = _owned_roots(exclude_root)
    for transcript, idx in _indexes():
        tag = os.path.basename((idx["cwd"] or "").rstrip(os.sep)) or "claude"
        for path in idx["files"]:
            if path in seen:
                continue
            seen.add(path)
            if not _viewable(path):
                continue
            if any(_under(path, root) for root in owned):
                continue
            try:
                st = os.stat(path)
            except (OSError, ValueError):
                # Deleted since, unreadable, or a path stat() refuses outright
                # (ValueError, e.g. an embedded NUL) — either way, no card.
                # _note_file already refuses NUL at ingestion; this is the
                # belt for any other value the OS rejects.
                continue
            if not stat_module.S_ISREG(st.st_mode):
                continue
            is_page = app_listing.is_html(path)
            yield {
                "name": os.path.basename(path),
                # The cwd's basename groups a session's output the way the
                # user thinks of it ("sandbox", "render"), and the hub's tag
                # chips turn that into a filter for free — same move as the
                # science source's project tags.
                "tag": tag,
                # The FILE, not a folder: it is the unit this source lists,
                # and it is what makes `path` unique across cards.
                "path": path,
                "entry": path,
                "entry_html": path if is_page else None,
                "title": app_listing.entry_title(path) if is_page else None,
                # Filesystem recency, like every other source — the transcript
                # says when the session wrote it, the mtime says when ANYTHING
                # last did, and Recent means the latter.
                "updated_at": st.st_mtime,
                "source": SOURCE,
            }


def list_apps(exclude_root: str) -> list[dict]:
    """Viewable files recent Claude Code sessions wrote, as app dicts.

    `exclude_root` is the workspace the caller lists itself. Empty when Claude
    Code isn't installed — the common case, not worth reporting. Unsorted; the
    caller merges and sorts once. Capped at `MAX_APPS` with the walk stopped
    there, one lookahead `next()` telling an honest warning from a silent cap."""
    stream = _iter_apps(exclude_root)
    apps = list(itertools.islice(stream, MAX_APPS))
    if next(stream, None) is not None:
        logger.warning("claude-session: listing capped at %d files; the "
                       "transcripts name more and the walk stopped there",
                       MAX_APPS)
    return apps


# ------------------------------------------------------------- related parts

def _session_summary(transcript: dict, idx: dict) -> dict:
    return {
        "session": transcript["session"],
        "cwd": idx["cwd"],
        "prompt": idx["prompt"],
        "transcript": transcript["path"],
        "updated_at": transcript["mtime"],
        "file_count": len(idx["files"]),
    }


def list_sessions() -> list[dict]:
    """The consulted sessions, newest first — the index a UI starts from.

    Each: `{session, cwd, prompt, transcript, updated_at, file_count}`.
    `prompt` is the session's first human message, squashed to one capped
    line: it is the only label a session has that a person would recognise.
    """
    return [_session_summary(t, idx) for t, idx in _indexes()]


def _file_record(path: str, meta: dict) -> dict:
    try:
        exists = stat_module.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        exists = False  # ValueError: a path stat() refuses (embedded NUL)
    return {
        "path": path,
        "exists": exists,
        # Whether the LISTING would card it — a UI hint, not a gate: the
        # whole point of this pivot is that it lists everything, source code
        # included, where `list_apps` deliberately does not.
        "viewable": _viewable(path),
        "first_ts": meta["first_ts"],
        "last_ts": meta["last_ts"],
        "writes": meta["writes"],
    }


def session_files(session_id: str) -> dict | None:
    """Transcript → every file it touched; None for an unknown session.

    The id is MATCHED against the enumerated window, never joined into a path
    (`file_history._resolve`'s confinement rule): a crafted id simply matches
    nothing. A session resumed from more than one directory has one transcript
    per slug; they merge here, newest transcript's cwd/prompt winning.
    """
    matched = [(t, idx) for t, idx in _indexes() if t["session"] == session_id]
    if not matched:
        return None
    head, head_idx = matched[0]
    files: dict[str, dict] = {}
    for _t, idx in matched:
        for path, meta in idx["files"].items():
            _note = files.get(path)
            if _note is None:
                files[path] = dict(meta)
            else:
                _note["writes"] += meta["writes"]
                for key, pick in (("first_ts", min), ("last_ts", max)):
                    stamps = [s for s in (_note[key], meta[key]) if s is not None]
                    _note[key] = pick(stamps) if stamps else None
    records = [_file_record(path, meta) for path, meta in files.items()]
    records.sort(key=lambda r: (r["last_ts"] or 0.0, r["path"]), reverse=True)
    return dict(_session_summary(head, head_idx),
                file_count=len(records), files=records)


def sessions_for_file(path: str) -> list[dict]:
    """File → every consulted session that touched it, newest first.

    Each: the `list_sessions` summary plus this file's `{first_ts, last_ts,
    writes}` within that session.
    """
    target = os.path.abspath(path)
    out = []
    for transcript, idx in _indexes():
        meta = idx["files"].get(target)
        if meta is None:
            continue
        out.append(dict(_session_summary(transcript, idx),
                        first_ts=meta["first_ts"], last_ts=meta["last_ts"],
                        writes=meta["writes"]))
    return out


def file_versions(path: str) -> list[dict]:
    """File → its checkpointed versions, newest first — `file_history`'s
    timeline (id, session, version, mtime, size, lines, differs, added,
    removed, exact), which is per-session-chained and mtime-ordered for the
    reasons that module owns. Unenriched: the creation-boundary ghosts need a
    transcript scan per render and matter to REVERTING, not to viewing a
    history — and nothing here reverts. Degrades to [] when the store is
    unreadable, matching every other read in this module."""
    try:
        return _file_history().list_versions(path)
    except OSError:
        logger.debug("claude-session: file-history scan failed for %s", path,
                     exc_info=True)
        return []


def related(path: str) -> dict:
    """Everything this store knows about one file, in one call.

    The pivot the git-history-like view renders: the sessions that touched the
    file (each linking onward to `session_files` — transcript → all its
    files), and the checkpointed versions of the file itself.
    """
    target = os.path.abspath(path)
    try:
        exists = stat_module.S_ISREG(os.stat(target).st_mode)
    except (OSError, ValueError):
        # ValueError covers a path stat() refuses outright (embedded NUL) —
        # this one arrives straight off the query string, so the ingestion
        # guard in _note_file never saw it. The abspath above cannot be the
        # raiser (asked in review): ntpath.abspath has caught (OSError,
        # ValueError) from _getfullpathname and fallen back to pure-string
        # resolution since Python 3.8 (CPython gh-75230), below this
        # package's >=3.10 floor — on every supported interpreter the NUL
        # surfaces here, at stat, inside the belt.
        exists = False
    return {
        "file": target,
        "exists": exists,
        "sessions": sessions_for_file(target),
        "versions": file_versions(target),
    }
