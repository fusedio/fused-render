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
// Colour is per PROJECT, hashed from the task's folder (schedule-lib.taskColour):
// five days of a daily task read as one thing across the grid, AND every task out
// of one repo carries that repo's hue (Akshil, 2026-08-17).
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
// its chips get a larger label, and it is what the calendar OPENS on: four
// readable columns starting at today beat seven cramped ones starting at a
// Monday that may be behind you. Which range is up is remembered, so that
// default only ever applies to a reader who has not chosen — see RANGE_KEY.
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
  archiveTask,
  deleteTask,
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
  chipAccessibleName,
  folderHref,
  groupScheduled,
  dayKey,
  DEFAULT_RANGE,
  initialRange,
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
  threadForDay,
  windowBounds,
} from "./schedule-lib";
import type { CalendarChip, CalendarRange, QueueRole } from "./schedule-lib";
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
  isRunningIn,
  messageTone as taskMessageTone,
  openMessageHref,
  popoverPill,
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
// a time plans four days at a time every time. A FIRST visit opens on the 4-day
// range (schedule-lib.DEFAULT_RANGE); a remembered Week is never stamped over,
// which is the same contract `fused-render:scheduled-view` has for the List /
// Board / Calendar switch this toggle now sits beside.
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
export const ICON_FOLDER = icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />);
export const ICON_PLUS = icon(<><path d="M12 5v14" /><path d="M5 12h14" /></>);
export const ICON_SHIELD = icon(<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />);
export const ICON_EDIT = icon(<><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>);
// Filing away — lucide `archive`, the same lidded box the List rows and the
// Board cards wear for the same verb (ScheduleTaskViews.ICON_ARCHIVE), redrawn
// here only because the two files keep separate `icon()` sizes.
export const ICON_ARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" /></>);
// Gone for good — lucide `trash-2`, the glyph the New task modal's own delete
// wears (NewJobModal.ICON_TRASH), redrawn at this file's icon size. Distinct
// from the archive box on purpose: the box keeps, the can does not, and the
// two verbs share one header slot across different task states.
export const ICON_TRASH = icon(
  <><path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <line x1="10" y1="11" x2="10" y2="17" />
    <line x1="14" y1="11" x2="14" y2="17" /></>);
// No repeat / skip / cancel glyphs any more (Akshil, 2026-08-19). The repeat
// arrows doubled the recurrence sentence they sat next to, and the skip/cancel
// pair left with the per-row actions they decorated — see the popover's thread
// rows for where they went and why.
export const ICON_NOTES = icon(<><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="14" y2="17" /></>);
export const ICON_RESTORE = icon(<><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></>);
// The three views, as marks — the List / Board / Calendar switcher in
// Scheduled.tsx. They live here with the rest of the page's vocabulary rather
// than in that file because this is where `icon()` is, and a second copy of
// that helper is how two icon sizes start. lucide `list`, `columns-3` and
// `calendar`, unmodified: the switcher is the first control a person reads on
// this page, so it should wear the shapes they already know from every other
// board and calendar they use rather than bespoke ones.
//
// ICON_NOTES above is three lines too, and stays a separate glyph: it means
// "this run has a note", and a mark that means two things on one page means
// neither. The list icon carries the leading dots that one does not.
export const ICON_VIEW_LIST = icon(
  <><line x1="9" y1="6" x2="20" y2="6" /><line x1="9" y1="12" x2="20" y2="12" />
    <line x1="9" y1="18" x2="20" y2="18" /><line x1="4" y1="6" x2="4.01" y2="6" />
    <line x1="4" y1="12" x2="4.01" y2="12" /><line x1="4" y1="18" x2="4.01" y2="18" /></>,
);
export const ICON_VIEW_BOARD = icon(
  <><rect x="3" y="3" width="18" height="18" rx="2" />
    <line x1="9" y1="3" x2="9" y2="21" /><line x1="15" y1="3" x2="15" y2="21" /></>,
);
export const ICON_VIEW_CALENDAR = icon(
  <><rect x="3" y="4" width="18" height="17" rx="2" />
    <line x1="3" y1="10" x2="21" y2="10" /><line x1="8" y1="2" x2="8" y2="6" />
    <line x1="16" y1="2" x2="16" y2="6" /></>,
);

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

  // Is the task actually working RIGHT NOW — tasks-lib's reading, asked of the
  // popover's merged list (a superset of chip.messages) so the freshly fetched
  // thread can light it too; ghosts are safe to include because isRunningNow
  // refuses anything that never started. This is the TASK-level fact, and it
  // gates exactly the things that are about the task: the header's lifecycle
  // slot (no destructive verb mid-run) and a one-off's pill, which IS its
  // task's status.
  const liveNow = isRunningIn(task, messages);

  // And the same rule asked of THE CLICKED DAY only — chip.messages, the exact
  // list the grid chip decides its own shimmer by (bugbot, 2026-08-19): a
  // recurring task runs on many days, and a popover opened on Tuesday must not
  // say In Progress because Thursday's occurrence happens to be in flight. This
  // is what the pill's word and its shimmering ink follow on a recurring task.
  const liveToday = isRunningIn(task, chip.messages);

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

  // THE HEADER'S ONE LIFECYCLE VERB (Akshil, 2026-08-19). The approved design
  // gives the header's right edge a pencil and ONE state-dependent slot:
  //
  //   done / failed  -> Archive — the same `api.archiveTask(task.key)` call the
  //                     List's row button and the Board's drop make, so the
  //                     three views file a task through one door. No confirm,
  //                     for the reason the List and Board ask none: archiving
  //                     is reversible by design (unarchiveTask);
  //   upcoming,      -> Delete — `api.deleteTask(task.key)`: the pending work
  //   archived          is cancelled and the ROW is tombstoned off every view.
  //                     The transcript survives (D306 — the server says so in
  //                     every answer), but the row does not come back on its
  //                     own, so this one is armed by its FIRST press and fired
  //                     by the SECOND — the same two-press confirm the template
  //                     editor's Disable uses (RowEditorModal), which keeps a
  //                     modal from stacking on a popover;
  //   running        -> nothing. No destructive verb mid-run — the server
  //                     refuses a running delete with a 409 anyway, and a
  //                     button whose only outcome is a refusal is worse than
  //                     no button.
  //
  // ONE slot means the two verbs never show together: a settled outcome takes
  // Archive (it wins the overlap), and Delete waits for the task in the
  // Archive lane — done/failed work is a result somebody may still want filed,
  // not walked straight past to the shredder.
  const col = taskColumn(task);
  const canArchive = !liveNow && (task.failed || col === "done" || col === "failed");
  const canDelete =
    !liveNow && !canArchive && (col === "upcoming" || col === "archived");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const archive = async () => {
    setRunning(true);
    setError("");
    try {
      await archiveTask(task.key);
      // Archiving may take the task's chips off the grid entirely, so the panel
      // closes rather than describing a chip the next poll has removed — the
      // exact lifecycle rule runTask documents above.
      onReload();
      onClose();
    } catch (e) {
      // The server's own sentence, in the quiet line the run button already
      // uses, and a reload so the grid corrects itself either way.
      setError((e as Error).message);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  const doDelete = async () => {
    // The confirm step: the first press only ARMS (the button goes red and its
    // name changes to say what the next press does), the second one deletes.
    // Closing the popover disarms it for free — the state unmounts with the
    // panel — so a stale "armed" can never greet the next open.
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setRunning(true);
    setError("");
    try {
      await deleteTask(task.key);
      // The task's chips are about to leave the grid entirely — the same
      // close-and-reload archiving does, for the stronger reason.
      onReload();
      onClose();
    } catch (e) {
      // A 409 lands here too ("that task is running — stop the run first"):
      // the server's own sentence in the quiet line, and the button DISARMS,
      // so the next press starts the two-step over rather than firing on
      // stale consent.
      setError((e as Error).message);
      setConfirmDelete(false);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  // NO PER-ROW CANCEL ANY MORE (Akshil, 2026-08-19: "let us hide skip for
  // now"). The two cancels this panel used to make — queue endpoint for a
  // QUEUED entry, schedule cancel for a future one — left with the ⊗/✕ buttons
  // that fired them; the List still offers both, and schedule-lib.msgCancelKind
  // (which decided per row, and is still tested) keeps feeding msgNote so the
  // row's tooltip goes on saying "queued" and "too late to cancel". If the
  // buttons come back, the handler is in git history at this spot.

  const dayLabel = chip.time.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  // THE HEADER'S PILL — tasks-lib.popoverPill's decision, in the app's five
  // words, and the full reasoning is on that function: a ONE-OFF's pill is its
  // task's status (taskColumn + failed, exactly what the List's row and the
  // Board's card hand StatusIcon); a REPEATING task's pill answers for the
  // clicked OCCURRENCE — In Progress while THIS DAY works (`liveToday`, the
  // chip's own reading; `liveNow` would say In Progress on every day of a rule
  // whose run is live somewhere else), Upcoming for a projected or not-yet-run
  // day, and a past day's own outcome (Done/Failed) from the newest real run
  // among the rows drawn right below it (Akshil, 2026-08-19: a rule's
  // task-level column is nearly always "upcoming", which made the pill useless
  // on the one view that is about days).
  //
  // AND ALWAYS SOLID (Akshil, 2026-08-19). The pill used to inherit the clicked
  // chip's dashes through `chip.projected`; the dashes are a DAY-scoped drawing
  // ("nothing written down yet") and stay on the grid's ghost chips and the
  // ghost rings on the occurrence rows — never on a status word.
  const recurring = Boolean(chip.recurring || repeat);
  // What the pill (word and shimmer alike) means by "running": the clicked
  // day's occurrences for a rule, the task itself for a one-off — so the pill
  // and the chip it was opened from can never disagree about the same day.
  const pillLive = recurring ? liveToday : liveNow;
  const pill = useMemo(
    () => popoverPill(task, recurring, chip.projected, pillLive, today),
    [task, recurring, chip.projected, pillLive, today],
  );

  const nowSec = Math.floor(Date.now() / 1000);

  const row = (m: TaskMessage, sameDayRow: boolean) => {
    const status = runStatus(m, taskMessageTone(m));
    const role = queueRole(m, roles, nowSec);
    const t = new Date(m.at * 1000);
    // schedule-lib's per-row decision, still recomputed from the freshly-polled
    // queue — not for a control any more (the cancel button is gone, see below)
    // but for the NOTE, which is the words for the states the ring cannot
    // spell: "queued", "ran 2 days late", "too late to cancel".
    const kind = msgCancelKind(m, role);
    const note = msgNote(m, kind);
    return (
      <li key={m.message_id} className="schedule-cal-msg">
        <button
          type="button"
          className={"schedule-cal-msg-open" + (canOpen(m) ? "" : " is-inert")}
          onClick={() => openMessage(m)}
          // The status WORD moved in here when it left the row's ink: the ring
          // carries the fact and the tooltip carries the name for it, which is
          // the same division the row's time already makes (relative in the
          // ink, exact in the title).
          title={[t.toLocaleString(), status.label, note].filter(Boolean).join(" — ")}
        >
          {/* The Board's ring, at row scale. `is-ghost` is the one thing the
              Board has no case for: a projected run, dashed because the word
              beside it says "Upcoming" like any other scheduled run and
              something has to tell the two apart.

              It carries this row's UNREAD too, since 2026-08-18 — a filled centre
              on a run nobody has opened, hollow once they have — which is the same
              glyph the List's thread rows and the Board's cards wear, and it
              replaced a blue dot of the calendar's own that sat after the body.
              Three views, one mark. No `count`: this is a leaf, and "1 unread"
              over a dot that already means unread is a caption for nothing. */}
          <span className={"schedule-cal-ring" + (status.projected ? " is-ghost" : "")}>
            <StatusIcon status={status.column} failed={status.failed} unread={m.unread} />
          </span>
          <span className="schedule-cal-msg-time">
            {sameDayRow
              ? clockTime(t)
              : t.toLocaleDateString(undefined, { month: "short", day: "numeric" })}
          </span>
          {/* RING AND TIME, AND NOTHING ELSE (Akshil, 2026-08-19). The row used
              to carry two more things, and both are gone:

              THE BODY's first line — already trimmed on 2026-08-18 to only rows
              that said something new — is out entirely: this popover is about
              ONE task, its title is the headline above, and even a genuinely
              different prompt is one click away in the transcript the row opens.

              THE NOTE ("queued", "ran 2 days late", "too late to cancel") is
              out of the ink too. schedule-lib.msgNote still writes it and the
              row's title above still carries it — the fact survives as a
              tooltip, the row just stops being a sentence. What distinguishes
              one occurrence from another is the ring's state and the TIME, and
              those are what is left. */}
        </button>
        {/* NO ⊗ SKIP / ✕ CANCEL BUTTON, AND NO HELD SPINNER (Akshil,
            2026-08-19: "let us hide skip for now"). The per-row cancel left,
            and the turning glyph left with it: its whole argument was holding
            the SAME 24px box so the note did not jump sideways when the button
            vanished queued → sending — a slot-stability fix for a slot that no
            longer exists. Sending itself was never the spinner's fact to carry;
            the ring and the tooltip's note still say it. */}
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
        {/* No ↻ glyph beside the id any more (Akshil, 2026-08-19). It was
            decorative by its own admission — the recurrence is spelled out in
            words two lines down — and a glyph whose label is already printed
            only ever repeats it.

            The pill sits HERE, against the id it labels (Akshil, 2026-08-19) —
            swatch, id, status as one left-aligned statement of what this is —
            and the header's right edge holds the panel's quiet tools instead:
            the pencil (the same handler the footer's Edit button used to fire)
            and the one lifecycle verb `canArchive` above decides. */}
        <span
          className={
            `schedule-state schedule-state--${pill.column}` +
            (pillLive ? " is-running" : "")
          }
        >
          {pill.label}
        </span>
        <div className="schedule-cal-pop-tools">
          {onEditEntry && chip.anchor.entry_id && (
            <button
              type="button"
              className="schedule-cal-pop-tool"
              title="Edit"
              aria-label="Edit"
              onClick={() => {
                onEditEntry(chip.anchor.template_id || chip.anchor.entry_id);
                onClose();
              }}
            >
              {ICON_EDIT}
            </button>
          )}
          {canArchive && (
            <button
              type="button"
              className="schedule-cal-pop-tool"
              title="Archive"
              aria-label="Archive"
              disabled={running}
              onClick={() => void archive()}
            >
              {ICON_ARCHIVE}
            </button>
          )}
          {canDelete && (
            // The two-press confirm: the VISIBLE state and the accessible name
            // change together, so what a screen reader hears is exactly what
            // the red is saying — the next press is the one that deletes.
            <button
              type="button"
              className={
                "schedule-cal-pop-tool schedule-cal-pop-tool--danger" +
                (confirmDelete ? " is-armed" : "")
              }
              title={confirmDelete ? "Click again to delete" : "Delete"}
              aria-label={confirmDelete ? "Click again to delete" : "Delete"}
              disabled={running}
              onClick={() => void doDelete()}
            >
              {ICON_TRASH}
            </button>
          )}
        </div>
      </div>

      {/* TITLE THEN DESCRIPTION, as one writing block, exactly as the New task
          modal presents the same two fields (new-task.css `.new-task-write`).
          They are what the user WROTE, and a form that asks for a title above a
          description and then plays them back as a heading and an icon-led fact
          row has made the same pair look like two different kinds of thing.

          So the description loses its icon and its place in the fact list. It
          was drawn there with a notes glyph, which put "what this task is" in
          the same register as "which folder it runs in" — one is prose to read,
          the other is a value to recognise, and the icon column is for the
          second. Smaller and muted under the title says the same hierarchy
          without spending a glyph.

          The title takes the size the hierarchy needs: it is the first thing
          read in this panel and it was the same 14px as the fact rows' 12px,
          which is not a step. */}
      <div className="schedule-pop-write">
        <p className="schedule-pop-title">{task.title || firstLine(chip.anchor.body)}</p>
        {task.description && (
          <p className="schedule-pop-desc">{task.description}</p>
        )}
      </div>

      {/* THE TWO FACTS AS ONE MUTED LINE (Akshil, 2026-08-19): "Every 2 weeks
          on Monday · /path/to/folder". They were two icon-led rows — the Google
          event-card shape — but in a panel this small two single-fact rows read
          as a form, and the icons were carrying labels the words already carry.
          The recurrence keeps whole words (it is read); the folder ellipsizes
          (it is recognised, and its tail matters less than its head); the "·"
          exists only when both sides do. */}
      {(repeat || task.target) && (
        <p className="schedule-pop-meta">
          {repeat && <span className="schedule-pop-meta-rep">{repeat}</span>}
          {repeat && task.target && (
            <span className="schedule-pop-meta-sep" aria-hidden="true">·</span>
          )}
          {task.target && <code title={task.target}>{task.target}</code>}
        </p>
      )}

      {/* ONE CONTINUOUS LIST (Akshil, 2026-08-19). The "Earlier…" header made
          the rest of the thread a second labelled block, and with the rows
          slimmed to ring + time the label was bigger than what it introduced.
          The split itself stays —
          threadForDay still puts the day's runs first, then the rest newest-
          first — and the seam is already visible without a header: the day's
          rows print clock times, everything after prints dates. */}
      <div className="schedule-cal-thread">
        <p className="schedule-cal-thread-head">{dayLabel}</p>
        <ul className="schedule-cal-msgs">
          {today.map((m) => row(m, true))}
          {rest.map((m) => row(m, false))}
        </ul>
      </div>

      {error && <p className="schedule-card-why">{error}</p>}

      {/* NO Edit DOWN HERE ANY MORE (Akshil, 2026-08-19): the pencil moved to
          the header's tool rail, same handler, so the footer is left holding
          only the two things a footer is for — going somewhere (Open in
          Explorer) and starting work (Run now / Re-run). */}
      <div className="schedule-card-actions">
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
    // during first render — a preference must never cost the page. WHICH range a
    // given stored value means is schedule-lib.initialRange's answer and is
    // tested there; a store that cannot be read is the same situation as an
    // empty one, so both land on the same default.
    try {
      return initialRange(localStorage.getItem(RANGE_KEY));
    } catch {
      return DEFAULT_RANGE;
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
      {/* `‹ Today ›`  ……  `[ 4 days | Week ]  August 2026`.
          Stepping stays anchored at the left edge, where the view toggle above
          it also starts; the range pair moved across to sit immediately left of
          the month, which stays the rightmost thing on the row (Akshil,
          2026-08-17). DOM order IS reading order here — the right end is one box
          carrying the bar's ONE auto margin (`.schedule-cal-bar-end`, and the
          rule there says why a second auto margin cannot exist). The toggle
          itself is the page's own `.schedule-form-seg`, the same control as
          List / Board / Calendar, reused and not restyled. */}
      <div className="schedule-cal-bar">
        <div className="schedule-cal-nav">
          <button type="button" className="btn btn-secondary" onClick={() => shift(-1)}
                  aria-label={range === "week" ? "Previous week" : "Previous 4 days"}>‹</button>
          <button type="button" className="btn btn-secondary" onClick={goToday}>Today</button>
          <button type="button" className="btn btn-secondary" onClick={() => shift(1)}
                  aria-label={range === "week" ? "Next week" : "Next 4 days"}>›</button>
        </div>
        {/* The toggle and the month are ONE group, and the wrapper is what makes
            them one: it carries the row's single auto margin (so the pair goes
            to the right edge in the order written, month last) and it keeps them
            together when the bar runs out of room — a narrow window drops the
            pair onto a second line still right-aligned, instead of leaving the
            toggle up top and stranding "August 2026" alone at the left. */}
        <div className="schedule-cal-bar-end">
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
                        // RUNNING RIGHT NOW. The List has an In Progress section
                        // and the Board an In Progress lane; a calendar is
                        // ordered by time and has neither, so until now the one
                        // chip on today's grid that was actually working looked
                        // exactly like the four that had finished. The rule is
                        // tasks-lib's — the same reading of state and turn the
                        // other two views file a card by — and the CSS is the
                        // app's ONE running treatment: the title's ink turns
                        // the In Progress yellow and shimmers, exactly as the
                        // sidebar's "N running" does, with a flat yellow label
                        // under prefers-reduced-motion.
                        //
                        // ASKED OF THE WHOLE CHIP, not of its anchor (bugbot,
                        // 2026-08-18). A chip is a task on a DAY and the anchor
                        // is only that day's EARLIEST message, so a day whose
                        // 05:00 run has finished and whose 14:00 run is in flight
                        // was asking about the finished one — while the running
                        // mark landed on whichever day held the task's newest
                        // row, routinely tomorrow's pending occurrence.
                        (isRunningIn(chip.task, chip.messages) ? " is-running" : "") +
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
                        // Same FOLDER, same colour, right across the grid.
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
