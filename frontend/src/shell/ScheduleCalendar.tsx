// The Tasks page's Calendar view — the same unit the List and the Board show (a
// TASK), on a time axis.
//
// The rule the whole file is built on, and the one thing to understand before
// changing anything here:
//
//   ONE CHIP PER TASK PER DAY, anchored at that task's EARLIEST message that
//   day. Later messages the same day nest INSIDE it and the anchor carries the
//   count. An hourly rule is therefore one chip with `+23`, not 24 chips.
//
// All three views show tasks; the calendar is the one with a time axis, so the
// axis decides PLACEMENT — it does not get to change the unit. The accepted cost
// is that a task's 7pm run has no chip at 7pm; the `+N` badge names it and the
// popover lists it with its real time, which is why the popover is not an
// afterthought here. The rejected alternative (a chip per message) shows one
// task many times in a single day, which the other two views never do.
//
// Chips are a FIXED one line tall. A message has a start time and no duration,
// so there is nothing for a variable height to encode — the same reason Google
// Calendar draws a short event as one line. Do not size chips by duration.
//
// Colour is per TASK, derived from its key (schedule-lib.taskColour) so five
// days of a daily task read as one thing across the grid.
//
// AN ARCHIVED TASK DRAWS NOTHING. The grid is for what is going to happen and
// what did; a task that was cancelled or skipped outright is filed away, and
// three chips at one time with two of them struck through read as noise (Akshil,
// 2026-08-17). It is the TASK's status that decides, never a run's — a live task
// with one skipped occurrence still draws its chip, and the skip is an Archive
// row inside the popover. taskChips is where the rule lives.
//
// A RULE'S SHAPE IS DRAWN IN BOTH DIRECTIONS. Future occurrences come from the
// server's projection; the slots a PAST-anchored rule went by come from a walk
// the client does itself, because the server has no reason to compute runs it
// will never create. Only the most recent of those past slots is real (catch-up
// materializes exactly one, at that slot's own time) and the ghost over it is
// deduped away; the rest are outlined, greyed and faded, and say Archive.
// Nothing here asks for them to exist — they are drawn, never created.
//
// Two ranges: the week, and Google's "4 days" — today leftmost, arrows stepping
// four days, Today snapping back. The 4-day range earns its place on width, so
// its chips get a larger label.
//
// QUEUED work — past due and waiting to be claimed — has no strip across the top
// of the grid. It is a MESSAGE, its siblings are already listed in the task's
// popover thread, and a band across the week said otherwise (Akshil,
// 2026-08-17). The queue's two states ride the thread row they belong to, and so
// does the cancel: cancelling races the claim, so an entry the server has
// already handed to the sender comes back REFUSED — and is said so, never
// silently dropped. Missed occurrences are NOT in the queue: they stay greyed on
// their original past slot so history stays readable.
//
// ONE STATUS VOCABULARY. The popover's pill and every thread row say the app's
// five words — Upcoming / In Progress / Done / Failed / Archive — and nothing
// else. Failure is a WORD, not just a red ring (Akshil, 2026-08-17); the red
// stays as reinforcement. The two settled edge cases: a skipped occurrence reads
// Archive (filed away, never attempted), and a projected run reads Upcoming with
// a dashed ring, because a projection genuinely is upcoming and the outline is
// what says nothing has been written down yet. The mapping is not this file's
// (that would be a second answer to a question tasks-lib already answers): the
// column comes from tasks-lib.messageTone, the wording from
// schedule-lib.runStatus.
//
// AND THE PILL AND THE ROWS ARE SCOPED DIFFERENTLY ON PURPOSE. The pill is the
// TASK's status (tasks-lib.taskColumn, via schedule-lib.taskStatus) because it
// sits beside TASK-023 in the header and a chip IS a task — the same noun the
// List and the Board show, so the same word. Each ROW is its own run's status.
// The pill used to be the day's WORST run, which is how it came to read "Failed"
// beside a "Run now" button on a rule whose task was merely upcoming; the memo
// that fixed it carries the argument. Day-level facts still reach the eye
// through pixels — the chip's wash (schedule-lib.dayTone) and the dashed ring on
// a projected row — never through a second, contradicting word.
//
// RUN NOW / RE-RUN IS IN THE POPOVER FOOTER, and it is the List's button rather
// than a second one: tasks-lib.taskRunIntent decides whether it appears, which
// message it fires, which of the two calls that is, and which of the two words it
// says. All three views therefore agree by construction (Akshil, 2026-08-17:
// "let's have it in all views why just list view? for future and failed
// events"). The popover CLOSES on a clean run and stays only to show a sentence —
// see runTask, where the reasoning is, because the panel is anchored to a chip
// that the run itself changes.
//
// CHIPS ARE PLACED BY `at`, which is always the time the message was SCHEDULED
// for. `ran_at` is when it went. A task due Monday and caught up on Wednesday
// stays in Monday's column and the popover row says "ran 2 days late" — moving
// the chip would make the grid disagree with the schedule the user wrote.
//
// The layout maths is all in schedule-lib.ts and tested there; this file adds
// pixels and nothing else.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  cancelQueued,
  cancelScheduledMessage,
  getTaskMessages,
  getTasksScheduled,
  markTaskMessageRead,
  resendScheduledMessage,
  runScheduledNow,
} from "@platform/lib/api";
import type { ScheduledMessage, Task, TaskMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import {
  assignLanes,
  calendarThreads,
  cancelOutcome,
  chipAccessibleName,
  folderHref,
  groupScheduled,
  dayKey,
  firstLine,
  isProjectedId,
  minutesOfDay,
  msgCancelKind,
  msgNote,
  popoverPos,
  queueRole,
  queueRoles,
  rangeDays,
  rangeLabel,
  rangeStart,
  repeatTextFor,
  runStatus,
  sameDay,
  scrollTarget,
  stepRange,
  taskChips,
  taskStatus,
  threadForDay,
  windowBounds,
} from "./schedule-lib";
import type { CalendarChip, CalendarRange, MsgCancelKind, QueueRole } from "./schedule-lib";
// Where a click GOES is owned by tasks-lib, and by nothing else: taskHref opens
// the thread, messageHref opens the one turn inside it. The calendar used to
// build that url itself out of explorerUrl, which silently dropped the message
// anchor and landed every reader at the top of the conversation.
//
// messageTone is imported for the same reason: WHICH COLUMN a message is in is
// tasks-lib's answer, and the calendar now says the Board's words, so it asks
// rather than deciding. Aliased because schedule-lib exports a same-named
// function that produces a CSS class — the two are documented at both ends.
// taskRunIntent is imported for the third time in the app and for the same
// reason: Run now / Re-run is ONE decision — whether it is offered, which
// message it acts on, which of the two calls that is, and which word goes on the
// button. The List asks, the Board's drag asks, and the calendar popover now
// asks. Three views working it out separately is how the status vocabulary went
// wrong, and this one has the most rope to hang itself with: the popover's own
// thread list holds PROJECTED rows the other two views have never seen, so the
// intent is asked of the TASK (the same object the List hands it) and never of
// the merged list below.
// taskColumn is imported for the header PILL, and for the same reason the button
// beside it asks tasks-lib: which column a task is in is one answer, given once.
// The pill used to answer a different question (the day's worst run) and could
// therefore contradict the button — see the `pill` memo below.
import {
  messageTone as taskMessageTone,
  openMessageHref,
  taskColumn,
  taskHref,
  taskRunIntent,
} from "./tasks-lib";
import type { TaskRunIntent } from "./tasks-lib";
// The Board's own status ring, reused rather than re-drawn: one vocabulary means
// one glyph too, so a Done row in the popover is the same mark as a Done card on
// the board — red on Failed, and dashed (below) when it is only projected.
import { StatusIcon } from "./ScheduleTaskViews";

// One hour of grid, in px. 44 puts a full day at ~1050px — tall enough that two
// runs half an hour apart do not collide, short enough that the 8am–6pm band a
// person actually schedules into fits a laptop viewport. Mirrored by the hour
// ruler painted in schedule.css; the two must move together.
const HOUR_H = 44;

// Empty-grid clicks snap to the half hour: the grid is a minute-precision
// surface read at hour precision, and "9:30" is almost always what a click at
// 9:26 meant. The New job form keeps minute precision for those who want it.
const SNAP_MIN = 30;

// Which range is up, remembered across visits — a person who plans four days at
// a time plans four days at a time every time.
const RANGE_KEY = "fused-render:scheduled-cal-range";

// Small stroke icons, the GlobalSidebar recipe (16px, stroke=currentColor) at
// button scale. Inline rather than a library — these are the page's whole
// vocabulary. ICON_CLOCK and ICON_FOLDER are imported by NewJobModal.
const icon = (paths: React.ReactNode) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {paths}
  </svg>
);

export const ICON_CLOCK = icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></>);
export const ICON_REPEAT = icon(<><path d="M17 2l4 4-4 4" /><path d="M3 11v-1a4 4 0 0 1 4-4h14" /><path d="M7 22l-4-4 4-4" /><path d="M21 13v1a4 4 0 0 1-4 4H3" /></>);
export const ICON_FOLDER = icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />);
export const ICON_SHIELD = icon(<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />);
export const ICON_EDIT = icon(<><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>);
export const ICON_SKIP = icon(<><polygon points="5 4 15 12 5 20 5 4" /><line x1="19" y1="5" x2="19" y2="19" /></>);
export const ICON_CANCEL = icon(<><circle cx="12" cy="12" r="9" /><path d="M8 8l8 8M16 8l-8 8" /></>);
export const ICON_NOTES = icon(<><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="14" y2="17" /></>);
export const ICON_RESTORE = icon(<><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></>);
export const ICON_INBOX = icon(<><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></>);
// Run now, and the word for doing it AGAIN. Two glyphs rather than one, for the
// reason the archive pair is two: a person looking at a task that broke has to
// see that this restarts it, and a play triangle says "start", not "again".
// Deliberately the SAME geometry as ScheduleTaskViews' pair (lucide `play` and
// `rotate-ccw`) so one action is one mark across List, Board and Calendar —
// copied rather than imported only because those two are module-private there
// and this file may not edit them.
//
// ICON_RESTORE above is a rotate-ccw variant too, and the two are kept apart on
// purpose: that one is Unskip (put a cancelled occurrence BACK), this one is
// Re-run (send it again). Different arcs, and they never appear in one row.
const ICON_PLAY = icon(<polygon points="6 3 20 12 6 21 6 3" />);
const ICON_RERUN = icon(
  <><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
    <path d="M3 3v5h5" /></>,
);
// lucide `loader-circle`, same recipe as the rest. It stands where a cancel
// cannot: a claimed entry is a brief state the server will not withdraw, and
// that slot has to look occupied on purpose rather than empty by accident.
const ICON_STARTING = icon(<path d="M21 12a9 9 0 1 1-6.219-8.56" />);

const clockTime = (d: Date) =>
  d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

// Dismiss-on-outside-pointerdown plus Esc — the GlobalSidebar menu pattern,
// shared by both popovers here so they cannot drift apart.
function useDismiss(ref: React.RefObject<HTMLElement>, onClose: () => void) {
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Stop the shell (or an ancestor modal) also acting on this Esc.
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [ref, onClose]);
}

// Keep a floating panel on screen. The geometry (flip, then clamp) is
// schedule-lib.popoverPos and tested there; what lives here is the MEASUREMENT,
// which a pure function cannot do: the panel's height depends on how long the
// thread is, and the thread arrives one fetch after the panel opens. So it is
// placed from the CSS max-height first, then re-placed from the box it actually
// became — a chip clicked near the bottom of a tall window otherwise opens a
// popover whose action row is off-screen.
const POPOVER_W = 360;
// Mirrors `.schedule-cal-popover { max-height: min(70vh, 560px) }`.
const popoverMaxH = () => Math.min(560, window.innerHeight * 0.7);

function useAnchored(
  ref: React.RefObject<HTMLElement>,
  at: { x: number; y: number },
  // One scalar standing for everything that can change the panel's height — the
  // thread landing, an error line appearing — so the re-place happens on the
  // same frame the box grows. A scalar rather than a spread dep list: a deps
  // array whose LENGTH varies is the one thing the hooks rule cannot check.
  sig: string,
) {
  const [pos, setPos] = useState(() =>
    popoverPos(at.x, at.y, POPOVER_W, popoverMaxH(), window.innerWidth, window.innerHeight),
  );
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    setPos(popoverPos(at.x, at.y, box.width, box.height, window.innerWidth, window.innerHeight));
  }, [ref, at.x, at.y, sig]);
  return pos;
}

// ---- The chip popover ----------------------------------------------------------
// The mitigation for the one known cost of the one-chip-per-day rule, so it has
// to be good: the task's thread, THAT DAY's messages first with their real
// times, then the rest newest-first. The 7pm run that has no chip of its own is
// right here, named and clickable.

function ChipPopover({
  chip,
  repeat,
  roles,
  at,
  onClose,
  onReload,
  onEditEntry,
}: {
  chip: CalendarChip;
  /** The recurrence in words ("Daily"), "" for a one-off. */
  repeat: string;
  /** What the scheduler's queue says about each entry id — this is where the
   * old all-day strip's two facts (queued, running) now live. */
  roles: Map<string, QueueRole>;
  at: { x: number; y: number };
  onClose: () => void;
  onReload: () => void;
  onEditEntry?: (entryId: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [thread, setThread] = useState<TaskMessage[] | null>(null);
  const [busy, setBusy] = useState("");
  // Its own flag rather than a share of `busy`: that one is a MESSAGE id, and it
  // greys one thread row's cancel. This greys a footer button, and the two can
  // legitimately be about different messages at once.
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  useDismiss(ref, onClose);

  const task = chip.task;

  // The full thread is a separate call on purpose (a full transcript parse is
  // too expensive to do for every row), so it is fetched only when a chip is
  // actually opened. Until it lands — or if it fails — the day's own messages
  // are already in hand and the popover is useful without it.
  useEffect(() => {
    let live = true;
    getTaskMessages(task.key).then(
      (r) => live && setThread(r.messages ?? []),
      () => live && setThread(null),
    );
    return () => {
      live = false;
    };
  }, [task.key]);

  const messages = useMemo(() => {
    const merged = new Map<string, TaskMessage>();
    for (const m of [...(task.messages ?? []), ...chip.messages, ...(thread ?? [])])
      merged.set(m.message_id, m);
    return [...merged.values()];
  }, [task.messages, chip.messages, thread]);

  const { today, rest } = useMemo(
    () => threadForDay(messages, chip.day),
    [messages, chip.day],
  );

  // Placed once from the CSS cap, then again from the box it became — the
  // thread lands a fetch later and an error line can appear under it, and both
  // change the height that decides whether the panel fits below the click.
  const pos = useAnchored(ref, at, `${today.length}:${rest.length}:${error}`);

  // Clicking a message opens the explorer's Claude chat ON THAT TURN, and that
  // click is what marks it read (§7). The url is messageHref's to build — the
  // same one the List uses — so the calendar lands the reader exactly where the
  // list does, `msg=` anchor and all, instead of at the top of the thread.
  //
  // openMessageHref answers for every row that has nowhere to go — a task with
  // no session yet, and a PROJECTED occurrence, which is cron arithmetic rather
  // than a message and must never mint a `msg=` pointing at nothing. It is
  // tested there, not re-decided here.
  const canOpen = (m: TaskMessage) => openMessageHref(task, m) !== null;

  const openMessage = (m: TaskMessage) => {
    const to = openMessageHref(task, m);
    if (!to) return;
    if (m.unread) markTaskMessageRead(task.key, m.message_id).then(onReload, () => {});
    navigateUrl(to);
    onClose();
  };

  // The footer button opens the thread itself, top of the chat — taskHref, the
  // same function the List's task row uses. It answers null until the task HAS a
  // session, which is the whole window a scheduled run spends in flight before
  // its watcher reports one, so the fallback is not optional: that is the state
  // a run parked on a permission prompt is in, and it is exactly when somebody
  // needs a way in (Akshil, 2026-08-17). folderHref opens the run's folder with
  // the Claude pane on it, and says why there.
  const threadHref = taskHref(task) ?? folderHref(task);

  // Run now / Re-run, the List's and the Board's action, reachable from the one
  // surface the calendar has for a task (Akshil, 2026-08-17: "can we have the
  // rerun in calendar view also... let's have it in all views why just list
  // view? for future and failed events").
  //
  // NOT RE-DERIVED, and asked of `task` — the exact expression the List row
  // uses, on the exact same object — so the calendar cannot offer the action
  // when the List would not, cannot say a different word, and cannot send a
  // different entry id. Everything about it is tasks-lib's: whether at all,
  // which of the two calls, which message, and the label ("Run now" while
  // something is still pending, "Re-run" once the task reads as failed).
  //
  // The one thing added here, and it is a guard rather than a rule: a PROJECTED
  // row must never be fired. The popover is the only place in the app whose
  // message list holds ghosts, and a ghost's `entry_id` is the recurring RULE's,
  // so spending it would ask the server to run something that was only ever
  // drawn. `task.messages` are the server's own and hold no ghosts today, which
  // makes this unreachable — it is here so it stays unreachable if the popover
  // ever hands its merged list (which does hold them) to this function.
  const run = useMemo(() => {
    const intent = taskRunIntent(task);
    if (!intent || isProjectedId(intent.messageId)) return null;
    return intent;
  }, [task]);

  // THE POPOVER'S LIFECYCLE AROUND A RUN, which is the whole difficulty: this
  // panel is anchored to a chip, and running the task CHANGES that chip. So it
  // closes on a clean success and stays only when there is a sentence to read —
  // the shape the queued cancel below already has, for a stronger reason here.
  //
  // Re-anchoring was considered and is not defined. `openChip` holds a snapshot
  // taken at click time and the grid never re-derives it, so a panel left open
  // would go on describing the run that has just gone. Worse for the re-send
  // half: it creates a one-off due NOW, which belongs to TODAY's column — very
  // often not this chip's day at all — so there is no chip on this day for the
  // new message to be anchored to. Closing is also the only option that cannot
  // leave the panel pinned to a chip the next poll has removed.
  //
  // The exception is the useful one. A 409 ("that conversation already has a
  // turn open") reads as WAIT, not as broken, and a re-send answers 200 with a
  // `note` when the new message was queued rather than sent — neither changed
  // this chip, so staying put is honest, and staying put is the only way the
  // sentence gets read. Both land in the same quiet line a refused cancel uses.
  const runTask = async (intent: TaskRunIntent) => {
    setRunning(true);
    setError("");
    try {
      // WHICH call is not decided here: `kind` came out of tasks-lib and this is
      // the only place it is spent.
      let said = "";
      if (intent.kind === "resend") {
        said = (await resendScheduledMessage(intent.entryId)).note ?? "";
      } else {
        await runScheduledNow(intent.entryId);
      }
      // The grid has to re-read either way — a pending run has become sent, or a
      // failed task has grown a new pending message somewhere in the window.
      onReload();
      if (said) setError(said);
      else onClose();
    } catch (e) {
      // The server's own sentence, verbatim, and a reload anyway: if the refusal
      // was the honest race (it fired while this was open) the grid corrects
      // itself to whatever actually happened.
      setError((e as Error).message);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  // Two cancels, one button, and msgCancelKind says which. A QUEUED entry goes
  // through the queue endpoint, which is the only one that can answer honestly
  // when the race is lost: an entry claimed for sending between paint and press
  // comes back `refused`, and that sentence goes up rather than the popover
  // closing as if it had worked. Everything still in the future is a plain
  // schedule cancel.
  const cancelMessage = async (m: TaskMessage, kind: MsgCancelKind) => {
    // Nothing is drawn for `held` or `none`, so nothing should reach here — and
    // if it somehow does, do nothing rather than ask the server for the refusal
    // it would certainly give. The row already says why it has no control.
    if (kind !== "queued" && kind !== "scheduled") return;
    setBusy(m.message_id);
    setError("");
    try {
      if (kind === "queued") {
        const r = await cancelQueued([m.entry_id]);
        const said = cancelOutcome(r.cancelled ?? [], r.refused ?? []);
        setError(said);
        onReload();
        if (!said) onClose();
      } else {
        await cancelScheduledMessage(m.entry_id);
        onReload();
        onClose();
      }
    } catch (e) {
      // The likeliest failure is the honest race — it fired while the popover
      // was open — so the server's words go up and the page refreshes anyway.
      setError((e as Error).message);
      onReload();
    } finally {
      setBusy("");
    }
  };

  const dayLabel = chip.time.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  // THE HEADER'S PILL IS THE TASK'S STATUS — `taskColumn(task)` and
  // `task.failed`, the exact two values the List's row and the Board's card hand
  // StatusIcon, so the three views cannot say different words about one task.
  //
  // It was the DAY's worst run until 2026-08-17 (schedule-lib.dayStatus, now
  // gone), and that made this panel argue with itself: a recurring rule whose
  // task is Upcoming but whose day holds one failed run showed a pill reading
  // "Failed" next to the footer button reading "Run now". Two facts, one panel,
  // disagreeing — and the pill sits beside TASK-023 in this header, so it is
  // labelling that task and not that column. Each run's own outcome is still on
  // its own row in the thread below, which is where a day-level word belongs.
  //
  // `chip.projected` is the one day-scoped thing left on it and is not a word:
  // the dashes say nothing on this day is written down yet. The chip on the grid
  // keeps its own day-level cues (schedule-lib.dayTone paints the wash, and a
  // day holding a failure still reads differently from a clean one) — those are
  // pixels, they were never the duplicated fact, and they stay.
  const pill = useMemo(
    () => taskStatus(taskColumn(task), task.failed, chip.projected),
    [task, chip.projected],
  );

  const nowSec = Math.floor(Date.now() / 1000);

  const row = (m: TaskMessage, sameDayRow: boolean) => {
    const status = runStatus(m, taskMessageTone(m));
    const role = queueRole(m, roles, nowSec);
    const t = new Date(m.at * 1000);
    // ONE decision, recomputed every render from the freshly-polled queue, so
    // the row's control follows the entry through scheduled → queued → held
    // instead of being reasoned out again beside the markup. schedule-lib owns
    // the rule — and the tests for it — including the one that used to be wrong
    // here: a `sending` entry gets no cancel, because the server refuses that
    // state every single time and a button that can only fail is worse than
    // none. The RACE still keeps its button (`queued`), and its refusal still
    // comes back through cancelOutcome; a row we already know is claimed when we
    // draw it is a different situation, and only the race justifies the offer.
    const kind = msgCancelKind(m, role);
    const note = msgNote(m, kind);
    return (
      <li key={m.message_id} className="schedule-cal-msg">
        <button
          type="button"
          className={"schedule-cal-msg-open" + (canOpen(m) ? "" : " is-inert")}
          onClick={() => openMessage(m)}
          title={note ? `${t.toLocaleString()} — ${note}` : t.toLocaleString()}
        >
          {/* The Board's ring, at row scale. `is-ghost` is the one thing the
              Board has no case for: a projected run, dashed because the word
              beside it says "Upcoming" like any other scheduled run and
              something has to tell the two apart. */}
          <span className={"schedule-cal-ring" + (status.projected ? " is-ghost" : "")}>
            <StatusIcon status={status.column} failed={status.failed} />
          </span>
          <span className="schedule-cal-msg-time">
            {sameDayRow
              ? clockTime(t)
              : t.toLocaleDateString(undefined, { month: "short", day: "numeric" })}
          </span>
          <span className="schedule-cal-msg-body">{firstLine(m.body) || "(no prompt)"}</span>
          {/* role="img" so the dot is legitimately exposed: an aria-label on a
              role-less span is not reliably announced, and this dot is the ONLY
              carrier of the fact — unlike the ↻ and the pulse, it duplicates
              nothing, so it is named rather than hidden. */}
          {m.unread && (
            <span className="schedule-cal-msg-unread" role="img" aria-label="Unread" />
          )}
          {note && (
            <span
              className={
                "schedule-cal-msg-note" + (kind === "held" ? " is-held" : "")
              }
            >
              {note}
            </span>
          )}
          {/* The app's word, and only ever one of its five. */}
          <span className="schedule-cal-msg-state">{status.label}</span>
        </button>
        {kind === "held" ? (
          // A claimed entry has no cancel, so the slot holds a turning glyph
          // rather than a dead button: the same 24px box, so the note and the
          // status word beside it do not slide sideways the instant the entry
          // moves queued → sending, and it moves, because standing here has to
          // say "brief, in hand" and not "stuck, control missing". Nothing to
          // press and nothing to tab to — the WORDS are the note above
          // (schedule-lib.msgNote), because a title tooltip is not reachable by
          // keyboard and this is the only explanation the row gets.
          <span className="schedule-cal-msg-held" aria-hidden="true">
            {ICON_STARTING}
          </span>
        ) : kind !== "none" ? (
          <button
            type="button"
            className="schedule-cal-msg-act"
            disabled={busy === m.message_id}
            aria-label={
              m.template_id
                ? `Skip the ${clockTime(t)} run`
                : `Cancel the ${clockTime(t)} message`
            }
            title={m.template_id ? "Skip this run" : "Cancel this message"}
            onClick={() => cancelMessage(m, kind)}
          >
            {m.template_id ? ICON_SKIP : ICON_CANCEL}
          </button>
        ) : null}
      </li>
    );
  };

  return (
    <div
      ref={ref}
      className="schedule-cal-popover"
      style={pos}
      role="dialog"
      aria-label="Task details"
    >
      <div className="schedule-cal-pop-head">
        <span
          className="schedule-cal-swatch"
          style={{ ["--chip" as string]: `var(--task-c${chip.colour})` }}
          aria-hidden="true"
        />
        <span className="schedule-cal-pop-id">{task.task_id}</span>
        {chip.recurring && (
          // Decorative: the recurrence is spelled out in the rows below, and a
          // glyph carrying its own label only ever repeats a word.
          <span className="schedule-cal-pop-rep" aria-hidden="true">↻</span>
        )}
        <span
          className={
            `schedule-state schedule-state--${pill.column}` +
            (pill.projected ? " is-projected" : "")
          }
        >
          {pill.label}
        </span>
      </div>

      <p className="schedule-pop-title">{task.title || firstLine(chip.anchor.body)}</p>

      <div className="schedule-pop-rows">
        {repeat && (
          <span className="schedule-pop-row">
            {ICON_REPEAT}
            <span>{repeat}</span>
          </span>
        )}
        <span className="schedule-pop-row">
          {ICON_FOLDER}
          <code title={task.target}>{task.target}</code>
        </span>
        {task.description && (
          <span className="schedule-pop-row">
            {ICON_NOTES}
            <span>{task.description}</span>
          </span>
        )}
      </div>

      <div className="schedule-cal-thread">
        <p className="schedule-cal-thread-head">{dayLabel}</p>
        <ul className="schedule-cal-msgs">{today.map((m) => row(m, true))}</ul>
        {rest.length > 0 && (
          <>
            <p className="schedule-cal-thread-head">Earlier in this thread</p>
            <ul className="schedule-cal-msgs">{rest.map((m) => row(m, false))}</ul>
          </>
        )}
      </div>

      {error && <p className="schedule-card-why">{error}</p>}

      <div className="schedule-card-actions">
        {onEditEntry && chip.anchor.entry_id && (
          <button type="button" className="btn btn-secondary"
                  onClick={() => { onEditEntry(chip.anchor.template_id || chip.anchor.entry_id); onClose(); }}>
            {ICON_EDIT} Edit
          </button>
        )}
        {threadHref && (
          <button type="button" className="btn btn-secondary"
                  onClick={() => navigateUrl(threadHref)}>
            {ICON_INBOX} Open in Explorer
          </button>
        )}
        {/* LAST in the row, and secondary like its neighbours, because it must
            not read as this panel's default press: Edit and Open in Explorer
            both only look at things, and this one starts work. Last also puts it
            last in the tab order, so nobody arrives on it by pressing Tab once.
            The word is tasks-lib's — Run now, or Re-run on a task that broke —
            and the title carries the half a person would fear (the scheduled
            time does not move; a re-send is a NEW message in the same thread).
            The visible label IS the accessible name, so no aria-label doubles
            it. */}
        {run && (
          <button
            type="button"
            className="btn btn-secondary schedule-cal-pop-run"
            disabled={running}
            title={run.title}
            onClick={() => void runTask(run)}
          >
            {run.rerun ? ICON_RERUN : ICON_PLAY} {run.label}
          </button>
        )}
      </div>
    </div>
  );
}

// ---- The grid ------------------------------------------------------------------

export default function ScheduleCalendar({
  tasks,
  entries = [],
  queued = [],
  running = [],
  onReload,
  onCreateAt,
  onEditEntry,
}: {
  /** Every task. The calendar draws only their `scheduled` messages. */
  tasks: Task[];
  /**
   * The raw schedule entries, for the ONE thing tasks cannot carry: a recurring
   * rule's projected future occurrences (`upcoming`), which are server-side
   * cron math and not messages yet. Optional — without them the grid simply
   * stops at each rule's next materialized run.
   */
  entries?: ScheduledMessage[];
  /** getScheduleQueue().queued — past due, waiting to be claimed. No longer a
   * strip across the grid: it marks the thread rows those entries ARE. */
  queued?: ScheduledMessage[];
  /** getScheduleQueue().running — mid-flight, same treatment. */
  running?: ScheduledMessage[];
  onReload: () => void;
  onCreateAt: (time: Date) => void;
  /** Opens the New task modal on an existing entry (a rule, or a one-off). */
  onEditEntry?: (entryId: string) => void;
}) {
  const [range, setRange] = useState<CalendarRange>(() => {
    // localStorage can THROW (private mode, locked-down webviews) and this runs
    // during first render — a preference must never cost the page.
    try {
      return localStorage.getItem(RANGE_KEY) === "4day" ? "4day" : "week";
    } catch {
      return "week";
    }
  });
  const [start, setStart] = useState(() => rangeStart(new Date(), range));
  const [openChip, setOpenChip] = useState<{ chip: CalendarChip; x: number; y: number } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // What the scheduler's queue says, by entry id — read by the popover's thread
  // rows, which is the only place the queue is now shown.
  const roles = useMemo(() => queueRoles(queued, running), [queued, running]);

  const pickRange = (next: CalendarRange) => {
    setRange(next);
    // Re-anchor rather than jump: the week snaps to its Monday, the 4-day range
    // starts on the day you were already looking at. Today is what snaps back.
    setStart((s) => rangeStart(s, next));
    setOpenChip(null);
    try {
      localStorage.setItem(RANGE_KEY, next);
    } catch {
      // A blocked store forgets the choice; the switch itself still happens.
    }
  };

  const days = useMemo(() => rangeDays(start, range), [start, range]);

  // Every scheduled message in the visible days, from the windowed endpoint —
  // the one question the task listing cannot answer, since it ships only each
  // task's three most recent and a calendar draws a week. `null` means "we do
  // not have it": either not fetched yet, or the call failed.
  const [windowed, setWindowed] = useState<Record<string, TaskMessage[]> | null>(null);
  const { from, to } = useMemo(() => windowBounds(days), [days]);

  // Re-fetched whenever the window moves — the arrows, Today, and the range
  // toggle all move it, and all three land here as a changed `from`/`to`. Also
  // on each `tasks` poll, so a run that fires while the page is open appears.
  //
  // The failure path is the page's standing posture for a SECOND feed (see
  // Scheduled.tsx's sessions): keep what we have and let the grid fall back to
  // each task's own three messages. A degraded calendar beats a blank one, and
  // it beats a red banner over a grid that is still mostly right.
  useEffect(() => {
    if (!from && !to) return;
    let live = true;
    getTasksScheduled(from, to).then(
      (r) => live && setWindowed(groupScheduled(r.items ?? [])),
      // Not cleared to null on failure: the previous window's threads are stale
      // but taskChips only ever reads the days it was handed, so stale rows for
      // days now off-screen cost nothing, and dropping them would blank chips
      // that are still correct.
      () => {},
    );
    return () => {
      // The guard is the point: arrows pressed faster than the round trip must
      // not let an older window's answer land last.
      live = false;
    };
  }, [from, to, tasks]);

  // Windowed messages with the recurring rules' projections layered on top, so
  // a projected run obeys exactly the same one-chip-per-day rule a real one
  // does. `days` goes in because projection now runs BOTH WAYS: the slots a
  // past-anchored rule skipped are drawn too, and how far back to walk is the
  // window on screen, not a fixed horizon off now.
  const threads = useMemo(
    () => calendarThreads(tasks, entries, windowed, new Date(), days),
    [tasks, entries, windowed, days],
  );

  const chipsByDay = useMemo(
    () => taskChips(tasks, days, threads),
    [tasks, days, threads],
  );

  // WHERE THE GRID OPENS, and — just as load-bearing — WHEN it is allowed to
  // decide that again.
  //
  // The target is schedule-lib.scrollTarget and is tested there: today's now-line
  // when today is in view with anything on it, otherwise the earliest chip in the
  // range, otherwise the old 7am. It exists because one chip per task per day
  // anchors at the day's EARLIEST slot, so an hourly rule's whole day is a single
  // chip at 00:00 — which the old fixed 7am opened seven hours below, on a column
  // that then read as empty.
  //
  // THE PLACEMENT BELONGS TO THE RANGE, NOT TO THE DATA. Arrows, Today and the
  // week/4-day toggle move the window and earn a re-place; the 20s poll does not.
  // A grid that re-aims itself every poll drags a reader who has scrolled back to
  // wherever it thinks they should be, which is worse than opening in the wrong
  // place — so a range that has been placed against real chips is never placed
  // again.
  //
  // The one exception, and the reason `aimed` remembers more than a key: a new
  // range paints BEFORE its windowed fetch answers, so the first placement has no
  // chips to aim at. Exactly one upgrade is allowed, when chips first arrive for
  // that range. After that the scroll is the reader's.
  const rangeKey = days.length ? `${dayKey(days[0])}:${days.length}` : "";
  const aimed = useRef({ key: "", withChips: false });
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const chips = [...chipsByDay.values()].flat();
    const moved = aimed.current.key !== rangeKey;
    if (!moved && (aimed.current.withChips || !chips.length)) return;
    el.scrollTo({ top: scrollTarget(chips, days, new Date(), el.clientHeight, HOUR_H) });
    aimed.current = { key: rangeKey, withChips: chips.length > 0 };
  }, [rangeKey, days, chipsByDay]);

  // "Daily", "Every 2 weeks on Monday" — the recurrence in words, per rule.
  // Built once rather than per chip: a daily task holds one chip a day across
  // the whole window, and every one of them asks the same question.
  const repeatByTemplate = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of entries) {
      const text = repeatTextFor(e.id, entries);
      if (text) map.set(e.id, text);
    }
    return map;
  }, [entries]);

  const now = new Date();
  const label = rangeLabel(days);

  const shift = (delta: number) => {
    setStart((s) => stepRange(s, range, delta));
    setOpenChip(null);
  };

  const goToday = useCallback(() => {
    setStart(rangeStart(new Date(), range));
    setOpenChip(null);
  }, [range]);

  const clickGrid = (day: Date, e: React.MouseEvent<HTMLDivElement>) => {
    // Only the column itself — a click that landed on a chip is that chip's,
    // and it stopPropagation()s before reaching here.
    const rect = e.currentTarget.getBoundingClientRect();
    const minutes = ((e.clientY - rect.top) / HOUR_H) * 60;
    // Clamped to the day's last slot: rounding at the very bottom of a column
    // could reach 24:00, and setMinutes(1440) rolls into TOMORROW — a click in
    // Friday opening a task for Saturday (Bugbot, PR #538).
    const snapped = Math.min(
      Math.round(minutes / SNAP_MIN) * SNAP_MIN,
      24 * 60 - SNAP_MIN,
    );
    const t = new Date(day);
    t.setMinutes(snapped, 0, 0);
    onCreateAt(t);
  };

  const cols = { ["--cal-days" as string]: days.length } as React.CSSProperties;

  return (
    <div className={"schedule-cal" + (range === "4day" ? " is-wide" : "")} style={cols}>
      {/* Controls left — chevrons flanking Today, then the range pair — and
          where-you-are on the right edge (Akshil, 2026-08-14). */}
      <div className="schedule-cal-bar">
        <div className="schedule-cal-nav">
          <button type="button" className="btn btn-secondary" onClick={() => shift(-1)}
                  aria-label={range === "week" ? "Previous week" : "Previous 4 days"}>‹</button>
          <button type="button" className="btn btn-secondary" onClick={goToday}>Today</button>
          <button type="button" className="btn btn-secondary" onClick={() => shift(1)}
                  aria-label={range === "week" ? "Next week" : "Next 4 days"}>›</button>
        </div>
        <div className="schedule-form-seg" role="radiogroup" aria-label="Range">
          <button type="button"
                  className={"btn btn-secondary" + (range === "4day" ? " is-active" : "")}
                  aria-pressed={range === "4day"}
                  onClick={() => pickRange("4day")}>
            4 days
          </button>
          <button type="button"
                  className={"btn btn-secondary" + (range === "week" ? " is-active" : "")}
                  aria-pressed={range === "week"}
                  onClick={() => pickRange("week")}>
            Week
          </button>
        </div>
        <span className="schedule-cal-range">{label}</span>
      </div>

      <div className="schedule-cal-head">
        <div className="schedule-cal-gutter" aria-hidden="true" />
        {days.map((day) => (
          <div key={dayKey(day)}
               className={"schedule-cal-day-head" + (sameDay(day, now) ? " is-today" : "")}>
            <span className="schedule-cal-day-name">
              {day.toLocaleDateString(undefined, { weekday: "short" })}
            </span>
            <span className="schedule-cal-day-num">{day.getDate()}</span>
          </div>
        ))}
      </div>

      {/* No all-day row. What used to sit here — the Queued strip — was a band
          across the week for work that belongs to individual messages, and it
          reads as one now: on the thread rows inside the chip popover. */}

      <div className="schedule-cal-scroll" ref={scrollRef}>
        <div className="schedule-cal-grid" style={{ height: 24 * HOUR_H }}>
          <div className="schedule-cal-gutter">
            {Array.from({ length: 23 }, (_, i) => (
              <span key={i + 1} className="schedule-cal-hour" style={{ top: (i + 1) * HOUR_H }}>
                {new Date(2000, 0, 1, i + 1).toLocaleTimeString(undefined, { hour: "numeric" })}
              </span>
            ))}
          </div>
          {days.map((day) => {
            const key = dayKey(day);
            // Lane packing is Google's side-by-side split for chips that
            // overlap. It is doing LESS work than it used to: one task can no
            // longer produce two chips in a day, so lanes only ever separate
            // DIFFERENT tasks that happen to start close together.
            const chips = assignLanes(chipsByDay.get(key) ?? []);
            return (
              <div key={key}
                   className={"schedule-cal-col" + (sameDay(day, now) ? " is-today" : "")}
                   onClick={(e) => clickGrid(day, e)}>
                {sameDay(day, now) && (
                  <div className="schedule-cal-now"
                       style={{ top: (now.getHours() + now.getMinutes() / 60) * HOUR_H }} />
                )}
                {chips.map((chip) => {
                  const later = chip.messages
                    .slice(1)
                    .map((m) => clockTime(new Date(m.at * 1000)));
                  // One string for the tooltip AND the accessible name. Named
                  // explicitly rather than left to fall out of the visible
                  // text: the name then survives the narrow-lane rule that
                  // hides the time, and the ↻ can go decorative instead of
                  // announcing a bare "Repeats" (audit 2026-08-17).
                  const name = chipAccessibleName(
                    chip.task.title || firstLine(chip.anchor.body),
                    repeatByTemplate.get(chip.templateId) ?? "",
                    clockTime(chip.time),
                    later,
                  );
                  return (
                    <button
                      key={chip.key}
                      type="button"
                      className={
                        "schedule-cal-chip schedule-cal-chip--" + chip.tone +
                        (chip.projected ? " is-projected" : "") +
                        (chip.time < now ? " is-past" : "") +
                        // Three lanes leave a chip ~70px wide, which cannot hold
                        // both the title and the clock — the title collapsed to
                        // two characters (audit 2026-08-16). The CSS drops the
                        // time; the hour ruler already carries it.
                        (chip.lanes >= 3 ? " is-narrow" : "")
                      }
                      style={{
                        top: (minutesOfDay(chip.time) / 60) * HOUR_H,
                        // Side-by-side equal lanes, Google Calendar's split. The
                        // z-index stays a variable, NOT inline: inline would beat
                        // the stylesheet's :hover raise (QA 2026-08-14), and an
                        // inline style outranks any selector.
                        left: `calc(${(chip.lane * 100) / chip.lanes}% + 1px)`,
                        width: `calc(${100 / chip.lanes}% - 3px)`,
                        ["--lane" as string]: chip.lane,
                        // Same task, same colour, right across the grid.
                        ["--chip" as string]: `var(--task-c${chip.colour})`,
                      } as React.CSSProperties}
                      title={name}
                      aria-label={name}
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpenChip({ chip, x: e.clientX, y: e.clientY });
                      }}
                    >
                      <span className="schedule-cal-chip-text">
                        {chip.task.title || firstLine(chip.anchor.body)}
                      </span>
                      {chip.recurring && (
                        // Decorative. The recurrence is in the chip's own
                        // accessible name, in words ("Daily"), so a label here
                        // would both say less and collide with the New task
                        // form's own "Repeats" control.
                        <span className="schedule-cal-chip-rep" aria-hidden="true">↻</span>
                      )}
                      {chip.extra > 0 && (
                        // The mitigation, named: the later runs of the day have
                        // no chip of their own, so the anchor counts them and
                        // the popover lists them with their real times.
                        <span className="schedule-cal-chip-more"
                              title={`also ${later.join(", ")}`}>
                          +{chip.extra}
                        </span>
                      )}
                      <span className="schedule-cal-chip-time">{clockTime(chip.time)}</span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>

      {openChip && (
        <ChipPopover
          chip={openChip.chip}
          repeat={repeatByTemplate.get(openChip.chip.templateId) ?? ""}
          roles={roles}
          at={{ x: openChip.x, y: openChip.y }}
          onClose={() => setOpenChip(null)}
          onReload={onReload}
          onEditEntry={onEditEntry}
        />
      )}
    </div>
  );
}
