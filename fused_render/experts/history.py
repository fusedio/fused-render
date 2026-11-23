"""The home's history: a git repository only the app commits to.

Commits: after every clean chat turn that changed files (`after_turn`, from `agent._poll` through `turn_ended`),
"Chat: <first words>" (inbox/ left out),
or "Update" with the update's answer as the body; a hand edit of memory or skills, "You edited memory", committed
the next time the app looks (`commit_edits`: before an expert session starts and when Changes is listed).
"Changes" is the log of memory.md and the skills. Before any commit the app rewrites `.claude/settings.json` and
drops repository config a session could have planted (`_sanitize_repo`), so no git command of the app runs a command
a session configured. Without git every function is a no-op.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Sequence

from fused_render.experts import chat, notes
from fused_render.experts import home as hm

logger = logging.getLogger(__name__)

#: Identity for app commits when the user has none configured (env only fills it in, never overrides theirs).
_IDENT = ("Fused experts", "experts@fused.invalid")
KNOWLEDGE = ("memory.md", ".claude/skills")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def available() -> bool:
    return shutil.which("git") is not None


def _lock(home: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.realpath(home), threading.Lock())


def _git(home: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", _IDENT[0])
    env.setdefault("GIT_AUTHOR_EMAIL", _IDENT[1])
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    # No hooks, signing or pager: the user's global config must not make an app commit hang. An absolute git, `-C`
    # and close_fds=False keep CPython on posix_spawn (a fork with libproj resident dies; test_git_posix_spawn.py).
    return subprocess.run([shutil.which("git") or "git", "-C", home, "-c", "core.hooksPath=/dev/null",
                           "-c", "commit.gpgsign=false", "-c", "core.pager=cat", "-c", "core.quotepath=false",
                           "-c", "core.fsmonitor=false", *args],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30, check=check, close_fds=False)


def init(home: str) -> None:
    if not available():
        return
    try:
        _git(home, "init", "-q", check=True)
    except (OSError, subprocess.SubprocessError):
        logger.warning("experts: git init failed in %s", home, exc_info=True)
        return
    commit(home, "Created")


#: `core.*` keys `git init` writes (any platform); any other key in the home's `.git/config` was not the app's.
_CORE_KEYS = ("repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode",
              "symlinks")


def _sanitize_repo(home: str) -> None:
    """Drop repository config a session could have planted (a clean filter, an fsmonitor, an external diff all run
    commands inside the app's own git calls): `.git/config` keeps only the `core` keys git init writes, and
    `.git/info/attributes` goes. Reading a config file with `git config --file` runs nothing."""
    cfg = os.path.join(home, ".git", "config")
    res = _git(home, "config", "--file", cfg, "--list")
    keep, other = [], res.returncode != 0
    for ln in res.stdout.splitlines():
        key, _, val = ln.partition("=")
        sect, _, name = key.partition(".")
        if sect == "core" and name in _CORE_KEYS:
            keep.append((name, val))
        else:
            other = True
    if other:
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("[core]\n" + "".join(f"\t{k} = {v}\n" for k, v in keep or [("bare", "false")]))
    attrs = os.path.join(home, ".git", "info", "attributes")
    if os.path.lexists(attrs):
        os.remove(attrs)


def _ready(home: str) -> bool:
    """Whether the app may run git in the home: it is a repository of its own (never an enclosing one's work
    tree), with its config cleaned first. For the calls outside `commit` that can run a configured command (a
    checkout's smudge filter, a status's clean filter)."""
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return False
    with _lock(home):
        _sanitize_repo(home)
    return True


def commit(home: str, message: str, only: Sequence[str] = (".",)) -> None:
    """Commit what changed in the home under `only` (paths relative to it). Never raises."""
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return
    try:
        with _lock(home):
            hm.ensure_settings(home)
            _sanitize_repo(home)
            _git(home, "add", "-A", "--", *only, check=True)
            if _git(home, "diff", "--cached", "--quiet").returncode:
                _git(home, "commit", "-q", "-m", message, check=True)
    except (OSError, subprocess.SubprocessError):
        logger.warning("experts: commit failed in %s", home, exc_info=True)


def commit_edits(home: str) -> None:
    """Commit what changed in memory or skills since the last commit, unless a session is open in the home (a turn
    that just ended is committed by its run's next poll, `after_turn`; an idle session ends about 30 s after its
    turn). A change no newer than the main chat's transcript is that chat's turn, which no page polled: it gets the
    turn's label. A newer one is the user's hand edit."""
    from fused_render.claude_agent import agent_module

    if next(agent_module()._live_runs_in(home, idle=True), None) is not None or not _ready(home):
        return
    dirty = _git(home, "status", "--porcelain", "-z", "--untracked-files=all", "--", *KNOWLEDGE).stdout.split("\0")
    edited = max((os.path.getmtime(os.path.join(home, ln[3:])) for ln in dirty
                  if ln[3:] and os.path.exists(os.path.join(home, ln[3:]))), default=0.0)
    main = chat.load(home)["main"]
    try:
        by_chat = bool(main) and edited and os.path.getmtime(chat.transcript(home, main)) >= edited
    except OSError:
        by_chat = False
    if by_chat:
        after_turn(home, main)
    commit(home, "You edited memory", KNOWLEDGE)


def restore(home: str) -> None:
    """Memory, its archive and skills back to the last commit (a failed update's edits)."""
    if _ready(home):
        paths = (*KNOWLEDGE, "memory-archive.md")
        for path in paths:  # one at a time: a path HEAD lacks fails the whole checkout
            _git(home, "checkout", "HEAD", "--", path)
        _git(home, "clean", "-fdq", "--", *paths)


def turn_ended(target: str, session_id: str) -> bool:
    """`agent._poll`'s one call after a clean turn of any session, with the switch on. A turn in a home is committed
    here (True: the agent commits nothing). A conversation that involves an expert (a task recorded by its tool
    call, a breakout, the main chat; never a learning run) gets its inbox pointer back if a run removed it."""
    from fused_render.experts import flag, paths, store, update

    if not flag.on():
        return False
    spec = store.get(paths.spec_of_path(target) or "")
    if spec is None:
        for s in store.all():
            if session_id in chat.load(s.home)["sources"]:
                update.point(s, session_id, target, "task")
        return False
    st = chat.load(spec.home)
    if not (st["update"] and st["update"]["session"] == session_id):
        update.point(spec, session_id, spec.home, "chat" if session_id == st["main"] else "breakout")
    after_turn(spec.home, session_id)
    return True


def after_turn(home: str, session_id: str) -> None:
    """A clean turn of a session in the home ended. A learning run's turn moves the inbox files it was given to
    `inbox-archive/`, moves
    each listed conversation's start line to where the run began, and commits "Update" with its reply as the body;
    any other commits "Chat: <first words of the prompt>"."""
    msgs, _ = chat.messages(chat.transcript(home, session_id))
    up = chat.load(home)["update"]
    if up and up["session"] == session_id:
        os.makedirs(os.path.join(home, "inbox-archive"), exist_ok=True)
        for name in up["files"]:  # kept, not deleted: nothing reads them
            try:
                os.replace(os.path.join(home, "inbox", name), os.path.join(home, "inbox-archive", name))
            except OSError:
                pass

        def fn(d):
            for sid, rec in d["sources"].items():
                if rec["file"] in up["files"]:
                    rec.update(line=up["lines"].get(sid, rec["line"]), file="")
            d["update"] = None
        chat.change(home, fn)
        # Its final message only: earlier text blocks are its working notes.
        answer = next((m["text"] for m in reversed(msgs) if m["role"] == "assistant"), "")
        commit(home, "Update\n\n" + answer if answer else "Update")
        return
    users = [m for m in msgs if m["role"] == "user"]
    if users:
        # inbox/ is left for the run's "Notes waiting" commit.
        commit(home, "Chat: " + chat.words(users[-1]["text"]), (".", ":(exclude)inbox"))


def changes(home: str) -> list[dict]:
    """`[{sha, at, label, read, changed}]`, newest first: every learning run ("Update"; `read` counts the inbox
    entries it moved to the archive) and every other commit that changed memory or skills, the first commit excepted."""
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return []
    rows = []
    log = _git(home, "log", "--no-renames", "--format=%x1e%H%x1f%ct%x1f%P%x1f%s", "--name-status").stdout
    for block in log.split("\x1e")[1:]:
        head, _, files = block.partition("\n")
        sha, at, parents, subject = head.split("\x1f")
        names = [ln.split("\t") for ln in files.splitlines() if "\t" in ln]
        changed = any(p.startswith(KNOWLEDGE) for _, *ps in names for p in ps)
        if parents and (changed or subject == "Update"):
            rows.append({"sha": sha, "at": float(at), "label": subject, "changed": changed,
                         "read": sum(st == "D" and ps[0].startswith("inbox/") for st, *ps in names)})
    return rows


def learned(home: str, sha: str) -> dict:
    """For a learning run's commit, `{reply, read: [{kind, title}]}`: its reply (the commit body) and the inbox
    entries it removed, titled from their first line in the parent commit. `{}` for any other commit."""
    subject, _, reply = _git(home, "show", "-s", "--format=%s%x1f%b", sha).stdout.partition("\x1f")
    if subject != "Update":
        return {}
    gone = _git(home, "show", "--no-renames", "--format=", "--name-only", "--diff-filter=D", sha, "--",
                "inbox").stdout.split()
    return {"reply": reply.strip(), "read": [notes.parse(_git(home, "show", f"{sha}^:{n}").stdout) for n in gone]}


def diff(home: str, sha: str) -> str | None:
    """That commit's hunks over memory and skills (no file headers), or None for anything that is not one of the
    home's commits."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or "") or not _ready(home):
        return None
    res = _git(home, "show", "--format=", "--no-color", "--no-ext-diff", "--no-textconv", sha, "--", *KNOWLEDGE)
    if res.returncode:
        return None
    out, hunk = [], False
    for ln in res.stdout.splitlines(keepends=True):
        hunk = ln.startswith("@@") or (hunk and not ln.startswith("diff --git"))
        if hunk:
            out.append(ln)
    return "".join(out)
