// What the sidebar's Tasks entry knows about the Tasks page, shared by the two
// readers of it: the entry itself (GlobalSidebar) and the page (Scheduled).
//
// ONE POLL, TWO READERS — the shape aiRuntime.ts established for the AI Models
// dot, for the same reason. The sidebar needs two numbers and the page needs
// every row; polling twice would ask the same endpoint twice a minute for one
// answer, and worse, the two would disagree for a beat after anything changed.
// So the poll lives here, the sidebar subscribes, and the PAGE publishes what
// its own poll returned (publishTasks) — which resets the timer below, so while
// the page is open this module never calls the server at all.
//
// The cadence follows the state, not the clock: while something is running the
// dot's colour can change on any tick, and while nothing is running the only
// thing that can move is a completion nobody is waiting on this second. An idle
// machine costs one compact `GET /api/tasks/pulse` every 30 seconds. Only the
// Tasks page itself asks for titles, paths, descriptions, and message previews.
//
// WHAT IS NOT HERE: the route. "The reader has landed on /tasks" is the
// sidebar's fact, not this module's — it calls markTasksSeen() — because a store
// that reads location.pathname is a store that has to be told when the pathname
// changes.
import { useEffect, useState } from "react";
import { getTaskChanges, getTasksPulse } from "@platform/lib/api";
import type { Task, TaskChanges, TaskPulseTask } from "@platform/lib/api";
import {
  EMPTY_TASKS_PULSE,
  TASKS_SEEN_KEY,
  parseTasksSeen,
  sameSeen,
  samePulse,
  seenAfterVisit,
  tasksPulse,
} from "./tasks-lib";
import type { TasksPulse, TasksSeen } from "./tasks-lib";

/** While something is running. Faster than the page's own 20s poll on purpose:
 *  this is the interval a "it finished" mark waits out. */
const ACTIVE_MS = 10_000;
const IDLE_MS = 30_000;

let tasks: TaskPulseTask[] = [];
let seen: TasksSeen = readSeen();
let pulse: TasksPulse = EMPTY_TASKS_PULSE;
let timer: number | null = null;
let inFlight = false;
/** Which answer is newest. Every publish bumps it; a self-poll captures it on
 *  departure and publishes only if nothing fresher landed while it was in
 *  flight (bugbot, 2026-08-18: a stale self-poll resolving after the page's
 *  own publish must lose, not overwrite). */
let generation = 0;
/** Has a real answer landed? `tasks` is `[]` both before the first read and on a
 *  machine with no tasks, and those two must not be treated alike — see
 *  markTasksSeen, where mistaking one for the other throws away the reader's
 *  dismissals. */
let loaded = false;
/** How many owners are feeding this store from their OWN poll (the Tasks page).
 *  While there is one, this module does not poll at all — see schedule. */
let feeders = 0;
const listeners = new Set<(p: TasksPulse) => void>();
/** Readers of the ROWS rather than the summary (the sidebar's Current apps
 *  section). Fired on every publish, not only on a changed summary: two
 *  answers with the same running/unseen counts can still name different
 *  projects. Counted with `listeners` for the poll's start/stop, so a rows
 *  reader alone keeps the poll alive too. */
const rowListeners = new Set<(rows: TaskPulseTask[]) => void>();

function readSeen(): TasksSeen {
  try {
    return parseTasksSeen(localStorage.getItem(TASKS_SEEN_KEY));
  } catch {
    // A blocked or throwing store (private mode, locked-down webviews) costs
    // the dismissal — one dot too many — never the sidebar.
    return {};
  }
}

function writeSeen(next: TasksSeen) {
  if (sameSeen(seen, next)) return;
  seen = next;
  try {
    localStorage.setItem(TASKS_SEEN_KEY, JSON.stringify(next));
  } catch {
    // Same trade as readSeen: the dismissal is a convenience, not the feature.
  }
  recompute();
}

/** Publish only on a CHANGED pair. Every poll and every page publish lands here,
 *  and the sidebar re-rendering four times a minute over two identical numbers
 *  is the sort of cost that is invisible until it is not. It is also what stops
 *  the sidebar's own "mark seen while on /tasks" effect from looping. */
function recompute() {
  const next = tasksPulse(tasks, seen);
  if (samePulse(pulse, next)) return;
  pulse = next;
  for (const listener of listeners) listener(next);
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  const departed = generation;
  try {
    const answer = (await getTasksPulse()).tasks ?? [];
    // A feeder took over, or a fresher publish landed, while this request was
    // in the air: this answer is already history. Drop it.
    if (feeders === 0 && generation === departed) publishTasks(answer);
  } catch {
    // A failed read is not news: the sidebar keeps the last answer it had rather
    // than dropping a dot because one poll lost a race with a restart.
  } finally {
    inFlight = false;
    schedule();
  }
}

/**
 * Arm the next self-poll — or, deliberately, do not.
 *
 * NOTHING IS POLLED WHILE SOMEONE ELSE IS FEEDING US (bugbot, 2026-08-18).
 * Restarting the timer on every publish was not enough: the page polls every 20s
 * and this module re-armed at 10s whenever anything was running, so the busiest
 * case — the Tasks page open with work in flight — fired an EXTRA request between
 * the page's own, which is exactly the double-poll the shared store exists to
 * prevent. A feeder is not a hint about timing, it is a statement that this
 * module is not the poller, so the timer simply does not run.
 */
function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  // THE FAST LANE FOLLOWS THE SAME RULE AS THE TIMER, and is started and
  // stopped from the same place so the two can never disagree about who is
  // polling: one reader and no feeder means this module is the poller, on both
  // clocks.
  syncFastLane();
  if (listeners.size + rowListeners.size === 0 || feeders > 0) return;
  timer = window.setTimeout(poll, pulse.running > 0 ? ACTIVE_MS : IDLE_MS);
}

// ---- the fast lane -----------------------------------------------------------
//
// "1 running" IN THE RAIL SHOULD BE INSTANT, and on the two intervals above it
// was not: a run that started the moment after a poll went unmentioned for ten
// seconds, and one that started on an idle machine for thirty (Akshil,
// 2026-09-12: "should be instant… everywhere in UI"). The number itself is
// cheap to fetch; what was slow was WAITING to ask.
//
// So the store watches `/api/tasks/changes` — the same long-poll the Tasks page
// runs (Scheduled.tsx) against the same server-side watcher, which answers the
// moment a session starts, resumes, takes a prompt, grows, or any queue verb
// rings it. On a generation move this calls `poll()` at once; the intervals stay
// exactly as they were, as the floor under a watcher that missed something.
//
// ONE POLLER, STILL. The loop runs only while this module is the poller — a
// reader mounted and NO feeder — because the Tasks page runs this very lane
// itself and publishes what it learns (`publishTasks`), which is how the
// sidebar comes along without a second connection. `useTasksFeeder` therefore
// stands the whole module down, this lane included, and starting it back up is
// the same `schedule()` call that re-arms the timer.

/** The long-poll's own wait, in seconds — the server caps it at its own
 *  `MAX_WAIT_SEC`. Shorter than a proxy's idle timeout on purpose. */
const CHANGES_WAIT_S = 25;
/** A failed call backs off rather than hammering a server that is restarting.
 *  The same 3 s the Tasks page's lane spends. */
export const FAST_LANE_BACKOFF_MS = 3000;
/** …and it DOUBLES while the same thing keeps happening, up to this. Three
 *  seconds is the right first guess about a server mid-restart; it is the wrong
 *  forever-cadence for a server that is never going to answer this endpoint
 *  properly (an old build behind a new shell, a login page in front of it). */
export const FAST_LANE_BACKOFF_MAX_MS = 30_000;
/** How many hollow laps in a row end the lane for this page load. */
export const FAST_LANE_MAX_STALLS = 5;
/** A lapse — "25 s passed and nothing moved" — is the watcher's normal answer
 *  and must stay free. What is NOT normal is the same non-answer returning
 *  immediately: under this, a lap that did not move the generation cannot have
 *  waited on anything, so it is a hot loop rather than a long poll. */
export const FAST_LANE_MIN_LAP_MS = 1000;

/** The loop's world, so a suite can drive it with a fake fetch and a fake clock
 *  instead of a real connection and a real 25 seconds. */
export interface FastLaneDeps {
  /** `/api/tasks/changes?since=…`. */
  changes(since: number): Promise<TaskChanges>;
  /** Something moved: re-read the pulse NOW. */
  onChange(): void;
  visible(): boolean;
  /** Resolves on the next hidden → visible edge. */
  untilVisible(): Promise<void>;
  sleep(ms: number): Promise<void>;
  /** The lane has been stood down (last reader gone, or a feeder took over). */
  stopped(): boolean;
  /** The clock, only ever read as a difference — how long one lap took, which
   *  is what separates a 25 s lapse from a server answering instantly. Default
   *  `Date.now`; a suite hands over a fake one. */
  now?(): number;
}

/**
 * Watch the server's change generation, and poke the pulse when it moves.
 *
 * THE FIRST ANSWER IS A HANDSHAKE, NOT NEWS. `since < 0` is answered at once
 * with the current generation and no keys (tasks_watch.wait), which is exactly
 * what a store that has just mounted wants: a starting point, without a poke —
 * `useTasksPulse` already reads the pulse on mount, and poking here would make
 * every mount pay for two.
 *
 * WHAT THE ROWS SAY IS NOT READ, deliberately. The page merges them because it
 * draws them; this module wants the compact `/api/tasks/pulse` shape and gets it
 * by asking. The long-poll is a doorbell, and the answer to it is one small GET.
 *
 * A HIDDEN TAB SITS THE LOOP OUT. The browser throttles its timers to about a
 * lap a minute anyway, and a backgrounded window holding a connection open is
 * the one cost this must not add per window; `untilVisible` resumes it, and the
 * handshake on the way back in re-syncs the generation for free.
 *
 * A HOLLOW LAP IS PACED LIKE A FAILURE, and one that repeats ends the lane.
 * Only a THROWN call used to back off, and the loop's whole pacing rested on an
 * answer it never checked: a 200 with no numeric `generation` — an older server,
 * a proxy or login page returning HTML with a 200, a shape that moved — left
 * `since` at -1, which the server answers at once as a handshake, which leaves
 * `since` at -1. That is a request storm with nothing in it that can ever slow
 * it down, from every window with a sidebar open. So the lap, not the
 * exception, is what is judged: one that did not move the generation AND came
 * back faster than any real wait (`FAST_LANE_MIN_LAP_MS`) is hollow, and hollow
 * laps back off — doubling to `FAST_LANE_BACKOFF_MAX_MS` — and, after
 * `FAST_LANE_MAX_STALLS` of them in a row, stand the lane down for good. A
 * genuine 25 s lapse moves nothing either and is NOT hollow: it already waited,
 * which is the difference the clock is read for. Standing down costs the news
 * its earliness, never the news: the 10/30 s intervals are still underneath,
 * and `schedule()` keeps running them.
 */
export async function watchTaskChanges(deps: FastLaneDeps): Promise<void> {
  const now = deps.now ?? (() => Date.now());
  let since = -1;
  /** Consecutive hollow laps. A thrown call is not one of these — an
   *  unreachable server is a server that can come back, and the backoff alone
   *  is the right answer to it. */
  let stalls = 0;
  let backoff = FAST_LANE_BACKOFF_MS;
  while (!deps.stopped()) {
    if (!deps.visible()) {
      await deps.untilVisible();
      continue;
    }
    let lap: "ok" | "hollow" | "failed";
    const started = now();
    try {
      const answer = await deps.changes(since);
      if (deps.stopped()) return;
      const gen =
        typeof answer.generation === "number" && Number.isFinite(answer.generation)
          ? answer.generation
          : -1;
      const handshake = since < 0;
      // `full: true` is "you are further behind than I remember" — a server that
      // restarted, or a store that slept through the ring. It is news by
      // definition: read the pulse and start again from the generation it named.
      const moved =
        !handshake &&
        (answer.full === true ||
          (gen >= 0 && gen > since) ||
          (answer.rows?.length ?? 0) > 0 ||
          (answer.gone?.length ?? 0) > 0);
      // What the NEXT question will ask with. A `full` answer counts as
      // progress even when it names a lower generation (the restart case) —
      // because it changes the question — but only once: a server stuck
      // repeating the same `full` is answering nothing, and says so by not
      // moving this.
      const advanced = gen >= 0 && (gen > since || (answer.full === true && gen !== since));
      if (gen >= 0) since = gen;
      lap = advanced || now() - started >= FAST_LANE_MIN_LAP_MS ? "ok" : "hollow";
      // A HOLLOW LAP IS NOT NEWS, and the poke is where a storm would cost
      // most: every one of them is a `/api/tasks/pulse` of its own. A server
      // repeating `full: true` instantly would otherwise turn one unanswerable
      // long-poll into two fetches a millisecond.
      if (moved && lap === "ok") deps.onChange();
    } catch {
      if (deps.stopped()) return;
      lap = "failed";
    }
    if (lap === "ok") {
      stalls = 0;
      backoff = FAST_LANE_BACKOFF_MS;
      continue;
    }
    if (lap === "hollow" && ++stalls >= FAST_LANE_MAX_STALLS) return;
    if (deps.stopped()) return;
    await deps.sleep(backoff);
    backoff = Math.min(backoff * 2, FAST_LANE_BACKOFF_MAX_MS);
  }
}

let lane: { stop(): void } | null = null;
/** The lane gave up on this server (see watchTaskChanges' hollow laps) — do not
 *  start it again for this page load. Without this the stand-down buys nothing:
 *  `schedule()` runs on every publish, and every one of them would start a fresh
 *  loop to burn its five laps against the same server. Module lifetime on
 *  purpose: what the lane gave up on is the build being served, and that changes
 *  with a reload. */
let laneDown = false;

function syncFastLane() {
  const wanted =
    listeners.size + rowListeners.size > 0 &&
    feeders === 0 &&
    typeof document !== "undefined" &&
    !laneDown;
  if (!wanted) {
    lane?.stop();
    lane = null;
    return;
  }
  if (lane) return;
  let stopped = false;
  let inflight: AbortController | null = null;
  /** Set while the lane is parked on a hidden tab: the way to end a wait that
   *  has no timer and no request behind it. Without this, a sidebar unmounted
   *  while the window was in the background left a listener and a promise that
   *  nothing could ever settle. */
  let wake: (() => void) | null = null;
  const handle = {
    stop() {
      stopped = true;
      // The open long-poll goes with it: a lane nobody is reading must not hold
      // a connection until its 25 s lapse.
      inflight?.abort();
      wake?.();
    },
  };
  lane = handle;
  void watchTaskChanges({
    stopped: () => stopped,
    visible: () => document.visibilityState === "visible",
    untilVisible: () =>
      new Promise<void>((resolve) => {
        const done = () => {
          document.removeEventListener("visibilitychange", onChange);
          wake = null;
          resolve();
        };
        const onChange = () => {
          if (document.visibilityState === "visible") done();
        };
        wake = done;
        document.addEventListener("visibilitychange", onChange);
      }),
    sleep: (ms) =>
      new Promise<void>((resolve) => {
        window.setTimeout(resolve, ms);
      }),
    changes: (since) => {
      inflight = new AbortController();
      return getTaskChanges(since, CHANGES_WAIT_S, inflight.signal);
    },
    // `poll()` carries the in-flight and generation guards already, so a poke
    // can never land a stale answer over a fresher one.
    onChange: () => {
      void poll();
    },
  }).finally(() => {
    if (lane === handle) lane = null;
    // The loop returned while nobody had stood it down: it gave up on a server
    // that cannot answer this endpoint. That verdict outlives this handle.
    if (!stopped) laneDown = true;
  });
}

/** The window event a poke sends when a feeder page owns the poll: the store
 *  may not fetch over a feeder (that is the double-poll again), so it asks THE
 *  PAGE to run its own reload now. Scheduled.tsx listens for exactly this and
 *  publishes back through publishTasks, the same round trip as its timer. */
export const TASKS_POKE_EVENT = "fused-render:tasks-poke";

/**
 * "Something just changed — re-read NOW rather than on the next tick."
 *
 * Called by the surfaces that learn a scheduled run ended long before any timer
 * here would: the queue card's job snapshot (about a second behind the turn —
 * ActivityDock) and the schedule's own done/failed events (App wiring
 * useScheduleEvents). Without this the sidebar and the Tasks page sat out
 * their 10–30s cadences while the status bar already said finished —
 * the same run, two answers, for most of a minute (Akshil, 2026-08-19: "if
 * finished in one, finished in the other").
 *
 * The feeder contract is honoured, not bypassed: while the Tasks page is
 * feeding this store the store must not fetch (that is the double-poll the
 * feeder exists to prevent), so the poke is forwarded to the page as a window
 * event and the page's OWN reload answers. Unfed, the store polls itself
 * immediately — poll() already carries the in-flight and generation guards, so
 * a poke can never land a stale answer over a fresher one.
 */
export function pokeTasks() {
  if (feeders > 0) {
    window.dispatchEvent(new Event(TASKS_POKE_EVENT));
    return;
  }
  // Nobody reading and nobody feeding: nothing on screen to update, and a
  // fetch for an unmounted sidebar is the waste schedule() already refuses.
  if (listeners.size + rowListeners.size === 0) return;
  void poll();
}

/** The localStorage key the chat template (templates/claude/template.html)
 *  stamps when an interactive turn starts or ends. Interactive turns create no
 *  sys:schedule job and no schedule event — neither producer above fires for
 *  them — so a follow-up typed into a chat left every tasks surface stale until
 *  its next slow poll (Akshil, 2026-08-19: "the task's unread status does not
 *  update"). Every same-origin document EXCEPT the writer receives a `storage`
 *  event for the stamp, and the chat runs in its own iframe document, so the
 *  shell around it — and a Tasks page open in another window entirely — hears
 *  the turn for free, with no postMessage and no new endpoint. */
export const CHAT_ACTIVITY_KEY = "fused-render:chat-activity";

/** The storage half of that poke: App forwards every storage event's key here,
 *  and only the chat's stamp is news about /api/tasks — the other rows this
 *  origin writes (seen stamps, list memory) are the readers' own state. */
export function pokeOnChatActivity(key: string | null) {
  if (key === CHAT_ACTIVITY_KEY) pokeTasks();
}

/**
 * The rows as they stand RIGHT NOW, read synchronously.
 *
 * For a first render, not for a subscription — useTasksPulseRows is still the
 * way to follow the rows over time. The Tasks page seeds its own state from
 * this so it can paint before /api/tasks answers (which is 2.9s on a cold
 * process): the sidebar's poll has usually already put every task's key,
 * status, project and title in here, and a row drawn from those is the same row
 * the listing will confirm. Empty until the first answer lands, which is the
 * behaviour the page had before this existed.
 */
export function readTasksRows(): TaskPulseTask[] {
  return tasks;
}

/**
 * The last FULL /api/tasks answer of this JS session — remembered here, beside
 * the pulse rows, because both are the same question asked at two widths.
 *
 * The Tasks page unmounts on every navigation (App keys it on the nav epoch),
 * so List → Home → List used to throw away a complete listing and go back to a
 * skeleton while the same 2.9s call ran again. Module scope outlives the
 * component and dies with the reload, which is the right lifetime: a listing
 * carried across a reload could be arbitrarily old, and there is nothing to
 * invalidate it against before the page's own poll answers anyway.
 */
let listing: Task[] | null = null;

export function rememberListing(next: Task[]) {
  listing = next;
}

export function readListing(): Task[] | null {
  return listing;
}

/** A poll that FAILED is news about the listing too: what is remembered may
 *  describe a server that has since gone away, and a remount seeding from it
 *  would paint rows over a page that then says "Tasks could not be loaded".
 *  Forgetting makes the next mount start from the skeleton, as a first visit
 *  does (review, #1079). */
export function forgetListing() {
  listing = null;
}

/** Hand over a known-fresh answer — what the Tasks page's own poll returned. */
export function publishTasks(next: TaskPulseTask[]) {
  generation += 1;
  tasks = next;
  loaded = true;
  recompute();
  for (const listener of rowListeners) listener(next);
  schedule();
}

/**
 * "I poll this endpoint myself; take my answers and do not make your own calls."
 *
 * The Tasks page holds one of these for as long as it is mounted, which is
 * exactly as long as its own poll is running. Mount/unmount rather than a
 * timestamp heuristic: the store then knows whether it is the poller instead of
 * guessing from how recently someone published.
 */
export function useTasksFeeder() {
  useEffect(() => {
    feeders++;
    schedule();
    return () => {
      feeders--;
      schedule();
    };
  }, []);
}

/**
 * The reader is looking at the page: every completion on screen counts as shown.
 *
 * Called on landing AND on every poll while the entry is active, which is what
 * makes the mark stay gone while the page is open — a dot pointing at a row the
 * reader is looking at is noise. It comes back when a task completes after the
 * visit, because that completion was never stamped (tasks-lib.seenAfterVisit).
 *
 * A NO-OP UNTIL A REAL ANSWER HAS LANDED (bugbot, 2026-08-18). The first render
 * on /tasks runs this against an EMPTY store — the fetch has not come back yet —
 * and stamping "every done task on screen" over an empty screen wrote `{}` and
 * threw away every dismissal the reader had. Someone who opened the page and
 * left before the first poll answered lost the lot, permanently. `loaded` is the
 * difference between "no tasks" and "no answer yet", and the write MERGES over
 * the answer (tasks-lib.seenAfterVisit) rather than replacing the map, so a
 * stamp survives anything short of its task leaving the list.
 */
export function markTasksSeen() {
  if (!loaded) return;
  writeSeen(seenAfterVisit(tasks, seen));
}

/** Subscribe to the summary. Polling starts with the first reader and stops with
 *  the last — nothing polls on behalf of a sidebar nobody has mounted. */
export function useTasksPulse(): TasksPulse {
  const [current, setCurrent] = useState<TasksPulse>(pulse);
  useEffect(() => {
    listeners.add(setCurrent);
    // Read immediately rather than waiting out an interval: a sidebar that has
    // just mounted should not claim "nothing is running" for ten seconds first.
    //
    // UNLESS SOMEONE IS FEEDING US. The sidebar remounts on every navigation
    // (App keys it on the nav epoch), so an unconditional read here would fire a
    // second /api/tasks alongside the Tasks page's own on every trip to that
    // page — the same double-poll the feeder exists to prevent, just spent per
    // navigation instead of per tick. A feeder's answer is already on its way.
    if (feeders === 0) void poll();
    else schedule();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}

/** Subscribe to the compact rows themselves — `key`, `status`, `project`,
 *  `last_active` — for a reader that groups tasks rather than counts them (the
 *  sidebar's Current apps section, D487). Same store, same poll, same feeder
 *  contract as useTasksPulse: this is NOT a second /api/tasks poller. */
export function useTasksPulseRows(): TaskPulseTask[] {
  const [rows, setRows] = useState<TaskPulseTask[]>(tasks);
  useEffect(() => {
    rowListeners.add(setRows);
    setRows(tasks);
    if (feeders === 0) void poll();
    else schedule();
    return () => {
      rowListeners.delete(setRows);
      schedule();
    };
  }, []);
  return rows;
}
