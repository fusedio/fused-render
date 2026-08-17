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
} from "@platform/lib/api";
import type { ScheduledMessage, Task, TaskMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import {
  assignLanes,
  calendarThreads,
  cancelOutcome,
  chipAccessibleName,
  dayStatus,
  groupScheduled,
  dayKey,
  firstLine,
  lateText,
  minutesOfDay,
  popoverPos,
  queueRole,
  queueRoles,
  rangeDays,
  rangeLabel,
  rangeStart,
  repeatTextFor,
  runStatus,
  sameDay,
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
import { messageTone as taskMessageTone, openMessageHref, taskHref } from "./tasks-lib";
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
  // same function the List's task row uses, and null when there is no session.
  const threadHref = taskHref(task);

  // Two cancels, one button. A QUEUED or RUNNING entry goes through the queue
  // endpoint, which is the only one that can answer honestly when the race is
  // lost: an entry already claimed for sending comes back `refused`, and that
  // sentence goes up rather than the popover closing as if it had worked.
  // Everything still in the future is a plain schedule cancel.
  const cancelMessage = async (m: TaskMessage, role: QueueRole) => {
    setBusy(m.message_id);
    setError("");
    try {
      if (role) {
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

  // The day's pill, in the one vocabulary: the column comes from tasks-lib, the
  // word from schedule-lib, and the two facts the word folds away (failed,
  // projected) come back as the pill's colour and its dashes.
  const day = useMemo(
    () => dayStatus(chip.messages.map((m) => runStatus(m, taskMessageTone(m)))),
    [chip.messages],
  );

  const nowSec = Math.floor(Date.now() / 1000);

  const row = (m: TaskMessage, sameDayRow: boolean) => {
    const status = runStatus(m, taskMessageTone(m));
    const role = queueRole(m, roles, nowSec);
    const t = new Date(m.at * 1000);
    // A running entry is cancellable too — that is what the strip's own cancel
    // did, and it is the burst case's only brake. The server still gets to
    // refuse it, which is the whole point of routing through cancelQueued.
    const cancellable =
      !!m.entry_id && !status.projected && (m.state === "pending" || role === "running");
    // The row's second line of fact, when there is one. A catch-up says how far
    // behind it ran (`at` vs `ran_at` — never the chip's position); a queued one
    // says it is waiting to be claimed, which "Upcoming" alone does not.
    const note = lateText(m) || (role === "queued" ? "queued" : "");
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
          {note && <span className="schedule-cal-msg-note">{note}</span>}
          {/* The app's word, and only ever one of its five. */}
          <span className="schedule-cal-msg-state">{status.label}</span>
        </button>
        {cancellable && (
          <button
            type="button"
            className="schedule-cal-msg-act"
            disabled={busy === m.message_id}
            aria-label={
              role === "running"
                ? `Cancel the ${clockTime(t)} run, which is already going`
                : m.template_id
                  ? `Skip the ${clockTime(t)} run`
                  : `Cancel the ${clockTime(t)} message`
            }
            title={
              role === "running"
                ? "Cancel this run"
                : m.template_id
                  ? "Skip this run"
                  : "Cancel this message"
            }
            onClick={() => cancelMessage(m, role)}
          >
            {m.template_id && role !== "running" ? ICON_SKIP : ICON_CANCEL}
          </button>
        )}
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
            `schedule-state schedule-state--${day.column}` +
            (day.projected ? " is-projected" : "")
          }
        >
          {day.label}
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

  // First paint lands just above 7am, not midnight — the band where schedules
  // live. The 12px of slack keeps the 7am gutter label (centred on its line)
  // from being cut in half at the container's top edge.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 7 * HOUR_H - 12 });
  }, []);

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
  // a future run obeys exactly the same one-chip-per-day rule a real one does.
  const threads = useMemo(
    () => calendarThreads(tasks, entries, windowed),
    [tasks, entries, windowed],
  );

  const chipsByDay = useMemo(
    () => taskChips(tasks, days, threads),
    [tasks, days, threads],
  );

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
