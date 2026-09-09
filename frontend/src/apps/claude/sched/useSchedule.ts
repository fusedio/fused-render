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
import type { ChatController } from "../protocol/controller-api";
import {
  BLOCKED_PLACEHOLDER,
  createScheduleWatcher,
  NAV_LOCKED_REASON,
  schedBlockReason,
  schedFindTask,
  schedIsRepeat,
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
  timers?: {
    setInterval(fn: () => void, ms: number): unknown;
    clearInterval(handle: unknown): void;
  };
}

export interface ScheduleApi {
  getSchedule(): Promise<{ entries?: SchedEntry[] }>;
  getTasks(): Promise<{ tasks?: SchedTask[] }>;
  cancelScheduledMessage(id: string): Promise<unknown>;
}

const PLATFORM_API: ScheduleApi = {
  getSchedule: () => getSchedule() as Promise<{ entries?: SchedEntry[] }>,
  getTasks: () => getTasks() as unknown as Promise<{ tasks?: SchedTask[] }>,
  cancelScheduledMessage: (id) => cancelScheduledMessage(id),
};

export interface ScheduleState {
  blockers: SchedEntry[];
  /** `blockers.length > 0` — the ONE answer the composer and the calendar button
   *  both read, so they can never disagree about what this chat is doing. */
  blocked: boolean;
  /** Why, in one sentence, for the banner AND for the disabled button's tooltip
   *  and spoken name. `""` when nothing is off. */
  reason: string;
  placeholder: string;
  /** The calendar button is off for the block OR for the mode's lock. */
  schedDisabled: boolean;
  rec: SchedTask | null;
  armed: boolean;
  refused: boolean;
  stopping: boolean;
  /** `Date.now()` as of the last poll that saw a row, so the banner's when-text
   *  is computed against the current clock rather than the one the entry last
   *  changed on. 0 before any such poll — nothing is drawn then. */
  tick: number;
  onStop(): void;
  onRow(): void;
  cardRef: React.MutableRefObject<HTMLDivElement | null>;
  /** T:16776 — the visible conversation was REPLACED. */
  reset(): void;
}

export function useSchedule(opts: UseScheduleOptions): ScheduleState {
  const { controller, file, sessionId, inChat, navLocked, followBottom, onNavigate } = opts;
  /** Read at CALL time: the poller and the row cache both outlive any one
   *  render, and neither may rebuild for a new callback identity. */
  const api = opts.api || PLATFORM_API;
  const hooks = useRef({ setRunParam: opts.setRunParam, controller, api });
  hooks.current = { setRunParam: opts.setRunParam, controller, api };
  /** Read at watcher-BUILD time rather than call time, and through a ref so the
   *  memo below keeps its one honest dependency. A fixed seam: handed in once
   *  by a suite, never swapped mid-life. */
  const timers = useRef(opts.timers);

  const [blockers, setBlockers] = useState<SchedEntry[]>([]);
  const [rec, setRec] = useState<SchedTask | null>(null);
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

  // Read by the poller, which outlives any one render.
  const live = useRef({ sessionId, inChat });
  live.current = { sessionId, inChat };

  /**
   * SAME LIST, SAME OBJECT. The poll answers every 15 s and hands back a fresh
   * array each time; published blindly, that is a re-render of the composer's
   * whole column four times a minute for no change at all — and, for a caller
   * whose controller identity is not stable, a render loop.
   *
   * Compared on WHAT THE BANNER DRAWS — the id order AND each entry's `due`
   * and `state` — rather than on identity alone. The id order is not enough: a
   * re-issued `due` would never land at all.
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
    setBlockers((prev) => {
      const same =
        prev.length === rows.length &&
        prev.every(
          (e, i) =>
            e.id === rows[i].id && e.due === rows[i].due && e.state === rows[i].state,
        );
      return same ? prev : rows;
    });
  }, []);

  const next = blockers[0] ?? null;
  const nextId = next ? String(next.id) : "";
  /**
   * THE LANDING PAGE IS NEVER BLOCKED, and asserted HERE rather than trusted
   * from the poller. `schedPendingHere` already answers `[]` with no session,
   * but that answer only arrives on a TICK — so leaving a blocked chat by Back
   * left the home composer shut, with the banner (which only ever draws inside
   * a chat) not there to say why, for up to a poll interval. The session id is
   * a render-time fact, so the block reads it directly and the home composer is
   * open on the same paint that leaves the conversation (Bugbot PR #1075).
   */
  const blocked = blockers.length > 0 && !!sessionId;

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
    [file, absorb],
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
    setRec(null);
    watcher.resetForNewTranscript();
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
  useEffect(() => {
    // A HALF-PRESSED STOP AND A REFUSAL BOTH BELONG TO THE MESSAGE THEY WERE
    // MADE AGAINST (T:17091-17097 on hide, T:17146 when a different entry
    // arrives). Display already keys both on `nextId`, so nothing is visibly
    // wrong today — but a blocker that leaves the front and comes back would
    // find its destructive button armed with no press behind it, which is the
    // one direction this control must never be wrong in.
    setArmedId("");
    setRefusedId("");
    if (!nextId) {
      setRec(null);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const data = await hooks.current.api.getTasks();
        if (!alive) return;
        setRec(schedFindTask(data.tasks, nextRef.current, live.current.sessionId));
      } catch {
        // No record of the attempt, so the next blocking entry tries again.
        if (alive) setRec(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [nextId]);

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
    if (blocked && !wasBlocked.current) followBottom?.();
    wasBlocked.current = blocked;
  }, [blocked, followBottom]);

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
        setBlockers((prev) =>
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
  const reason = blocked ? schedBlockReason(next) : locked ? NAV_LOCKED_REASON : "";

  return {
    blockers,
    blocked,
    reason,
    placeholder: BLOCKED_PLACEHOLDER,
    schedDisabled: blocked || locked,
    rec,
    armed: !!nextId && armedId === nextId,
    refused: !!nextId && refusedId === nextId,
    stopping,
    tick,
    onStop,
    onRow,
    cardRef,
    reset,
  };
}
