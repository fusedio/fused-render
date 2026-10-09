"""The topic catalog of the events bus (server/events.py).

Every topic here is one live fact a browser used to poll for. A topic's
`snapshot` IS the matching GET handler's inner function, so a subscribe
answers exactly what the GET would (tests compare the two per topic), and its
producer is one line at the write point that already existed, or one
stat/diff loop per key for state written by another process.

Registered once per process by `register_all()` (server/app.py, at import of
the app factory — the registry is process-global like the routers, and tests
that build several apps register the same topics each time, idempotently).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Hashable

from fused_render import jobs as jobs_mod
from fused_render import tasks_watch
from fused_render.server.events import RETRY, Topic, TopicError, bus, simple_topic
from fused_render.server.watch import _WATCH_REGISTRY, _mtime_or_none


# --------------------------------------------------------------- tasks.listing

class TasksListingTopic(Topic):
    """`GET /api/tasks` as a feed: the snapshot is the listing, the deltas are
    `/api/tasks/changes`' answers with the client's cursor kept server-side.

    Params: `scope` ("app"), `under` (an absolute folder), `page` (what
    `X-Fused-Page` would carry, percent-encoded — a socket cannot set
    headers). The key is the resolved scope folder, so every document asking
    for the same folder shares one snapshot build.

    The producer is `tasks_watch` itself: its `_bump` wakes the bus (the
    listener app.py registers), and the snapshot builder's own floor rebuild
    re-derives truth server-side — the client has no floor read (D3)."""

    name = "tasks.listing"
    kind = "event"
    hidden_ok = True

    def validate(self, params: dict) -> dict:
        from fused_render.server.routers import tasks as tasks_router
        under = str(params.get("under") or "")
        scope = str(params.get("scope") or "")
        page = params.get("page")
        scope_dir, refusal = tasks_router._scope_dir(under, scope, page if isinstance(page, str) and page else None)
        if refusal is not None:
            body = json.loads(bytes(refusal.body) or b"{}")
            raise TopicError(body.get("error") or "refused", refusal.status_code)
        return {"scope_dir": scope_dir, "page": page if isinstance(page, str) else ""}

    def key(self, params: dict) -> Hashable:
        return params.get("scope_dir", "")

    def snapshot(self, params: dict) -> dict:
        from fused_render.server.routers import tasks as tasks_router
        rows, gen = tasks_router._listing()
        scope_dir = params.get("scope_dir", "")
        return {"tasks": tasks_router._scoped(rows, scope_dir) if scope_dir else rows,
                "generation": gen}

    def generation(self, body: dict) -> int | None:
        gen = body.get("generation")
        return gen if isinstance(gen, int) else None

    def delta(self, params: dict, since: int):  # type: ignore[override]
        from fused_render.server.routers import tasks as tasks_router
        gen, keys = tasks_watch.wait(since, timeout=0)
        if keys is None:
            return None  # behind the ring, or a restarted watcher: snapshot
        if gen <= since or not keys:
            return {"generation": gen, "rows": [], "gone": [],
                    "drafts": {"changed": [], "gone": []}}, gen
        answer = tasks_router._changes_answer(since, gen, keys, params.get("scope_dir", ""))
        if int(answer.get("generation", since)) <= since:
            return RETRY  # the snapshot builder has not caught up with `gen`
        return answer, int(answer["generation"])


# -------------------------------------------------------------------- fs.watch

class FsWatchTopic(Topic):
    """The old `/api/fs/events` (SPEC §13.2) as a topic: `{path, mtime}` per
    change, pushed from the coalescing stat registry (server/watch.py), one
    ticker per path however many documents watch it. The snapshot is the
    current mtimes, so a client knows where it stands; a change after that
    arrives as a delta `{"changes": [{path, mtime}]}`. Not `hidden_ok`: a
    reload must fire on a hidden pane too."""

    name = "fs.watch"
    kind = "event"
    hidden_ok = False

    def validate(self, params: dict) -> dict:
        raw = params.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise TopicError("paths: expected a list of absolute paths")
        paths: list[str] = []
        for p in raw:
            if not isinstance(p, str) or not p or not os.path.isabs(p):
                raise TopicError("paths: every entry must be an absolute path")
            if p not in paths:
                paths.append(p)
        return {"paths": sorted(paths)}

    def key(self, params: dict) -> Hashable:
        return tuple(params["paths"])

    def snapshot(self, params: dict) -> dict:
        return {"paths": [{"path": p, "mtime": _mtime_or_none(p)} for p in params["paths"]]}

    async def attach(self, key, params: dict, bus_):
        queue: asyncio.Queue = asyncio.Queue()
        entries = [await _WATCH_REGISTRY.subscribe(p, queue) for p in params["paths"]]

        async def pump():
            while True:
                msg = await queue.get()
                try:
                    change = json.loads(msg)
                except ValueError:
                    continue
                bus_.push_delta(self.name, key, {"changes": [change]})

        task = asyncio.create_task(pump())

        def detach():
            task.cancel()
            for entry in entries:
                _WATCH_REGISTRY.unsubscribe(entry, queue)

        return detach


# ------------------------------------------------------------------------ jobs

class JobsTopic(Topic):
    """`GET /api/jobs` as a feed. `{id}` narrows the snapshot to one row (what
    `fused.watchJob` used to filter client-side out of the whole list).

    Event-sourced from `jobs.upsert/dismiss/forget/clear_finished` (the
    `on_change` listener), plus a slow server-side tick while anyone is
    subscribed, because rows also leave on a CLOCK (`_sweep` ages them out
    on the next write, which may be far off) — one process paying one cheap
    list per interval instead of every window polling at 1 s. Not
    `hidden_ok`: the notifications surface fires on a finished job whether or
    not the window is in front."""

    name = "jobs"
    kind = "event"
    hidden_ok = False
    poll_interval_s = 5.0

    def validate(self, params: dict) -> dict:
        job_id = params.get("id")
        if job_id is not None and not isinstance(job_id, str):
            raise TopicError("id: expected a string")
        return {"id": job_id or ""}

    def key(self, params: dict) -> Hashable:
        return params.get("id", "")

    def snapshot(self, params: dict) -> dict:
        now = time.time()
        rows = jobs_mod.list_jobs(now=now, mark_read=True)
        job_id = params.get("id", "")
        if job_id:
            rows = [r for r in rows if r.get("id") == job_id]
        return {"jobs": rows, "now": now}

    def signature(self, params: dict):
        rows = jobs_mod.list_jobs(now=time.time())
        job_id = params.get("id", "")
        return [(r.get("id"), r.get("state"), r.get("updated_at")) for r in rows
                if not job_id or r.get("id") == job_id]


# ---------------------------------------------------------------- env.progress

class EnvProgressTopic(Topic):
    """`GET /api/env/progress?key=` as a feed, for the runtime's install row:
    the worker writes `progress.json` from another process (it cannot import
    this server), so this is a poll-and-diff producer — ONE reader per key
    per process at the old client cadence, instead of every page that is
    installing reading it at 2 Hz. The snapshot is the GET's body."""

    name = "env.progress"
    kind = "polldiff"
    hidden_ok = False  # an install the user is waiting on must still finish
    poll_interval_s = 0.5
    signature_is_snapshot = True

    def validate(self, params: dict) -> dict:
        from fused_render import envinstall
        key = params.get("key")
        if not isinstance(key, str) or not envinstall.valid_key(key):
            raise TopicError("'key' is not a valid install key (expected 16 lowercase hex "
                             "characters, as returned by /api/run's needs_install)")
        return {"key": key}

    def key(self, params: dict) -> Hashable:
        return params["key"]

    def snapshot(self, params: dict) -> dict:
        from fused_render import envinstall
        try:
            prog = envinstall.progress(params["key"])
        except (ImportError, RuntimeError) as e:
            return {"ok": False, "key": params["key"], "error": str(e)}
        return {"ok": True, "key": params["key"], "progress": prog}


# ------------------------------------------------------------- apps.background

class AppsBackgroundTopic(Topic):
    """`GET /api/apps/background/status?html=` as a feed (runtime.js's
    `fused.daemon.watch`): the start/stop routes publish, and a poll-and-diff
    producer at the old 5 s client cadence covers the pid dying on its own —
    one liveness check per folder per process, for every page watching it."""

    name = "apps.background"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 5.0
    signature_is_snapshot = True

    def validate(self, params: dict) -> dict:
        from fused_render.server.routers import background_apps as bg
        html = params.get("html")
        if not isinstance(html, str) or not html:
            raise TopicError("query must include 'html'")
        folder = bg._folder_for(html)
        if folder is None:
            raise TopicError("query must include 'html'")
        return {"html": html, "folder": folder}

    def key(self, params: dict) -> Hashable:
        return params["folder"]

    def snapshot(self, params: dict) -> dict:
        from fused_render.server.routers import background_apps as bg
        return bg.background_status(params["folder"])


# ---------------------------------------------------------------- registration

_REGISTERED = False


def register_all() -> None:
    """Register every topic and wire the event-sourced producers. Idempotent."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    bus.register(TasksListingTopic())
    bus.register(FsWatchTopic())
    bus.register(JobsTopic())
    bus.register(EnvProgressTopic())
    bus.register(AppsBackgroundTopic())
    tasks_watch.listeners.append(lambda: bus.publish("tasks.listing"))
    jobs_mod.on_change.append(lambda: bus.publish("jobs"))


__all__ = ["register_all", "simple_topic", "TasksListingTopic", "FsWatchTopic", "JobsTopic"]
