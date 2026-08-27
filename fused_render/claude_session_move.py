"""Carry an app folder's Claude sessions along when the folder is moved.

Claude Code keys its session store by working directory: a session run in
`/A/app` lives at `~/.claude/projects/-A-app/<id>.jsonl` (every non-alphanumeric
character of the cwd becomes `-`), and `claude --resume <id>` looks only in the
bucket for the cwd it is started from. Move `/A/app` to `/B/app` and every
conversation about the app is still on disk and unreachable from the app: the
Tasks list is empty, "continue in terminal" finds nothing, resume fails.

`relocate(old_root, new_root)` moves every transcript whose recorded cwd is
`old_root` or a directory under it into the bucket for the corresponding path
under `new_root`, rewriting the `cwd` field on the way so the readers that
trust it (routers/claude_sessions.py, the Home row) see the new path, and
carries each transcript's side directory (`<id>/`, tool results) with it. The
caller is `app_fused_dir._ensure_meta`, at the one moment the move is known:
the `.fused/meta.json` witness disagreeing with the live path (D548, SPEC §47).

What decides membership is the TRANSCRIPT'S OWN `cwd` LINE, never the bucket
name. The bucket name is a lossy encoding — `-A-app-` prefixes the bucket for
`/A/app-old` (a sibling that did not move) as well as `/A/app/sub` — and one
bucket can hold transcripts from two cwds that collide under the munge. So the
bucket names are only used to shortlist directories worth opening; each file
is then read for its first `cwd` and moved only when that cwd is under
`old_root`. A transcript with no `cwd` line at all is not a match and not a
blocker: it belongs to nothing we can name.

A transcript is left where it is when the session is RUNNING — a process is
appending to that path and moving it from under it would split the
conversation. Running is either a live entry in `~/.claude/sessions/` (pid
checked, those files go stale) or `session_liveness.transcript_turn_open` saying
a turn is in flight. Those count as `pending`, and `complete` is False, so the
caller leaves the witness in place and the next open tries again.

When the destination already has a transcript of the same id, the destination
wins and the source is left alone (not pending): a copy-on-resume has already
carried it, and the live continuation is the one at the new path. A source that
vanishes mid-run (a concurrent relocate got there first) is done, not failed.

Deliberately NOT touched: the `projects` map in `~/.claude.json` (the CLI owns
that file — claude_config/mcp.py), `~/.claude/history.jsonl` (cosmetic prompt
history), and `file-history/` / `todos/` (keyed by session id, so a cwd move
does not affect them). A bucket's `memory/` folder and any other unknown entry
move only when the destination has no file of that name; nothing is ever
overwritten.

Best-effort, like everything on the render path: nothing here raises. The
roots are module attributes resolved at call time so tests can point them at a
temp tree instead of the machine's real store.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time

from fused_render import session_liveness

logger = logging.getLogger(__name__)

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
#: Claude Code's registry of live interactive sessions: one `<pid>.json` per
#: process with `pid`, `sessionId` and `cwd`. Entries outlive their process.
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")

#: How far into a transcript to look for its `cwd` before giving up. The field
#: is on the first user/assistant record, which sits behind a handful of
#: housekeeping lines (mode, permission-mode, file-history-snapshot).
_HEAD_LINES = 64


def munge(path: str) -> str:
    """A cwd's bucket name under `~/.claude/projects` — Claude Code's own rule,
    the same one `templates/claude/agent.py._munge` uses."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _under(path: str, root: str) -> str | None:
    """`path` relative to `root` ("" for root itself), or None when `path` is
    not `root` and not inside it. A directory boundary, not a string prefix:
    `/A/app-old` is not under `/A/app`."""
    p, r = _norm(path), _norm(root)
    if p == r:
        return ""
    if p.startswith(r.rstrip(os.sep) + os.sep):
        return os.path.abspath(path)[len(os.path.abspath(root).rstrip(os.sep)) + 1:]
    return None


def transcript_cwd(path: str) -> str | None:
    """The first `cwd` a transcript records, or None if none in the head."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(_HEAD_LINES):
                line = f.readline()
                if not line:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return None


def _live_session_ids(sessions_dir: str) -> set:
    """Session ids with a registry entry whose process is still alive."""
    live = set()
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return live
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, name), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        pid, sid = entry.get("pid"), entry.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass  # alive but not ours (EPERM) — still alive
        live.add(sid)
    return live


def _rewrite_cwd(src: str, dst: str, old_root: str, new_root: str) -> None:
    """Copy `src` to `dst`, repointing every `cwd` under `old_root`.

    Only a line that changes is re-serialised; everything else — unparseable
    lines included — is copied byte for byte, so a transcript that did not
    need rewriting is not reformatted on the way past.
    """
    tmp = dst + ".tmp"
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        for raw in fin:
            if b'"cwd"' in raw:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    obj = None
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                rel = _under(cwd, old_root) if isinstance(cwd, str) else None
                if rel is not None:
                    obj["cwd"] = os.path.join(new_root, rel) if rel else new_root
                    raw = json.dumps(obj, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8") + b"\n"
            fout.write(raw)
    os.replace(tmp, dst)


def _move_aside(src: str, dst: str) -> bool:
    """Move a side entry (a transcript's `<id>/` dir, `memory/`, anything
    else in a bucket) unless the destination already has one. True if `src`
    is gone afterwards."""
    if not os.path.exists(src):
        return True
    if os.path.exists(dst):
        return False
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(src, dst)
    return True


def _candidate_buckets(projects_dir: str, old_root: str) -> list:
    """Bucket dirs that COULD hold a transcript from under `old_root` — the
    exact bucket and every one its munge prefixes. A shortlist only; the
    transcripts inside decide (module docstring)."""
    stem = munge(old_root)
    try:
        names = os.listdir(projects_dir)
    except OSError:
        return []
    return [os.path.join(projects_dir, n) for n in names
            if n == stem or n.startswith(stem + "-")]


def relocate(old_root: str, new_root: str, *, projects_dir: str | None = None,
             sessions_dir: str | None = None) -> dict:
    """Move every Claude session recorded under `old_root` to its place under
    `new_root`. Returns `{"moved": [ids], "pending": [ids], "complete": bool}`;
    `complete` is False only when a transcript that belongs to the app could
    not be moved (running, or an OS error) and should be retried later.
    """
    projects_dir = projects_dir or PROJECTS_DIR
    sessions_dir = sessions_dir or SESSIONS_DIR
    moved, pending = [], []
    live = _live_session_ids(sessions_dir)
    now = time.time()
    for bucket in _candidate_buckets(projects_dir, old_root):
        try:
            names = sorted(os.listdir(bucket))
        except OSError:
            continue
        targets = {}
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            src = os.path.join(bucket, name)
            cwd = transcript_cwd(src)
            rel = _under(cwd, old_root) if cwd else None
            if rel is None:
                continue  # another cwd's transcript sharing the bucket, or no cwd
            sid = name[:-len(".jsonl")]
            new_cwd = os.path.join(new_root, rel) if rel else new_root
            target = os.path.join(projects_dir, munge(new_cwd))
            targets[target] = new_cwd
            dst = os.path.join(target, name)
            if sid in live:
                pending.append(sid)
                logger.info("session %s is live; leaving its transcript at %s", sid, src)
                continue
            try:
                if session_liveness.transcript_turn_open(src, now):
                    pending.append(sid)
                    logger.info("session %s is mid-turn; leaving %s", sid, src)
                    continue
                if os.path.exists(dst):
                    # A copy-on-resume already carried it; the continuation
                    # at the new path is the live one.
                    logger.info("session %s already at %s; source left alone", sid, dst)
                    continue
                os.makedirs(target, exist_ok=True)
                _rewrite_cwd(src, dst, old_root, new_root)
                os.remove(src)
                _move_aside(os.path.join(bucket, sid), os.path.join(target, sid))
                moved.append(sid)
            except FileNotFoundError:
                continue  # a concurrent relocate got there first
            except OSError:
                pending.append(sid)
                logger.debug("moving session %s failed", sid, exc_info=True)
        # Whatever else the bucket held (memory/, unknown files) follows the
        # transcripts when they all went to one place and NO transcript is
        # left behind — one still there (live, or another cwd's under the
        # same munge) may own that memory too, so it stays with it.
        if len(targets) == 1 and not pending:
            target = next(iter(targets))
            try:
                left = os.listdir(bucket)
                if any(n.endswith(".jsonl") for n in left):
                    continue
                for name in left:
                    if name.endswith(".tmp"):
                        continue
                    _move_aside(os.path.join(bucket, name), os.path.join(target, name))
                if not os.listdir(bucket):
                    os.rmdir(bucket)
            except OSError:
                logger.debug("tidying bucket %s failed", bucket, exc_info=True)
    return {"moved": moved, "pending": pending, "complete": not pending}
