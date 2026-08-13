"""Every Artifact this machine has published, recovered from Claude Code's own
session transcripts — the entry contract behind `GET /api/claude-artifacts`.

There is no artifacts index on disk to read. Publishing an Artifact is a tool
call inside a Claude Code session, and the only durable trace it leaves locally
is in the session transcript at ~/.claude/projects/<encoded-cwd>/<id>.jsonl.
Two kinds of line there matter, and BOTH are needed:

  * a top-level `{"type": "frame-link", ...}` record — one per successful
    publish, carrying the four facts that make an artifact addressable: the
    local `path` that was published, the hosted `frameUrl`, the `title`, and a
    `timestamp`. This is the authoritative publish record; if it isn't there,
    the publish didn't land, which is why an artifact with no frame-link is not
    listed at all.
  * the assistant's `tool_use` record for the Artifact call, which is the only
    place the `description` and `favicon` the author chose ever appear. The
    frame-link doesn't echo them back.

So the shape below is a JOIN, keyed on the local file path within a session,
between "what got published" and "how the author described it".

Identity is the `frameUrl`, not the file path. One page is redeployed many
times — a `.html` in a scratchpad gets published, edited, published again — and
every redeploy writes another frame-link for the SAME url. Those are one
artifact with a history, not N artifacts: latest publish wins for what to show
(title, path, description), `created_at` is the first publish and `updated_at`
the last. The dedupe also has to run ACROSS transcripts, because an artifact
can be updated from a different session later via the tool's `url` parameter —
a per-session merge alone would list that page twice.

Cost is the reason for the two-stage prefilter and the per-file cache. Real
transcript stores run to hundreds of megabytes; `json.loads` on every line of
that per request is not viable, and neither is re-reading unchanged files. So
lines are substring-screened before parsing (a cheap `in` test rejects
virtually all of them), and each transcript's merged result is cached against
its (mtime, size) so a repeat request only re-reads the transcripts that
actually moved.

Nothing in here raises for input it cannot read: an unreadable transcript, a
truncated JSON line, a timestamp in an unexpected format, a missing projects
dir — each degrades to "no artifacts from that", never to a failed listing.
`exists` is deliberately the one fact NOT cached: scratchpad files are
temporary and the caller needs to know, right now, whether the local source is
still openable.
"""
import glob
import json
import os
from datetime import datetime, timezone

from fused_render._view_url_codec import canonical_fs_path

# CLAUDE_CONFIG_DIR wins where set — same rule (and same deliberate local
# duplication) as server/routers/claude_sessions.py, user_skills.py and the
# claude template's CLAUDE_DIR.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

# Substring screens applied to a raw line BEFORE json.loads. Cheap enough to run
# over every line of a multi-hundred-MB store; a line that passes is one of the
# few thousand we actually care about. Written as the JSON-encoded field values so
# they can't match a stray mention in prose — but only as a *filter*, so a false
# positive costs one wasted parse and a future format change (spaces after the
# colons, say) still matches on the bare tokens.
_FRAME_LINK_HINT = "frame-link"
_TOOL_USE_HINTS = ("Artifact", "tool_use")

# How far into a transcript to hunt for its `cwd` when no line we parse for
# other reasons happens to carry one. It is normally on the very FIRST record
# (claude_sessions._session_cwd relies on the same thing), so this is a bound on
# a pathological file rather than a real budget — without it, a transcript that
# never records a cwd would cost a full json.loads of every line.
_CWD_PROBE_LINES = 20

# path -> (mtime, size, merged-artifacts-for-that-transcript). Keyed by absolute
# path, so a monkeypatched PROJECTS_DIR under tmp can never collide with a real
# transcript's entry. Pruned to the transcripts currently on disk on each
# listing, so a deleted session doesn't pin its result for the process lifetime.
_CACHE: dict[str, tuple[float, int, list[dict]]] = {}


def reset_cache() -> None:
    """Forget every cached transcript. For tests, and for any caller that wants
    the next listing to re-read from disk unconditionally."""
    _CACHE.clear()


def _epoch(value) -> float | None:
    """A transcript's ISO-8601 timestamp as an epoch float, or None.

    The trailing "Z" is rewritten rather than passed through: `fromisoformat`
    only learned to accept it in 3.11, and this package still runs on 3.10. A
    timestamp with no zone at all is read as UTC — every writer of these records
    emits UTC, and guessing local time would silently shift an artifact's
    position in a listing sorted by time.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _text(value) -> str | None:
    """A transcript string field, or None for anything unusable — absent, the
    wrong type, or empty. Empty and absent are the same claim here ("no title
    was recorded"), and collapsing them keeps the UI from having to know."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def artifact_dict(
    file_path: str,
    remote_url: str,
    *,
    title: str | None = None,
    description: str | None = None,
    favicon: str | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
    created_at: float | None = None,
    updated_at: float | None = None,
) -> dict:
    """One listed artifact — the single place the entry shape is built.

    `exists` is resolved HERE and nowhere else, on every call: it is the only
    field that can change without any transcript changing (the published file
    was in a scratchpad and got cleaned up), so it must never come out of the
    per-transcript cache the rest of these fields do.

    A mount-backed path is never stat'ed and reports `exists: False`. The
    kernel os.stat is the GETATTR that wedges a dead mount (see
    server/mount.py), and this runs once per artifact on every listing — one
    bad mount would hang the whole page. False is also the SAFE answer, not
    just the cheap one: `exists: True` would make the UI render a live iframe
    preview per card, each of which is a read through the same mount. The card
    falls back to its hosted claude.ai link, which is always openable.
    """
    # Lazy import, following server/mount.py's own use of this helper: the
    # mounts machinery would be a heavy top-level dependency for a module that
    # is otherwise pure transcript parsing.
    from fused_render.shell.mounts import is_mount_backed

    return {
        # The local file that was published, in the shell's canonical form
        # (forward slashes — see canonical_fs_path, and the same call in the
        # sessions router): a Windows transcript records the backslashed form,
        # and frontend helpers like `basename` split on "/" only. Not otherwise
        # re-resolved: that string is what the publish actually used, and it is
        # already absolute.
        "file_path": canonical_fs_path(file_path),
        "exists": not is_mount_backed(file_path) and os.path.isfile(file_path),
        "remote_url": remote_url,
        "title": title,
        "description": description,
        "favicon": favicon,
        # The session of the LATEST publish, not the first: it is offered as
        # "where this artifact is being worked on", and for an artifact updated
        # from a second session the first one is the stale answer.
        "session_id": session_id,
        "cwd": cwd,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _artifact_tool_inputs(obj) -> list[dict]:
    """Every Artifact tool call `input` in an assistant record, in call order.

    ALL of them, not the first: one assistant message can carry several
    parallel tool calls, and taking only the first would strip the later
    publishes of their description and favicon.

    Rejects the calls that published nothing: a missing/blank `file_path` (an
    incomplete streamed call, or one that errored before it had a target) and
    `action: "list"`, which only enumerates existing artifacts. Either would
    otherwise contribute a description to whichever artifact shared its path.
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    inputs = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        if item.get("name") != "Artifact":
            continue
        data = item.get("input")
        if not isinstance(data, dict) or data.get("action") == "list":
            continue
        if not _text(data.get("file_path")):
            continue
        inputs.append(data)
    return inputs


def _parse_transcript(jsonl_path: str) -> list[dict]:
    """One transcript's artifacts, already merged within the session.

    Returns raw records (the `artifact_dict` keyword set minus `exists`) rather
    than finished entries, because this is exactly what gets cached and merged
    again across sessions — see the module docstring.

    Line order is the tiebreaker for "latest" wherever a timestamp is missing,
    which is the right fallback: a transcript is append-only, so later in the
    file IS later in time.
    """
    frames: dict[str, dict] = {}
    # file_path -> (rank, input). The metadata join is per file path within the
    # session; the frame-link is what turns it into a url.
    tool_inputs: dict[str, tuple[int, dict]] = {}
    cwd: str | None = None
    order = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                order += 1
                is_frame = _FRAME_LINK_HINT in line
                is_tool = all(hint in line for hint in _TOOL_USE_HINTS)
                # The cwd probe is the only reason to parse an uninteresting
                # line at all, and only near the top of the file.
                if not is_frame and not is_tool and (
                    cwd is not None or order > _CWD_PROBE_LINES
                ):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # truncated / partially-written line: skip it
                if not isinstance(obj, dict):
                    continue
                if cwd is None:
                    cwd = _text(obj.get("cwd"))
                if is_frame and obj.get("type") == "frame-link":
                    url = _text(obj.get("frameUrl"))
                    path = _text(obj.get("path"))
                    if not url or not path:
                        continue
                    stamp = _epoch(obj.get("timestamp"))
                    rank = (stamp if stamp is not None else -1.0, order)
                    frame = frames.get(url)
                    if frame is None:
                        frames[url] = frame = {"rank": rank, "created_at": stamp,
                                               "updated_at": stamp}
                    elif rank >= frame["rank"]:
                        frame["rank"] = rank
                    else:
                        # An out-of-order record still moves the time span, but
                        # must not overwrite the newer display fields.
                        _widen(frame, stamp)
                        continue
                    frame["path"] = path
                    frame["title"] = _text(obj.get("title"))
                    frame["session_id"] = _text(obj.get("sessionId"))
                    _widen(frame, stamp)
                elif is_tool:
                    for data in _artifact_tool_inputs(obj):
                        path = _text(data.get("file_path"))
                        prior = tool_inputs.get(path)
                        if prior is None:
                            tool_inputs[path] = (order, data)
                        elif order >= prior[0]:
                            # Merged, not replaced: a republish routinely omits
                            # the optional description (it means "unchanged",
                            # not "cleared"), and losing it would strip the
                            # card of its summary after every update.
                            tool_inputs[path] = (order, {**prior[1], **data})
    except OSError:
        return []  # unreadable/vanished transcript: contributes nothing

    records = []
    for url, frame in frames.items():
        _, data = tool_inputs.get(frame["path"], (0, {}))
        records.append({
            "file_path": frame["path"],
            "remote_url": url,
            # The frame-link's title is what the page actually published under;
            # the tool input's is only a fallback for the publish that let the
            # HTML's own <title> supply it and got nothing echoed back.
            "title": frame["title"] or _text(data.get("title")),
            "description": _text(data.get("description")),
            "favicon": _text(data.get("favicon")),
            "session_id": frame["session_id"],
            "cwd": cwd,
            "created_at": frame["created_at"],
            "updated_at": frame["updated_at"],
        })
    return records


def _widen(frame: dict, stamp: float | None) -> None:
    """Grow a frame's [created_at, updated_at] span to include `stamp`. A
    timestamp we couldn't read contributes nothing rather than a zero."""
    if stamp is None:
        return
    if frame["created_at"] is None or stamp < frame["created_at"]:
        frame["created_at"] = stamp
    if frame["updated_at"] is None or stamp > frame["updated_at"]:
        frame["updated_at"] = stamp


def _transcript_artifacts(jsonl_path: str) -> list[dict]:
    """`_parse_transcript` behind the (mtime, size) cache. A transcript that
    cannot even be stat'ed contributes nothing."""
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return []
    key = os.path.abspath(jsonl_path)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    records = _parse_transcript(jsonl_path)
    _CACHE[key] = (st.st_mtime, st.st_size, records)
    return records


def _absorb(merged: dict[str, dict], record: dict) -> None:
    """Fold one transcript's record into the global by-url index.

    Newest publish owns the display fields; the span always widens. This is the
    across-session half of the dedupe — the same page updated from a second
    session is one row, dated from its first publish to its last.

    Owning is not wiping: a display field the newer publish DIDN'T carry
    (an update via the tool's `url` parameter states no description/favicon,
    and its title can fail to join) falls back to the older publish's value.
    Metadata only ever gets more complete; a plain republish can't blank a
    card that used to have a summary and an emoji.
    """
    url = record["remote_url"]
    existing = merged.get(url)
    if existing is None:
        merged[url] = dict(record)
        return
    newer = _is_newer(record.get("updated_at"), existing.get("updated_at"))
    created = _min_stamp(existing.get("created_at"), record.get("created_at"))
    updated = _max_stamp(existing.get("updated_at"), record.get("updated_at"))
    # Whichever publish ends up owning the row, fill its gaps from the loser —
    # transcripts arrive in glob order, not time order, so the older publish
    # can just as well be the SECOND one absorbed.
    loser = existing if newer else record
    if newer:
        merged[url] = dict(record)
    for field in ("title", "description", "favicon"):
        if merged[url][field] is None:
            merged[url][field] = loser[field]
    merged[url]["created_at"] = created
    merged[url]["updated_at"] = updated


def _is_newer(candidate: float | None, current: float | None) -> bool:
    if candidate is None:
        return False
    return current is None or candidate > current


def _min_stamp(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def _max_stamp(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


def list_artifacts() -> list[dict]:
    """Every artifact published from this machine's Claude Code sessions,
    newest update first. [] when there are no transcripts at all — a listing
    degrades to what it could see and never fails."""
    merged: dict[str, dict] = {}
    seen: set[str] = set()
    for jsonl_path in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))):
        seen.add(os.path.abspath(jsonl_path))
        for record in _transcript_artifacts(jsonl_path):
            _absorb(merged, record)
    # pop(), not del: two overlapping listings can both see the same stale key,
    # and the second del would KeyError. FastAPI serves sync routes from a
    # threadpool, so overlapping is normal, not exotic.
    for stale in set(_CACHE) - seen:
        _CACHE.pop(stale, None)
    # Undated artifacts sort last rather than first: `reverse` would otherwise
    # promote the ones we know least about to the top of the page.
    artifacts = [artifact_dict(**record) for record in merged.values()]
    artifacts.sort(
        key=lambda a: (a["updated_at"] is not None, a["updated_at"] or 0.0),
        reverse=True,
    )
    return artifacts
