// The React seam for the scheduled-message block (inventory 05 §C). The RULES
// are in `scheduled.ts` and none of them are here: this owns the poller's life,
// the three pieces of page state the banner reads (armed / refused / stopping),
// the `/api/tasks` row cache, and the two document-level gestures that disarm a
// half-pressed stop.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelScheduledMessage,
  getSchedule,
  getTasks,
} from "@platform/lib/api";
// THE ONE `/api/tasks` + `/api/tasks/changes` LOOP THIS DOCUMENT RUNS
// (`shell/tasksPulse`, one of the four shell modules an app may import —
// scripts/check-boundaries.mjs). Subscribed rather than polled: the server rings
// that long-poll the instant the queue manager moves anything
// (`tasks_watch.notify`), which is how the Tasks page repaints a queued row in
// ~100ms while this pane sat on a 3-15s schedule lap and a 5s row-read floor.
import { subscribeListing } from "@shell/tasksPulse";
import type { ChatController } from "../protocol/controller-api";
import { useProjectQueueEnabled } from "../feature-flag";
import { waitingFor } from "./waiting";
import {
  BLOCKED_PLACEHOLDER,
  createScheduleWatcher,
  NAV_LOCKED_REASON,
  schedBlockReason,
  schedCalendarHere,
  schedFindTask,
  schedIsRepeat,
  schedPendingOnly,
  schedSameRow,
  schedStopTarget,
  SCHEDULE_URL,
  SCHEDULE_VIEW_KEY,
  type SchedEntry,
  type SchedTask,
} from "./scheduled";

export interface UseScheduleOptions {
  controller: ChatController;
  file: string | null;
  /** The session on screen, `""` on the landing page. */
  sessionId: string;
  /**
   * THE ENTRY A SESSION-LESS CHAT'S MESSAGES ARE GROUPED UNDER
   * (`sched/queue-leader`), `""` for everything else.
   *
   * A chat whose first message was queued has no session for anything to name,
   * so its waiting rows, its `/api/tasks` row (`pending:<leader>`) and the card
   * over its composer all hang off this id instead. Held by the chat rather than
   * here because it is written in the send window, one tick before any render.
   */
  leaderId?: string;
  /** False on the landing page: nothing can be pending in a conversation that
   *  does not exist yet, and there is nothing to render a turn into. */
  inChat: boolean;
  /** A mode holds the way out (T:17242 `annNavLocked`) — the calendar button's
   *  OTHER reason for being off, which never disables the box. */
  navLocked?: boolean;
  /** Put a bottom-pinned transcript back at the bottom on the hidden → shown
   *  edge (T:17198-17213). */
  followBottom?(): void;
  /** The shell hop the row makes. */
  onNavigate?(url: string): void;
  /** `params.set("run", id, {history:"replace"})` — a fired scheduled run goes
   *  on the URL for the same reason a send's does: a reload, or a mode switch
   *  that remounts this frame, re-attaches from the param. Without it the
   *  stream is simply lost, and the next frame's baseline then writes the same
   *  run off as predating it (T:17437). */
  setRunParam(runId: string): void;
  /**
   * THE PROJECT QUEUE'S SWITCH (prefs `queue.enabled`), injectable so a suite
   * can drive both sides of it without touching a process-global. Omitted, it
   * is the same subscribed pref every chat embed already pays one `/api/prefs`
   * GET for (`feature-flag`), so asking here costs nothing.
   */
  queueEnabled?: boolean;
  /**
   * The three endpoint calls, injectable — and injectable rather than
   * module-mocked for a reason worth recording: `bun test` runs every suite in
   * ONE process, so a `mock.module("@platform/lib/api", …)` replaces that
   * module for every suite loaded AFTER it, and the first casualty was the
   * task-number hook two files along. A seam costs three lines; a global
   * replacement costs whoever runs next.
   */
  api?: ScheduleApi;
  /**
   * The poll's timers, injectable for the same reason the three endpoint calls
   * are: the alternative is a suite that waits 15 real seconds, or one that
   * replaces a global for every suite loaded after it. Forwarded straight to
   * `createScheduleWatcher`, which has taken them from the start.
   */
  /**
   * THE WALL CLOCK the row's own re-read is paced by (`REC_REFRESH_MS`),
   * injectable for the timers' reason: a suite that proved the floor with the
   * real clock would sleep five seconds per assertion.
   */
  now?: () => number;
  timers?: {
    setInterval(fn: () => void, ms: number): unknown;
    clearInterval(handle: unknown): void;
  };
  /**
   * THE TASKS CHANGE FEED, injectable for the three endpoint calls' reason: the
   * real one is a module-global long-poll shared by the whole document, and a
   * suite that drove it would be driving it for every suite loaded after it.
   * Omitted, it is `shell/tasksPulse.subscribeListing` — the feed the Tasks page
   * and Recent chats already read, refcounted to one socket per document.
   */
  subscribeRows?(cb: (rows: SchedTask[]) => void): () => void;
}

export interface ScheduleApi {
  getSchedule(): Promise<{ entries?: SchedEntry[] }>;
  getTasks(): Promise<{ tasks?: SchedTask[] }>;
  cancelScheduledMessage(id: string): Promise<unknown>;
}

/** ONE empty array, reused: the block's list is compared by identity by every
 *  consumer downstream of it, and a fresh `[]` every render would re-render the
 *  composer's whole column four times a minute for a card that is not drawn. */
const EMPTY_ROWS: SchedEntry[] = [];

/**
 * THE FLOOR BETWEEN TWO `/api/tasks` READS FOR THIS CHAT'S OWN ROW (🔴 review
 * 2026-09-12).
 *
 * The row is re-read because the queue's fields move under a fixed entry id —
 * the task in front finishes, somebody skips ahead — and it used to be re-read
 * once per SCHEDULE POLL LAP. That rate is 3 s while the composer is shut, so a
 * chat sitting behind a blocker asked the whole tasks listing twenty times a
 * minute for a handful of fields that change when a FOLDER does.
 *
 * 5 s is the floor rather than the interval: the effect still only runs on a
 * tick, so a slow lap (15 s) re-reads at its own pace and a fast one is capped
 * here. Low enough that "behind TASK-038" cannot linger visibly after the folder
 * frees — the long-poll's own wake-ups land inside it.
 */
const REC_REFRESH_MS = 5000;

const PLATFORM_API: ScheduleApi = {
  getSchedule: () => getSchedule() as Promise<{ entries?: SchedEntry[] }>,
  getTasks: () => getTasks() as unknown as Promise<{ tasks?: SchedTask[] }>,
  cancelScheduledMessage: (id) => cancelScheduledMessage(id),
};

/**
 * THE DOCUMENT'S ONE LISTING FEED, narrowed to "here are the rows".
 *
 * A FAILED READ IS DROPPED ON THE FLOOR rather than published as "no rows", for
 * `protocol/sessions`' reason: the feed answers a dead `GET /api/tasks` with
 * `{rows: [], failed: true}` because it has forgotten its listing, and folding
 * that in would take this chat's queue caption down over a read that failed
 * rather than over a queue that moved.
 */
function platformRows(cb: (rows: SchedTask[]) => void): () => void {
  return subscribeListing((ev) => {
    if (ev.failed) return;
    cb(ev.rows as unknown as SchedTask[]);
  });
}

/**
 * IS THIS THE SAME ANSWER ABOUT THIS CHAT? The feed fires for every row on the
 * machine — a `claude` typed in a terminal three folders away is a listing
 * event — so a blind `setState` per answer would re-render the whole chat
 * several times a second on a busy box, and bump `recGen`, which Run next's
 * optimistic claim is measured against.
 *
 * Compared on THE FIELDS THIS PANE DRAWS and nothing else: the key (a queued
 * chat rekeys from `pending:<leader>` onto its session the moment it runs), the
 * status word, every queue field and the number. A title edit, a
 * new message on the row, an unread count — none of those are this hook's.
 */
export function sameQueueRow(a: SchedTask | null, b: SchedTask | null): boolean {
  if (!a || !b) return a === b;
  return (
    a.key === b.key &&
    a.status === b.status &&
    a.task_id === b.task_id &&
    a.queue_position === b.queue_position &&
    a.queue_ahead === b.queue_ahead &&
    a.queue_ahead_title === b.queue_ahead_title &&
    a.queue_ahead_session === b.queue_ahead_session &&
    a.queue_ahead_target === b.queue_ahead_target &&
    a.queue_priority === b.queue_priority &&
    a.queue_waiting === b.queue_waiting &&
    a.queue_blocking === b.queue_blocking
  );
}

export interface ScheduleState {
  /**
   * WHAT THE SCHEDULED-MESSAGE BLOCK DRAWS — and under the project queue that is
   * NOTHING AT ALL.
   *
   * The block was one card explaining why the box was shut. Under the flag the
   * same fact is drawn as the message itself: a dashed bubble at its place in the
   * transcript with one line under it (`ui/Waiting`), plus one summary card over
   * the composer. Two shapes for one fact, a few pixels apart, is exactly what
   * browser QA sent back on 2026-09-12 — so the block does not draw a SECOND
   * thing beside them, it draws nothing, and `blockers` is empty.
   *
   * FLAG OFF IT IS MAIN'S, byte for byte: every pending message aimed here.
   */
  blockers: SchedEntry[];
  /**
   * EVERY pending message aimed at this conversation, whatever put it there —
   * what the waiting rows are drawn from, and the reason a reload shows the same
   * picture as the send did.
   *
   * The SAME list the block used to take; the difference is who draws it.
   */
  waitingHere: SchedEntry[];
  /** EVERY entry the last successful poll saw, whole — pending, claimed, and
   *  already run. What `waitingFor` asks its question of: the rows that have run
   *  carry the only link between this conversation's session id and the entry
   *  its followers name. Null before any poll has answered, for `pendingIds`'
   *  reason: an empty list there would read as "they already went". */
  allRows: SchedEntry[] | null;
  /** Is the box shut — the ONE answer the composer and the calendar button both
   *  read, so they can never disagree about what this chat is doing.
   *
   *  FLAG OFF: any pending message aimed here. UNDER THE QUEUE: only a message
   *  the READER SCHEDULED (`schedIsCalendar` — no `origin`), because that is a
   *  turn the scheduler is about to start in this session and a line typed over
   *  it would be two messages racing into one run. A chat-origin entry is the
   *  reader's own line, already admitted into this conversation's own order, and
   *  it never shuts anything. */
  blocked: boolean;
  /** Why, in one sentence, for the banner AND for the disabled button's tooltip
   *  and spoken name. `""` when nothing is off. */
  reason: string;
  placeholder: string;
  /** The calendar button is off for the block OR for the mode's lock. */
  schedDisabled: boolean;
  rec: SchedTask | null;
  /**
   * THIS CHAT'S OWN `/api/tasks` ROW, LIVE — off the change feed rather than off
   * a lap (`shell/tasksPulse`), and NOT gated on there being a waiting entry the
   * way `rec` is.
   *
   * `rec` answers "what is in front of the message at the head of this chat's
   * line", so it is published only while the row still belongs to that entry
   * (T:16997-16999) and it goes null the instant the entry does. The HEADER is
   * asking a different question — "how is this conversation doing" — and it has
   * to be able to say `in_progress` in the same paint the queued word leaves in.
   * Null until the feed has answered, and on every flag-off chat.
   */
  row: SchedTask | null;
  /** HOW MANY ROWS HAVE BEEN READ, counting from mount — the clock an optimistic
   *  claim is measured against. A caller that painted something the row in hand
   *  cannot know yet (Run next) remembers this number and holds its claim until
   *  it changes, which is exactly "until the server has answered again". */
  recGen: number;
  armed: boolean;
  refused: boolean;
  stopping: boolean;
  /** `Date.now()` as of the last poll that saw a row, so the banner's when-text
   *  is computed against the current clock rather than the one the entry last
   *  changed on. 0 before any such poll — nothing is drawn then. */
  tick: number;
  /** Every pending entry id the last successful poll saw, whatever conversation
   *  it belongs to, or null before any has answered — the queue chips' liveness.
   *  See the state of the same name for why null is not an empty set. */
  pendingIds: ReadonlySet<string> | null;
  /** Entry id → the Claude session its run opened, for every entry that has
   *  reported one (`scheduled.schedRanSessions`). Null before any successful
   *  poll. THE ONE ROAD a chat whose first message was queued has to its own
   *  session id — see the state of the same name. */
  ranSessions: ReadonlyMap<string, string> | null;
  onStop(): void;
  onRow(): void;
  cardRef: React.MutableRefObject<HTMLDivElement | null>;
  /** T:16776 — the visible conversation was REPLACED. */
  reset(): void;
  /**
   * ASK THE SCHEDULE NOW, rather than at the end of the current 15 s lap.
   *
   * For a press that CHANGED the schedule from this pane — the chip's Cancel is
   * the one caller — where waiting a lap means the poll's answer (and everything
   * derived from it: `blockers`, `pendingIds`) describes a world the reader has
   * already left. Fire and forget: the tick publishes through the same
   * `onBlockers`/`onPending` every other lap does, and a failing one fails open
   * exactly as it always has.
   */
  refresh(): void;
}

export function useSchedule(opts: UseScheduleOptions): ScheduleState {
  const { controller, file, sessionId, inChat, navLocked, followBottom, onNavigate } = opts;
  /** "" for every chat that has a session, which is all of them but one. */
  const leaderId = opts.leaderId ?? "";
  /** Read at CALL time: the poller and the row cache both outlive any one
   *  render, and neither may rebuild for a new callback identity. */
  const api = opts.api || PLATFORM_API;
  const hooks = useRef({ setRunParam: opts.setRunParam, controller, api });
  hooks.current = { setRunParam: opts.setRunParam, controller, api };
  /** Read at watcher-BUILD time rather than call time, and through a ref so the
   *  memo below keeps its one honest dependency. A fixed seam: handed in once
   *  by a suite, never swapped mid-life. */
  const timers = useRef(opts.timers);
  /** …and the change feed, read at build time for the same reason. */
  const rowsFeed = useRef(opts.subscribeRows ?? platformRows);

  /** THE POLL'S OWN ANSWER, unfiltered — every pending message aimed at this
   *  conversation. What the block DRAWS is derived from it below. */
  const [allBlockers, setAllBlockers] = useState<SchedEntry[]>([]);
  /**
   * The `/api/tasks` answer AND the entry id it was read for, in one state cell
   * — T's `schedTaskRow` + `schedTaskRowFor` pair (T:16997-16999). Held together
   * because `schedTaskRec` reads them together: a cache belonging to the message
   * that WAS blocking must not label the one that is, so the row is published
   * only while its id is still the id at the front of the queue.
   */
  const [recRow, setRecRow] = useState<{
    id: string;
    task: SchedTask | null;
    /** WHEN this row's read FINISHED, in ms (`Date.now()`) — a WALL CLOCK and
     *  not the poll lap it used to be (🔴 review 2026-09-12). The lap tied the listing's
     *  cost to the schedule's rate, and that rate is 3 s while the composer is
     *  shut: a chat sitting behind a blocker asked `/api/tasks` twenty times a
     *  minute for fields that move when a FOLDER does. The clock caps it at one
     *  read per `REC_REFRESH_MS` however fast the poll runs, and keeps the row
     *  fresh on a slow lap as well. */
    at: number;
    /** Bumped on every write, so a caller can tell "the same row again" from "a
     *  fresh answer that happens to say the same thing". */
    gen: number;
  }>({
    id: "",
    task: null,
    at: 0,
    gen: 0,
  });
  /** Keyed by ID, not a class on the button, for the reason the refusal is: the
   *  15 s poll re-renders this card, and an armed button that quietly disarmed
   *  itself a few seconds after the press would be worse than no confirm at all
   *  (T:16872-16882). */
  const [armedId, setArmedId] = useState("");
  /** The entry whose cancel came back refused (it was already away). Kept by ID
   *  so the sentence belongs to a message rather than to the banner, and so the
   *  reconciling poll re-renders it rather than wiping it (T:16868-16871). */
  const [refusedId, setRefusedId] = useState("");
  const [stopping, setStopping] = useState(false);
  /**
   * THE CLOCK THE LAST POLL SAW, and the whole reason it is page state:
   * `schedWhenText` is a function of the WALL CLOCK, not of the entry — a
   * pending row crosses from "14:00 today" to "any moment now" with nothing
   * about the entry changing at all. T gets that for free because it repaints
   * the whole card on every tick (T:17088); `absorb` below does not, and must
   * not, because the identity dedupe it keeps is what stops a caller with an
   * unstable controller from re-rendering in a loop. So the dedupe stays and
   * the clock is published beside it — a timestamp rather than a bare counter,
   * so the banner READS it instead of merely being re-rendered by it.
   */
  const [tick, setTick] = useState(0);
  const cardRef = useRef<HTMLDivElement | null>(null);

  /** SUBSCRIBED, not read once: the one `/api/prefs` answer may still be in
   *  flight when this mounts, and a composer that learned the flag only on its
   *  next navigation would sit shut for a whole conversation over a message the
   *  queue would have taken. An injected value wins, for the suites. */
  const queuePref = useProjectQueueEnabled();
  const queueOn = opts.queueEnabled ?? queuePref;

  // Read by the poller, which outlives any one render.
  const live = useRef({ sessionId, inChat, leaderId, queueOn });
  live.current = { sessionId, inChat, leaderId, queueOn };

  /**
   * SAME LIST, SAME OBJECT. The poll answers every 15 s and hands back a fresh
   * array each time; published blindly, that is a re-render of the composer's
   * whole column four times a minute for no change at all — and, for a caller
   * whose controller identity is not stable, a render loop.
   *
   * Compared on WHAT THE BANNER DRAWS — the id order AND every field any cell
   * of the card reads (`schedSameRow`) — rather than on identity alone. The id
   * order is not enough: a re-issued `due` would never land at all, and neither
   * would a `message` edited on the Tasks page or an entry that gains or loses
   * its repeat-ness.
   *
   * AND THE CLOCK IS PUBLISHED REGARDLESS, because the entry is only half of
   * what the row draws: `schedWhenText` crosses from "14:00 today" to "any moment
   * now" at the due boundary (T:16921-16931) with every field of the entry
   * unchanged, so a dedupe on the entry alone froze that cell for the life of
   * the pendency. Only while there IS a row — the point of the dedupe is that an
   * open composer's column does not re-render four times a minute for nothing,
   * and an empty schedule still draws nothing at all.
   */
  const absorb = useCallback((rows: SchedEntry[]) => {
    if (rows.length) setTick(Date.now());
    setAllBlockers((prev) => {
      const same =
        prev.length === rows.length && prev.every((e, i) => schedSameRow(e, rows[i]));
      return same ? prev : rows;
    });
  }, []);

  /**
   * EVERY pending entry id the last successful tick saw (scheduled.ts
   * `onPending`) — the queue chips' liveness, and the one thing `blockers`
   * cannot answer for them: that list is filtered by SESSION, and the chat that
   * most needs a chip is the brand-new one that has no session yet.
   *
   * NULL until the first such tick, which is what keeps a chip up across the
   * window between "the send queued" and "the poller has looked". An empty set
   * there would read as "it already went" and take the chip straight back down.
   */
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string> | null>(null);
  const absorbPending = useCallback((ids: string[]) => {
    setPendingIds(new Set(ids));
  }, []);

  /** …AND THE ROWS THEMSELVES, ALL OF THEM (scheduled.ts `onAllRows`). A chat
   *  with no session has no session-filtered list — the filter needs a session —
   *  so its waiting messages are found in here through the entry they follow,
   *  and the entry that has already RUN is what ties that group to the session
   *  the chat has just adopted. Deduped on the same shape `absorb` compares, so
   *  an unchanged schedule does not re-render the composer's column four times a
   *  minute. */
  const [allRows, setAllRows] = useState<SchedEntry[] | null>(null);
  /** HOW MANY TIMES THE SCHEDULE HAS ANSWERED, counting from mount — the lap the
   *  row read is re-armed on. `tick` cannot do that job: it is only stamped when
   *  a lap saw a row for THIS session, and a chat with no session (its first
   *  message queued) has none by construction, so its row would be read once and
   *  then never again. This counts ANSWERS, which is what "ask again" means. */
  const [lap, setLap] = useState(0);
  const absorbAllRows = useCallback((rows: SchedEntry[]) => {
    setLap((n) => n + 1);
    setAllRows((prev) => {
      const same =
        !!prev &&
        prev.length === rows.length &&
        prev.every((e, i) => schedSameRow(e, rows[i]));
      return same ? prev : rows;
    });
  }, []);

  /**
   * WHICH SESSION EACH RUN ENTRY OPENED — and the only way a chat whose first
   * message was queued ever learns its own.
   *
   * That chat has no session: nothing of its has run, so every later send joins
   * the first entry as a follower (`sched/queue-leader`). When the scheduler
   * finally runs the leader, the run opens a session and the entry records it;
   * without reading it back off the entry the chat stays session-less for ever
   * — followers behind a leader that has already gone, and a transcript that
   * shows none of what it said. `ClaudeChat` adopts it through the same
   * `openSession` every other "open that conversation" gesture spends.
   *
   * DEDUPED ON CONTENT, unlike `pendingIds`. Its reader is an EFFECT that opens
   * a conversation, not a filter: a fresh Map four times a minute would re-run
   * that effect for a fact that did not change, and the entry that matters here
   * stops changing the moment it has run.
   */
  const [ranSessions, setRanSessions] = useState<ReadonlyMap<string, string> | null>(null);
  const absorbSessions = useCallback((next: Map<string, string>) => {
    setRanSessions((cur) => {
      if (cur && cur.size === next.size) {
        let same = true;
        for (const [id, sid] of next) {
          if (cur.get(id) !== sid) {
            same = false;
            break;
          }
        }
        if (same) return cur;
      }
      return next;
    });
  }, []);


  /**
   * WHAT THE BLOCK DRAWS UNDER THE QUEUE: nothing.
   *
   * One queued message used to be TWO cards a few pixels apart in two
   * vocabularies — a chip saying "Queued · #1 in line · behind TASK-006" and this
   * block, directly beneath it, saying "Blocked — a scheduled message runs in
   * this chat … Cancel this message" about the very same entry, disagreeing about
   * whether anything was blocked at all (Akshil, browser QA 2026-09-12). The
   * first fix filtered the block's list; the second is this one, and it is the
   * honest version of the same decision: under the flag the waiting messages are
   * drawn as messages (`ui/Waiting`) and summarised once over the composer, so
   * there is no third place for the same fact to be said in.
   *
   * FLAG OFF THE BLOCK IS MAIN'S, byte for byte.
   */
  /**
   * FLAG OFF, THE LIST IS PENDING-ONLY — main, byte for byte.
   *
   * The poller widened `schedPendingHere` to `schedIsWaiting` (pending OR
   * `sending`) so the queue's ROWS would not blink out for the second between a
   * claim and the turn appearing. The same list is what flag-off reads, so the
   * widening reached a composer main never shut: an entry the scheduler had just
   * claimed briefly blocked the box, with the banner naming a message that was
   * already on its way (regression found 2026-09-12).
   *
   * So the flag-off road narrows it back at the seam rather than the poller
   * narrowing it for everybody: `sending` is genuinely a row the queue draws.
   * Every flag-off consumer below — the block, `waitingHere`, `next`, `hasCard`
   * and therefore `blocked` — reads this one.
   */
  const pendingBlockers = useMemo(
    () => (queueOn ? allBlockers : schedPendingOnly(allBlockers)),
    [allBlockers, queueOn],
  );
  const blockers = useMemo(
    () => (queueOn ? EMPTY_ROWS : pendingBlockers),
    [pendingBlockers, queueOn],
  );
  /** THE ROWS THE CHAT DRAWS — the poll's own answer, unfiltered by anything this
   *  module decides. Named apart from `blockers` because they are no longer the
   *  same question: one is "what is waiting", the other is "what is this card
   *  drawing". */
  const waitingHere = useMemo(
    () => (queueOn ? waitingFor(allRows, sessionId, leaderId) : pendingBlockers),
    [queueOn, allRows, sessionId, leaderId, pendingBlockers],
  );
  /** …and the half of it the READER scheduled, which is the only half that still
   *  shuts the box (see `blocked`). */
  const calendarHere = useMemo(() => schedCalendarHere(pendingBlockers), [pendingBlockers]);

  /** THE ENTRY EVERYTHING DOWNSTREAM IS ABOUT — the soonest message waiting for
   *  this conversation, whether or not any card is drawing it. `rec` is fetched
   *  for it (the chat's own task row, which carries the queue's fields), and flag
   *  off it is the block's own row exactly as before. */
  const next = waitingHere[0] ?? null;
  const nextId = next ? String(next.id) : "";
  /**
   * WHAT THE CHAT STILL PAYS FOR EVEN WITH NO CARD DRAWN: the `/api/tasks` row.
   *
   * It is no longer decoration on a banner. Under the queue that row is where
   * "1st in line · behind TASK-038" comes from — the queue fields the server puts
   * on this conversation's own task — and it is the reason a reload draws the
   * same sentence the send did. `hasCard` is therefore "is anything of this
   * conversation's waiting", which is exactly what it always was, and the fetch
   * and the bottom-pinning correction below both still hang off it.
   *
   * THE LANDING PAGE IS NEVER ANY OF THIS, and that is asserted HERE rather than
   * trusted from the poller: `schedPendingHere` already answers `[]` with no
   * session, but that answer only arrives on a TICK — so leaving a blocked chat
   * by Back left the home composer shut, with the banner (which only ever draws
   * inside a chat) not there to say why, for up to a poll interval (Bugbot
   * PR #1075).
   */
  const hasCard = waitingHere.length > 0 && (!!sessionId || !!leaderId);
  // ── the poller ────────────────────────────────────────────────────────────
  //
  // Rebuilt only for a new TARGET: the session and the view are read through
  // the ref above, so entering a chat cannot restart the baseline. Its
  // `resetForNewTranscript` is what the session change calls instead.
  const watcher = useMemo(
    () =>
      createScheduleWatcher({
        file,
        fetchSchedule: () => hooks.current.api.getSchedule(),
        sessionId: () => live.current.sessionId,
        inChat: () => live.current.inChat,
        busy: () => hooks.current.controller.isBusy(),
        onBlockers: absorb,
        // The queue's three feeds are ALWAYS wired and gated AT CALL TIME on
        // the live flag: each is a fresh Set/Map per lap and a re-render of the
        // whole chat, which an idle flag-off chat must not pay (review,
        // 2026-09-16) — but the watcher is built once, so a gate at build time
        // would never learn a later flip (Bugbot). `live.current` is what the
        // poller already reads for the session and the chat state.
        onPending: (ids) => {
          if (live.current.queueOn) absorbPending(ids);
        },
        onAllRows: (rows) => {
          if (live.current.queueOn) absorbAllRows(rows);
        },
        onSessions: (map) => {
          if (live.current.queueOn) absorbSessions(map);
        },
        addNote: (text) => hooks.current.controller.addNote(text),
        setRunParam: (runId) => hooks.current.setRunParam(runId),
        resumeRun: (runId) =>
          hooks.current.controller.resumeRun(runId, { neverShown: true }),
        shownRun: (runId) => hooks.current.controller.hasShownRun(runId),
        ...(timers.current
          ? {
              setInterval: timers.current.setInterval,
              clearInterval: timers.current.clearInterval,
            }
          : {}),
      }),
    // THE TARGET AND NOTHING ELSE. Every other dep is read at CALL time through
    // the two refs above, because a watcher rebuilt mid-life would re-baseline —
    // and the baseline is what stops a reload from re-rendering turns history
    // has already restored.
    [file, absorb, absorbPending, absorbAllRows, absorbSessions],
  );
  useEffect(() => watcher.start(), [watcher]);

  /**
   * A CANCEL IN FLIGHT BELONGS TO THE TRANSCRIPT IT WAS PRESSED IN. Bumped by
   * `reset`, read by `onStop`'s continuation: without it a replaced transcript
   * left `stopping` true until the request returned — which is a DISABLED stop
   * button on the next conversation's banner — and the continuation then went
   * on to write a refusal, or a `blockers` edit, against a list that is no
   * longer the same list (Bugbot PR #1075).
   */
  const stopGen = useRef(0);

  /** T:16776-16786. Cleared LOCALLY as well as in the watcher: unblocking is the
   *  safe direction to be briefly wrong in, and the poll fired underneath
   *  re-establishes the block for this session immediately. */
  const reset = useCallback(() => {
    stopGen.current += 1;
    setArmedId("");
    setRefusedId("");
    setStopping(false);
    setRecRow((cur) => ({ id: "", task: null, at: 0, gen: cur.gen + 1 }));
    watcher.resetForNewTranscript();
  }, [watcher]);

  /** The poller's own tick, on demand — `watcher.tick()` is public and
   *  re-entrant-safe (it publishes and re-arms exactly as a timed lap does), and
   *  `resetForNewTranscript` has always spent it this way. */
  const refresh = useCallback(() => {
    void watcher.tick();
  }, [watcher]);

  // ── the row's number and name ─────────────────────────────────────────────
  //
  // TASK-nnn, the task's name and its state are NOT in `/api/schedule` — only
  // `/api/tasks` hands the numbers out. So the row is filled from two reads: the
  // schedule says what is pending and when (and is the only thing the BLOCK
  // depends on), the tasks listing says what to call it. Fetched once per
  // blocking entry and AFTER the card is on screen, and FAILING OPEN: an
  // unreadable listing costs the number and the state, never the row.
  // THE ID IS THE KEY and the entry is read through a ref: the poll hands back
  // a fresh object every 15 s, so keying on the entry itself would refetch the
  // whole tasks listing on every tick — which is exactly what T's
  // `schedTaskRowFor` cache exists to prevent.
  const nextRef = useRef<SchedEntry | null>(next);
  nextRef.current = next;
  /** …and the clock the floor is measured on, through a ref for `nextRef`'s
   *  reason: a caller passing a fresh closure must not re-arm the effect. */
  const nowRef = useRef<() => number>(opts.now ?? Date.now);
  nowRef.current = opts.now ?? Date.now;
  /** T's `schedTaskRowBusy` (T:17006). ONE listing in flight at a time — and a
   *  read this refuses is not a read lost: the effect re-arms on the next poll's
   *  `tick`, exactly as T's re-render does, so the id that lost the race is
   *  filled a beat later instead of racing the winner to `setRecRow`. */
  const rowBusy = useRef(false);
  useEffect(() => {
    // A HALF-PRESSED STOP AND A REFUSAL BOTH BELONG TO THE MESSAGE THEY WERE
    // MADE AGAINST (T:17091-17097 on hide, T:17146 when a different entry
    // arrives). Display already keys both on `nextId`, so nothing is visibly
    // wrong today — but a blocker that leaves the front and comes back would
    // find its destructive button armed with no press behind it, which is the
    // one direction this control must never be wrong in.
    setArmedId("");
    setRefusedId("");
  }, [nextId]);
  useEffect(() => {
    // NEVER BEFORE THE CARD (T:17258). The listing only DECORATES the row, so
    // it is read after the card is on screen and not at all when there is no
    // card — which is what leaving the chat by Back is: `hasCard` goes false on
    // that paint while the blockers are still in hand, and T spends nothing
    // there. Asked of the card and not of the block, because under the project
    // queue the card is drawn with the box wide open and a row that never
    // learned its number would read as a task the server had lost.
    if (!hasCard || !nextId) return;
    // ONE READ PER BLOCKING MESSAGE (T:17006). The 15 s poll re-runs this
    // effect through `tick`; the id already fetched for is not fetched again.
    //
    // UNDER THE QUEUE THAT CACHE IS WRONG, and it is wrong in the one direction
    // that matters: the queue's fields (`queue_ahead`, `queue_position`,
    // `queue_priority`, `queue_waiting`) change UNDER A FIXED ENTRY ID every time
    // the folder moves — the task in front finishes, somebody else skips ahead,
    // Run next is pressed — so a row read once said "behind TASK-038" for the
    // life of the chat while the folder had long since freed (Bugbot PR #1124).
    //
    // SO IT IS RE-READ ON A CLOCK OF ITS OWN, not once per poll lap (🔴 review
    // 2026-09-12). The lap looked like the same cadence the rows are drawn at,
    // and it is not: the schedule poll runs at 3 s while the composer is shut,
    // which made a chat sitting behind a blocker ask `/api/tasks` twenty times a
    // minute. `REC_REFRESH_MS` is the floor between two reads whatever the poll
    // is doing — and it still re-reads on a SLOW lap, because the effect re-runs
    // on every tick and the clock, not the tick, is what says "again".
    const stale = queueOn && nowRef.current() - recRow.at >= REC_REFRESH_MS;
    if (rowBusy.current || (recRow.id === nextId && !stale)) return;
    const id = nextId;
    rowBusy.current = true;
    void (async () => {
      try {
        const data = await hooks.current.api.getTasks();
        // The entry may have been stopped while this was in flight. T's
        // `schedTaskRec` throws that answer away by comparing ids at READ time;
        // storing the id beside the row does the same job here.
        setRecRow((cur) => ({
          id,
          task: schedFindTask(
            data.tasks,
            nextRef.current,
            live.current.sessionId,
            live.current.leaderId,
          ),
          // STAMPED WHERE THE READ ENDED, not where it began (Bugbot PR #1124).
          // Taken at the start, a listing that took longer than `REC_REFRESH_MS`
          // was already stale by the time it landed — the effect re-ran on the
          // new `at`, read `stale`, and fired again immediately: a tight loop of
          // whole-listing reads, and the slower the machine the tighter it got.
          // The floor is a floor between two reads, so it is measured from the
          // end of one.
          at: nowRef.current(),
          gen: cur.gen + 1,
        }));
      } catch {
        // No id recorded, so the next poll tries again (T:17002-17004).
      } finally {
        rowBusy.current = false;
      }
    })();
    // `lap` stays in the deps and is no longer the RULE: it is what re-runs this
    // effect for a chat with no session (whose `tick` is never stamped), while
    // the clock above decides whether the run actually spends a read.
  }, [hasCard, nextId, tick, lap, recRow.id, recRow.at, queueOn]);
  // ── the live row (the tasks change feed) ──────────────────────────────────
  //
  // THE QUEUE MOVES ON EVENTS AND THIS PANE WAS THE ONLY SURFACE STILL ASKING ON
  // A LAP (Akshil, 2026-09-17). `/api/schedule` runs at 15s for a chat with
  // nothing pending in its own session — which is exactly the brand-new queued
  // chat, whose entries name no session — and the `/api/tasks` re-read above is
  // floored at `REC_REFRESH_MS` on top of it. So "behind TASK-046" lingered for
  // seconds after the folder freed, the "1 message waiting" pill outlived the
  // message, and the pane only learned its turn had started when the schedule
  // lap finally came round, which for a short reply is after the reply.
  //
  // The server already rings a long-poll the moment the manager moves anything
  // (`tasks_watch.notify`, design.md "Event transport"), and the Tasks page and
  // Recent chats repaint off it in ~100ms. This subscribes the chat to the SAME
  // feed — not a second poller: `subscribeListing` is refcounted to one
  // `GET /api/tasks` and one `/api/tasks/changes` per document however many
  // surfaces read it.
  //
  // THE POLL STAYS as the floor under it: a feed that has not started, a browser
  // that dropped the socket, or a server too old to ring still get their answer
  // a lap later. Nothing here replaces a read; it only arrives first.
  const [liveRow, setLiveRow] = useState<SchedTask | null>(null);
  /** The status word the last feed answer carried, so the schedule is asked
   *  again on a CHANGE and not on every listing event the machine produces. */
  const liveStatus = useRef("");
  useEffect(() => {
    // Flag off is main byte for byte, and the landing subscribes to nothing.
    if (!queueOn || !inChat || (!sessionId && !leaderId)) return;
    liveStatus.current = "";
    setLiveRow(null);
    return rowsFeed.current((rows) => {
      // THE SAME THREE WAYS IN the lap's own read spends (`schedFindTask`): this
      // session's key, `pending:<entry>`, the leader's key, and failing those a
      // scan for the message. One finder, so the feed and the poll cannot
      // disagree about which row is this conversation's.
      const task = schedFindTask(
        rows,
        nextRef.current,
        live.current.sessionId,
        live.current.leaderId,
      );
      if (!task) {
        // NOT A DELTA THAT DIDN'T MENTION US — THE ROW IS GONE. `subscribeListing`
        // (`shell/tasksPulse.emitListing`) always hands `platformRows` the FULL
        // merged listing, never a bare delta: a change-poll's own `rows`/`gone`
        // pair is folded into `held` before it goes out, and a failed read is
        // dropped before it ever reaches this callback (`platformRows` above).
        // So an answer that reaches here and has nothing for this chat's
        // session/leader/message key is the manager saying the entry left the
        // queue — cancelled, or run and gone — and holding the last-known task
        // would leave the header's dashed "queued · …" caption on screen for a
        // message that is no longer waiting on anything (Bugbot review).
        setLiveRow((cur) => (cur === null ? cur : null));
        const goneId = nextRef.current ? String(nextRef.current.id || "") : "";
        if (goneId) {
          setRecRow((cur) =>
            cur.id === goneId && cur.task !== null
              ? { id: goneId, task: null, at: nowRef.current(), gen: cur.gen + 1 }
              : cur,
          );
        }
        return;
      }
      setLiveRow((cur) => (sameQueueRow(cur, task) ? cur : task));
      // …AND IT IS ALSO THE ROW `rec` PUBLISHES, because every caption in this
      // pane reads that one — the per-message "queued · behind TASK-046", the
      // card over the composer and its count. Stamped `at` as a read, so the
      // floor above counts this as the read it just saved.
      const id = nextRef.current ? String(nextRef.current.id || "") : "";
      if (id) {
        setRecRow((cur) =>
          cur.id === id && sameQueueRow(cur.task, task)
            ? cur
            : { id, task, at: nowRef.current(), gen: cur.gen + 1 },
        );
      }
      // AND THE SCHEDULE IS ASKED AGAIN THE MOMENT THE WORD CHANGES. The row is
      // the fast half of the picture; the other half — whether the entry is
      // still pending, and which session its run opened (`ranSessions`, the one
      // road a queued new chat has to its own transcript) — only `/api/schedule`
      // knows, and waiting out its lap is what left a started message drawn as a
      // dashed queued bubble for the length of the reply.
      const word = String(task.status || "");
      if (word !== liveStatus.current) {
        liveStatus.current = word;
        void watcher.tick();
      }
    });
  }, [queueOn, inChat, sessionId, leaderId, watcher]);

  /** T:16997-16999 — the row is null the moment it stops being THIS entry's. */
  const rec = recRow.id && recRow.id === nextId ? recRow.task : null;

  /**
   * THE BOX IS SHUT FOR A MESSAGE THE READER SCHEDULED, AND FOR NOTHING ELSE.
   *
   * Flag off this is `hasCard`, unchanged: any pending entry aimed here is one
   * the scheduler is about to send INTO this session, and a line typed over it is
   * two messages racing into one run. The only defence the chat had was to shut
   * the box until the entry went.
   *
   * UNDER THE QUEUE the admission took that job — a second send is admitted into
   * this conversation's own line, in the order it was typed, and the row under it
   * says so. But that is only true of messages the ADMISSION created. A calendar
   * entry aimed at this session is still a turn about to start here out of the
   * scheduler's own hand, so it still shuts the box.
   *
   * AND THE SERVER IS ASKED FIRST (`queue_blocking`, design.md UI: "one rule,
   * server first"). It is the side that decides the line, so a client rule that
   * disagreed with it would be a composer shut — or open — for a reason the rest
   * of the app does not share. `schedIsCalendar` is that same rule read off the
   * ENTRY and it stays as the FALLBACK, for the paint before the row lands and
   * for a server too old to send the field: its `origin`-absent test falls the
   * cautious way, which is the right direction to be briefly wrong in.
   */
  const rowBlocking = rec && typeof rec.queue_blocking === "boolean" ? rec.queue_blocking : null;
  const blocked = queueOn
    ? (rowBlocking === null ? calendarHere.length > 0 : rowBlocking) && !!sessionId
    : hasCard;
  /** …and the entry the reason is written about. Flag off it is `next`; under the
   *  queue it is the first CALENDAR entry, which may not be the first waiting one
   *  at all — naming a chat send in a sentence about why the box is shut would be
   *  a banner about the wrong message. */
  const blockAbout = queueOn ? (calendarHere[0] ?? null) : next;

  // ── the layout-shift correction (T:17198-17213) ───────────────────────────
  //
  // The card sits directly above the composer, so the one thing it must never
  // do is move the box a reader is reaching for — and it does not: only the
  // transcript grows, so the height the card takes is height the transcript
  // gives up. What DOES move is the transcript's last line, by the card's
  // height, so a reader who was at the bottom is put back at the bottom. Only
  // on the hidden → shown edge: on the way out the log grows downward and there
  // is nothing to correct.
  const wasBlocked = useRef(false);
  useEffect(() => {
    if (hasCard && !wasBlocked.current) followBottom?.();
    wasBlocked.current = hasCard;
  }, [hasCard, followBottom]);

  // ── the way back out of an armed stop (T:17353-17377) ─────────────────────
  //
  // There is no second button to be the "Keep it", so the escape is the gesture
  // instead — a press outside, or Escape — which is what the rest of this chat
  // teaches a reader to expect. Disarming is always the safe direction to be
  // wrong in: the cost of a lost arm is one more press, and the cost of a stale
  // one is every future run of a task.
  useEffect(() => {
    if (!armedId) return;
    const onDown = (ev: PointerEvent) => {
      // `contains` is the CARD's own answer, asked directly (T:17367). An
      // `instanceof Node` guard in front of it reads as belt-and-braces and is
      // the opposite: `Node` is a DOM global, and in a document that does not
      // define one the guard THREW inside the listener — taking the disarm with
      // it and leaving a destructive button armed.
      const card = cardRef.current;
      if (card && ev.target && card.contains(ev.target as Node)) return;
      setArmedId("");
    };
    // Capture phase and the event is CONSUMED: the document-level Escape binding
    // has claimants of its own, and backing out of a half-pressed confirm must
    // not also trip one of them.
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.stopPropagation();
      ev.preventDefault();
      setArmedId("");
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [armedId]);

  const onStop = useCallback(() => {
    if (!next || !next.id) return;
    const repeat = schedIsRepeat(next);
    const target = schedStopTarget(next);
    if (!target) return;
    /** The entry the press was made AGAINST, captured now: the poll answers
     *  every 15 s and `blockers` is re-read by the continuation below. */
    const id = String(next.id);
    // TWO PRESSES for a repeat, and only for a repeat. The write below stops the
    // recurring task, which spends every run it would ever have made, and
    // nothing on this page can restore them — so the first press only arms, and
    // the label it arms into names that loss. A one-off is a single press:
    // cancelling it loses one message the user can schedule again, and a confirm
    // on that would be ceremony.
    if (repeat && armedId !== id) {
      setArmedId(id);
      return;
    }
    setArmedId("");
    setStopping(true);
    const gen = stopGen.current;
    void (async () => {
      try {
        // The one write this chat makes to the schedule store, and it exists
        // because the block it lifts is this chat's own. `target` is the
        // TEMPLATE's id for a repeat and the entry's own for a one-off:
        // `schedule.cancel` already reads a template id as "no further runs" and
        // cancels the materialized occurrence with it, so one endpoint serves
        // both cases. (`postJson` carries `X-Fused: 1` — D3.)
        await api.cancelScheduledMessage(target);
        if (gen !== stopGen.current) return;
        // Applied LOCALLY so the box opens on the click rather than on the next
        // poll — 15 seconds of dead composer after a successful cancel reads as
        // a button that did nothing. The poll is still the source of truth.
        //
        // BY ID, never by position. A stopped repeat takes EVERY blocker that
        // belongs to its template — the template is cancelled, so an occurrence
        // of it left in the list would have the banner naming a run the server
        // has dropped — and a one-off takes ITS OWN ENTRY. `slice(1)` was the
        // same thing only while the list had not moved underneath the press: a
        // poll that reordered it, or a transcript replacement, and the cancel
        // dropped a row that is still pending while leaving its own (Bugbot
        // PR #1075).
        setRefusedId("");
        setAllBlockers((prev) =>
          prev.filter((e) =>
            repeat ? schedStopTarget(e) !== target : String(e.id) !== id,
          ),
        );
      } catch {
        // A cancel that raced the send is refused, not silently swallowed: the
        // entry is away, the composer stays shut, and that is the true state.
        if (gen !== stopGen.current) return;
        setRefusedId(id);
      } finally {
        // The transcript this press belonged to may be gone, and `reset` has
        // already published the open state for the new one — so a late
        // continuation says nothing at all rather than re-deciding it.
        if (gen === stopGen.current) {
          setStopping(false);
          void watcher.tick();
        }
      }
    })();
  }, [next, armedId, watcher, api]);

  /**
   * Where the row LANDS: the Tasks page, on the calendar, which is the view that
   * answers the question a blocked chat is asking — "when does this let go?"
   * (T:17038-17058).
   *
   * The view is chosen by writing the shell's own remembered-view row rather
   * than by a param, because `Scheduled.tsx` reads that preference on mount and
   * has no URL param for it. Writing it is the same gesture as pressing that
   * page's Calendar button, and a denied store just means the page opens on
   * whichever view the reader last used — one press from the right one, never a
   * dead end.
   *
   * It does NOT reopen the composer and does not pretend to: leaving a blocked
   * chat to look at the thing blocking it is a different errand from unblocking
   * it, and the button beside it is still the only control that does the second.
   */
  const onRow = useCallback(() => {
    if (!next) return;
    try {
      localStorage.setItem(SCHEDULE_VIEW_KEY, "calendar");
    } catch {
      // Denied — the page keeps its own last view.
    }
    onNavigate?.(SCHEDULE_URL);
  }, [next, onNavigate]);

  const locked = !blocked && !!navLocked;
  const reason = blocked ? schedBlockReason(blockAbout) : locked ? NAV_LOCKED_REASON : "";

  return {
    blockers,
    waitingHere,
    allRows,
    blocked,
    pendingIds,
    ranSessions,
    reason,
    placeholder: BLOCKED_PLACEHOLDER,
    schedDisabled: blocked || locked,
    rec,
    row: liveRow,
    recGen: recRow.gen,
    armed: !!nextId && armedId === nextId,
    refused: !!nextId && refusedId === nextId,
    stopping,
    tick,
    onStop,
    onRow,
    cardRef,
    reset,
    refresh,
  };
}
