"""Read Claude Code's own file-history store, and restore a file from it.

SPEC §34 / DECISIONS D194, D195.

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

Four semantics that are each easy to get wrong, all verified against real
sessions, and each the subject of its own test:

  1. **Versions are checkpoints, not per-edit pre-images.** For roughly half of
     real files the highest `@vN` equals what is on disk; for the other half it
     does not, because the file moved on after the last checkpoint (6 of 13
     matched, 7 did not). So "revert the last change" is NOT "restore the
     highest N".
  2. **...but it is not "the newest version that differs from disk" either, and
     that near-miss is the sharpest lesson in this module.** That rule reads as
     obviously fine and oscillates on the SECOND press: with disk == v3, v3 does
     not differ so the target is v2; once disk == v2, v3 differs and is newest,
     so the target is v3 again — a two-state ping-pong in which v1 is
     unreachable forever. Undo is POSITIONAL: locate where disk sits in the
     chain, then step backwards. See `_locate`.
  3. **A null `backupFileName` means the file did not exist** at that
     checkpoint — Claude created it. Reverting across that boundary is a DELETE,
     not a restore of empty content. The filesystem cannot represent "no
     content", so this fact lives only in the transcript, and it is attributed by
     the record's ABSOLUTE `realParentDir`, never by a path suffix (see
     `_ghost_from` for the cross-project delete that taught us).
  4. **Chains are per-session.** One path edited across several sessions has a
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
import glob as _glob
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
#: Above this a transcript is not read at all. Enrichment is already opt-in for
#: the timeline; this is the second guard, so a pathological transcript cannot
#: cost a render even when something asks for enrichment.
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


def _why(exc, path) -> str:
    """A failure a user can act on: the path, the reason, and the errno.

    "no versions for this file" is a fact about the FILE; a permissions problem
    is a fact about the machine, and collapsing the second into the first sends
    the user looking in the wrong place. `chmod` is actionable; "no versions" is
    not.
    """
    errno = getattr(exc, "errno", None)
    reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return "%s: %s%s" % (path, reason,
                         "" if errno is None else " (errno %d)" % errno)


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
        template child can never do.
      * an EXISTING file needs W_OK on itself, not merely on its directory: the
        `os.replace` below goes through the directory and would otherwise
        silently blow past a `chmod -w` file.
      * the DIRECTORY needs W_OK either way, because mkstemp and the replace
        both land there. (This half `_sidecar_writable` does not need and a
        replace does.)

    The two ways the mount probe can be unavailable are handled DIFFERENTLY on
    purpose, and the difference is the whole point of the first bullet. A MISSING
    `appenv` — a copy of this folder taken without its `shared/` sibling —
    degrades to the pure os.access rule, because there is no flag to consult and
    refusing every local file would break the feature outright. But a probe that
    RAISES fails CLOSED: one blanket `except Exception` around the call re-opens
    exactly the incident the probe exists for, letting a malformed
    `FUSED_RENDER_RO_MOUNTS` (or any OSError normalizing a path) fall through to
    the lie, report `ok: True`, and surface the 403 later at an async upload
    where this UI will never see it.
    """
    return writable_reason(file) == ""


def writable_reason(file: str) -> str:
    """"" when `file` is writable, else WHY it is not, in a sentence a user can
    act on.

    The reason has to travel with the verdict. The view disables its revert
    controls on the bool, and "it cannot be reverted" with no cause is a dead end
    for the three genuinely different situations here: a read-only mount (nothing
    local to fix — the remote rejects writes), a `chmod -w` file (fixable), and an
    unwritable directory (fixable, and a different thing to fix). This module
    already distinguishes all three to reach its answer; throwing that away and
    letting the UI guess was the actual defect.
    """
    file = os.path.abspath(file)
    # A SYMLINK, or a DIRECTORY, is refused for reasons that have nothing to do
    # with permissions — but they belong here anyway, because this is the function
    # every layer already consults to decide whether to offer a revert at all.
    # `apply_revert` refused both correctly and refused them ALONE, one layer below
    # the decision: so the sheet opened on a target that could not succeed, the
    # bridge stashed the sidecar with content read THROUGH the link, and only then
    # did the write raise — a failed revert that still mutated `revertStash`, with
    # the wrong file's content in it. Same shape as the read-only case before it:
    # the guard existed, just under the layer that offers the action.
    if os.path.isdir(file):
        return "it is a directory, not a file"
    if os.path.islink(file):
        return ("it is a symlink — os.replace would replace the LINK rather than "
                "write through it, and the checkpoint chain belongs to the path "
                "the view opened, not to the link's target")
    try:
        from appenv import mount_read_only
    except ImportError:
        mount_read_only = None
    if mount_read_only is not None:
        try:
            if mount_read_only(file):
                return ("this file is on a read-only mount, so the remote would "
                        "reject the write (os.access cannot see that)")
        except Exception as exc:
            # Unanswerable => not writable; see the docstring.
            return ("the read-only-mount check failed (%s: %s), so writing here "
                    "cannot be shown to be safe"
                    % (type(exc).__name__, exc))
    parent = os.path.dirname(file) or "."
    if not os.access(parent, os.W_OK):
        return "its directory (%s) is not writable" % parent
    if os.path.exists(file) and not os.access(file, os.W_OK):
        return "the file itself is read-only"
    return ""


# ----------------------------------------------------------------- content

def _read(path):
    """(bytes, None) or (None, exception).

    The exception comes back rather than being swallowed because a version that
    cannot be read is DROPPED from the timeline, and a drop moves where the
    backward walk starts and lands (`_locate`) — so the reason has to reach the
    user instead of dying here.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(), None
    except OSError as exc:
        return None, exc


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
    is what makes every content version `differ` (so "Claude deleted my file"
    has an undo) while making a did-not-exist boundary MATCH — which is what
    makes the backward walk terminate there instead of offering a delete that
    would do nothing. `[]` is what makes a delta a plain "+N added" instead of
    an unknown. Binary content is the opposite pair — bytes present, lines None.
    """
    data = _read(file)[0] if os.path.isfile(file) else None
    lines = [] if data is None else _lines(data)
    return {
        "exists": data is not None,
        "size": len(data) if data is not None else 0,
        "lines": len(lines) if lines is not None else None,
    }, data, lines


# ----------------------------------------------------------------- enumeration

def _session_dirs():
    """(list of (sessionId, dir), error-or-None) under the history root.

    A missing root is NOT an error — it means Claude Code has never run here.
    An unreadable one is, and it comes back as the exception rather than as an
    empty list, because "no versions for this file" and "I could not look" are
    different sentences and only one of them is actionable.
    """
    root = history_root()
    try:
        names = os.listdir(root)
    except (FileNotFoundError, NotADirectoryError):
        return [], None
    except OSError as exc:
        return [], exc
    out = []
    for n in sorted(names):
        p = os.path.join(root, n)
        if os.path.isdir(p):
            out.append((n, p))
    return out, None


def _ghosts(file, sessions):
    """Did-not-exist checkpoints, read from the session transcripts.

    The one fact the filesystem cannot carry (semantic 3), hence the only reason
    to open a transcript at all — and it is opt-in for the timeline because these
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

    A GLOB over `projects/*/<session>.jsonl` rather than a slug cross-product:
    the slug is the cwd with separators replaced, which is lossy (a directory
    name containing `-` is indistinguishable from a separator), so it was never
    evidence of anything anyway. Attribution is `_ghost_from`'s job, and it uses
    a fact that is not lossy.
    """
    ap = os.path.abspath(file)
    out = []
    projects = os.path.join(config_dir(), _PROJECTS_SUBDIR)
    for session, _dir in sessions:
        for path in _glob.glob(os.path.join(projects, "*",
                                            _glob.escape(session) + ".jsonl")):
            try:
                if os.path.getsize(path) > TRANSCRIPT_BYTE_CAP:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "file-history-delta" not in line:
                            continue
                        if "backupFileName" not in line or "null" not in line:
                            continue
                        entry = _ghost_from(json.loads(line), session, ap)
                        if entry is not None:
                            out.append(entry)
            except Exception:
                # Missing, unreadable, oversized, corrupt — all the same
                # non-event. See the docstring.
                continue
    return out


def _ghost_from(rec, session, ap):
    """One did-not-exist entry, or None when the record is not about `ap`.

    ATTRIBUTION IS AN IDENTITY TEST, and this is the sharpest correctness rule in
    the module. The first version matched on nothing but `trackingPath` being a
    path-boundary suffix of the target — and `trackingPath` is repo-relative,
    with this module having no idea what the repo root is. `src/main.py`,
    `README.md` and `index.ts` recur across every checkout on the disk, so an
    unrelated project that CREATED its own `src/main.py` injected a ghost into
    this file's timeline; because ghosts sort by the transcript's own timestamp
    it was typically the NEWEST entry, so it became the revert target and turned
    "Revert last change" into a DELETE of a file Claude never created, behind a
    confirm sheet asserting "Claude created it" about a file it had never seen.

    The record already carries `realParentDir` — an ABSOLUTE directory, present
    on null-backup records — so the rule is
    `join(realParentDir, basename(trackingPath)) == abspath(target)`. No suffix
    heuristic, no cwd guess, no cross-project collision possible.

    A record with no usable `realParentDir` is REFUSED rather than falling back
    to the suffix match: guessing here means offering to delete the wrong file,
    and the cost of refusing is only that one boundary row does not appear.
    """
    if not isinstance(rec, dict) or rec.get("type") != "file-history-delta":
        return None
    backup = rec.get("backup")
    if isinstance(backup, dict) and backup.get("backupFileName"):
        return None  # a real content checkpoint — the filesystem already has it
    tracking = rec.get("trackingPath")
    if not isinstance(tracking, str) or not tracking:
        return None
    parent = backup.get("realParentDir") if isinstance(backup, dict) else None
    if not isinstance(parent, str) or not parent:
        return None  # unattributable => refused, never guessed
    name = os.path.basename(tracking.replace("\\", "/").rstrip("/"))
    if not name or os.path.abspath(os.path.join(parent, name)) != ap:
        return None
    version = 0
    if isinstance(backup, dict) and isinstance(backup.get("version"), int):
        version = backup["version"]
    when = backup.get("backupTime") if isinstance(backup, dict) else None
    return {
        "id": "%s@none%d" % (session, version),
        "session": session,
        "version": version,
        "existed": False,
        "path": None,
        "mtime": _epoch(when or rec.get("timestamp")),
        "size": 0,
        "lines": 0,
    }


def _epoch(stamp):
    """ISO-8601 (with the store's trailing `Z`) to epoch seconds; 0.0 when it
    cannot be read, which sorts the entry to the very start of the timeline —
    the right place for a "before anything" marker whose time is unknown. The
    view renders 0 as "time unknown" rather than as 1970."""
    if not isinstance(stamp, str):
        return 0.0
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _scan(file, enrich):
    """(entries newest-first, skipped, store_error).

    `skipped` is not bookkeeping. A dropped version moves where the backward walk
    starts and where it lands (`_locate`), so every drop is recorded with a reason
    and a time, and the automatic revert refuses rather than silently walking to
    a different point in history.
    """
    file = os.path.abspath(file)
    key = path_hash(file)
    _, cur_bytes, cur_lines = _current(file)

    entries = []
    skipped = []
    sessions, store_error = _session_dirs()
    for session, sdir in sessions:
        try:
            names = os.listdir(sdir)
        except OSError as exc:
            skipped.append({"session": session, "version": None, "mtime": None,
                            "reason": _why(exc, sdir)})
            continue
        for name in names:
            m = _VERSION_RE.match(name)
            if not m or m.group(1) != key:
                continue
            version = int(m.group(2))
            p = os.path.join(sdir, name)
            try:
                mtime = os.path.getmtime(p)
            except OSError as exc:
                skipped.append({"session": session, "version": version,
                                "mtime": None, "reason": _why(exc, p)})
                continue
            data, exc = _read(p)
            if data is None:
                skipped.append({"session": session, "version": version,
                                "mtime": mtime, "reason": _why(exc, p)})
                continue
            lines = _lines(data)
            added, removed, exact = _delta(cur_lines, lines, cur_bytes, data)
            entries.append({
                "id": "%s@v%d" % (session, version),
                "session": session,
                "version": version,
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
            g["differs"] = cur_bytes is not None
            g["added"] = 0
            g["removed"] = len(cur_lines) if cur_lines is not None else None
            g["exact"] = cur_lines is not None
            entries.append(g)

    entries.sort(key=lambda e: (e["mtime"], e["version"]), reverse=True)
    return entries, skipped, store_error


def list_versions(file, enrich: bool = False) -> list:
    """Every version of `file` across every session, NEWEST FIRST.

    Newest-first because every consumer wants that end: the positional walk
    counts forward from index 0 into the past, and the timeline renders top-down
    from the most recent. One order, so no caller has to remember which way round
    it is.

    Ordering is by the backup file's mtime, with the version number only as a
    tiebreak WITHIN a session — never across (semantic 4). Entries:

        {id, session, version, existed, path, mtime, size, lines,
         differs, added, removed, exact}
    """
    return _scan(file, enrich)[0]


# ------------------------------------------------------- the revert selection

def _locate(entries, cur_bytes):
    """(position index, target entry, at_earliest) — the revert rule.

    Undo is POSITIONAL. The rule this replaced — "the newest version whose
    content differs from disk" — is a near-miss that reads as obviously correct
    and oscillates on the second press:

        disk == v3  ->  v3 differs=False, v2 differs=True  ->  target v2
        disk == v2  ->  v3 differs=True and is newest      ->  target v3

    ...for ever, with v1 unreachable at any point. It answers "which checkpoint
    is most recent and isn't what I have", which is not what undo means.

    So: find where disk SITS in the chain, then step backwards.

      1. position = index of the NEWEST entry whose content equals disk. Newest,
         not oldest: with duplicate content on both sides of a real checkpoint,
         walking from the oldest match would step straight over that checkpoint.
      2. target = the first entry OLDER than position (a higher index) that still
         DIFFERS from disk. The differs-check stays inside the walk — it is
         deliberately not "the entry at position+1" — because identical adjacent
         versions are common and restoring one would write the same bytes back,
         an action that looks like a broken button.
      3. position == -1 (disk is in no checkpoint at all — the `unique_current`
         case, derived from this same index so the two can never disagree) means
         the first step back is "discard to the most recent checkpoint":
         target = entry 0.
      4. no such older entry => at_earliest. A distinct terminal state that
         DISABLES the button, rather than falling back to something newer —
         falling back is precisely what made the old rule a two-state toggle.

    Consequence, and it is intended: the chain is walked in one direction only.
    "Redo" needs no new UI, because the timeline already lets the user click a
    newer row explicitly — and that asymmetry is right for a button labelled
    "Revert last change".
    """
    position = -1
    for i, v in enumerate(entries):
        if not v["differs"]:
            position = i
            break
    if position == -1:
        return -1, (entries[0] if entries else None), not entries
    for v in entries[position + 1:]:
        if v["differs"]:
            return position, v, False
    return position, None, True


def _unsafe_skips(skipped, target):
    """The skips that make the positional walk unsafe.

    A skip corrupts POSITION, not merely the target: an unread version might have
    been the entry that equals disk, or a nearer step back. So anything at or
    newer than the chosen target disqualifies the automatic choice, as does a
    skip whose own time is unknown. Older skips are harmless WHEN THERE IS A
    TARGET — one unreadable ancient checkpoint must not cost the user their undo.

    When there is NO target the floor is `-inf`, so every skip counts, and that
    is deliberate rather than the bug it looks like: "no target" means the walk
    found nothing older that differs, and a version we failed to read is exactly
    a candidate for the older differing entry we did not find. There is no
    subset of skips that could not matter here. Claiming terminality from a scan
    with a hole in it would be asserting something unprovable — the same class of
    error as the oscillating rule, a confident answer from an incomplete read —
    so `_selection` reports it as `unconfirmed` instead.
    """
    floor = target["mtime"] if target else float("-inf")
    return [s for s in skipped if s["mtime"] is None or s["mtime"] >= floor]


def offer_reason(file, target, blocking, at_earliest, unconfirmed, versions):
    """"" when the AUTOMATIC revert may be offered, else why it may not.

    THE single authority for "may this action be offered, and if not why", and it
    exists because three separate findings had the same root cause: a guard that
    lived below the layer deciding whether to offer the action. The read-only
    verdict was checked in `apply_revert` but not in the plan, so the sheet opened
    on a doomed target; the symlink refusal likewise, so the stash ran first; and
    the blocking-skips refusal was computed in `revert_plan` while the panel
    published a striped target and an enabled button that could only ever produce
    that refusal. Point-fixing each one would leave the fourth to be found by a
    user, so the panel, the rows, the sheet and the bridge now all read ONE answer.

    Deliberately about the AUTOMATIC choice only. An explicitly clicked version is
    a different request and stays available: "revert the last change" is a
    question this module answers (and can therefore decline to answer when the
    scan has a hole in it), whereas "revert to THIS version" is the user naming
    the target themselves, where there is nothing left to guess. That asymmetry is
    stated here rather than left as an accident of which gate happens to run.
    """
    why = writable_reason(file)
    if why:
        return "This file cannot be reverted: " + why
    if blocking:
        # A refusal `revert_plan` will certainly produce, so it must not be
        # presented as an available action first (N1: with a target still alive
        # the panel struck it as "this is what Revert does" and every press hit
        # the same refusal).
        n = len(blocking)
        if unconfirmed:
            return ("%d version(s) could not be read, so this cannot be confirmed "
                    "as the earliest checkpoint — refusing to guess." % n)
        return ("%d version(s) could not be read, so the last change cannot be "
                "identified — refusing to guess." % n)
    if not versions:
        return "Claude has no recorded versions of this file."
    if at_earliest or target is None:
        return ("Already at the earliest checkpoint Claude recorded — nothing "
                "older to revert to.")
    return ""


def _selection(entries, cur_bytes, skipped):
    """(position, target, at_earliest, unconfirmed, blocking) — one computation.

    `timeline` and `revert_plan` both route through here so they cannot disagree
    about what the button will do, which is how the first version of this ended up
    with a panel claiming one thing and a plan doing another.

    `at_earliest` and `unconfirmed` are mutually exclusive and both terminal for
    the button, but they are DIFFERENT states and the user needs to be told which:
    the first is "there is nothing older" (a fact), the second is "a version could
    not be read, so whether there is anything older is unknown" (an admission).
    """
    position, target, terminal = _locate(entries, cur_bytes)
    blocking = _unsafe_skips(skipped, target)
    unconfirmed = bool(blocking) and target is None
    return position, target, terminal and not unconfirmed, unconfirmed, blocking


# ----------------------------------------------------------------- the payload

def timeline(file, enrich: bool = False) -> dict:
    """Everything the view needs in one call, including its own empty states.

    `note` is the degradation channel: a human sentence for "no store", "no
    versions for this file", "already at the earliest checkpoint", "a version
    could not be read". Each of those is an ordinary, expected state — the whole
    point of returning them as data is that the view renders an informative panel
    instead of a traceback overlay.
    """
    file = os.path.abspath(file)
    cur, cur_bytes, _cl = _current(file)
    versions, skipped, store_error = _scan(file, enrich)
    position, target, at_earliest, unconfirmed, blocking = _selection(
        versions, cur_bytes, skipped)
    root = history_root()
    why_not = writable_reason(file)

    # ONE answer, so the panel cannot advertise an action the plan will refuse.
    # Publishing `revert` only when it may actually be offered is what makes the
    # striped row and the enabled button correct by construction rather than by
    # two separate conditions the view has to keep in step.
    blocked = offer_reason(file, target, blocking, at_earliest, unconfirmed,
                           versions)
    # FH-3's rule, and the one case where a refusal is PROVISIONAL rather than
    # final: an unenriched scan cannot see the creation boundary, so its
    # "nothing older" is a guess and must not disable anything. There is no
    # target to publish either (the walk found none), so the button needs its own
    # signal — `offer` — rather than inferring permission from `revert`.
    provisional = (bool(blocked) and not enrich and not blocking
                   and versions and not writable_reason(file)
                   and (at_earliest or target is None))
    if provisional:
        blocked = ""
    offer = not blocked
    notes = []
    if store_error is not None:
        available = True  # it is THERE; we could not read it
        notes.append("Cannot read the file-history store — "
                     + _why(store_error, root))
    else:
        available = os.path.isdir(root)
        if not available:
            notes.append("No Claude Code file history on this machine (%s)."
                         % root)
        elif not versions:
            notes.append("Claude has no recorded versions of this file.")
        elif blocked:
            # Every un-offerable state is a STATE with its reason on screen, not
            # an error discovered by clicking: each would refuse identically on
            # every press. The `at_earliest and not enrich` carve-out is FH-3's
            # rule — an unenriched scan may not claim terminality, so it stays
            # quiet and lets the click ask the (always-enriched) plan.
            notes.append(blocked)
        elif at_earliest and enrich:
            # Only claimable from an ENRICHED scan. Without the transcripts the
            # did-not-exist boundary is invisible, so an unenriched scan reports
            # at_earliest one step early — which, when the view believed it,
            # disabled the button on a file whose remaining step back was a
            # delete the plan would happily have offered. Found by pressing the
            # button four times in the running app.
            notes.append("Already at the earliest checkpoint Claude recorded — "
                         "nothing older to revert to.")
    for s in skipped:
        notes.append("A version could not be read (%s) — %s"
                     % ("v%d" % s["version"] if s["version"] else "session",
                        s["reason"]))

    return {
        "file": file,
        "hash": path_hash(file),
        "available": available,
        "writable": why_not == "",
        # Travels WITH the verdict: "it cannot be reverted" with no cause is a
        # dead end, and a read-only mount, a chmod'd file and an unwritable
        # directory are three different things to do about it.
        "writable_reason": why_not,
        "current": cur,
        "versions": versions,
        "position": versions[position]["id"] if position >= 0 else None,
        # The row to stripe: only ever the target of an action that may actually
        # be offered, so the stripe cannot advertise a refusal.
        "revert": target["id"] if (target and offer) else None,
        # May the button be pressed? Separate from `revert` because of the
        # provisional case above, where there is no target to name and the click
        # is nonetheless the right thing to allow — the plan is the authority and
        # it enriches.
        "offer": offer,
        "offer_reason": blocked,
        # PROVISIONAL unless `enriched`. `revert_plan` always enriches (it has no
        # `enrich` parameter at all), so an unenriched timeline can be one step
        # short of the truth and must never be used to decide that there is
        # nothing left to revert — see the note above.
        "at_earliest": at_earliest,
        # Terminality could NOT be established because the scan had a hole in it.
        # Distinct from at_earliest on purpose (see `_selection`) and equally
        # terminal for the button — but it is an admission, not a fact.
        "unconfirmed": unconfirmed,
        "blocking": blocking,
        "enriched": bool(enrich),
        "unique_current": cur_bytes is not None and position == -1,
        "skipped": skipped,
        "note": " ".join(notes),
    }


def _resolve(file, entry_id):
    """Match an opaque selector against the enumerated timeline.

    This is the whole path-confinement story, and it is a matching problem
    rather than a sanitizing one: nothing the client sends is ever joined into a
    path, so an id carrying `..`, separators or an absolute prefix does not
    "escape" — it simply matches no entry. `_ID_RE` runs first only to reject
    obvious garbage cheaply (and to keep a non-string from reaching `.match`).
    """
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        raise ValueError("bad version selector: %r" % (entry_id,))
    for v in list_versions(file, enrich=True):
        if v["id"] == entry_id:
            return v
    raise ValueError("no such version for this file: %r" % (entry_id,))


def revert_plan(file, entry_id=None) -> dict:
    """What a revert would do — the payload the confirm step renders.

    `entry_id=None` means "the last change", resolved by the positional rule in
    `_locate`.

    There is deliberately NO `enrich` parameter. It used to be one, the view
    passed its History-panel state into it, and so the same button performed a
    RESTORE before the panel had been expanded and a DELETE after — one click,
    two different destructive outcomes, decided by whether a disclosure widget
    was open. Enrichment is unconditional here: it is paid once, on an explicit
    click, and only the boot timeline stays unenriched for the documented perf
    reason.

    `unique_current` is the sharp one, and it is the same index that drives case
    3 of the walk: current on-disk content is in NO checkpoint, so a restore
    destroys the only copy. When it is True the UI must not let the write land
    without an explicit confirmation showing the loss — and `annotate.py`
    additionally refuses the write unless that confirmation is echoed back.

    Every "nothing to revert to" answer is returned as data (`ok: False` +
    `error`, with `at_earliest` distinguishing the terminal state from an empty
    store), never raised: they are ordinary states of a file, not failures.
    """
    file = os.path.abspath(file)
    cur, cur_bytes, _cl = _current(file)
    versions, skipped, store_error = _scan(file, enrich=True)
    position, auto_target, at_earliest, unconfirmed, blocking = _selection(
        versions, cur_bytes, skipped)

    if entry_id is None:
        # The SAME authority the panel reads, so the two can never disagree about
        # whether this action is available (that disagreement was the whole of
        # N1: a striped row and a live button over a plan that always refused).
        blocked = offer_reason(file, auto_target, blocking, at_earliest,
                              unconfirmed, versions)
        if store_error is not None:
            blocked = ("Cannot read the file-history store — "
                       + _why(store_error, history_root()))
        if blocked:
            # The per-version reasons are appended HERE and not in offer_reason:
            # the panel wants one short sentence it can render inline, and the
            # click wants the errno detail that says which file to go and fix.
            err = blocked
            if blocking:
                err += " " + " ".join(s["reason"] for s in blocking)
            return {"ok": False, "at_earliest": at_earliest,
                    "unconfirmed": unconfirmed, "blocking": blocking,
                    "skipped": skipped, "current": cur, "error": err}
        entry = auto_target
    else:
        # An explicit id is a different request and is NOT gated on the automatic
        # refusals (see offer_reason): the user named this version, so there is
        # nothing to guess. It is still gated on writability, below — that one is
        # about whether the write can land at all, which no amount of naming fixes.
        entry = _resolve(file, entry_id)
        why = writable_reason(file)
        if why:
            return {"ok": False, "at_earliest": False, "unconfirmed": False,
                    "blocking": [], "skipped": skipped, "current": cur,
                    "error": "This file cannot be reverted: " + why}

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
        "position": versions[position]["id"] if position >= 0 else None,
        "target": {"size": entry["size"], "lines": entry["lines"],
                   "existed": entry["existed"]},
        "unique_current": cur_bytes is not None and position == -1,
        "at_earliest": False,
        "unconfirmed": False,
        "blocking": [],
        "skipped": skipped,
        "writable": True,   # a plan is only returned for a writable target now
        "writable_reason": "",
    }


def apply_revert(file, entry_id) -> dict:
    """Put `entry_id`'s content back on disk (or delete the file).

    Refuses rather than degrades, because every refusal here is a case where
    guessing destroys something:

      * an unresolvable selector, or a target the store has never recorded a
        version of — the only path guard a module that cannot see the view can
        offer. A crafted `file` param therefore reaches nothing Claude never
        edited;
      * a directory target;
      * a SYMLINK target. `os.replace` swaps the LINK for a regular file rather
        than writing through it, so the real file kept its pre-revert content
        while the call reported success — and the sidecar stash captured a file
        that was never overwritten. Refusing is chosen over `realpath`-ing first,
        deliberately: the store's key is the sha256 of the path the VIEW opened,
        so following the link would revert a path whose own timeline is a
        different chain, and silently editing a file the user did not name is
        worse than declining to.
      * an unwritable target (`file_writable`, which includes the read-only
        mount that `os.access` lies about).

    Like `revert_plan`, this takes no `enrich` parameter — the selector must mean
    the same thing to the plan and to the write.

    The write is mkstemp + `os.replace` in the target's OWN directory — atomic,
    never cross-device, and no reader ever sees a half-written file. The mode of
    an existing file is carried onto the replacement, since a fresh mkstemp is
    0600 and a revert has no business changing a file's permissions.
    """
    file = os.path.abspath(file)
    if os.path.isdir(file):
        raise ValueError("target is a directory: %r" % (file,))
    if os.path.islink(file):
        raise ValueError("target is a symlink, which os.replace would replace "
                         "rather than write through: %r" % (file,))
    entry = _resolve(file, entry_id)
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

    data, exc = _read(entry["path"])
    if data is None:
        raise ValueError("version content is unreadable: %s"
                         % _why(exc, entry["path"]))
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
