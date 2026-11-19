"""The home's history: a git repository only the app commits to.

Commits: after every clean chat turn that changed files (`after_turn`, from `agent._poll` through `turn_ended`),
"Chat: <first words>" (inbox/ left out),
or "Update" with the update's answer as the body; a hand edit of memory or skills, "You edited memory", committed
the next time the app looks (`commit_edits`: before a specialist session starts and when Changes is listed).
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

from fused_render.specialists import chat
from fused_render.specialists import home as hm

logger = logging.getLogger(__name__)

#: Identity for app commits when the user has none configured (env only fills it in, never overrides theirs).
_IDENT = ("Fused specialists", "specialists@fused.invalid")
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
        logger.warning("specialists: git init failed in %s", home, exc_info=True)
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
        logger.warning("specialists: commit failed in %s", home, exc_info=True)


def commit_edits(home: str) -> None:
    """Commit what changed in memory or skills since the last commit, unless a session is open in the home (a turn
    that just ended is committed by its run's next poll, `after_turn`; an idle session ends about 30 s after its
    turn). A change no newer than the main chat's transcript is that chat's turn, which no page polled: it gets the
    turn's label. A newer one is the user's hand edit."""
    from fused_render.claude_agent import agent_module

    if not available() or next(agent_module()._live_runs_in(home, idle=True), None) is not None:
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
    """Memory and skills back to the last commit (a failed update's edits)."""
    if available():
        for path in KNOWLEDGE:  # one at a time: a path HEAD lacks fails the whole checkout
            _git(home, "checkout", "HEAD", "--", path)
        _git(home, "clean", "-fdq", "--", *KNOWLEDGE)


def turn_ended(target: str, session_id: str) -> bool:
    """`agent._poll`'s one call after a clean turn of any session, with the switch on. A turn in a home is committed
    here (True: the agent commits nothing); a breakout's turn, and a turn of a task that used a specialist, also go to
    the inbox."""
    from fused_render.specialists import flag, paths, store, update

    if not flag.on():
        return False
    spec = store.get(paths.spec_of_path(target) or "")
    if spec is None:
        for s in store.all():
            if session_id in chat.load(s.home)["tasks"]:
                update.gather(s, session_id, "tasks")
        return False
    if session_id in chat.load(spec.home)["breakouts"]:
        update.gather(spec, session_id, "breakouts")
    after_turn(spec.home, session_id)
    return True


def after_turn(home: str, session_id: str) -> None:
    """A clean turn of a session in the home ended. An update turn removes the inbox files it was given and
    commits "Update" with its answer as the body; any other commits "Chat: <first words of the prompt>"."""
    msgs, _ = chat.messages(chat.transcript(home, session_id))
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    if not users:
        return
    prompt = msgs[users[-1]]["text"]
    if prompt.startswith(chat.UPDATE_TAG):
        answer = "\n\n".join(m["text"] for m in msgs[users[-1] + 1:])
        for name in chat.load(home)["update_files"]:
            try:
                os.remove(os.path.join(home, "inbox", name))
            except OSError:
                pass
        chat.change(home, lambda d: d.update(update_files=[]))
        commit(home, "Update\n\n" + answer if answer else "Update")
        return
    # inbox/ is left for the update's "Notes waiting" commit.
    commit(home, "Chat: " + chat.words(prompt), (".", ":(exclude)inbox"))


def changes(home: str) -> list[dict]:
    """`[{sha, at, label}]`, newest first: the commits that changed memory or skills, the first one excepted."""
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return []
    out = _git(home, "log", "--format=%H%x1f%ct%x1f%P%x1f%s", "--", *KNOWLEDGE).stdout
    rows = [ln.split("\x1f") for ln in out.splitlines()]
    return [{"sha": sha, "at": float(at), "label": subject} for sha, at, parents, subject in rows if parents]


def diff(home: str, sha: str) -> str | None:
    """That commit's hunks over memory and skills (no file headers), or None for anything that is not one of the
    home's commits."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or "") or not available():
        return None
    res = _git(home, "show", "--format=", "--no-color", "--no-ext-diff", sha, "--", *KNOWLEDGE)
    if res.returncode:
        return None
    out, hunk = [], False
    for ln in res.stdout.splitlines(keepends=True):
        hunk = ln.startswith("@@") or (hunk and not ln.startswith("diff --git"))
        if hunk:
            out.append(ln)
    return "".join(out)
