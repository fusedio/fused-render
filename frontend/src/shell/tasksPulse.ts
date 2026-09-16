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
//
// AND THE FULL LISTING TOO, since 2026-09-15 — see "THE LISTING FEED" at the
// foot of this file. Same argument one width over: every surface that wants
// `/api/tasks` rows live was opening its own long-poll, and a wall of twelve
// chat cards opened twelve.
import { useEffect, useState } from "react";
import { getTasks, getTasksPulse } from "@platform/lib/api";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import {
  EMPTY_TASKS_PULSE,
  TASKS_SEEN_KEY,
  mergeTaskChanges,
  parseTasksSeen,
  sameSeen,
  samePulse,
  seenAfterVisit,
  tasksPulse,
} from "./tasks-lib";
import type { TasksPulse, TasksSeen } from "./tasks-lib";
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";

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
    // A feeder or the listing feed took over, or a fresher publish landed, while
    // this request was in the air: this answer is already history. Drop it.
    if (!fedElsewhere() && generation === departed) publishTasks(answer);
  } catch {
    // A failed read is not news: the sidebar keeps the last answer it had rather
    // than dropping a dot because one poll lost a race with a restart.
  } finally {
    inFlight = false;
    schedule();
  }
}

/**
 * IS SOMEBODY ELSE THE POLLER?
 *
 * Two owners can say yes, and every guard in this module has to ask BOTH
 * (bugbot, 2026-09-15). The Tasks page's `useTasksFeeder` was the first, and
 * `schedule`/`pokeTasks` learned about the listing feed when it landed — but
 * `poll` and the two subscribe hooks were still asking only about feeders, so a
 * sidebar remounting while a CHAT held the listing (no Tasks page anywhere)
 * fired `/api/tasks/pulse` and published its thinner answer over the full rows
 * the feed had just handed us: two reads, two sources, and a dot that disagreed
 * with the rows under it until the next tick.
 *
 * One predicate so the next owner cannot be added to three of four places.
 */
function fedElsewhere(): boolean {
  return feeders > 0 || listingSubs.size > 0;
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
  // A LISTING FEED IS A FEEDER TOO (2026-09-15). It publishes every row of
  // every answer through publishTasks, so a pulse poll beside it is the same
  // double-poll the feeder rule exists to prevent — just spent on the smaller
  // endpoint.
  // THE FAST LANE FOLLOWS THE SAME RULE AS THE TIMER, and is started and
  // stopped from the same place so the two can never disagree about who is
  // polling (see `syncFeedLane`).
  syncFeedLane();
  if (listeners.size + rowListeners.size === 0 || fedElsewhere()) return;
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
// So while this module is the poller — a pulse reader mounted and NOBODY
// feeding it — it follows the document's own listing feed (`subscribeListing`,
// below), which long-polls `/api/tasks/changes` against the server's change
// watcher and answers the moment a session starts, resumes, takes a prompt,
// grows, or any queue verb rings it. The feed publishes every answer through
// `publishTasks`, so the rail moves on the same tick the Tasks page would; the
// intervals above stay exactly as they were, as the floor under a watcher that
// missed something.
//
// ONE POLLER, STILL. The feed counts as a feeder (`fedElsewhere`), so the pulse
// timer stands down the moment the lane opens — one socket, one listing, and
// the sidebar comes along without a second connection. A page that runs the
// feed itself (Tasks, a chat) is the same subscription refcounted, not a second
// one; `useTasksFeeder` stands the whole module down, this lane included, and
// starting it back up is the same `schedule()` call that re-arms the timer.

/** This module's own subscription to the listing feed, or null while the lane
 *  is closed. */
let feedLane: (() => void) | null = null;

function syncFeedLane() {
  const wanted =
    listeners.size + rowListeners.size > 0 &&
    feeders === 0 &&
    typeof document !== "undefined" &&
    typeof window !== "undefined";
  if (!wanted) {
    if (feedLane) {
      const stop = feedLane;
      feedLane = null;
      stop();
    }
    return;
  }
  if (feedLane) return;
  // CLAIM THE SLOT BEFORE SUBSCRIBING. `subscribeListing` calls `schedule()`
  // synchronously when it is the first subscriber, and `schedule()` comes back
  // here — so with the slot still empty the nested call subscribed a SECOND
  // no-op reader whose disposer was then dropped, `listingSubs` could never
  // return to zero, and the long-poll outlived every reader for the life of
  // the document (merge audit, 2026-09-16). The rows arrive through
  // `publishTasks` inside the feed; nothing to do with the event itself.
  feedLane = () => {};
  feedLane = subscribeListing(() => {});
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
  // The listing feed answers for the rows — one read for the document, whoever
  // is watching them — and the page event below still answers for the schedule
  // and the queue, which are its own two feeds and not this module's.
  if (listingSubs.size > 0) refreshListing();
  if (feeders > 0) {
    window.dispatchEvent(new Event(TASKS_POKE_EVENT));
    return;
  }
  if (listingSubs.size > 0) return;
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
    //
    // AND A LISTING FEED COUNTS (`fedElsewhere`): a chat holding the feed
    // publishes the same rows through the same door, with no Tasks page in
    // sight, and this read would have landed a thinner answer over them.
    if (!fedElsewhere()) void poll();
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
    if (!fedElsewhere()) void poll();
    else schedule();
    return () => {
      rowListeners.delete(setRows);
      schedule();
    };
  }, []);
  return rows;
}

// ── THE LISTING FEED ────────────────────────────────────────────────────────
//
// ONE `GET /api/tasks` AND ONE `/api/tasks/changes` LONG-POLL PER DOCUMENT,
// however many surfaces want the rows.
//
// Every reader of the full listing used to run the pair itself: the Tasks page
// (its own 20s poll plus its own change loop), and `apps/claude/protocol/
// sessions.ts` ONCE PER SUBSCRIPTION — which is once per ClaudeChat mount, and
// a ClaudeChat mounts per card on the Tasks wall and per tile in Peek. Twelve
// cards therefore held twelve sockets open on a 25-second wait, against a
// browser cap of six per origin: every other request on the page — the boot
// reads, the icons, the prefs — queued behind them for minutes, and a reload
// only re-dealt the same hand. They also each re-GET the WHOLE listing on every
// bump, so one `claude` typed in a terminal cost twelve full listing reads.
//
// The store already owned "one poll, many readers" for the pulse, and the
// listing is the same question at a larger width — `readListing`/
// `rememberListing` above were already here. So the loop moves in, refcounted:
// it starts with the first subscriber and stops with the last, and a subscriber
// that arrives after the rows have landed is REPLAYED the current listing
// synchronously rather than waiting out a change that may never come.
//
// WHAT A SUBSCRIBER GETS is the whole machine's listing plus the delta that
// produced it, because the two readers narrow it differently: the Tasks page
// filters by its toolbar, the chat's list by pane (`ui/list-rows.taskInPane`),
// and the question "is this change worth repainting MY list" can only be
// answered where the scope is known. `delta === null` means a whole listing —
// the first read, a refresh, a `full` answer, or a failure — and those always
// concern everyone.

/** The long-poll's own wait, in seconds (T:18367). */
export const CHANGES_WAIT_S = 25;
/** How long a failed change-poll waits before trying again (T:18379). */
export const CHANGES_BACKOFF_MS = 3000;
/**
 * The floor under the long-poll: a full re-read every 20s, which is the Tasks
 * page's own `POLL_MS` moved in here. The watcher answers the moment anything
 * moves, so this is not the way news arrives — it is the answer to a watcher
 * that missed something (a `pending` message going `sent` on the server's own
 * 30s tick writes no transcript) and to the handshake window below.
 */
export const LISTING_FLOOR_MS = 20_000;

/** How long the watcher sits out after a `full` answer, so the catch-up listing
 *  has the field to itself. The Tasks page's own loop waited exactly this. */
export const CATCH_UP_SETTLE_MS = 1000;

/** What `/api/tasks/changes` answers (fused_render/tasks_watch.py). */
interface ChangesResponse {
  generation: number;
  rows?: Task[];
  gone?: string[];
  full?: boolean;
  drafts?: DraftsDelta;
}

/**
 * THE DRAFT HALF OF ONE CHANGE (design "one record", §3; contract §3).
 *
 * The listing has always said which ROWS moved; it now says which DRAFT RECORDS
 * moved too, with the version each is at. That is what lets an open composer or
 * task card take another tab's save within a second instead of finding out on
 * its next reload — and it is what let a whole coordination layer go: a `spent`
 * set, an in-flight map, and a listener a sender had to await.
 *
 * `key` is the DRAFT key, which is the listing's key for chat drafts (a session
 * id, or `new:<file>`) and `draft:<id>` for a task draft.
 *
 * `gone` IS NOISY BY CONSTRUCTION and the contract says so in as many words: the
 * announced key set covers ordinary task activity, so most of what turns up here
 * is a key that never had a draft. A subscriber must ignore `gone` for a key it
 * holds no version for, and must never discard unsaved words on one.
 */
export interface DraftsDelta {
  changed: { key: string; version: number }[];
  gone: string[];
}

/** Only what the feed needs off `fetch`, so a bun test can hand over a
 *  three-line stub instead of the whole DOM signature. */
export type FetchLike = (
  url: string,
  init?: { signal?: AbortSignal },
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

/** The browser pieces the feed reaches for — injectable for bun tests, which
 *  run every suite in ONE process and so cannot module-mock the platform api. */
export interface ListingEnv {
  fetch: FetchLike;
  /** Hidden tabs sit the long-poll out (T:18369-18372). */
  hidden(): boolean;
  /** Resolves on the next `visibilitychange`. The disposer must REMOVE the
   *  listener: a `{once:true}` listener that never fires holds the loop alive. */
  whenVisible(): { promise: Promise<void>; cancel(): void };
  sleep(ms: number): Promise<void>;
  /** The listing itself (`GET /api/tasks`). */
  tasks?: () => Promise<{ tasks?: Task[]; generation?: number }>;
  /**
   * THE PUSH SIDE, alongside the long-poll's pull side: subscribe `fn` to every
   * signal that says a chat just moved, and return the disposer. Attached ONCE
   * for the document now rather than once per subscriber — twelve cards used to
   * mean twelve listing reads per poke.
   */
  pokes?(fn: () => void): () => void;
  /**
   * The FLOOR REFRESH's clock, and deliberately not `after`/`sleep`: those are
   * a caller's own schedules (the recent list's two write-covering looks, the
   * backoff), and a test that asserts on the timers a subscription booked must
   * not find this one among them.
   */
  every?(ms: number, fn: () => void): () => void;
}

/** The change answer a listing event folded in — RAW, exactly as the server
 *  sent it, because "does this concern me" is asked of `project` and `gone`
 *  and a row with no `key` still carries a project worth asking about. */
export interface ListingDelta {
  rows: Task[];
  gone: string[];
}

export interface ListingEvent {
  /** The whole machine's listing as it now stands. `[]` on a failed read. */
  rows: Task[];
  /** The last FULL read failed. The chat's list reads this as "no chats"
   *  (T:18469); the Tasks page draws its quiet "could not be loaded" line. */
  failed: boolean;
  /** `null` ⇒ a whole listing, which concerns every reader. */
  delta: ListingDelta | null;
}

const listingSubs = new Set<(ev: ListingEvent) => void>();
const goneSubs = new Set<(keys: string[]) => void>();
const draftSubs = new Set<
  (changed: { key: string; version: number }[], gone: string[]) => void
>();
/** The newest server generation folded into `listing` — the guard that stops a
 *  full read which left BEFORE a delta from rolling the rows back when it
 *  lands after it (bugbot #892, the rule Scheduled.tsx used to keep itself). */
let listingGen = -1;
let listingFailed = false;
/** The running feed's teardown, and its loader, or null when nobody is
 *  subscribed. */
let feedStop: (() => void) | null = null;
let feedLoad: (() => void) | null = null;
/** One refresh per tick, not one per poke: `focus`, `storage` and
 *  `tasks-changed` all fire for the same turn ending, and the three used to be
 *  three listing reads. A microtask rather than a 250ms timer because the seat
 *  below already makes overlapping reads harmless — this only has to collapse
 *  the burst, not rate-limit the endpoint. */
let refreshQueued = false;

function emitDrafts(delta: DraftsDelta | undefined) {
  if (!delta) return;
  const changed = Array.isArray(delta.changed) ? delta.changed : [];
  const gone = Array.isArray(delta.gone) ? delta.gone : [];
  if (!changed.length && !gone.length) return;
  for (const sub of draftSubs) sub(changed, gone);
}

function emitListing(ev: ListingEvent) {
  for (const sub of listingSubs) sub(ev);
  const gone = ev.delta?.gone;
  if (gone && gone.length) for (const sub of goneSubs) sub(gone);
}

function browserListingEnv(): ListingEnv {
  return {
    fetch: (url, init) => fetch(url, init),
    hidden: () => document.hidden,
    whenVisible: () => {
      let fire: () => void = () => {};
      const promise = new Promise<void>((r) => {
        fire = r;
      });
      document.addEventListener("visibilitychange", fire, { once: true });
      return {
        promise,
        cancel: () => {
          document.removeEventListener("visibilitychange", fire);
          fire(); // let the awaiting loop wake and see `stopped`
        },
      };
    },
    sleep: (ms) => new Promise<void>((r) => setTimeout(r, ms)),
    every: (ms, fn) => {
      const id = setInterval(fn, ms);
      return () => clearInterval(id);
    },
    pokes: (fn) => {
      const onStorage = (ev: StorageEvent) => {
        // A `null` key is a `clear()`, which may well have taken the stamp with
        // it — treat it as news rather than working out whether it was ours.
        if (!ev.key || ev.key === CHAT_ACTIVITY_KEY) fn();
      };
      window.addEventListener(TASKS_CHANGED_EVENT, fn);
      window.addEventListener("storage", onStorage);
      window.addEventListener("focus", fn);
      return () => {
        window.removeEventListener(TASKS_CHANGED_EVENT, fn);
        window.removeEventListener("storage", onStorage);
        window.removeEventListener("focus", fn);
      };
    },
  };
}

function startFeed(env: ListingEnv) {
  let stopped = false;
  let abort: AbortController | null = null;
  let waking: { cancel(): void } | null = null;
  let seat = 0;
  /** A `full` answer has asked for a whole new listing and it has not landed
   *  yet, so the rows in hand describe a server this session has stopped
   *  believing. See the `r.full` branch below. */
  let catchingUp = false;
  const read = env.tasks || getTasks;

  const load = async () => {
    const mine = ++seat;
    try {
      const res = await read();
      if (stopped || seat !== mine) return;
      // The catch-up has landed (or this read superseded it): deltas count again.
      catchingUp = false;
      const rows = (Array.isArray(res?.tasks) ? res.tasks : []).filter(
        (t): t is Task => !!t && !!t.key,
      );
      // A full listing that left before a delta landed is OLDER than what is on
      // screen; applying it would roll the rows back and the generation with
      // them. The next read catches up.
      if (typeof res?.generation === "number") {
        if (res.generation < listingGen) return;
        listingGen = res.generation;
      }
      listingFailed = false;
      rememberListing(rows);
      publishTasks(rows);
      emitListing({ rows, failed: false, delta: null });
    } catch {
      if (stopped || seat !== mine) return;
      // A FAILED catch-up still ends it: holding deltas for ever behind a read
      // that will never land is worse than folding them into whatever comes next,
      // and the failure below forgets the rows anyway.
      catchingUp = false;
      // A listing that cannot be read is "no rows" to the chat's list and a
      // quiet line on the Tasks page — never an error page, and never rows kept
      // over a server that has since gone away (#1079).
      listingFailed = true;
      listingGen = -1;
      forgetListing();
      emitListing({ rows: [], failed: true, delta: null });
    }
  };

  const watch = async () => {
    let gen = -1;
    while (!stopped) {
      if (env.hidden()) {
        const wait = env.whenVisible();
        waking = wait;
        await wait.promise;
        waking = null;
        continue;
      }
      const ctl = new AbortController();
      abort = ctl;
      let r: ChangesResponse;
      try {
        const res = await env.fetch(`/api/tasks/changes?since=${gen}&wait=${CHANGES_WAIT_S}`, {
          signal: ctl.signal,
        });
        if (!res.ok) throw new Error(String(res.status));
        r = (await res.json()) as ChangesResponse;
      } catch {
        if (ctl.signal.aborted) return;
        await env.sleep(CHANGES_BACKOFF_MS);
        continue;
      } finally {
        if (abort === ctl) abort = null;
      }
      if (stopped) return;
      // The first call is a handshake that only learns the current generation
      // (T:18387-18389); the floor refresh covers what moved inside it.
      const handshake = gen < 0;
      gen = r.generation;
      if (handshake) continue;
      if (r.full) {
        // "Reload everything" includes a server that restarted and counts from
        // zero again: forget our generation FIRST, or the stale-listing guard in
        // `load` would refuse the very read that catches us up, forever.
        //
        // AND NOTHING IS FOLDED IN UNTIL THAT READ LANDS (bugbot, 2026-09-15).
        // The rows still held are the PRE-restart listing, and a delta arriving
        // in the window behind the catch-up GET used to be merged into them AND
        // to write `listingGen` — which then made the catch-up listing itself
        // look older than what was on screen, so it was dropped and the page
        // kept a mixture of pre-restart rows and post-restart deltas until
        // somebody reloaded. The old Tasks-page loop did not have this hole: it
        // refused to merge at all while its generation was `-1`. `catchingUp` is
        // that rule, restored.
        //
        // The pause is the old loop's too. It is not what makes this correct —
        // `catchingUp` is — but it keeps the watcher from spinning a round trip
        // against a server that is still handing out changes while it restarts.
        listingGen = -1;
        catchingUp = true;
        void load();
        await env.sleep(CATCH_UP_SETTLE_MS);
        continue;
      }
      const rows = r.rows || [];
      const gone = r.gone || [];
      // THE DRAFT DELTA IS ANNOUNCED FIRST AND UNCONDITIONALLY. It rides the
      // same answer as the rows but it is not about them: an answer whose rows
      // and `gone` are both empty can still carry a version bump for a record
      // two tabs are open on, and the fold below would `continue` past it.
      emitDrafts(r.drafts);
      if (!rows.length && !gone.length) continue;
      // A DELTA IS ABOUT ROWS WE NO LONGER TRUST. Dropped rather than queued: the
      // listing on its way is read AFTER this change was recorded, so it already
      // contains it, and the floor refresh plus every poke cover the sliver a
      // change can land in between the server's snapshot and its arrival here.
      if (catchingUp) continue;
      if (typeof r.generation === "number") listingGen = r.generation;
      const held = readListing();
      if (held === null) {
        // Nothing to fold into — the first read has not landed, or the last one
        // failed. Ask for the whole thing instead.
        void load();
        continue;
      }
      // THE FOLD IS GUARDED LIKE THE FETCH. Since `syncFeedLane` this loop is
      // the sidebar's only heartbeat too (`fedElsewhere` stands the pulse timer
      // down while the lane is open), so a subscriber that throws, or a row
      // shape the merge cannot take, must not end the loop: it would leave the
      // lane "open" with nobody polling behind it, and the timer would never
      // come back either (regression review, 2026-09-16). One bad answer costs
      // one backoff; the next long-poll and the floor refresh carry on.
      try {
        const merged = mergeTaskChanges(held, rows.filter((t) => !!t && !!t.key), gone);
        rememberListing(merged);
        publishTasks(merged);
        emitListing({ rows: merged, failed: false, delta: { rows, gone } });
      } catch {
        await env.sleep(CHANGES_BACKOFF_MS);
      }
    }
  };

  feedLoad = () => {
    void load();
  };
  void load();
  void watch();

  const unpoke = env.pokes?.(() => {
    if (!stopped) refreshListing();
  });
  const stopFloor = (env.every ?? ((ms, fn) => {
    const id = setInterval(fn, ms);
    return () => clearInterval(id);
  }))(LISTING_FLOOR_MS, () => {
    // A hidden tab has nothing on screen to keep fresh, and `useRefreshOnReturn`
    // (plus the `focus` poke) reads on the way back.
    if (!stopped && !env.hidden()) void load();
  });

  return () => {
    stopped = true;
    feedLoad = null;
    if (abort) {
      abort.abort();
      abort = null;
    }
    // A hidden tab's `watch` is parked on a promise nothing else will resolve.
    waking?.cancel();
    waking = null;
    unpoke?.();
    stopFloor();
  };
}

/**
 * Follow the full `/api/tasks` listing. One poll for the document however many
 * callers there are; it starts with the first and stops with the last.
 *
 * A caller that subscribes while rows are already held is REPLAYED them
 * synchronously — a card mounted five minutes into the page's life must not
 * wear a skeleton until something happens to change.
 *
 * `env` is honoured only from the call that STARTS the feed (bun tests hand one
 * over; the app never does), so a second subscriber cannot swap the transport
 * out from under the first.
 */
export function subscribeListing(
  cb: (ev: ListingEvent) => void,
  env: ListingEnv = browserListingEnv(),
): () => void {
  listingSubs.add(cb);
  // THE REPLAY, AND BEFORE THE FEED STARTS RATHER THAN ONLY FOR LATE ARRIVALS:
  // the listing outlives the feed (`rememberListing`, module scope), so a page
  // that unmounted and came back has a real answer in hand and must not wear a
  // skeleton over it for the length of one more round trip. The read below
  // repaints in place when it lands.
  const held = readListing();
  if (held !== null) cb({ rows: held, failed: false, delta: null });
  else if (listingFailed) cb({ rows: [], failed: true, delta: null });
  if (listingSubs.size === 1) {
    feedStop = startFeed(env);
    // The pulse poll stands down while the feed is live, exactly as it does for
    // a feeder: the listing carries every field the pulse does.
    schedule();
  }
  return () => {
    listingSubs.delete(cb);
    if (listingSubs.size === 0) {
      feedStop?.();
      feedStop = null;
      // AND THE GENERATION GOES WITH IT (bugbot, 2026-09-15). `listingGen` is
      // the guard against a full read that left BEFORE a delta landing after
      // it — a race that only exists inside one running feed. Kept across a
      // teardown it becomes a claim about a server counter this session has
      // stopped following: a server that restarted counts from zero again, so
      // the next feed's very first listing would be "older" than the number we
      // were holding, be dropped, and leave the page on the remembered rows
      // with deltas folding into them for ever. The rows survive
      // (`rememberListing`) because they are still the best answer we have; the
      // number does not, because nothing is left to race it.
      listingGen = -1;
      // AND SO DOES THE FAILURE (bugbot, 2026-09-15). A failed read forgets the
      // rows, so `readListing()` is null and the replay at the top of
      // `subscribeListing` falls through to `{rows: [], failed: true}` — which
      // the Tasks page draws as "could not be loaded" over an empty list,
      // throwing away the provisional rows it had just seeded from the pulse
      // store. That verdict was about a server we have since stopped asking; the
      // new feed's own first read answers for the server as it is NOW, one round
      // trip from here.
      listingFailed = false;
      schedule();
    }
  };
}

/**
 * WHICH DRAFT RECORDS MOVED, and to what version (design §3).
 *
 * Subscribed by the two editors a draft can be open in — the chat composer and
 * the New task card — each for its OWN key. `gone` clears or closes; a
 * `changed` whose version is newer than the one the subscriber holds is re-read
 * and adopted, unless the reader is mid-sentence, in which case the next save's
 * own 409 settles it (`platform/lib/drafts`, `AutosaveOptions.conflict`).
 *
 * Does not start the feed on its own — a side channel on a listing somebody else
 * is already following, exactly like `onGone`.
 *
 * WHAT THIS REPLACED: `App.tsx` used to hear `onGone`, re-read the WHOLE drafts
 * store and mark keys spent — one `GET /api/drafts` per announcement, which a
 * server re-announcing one key turned into hundreds of requests a second on a
 * real machine (the incident `coalesceLatest` was written for). The server now
 * says which keys and at which versions, so there is nothing to look up and
 * nothing to coalesce.
 */
/**
 * THIS RECORD IS GONE — say so NOW, before the server has been asked.
 *
 * `dropListingKeys`' twin, for the draft channel, and it exists for the same
 * reason: the trash on a draft row has to take effect under the pointer. The
 * ROW leaves through that function; this is what reaches the EDITOR that record
 * may also be open in — the composer behind the List, the New task card on top
 * of it — which would otherwise go on showing words the reader has just thrown
 * away until the next long-poll caught up.
 *
 * Optimistic, like its twin: the server's own announcement follows and says the
 * same thing, and a DELETE that failed leaves the record on the server for the
 * next read to find.
 */
export function announceDraftsGone(keys: readonly string[]): void {
  const gone = keys.filter((key) => !!key);
  if (!gone.length) return;
  for (const sub of draftSubs) sub([], [...gone]);
}

export function onDraftChange(
  cb: (changed: { key: string; version: number }[], gone: string[]) => void,
): () => void {
  draftSubs.add(cb);
  return () => {
    draftSubs.delete(cb);
  };
}

/** The keys the server said LEFT, for a reader that has something to clean up
 *  behind a task that is gone (PR C: a composer still holding a deleted
 *  draft's words). Does not start the feed on its own — it is a side channel on
 *  a listing someone else is already following. */
export function onGone(cb: (keys: string[]) => void): () => void {
  goneSubs.add(cb);
  return () => {
    goneSubs.delete(cb);
  };
}

/**
 * THESE ROWS ARE GONE — say so NOW, before the server has been asked.
 *
 * The optimistic half of a discard (PR C: the trash on a draft row). The row
 * the reader just pressed has to leave under the pointer, not after a DELETE
 * and a re-read; and it has to leave EVERYWHERE, because the same draft is a
 * row in the List, a card on the Board and a line in the chat's Recent list,
 * and three surfaces dropping it at three different moments is the flicker the
 * one feed exists to prevent.
 *
 * The held listing is the one place all three read from, so the drop happens
 * there and every subscriber hears one event. It is announced as a `gone`
 * DELTA, exactly as the long-poll would have announced it — so `onGone` fires
 * and the cleanup behind a vanished draft (a composer still holding its words)
 * runs the same way whoever pressed the button.
 *
 * NOT A SUBSTITUTE FOR THE REQUEST. The caller still deletes and still pokes;
 * this only decides what the page shows in between. If the delete fails, the
 * next read puts the row back, which is the right answer — the draft is still
 * there.
 *
 * `listingGen` is deliberately NOT bumped: this is not news from the server and
 * must not make the server's next answer look stale.
 */
export function dropListingKeys(keys: readonly string[]): void {
  const gone = keys.filter((key) => !!key);
  if (!gone.length) return;
  const held = readListing();
  if (held === null) {
    // Nothing on screen to take it off. The cleanup behind the key still has to
    // run, so the event goes out with no rows of its own.
    for (const sub of goneSubs) sub([...gone]);
    return;
  }
  const merged = mergeTaskChanges(held, [], [...gone]);
  if (merged.length === held.length) {
    for (const sub of goneSubs) sub([...gone]);
    return;
  }
  rememberListing(merged);
  publishTasks(merged);
  emitListing({ rows: merged, failed: false, delta: { rows: [], gone: [...gone] } });
}

/**
 * …AND BACK, when the write the drop was optimistic about FAILED.
 *
 * `dropListingKeys` takes a row off the page before the server has been asked.
 * If the DELETE then does not land, the draft is still there — and leaving the
 * page saying otherwise until the next floor refresh is the page lying about
 * what the reader still has (Bugbot #1166). The rows go back into the held
 * listing through the same merge a change-poll uses, so they land in the right
 * order rather than at the end.
 *
 * `listingGen` is untouched for `dropListingKeys`' reason: neither of these is
 * news from the server, and neither may make the server's next answer look
 * stale.
 */
export function restoreListingRows(rows: readonly Task[]): void {
  const back = rows.filter((row) => !!row && !!row.key);
  if (!back.length) return;
  const held = readListing();
  // Nothing is being held, so there is nothing to put a row back INTO — the
  // next read answers with it anyway, which is the state a failed drop wanted.
  if (held === null) return;
  const merged = mergeTaskChanges(held, [...back], []);
  rememberListing(merged);
  publishTasks(merged);
  emitListing({ rows: merged, failed: false, delta: { rows: [...back], gone: [] } });
}

/** "Something just changed — re-read the listing NOW." Collapsed to one read
 *  per tick and one per DOCUMENT; a no-op when nobody is following. */
export function refreshListing() {
  if (listingSubs.size === 0 || refreshQueued) return;
  refreshQueued = true;
  queueMicrotask(() => {
    refreshQueued = false;
    feedLoad?.();
  });
}

/** Whether the listing feed is the one polling `/api/tasks` right now. */
export function listingFeedLive(): boolean {
  return listingSubs.size > 0;
}

/** Test seam: the feed is module state that outlives a `bun test` case, and a
 *  suite that hands over its own `env` needs the next one to start clean. */
export function resetListingFeedForTests() {
  feedStop?.();
  feedStop = null;
  feedLoad = null;
  listingSubs.clear();
  goneSubs.clear();
  listingGen = -1;
  listingFailed = false;
  refreshQueued = false;
  listing = null;
}
