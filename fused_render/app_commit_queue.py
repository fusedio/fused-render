"""Debounced, serialized commits for app repos — one global worker task.

/api/fs/* mutations no longer commit inline: they mark() the app they touched
and return. A single asyncio task (started with the server, app.py) commits a
dirty app once it has been quiet for _QUIET_S, or unconditionally once it has
been dirty for _MAX_AGE_S (the editor autosaves every 250ms while the user
types, so a pure quiet-period debounce could starve forever).

Why a queue at all: with commits inline in the request handlers, two saves
(or a save racing a Claude turn sweep) run `git add`/`git commit` on the same
repo from two threadpool threads and the loser dies on index.lock — silently,
because commits are best-effort. One worker serializes every git operation
this server issues against app repos, so that contention is gone by
construction, and a burst of keystroke-saves collapses into one meaningful
commit ("Edit index.html" per editing pause) instead of a commit per
autosave tick.

Best-effort like app_git: nothing here may ever fail the mutation that
triggered it. Without a running worker (tests build the app without
lifespan) marks just accumulate until the next flush(). Scoping is
app_git's: mark() resolves the path through app_git.app_dir_for and is a
no-op anywhere outside an app folder.
"""
import asyncio
import logging
import os
import threading
import time

from fused_render import app_git

logger = logging.getLogger(__name__)

_QUIET_S = 1.0    # commit after this much silence
_MAX_AGE_S = 5.0  # ...but never sit on changes longer than this

_lock = threading.Lock()
# app_dir -> {"first": monotonic, "last": monotonic, "labels": [str, ...]}
_pending: dict[str, dict] = {}

_loop: asyncio.AbstractEventLoop | None = None
_wake: asyncio.Event | None = None
_task: asyncio.Task | None = None


def mark(path: str, verb: str) -> None:
    """Record that a successful mutation touched `path`; the worker commits
    the containing app after the debounce (or the next flush() when the
    worker is not running). No-op outside app dirs. Never raises."""
    try:
        app_dir = app_git.app_dir_for(path)
        if app_dir is None:
            return
        label = f"{verb} {os.path.basename(path.rstrip(os.sep))}"
        now = time.monotonic()
        with _lock:
            entry = _pending.get(app_dir)
            if entry is None:
                _pending[app_dir] = {"first": now, "last": now,
                                     "labels": [label]}
            elif label not in entry["labels"]:
                entry["labels"].append(label)
                entry["last"] = now
            else:
                entry["last"] = now
        loop, wake = _loop, _wake
        if loop is not None and wake is not None:
            # mark() runs on threadpool threads (the fs endpoints are sync
            # defs); this is the one loop-safe way to poke the worker.
            loop.call_soon_threadsafe(wake.set)
    except Exception:
        logger.debug("app commit mark failed (%s)", path, exc_info=True)


def _message(labels: list[str]) -> str:
    """One subject line for a burst. Same verb throughout collapses to
    'Edit a.html, b.css'; mixed verbs list the label pairs."""
    if len(labels) == 1:
        return labels[0]
    verbs = {label.split(" ", 1)[0] for label in labels}
    if len(verbs) == 1:
        parts = [label.split(" ", 1)[1] for label in labels]
        prefix = next(iter(verbs)) + " "
    else:
        parts, prefix = labels, ""
    extra = len(parts) - 3
    return (prefix + ", ".join(parts[:3])
            + (f" +{extra} more" if extra > 0 else ""))


def _take_due() -> tuple[list[tuple[str, list[str]]], float | None]:
    """Pop every app whose debounce has expired; also the delay until the
    next one expires (None when nothing is pending)."""
    now = time.monotonic()
    due: list[tuple[str, list[str]]] = []
    delay: float | None = None
    with _lock:
        for app_dir in list(_pending):
            entry = _pending[app_dir]
            wait = min(_QUIET_S - (now - entry["last"]),
                       _MAX_AGE_S - (now - entry["first"]))
            if wait <= 0:
                del _pending[app_dir]
                due.append((app_dir, entry["labels"]))
            else:
                delay = wait if delay is None else min(delay, wait)
    return due, delay


async def _run() -> None:
    while True:
        await _wake.wait()
        while True:
            due, delay = _take_due()
            for app_dir, labels in due:
                # commit() is best-effort and never raises; to_thread keeps
                # the subprocess wait off the event loop.
                await asyncio.to_thread(app_git.commit, app_dir,
                                        _message(labels))
            if delay is None:
                break
            await asyncio.sleep(delay)
        _wake.clear()
        # A mark() between the empty _take_due and the clear would be lost
        # with its wake-up eaten — re-arm if anything slipped in.
        with _lock:
            if _pending:
                _wake.set()


def start() -> None:
    """Start the worker on the running loop (server startup event)."""
    global _loop, _wake, _task
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    _task = _loop.create_task(_run(), name="app-commit-queue")


async def stop() -> None:
    """Stop the worker and flush whatever is still pending (server
    shutdown). New mark() calls fall back to inline commits from here on."""
    global _loop, _wake, _task
    task, _task, _loop, _wake = _task, None, None, None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await asyncio.to_thread(flush)


def flush() -> None:
    """Commit everything pending right now, ignoring the debounce."""
    while True:
        with _lock:
            if not _pending:
                return
            app_dir, entry = _pending.popitem()
        app_git.commit(app_dir, _message(entry["labels"]))
