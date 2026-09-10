// The scheduled-message half of PR4 (inventory 05 §C, T:16740-17460). Pure TS:
// no React, no DOM, no `fused.params` — every fact the rules need is passed in,
// which is what makes the whole file readable as a set of rules and testable
// without a server.
//
// TWO THINGS LIVE HERE and they are deliberately not the same thing:
//
//   * the BLOCK — a pending scheduled message aimed at this conversation shuts
//     the composer, because a task IS a session and typing ahead of a run that
//     will read whatever is in the thread is context pollution, not impatience
//     (Akshil, 2026-08-17). The block is the PENDENCY, never a time window: a
//     message due tomorrow pollutes exactly as much as one due in ten minutes.
//   * the ATTACH — a scheduled message that FIRES for this session streams its
//     turn into this transcript, because the reader was told to leave the chat
//     open and watch it.
//
// Both read the ONE `/api/schedule` payload the poller already fetches, so the
// pair costs no second endpoint and cannot disagree about whether this chat is
// blocked.
//
// Everything here FAILS OPEN. A schedule that cannot be read blocks nothing: a
// chat that locks itself over a network blip is a worse failure than the
// pollution the lock prevents, and the schedule is the only thing that could
// ever have explained it (T:17383-17390).

/** The `/api/schedule` entry, narrowed to what these rules read (T:16892-16899,
 *  17061-17070, 17395). Structurally satisfied by `platform/lib/api`'s
 *  `ScheduledMessage`; declared locally so the rules stay pure. */
export interface SchedEntry {
  id: string;
  state?: string;
  session_id?: string;
  claude_session_id?: string;
  due?: string;
  message?: string;
  target?: string;
  run_id?: string;
  template_id?: string;
  repeats?: string;
  rule?: unknown;
}

/** The `/api/tasks` row, narrowed the same way (T:17010-17014). */
export interface SchedTask {
  key: string;
  task_id?: string;
  title?: string;
  status?: string;
  failed?: boolean;
  messages?: { entry_id?: string }[];
}

/** T:16749. */
export const SCHEDULE_POLL_MS = 15000;
/**
 * AND THE FAST ONE, WHILE THE COMPOSER IS SHUT (FIX-D, P4R1-3).
 *
 * The block is a pure `state === "pending"` filter, so it is always CORRECT and
 * up to one interval STALE — and a scheduled haiku turn measured at 8 seconds
 * end to end fits inside a 15 s interval with room to spare. What the reader
 * saw was a dead composer for 8-16 seconds after the work had visibly
 * finished, which reads as "a done entry still blocks" (diagnosis §2.3,
 * measured on legacy identically).
 *
 * So the poll asks oftener for exactly as long as this chat is unusable, and
 * goes back to 15 s the moment it is not. Bounded by construction: the fast
 * rate only ever runs while there IS a blocker, which is a state the reader is
 * waiting to leave — and an open composer's column still re-renders four times
 * a minute at most, which is the whole point of `absorb`'s dedupe.
 */
export const SCHEDULE_POLL_BLOCKED_MS = 3000;
/** T:17038 — the shell's own remembered-view row, written so the Tasks page
 *  opens on the calendar. `Scheduled.tsx` reads this preference on mount and
 *  has no URL param for it, so writing it is the same gesture as pressing that
 *  page's Calendar button. */
export const SCHEDULE_VIEW_KEY = "fused-render:scheduled-view";
/** The route the row lands on. */
export const SCHEDULE_URL = "/tasks";
/** T:17222 — short, because this box is 300px wide in the side pane and a
 *  longer line is simply clipped. The banner carries the explanation; this only
 *  has to say the box is not broken. */
export const BLOCKED_PLACEHOLDER = "Waiting on a scheduled message…";
/** T:17242 — the calendar button's other reason for being off (annNavLocked). */
export const NAV_LOCKED_REASON = "finish or discard the notes first";
/** T:17427 / T:17434 — the two transcript notes, and the ◷ they are drawn with. */
export const NOTE_GLYPH = "◷";
export const NOTE_OURS = "Your scheduled message is running now.";
export const NOTE_FOREIGN =
  "A scheduled message for this folder just ran in another session.";

/** T:16966 — the five states the shell's board defines (schedule-lib.ts
 *  BOARD_COLUMNS) and the words it puts on them. Unknown is "Upcoming" here
 *  rather than the shell's "Done": every entry this banner can be looking at is
 *  PENDING, so a state that does not parse is a listing that has not answered
 *  yet, not a finished job. */
export const SB_STATES: Record<string, string> = {
  upcoming: "Upcoming",
  in_progress: "In Progress",
  done: "Done",
  failed: "Failed",
  archived: "Archive",
};

/**
 * Whether a fired entry's turn belongs in the transcript ON THIS SCREEN
 * (T:16754-16765). Getting this wrong is worse than not attaching at all:
 * splicing another conversation's turn into this one would be a page telling a
 * lie about what was said where.
 *
 * `mine` is the session on screen, `""` when there is none.
 */
export function scheduledRunIsOurs(entry: SchedEntry, mine: string): boolean {
  const ran = entry.claude_session_id || "";
  // With a session on screen, only that same conversation counts — whether the
  // entry named it up front (a follow-up scheduled from here) or reported it
  // once the run started.
  if (mine) return entry.session_id === mine || ran === mine;
  // With no session yet, a send that resumed nothing created one this frame can
  // adopt; one that resumed someone else's belongs on their screen.
  return !entry.session_id;
}

/**
 * Which pending messages are aimed at THIS conversation, soonest first
 * (T:16892-16899). `session_id` is the input ("resume this one") and
 * `claude_session_id` is what a run reported it landed in; either naming the
 * session on screen means the same thing here.
 *
 * With no session there is nothing a message can be pending IN — the landing
 * page's conversation does not exist yet — so the home composer is never
 * blocked.
 */
export function schedPendingHere(
  entries: readonly SchedEntry[] | null | undefined,
  mine: string,
): SchedEntry[] {
  if (!mine) return [];
  const ours = (entries || []).filter(
    (e) =>
      !!e &&
      e.state === "pending" &&
      (e.session_id === mine || e.claude_session_id === mine),
  );
  // ISO stamps with one offset spelling, so a string sort is a time sort.
  ours.sort((a, b) => String(a.due || "").localeCompare(String(b.due || "")));
  return ours;
}

/** T:17057 — a pending OCCURRENCE of a repeat carries `template_id`; a template
 *  itself carries `repeats` or `rule`. */
export function schedIsRepeat(entry: SchedEntry | null | undefined): boolean {
  return !!(entry && (entry.template_id || entry.repeats || entry.rule));
}

/**
 * EVERY FIELD THE BANNER DRAWS, and no more (T:17088-17165). T repaints the card
 * from scratch on every 15 s tick, so an entry edited on the Tasks page shows
 * its new wording within one interval; a dedupe on `id`/`due`/`state` alone
 * froze three separate cells against exactly that edit — the `.sb-name`
 * (`schedMsgLine` reads `message`), and the reason line, the stop button's label
 * and the refusal wording (all three read repeat-ness, i.e. `template_id` /
 * `repeats` / the presence of `rule`). `template_id` is compared by VALUE
 * because `schedStopTarget` posts it: a re-materialised template that keeps its
 * repeat-ness but changes its id must still reach the cancel endpoint.
 *
 * `rule` is compared by PRESENCE only — it is an opaque `unknown` off the wire
 * and nothing here reads inside it, so a deep compare would be a re-render for
 * a change no cell can show.
 */
export function schedSameRow(a: SchedEntry, b: SchedEntry): boolean {
  return (
    a.id === b.id &&
    a.due === b.due &&
    a.state === b.state &&
    a.message === b.message &&
    a.template_id === b.template_id &&
    a.repeats === b.repeats &&
    !!a.rule === !!b.rule
  );
}

/**
 * What id, posted to `/api/schedule/cancel`, actually reopens this box
 * (T:17068-17071). For a repeat that is the TEMPLATE — `_materialize` arms the
 * next occurrence the moment this one is skipped, so cancelling the occurrence
 * moves the block rather than lifting it.
 */
export function schedStopTarget(entry: SchedEntry | null | undefined): string {
  return schedIsRepeat(entry) && entry?.template_id
    ? String(entry.template_id)
    : String((entry && entry.id) || "");
}

/** T:17082-17086 — WHY the box is shut, in one sentence, and it has one author:
 *  the banner shows it as its only line of prose and the calendar button
 *  carries it as its tooltip and its spoken name. The repeat gets its own
 *  wording because the ESCAPE differs — "scheduled" is a message you can
 *  cancel, "repeating" is a job you have to stop. */
export function schedBlockReason(entry: SchedEntry | null | undefined): string {
  return schedIsRepeat(entry)
    ? "Blocked — a repeating message runs in this chat."
    : "Blocked — a scheduled message runs in this chat.";
}

/** The whole reason line: the soonest is NAMED by the row below and the rest are
 *  counted here, because naming one answers "what is coming?" and a list of five
 *  would be the Tasks page in a strip above a chat (T:17111-17113). */
export function schedWhyLine(blockers: readonly SchedEntry[]): string {
  const next = blockers[0];
  if (!next) return "";
  const others = blockers.length - 1;
  return schedBlockReason(next) + (others > 0 ? " " + others + " more after it." : "");
}

/** T:16904-16908 — local calendar days apart, computed from MIDNIGHTS rather
 *  than by dividing a millisecond gap: on the two DST days of the year a day is
 *  23 or 25 hours long, and "tomorrow" is a calendar fact, not an 86_400_000ms
 *  one. */
export function schedDayGap(then: Date, now: Date): number {
  const a = new Date(then.getFullYear(), then.getMonth(), then.getDate());
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((a.getTime() - b.getTime()) / 86400000);
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * When it goes, in the ROW's vocabulary — "14:00 tomorrow", the shape
 * `tasks-lib.ts messageTime` writes into the list view's time cell
 * (T:16921-16931).
 *
 * TWENTY-FOUR HOUR and not the locale's clock, which is the one place this row
 * departs from the rest of the chat: a 12-hour locale renders "02:00 PM", three
 * characters wider in a cell that has to stay readable in a 340px pane, and it
 * would not match the time on the page this row is quoting.
 *
 * Past due is "any moment now" and not a time in the past — a queued message is
 * waiting for the next sweep, so the stamp on it stopped being the answer to
 * "when?".
 */
export function schedWhenText(iso: string | undefined, now: Date = new Date()): string {
  const d = new Date(String(iso ?? ""));
  if (Number.isNaN(d.getTime())) return "at its scheduled time";
  if (d.getTime() <= now.getTime()) return "any moment now";
  const at = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  const gap = schedDayGap(d, now);
  if (gap <= 0) return at + " today";
  if (gap === 1) return at + " tomorrow";
  if (gap < 7) return at + " " + d.toLocaleDateString([], { weekday: "short" });
  return at + " " + d.toLocaleDateString();
}

/**
 * The scheduled message on one line — the row's NAME when the tasks listing has
 * no title of its own to give (T:16940-16943).
 *
 * Whitespace COLLAPSES rather than the first line winning: a prompt that opens
 * "Read the following and:" would otherwise preview as its own preamble. The CSS
 * ellipses whatever does not fit, so nothing is cut at a fixed character count
 * that a 340px pane and a 720px one would both get wrong.
 */
export function schedMsgLine(entry: SchedEntry | null | undefined): string {
  const text = String((entry && entry.message) || "")
    .replace(/\s+/g, " ")
    .trim();
  return text || "A scheduled message";
}

/**
 * The listing row for the message holding this box shut (T:16980-16995). Three
 * ways in, cheapest first: a task keyed by THIS session (a scheduled entry
 * naming a session is folded into that session's task — routers/tasks.py
 * `_collect`), a task keyed `pending:<entry id>` (the entry has no session yet),
 * and failing both a scan for the message itself.
 *
 * The scan is LAST because the listing carries only each task's three newest
 * messages, so it is the one that can legitimately miss.
 */
export function schedFindTask(
  tasks: readonly SchedTask[] | null | undefined,
  entry: SchedEntry | null | undefined,
  mine: string,
): SchedTask | null {
  const id = String((entry && entry.id) || "");
  const pending = "pending:" + id;
  let byMessage: SchedTask | null = null;
  for (const task of tasks || []) {
    if (!task) continue;
    if ((mine && task.key === mine) || task.key === pending) return task;
    if (!byMessage && (task.messages || []).some((m) => m && m.entry_id === id)) {
      byMessage = task;
    }
  }
  return byMessage;
}

/**
 * What the ring and the right-hand cell say — ABOUT THE ENTRY THIS BANNER IS
 * DRAWING, not about the task that holds it (FIX-B, P4R1-3).
 *
 * T read the state straight off `rec.status` (T:17122-17123), and `rec` is the
 * `/api/tasks` row for the WHOLE task. A task holding a finished run and a
 * future pending message answers `done` — legitimately, and
 * `routers/tasks.py`'s `_message_verdict` docstring defends that choice for the
 * Tasks board ("unread OUTPUT sitting in it"). It is simply not a sentence
 * about the blocker: the banner captioned a shut composer "Done · 13:26 today"
 * while the message it named had not run yet, five reproductions on both
 * stacks. `SB_STATES`'s own comment already asserted the thing the line below
 * it broke — "every entry this banner can be looking at is PENDING".
 *
 * So the ENTRY answers when it can. A pending entry is Upcoming whatever the
 * enclosing task's lane says; one whose run is away (`sent` — the window
 * between firing and the block lifting) is In Progress, which is the Tasks
 * page's own word for running. The listing row is the FALLBACK, kept whole for
 * a caller with no entry in hand and for a state this bundle does not know —
 * and `rec.failed` may only win there, because a pending entry cannot be the
 * failure a task-level flag is reporting.
 *
 * An unreadable listing still costs the number and the state and never the row
 * or the block.
 */
export function schedRowState(
  rec: SchedTask | null | undefined,
  entry?: SchedEntry | null,
): {
  state: string;
  label: string;
} {
  const own = String((entry && entry.state) || "");
  if (own === "pending") return { state: "upcoming", label: SB_STATES.upcoming };
  if (own === "sent" || own === "running") {
    return { state: "in_progress", label: SB_STATES.in_progress };
  }
  const state = rec && rec.status && SB_STATES[rec.status] ? rec.status : "upcoming";
  return {
    state: rec && rec.failed ? "failed" : state,
    label: rec && rec.failed ? "Failed" : SB_STATES[state],
  };
}

/**
 * WHAT IS COMING, in the row's name cell (FIX-C, P4R1-3).
 *
 * T named the row `rec.title` first (T:17130) — the CONVERSATION's title, taken
 * from some older message — and the banner exists to answer a narrower
 * question: what is the message that is about to run? The blocker's own words
 * are already in hand. So the entry's message wins, and `rec.title` is the
 * fallback for an entry that carries none of its own; `schedMsgLine`'s "A
 * scheduled message" is the last resort, as it always was.
 *
 * (The dots a reader reported here were not masking and not empty — the task's
 * title is literally ". . . . . . . . . . . . . . .", typed. Reading the
 * message instead is what makes that row show the reader their OWN pending
 * prompt rather than an unrelated old title.)
 */
export function schedRowName(
  entry: SchedEntry | null | undefined,
  rec?: SchedTask | null,
): string {
  if (!entry) return "";
  const own = String(entry.message || "").replace(/\s+/g, " ").trim();
  return own || (rec && rec.title) || schedMsgLine(entry);
}

/** T:17140-17143 — a refused cancel, keyed to the ENTRY so the reconciling poll
 *  re-renders the sentence rather than wiping it. */
export function schedRefusalNote(repeat: boolean): string {
  return repeat
    ? "The repeat is still on — it may already be running."
    : "Still scheduled — it may already be running.";
}

/** T:17153-17156 — say what the control DOES, and for the repeat say what it
 *  COSTS on the press that spends it. */
export function schedStopLabel(repeat: boolean, armed: boolean): string {
  return repeat
    ? armed
      ? "Cancel every future run"
      : "Stop the repeat"
    : "Cancel this message";
}

/** T:17157-17159. */
export function schedStopTitle(repeat: boolean): string {
  return repeat
    ? "Stops the repeating task: this chat reopens and no further runs are scheduled"
    : "Cancels this scheduled message, and this chat reopens";
}

/** T:17136-17137 — the ONE title in this card, and it describes the HOP rather
 *  than repeating text already on screen. */
export function schedRowTitle(rec: SchedTask | null | undefined): string {
  return "Open " + ((rec && rec.task_id) || "this task") + " on the Tasks calendar";
}

// ---- the poller (T:17379-17447) --------------------------------------------

export interface ScheduleWatcherDeps {
  /** The chat's target — an entry is only ours to render if it fired for it. */
  file: string | null;
  /** `GET /api/schedule`. THROWS or answers falsy on failure; either fails open. */
  fetchSchedule(): Promise<{ entries?: SchedEntry[] } | null | undefined>;
  /** The session on screen, `""` on the landing page. */
  sessionId(): string;
  /** False on the landing page — nothing to render a turn into (T:17394). */
  inChat(): boolean;
  /** `activeRun || sending`: a live turn owns the transcript (T:17429). */
  busy(): boolean;
  /** The pending messages aimed at this conversation, soonest first. Called on
   *  EVERY tick, including the failing ones (with `[]`). */
  onBlockers(blockers: SchedEntry[]): void;
  /** A ◷ row in the transcript. */
  addNote(text: string): void;
  /** `params.set("run", id, {history:"replace"})` — a reload, or a mode switch
   *  that remounts this frame, re-attaches from the param (T:17437). */
  setRunParam(runId: string): void;
  /** `resumeRun(id, {neverShown:true})`. */
  resumeRun(runId: string): Promise<void>;
  /** `controller.hasShownRun(id)` — has the CONTROLLER already taken this run
   *  (its own send, a `run` param, a turn the 5 s standing watch adopted)? */
  shownRun(runId: string): boolean;
  /** Injectable for tests. */
  setInterval?: (fn: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
}

export interface ScheduleWatcher {
  /** One pass: block, then at most one attach. */
  tick(): Promise<void>;
  /** Baseline immediately (T:17410-17416 — at LOAD, not one interval later),
   *  then watch every 15 s. Returns the stop. */
  start(): () => void;
  /** T:16776-16786 `scheduleResetForNewTranscript` — called whenever the VISIBLE
   *  conversation is replaced. */
  resetForNewTranscript(): void;
  /** Test seams. */
  readonly attached: ReadonlySet<string>;
  readonly noted: ReadonlySet<string>;
  baselined(): boolean;
}

export function createScheduleWatcher(deps: ScheduleWatcherDeps): ScheduleWatcher {
  /** run_ids attached or baselined (T:16746). */
  const attached = new Set<string>();
  /** foreign runs noted ONCE — never marked attached, so switching to that
   *  session still restores the turn from history (T:16747). */
  const noted = new Set<string>();
  let baselined = false;
  let stopped = false;
  /** Set by `start`, so a tick can ask for the OTHER rate — see `publish`. Null
   *  for a watcher nobody started (or one already stopped): `tick` is public and
   *  `resetForNewTranscript` calls it, and neither may arm a timer. */
  let rearm: ((ms: number) => void) | null = null;

  /**
   * THE ONE PLACE THE BLOCKERS LEAVE (FIX-D). Publishing the list and choosing
   * the poll's rate are the same decision made twice otherwise, and the failing
   * road publishes `[]` too — a schedule that cannot be read blocks nothing, so
   * it must also not leave this page polling three times a second forever.
   */
  function publish(rows: SchedEntry[]): void {
    deps.onBlockers(rows);
    rearm?.(rows.length ? SCHEDULE_POLL_BLOCKED_MS : SCHEDULE_POLL_MS);
  }

  async function tick(): Promise<void> {
    if (stopped) return;
    let data: { entries?: SchedEntry[] } | null | undefined;
    try {
      data = await deps.fetchSchedule();
      if (!data) throw new Error("no schedule");
    } catch {
      // Fail OPEN, both halves: no turn to attach and no block to impose.
      publish([]);
      return;
    }
    if (stopped) return;
    const entries = data.entries || [];
    // The block is a fact about the SCHEDULE, not about what this frame has
    // rendered — so it is applied before the home-view return and before the
    // baseline (T:17391-17394).
    publish(schedPendingHere(entries, deps.sessionId()));
    if (!deps.inChat()) return;
    const fired = entries.filter((e) => e && e.target === deps.file && e.run_id);
    // The FIRST pass is a silent baseline: every run already recorded happened
    // before this frame existed, and the transcript restore has accounted for
    // the ones that belong here (T:17398-17416).
    if (!baselined) {
      baselined = true;
      for (const entry of fired) attached.add(String(entry.run_id));
      return;
    }
    const mine = deps.sessionId();
    for (const entry of fired) {
      const runId = String(entry.run_id);
      if (attached.has(runId)) continue;
      // THE CONTROLLER MAY ALREADY OWN THIS RUN. The standing watch looks every
      // 5 s and this poll every 15, so a fired scheduled run is normally
      // ADOPTED FIRST — and `busy()` then holds this loop at the guard below
      // with the entry left unmarked, exactly as intended. Once the turn ends
      // `busy()` is false and the entry is still in the listing, so the next
      // tick used to `resumeRun` it with `neverShown` and append the very turn
      // the watch had just streamed a second time (Bugbot PR #1075).
      //
      // A run the controller has shown is ATTACHED, not resumable — the same
      // SCHEDULE_ATTACHED semantics as the baseline (T:16746-16765) — and it
      // gets no note: the turn is on screen, and "running now" would be a
      // sentence about a turn that has already finished.
      if (deps.shownRun(runId)) {
        attached.add(runId);
        continue;
      }
      if (!scheduledRunIsOurs(entry, mine)) {
        if (!noted.has(runId)) {
          noted.add(runId);
          deps.addNote(NOTE_FOREIGN);
        }
        continue;
      }
      // The live-turn guard sits HERE, adjacent to the call with nothing awaited
      // in between: checked at the top it could go stale across the fetch, and
      // an id written off as handled while `resumeRun` returned immediately is a
      // turn that never appears at all. Returning leaves the entry unmarked for
      // the next tick (T:17423-17429).
      if (deps.busy()) return;
      deps.addNote(NOTE_OURS);
      attached.add(runId);
      deps.setRunParam(runId);
      await deps.resumeRun(runId);
      return; // one at a time; the next tick picks up anything behind it
    }
  }

  function resetForNewTranscript(): void {
    attached.clear();
    noted.clear();
    baselined = false;
    // The block belongs to the conversation that WAS on screen, so it goes with
    // it rather than hanging over the next one for up to a poll interval.
    // Unblocking is the safe direction to be briefly wrong in, and the poll
    // fired underneath re-establishes it for this session immediately.
    publish([]);
    void tick();
  }

  return {
    tick,
    start() {
      stopped = false;
      const every = deps.setInterval || ((fn, ms) => setInterval(fn, ms));
      const clear = deps.clearInterval || ((h) => clearInterval(h as never));
      let handle: unknown = null;
      /** The rate CURRENTLY armed, so a tick that publishes the same-shaped
       *  answer as the last one does not tear the interval down and put an
       *  identical one back up four times a minute. */
      let armed = 0;
      const arm = (ms: number) => {
        if (handle !== null && armed === ms) return;
        if (handle !== null) clear(handle);
        armed = ms;
        handle = every(() => void tick(), ms);
      };
      rearm = arm;
      // The SLOW rate first: the first tick has not answered yet, and a page
      // that arrives on an open composer must not spend the fast rate finding
      // out. That tick then re-arms within milliseconds if this chat is blocked.
      arm(SCHEDULE_POLL_MS);
      void tick();
      return () => {
        stopped = true;
        rearm = null;
        if (handle !== null) clear(handle);
        handle = null;
      };
    },
    resetForNewTranscript,
    attached,
    noted,
    baselined: () => baselined,
  };
}
