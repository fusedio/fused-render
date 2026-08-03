"""runPython target for annotate/template.html: mirror the review's comments
into the target file's `<file>.json` sidecar as a write-only LOG.

The URL `comments` param stays the sole LIVE store the annotate view reads back;
this sidecar is pure history — every comment ever seen for the file, keyed by
`id`, updated in place. A comment that has disappeared from the incoming array
is simply left as its last-seen state, forever: absence NEVER deletes (each URL
carries only its own review subset). Only an id named in `deleted_ids` — sent on
the SAME `record` call, so upsert and tombstone land in one atomic
read-merge-write with no cross-call ordering race — is stamped `deleted_at`.
The tombstone is PERMANENT: recording an id never clears it, so a stale
bookmarked URL that still carries a deleted comment cannot silently resurrect
it. Nothing here is read back into the view.

It is the SAME sidecar the claude chat template keeps next to each target
(templates/claude/agent.py) and the bookmark history mirror
(fused_render/shell/bookmarks.py) writes to, so every key this module does not
own (claudeSessions, bookmarkHistory, lastSession, ...) is preserved through a
read-merge-write — a later claude turn round-trips them instead of clobbering
them off disk.

Stdlib only, save for one guarded lazy import of `../shared/appenv.py` (itself
stdlib-only): _sidecar_writable consults the mount read_only flag through it to
detect a read-only remote mount, degrading to pure os.access when appenv isn't
reachable (a copy of this folder taken without its `shared/` sibling). See
_sidecar_writable.

It ALSO serves the view's revert surface (SPEC §34, D194), which is a different
job on the same file: the version timeline and the restore itself live in
`../shared/file_history.py` (Claude Code's own checkpoint store, deliberately not
git), and this module is the bridge that (a) turns every failure into an
`{"error": ...}` dict, because anything raised out of `main` becomes the red
traceback overlay and "this file has no history" is not an error, and (b) stashes
the pre-restore content into the sidecar it already owns. That stash exists
because the current bytes on disk are frequently in NO checkpoint — the file
moved on after the last one — so a restore can vaporize work with no other copy.

Actions:
  main(action="record", file=..., comments=[...], deleted_ids=[...])
    -> {"recorded": True, "count": N, "deleted": M}
  main(action="status", file=...)
    -> {"writable": bool}  # can the sidecar be written? Commenting still
       # works read-only (the URL is the live store); the template just warns
       # that history won't be recorded.
  main(action="history", file=..., enrich=False)
    -> file_history.timeline(...) — versions + current + revert selector + note
  main(action="revert_plan", file=..., version_id=None, enrich=False)
    -> what the write would do, for the confirm step (version_id=None means
       "the last change")
  main(action="revert", file=..., version_id=None, enrich=False)
    -> {"ok": True, "action": "restore"|"delete", "stashed": bool,
        "timeline": {...}, ...}   # the post-write timeline, so the panel does
                                  # not show the pre-revert position for the
                                  # length of a second round trip
"""
import json
import os
import sys
import tempfile
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "annotate.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))


# ------------------------------------------------------------- sidecar store

def _sidecar_path(file: str) -> str:
    return file + ".json"


def _load_sidecar(file: str) -> dict:
    # Preserve every key we don't own (claudeSessions, bookmarkHistory,
    # lastSession, ...) so a later claude turn / bookmark write round-trips them
    # instead of clobbering them off disk. Corrupt or absent -> empty dict.
    try:
        with open(_sidecar_path(file), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = {}
    # Keep agent.py's _load_sidecar guard happy so a claude turn round-trips our
    # comments log instead of dropping it (same defense as bookmarks.py).
    data.setdefault("claudeSessions", [])
    return data


def _save_sidecar(file: str, data: dict) -> None:
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- comments log

def _record(file: str, comments: list, deleted_ids: list) -> dict:
    """Upsert each incoming comment (keyed by its `id`) into the sidecar's
    top-level "comments" log, preserving claudeSessions/bookmarkHistory/
    lastSession and every other key — then tombstone each `deleted_ids` entry
    in the SAME write, so a delete can never race a concurrent record call.

    Write-only LOG semantics (mirrors bookmarks.py `_record_history`): an id
    already in the log is updated in place — non-None incoming fields merged,
    server `updated_at` bumped; a new id is appended with recorded_at+updated_at.
    A comment that has DISAPPEARED from the incoming array is left untouched: its
    last-seen state persists forever — absence never deletes (each URL carries
    only its own review subset, so a missing id means "not in this review").
    `deleted_ids` is the ONE signal that says "deleted on purpose": each named
    log entry gets `deleted_at` stamped (unknown ids are ignored). The stamp is
    permanent — re-recording the id merges its fields but keeps `deleted_at`,
    so a stale URL still carrying the comment can't undo the delete.

    `createdAt` is the comment's own ms-epoch (Date.now, from the template);
    `recorded_at`/`updated_at`/`deleted_at` are server `time.time()` SECONDS
    (matching agent.py's created_at/last_used and bookmarks.py's history
    stamps). Different units in one file, by design — do not "unify" them."""
    file = os.path.abspath(file)
    data = _load_sidecar(file)
    log = data.get("comments")
    if not isinstance(log, list):
        log = []
    by_id = {c["id"]: c for c in log
             if isinstance(c, dict) and isinstance(c.get("id"), str)}

    now = time.time()
    count = 0
    for c in comments:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if not isinstance(cid, str):
            continue
        # Undefined JS fields never reach JSON, so incoming keys are already the
        # comment's live fields; the None guard only mirrors bookmarks.py so a
        # sparse update can't clobber a stored value with null.
        fields = {k: v for k, v in c.items() if v is not None}
        fields.pop("deleted_at", None)  # the stamp is server-owned, never incoming
        existing = by_id.get(cid)
        if existing is not None:
            existing.update(fields)
            existing["updated_at"] = now
        else:
            entry = {**fields, "recorded_at": now, "updated_at": now}
            log.append(entry)
            by_id[cid] = entry
        count += 1

    deleted = 0
    for did in deleted_ids:
        entry = by_id.get(did) if isinstance(did, str) else None
        if entry is None:
            continue
        entry["deleted_at"] = now
        entry["updated_at"] = now
        deleted += 1

    # Nothing recorded AND nothing tombstoned is a true no-op: never touch
    # disk, so an emptied URL can't spuriously create/rewrite the sidecar and
    # the existing log stays exactly as last seen.
    if count == 0 and deleted == 0:
        return {"recorded": True, "count": 0, "deleted": 0}

    data["comments"] = log
    # Honor a read-only sidecar: the os.replace in _save_sidecar goes through
    # the directory and would silently overwrite a chmod -w file. The caller
    # (template.html) treats record as best-effort, so this just becomes the
    # warning badge its status probe already showed.
    if not _sidecar_writable(file):
        raise PermissionError(f"{_sidecar_path(file)!r} is read-only")
    _save_sidecar(file, data)
    return {"recorded": True, "count": count, "deleted": deleted}


def _sidecar_writable(file: str) -> bool:
    """True iff _save_sidecar would succeed: an existing sidecar needs W_OK on
    itself (the os.replace above would otherwise bypass its read-only bit via
    the directory), a fresh one needs W_OK on the directory (mkstemp+replace
    both land there).

    False under a read-only remote mount, where os.access(W_OK) LIES: with
    CacheMode=full a write lands in the local VFS cache and only 403s at the
    async upload (the sidecar-write incident), so os.access would wave the
    doomed write through. Only the shell's persisted read_only flag can answer
    this, and it arrives via `shared/appenv` (FUSED_RENDER_RO_MOUNTS, re-exported
    on every mount-store write) rather than by importing fused_render — a
    template child under the fused engine has no PYTHONPATH, so that import
    ALWAYS failed there and every read-only mount looked writable. A copy of this
    folder taken without its `shared/` sibling still keeps the pure os.access
    behavior."""
    file = os.path.abspath(file)
    try:
        from appenv import mount_read_only
        if mount_read_only(file):
            return False
    except Exception:
        pass
    path = _sidecar_path(file)
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(os.path.dirname(path), os.W_OK)


# ------------------------------------------------------------- revert (§34)

#: Stash entries kept in the sidecar. Small on purpose: this is an "oh no, undo
#: the undo" buffer, not a version store — `file_history` already is one — and
#: the sidecar is a small JSON file the claude template rewrites constantly.
STASH_KEEP = 3
#: Content above this is NOT copied into the sidecar. Better a revert with no
#: stash (and the caller told, so the UI can make its confirm step firmer) than a
#: multi-megabyte sidecar that every other writer of it then has to round-trip.
STASH_BYTE_CAP = 256 * 1024


def _file_history():
    """The shared reader.

    ImportError ONLY — that is the one condition this degrades over: a copy of
    this folder taken without its `shared/` sibling, the same degradation
    `_sidecar_writable` has for appenv, where revert is simply not offered and
    nothing else in the view changes. A blanket `except Exception` also caught a
    SyntaxError or any other import-time bug inside `file_history.py` and
    reported it as "helper is not available", which reads as "you copied the
    folder wrong" and sends the reader to entirely the wrong place. Anything else
    now reaches `main`'s wrapper, which names the exception type.
    """
    import file_history
    return file_history


def _stash_plan(file: str) -> tuple:
    """(will_stash, note, content) — WITHOUT writing anything.

    Split out from `_stash` so the confirm sheet can state the truth BEFORE the
    click. The skip decision used to be made inside the revert, after the write
    was already committed to, so the sheet carried a permanent hedge ("a copy is
    kept ... unless too large or not text") and the one genuinely unrecoverable
    combination — content in no checkpoint AND no stash — was indistinguishable
    from the safe case. The user found out in the past tense. The predicate is
    cheap (a stat, a decode, an access), so there is no reason for the sheet not
    to know, and `_stash` consumes this same function so the promise and the
    action cannot drift apart.

    The three refusals are reported SEPARATELY and truthfully. Folding an EACCES
    into "not UTF-8 text", or a getsize failure into "nothing on disk" (which
    reads as "the file is absent"), describes a fixable machine problem as a fact
    about the content.
    """
    try:
        size = os.path.getsize(file)
    except FileNotFoundError:
        return False, "nothing on disk to stash", None
    except OSError as exc:
        return False, (f"could not measure the previous content — "
                       f"{_why(exc, file)}"), None
    if size > STASH_BYTE_CAP:
        return False, (f"previous content ({size} bytes) is too large to stash "
                       f"in the sidecar — it is not recoverable from here"), None
    # BINARY read + explicit decode. Text mode applied universal-newline
    # translation, so a CRLF file stashed as LF: the recovered content was not
    # the bytes that were destroyed, and it disagreed with the `size` recorded
    # beside it. This repo ships a `windows/` dir, so CRLF is a live case.
    try:
        with open(file, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return False, (f"previous content could not be read — "
                       f"{_why(exc, file)}"), None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, ("previous content is not UTF-8 text — not stashed, and "
                       "not recoverable from here"), None
    if not _sidecar_writable(file):
        return False, (f"{_sidecar_path(file)!r} is read-only — nothing "
                       f"stashed"), None
    return True, "", raw


def _why(exc, path) -> str:
    errno = getattr(exc, "errno", None)
    reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return "%s: %s%s" % (path, reason,
                         "" if errno is None else " (errno %d)" % errno)


def _stash(file: str, version_id: str) -> tuple:
    """Copy the CURRENT content into the sidecar's `revertStash`, newest last.

    Called before the write lands, because after it there is nothing left to
    copy. Returns (stashed, note) rather than raising: a revert that the user
    confirmed — having been told by `_stash_plan` whether a copy would be kept —
    must not then be blocked by a sidecar we could not write.

    `size` is the BYTE count of what was on disk, and `content` decodes back to
    exactly those bytes; the two must agree or a hand-recovery from the sidecar
    restores something that was never there.
    """
    ok, note, raw = _stash_plan(file)
    if not ok:
        return False, note
    content = raw.decode("utf-8")
    data = _load_sidecar(file)
    stash = data.get("revertStash")
    if not isinstance(stash, list):
        stash = []
    stash.append({
        "version_id": version_id,
        "at": time.time(),          # server seconds, like recorded_at/updated_at
        "size": len(raw),
        "lines": len(content.splitlines()),
        "content": content,
    })
    data["revertStash"] = stash[-STASH_KEEP:]
    try:
        _save_sidecar(file, data)
    except OSError as exc:
        return False, f"could not write the stash: {exc}"
    return True, ""


def _plan(file: str, version_id, fh) -> dict:
    """`file_history.revert_plan` completed with the stash predicate.

    The reader has no business knowing the sidecar exists, and the sheet has no
    business guessing whether a copy will be kept, so the bridge that owns the
    sidecar is where the two facts meet.
    """
    plan = fh.revert_plan(file, version_id)
    if plan.get("ok"):
        ok, note, _raw = _stash_plan(file)
        plan["stash"] = ok
        plan["stash_note"] = note
    return plan


def _revert(file: str, version_id, confirm_unique: bool) -> dict:
    """Apply a revert the caller has already seen a plan for.

    Two refusals that exist because the confirm gate used to live ONLY in the
    page. `action="revert"` with no `version_id` performed the destructive write
    off its own freshly-computed choice, with no plan echo and no confirmation
    token, and the sole guard was a source grep over today's template — which
    pins the page, not this function, so any future second caller inherited an
    unguarded file-destroying entry point.

      * the plan's `id` must be echoed back. That is also a freshness check: a
        plan built against one disk state and applied against another is exactly
        how a user confirms one diff and gets a different one.
      * when the plan reports `unique_current` — the bytes on disk are in no
        checkpoint, so the write destroys the only copy — `confirm_unique` must
        be true. Deliberately NOT demanded for an ordinary step back, where
        nothing unrecorded is lost: a token the caller always has to pass is a
        token nobody reads.
    """
    fh = _file_history()
    if not isinstance(version_id, str) or not version_id:
        return {"error": "revert requires the version_id from a revert_plan "
                         "call — this action never chooses a target itself"}
    plan = _plan(file, version_id, fh)
    if not plan.get("ok"):
        return plan
    # Writability is re-read from the plan BEFORE anything is written. `_stash`
    # runs first by design (after the write there is nothing left to copy), so a
    # target that cannot be written must be refused here or the stash lands and
    # `apply_revert` then raises — a failed revert that still mutated the sidecar.
    # For a symlink that was worse than useless: the stashed content was read
    # THROUGH the link, so the sidecar held the wrong file's bytes.
    if plan.get("writable") is False:
        return {"error": "This file cannot be reverted: "
                         + (plan.get("writable_reason") or "it is not writable")}
    if plan.get("unique_current") and not confirm_unique:
        return {"error": "this file's current content is in no checkpoint, so "
                         "the revert would destroy the only copy — pass "
                         "confirm_unique=true once the user has confirmed",
                "plan": plan}
    stashed, note = _stash(file, plan["id"])
    res = fh.apply_revert(file, plan["id"])
    res["stashed"] = stashed
    res["stash_note"] = note
    # The POST-write timeline, in the same response. The page used to follow every
    # revert with a second `history` call, and for that whole round trip the row
    # list went on showing the pre-revert position — precisely the window in which
    # the user is staring at it to find out whether the revert worked.
    #
    # ENRICHED unconditionally, like the plan and the write: this is what the panel
    # displays, and an unenriched timeline cannot see the did-not-exist boundary, so
    # adopting one would report `at_earliest` a step early. The caller's disclosure
    # state does not enter into it — that rule (`enrich` honoured for `history` and
    # nowhere else) is about what the button DOES, and this changes only what the
    # panel shows.
    #
    # Best-effort, and the field is simply ABSENT when it fails: the write already
    # landed and is already reported, so a failure to re-enumerate the store must
    # not turn a successful revert into an error. The page falls back to its own
    # `history` call, which reports its own failure in its own words.
    #
    # Absorbed for the USER, not for the log. With no trace at all, a timeline that
    # has started failing on every revert is indistinguishable from one that never
    # fails — the page falls back, the panel still paints, and the only symptom is a
    # round trip nobody can account for. stderr is where the engine already collects
    # a run's diagnostics, so naming the exception costs the user nothing.
    try:
        res["timeline"] = fh.timeline(file, enrich=True)
    except Exception as exc:
        print("annotate: post-revert timeline failed, the page will re-read it "
              "itself — %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    return res


def main(action: str = "record", file: str = "", comments=None,
         deleted_ids=None, version_id=None, enrich: bool = False,
         confirm_unique: bool = False) -> dict:
    if action == "status":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return {"writable": _sidecar_writable(file)}
    if action == "record":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not isinstance(comments, list):
            comments = []
        if not isinstance(deleted_ids, list):
            deleted_ids = []
        return _record(file, comments, deleted_ids)
    # Every revert action answers with data, never an exception: a raised error
    # here reaches the page as the red traceback overlay, and "no history for
    # this file" / "read-only mount" / "stale version id" are all ordinary
    # states of this surface that the panel renders as text.
    if action in ("history", "revert_plan", "revert"):
        if not file:
            return {"error": "missing target file (no _file param?)"}
        try:
            if action == "revert":
                return _revert(file, version_id, bool(confirm_unique))
            fh = _file_history()
            if action == "history":
                # `enrich` is honoured HERE and nowhere else: the boot timeline
                # stays off the 5 MB transcripts, while the plan and the write
                # always enrich, so a disclosure widget cannot change what the
                # button DOES (only what the panel shows).
                return fh.timeline(file, enrich=bool(enrich))
            return _plan(file, version_id, fh)
        except ImportError:
            return {"error": "file history helper (../shared/file_history.py) "
                             "is not available"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown action: {action}"}
