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
from fused_render.server.events import RETRY, Topic, TopicError, bus, canonical, simple_topic
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
    # The old idle cadence: frontend platform/lib/jobs.ts POLL_IDLE_MS (5 s),
    # DownloadManager's `useJobs` — the floor under which a row ageing out
    # was noticed before. (The 1 s busy cadence needs no server tick: every
    # report is a write, and every write publishes.)
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
    # The old client cadence (static/runtime.js `startInstall`'s poll, the
    # INSTALL_POLL_FAST_MS / INSTALL_POLL_MS pair): 100 ms for the first
    # second of the wait — a ~540 ms install must not sit out a 500 ms grid —
    # then 500 ms. Measured from the first subscriber, as the page measured
    # from its first poll.
    poll_interval_s = 0.5
    FAST_S = 0.1
    FAST_WINDOW_S = 1.0
    signature_is_snapshot = True

    def poll_interval(self, params, age_s, last):
        return self.FAST_S if age_s < self.FAST_WINDOW_S else self.poll_interval_s

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
    # The old client cadence: static/runtime.js `daemonWatch`'s POLL_MS (5 s).
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


# ======================================================================= phase 1
#
# The shell's ambient polls. Each topic's snapshot is the GET handler itself
# (they take no request-bound arguments), its producer the write point the
# design names — plus, where the design marks the topic poll-diff as well, a
# server-side tick at EXACTLY the old client cadence (D16), one per process.


def _call_handler(fn):
    """A route handler with no request-bound parameters, called directly: the
    body it returns IS the snapshot, so the two can never disagree."""
    def snapshot(params: dict) -> dict:
        body = fn()
        if not isinstance(body, dict):
            # A JSONResponse refusal (Windows' 501 for the terminal list).
            try:
                return json.loads(bytes(body.body) or b"{}")
            except Exception:  # noqa: BLE001
                return {}
        return body
    return snapshot


class ScheduleTopic(Topic):
    """`GET /api/schedule` (shell/Scheduled.tsx, the chat's useSchedule).
    Write-triggered by `schedule._write` (this process) and poll-diff for
    another process's scheduler at the old cadence: Scheduled.tsx POLL_MS
    (20 s) — the chat's own 15 s loop (sched/scheduled.ts) was the faster of
    the two readers, so the tick is 15 s."""
    name = "schedule"
    kind = "write"
    hidden_ok = True
    # Old client cadences: shell/Scheduled.tsx POLL_MS = 20000 and
    # apps/claude/sched/scheduled.ts (15 s): the faster one wins so no reader
    # gets staler than it was.
    poll_interval_s = 15.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import schedule as r
        return r.api_schedule()


class ScheduleQueueTopic(Topic):
    """`GET /api/schedule/queue` — the same write point and the same old
    cadence (shell/Scheduled.tsx POLL_MS, 20 s)."""
    name = "schedule.queue"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 20.0  # old cadence: shell/Scheduled.tsx POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import schedule as r
        return r.api_schedule_queue()


class ScheduleEventsTopic(Topic):
    """`GET /api/schedule/events`: the narrator's ring, per process as today
    (D16). Write-triggered by `schedule._emit` and the ack route. Not
    `hidden_ok`: a toast must fire for a window nobody is looking at."""
    name = "schedule.events"
    kind = "write"
    hidden_ok = False

    def snapshot(self, params):
        from fused_render.server.routers import schedule as r
        return r.api_schedule_events()


class UpdateTopic(Topic):
    """`GET /api/config`'s `update` field (platform/lib/update-status.ts).
    Write-triggered by every `UpdateManager._state` transition (a property
    setter), and poll-diff at the OLD client cadence for the fields that move
    without a state change (progress during an install): update-status.ts
    `pollDelay` — 2 s while installing/checking, 2 s for the first 20 s of a
    reader's life while idle, 15 s until 120 s, then 60 s."""
    name = "update"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 60.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render import update as self_update
        manager = self_update.manager()
        return {"update": manager.status() if manager is not None else None}

    def poll_interval(self, params, age_s, last):
        status = (last or {}).get("update") if isinstance(last, dict) else None
        state = (status or {}).get("state") if isinstance(status, dict) else None
        if state in ("installing", "checking"):
            return 2.0
        if status is not None and state == "idle":
            if age_s < 20.0:
                return 2.0
            if age_s < 120.0:
                return 15.0
        return 60.0


class AiRuntimeTopic(Topic):
    """`GET /api/ai/runtime` (apps/ai_models/lib/aiRuntime.ts). Write-triggered
    by the supervisor's load/unload/_report, and poll-diff for worker health
    at the old cadence: aiRuntime.ts ACTIVE_MS (1 s) while busy — anything
    downloading or a model not yet ready — else IDLE_MS (10 s)."""
    name = "ai.runtime"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 10.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import ai_runtime as r
        return r.api_ai_runtime()

    def poll_interval(self, params, age_s, last):
        if isinstance(last, dict):
            busy = bool(last.get("downloading")) or any(
                m.get("state") not in ("ready", "error") for m in (last.get("loaded") or []) if isinstance(m, dict))
            if busy:
                return 1.0
        return 10.0


class SystemActivityTopic(Topic):
    """`GET /api/system/activity?scope=` (shell/system-lib.ts, the Monitor
    page). Event-sourced from the sysmon sampler's own tick (its thread
    publishes after every sample), and `poll()` is what keeps the sampler
    awake, so the snapshot is the GET's body exactly. The cadence is the
    sampler's 1 s — at or better than every old reader (system-lib.ts
    FAST_POLL_MS 1 s / SLOW_POLL_MS 30 s; MonitorPage 2 s)."""
    name = "system.activity"
    kind = "event"
    hidden_ok = True

    def validate(self, params):
        scope = params.get("scope")
        return {"scope": "all" if scope == "all" else "fused"}

    def key(self, params):
        return params["scope"]

    def snapshot(self, params):
        from fused_render.server.routers import system as r
        return r.api_system_activity(params["scope"])


class EnginesRunningTopic(Topic):
    """`GET /api/engines/running` (shell/ActivityDock.tsx). Poll-diff: engine
    liveness is a `Popen.poll()`, at the old cadence ENGINES_POLL_MS (10 s)."""
    name = "engines.running"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 10.0  # old cadence: shell/ActivityDock.tsx ENGINES_POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import engines as r
        return r.api_engines_running()


class GitUpstreamTopic(Topic):
    """`GET /api/git-upstream` (shell/RepoUpdatesDock.tsx). Write-triggered
    by `_record/_record_failure/_record_pull`, the dismissals and the POST
    actions; plus the old cadence (RepoUpdatesDock POLL_MS, 6 s) as a
    poll-diff for the checks that land from the sync threads."""
    name = "git.upstream"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 6.0  # old cadence: shell/RepoUpdatesDock.tsx POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import git_upstream as r
        return r.api_git_upstream()


class LanPairingsTopic(Topic):
    """`GET /api/lan/pairings` (shell/RepoUpdatesDock.tsx, 6 s). Write-triggered
    by a device pairing and the dismiss route."""
    name = "lan.pairings"
    kind = "write"
    hidden_ok = True

    def snapshot(self, params):
        from fused_render import lan
        return lan.api_lan_pairings()


class LanDevicesTopic(Topic):
    """`GET /api/lan/devices` (shell/Preferences.tsx, 3 s while the pairing
    panel is open). Write-triggered by pairing and the revoke routes."""
    name = "lan.devices"
    kind = "write"
    hidden_ok = True

    def snapshot(self, params):
        from fused_render import lan
        return lan.api_lan_devices()


class CanvasesStatusTopic(Topic):
    """`GET /api/canvases/status`. Poll-diff: the credentials file is written
    by the `fused` CLI. Old cadences: apps/canvases/logged-in.ts POLL_MS
    (60 s), and 1.5 s while a login is in flight (ShareAppModal, Canvases,
    FusedAccountSection wait on the creds stamp at 1.5 s)."""
    name = "canvases.status"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 60.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render import canvases
        return canvases.api_canvases_status()

    def poll_interval(self, params, age_s, last):
        if isinstance(last, dict) and last.get("login_in_flight"):
            return 1.5
        return 60.0


class FdaTopic(Topic):
    """`GET /api/config`'s `fda` field (platform/lib/fda.ts). Poll-diff: TCC
    is OS state. Old cadence: fda.ts POLL_MS (3 s) until granted, after
    which nothing can change for this process and the tick stands down."""
    name = "fda"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 3.0  # old cadence: platform/lib/fda.ts POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.shell import fda as shell_fda
        return {"fda": shell_fda.snapshot()}

    def poll_interval(self, params, age_s, last):
        fda = last.get("fda") if isinstance(last, dict) else None
        if fda is None or (isinstance(fda, dict) and fda.get("granted")):
            return 3600.0  # final for this process: nothing to re-derive
        return 3.0


class IndexStatusTopic(Topic):
    """`GET /api/index/status` (platform/lib/index-status.ts). Poll-diff: the
    index worker is a child process. Old cadence: INDEX_POLL_MS (1.5 s)
    while scanning, INDEX_IDLE_POLL_MS (10 s) otherwise."""
    name = "index.status"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 10.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import index as r
        body = r.api_index_status(run_id="", since=0)
        return body if isinstance(body, dict) else {}

    def poll_interval(self, params, age_s, last):
        return 1.5 if isinstance(last, dict) and last.get("scanning") else 10.0


class BookmarksTopic(Topic):
    """`GET /api/bookmarks` (main.tsx, 30 s + focus). Write-triggered by the
    PUT; the missing-file flag (D127) moves with the disk, so the old 30 s
    cadence stays as a poll-diff."""
    name = "bookmarks"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 30.0  # old cadence: main.tsx BOOKMARK_POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.shell import bookmarks
        return bookmarks.bookmarks_payload()


class DockTopic(Topic):
    """`GET /api/dock` (dock/dock.ts, 1.5 s while visible). Write-triggered
    by the dock's own routes; its entries also follow the bots and apps on
    disk, so the old 1.5 s cadence stays as a poll-diff."""
    name = "dock"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 1.5  # old cadence: dock/dock.ts schedule()
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import dock as r
        return r.dock_get()


class TerminalListTopic(Topic):
    """`GET /api/terminal` (shell/TerminalDrawer.tsx, 2 s while open).
    Write-triggered by the pty registry (create/kill/reap); the Claude tabs'
    command log is appended by the agent's hooks, so the old 2 s cadence
    stays as a poll-diff."""
    name = "terminal.list"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 2.0  # old cadence: shell/TerminalDrawer.tsx CLAUDE_TAB_POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import terminal as r
        return _call_handler(r.api_terminal_list)(params)


class PrefsTopic(Topic):
    """`GET /api/prefs`. Write-triggered by the PUT."""
    name = "prefs"
    kind = "write"
    hidden_ok = True

    def snapshot(self, params):
        from fused_render.shell import prefs
        return prefs._prefs_response()


# ======================================================================= phase 2

class GithubSetupTopic(Topic):
    """The git template's publish modal: its four `/api/github/*` reads
    merged into one body `{status, install, login, publish}`. Write-triggered
    by the install/publish setters and the login start/cancel; the login
    thread's own writes ride the old cadence as a poll-diff (templates/git
    GH_POLL_MS, 1.5 s)."""
    name = "github.setup"
    kind = "write"
    hidden_ok = True
    poll_interval_s = 1.5  # old cadence: templates/git/template.html GH_POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render import github_login, github_setup
        return {"status": github_setup.summary(), "install": github_setup.install_status(),
                "login": github_login.status(), "publish": github_setup.publish_status()}


# ======================================================================= phase 3
#
# The streams. `claude.run` is the chat's 400 ms `{poll}`: the CLI writes
# `out.jsonl` from its own process, so the honest producer is the same read
# `_poll` always was, once per subscribed run per process, at the old
# cadence — every document watching that run gets the frame. `bots` keeps the
# page's per-bot cursors SERVER-SIDE: the frame's `gen` is the cursor map.


class ClaudeRunTopic(Topic):
    """`POST /api/claude/agent {action: "poll"}` as a feed, keyed by run id.
    The body is `_poll`'s whole-turn answer (the controller's contract: no
    cursor, every frame is the whole turn). Not `hidden_ok`: a run keeps
    streaming into a hidden chat."""
    name = "claude.run"
    kind = "event"
    hidden_ok = False
    poll_interval_s = 0.4  # old cadence: apps/claude/protocol/run-controller.ts POLL_MS
    signature_is_snapshot = True

    def validate(self, params):
        run_id = params.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise TopicError("run_id: expected a run id")
        return {"run_id": run_id, "file": str(params.get("file") or ""),
                "native": str(params.get("native") or "1"), "queue": str(params.get("queue") or "0")}

    def key(self, params):
        return (params["run_id"], params["file"], params["native"], params["queue"])

    def snapshot(self, params):
        from fused_render import claude_agent
        from fused_render._binding import bind_params
        agent = claude_agent.agent_module()
        bound = bind_params(agent.main, {"action": "poll", **params})
        return agent.main(**bound)


class ClaudeLiveTopic(Topic):
    """The chat's standing live-run watch (apps/claude/live/watch.ts) and its
    adoption probes (run-controller `adoptWatch`), merged: `{live: <live_run
    answer>, liveness: <GET /api/claude-sessions/liveness> | null}`. Poll-diff
    at the old cadences: 400 ms for the probe window (ADOPT_LAPS × POLL_MS,
    ~3.2 s after a subscribe), then the watch's LIVE_WATCH_MS (5 s)."""
    name = "claude.live"
    kind = "event"
    hidden_ok = True
    poll_interval_s = 5.0
    PROBE_S = 0.4
    PROBE_WINDOW_S = 3.5
    signature_is_snapshot = True

    def validate(self, params):
        return {"file": str(params.get("file") or ""), "session_id": str(params.get("session_id") or ""),
                "path": str(params.get("path") or "")}

    def key(self, params):
        return (params["file"], params["session_id"], params["path"])

    def snapshot(self, params):
        from fused_render import claude_agent
        from fused_render._binding import bind_params
        agent = claude_agent.agent_module()
        bound = bind_params(agent.main, {"action": "live_run", "file": params["file"],
                                         "session_id": params["session_id"]})
        live = agent.main(**bound)
        liveness = None
        if params["path"]:
            from fused_render.server.routers import claude_sessions as r
            body = r.api_claude_session_liveness(params["path"])
            liveness = body if isinstance(body, dict) else None
        return {"live": live, "liveness": liveness}

    def poll_interval(self, params, age_s, last):
        return self.PROBE_S if age_s < self.PROBE_WINDOW_S else self.poll_interval_s


class BotsTopic(Topic):
    """`GET /api/bots?cursors=&shot_for=&fast=` as a feed (apps/bots/state/
    store.ts). The page's per-bot cursors live here: a subscribe answers the
    first poll's body (cursors `{}`), the frame's `gen` is the cursor map the
    page used to keep, and every tick's delta is `bots_status` from THAT
    map — the same incremental events. Ticks at the old cadence (store.ts
    `loopDelay`: 400 ms with the Stage open, 1.5 s while a bot runs, else 3 s),
    and `Bot.emit`/`set_status` wake it at once. A frame that says nothing new
    is dropped (`frame_signature` ignores the clock). Not `hidden_ok`: the
    hand-over chime fires for a hidden window."""
    name = "bots"
    kind = "event"
    hidden_ok = False
    poll_interval_s = 3.0
    tick_only = True

    def validate(self, params):
        fast = params.get("fast")
        shot_for = params.get("shot_for")
        return {"fast": bool(fast) and str(fast) not in ("0", "false", ""),
                "shot_for": shot_for if isinstance(shot_for, str) else ""}

    def key(self, params):
        return (params["fast"], params["shot_for"])

    def _body(self, params, cursors: dict) -> dict:
        import json as _json
        from fused_render.bots import routes
        body = routes.bots_status(cursors=_json.dumps(cursors), shot_for=params["shot_for"],
                                  fast="1" if params["fast"] else "0")
        return body if isinstance(body, dict) else {}

    def snapshot(self, params):
        return self._body(params, {})

    def generation(self, body):
        return {b["id"]: b.get("seq", 0) for b in body.get("bots", []) if isinstance(b, dict) and "id" in b}

    def delta(self, params, since):  # type: ignore[override]
        cursors = since if isinstance(since, dict) else {}
        body = self._body(params, cursors)
        return body, self.generation(body)

    def frame_signature(self, body):
        return canonical({k: v for k, v in body.items() if k != "ts"})

    def poll_interval(self, params, age_s, last):
        if params["fast"]:
            return 0.4
        try:
            from fused_render.bots.routes import registry
            if any(b.meta.get("status") == "running" for b in registry.all()):
                return 1.5
        except Exception:  # noqa: BLE001
            pass
        return 3.0


class BotsBuildsTopic(Topic):
    """`GET /api/bots/builds` (apps/bots/builds). Write-triggered by
    `store.builds_write`."""
    name = "bots.builds"
    kind = "write"
    hidden_ok = True

    def snapshot(self, params):
        from fused_render.bots import routes
        return routes.bots_builds_get()


class BotsImessageTopic(Topic):
    """`GET /api/bots/imessage` (apps/bots/dialogs/PhoneSection.tsx). Poll-diff:
    the bridge is a separate process; old cadence POLL_MS (3 s)."""
    name = "bots.imessage"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 3.0  # old cadence: apps/bots/dialogs/PhoneSection.tsx POLL_MS
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.bots import routes
        return routes.bots_imessage()


# ======================================================================= phase 4
#
# The one-shot waits: a modal that polled until something it started was
# done. Each is a poll-diff at the old client cadence, keyed on what it waits
# for, alive only while the modal is subscribed.


class ShareUploadTopic(Topic):
    """`GET /api/share/file/upload/status?id=` (platform/ui/ShareFileModal.tsx):
    marker files written by the detached upload wrapper. Old cadence 1.2 s."""
    name = "share.upload"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 1.2  # old cadence: platform/ui/ShareFileModal.tsx pollUpload
    signature_is_snapshot = True

    def validate(self, params):
        from fused_render import share_file
        upload_id = params.get("id")
        if not isinstance(upload_id, str) or not upload_id:
            raise TopicError("missing id")
        if not share_file._valid_upload_id(upload_id):
            raise TopicError("invalid id")
        return {"id": upload_id}

    def key(self, params):
        return params["id"]

    def snapshot(self, params):
        from fused_render import share_file
        return share_file.read_upload_state(params["id"])


class HfAuthTopic(Topic):
    """`GET /api/hf/auth` (HubSearchScreen's HubLogin, Preferences): the
    device-code login writes the token file from its own thread. Old cadence
    2 s while a login is pending."""
    name = "hf.auth"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 2.0  # old cadence: apps/ai_models/local/HubSearchScreen.tsx HubLogin, shell/Preferences.tsx
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render.server.routers import hf_auth as r
        return r.api_hf_auth()


class ClaudeSetupTopic(Topic):
    """`GET /api/claude/install` + `/api/claude/login` merged
    (platform/lib/claude-setup.ts). The install and sign-in threads write
    module state; old cadences: INSTALL_POLL_MS (1.2 s) while an install
    runs, LOGIN_POLL_MS (2 s) while a sign-in is in flight, nothing otherwise
    — a POST is what starts either, and the page resyncs after it."""
    name = "claude.setup"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 2.0
    signature_is_snapshot = True

    def snapshot(self, params):
        from fused_render import claude_install, claude_login
        return {"install": claude_install.status(), "login": claude_login.status()}

    def poll_interval(self, params, age_s, last):
        if isinstance(last, dict):
            if (last.get("install") or {}).get("state") == "running":
                return 1.2
            if (last.get("login") or {}).get("in_flight"):
                return 2.0
        return 3600.0  # nothing moves without a POST, and the page resyncs after one


class CanvasesSyncTopic(Topic):
    """`GET /api/canvases/sync/status?name=` (apps/canvases/CanvasWorkspace.tsx):
    the sync manager's seq bumps. Old cadence 2 s per open canvas."""
    name = "canvases.sync"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 2.0  # old cadence: apps/canvases/CanvasWorkspace.tsx sync status poll
    signature_is_snapshot = True

    def validate(self, params):
        from fused_render import canvases
        name = params.get("name")
        if not isinstance(name, str) or not canvases._NAME_RE.fullmatch(name or ""):
            raise TopicError("'name' must be a canvas name (letters, digits, underscore)")
        return {"name": name}

    def key(self, params):
        return params["name"]

    def snapshot(self, params):
        from fused_render import canvases
        body = canvases.api_canvases_sync_status(params["name"])
        return body if isinstance(body, dict) else {}


class AppsDoctorTopic(Topic):
    """`GET /api/apps/doctor?path=&fetch=0` (platform/ui/useAppDoctorChecks.ts,
    AppDoctorModal): re-asked every CHECK_POLL_MS (4 s) only while a check
    task is live on some row, as the old pollers were."""
    name = "apps.doctor"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 4.0  # old cadence: platform/ui/useAppDoctorChecks.ts CHECK_POLL_MS
    signature_is_snapshot = True

    def validate(self, params):
        path = params.get("path")
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            raise TopicError("'path' must be an absolute app folder")
        return {"path": path}

    def key(self, params):
        return params["path"]

    def snapshot(self, params):
        from fused_render.server.routers import apps as r
        body = r.api_app_doctor(params["path"], fetch=False)
        return body if isinstance(body, dict) else {}

    def poll_interval(self, params, age_s, last):
        checks = last.get("checks") if isinstance(last, dict) else None
        if isinstance(checks, list) and any(isinstance(c, dict) and c.get("check_task") for c in checks):
            return 4.0
        return 3600.0  # nothing re-asks without a live check task; a POST resyncs


class AiMetricsTopic(Topic):
    """`GET /api/ai/metrics?minutes=` (apps/ai_models/usage/UsageTab.tsx), old
    cadence POLL_MS (5 s) while the tab is open."""
    name = "ai.metrics"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 5.0  # old cadence: apps/ai_models/usage/UsageTab.tsx POLL_MS
    signature_is_snapshot = True

    def validate(self, params):
        minutes = params.get("minutes", 15)
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise TopicError("minutes: expected a number")
        return {"minutes": minutes}

    def key(self, params):
        return params["minutes"]

    def snapshot(self, params):
        from fused_render.server import ai as r
        return r.api_ai_metrics(params["minutes"])


class AiHubCacheTopic(Topic):
    """The Hub catalog pool's build state for one capability
    (`hub_catalog_builder.build_status`), what HubSearchScreen re-asked its
    search for every 15 s while the pool was building. The search itself
    stays a POST, re-issued when this moves (D11)."""
    name = "ai.hubcache"
    kind = "polldiff"
    hidden_ok = True
    poll_interval_s = 15.0  # old cadence: apps/ai_models/local/HubSearchScreen.tsx pool re-poll
    signature_is_snapshot = True

    def validate(self, params):
        capability = params.get("capability")
        if not isinstance(capability, str) or not capability:
            raise TopicError("capability: expected a capability name")
        return {"capability": capability}

    def key(self, params):
        return params["capability"]

    def snapshot(self, params):
        from fused_render.ai import hub_catalog_builder
        return hub_catalog_builder.build_status(params["capability"])


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
    for topic in (ScheduleTopic(), ScheduleQueueTopic(), ScheduleEventsTopic(), UpdateTopic(),
                  AiRuntimeTopic(), SystemActivityTopic(), EnginesRunningTopic(), GitUpstreamTopic(),
                  LanPairingsTopic(), LanDevicesTopic(), CanvasesStatusTopic(), FdaTopic(),
                  IndexStatusTopic(), BookmarksTopic(), DockTopic(), TerminalListTopic(), PrefsTopic(),
                  GithubSetupTopic(), ClaudeRunTopic(), ClaudeLiveTopic(), BotsTopic(), BotsBuildsTopic(),
                  BotsImessageTopic(), ShareUploadTopic(), HfAuthTopic(), ClaudeSetupTopic(),
                  CanvasesSyncTopic(), AppsDoctorTopic(), AiMetricsTopic(), AiHubCacheTopic()):
        bus.register(topic)
    tasks_watch.listeners.append(lambda: bus.publish("tasks.listing"))
    jobs_mod.on_change.append(lambda: bus.publish("jobs"))


__all__ = ["register_all", "simple_topic", "TasksListingTopic", "FsWatchTopic", "JobsTopic"]
