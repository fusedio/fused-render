"""runPython target for claude/template.html's Artifacts section: the pages
Claude PUBLISHED while working on this target.

An artifact is the one thing a chat produces that outlives the chat and does not
live on disk — a hosted page on claude.ai. The transcript records it and then
scrolls away, so a folder someone has published five pages from looks, on the
landing screen, exactly like one that has published none. This module is what
the landing screen asks so it can say otherwise, and what the live chat asks so
a page announces itself the moment it is published rather than at the next
reload.

TWO readers, because the two questions are genuinely different and only one of
them is cheap:

* **`list`** — the artifacts published while working on THIS target, across
  every session, whose local source is still on disk. The cross-session scan
  (every transcript under ~/.claude/projects, plus a per-url merge of
  redeploys) is the SERVER's job and already done there
  (`/api/claude-artifacts`, cached): this action is an HTTP call to it, scoped
  by `cwd`, and then two filters the server cannot apply because they are about
  the TARGET rather than the directory — see `_list`. It deliberately does not
  reimplement the scan: a second implementation of "which artifacts exist" is
  how the two grow apart, and the server's is the one with the cache, the
  `exists` probe and the description/favicon join.
* **`live`** — the artifacts in ONE session's transcript, read straight off
  disk. Called on a timer while a run streams, which is exactly when the server
  cannot help: its answer is cached, and the frame-link that a publish just
  appended is a single line in a file whose name we already know. One `open()`
  of one transcript is cheaper than a cache invalidation would be, and it is
  never stale by construction.

The server is reached the sanctioned way — `../shared/appenv.origin()`, i.e. the
`FUSED_RENDER_ORIGIN` the app exports — because a template may not import
`fused_render` (SPEC PY-15). Stdlib otherwise.

NOTHING HERE RAISES. Both actions are decoration on a screen whose subject is a
conversation: an unset origin, a refused connection, a transcript that was
rotated away are all ordinary, and the honest answer to every one of them is an
empty list (plus `error` for the console) rather than the red traceback overlay
over a working chat.

Actions:
  main(action="list", file=...)                  -> {"artifacts": [...]}
      newest first, the server's own order. Entries carry file_path, exists
      (always true — see `_list`), remote_url, title, description, favicon,
      session_id, cwd, created_at, updated_at.
  main(action="live", session_id=..., file=...)  -> {"artifacts": [...]}
      oldest first (publish order), one session only. Entries carry
      remote_url, title, file_path, favicon, timestamp.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op. (Same
# preamble as agent.py, for the same reason.)
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "artifacts.py")

HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.join(os.path.dirname(HERE), "shared")
# Guarded insert: /api/run may exec this module repeatedly in one worker.
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
from appenv import origin as _origin

# Claude Code's own data dir, and it must be the SAME one the CLI writes to or
# `live` reads a transcript that does not exist. CLAUDE_CONFIG_DIR wins where
# the user set it — see agent.py's CLAUDE_DIR, which this mirrors deliberately
# rather than importing: `live` is the only reader here and one env lookup is
# cheaper than making this module depend on the whole agent.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")

# The listing is a landing-screen decoration behind a localhost hop. Long enough
# that a cold scan of a big ~/.claude/projects still lands, short enough that a
# server which is not answering does not hold the section's spinner for a screen
# the user is already typing into.
_TIMEOUT = 5.0

# Substring screens applied to a raw line BEFORE json.loads — a transcript is
# megabytes of tool output and assistant prose, and parsing all of it is the
# whole cost of this read. The same two screens the server's scanner uses, for
# the same two records: the `frame-link` is the publish that landed, and the
# Artifact `tool_use` beside it is the only place the favicon the author chose
# ever appears (the frame-link does not echo it back).
_FRAME_LINK_HINT = "frame-link"
_TOOL_USE_HINTS = ("Artifact", "tool_use")


def _workdir(file: str) -> str:
    """Claude's cwd for a target: a directory target IS the working directory, a
    file target's is its parent.

    The SAME rule as agent.py's `_workdir`, replicated rather than imported.
    Importing it would drag agent.py's whole module body (subprocess discovery,
    run-dir creation at import time) into a call that only wants a path, and a
    template may not add a sibling to sys.path just to borrow four words. The
    rule is one line and it is pinned by the thing that matters: the `cwd` the
    server stores comes from claude's own report of where it ran, so a drift
    here shows up immediately as an empty section, not as wrong data.
    """
    return file if os.path.isdir(file) else os.path.dirname(file)


def _munge(path: str) -> str:
    """A cwd's project-dir name under ~/.claude/projects: every non-alphanumeric
    char becomes '-' (claude-code's own rule — see agent.py `_munge`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _bad_id(value: str) -> bool:
    """Whether a session id from the page is unsafe to join into a path. It
    arrives as a URL param and gets joined onto a directory we own, so it may
    carry no separator and no leading dot — and on Windows a drive prefix
    ("d:x") makes os.path.join drop our directory entirely."""
    return not value or value.startswith(".") or any(c in value for c in "/\\:")


def _text(value):
    """A non-empty string, or None. Guards against a record whose field is
    present but null/numeric — the page renders these straight into the DOM."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ------------------------------------------------------------------ list

def _list(file: str) -> dict:
    """The artifacts published while working on THIS target, newest first.

    The server answers per DIRECTORY (`cwd`), which is one step coarser than
    what the screen means, so two filters run on top of its answer:

    ONE, no artifacts whose local source is KNOWN gone (`exists` False). A page
    published from a scratchpad that has since been cleaned up is still live at
    its url, but it is not something this target has any hold on any more, and
    a landing screen listing four dead scratchpads above three real pages reads
    as noise. `exists` None (a mount-backed path the server refuses to stat) is
    not "gone" — those rows stay and open their hosted page.

    The per-target session scoping this used to add on top (which chats were
    started on THIS file vs its folder) went with the per-file sidecar (D357):
    `cwd` is the only record left, so a file and its folder now show the same
    cwd-scoped list.
    """
    workdir = _workdir(file)
    if not workdir:
        return {"artifacts": [], "error": "no target"}
    base = _origin()
    if not base:
        # Nothing published this page's server location, so there is nowhere to
        # ask. Stated rather than swallowed: it means "the app did not export
        # FUSED_RENDER_ORIGIN", which is a deployment fact worth seeing in the
        # console, not an empty history.
        return {"artifacts": [], "error": "FUSED_RENDER_ORIGIN is not set"}
    url = (base.rstrip("/") + "/api/claude-artifacts?cwd="
           + urllib.parse.quote(workdir, safe=""))
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # A server that is starting, restarting or gone. The section simply has
        # nothing to show; the message is for the console.
        return {"artifacts": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return {"artifacts": [], "error": "unexpected response"}
    # Drop only the KNOWN-gone (exists is False). None means the server did
    # not stat the path (mount-backed, hang-avoidance) — those rows stay, and
    # the page opens their hosted url instead of claiming a local file.
    artifacts = [a for a in artifacts
                 if isinstance(a, dict) and a.get("exists") is not False]
    return {"artifacts": artifacts}


# ------------------------------------------------------------------ live

def _favicons(obj, into: dict) -> None:
    """Record `file_path -> favicon` for every Artifact tool call in an assistant
    record. ALL of them, not the first: one message can carry several parallel
    publishes. `action: "list"` and a call with no target published nothing and
    would otherwise donate an emoji to whichever artifact shared its path.

    A LATER call overwrites an earlier one for the same path, but only when it
    states a favicon — a republish routinely omits the optional field, and that
    means "unchanged", not "cleared"."""
    message = obj.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        if item.get("name") != "Artifact":
            continue
        data = item.get("input")
        if not isinstance(data, dict) or data.get("action") == "list":
            continue
        target = _text(data.get("file_path"))
        icon = _text(data.get("favicon"))
        if target and icon:
            into[target] = icon


def _live(session_id: str, file: str, cwd: str = "") -> dict:
    """The artifacts in ONE session's transcript, in publish order.

    `cwd` is an override for the caller that already knows it; otherwise it is
    derived from the target by the same rule everything else here uses, so the
    page never has to do path arithmetic to ask this question.

    Redeploys collapse by `frameUrl` — a page republished four times is ONE
    artifact — and the LAST record wins the display fields, because the last
    publish is what is at that url now. Identity is the url and not the file
    path for the same reason the server does it that way: two different local
    files can be published to the same page, and one file republished under a
    new url is a new page.

    The favicon join is per file path within this one session, which is all the
    strip needs: the emoji is stated by the tool call that published, and both
    records are in the same transcript a few lines apart.
    """
    if _bad_id(session_id):
        return {"artifacts": []}
    ground = cwd or _workdir(file)
    if not ground:
        return {"artifacts": []}
    path = os.path.join(PROJECTS, _munge(ground), session_id + ".jsonl")

    found = {}
    icons = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                is_frame = _FRAME_LINK_HINT in line
                is_tool = all(hint in line for hint in _TOOL_USE_HINTS)
                if not is_frame and not is_tool:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # a partially-written tail line: skip it
                if not isinstance(obj, dict):
                    continue
                if is_tool:
                    _favicons(obj, icons)
                if not is_frame or obj.get("type") != "frame-link":
                    continue
                url = _text(obj.get("frameUrl"))
                if not url:
                    continue
                # dict order IS publish order: a transcript is append-only, so
                # first-seen is first-published, and re-assigning an existing key
                # updates the value without moving it. That keeps a republished
                # page where the user first saw it in the strip instead of
                # jumping it to the end mid-run.
                found[url] = {
                    "remote_url": url,
                    "title": _text(obj.get("title")),
                    "file_path": _text(obj.get("path")),
                    "timestamp": _text(obj.get("timestamp")),
                }
    except OSError:
        return {"artifacts": []}  # no transcript yet, or unreadable

    # Joined at the end, not inline: the tool call can be written EITHER side of
    # its frame-link (the publish is what the call produces, but a streamed
    # assistant record can be flushed after it), so the emoji is only reliably
    # known once the whole file has been read.
    for record in found.values():
        record["favicon"] = icons.get(record["file_path"])
    return {"artifacts": list(found.values())}


def main(action: str = "list", file: str = "", session_id: str = "",
         cwd: str = "") -> dict:
    if action == "list":
        return _list(file)
    if action == "live":
        return _live(session_id, file, cwd)
    return {"artifacts": [], "error": "unknown action: %s" % action}
