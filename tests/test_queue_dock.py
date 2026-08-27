"""The queue: rows in the ONE bottom-right card for work about to run or running now.

It used to be a card of its own stacked on top of the download manager, and it is
not any more — "this queue and notification thing should be same no? why duplicate
popups? just replace the queue -> thinking -> done" (Akshil, 2026-08-17). One
container, one header, one count, one lifecycle inside it:

    queued -> starting -> running -> finished / failed

Two halves to test, and they are tested in the two ways they can be:

* the ENDPOINT it reads (`GET /api/schedule/queue`), against a live app — what is
  in each of its three lists, and above all what is NOT: a message scheduled for
  later today is not queued, it is scheduled ("show me the queued that are like
  in the current time or past time, not future time").
* the COMPONENTS, structurally, like the template suites: that the link a row
  offers is the app's own `explorerUrl` rather than a path assembled in the card,
  that a refusal is spoken rather than swallowed, that an empty card is no card at
  all, and that there is exactly ONE card in that corner.

The row-level rules (ordering, dedupe, the header count, what Cancel queued counts)
are unit-tested in frontend/src/shell/queue-dock-lib.test.ts, where they are pure.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, jobs, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONT = os.path.join(_ROOT, "frontend", "src")
# The queue half (rows, polling, Cancel queued) and the card it fills (plate, header,
# count, one list, collapse, Clear).
_DOCK = os.path.join(_FRONT, "shell", "QueueDock.tsx")
_CARD = os.path.join(_FRONT, "platform", "ui", "DownloadManager.tsx")
_HOST = os.path.join(_FRONT, "platform", "ui", "NotificationHost.tsx")
_BAR = os.path.join(_FRONT, "platform", "ui", "StatusBar.tsx")
_APP = os.path.join(_FRONT, "shell", "App.tsx")
_CSS = os.path.join(_FRONT, "styles", "notifications.css")


def _at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A store, a wake stub and a clean event log per test — the same isolation
    test_schedule_queue.py sets up, for the same process-global reasons."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    schedule._events.clear()
    schedule._watched.clear()
    # The job registry is a process-global too, and a scheduled send writes to it —
    # so the half of the card the queue does NOT draw has to be isolated the same way.
    jobs.reset()
    yield
    schedule._events.clear()
    schedule._watched.clear()
    jobs.reset()


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"message": prompt, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


# --------------------------------------------------------------- the endpoint


def test_the_dock_shows_past_due_only(client, target):
    """The line the whole feature turns on. Two messages, one overdue and one for
    later today: only the overdue one is queued, because "queued" means the
    scheduler is about to take it — not "scheduled at some point"."""
    overdue = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "overdue",
                                "due": _at(-600).isoformat()}).json()["entry"]
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "this afternoon",
                      "due": _at(4 * 3600).isoformat()})

    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["queued"]] == [overdue["id"]]
    assert [e["message"] for e in body["queued"]] == ["overdue"]


def test_a_live_turn_is_listed_with_what_a_link_needs(client, target, spawned):
    """`live` is what the dock draws for a run already going, and it exists for
    one reason: a run parked on a permission prompt was visible in the job
    registry and unreachable, because a job row knows a title and a status line
    and not WHERE the session is. These entries carry the target and the session
    the turn landed in."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    schedule.tick()

    body = client.get("/api/schedule/queue").json()
    live = body["live"]
    assert [e["id"] for e in live] == [entry["id"]]
    assert live[0]["target"] == str(target)
    assert live[0]["state"] == "sent"
    # and it is out of the queue: it has been claimed and spawned
    assert body["queued"] == []


def test_exactly_one_half_owns_the_run_at_each_step(client, target, spawned):
    """The two halves of the one list must not both draw a run, and must not both
    skip it. The queue half draws whatever `/api/schedule/queue` lists; the job half
    draws `/api/jobs` minus the runs it is TOLD the queue is drawing. So the property
    to pin on the server side is which of the two even has a record at each step, and
    that they key on the same entry id.

        queued   — in `queued`, and NO job row yet (the row is written at spawn)
        live     — in `live`, and a `running` job row: the one overlap, and the only
                   step where the client has to choose. It chooses by entry id, so
                   the id in the queue list and the id inside `sys:schedule:<id>`
                   have to be the same string.
        finished — out of every queue list, and the job row is terminal: the outcome
                   report, and the only row left."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    job_id = f"sys:schedule:{entry['id']}"

    # queued: the queue half alone
    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["queued"]] == [entry["id"]]
    assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == []

    # live: both have a record, and the id the client joins on is the same one
    schedule.tick()
    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["live"]] == [entry["id"]]
    assert body["queued"] == [] and body["running"] == []
    live_job = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert len(live_job) == 1
    assert live_job[0]["state"] == "running"
    # and it can really be stopped, which is what the row's ✕ promises
    assert live_job[0]["cancellable"] is True

    # finished: the queue half is out and the job row is the whole story
    schedule._update(entry["id"], turn="ok")
    schedule._report(entry["id"], state="done", detail="finished")
    body = client.get("/api/schedule/queue").json()
    assert (body["live"], body["queued"], body["running"]) == ([], [], [])
    done = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in done] == ["done"]


def test_a_finished_turn_leaves_the_dock(client, target, spawned):
    """`live` is work IN FLIGHT, not history. A turn that ended has a `turn`
    verdict written on it, and the dock is a picture of what is about to happen —
    the job registry's row is where the outcome is reported."""
    entry = schedule.create(str(target), "already done", _at(-60))
    schedule.tick()
    schedule._update(entry["id"], turn="ok")

    assert client.get("/api/schedule/queue").json()["live"] == []


def test_the_only_overlap_the_server_leaves_is_the_mirror_image(client, target, spawned):
    """WHICH WAY the two records can disagree, because it decides which half has to
    cope. `_watch_turn` writes the entry's `turn` verdict BEFORE reporting the job
    terminal, and `live` is "sent with no turn" — so between those two writes the entry
    is out of every queue list while its job row is still `running`. That is the mirror
    image, it happens on every single run, and the job half is what draws it: one row,
    with the ✕ that really stops the process.

    The other direction — a live queue entry whose job row is already terminal — is not
    a state the server passes through at all. It was a CLIENT artifact: /api/jobs is
    polled about once a second and /api/schedule/queue every six, so the job half knew
    the turn had ended while the queue half was still painting it live, and a terminal
    job row exempt from the handover appeared beside it. Hence `jobRows` dropping a
    drawn run whatever its state, and `openRows` retiring the row against the job
    snapshot instead of the next queue read."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    job_id = f"sys:schedule:{entry['id']}"
    schedule.tick()

    schedule._update(entry["id"], turn="ok")  # the verdict, before the job report
    body = client.get("/api/schedule/queue").json()
    assert (body["live"], body["queued"], body["running"]) == ([], [], [])
    mid = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in mid] == ["running"]
    assert mid[0]["cancellable"] is True  # and its stop is still real

    # and the job report lands second, which is when the row becomes the outcome
    schedule._report(entry["id"], state="done", detail="finished")
    after = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in after] == ["done"]


def test_the_queue_read_stays_open_and_changes_nothing(client, target):
    """Merely LOOKING at the queue must not change what runs — the tick owns every
    state change. And the read is unguarded, like every other read."""
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "overdue",
                      "due": _at(-600).isoformat()})
    before = schedule.list_entries()
    assert client.get("/api/schedule/queue").status_code == 200
    assert client.get("/api/schedule/queue").status_code == 200
    assert schedule.list_entries() == before


# --------------------------------------------------------------- the component


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def dock() -> str:
    return _read(_DOCK)


@pytest.fixture(scope="module")
def card() -> str:
    return _read(_CARD)


def test_every_row_offers_the_way_there(dock):
    """The feature request underneath the whole card: a run parked on a permission
    prompt could not be found. The link is `explorerUrl` — the app's one answer to
    "open this session" — never a path assembled in this component."""
    assert "explorerUrl(entry.target," in dock
    assert "Open in Explorer" in dock
    assert "/explorer/view" not in dock, "the URL must not be re-derived here"
    # the session the turn LANDED in first, the one it was told to resume second
    assert "entry.claude_session_id || entry.session_id" in dock


def test_the_card_reads_the_server_s_queue_and_filters_nothing(dock):
    """What counts as queued is the server's answer. A client-side filter on due
    time would be a second definition of the word, free to drift from the one the
    scheduler acts on."""
    assert "getScheduleQueue()" in dock
    assert "/api/schedule\"" not in dock, "the listing is not this card's feed"
    assert "Date.now()" not in dock, "the card must not decide what is due"


def test_a_refusal_is_spoken(dock):
    """`cancelQueued` answers with two lists because partial success is the normal
    outcome — cancelling races the claim. Dropping the refused half teaches the
    user the button lies."""
    assert "cancelOutcome(" in dock
    assert "r.refused" in dock
    assert 'cancelQueued("all")' in dock, "Cancel queued lives here and nowhere else"


def test_an_empty_card_draws_the_idle_state_not_no_card(card):
    """SUPERSEDED by D565 (user verdict: "different categories of status bar
    should be always present"): this used to return null outright — "a
    picture of work in progress has nothing to draw when there is none" —
    and that rule is gone on purpose, not merely relaxed. Both halves empty
    now draws the IDLE readout ("Idle") instead of vanishing; the gate
    still only ever reasons about jobs and queue, since repo updates are
    their own sibling section (SPEC §36, RepoUpdatesDock.tsx)."""
    assert "const idle = jobs.length === 0 && queued === 0;" in card
    assert "if (jobs.length === 0 && queued === 0) return null;" not in card
    assert '<span className="dl-idle">Idle</span>' in card


def test_there_is_one_card_not_two(dock, card):
    """The whole point of the merge. The queue contributes ROWS and numbers; the
    plate, the header, the count, the collapse and Clear are the card's, and the
    queue no longer draws any of them."""
    # no second plate, no second header, no second list, no second count
    css = _read(_CSS)
    for gone in ("q-host", "q-head", "q-rows", "q-summary"):
        assert gone not in dock, f"the queue still draws its own {gone}"
        assert f".{gone} {{" not in css, f"the second card's .{gone} rule survives"
    # the rows land in the card's one list, above the job rows
    assert "{queue?.rows}" in card
    assert card.index("{queue?.rows}") < card.index("jobs.map((job)")
    # and the one header count is told about them
    assert "jobsSummary(jobs, count)" in card


def test_the_fold_takes_every_row_now_not_just_the_jobs(card):
    """D562 (user call, 2026-08-27): 'everything is foldable, even for the job
    cards' — reversing the earlier partial fold (D558/D559), which pinned the
    queue's rows and a live-run stand-in outside the collapse. Collapsed now
    renders no `.dl-rows` at all, no exemption; collapsed also renders no
    panel at all any more (D563, status bar redesign), so there is nothing
    left standing in a header for reachability to move to — `showCancelAll`
    dropped the collapsed-threshold parameter this docstring used to point
    at (queue-dock-lib.ts's own doc has the current rule)."""
    assert "rowsShown" not in card, "the two-field queue/jobs split is gone"
    assert "foldedJobRows" not in card, "no row is exempt from the fold any more"
    assert "!collapsed && (" in card, "the whole rows block is gated on collapsed alone"
    assert "{queue?.rows}" in card, "the queue's rows still render, just not exempt"
    jobs_ts = _read(os.path.join(_FRONT, "platform", "lib", "jobs.ts"))
    assert "export function rowsShown" not in jobs_ts
    assert "export function foldedJobRows" not in jobs_ts
    css = _read(_CSS)
    assert ".dl-rows.is-folded" not in css, "no partial-height cap — collapsed omits the rows entirely"


def test_the_job_half_is_told_which_runs_the_queue_draws(dock, card):
    """One row per unit of work needs the two halves to AGREE on who draws what, and
    the job half used to guess: it dropped every running `sys:schedule:*` job on the
    assumption that a queue row for it existed. Two ways for that to be false — the
    queue read fails (no rows, and after a failed first read no last snapshot either)
    or the card is mounted bare with nothing filling the slot — and either way a turn
    that was genuinely executing had no row in either half, so no title, no status
    line and no reachable stop. Worse than the invisible-and-unreachable run this
    whole surface was asked for.

    So the ids travel through the same slot the rows do, and they come off the same
    array the rows are rendered from."""
    assert "drawn: string[]" in card, "the slot has to carry the ids, not just the rows"
    assert "jobRows(reported, queue?.drawn)" in card
    assert "drawn: drawnIds(rows)" in dock
    # and the guess is gone from the rule itself
    jobs_ts = _read(os.path.join(_FRONT, "platform", "lib", "jobs.ts"))
    assert "export function jobRows(jobs: Job[], drawn?" in jobs_ts
    assert "startsWith(SCHEDULE_JOB_PREFIX) && isRunning(j)" not in jobs_ts, \
        "the unconditional drop is what left a live run with no row anywhere"


def test_the_two_halves_share_one_job_snapshot_so_the_handover_is_not_a_race(dock, card):
    """The ids only mean one row per run if both halves are talking about the same
    moment, and they were not: the card polls /api/jobs about every second and this half
    polled its queue every six. So a run that ended was terminal in the card while the
    queue half still called it live, and terminal job rows were EXEMPT from the handover
    ("a finished run has left the queue") — two rows for one run, for as long as several
    seconds. Two halves of one fix:

    * `jobRows` drops a drawn run whatever its state, so a duplicate is impossible at
      every instant rather than whenever two timers happen to agree;
    * the card hands its snapshot back up (`onJobs`) and the queue half retires the row
      against it (`openRows`), so the outcome row waits a render rather than a poll —
      and is not stranded at all when the queue read is failing and its last snapshot
      is (rightly) kept.

    It also deletes what used to be a second forever-poll of the same endpoint."""
    assert "onJobs?: (jobs: Job[]) => void" in card, "the slot has to carry the way back"
    assert "onJobs?.(reported)" in card, "the FULL list, not the rows this card draws"
    # Not a bare setState any more: the callback also POKES the tasks store on a
    # run starting or ending (PR #646) — the pin follows the handoff, which is
    # what the two-halves contract actually needs.
    assert "onJobs," in dock and "const onJobs = useCallback(" in dock
    assert "openRows(queueRows(" in dock, "the rows are filtered before anything is told"
    assert "fetchJobs" not in dock, "the queue half must not poll the job registry itself"
    # and the exemption that was the duplicate is gone from the rule
    jobs_ts = _read(os.path.join(_FRONT, "platform", "lib", "jobs.ts"))
    assert "if (!isRunning(j)) return true;" not in jobs_ts, \
        "a terminal row exempt from `drawn` is the same run twice for a poll"
    assert 'return entry === "" || !ids.has(entry);' in jobs_ts
    lib = _read(os.path.join(_FRONT, "shell", "queue-dock-lib.ts"))
    assert "export function openRows(rows: QueueRow[], jobs: Job[]): QueueRow[]" in lib
    # only a LIVE row is handed over: a queued or sending row's terminal job belongs to
    # the previous run of a re-queued entry, and retiring it would cost the entry its
    # only row in either half.
    assert 'r.role === "live" && ended.has(' in lib


def test_a_stand_in_job_row_folds_like_any_other_now(card):
    """A live run the queue half is NOT drawing still gets exactly one row, and it
    is a job row (`jobRows` — unaffected by D562, this is ownership, not fold).
    It used to be exempt from the collapse specially (`foldedJobRows`); D562
    (user call, 2026-08-27) removed every such exemption, so this row now folds
    like any other — collapsed, it is simply not on screen (D563's chip
    carries no row and no button), not a per-row carve-out."""
    assert "foldedJobRows" not in card
    jobs_ts = _read(os.path.join(_FRONT, "platform", "lib", "jobs.ts"))
    assert "export function foldedJobRows" not in jobs_ts
    assert "export function jobRows(jobs: Job[], drawn?" in jobs_ts


def test_the_stored_fold_is_only_ever_written_by_a_press(card):
    """No auto-expand and no silent rewrite: the preference is the user's. A card
    folded on purpose stays folded — it just stops swallowing the queue's cancels.
    One setter call (the header toggle) and one write beside it."""
    assert card.count("setCollapsed(") == 1
    assert card.count("saveCollapsed(") == 2  # the writer, and its one call site


def test_the_column_owns_where_it_sits(dock, card):
    """Placement belongs to StatusBar / notifications.css (D563) — neither the
    queue nor the card positions itself inline, exactly as it never did when
    NotificationHost owned this instead."""
    for gone in ("position: fixed", "position:fixed", "zIndex", "z-index"):
        assert gone not in dock
        assert gone not in card


def test_the_bar_reserves_space_inside_main_not_the_floating_column(dock, card):
    """D563 (user call: "the collapsed notification is also taking too much
    space... it is impossible to use the claude template with it"). The two
    cards moved OUT of NotificationHost's fixed, floating column and into a
    bar mounted inside `#main`, which reserves layout space for it instead of
    overlaying whatever is under it. Toasts, FdaCard and ServerStatusBanner —
    all short-lived or exceptional enough that overlaying the page is still
    the right call — are the ones left in NotificationHost's column.

    The real BEHAVIOUR this bar composition produces — three sections in
    order, an omitted slot rendering nothing rather than an empty wrapper —
    is a `frontend/src/platform/ui/StatusBar.test.tsx` render test now
    (code review finding #8: a source-literal grep here cannot see whether
    a component actually behaves the way its source claims). This function
    stays a structural/placement check only."""
    app = _read(_APP)
    host = _read(_HOST)
    bar = _read(_BAR)
    # StatusBar is mounted INSIDE #main, alongside the routed content, not as
    # a sibling of it the way NotificationHost is.
    main_at = app.index('<div id="main">')
    bar_use_at = app.index("<StatusBar")
    main_close_at = app.index("</div>", bar_use_at)
    assert main_at < bar_use_at < main_close_at, "StatusBar must render INSIDE #main"
    assert "<NotificationHost />" in app, "the two moved entries are gone from its props"
    assert "activity?: ReactNode" not in host, "NotificationHost no longer takes them"
    assert "repoUpdates?: ReactNode" not in host
    assert "DownloadManager" not in host, "the bare-manager fallback moved to StatusBar"
    assert "activity?: ReactNode" in bar
    assert "repoUpdates?: ReactNode" in bar
    assert "models?: ReactNode" in bar, "D565: a third, always-present section"


def test_the_bar_is_always_present_now_not_gone_when_empty(dock, card):
    """D565 (user verdict on the shipped round-1 bar: "this is very ugly.
    different categories of status bar should be always present and look
    better"). Round 1's `.status-bar:empty { display: none }` rule —
    collapsing the bar to nothing the moment both cards had nothing to show
    — is SUPERSEDED, not extended: the three categories are a fixed status
    readout now, each drawing its own idle text (`No models loaded` / `Idle`
    / `Up to date`) instead of vanishing. `#main` is therefore permanently
    shorter by the bar's height on every page, which is the accepted cost
    the user's own words call for."""
    css = _read(_CSS)
    bar_tsx = _read(_BAR)
    assert ".status-bar:empty" not in css, "the always-gone rule must not survive next to always-present"
    assert '<span className="dl-idle">Idle</span>' in card, "the activity section's own idle text"


def test_the_shell_composes_the_card_and_the_bar_places_it(dock):
    """platform may not import shell (frontend/scripts/check-boundaries.mjs) and a
    queue row has to speak explorerUrl, which lives in shell. So the dependency
    runs the way the boundary allows: shell imports the card and fills its `queue`
    slot, and StatusBar (D563 — no longer NotificationHost, see the placement
    test below) takes the composed thing as its ONE activity entry.

    Omitted, the bare manager stands in: platform does not come to depend on a
    shell that may not be there."""
    assert 'import DownloadManager from "@platform/ui/DownloadManager"' in dock
    bar = _read(_BAR)
    assert "activity?: ReactNode" in bar
    assert "{activity ?? <DownloadManager />}" in bar
    # one entry in the bar, not two stacked cards
    assert bar.count("<DownloadManager />") == 1
