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
// machine costs one `GET /api/tasks` every 30 seconds.
//
// WHAT IS NOT HERE: the route. "The reader has landed on /tasks" is the
// sidebar's fact, not this module's — it calls markTasksSeen() — because a store
// that reads location.pathname is a store that has to be told when the pathname
// changes.
import { useEffect, useState } from "react";
import { getTasks } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
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

let tasks: Task[] = [];
let seen: TasksSeen = readSeen();
let pulse: TasksPulse = EMPTY_TASKS_PULSE;
let timer: number | null = null;
let inFlight = false;
/** Has a real answer landed? `tasks` is `[]` both before the first read and on a
 *  machine with no tasks, and those two must not be treated alike — see
 *  markTasksSeen, where mistaking one for the other throws away the reader's
 *  dismissals. */
let loaded = false;
/** How many owners are feeding this store from their OWN poll (the Tasks page).
 *  While there is one, this module does not poll at all — see schedule. */
let feeders = 0;
const listeners = new Set<(p: TasksPulse) => void>();

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
  try {
    publishTasks((await getTasks()).tasks ?? []);
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
  if (listeners.size === 0 || feeders > 0) return;
  timer = window.setTimeout(poll, pulse.running > 0 ? ACTIVE_MS : IDLE_MS);
}

/** Hand over a known-fresh answer — what the Tasks page's own poll returned. */
export function publishTasks(next: Task[]) {
  tasks = next;
  loaded = true;
  recompute();
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
