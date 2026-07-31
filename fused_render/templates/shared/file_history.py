"""Read Claude Code's own file-history store, and restore a file from it.

SPEC §33 / DECISIONS D193.

This is the undo the editor templates never had: Claude Code checkpoints a full
copy of every file it is about to change, and that store — not git — is the
authority here. Git answers "what did the last commit say"; this answers "what
did this file look like before the agent touched it", which is the question a
reviewer sitting in the annotate view actually asks, and it has an answer even
in a directory that is not a repository at all.

    <claude-config-dir>/file-history/<sessionId>/<sha256(abspath)[:16]>@v<N>

Each `@vN` is a FULL COPY of the content at a checkpoint, never a diff, and the
filename key is a pure function of the absolute path — so the whole timeline for
one file is enumerable from the filesystem, with no transcript parsing at all.
That is the entire reason this module is cheap enough to call on a view render:
the session transcripts (`<config>/projects/<slug>/<sessionId>.jsonl`) reach
5 MB+, and reading one per render would be a performance trap. They are consulted
only under `enrich=True`, only line-prefiltered, and never allowed to fail
loudly (see `_ghosts`).

Three semantics that are each easy to get wrong, all verified against a real
session, and each the subject of its own test:

  1. **Versions are checkpoints, not per-edit pre-images.** For roughly half of
     real files the highest `@vN` equals what is on disk; for the other half it
     does not, because the file moved on after the last checkpoint (6 of 13
     matched, 7 did not). So "revert the last change" is NOT "restore the
     highest N" — it is *restore the most recent version whose content DIFFERS
     from disk*, a rule that is correct under either reading and sidesteps the
     pre/post-image ambiguity entirely.
  2. **A null `backupFileName` means the file did not exist** at that
     checkpoint — Claude created it. Reverting across that boundary is a DELETE,
     not a restore of empty content. The filesystem cannot represent "no
     content", so this fact lives only in the transcript and only arrives with
     `enrich=True`.
  3. **Chains are per-session.** One path edited across several sessions has a
     separate chain under each `<sessionId>/`, and the numbers RESTART: two
     sessions both holding a `@v2` is ordinary. A global timeline therefore
     merges every session dir and orders by TIME (the backup file's mtime),
     never by N.

Strictly READ-ONLY with respect to the Claude config dir. Nothing here writes,
moves or unlinks anything under it, ever — that is the user's live edit history
and this module is a guest in it. The only write it performs is to the target
file itself, via mkstemp + `os.replace` in the target's own directory (the same
atomicity rule as `annotate.py::_save_sidecar`), gated on `file_writable`.

Stdlib only, and reachable by `sys.path`-relative import rather than
`import fused_render...`, for the reason `appenv.py` next door documents at
length: a template child under the fused engine has NO PYTHONPATH, so the
package import always fails there. Templates adopt this the way annotate.py
adopts appenv — `sys.path.insert` on `../shared` then `import file_history`.

The selector a client hands back is an OPAQUE ID (`"<session>@v<N>"`, or
`"<session>@none<N>"` for a did-not-exist checkpoint) which is matched against
the enumerated timeline and never joined into a path. Traversal has nothing to
traverse: every path this module opens it built itself out of a directory it
listed and a hash it derived.
"""
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

#: Above this, a line delta degrades to a net line-count difference and reports
#: `exact: False`. difflib is quadratic in the worst case and a timeline renders
#: every version, so an unbounded diff is a way for one big file to hang a view.
DIFF_BYTE_CAP = 2 * 1024 * 1024
#: Above this a transcript is not read at all. Enrichment is already opt-in;
#: this is the second guard, so a pathological transcript cannot cost a render
#: even when something asks for enrichment.
TRANSCRIPT_BYTE_CAP = 64 * 1024 * 1024

_HISTORY_SUBDIR = "file-history"
_PROJECTS_SUBDIR = "projects"
#: `@v` plus the exact decimal form the store writes — no sign, no padding, no
#: fraction. Anything else stays invisible rather than becoming a second,
#: ambiguous "version 1" next to the real one.
_VERSION_RE = re.compile(r"^([0-9a-f]{16})@v([1-9][0-9]*)$")
_ID_RE = re.compile(r"^(?P<session>.+)@(?:v(?P<version>[1-9][0-9]*)"
                    r"|none(?P<ghost>0|[1-9][0-9]*))$")


# ----------------------------------------------------------------- locations

def config_dir() -> str:
    """Claude Code's config dir: `CLAUDE_CONFIG_DIR` when set, else `~/.claude`.

    `expanduser` on a `join` rather than a literal `"~/.claude"`, because this
    package ships a `windows/` dir: a hardcoded forward slash survives
    `expanduser` unchanged there and then never matches a normalized path.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser(os.path.join("~", ".claude"))


def history_root() -> str:
    return os.path.join(config_dir(), _HISTORY_SUBDIR)


def path_hash(file: str) -> str:
    """The store's filename key for a path.

    sha256 of the ABSOLUTE path, first 16 hex chars — verified on 13/13 files of
    a real session. `abspath` is not cosmetic: a relative path hashes to
    something the store has never heard of, so the lookup would silently find
    nothing instead of failing.
    """
    return hashlib.sha256(os.path.abspath(file).encode()).hexdigest()[:16]


# ----------------------------------------------------------------- writability

def file_writable(file: str) -> bool:
    """True iff `apply_revert` could actually replace `file`.

    The same three-part gate as `annotate.py::_sidecar_writable`, and for the
    same reasons:

      * a read-only remote mount is asked about FIRST, because `os.access(W_OK)`
        LIES there — with CacheMode=full the write lands in the local VFS cache
        and only 403s at the async upload (the sidecar-write incident), so
        os.access would wave a doomed write through. Only the shell's persisted
        `read_only` flag knows, and it arrives through `appenv`'s env contract
        (`FUSED_RENDER_RO_MOUNTS`) rather than a `fused_render` import, which a
        template child can never do. A copy of this folder taken without its
        `shared/` sibling has no `appenv` at all and keeps the pure os.access
        behaviour.
      * an EXISTING file needs W_OK on itself, not merely on its directory: the
        `os.replace` below goes through the directory and would otherwise
        silently blow past a `chmod -w` file.
      * a file that does not exist yet (restoring one Claude deleted) needs W_OK
        on the directory, where mkstemp+replace both land.
    """
    file = os.path.abspath(file)
    try:
        from appenv import mount_read_only
        if mount_read_only(file):
            return False
    except Exception:
        pass
    if not os.access(os.path.dirname(file) or ".", os.W_OK):
        return False  # mkstemp AND the replace both land in the directory
    return os.access(file, os.W_OK) if os.path.exists(file) else True


# ----------------------------------------------------------------- content

def _read(path):
    """Bytes, or None when unreadable. None is a real answer here, not an
    error: a version whose bytes cannot be read is not restorable and not
    comparable, so it is dropped from the timeline rather than shown as an
    option that would fail on click."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _lines(data):
    """Line list, or None for content that is not UTF-8 text.

    None propagates into `exact: False` and a null delta rather than a wrong
    number: plenty of checkpointed files are binary, and they are still
    perfectly restorable byte-for-byte — they just have no honest line count.
    """
    if data is None:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _delta(cur_lines, ver_lines, cur_bytes, ver_bytes):
    """(added, removed, exact) for restoring `ver` over `cur`.

    Stated as WHAT THE RESTORE DOES — lines it introduces, lines it takes away
    — because that is the number a confirm step has to show. The reverse framing
    reads identically on symmetric edits and lies on every asymmetric one.
    """
    if cur_lines is None or ver_lines is None:
        return None, None, False
    if max(len(cur_bytes or b""), len(ver_bytes or b"")) > DIFF_BYTE_CAP:
        # Net counts only. Honest and O(1) — and flagged inexact, so the UI can
        # say "~" rather than implying a diff nobody computed.
        return (max(0, len(ver_lines) - len(cur_lines)),
                max(0, len(cur_lines) - len(ver_lines)), False)
    added = removed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, cur_lines, ver_lines, autojunk=False).get_opcodes():
        if op in ("insert", "replace"):
            added += j2 - j1
        if op in ("delete", "replace"):
            removed += i2 - i1
    return added, removed, True


def _current(file):
    """(display dict, bytes-or-None, lines-or-None) for what is on disk now.

    An ABSENT file reads as empty content for diff purposes (`[]` lines) but
    keeps `bytes = None`, and the two are used for different questions: absence
    is what makes every version `differ` (so "Claude deleted my file" has an
    undo), while `[]` is what makes its delta a plain "+N added" instead of an
    unknown. Binary content is the opposite pair — bytes present, lines None.
    """
    data = _read(file) if os.path.isfile(file) else None
    lines = [] if data is None else _lines(data)
    return {
        "exists": data is not None,
        "size": len(data) if data is not None else 0,
        "lines": len(lines) if lines is not None else None,
    }, data, lines


# ----------------------------------------------------------------- enumeration

def _session_dirs():
    """Every `<sessionId>/` under the history root, or [] when there is no store.

    A missing root, an unreadable one, and a root full of stray files
    (`.DS_Store`) are all the same non-event: return what can be listed. This
    module is a guest in someone else's directory and has no standing to fail
    the caller over its shape.
    """
    root = history_root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        p = os.path.join(root, n)
        if os.path.isdir(p):
            out.append((n, p))
    return out


def _ghosts(file, sessions):
    """Did-not-exist checkpoints, read from the session transcripts.

    The one fact the filesystem cannot carry (semantic 2), hence the only reason
    to open a transcript at all — and it is opt-in for the caller because these
    files reach 5 MB+. Three things keep the cost bounded and the failure modes
    quiet:

      * a byte cap, checked by `stat` before anything is opened;
      * a SUBSTRING PREFILTER per line, so `json.loads` runs only on the handful
        of lines that could possibly be a null-backup delta. A 5 MB transcript
        is then a 5 MB `in` scan, not 5 MB of JSON parsing;
      * a blanket except around each transcript. Corrupt, truncated, NUL-ridden,
        half-written by a live session — every one of those degrades to "no
        ghost entries", never to an error in the view. The filesystem truth is
        already complete without them.

    `trackingPath` is repo-relative, and this module does not know the repo
    root, so it is matched as a path-boundary SUFFIX of the target's absolute
    path (or accepted outright when the transcript recorded an absolute path).
    Ambiguity is bounded by the fact that we only consult transcripts of
    sessions that exist in the store at all.
    """
    ap = os.path.abspath(file)
    want = ap.replace("\\", "/")
    out = []
    projects = os.path.join(config_dir(), _PROJECTS_SUBDIR)
    for session, _dir in sessions:
        for slug in _project_slugs(projects):
            path = os.path.join(projects, slug, session + ".jsonl")
            try:
                if os.path.getsize(path) > TRANSCRIPT_BYTE_CAP:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "file-history-delta" not in line:
                            continue
                        if "backupFileName" not in line or "null" not in line:
                            continue
                        rec = json.loads(line)
                        entry = _ghost_from(rec, session, want)
                        if entry is not None:
                            out.append(entry)
            except Exception:
                # Missing, unreadable, oversized, corrupt — all the same
                # non-event. See the docstring.
                continue
    return out


def _project_slugs(projects):
    try:
        return sorted(os.listdir(projects))
    except OSError:
        return []


def _ghost_from(rec, session, want):
    if not isinstance(rec, dict) or rec.get("type") != "file-history-delta":
        return None
    backup = rec.get("backup")
    if isinstance(backup, dict) and backup.get("backupFileName"):
        return None  # a real content checkpoint — the filesystem already has it
    tracking = rec.get("trackingPath")
    if not isinstance(tracking, str) or not tracking:
        return None
    rel = tracking.replace("\\", "/").lstrip("/")
    if not (want == rel or want.endswith("/" + rel)):
        return None
    version = 0
    if isinstance(backup, dict) and isinstance(backup.get("version"), int):
        version = backup["version"]
    when = None
    if isinstance(backup, dict):
        when = backup.get("backupTime")
    when = when or rec.get("timestamp")
    return {
        "id": "%s@none%d" % (session, version),
        "session": session,
        "version": version,
        "existed": False,
        "path": None,
        "mtime": _epoch(when),
        "size": 0,
        "lines": 0,
    }


def _epoch(stamp):
    """ISO-8601 (with the store's trailing `Z`) to epoch seconds; 0.0 when it
    cannot be read, which sorts the entry to the very start of the timeline —
    the right place for a "before anything" marker whose time is unknown."""
    if not isinstance(stamp, str):
        return 0.0
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def list_versions(file, enrich: bool = False) -> list:
    """Every version of `file` across every session, NEWEST FIRST.

    Newest-first because every consumer wants that end: the revert rule takes
    the first entry that differs, and the timeline renders top-down from the
    most recent. One order, so no caller has to remember which way round it is.

    Ordering is by the backup file's mtime, with the version number only as a
    tiebreak WITHIN a session — never across (semantic 3). Entries:

        {id, session, version, existed, path, mtime, size, lines,
         differs, added, removed, exact}

    `differs` compares content against what is on disk right now, which is the
    whole basis of the revert rule (semantic 1). `enrich=True` additionally
    reads the transcripts to surface did-not-exist checkpoints (semantic 2).
    """
    file = os.path.abspath(file)
    key = path_hash(file)
    _, cur_bytes, cur_lines = _current(file)

    entries = []
    sessions = _session_dirs()
    for session, sdir in sessions:
        try:
            names = os.listdir(sdir)
        except OSError:
            continue  # unreadable session dir: skipped, never fatal
        for name in names:
            m = _VERSION_RE.match(name)
            if not m or m.group(1) != key:
                continue
            p = os.path.join(sdir, name)
            data = _read(p)
            if data is None:
                continue  # vanished or unreadable mid-scan — not an option
            lines = _lines(data)
            added, removed, exact = _delta(cur_lines, lines, cur_bytes, data)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            entries.append({
                "id": "%s@v%s" % (session, m.group(2)),
                "session": session,
                "version": int(m.group(2)),
                "existed": True,
                "path": p,
                "mtime": mtime,
                "size": len(data),
                "lines": len(lines) if lines is not None else None,
                # Byte comparison, not "is this the highest N" (semantic 1).
                # `cur_bytes` is None when the file is absent, so every version
                # differs from a deleted file — which is what gives "Claude
                # deleted my file" an undo.
                "differs": data != cur_bytes,
                "added": added,
                "removed": removed,
                "exact": exact,
            })

    if enrich:
        for g in _ghosts(file, sessions):
            g = dict(g)
            # Deleting is a no-op once the file is already gone, so an absent
            # target makes the boundary NOT differ — the timeline must not offer
            # a revert that would do nothing.
            g["differs"] = cur_bytes is not None
            g["added"] = 0
            g["removed"] = len(cur_lines) if cur_lines is not None else None
            g["exact"] = cur_lines is not None
            entries.append(g)

    entries.sort(key=lambda e: (e["mtime"], e["version"]), reverse=True)
    return entries


# ----------------------------------------------------------------- the payload

def timeline(file, enrich: bool = False) -> dict:
    """Everything the view needs in one call, including its own empty states.

    `note` is the degradation channel: a human sentence for "no store", "no
    versions for this file", "nothing to revert". Each of those is an ordinary,
    expected state — the whole point of returning them as data is that the view
    renders an informative panel instead of a traceback overlay.
    """
    file = os.path.abspath(file)
    cur, _bytes, _lines_ = _current(file)
    versions = list_versions(file, enrich=enrich)
    root = history_root()
    available = os.path.isdir(root)
    target = next((v for v in versions if v["differs"]), None)

    if not available:
        note = ("No Claude Code file history on this machine (%s)."
                % root)
    elif not versions:
        note = "Claude has no recorded versions of this file."
    elif target is None:
        note = "This file already matches its most recent checkpoint."
    else:
        note = ""

    return {
        "file": file,
        "hash": path_hash(file),
        "available": available,
        "writable": file_writable(file),
        "current": cur,
        "versions": versions,
        "revert": target["id"] if target else None,
        "note": note,
    }


def _resolve(file, entry_id, enrich):
    """Match an opaque selector against the enumerated timeline.

    This is the whole path-confinement story, and it is a matching problem
    rather than a sanitizing one: nothing the client sends is ever joined into a
    path, so an id carrying `..`, separators or an absolute prefix does not
    "escape" — it simply matches no entry. `_ID_RE` runs first only to reject
    obvious garbage cheaply (and to keep a non-string from reaching `.match`).
    """
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        raise ValueError("bad version selector: %r" % (entry_id,))
    for v in list_versions(file, enrich=enrich):
        if v["id"] == entry_id:
            return v
    raise ValueError("no such version for this file: %r" % (entry_id,))


def revert_plan(file, entry_id=None, enrich: bool = False) -> dict:
    """What a revert would do — the payload the confirm step renders.

    `entry_id=None` means "the last change", i.e. the newest version whose
    content differs from disk (semantic 1).

    `unique_current` is the sharp one. Current on-disk content is frequently in
    NO checkpoint — the file moved on after the last one — so a naive restore
    vaporizes work that exists nowhere else, with no undo. When it is True the
    UI must not let the write land without an explicit confirmation that shows
    the loss. A "no version to revert to" answer is returned as data
    (`ok: False` + `error`), never raised: it is an ordinary state of a file
    Claude has not touched, not a failure.
    """
    file = os.path.abspath(file)
    cur, cur_bytes, _cl = _current(file)
    versions = list_versions(file, enrich=enrich)

    if entry_id is None:
        entry = next((v for v in versions if v["differs"]), None)
        if entry is None:
            return {"ok": False, "current": cur,
                    "error": ("Claude has no recorded versions of this file."
                              if not versions else
                              "This file already matches its most recent "
                              "checkpoint — nothing to revert.")}
    else:
        entry = _resolve(file, entry_id, enrich)

    # `differs` already IS "this version's bytes are not what is on disk", so
    # "no version holds the current content" is just "all of them differ" — no
    # second pass over the version files.
    unique = bool(cur_bytes is not None
                  and all(v["differs"] for v in versions if v["existed"]))
    return {
        "ok": True,
        "id": entry["id"],
        "session": entry["session"],
        "version": entry["version"],
        "action": "restore" if entry["existed"] else "delete",
        "added": entry["added"],
        "removed": entry["removed"],
        "exact": entry["exact"],
        "mtime": entry["mtime"],
        "current": cur,
        "target": {"size": entry["size"], "lines": entry["lines"],
                   "existed": entry["existed"]},
        "unique_current": unique,
        "writable": file_writable(file),
    }


def apply_revert(file, entry_id, enrich: bool = False) -> dict:
    """Put `entry_id`'s content back on disk (or delete the file).

    Refuses rather than degrades, because every refusal here is a case where
    guessing destroys something:

      * an unresolvable selector, or a target the store has never recorded a
        version of — the only path guard a module that cannot see the view can
        offer. A crafted `file` param therefore reaches nothing Claude never
        edited;
      * a directory target;
      * an unwritable target (`file_writable`, which includes the read-only
        mount that `os.access` lies about).

    The write is mkstemp + `os.replace` in the target's OWN directory — atomic,
    never cross-device, and no reader ever sees a half-written file. The mode of
    an existing file is carried onto the replacement, since a fresh mkstemp is
    0600 and a revert has no business changing a file's permissions.
    """
    file = os.path.abspath(file)
    if os.path.isdir(file):
        raise ValueError("target is a directory: %r" % (file,))
    entry = _resolve(file, entry_id, enrich)
    if not file_writable(file):
        raise PermissionError("%r is not writable" % (file,))

    if not entry["existed"]:
        # The file did not exist at this checkpoint, so reverting to it is a
        # DELETE — never a restore of empty content, which would leave a
        # zero-byte file Claude never created.
        try:
            os.unlink(file)
        except FileNotFoundError:
            pass
        return {"ok": True, "action": "delete", "id": entry["id"], "bytes": 0}

    data = _read(entry["path"])
    if data is None:
        raise ValueError("version content is unreadable: %r" % (entry_id,))
    parent = os.path.dirname(file) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".fh-tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, os.stat(file).st_mode & 0o7777)
        except OSError:
            pass  # target absent (restoring a deleted file) — keep mkstemp's mode
        os.replace(tmp, file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "action": "restore", "id": entry["id"],
            "bytes": len(data)}
