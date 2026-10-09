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
//
// SINCE 2026-10-09 NOTHING HERE POLLS. The rows arrive over the document's
// events-bus socket (`tasks.listing`, platform/lib/events): the server pushes
// a snapshot on subscribe and a delta on every change, and the pulse the
// sidebar reads is derived from those same rows — the compact
// `/api/tasks/pulse` self-poll, its 10/30 s timer, the 20 s floor read and
// the "feeder" contract that kept two pollers from colliding are all gone,
// because there is one subscription and it is the server's job to say when
// something moved (D3).
import { useEffect, useState } from "react";
import { queueEnabled } from "@apps/claude/feature-flag";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import type { FusedEventsCallback, FusedEventsSubscribeOptions } from "@platform/lib/events";
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

let tasks: TaskPulseTask[] = [];
let seen: TasksSeen = readSeen();
let pulse: TasksPulse = EMPTY_TASKS_PULSE;
/** Has a real answer landed? `tasks` is `[]` both before the first read and on a
 *  machine with no tasks, and those two must not be treated alike — see
 *  markTasksSeen, where mistaking one for the other throws away the reader's
 *  dismissals. */
let loaded = false;
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

/**
 * Keep the listing lane in step with the readers: open while anyone reads the
 * pulse or the rows, closed when nobody does. Every publish and every
 * subscribe lands here, so the lane can never outlive its last reader.
 */
function schedule() {
  syncFeedLane();
}

// ---- the lane -----------------------------------------------------------------
//
// "1 running" IN THE RAIL SHOULD BE INSTANT (Akshil, 2026-09-12: "should be
// instant… everywhere in UI"). While a pulse reader is mounted this module
// follows the document's own listing feed (`subscribeListing`, below), which
// the server pushes the moment a session starts, resumes, takes a prompt,
// grows, or any queue verb rings it. The feed publishes every frame through
// `publishTasks`, so the rail moves on the same tick the Tasks page would. A
// page that follows the feed itself (Tasks, a chat) is the same subscription
// refcounted, not a second one.

/** This module's own subscription to the listing feed, or null while the lane
 *  is closed. */
let feedLane: (() => void) | null = null;

function syncFeedLane() {
  const wanted =
    listeners.size + rowListeners.size > 0 &&
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

/**
 * "Something just changed — re-read NOW rather than on the next tick."
 *
 * Called by the surfaces that learn a scheduled run ended before the watcher
 * would: the queue card's job snapshot (ActivityDock) and the schedule's own
 * done/failed events (App wiring useScheduleEvents). Under the bus this is
 * one `resync` of the listing subscription — the server answers with a fresh
 * snapshot — and a no-op when nobody is following the rows (nothing on screen
 * to update).
 */
export function pokeTasks() {
  if (listingSubs.size > 0) refreshListing();
}

/** The localStorage key the chat (apps/claude ClaudeChat `stampChatActivity`)
 *  stamps when an interactive turn starts or ends. Interactive turns create no
 *  sys:schedule job and no schedule event — neither producer above fires for
 *  them — so a follow-up typed into a chat left every tasks surface stale until
 *  its next slow poll (Akshil, 2026-08-19: "the task's unread status does not
 *  update"). Every same-origin document EXCEPT the writer receives a `storage`
 *  event for the stamp, so a Tasks page open in another window hears the turn
 *  for free, with no postMessage and no new endpoint. */
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

/**
 * THE 20 s FLOOR READ, WITH THE ROWS IT DID NOT CHANGE KEPT (2026-10-05, macOS
 * 14 native windows).
 *
 * A full listing is a fresh object per row, and the List's rows are memoised
 * on the row object (`ScheduleTaskViews` `TaskRow`): a fresh object for a task
 * that has not moved is a re-render of a row that has nothing to redraw —
 * seven hundred of them, every twenty seconds, which on Safari 17's engine is
 * the pause the memo exists to remove. Deltas already keep what they did not
 * touch (tasks-lib.mergeTaskChanges folds into the held rows); this gives the
 * full read the same manners. A row whose JSON reads the same as the one held
 * under its key IS the held row; anything else — new key, any changed field,
 * a different field order — is the server's fresh object, which is exactly
 * what every reader got before this.
 *
 * `JSON.stringify` of 700 rows with three messages each is single-digit
 * milliseconds, once per floor read, off the render path.
 */
export function reuseUnchangedRows(held: Task[] | null, next: Task[]): Task[] {
  if (!held || held.length === 0 || next.length === 0) return next;
  const prior = new Map<string, Task>();
  for (const t of held) prior.set(t.key, t);
  let reused = false;
  const rows = next.map((t) => {
    const was = prior.get(t.key);
    if (!was || was === t) return t;
    // THE SERVER'S DIGEST FIRST (2026-10-09, D7): a row carries `row_hash`,
    // a short digest of its own content stamped at build time, so two rows
    // compare as two short strings. The stringify below is the fallback for
    // a row from a server that does not stamp one yet.
    if (was.row_hash && t.row_hash) {
      if (was.row_hash !== t.row_hash) return t;
    } else {
      try {
        if (JSON.stringify(was) !== JSON.stringify(t)) return t;
      } catch {
        return t;
      }
    }
    reused = true;
    return was;
  });
  return reused ? rows : next;
}

/** A poll that FAILED is news about the listing too: what is remembered may
 *  describe a server that has since gone away, and a remount seeding from it
 *  would paint rows over a page that then says "Tasks could not be loaded".
 *  Forgetting makes the next mount start from the skeleton, as a first visit
 *  does (review, #1079). */
export function forgetListing() {
  listing = null;
}

/** Hand over a known-fresh answer — what the listing feed just received. */
export function publishTasks(next: TaskPulseTask[]) {
  tasks = next;
  loaded = true;
  recompute();
  for (const listener of rowListeners) listener(next);
  schedule();
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

/** Subscribe to the summary. The lane opens with the first reader and closes
 *  with the last — nothing is subscribed on behalf of a sidebar nobody has
 *  mounted. */
export function useTasksPulse(): TasksPulse {
  const [current, setCurrent] = useState<TasksPulse>(pulse);
  useEffect(() => {
    listeners.add(setCurrent);
    // The lane opens with the first reader (`schedule` → `syncFeedLane`); a
    // sidebar remounting while a page already follows the feed is the same
    // subscription refcounted, and the cached snapshot is replayed at once.
    schedule();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}

/** Subscribe to the compact rows themselves — `key`, `status`, `project`,
 *  `last_active` — for a reader that groups tasks rather than counts them (the
 *  sidebar's Current apps section, D487). Same store, same lane as
 *  useTasksPulse: this is NOT a second subscription.
 *
 *  `enabled: false` is a reader that does not count — no listener, so no poll
 *  and no listing feed on its behalf. A hook cannot be called conditionally,
 *  so a caller that must not subscribe in some documents (an embed pane:
 *  useTaskStatusNotify) says so here instead. */
export function useTasksPulseRows(enabled = true): TaskPulseTask[] {
  const [rows, setRows] = useState<TaskPulseTask[]>(tasks);
  useEffect(() => {
    if (!enabled) return;
    rowListeners.add(setRows);
    setRows(tasks);
    schedule();
    return () => {
      rowListeners.delete(setRows);
      schedule();
    };
  }, [enabled]);
  return rows;
}

// ── THE LISTING FEED ────────────────────────────────────────────────────────
//
// ONE `tasks.listing` SUBSCRIPTION PER DOCUMENT, however many surfaces want
// the rows. It rides the document's one events-bus socket (platform/lib/events,
// D1), which is outside WebKit's six-connection pool.
//
// Every reader of the full listing used to run its own `GET /api/tasks` +
// `/api/tasks/changes` long-poll pair: the Tasks page, and `apps/claude/
// protocol/sessions.ts` ONCE PER SUBSCRIPTION — once per ClaudeChat mount, and
// a ClaudeChat mounts per card on the Tasks wall. Twelve cards held twelve
// sockets open on a 25-second wait against a browser cap of six per origin.
//
// The store already owned "one poll, many readers" for the pulse, and the
// listing is the same question at a larger width. So the feed lives here,
// refcounted: it opens with the first subscriber and closes with the last,
// and a subscriber that arrives after the rows have landed is REPLAYED the
// current listing synchronously rather than waiting out a change that may
// never come.
//
// WHAT THE SERVER SENDS. A `snap` is the whole listing (`GET /api/tasks`'s
// body); a `delta` is `/api/tasks/changes`' answer — the rows that moved, the
// keys that left, the draft versions that changed — computed server-side from
// the generation THIS document was last sent. Frames arrive in order on one
// socket, so a snapshot is never older than a delta that preceded it, and the
// generation guard the long-poll needed (bugbot #892) has nothing left to
// guard. No floor read exists: a watcher that missed something is a server
// bug (D3), and the server's own builder re-derives truth on its floor.
//
// WHAT A SUBSCRIBER GETS is the whole machine's listing plus the delta that
// produced it, because the two readers narrow it differently: the Tasks page
// filters by its toolbar, the chat's list by pane (`ui/list-rows.taskInPane`),
// and the question "is this change worth repainting MY list" can only be
// answered where the scope is known. `delta === null` means a whole listing —
// a snapshot, a refresh, or a failure — and those always concern everyone.

/** What a `tasks.listing` snapshot carries (`GET /api/tasks`). */
interface ListingSnapshot {
  tasks?: Task[];
  generation?: number;
}

/** What a `tasks.listing` delta carries (`/api/tasks/changes`' answer). */
interface ListingDeltaFrame {
  generation?: number;
  rows?: Task[];
  gone?: string[];
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

/**
 * WHAT A DRAFT SUBSCRIBER IS HANDED, and the third argument is the one worth
 * naming: WHOSE `gone` this is.
 *
 * `false` — the change FEED said so, and its `gone` is the noisy set described
 * above: ignore it for a key you hold no version for.
 *
 * `true` — THIS DOCUMENT said so (`announceDraftsGone`), after watching its own
 * DELETE land. There is no noise in that: the record named is gone, and the
 * subscriber holding it must let it go even though the delete has already
 * forgotten the version that would otherwise vouch for the key.
 */
export type DraftChangeListener = (
  changed: { key: string; version: number }[],
  gone: string[],
  certain: boolean,
) => void;

/** The subscribe the feed rides — `fusedEvents.subscribe`'s shape, so a bun
 *  test can hand over a scripted one and drive frames by hand. */
export type SubscribeLike = (
  topic: string,
  params: Record<string, unknown> | null | undefined,
  cb: FusedEventsCallback<ListingSnapshot, ListingDeltaFrame>,
  opts?: FusedEventsSubscribeOptions,
) => () => void;

/** The pieces the feed reaches for — injectable for bun tests, which run every
 *  suite in ONE process and so cannot module-mock the events client. */
export interface ListingEnv {
  /** The bus subscription (`tasks.listing`, no params: the whole machine). */
  subscribe: SubscribeLike;
  /** Ask the bus for a fresh snapshot of the subscription now. */
  resync(): void;
  /**
   * THE PUSH SIDE of this document's own writes: subscribe `fn` to every local
   * signal that says a chat just moved (a `tasks-changed` event, the chat
   * activity stamp, a focus), and return the disposer. Attached ONCE for the
   * document rather than once per subscriber. Each fires one `resync`.
   */
  pokes?(fn: () => void): () => void;
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
const draftSubs = new Set<DraftChangeListener>();
let listingFailed = false;
/** The running feed's teardown, and its resync, or null when nobody is
 *  subscribed. */
let feedStop: (() => void) | null = null;
let feedLoad: (() => void) | null = null;
/** One resync per tick, not one per poke: `focus`, `storage` and
 *  `tasks-changed` all fire for the same turn ending, and the three used to be
 *  three listing reads. */
let refreshQueued = false;

function emitDrafts(delta: DraftsDelta | undefined) {
  if (!delta) return;
  const changed = Array.isArray(delta.changed) ? delta.changed : [];
  const gone = Array.isArray(delta.gone) ? delta.gone : [];
  if (!changed.length && !gone.length) return;
  // NOT certain: this is the feed's announced key set, which is noisy by
  // construction (contract §3) — see `DraftChangeListener`.
  for (const sub of draftSubs) sub(changed, gone, false);
}

function emitListing(ev: ListingEvent) {
  for (const sub of listingSubs) sub(ev);
  const gone = ev.delta?.gone;
  if (gone && gone.length) for (const sub of goneSubs) sub(gone);
}

function browserListingEnv(): ListingEnv {
  return {
    subscribe: (topic, params, cb, opts) => subscribeTopic(topic, params, cb, opts),
    resync: () => {
      resyncTopic("tasks.listing", {});
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

  const onSnapshot = (snap: ListingSnapshot) => {
    const fresh = (Array.isArray(snap?.tasks) ? snap.tasks : []).filter(
      (t): t is Task => !!t && !!t.key,
    );
    listingFailed = false;
    // Same object for a row the snapshot did not change, so the memoised rows
    // below it stand still (`reuseUnchangedRows`).
    const rows = reuseUnchangedRows(readListing(), fresh);
    rememberListing(rows);
    publishTasks(rows);
    emitListing({ rows, failed: false, delta: null });
  };

  const onDelta = (r: ListingDeltaFrame) => {
    const rows = r.rows || [];
    const gone = r.gone || [];
    // THE DRAFT DELTA IS ANNOUNCED FIRST AND UNCONDITIONALLY. It rides the
    // same frame as the rows but it is not about them: a frame whose rows and
    // `gone` are both empty can still carry a version bump for a record two
    // tabs are open on, and the fold below would return past it.
    emitDrafts(r.drafts);
    if (!rows.length && !gone.length) return;
    const held = readListing();
    if (held === null) {
      // Nothing to fold into — the last snapshot failed. Ask for a whole one.
      env.resync();
      return;
    }
    // THE FOLD IS GUARDED. This feed is the sidebar's only heartbeat, so a
    // subscriber that throws, or a row shape the merge cannot take, must not
    // end it (regression review, 2026-09-16). One bad frame costs one frame.
    try {
      // THE QUEUE'S REKEY IS A FOLD, NOT A DELETE AND AN INSERT (tasks-lib
      // `mergeTaskChanges`, and the identity rule above it). A dispatched
      // message's `pending:<entry>` row leaves in the same frame its session
      // row arrives in, and with the flag up the merge treats the two as one
      // task so the list never shows both and never shows neither.
      const queueOn = queueEnabled();
      const merged = mergeTaskChanges(
        held, rows.filter((t) => !!t && !!t.key), gone, queueOn,
      );
      rememberListing(merged);
      publishTasks(merged);
      emitListing({ rows: merged, failed: false, delta: { rows, gone } });
      // …AND A ROW WE HELD OVER ITS OWN `gone` IS A CLAIM, not news. The merge
      // keeps a dispatched pending row painted rather than leaving a hole, and
      // the only thing that can settle it is the whole listing — asked for
      // here, so the answer is one frame away.
      if (queueOn && gone.some((key) => merged.some((row) => row.key === key))) {
        env.resync();
      }
    } catch {
      // Next frame.
    }
  };

  const off = env.subscribe(
    "tasks.listing",
    {},
    (snap, delta, meta) => {
      if (stopped) return;
      if (meta.error !== undefined) {
        // A listing the server cannot answer is "no rows" to the chat's list
        // and a quiet line on the Tasks page — never an error page, and never
        // rows kept over a server that has since gone away (#1079).
        listingFailed = true;
        forgetListing();
        emitListing({ rows: [], failed: true, delta: null });
        return;
      }
      if (snap !== null) onSnapshot(snap);
      else if (delta !== null) onDelta(delta);
    },
    { hiddenOk: true },
  );

  feedLoad = () => env.resync();
  const unpoke = env.pokes?.(() => {
    if (!stopped) refreshListing();
  });

  return () => {
    stopped = true;
    feedLoad = null;
    unpoke?.();
    off();
  };
}

/**
 * Follow the full `/api/tasks` listing. One subscription for the document
 * however many callers there are; it opens with the first and closes with the
 * last.
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
    // The lane follows: the listing carries every field the pulse does.
    schedule();
  }
  return () => {
    listingSubs.delete(cb);
    if (listingSubs.size === 0) {
      feedStop?.();
      feedStop = null;
      // THE FAILURE GOES WITH IT (bugbot, 2026-09-15). A failed read forgets
      // the rows, so `readListing()` is null and the replay at the top of
      // `subscribeListing` falls through to `{rows: [], failed: true}` — which
      // the Tasks page draws as "could not be loaded" over an empty list,
      // throwing away the provisional rows it had just seeded from the pulse
      // store. That verdict was about a server we have since stopped asking;
      // the new feed's own snapshot answers for the server as it is NOW.
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
  // CERTAIN, and that is the whole difference between this and the feed's own
  // `gone`. This document just deleted these records and watched the DELETE
  // land, so a subscriber must act on its key WHETHER OR NOT it is holding a
  // version for it — which is exactly the case the trash in Recent chats hits:
  // `deleteChatDraft` forgets the version as the record goes (contract §2), so
  // by the time this runs the composer's `draftVersion(key)` is already
  // `undefined` and the noisy-`gone` guard would swallow the one announcement
  // that was never noise (Akshil, 2026-09-16: trashing the row left the box
  // full, and the next keystroke wrote the record straight back at v1).
  for (const sub of draftSubs) sub([], [...gone], true);
}

export function onDraftChange(cb: DraftChangeListener): () => void {
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
 * Not news from the server, and nothing here makes the server's next frame
 * look stale: frames are applied in the order they arrive.
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
  // FLAG-OFF MERGE ON PURPOSE, whatever the switch says: this is the page taking
  // a row off BEFORE the server has been asked (a draft discarded, a task
  // erased), and the queue's "hold a dispatched pending row" rule would paint
  // the very row the reader just deleted as a run that had started.
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
 * Not news from the server either, for `dropListingKeys`' reason.
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

/** "Something just changed — ask for a fresh snapshot NOW." Collapsed to one
 *  resync per tick and one per DOCUMENT; a no-op when nobody is following. An
 *  event, not a timer: the server pushes every change it sees, this covers the
 *  sliver between a write this document just made and the watcher's tick. */
export function refreshListing() {
  if (listingSubs.size === 0 || refreshQueued) return;
  refreshQueued = true;
  queueMicrotask(() => {
    refreshQueued = false;
    feedLoad?.();
  });
}

/** Whether the listing feed is subscribed right now. */
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
  listingFailed = false;
  refreshQueued = false;
  listing = null;
}
