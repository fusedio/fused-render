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

function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  if (listeners.size === 0) {
    timer = null;
    return;
  }
  timer = window.setTimeout(poll, pulse.running > 0 ? ACTIVE_MS : IDLE_MS);
}

/** Hand over a known-fresh answer — what the Tasks page's own poll returned — so
 *  the sidebar tracks the page for free and this module's timer starts over. */
export function publishTasks(next: Task[]) {
  tasks = next;
  recompute();
  schedule();
}

/**
 * The reader is looking at the page: every completion on screen counts as shown.
 *
 * Called on landing AND on every poll while the entry is active, which is what
 * makes the mark stay gone while the page is open — a dot pointing at a row the
 * reader is looking at is noise. It comes back when a task completes after the
 * visit, because that completion was never stamped (tasks-lib.seenAfterVisit).
 */
export function markTasksSeen() {
  writeSeen(seenAfterVisit(tasks));
}

/** Subscribe to the summary. Polling starts with the first reader and stops with
 *  the last — nothing polls on behalf of a sidebar nobody has mounted. */
export function useTasksPulse(): TasksPulse {
  const [current, setCurrent] = useState<TasksPulse>(pulse);
  useEffect(() => {
    listeners.add(setCurrent);
    // Read immediately rather than waiting out an interval: a sidebar that has
    // just mounted should not claim "nothing is running" for ten seconds first.
    void poll();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}
