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
// Above the grid, where Google puts its all-day row, sits the QUEUED strip: what
// is past due waiting to be claimed and what is mid-flight (getScheduleQueue),
// and the cancel surface for both. Cancelling races the claim, so an entry the
// server has already handed to the sender comes back REFUSED — and is said so,
// never silently dropped. Missed occurrences are NOT in the queue: they stay
// greyed on their original past slot so history stays readable.
//
// The layout maths is all in schedule-lib.ts and tested there; this file adds
// pixels and nothing else.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  groupScheduled,
  dayKey,
  firstLine,
  isProjected,
  messageTone,
  minutesOfDay,
  queueSummary,
  rangeDays,
  rangeLabel,
  rangeStart,
  relativeDue,
  repeatTextFor,
  sameDay,
  stepRange,
  taskChips,
  threadForDay,
  windowBounds,
} from "./schedule-lib";
import type { CalendarChip, CalendarRange } from "./schedule-lib";
// Where a click GOES is owned by tasks-lib, and by nothing else: taskHref opens
// the thread, messageHref opens the one turn inside it. The calendar used to
// build that url itself out of explorerUrl, which silently dropped the message
// anchor and landed every reader at the top of the conversation.
import { openMessageHref, taskHref } from "./tasks-lib";

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
// The queue's own glyph: work that has come due and is waiting to go up.
export const ICON_QUEUE = icon(<><path d="M12 19V5" /><path d="M5 12l7-7 7 7" /></>);

// A message's own words for what happened to it — the vocabulary the popover's
// thread is read by. Short, because it sits beside a time on one line.
const MESSAGE_LABELS: Record<string, string> = {
  upcoming: "Scheduled",
  sending: "Running",
  ran: "Ran",
  error: "Failed",
  missed: "Missed",
  skipped: "Skipped",
};

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

// Keep a floating panel on screen: flip left of the click when the right edge
// would clip, and clamp vertically. Sizes match the CSS.
function anchorStyle(x: number, y: number, w: number, h: number) {
  return {
    left: Math.max(8, Math.min(x + 8, window.innerWidth - w - 16)),
    top: Math.max(8, Math.min(y + 8, window.innerHeight - h - 16)),
  };
}

// ---- The chip popover ----------------------------------------------------------
// The mitigation for the one known cost of the one-chip-per-day rule, so it has
// to be good: the task's thread, THAT DAY's messages first with their real
// times, then the rest newest-first. The 7pm run that has no chip of its own is
// right here, named and clickable.

function ChipPopover({
  chip,
  repeat,
  at,
  onClose,
  onReload,
  onEditEntry,
}: {
  chip: CalendarChip;
  /** The recurrence in words ("Daily"), "" for a one-off. */
  repeat: string;
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

  const cancelMessage = async (m: TaskMessage) => {
    setBusy(m.message_id);
    setError("");
    try {
      await cancelScheduledMessage(m.entry_id);
      onReload();
      onClose();
    } catch (e) {
      // The likeliest failure is the honest race — it fired while the popover
      // was open — so the server's words go up and the page refreshes anyway.
      setError((e as Error).message);
      onReload();
    } finally {
      setBusy("");
    }
  };

  const pos = anchorStyle(at.x, at.y, 360, 420);
  const dayLabel = chip.time.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  const row = (m: TaskMessage, sameDayRow: boolean) => {
    const tone = messageTone(m);
    const t = new Date(m.at * 1000);
    const cancellable = m.state === "pending" && !!m.entry_id && !isProjected(m);
    return (
      <li key={m.message_id} className="schedule-cal-msg">
        <button
          type="button"
          className={"schedule-cal-msg-open" + (canOpen(m) ? "" : " is-inert")}
          onClick={() => openMessage(m)}
          title={t.toLocaleString()}
        >
          <span className={`schedule-cal-msg-dot schedule-cal-msg-dot--${tone}`} aria-hidden="true" />
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
          <span className="schedule-cal-msg-state">
            {/* "Projected", not "Scheduled": nothing has been written down for
                this one yet — the rule simply says it will happen. */}
            {isProjected(m) ? "Projected" : MESSAGE_LABELS[tone] ?? tone}
          </span>
        </button>
        {cancellable && (
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
            onClick={() => cancelMessage(m)}
          >
            {m.template_id ? ICON_SKIP : ICON_CANCEL}
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
        <span className={`schedule-state schedule-state--${chip.tone}`}>
          {MESSAGE_LABELS[chip.tone] ?? chip.tone}
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

// ---- The queued strip ----------------------------------------------------------

function QueuePopover({
  queued,
  running,
  at,
  onClose,
  onReload,
}: {
  queued: ScheduledMessage[];
  running: ScheduledMessage[];
  at: { x: number; y: number };
  onClose: () => void;
  onReload: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  useDismiss(ref, onClose);

  const act = async (ids: string[] | "all", tag: string) => {
    setBusy(tag);
    setNote("");
    try {
      const r = await cancelQueued(ids);
      // `refused` is the honest half of the answer: the server had already
      // claimed that entry for sending. Say so — dropping it silently teaches
      // the reader that the button lies.
      setNote(cancelOutcome(r.cancelled ?? [], r.refused ?? []));
      onReload();
      if (!(r.refused ?? []).length) onClose();
    } catch (e) {
      setNote((e as Error).message);
      onReload();
    } finally {
      setBusy("");
    }
  };

  const pos = anchorStyle(at.x, at.y, 360, 320);

  const row = (entry: ScheduledMessage, live: boolean) => (
    <li key={entry.id} className="schedule-cal-q-row">
      {live ? (
        // Decorative: the row's own "running" text three columns along is the
        // fact; labelling the dot too says it twice.
        <span className="schedule-tv-pulse" aria-hidden="true" />
      ) : (
        <span className="schedule-cal-q-dot" aria-hidden="true" />
      )}
      <span className="schedule-cal-q-body" title={entry.message}>
        {firstLine(entry.message) || "(no prompt)"}
      </span>
      <span className="schedule-cal-q-when">
        {live ? "running" : relativeDue(entry.due)}
      </span>
      <button
        type="button"
        className="schedule-cal-msg-act"
        disabled={busy !== ""}
        aria-label={
          `Cancel ${live ? "the running" : "the queued"} message: ` +
          (firstLine(entry.message) || "(no prompt)")
        }
        title={live ? "Cancel this run" : "Cancel this message"}
        onClick={() => act([entry.id], entry.id)}
      >
        {ICON_CANCEL}
      </button>
    </li>
  );

  return (
    <div ref={ref} className="schedule-cal-popover schedule-cal-qpop"
         style={pos} role="dialog" aria-label="Queued messages">
      <div className="schedule-cal-pop-head">
        <span className="schedule-cal-pop-id">Queued</span>
        <span className="schedule-cal-q-count">
          {running.length + queued.length}
        </span>
      </div>
      {/* The one thing this strip exists to be honest about: nothing fires
          while the app is closed, so this is what came due meanwhile. */}
      <p className="schedule-cal-q-note">
        Past due while the app was closed. These run as soon as they are claimed.
      </p>
      <ul className="schedule-cal-msgs">
        {running.map((e) => row(e, true))}
        {queued.map((e) => row(e, false))}
      </ul>
      {note && <p className="schedule-card-why">{note}</p>}
      <div className="schedule-card-actions">
        <button type="button" className="btn btn-secondary" disabled={busy !== ""}
                onClick={() => act("all", "all")}>
          {ICON_CANCEL} {busy === "all" ? "Cancelling…" : "Cancel all"}
        </button>
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
  /** getScheduleQueue().queued — past due, waiting to be claimed. */
  queued?: ScheduledMessage[];
  /** getScheduleQueue().running — mid-flight. */
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
  const [openQueue, setOpenQueue] = useState<{ x: number; y: number } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

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
  const todayIndex = days.findIndex((d) => sameDay(d, now));
  const queueCount = queued.length + running.length;

  const shift = (delta: number) => {
    setStart((s) => stepRange(s, range, delta));
    setOpenChip(null);
    setOpenQueue(null);
  };

  const goToday = useCallback(() => {
    setStart(rangeStart(new Date(), range));
    setOpenChip(null);
    setOpenQueue(null);
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

      {/* An empty queue renders NOTHING — no strip, no empty-state chrome. The
          row only exists when there is work in it. */}
      {queueCount > 0 && (
        <div className="schedule-cal-queue">
          <span className="schedule-cal-queue-label">Queued</span>
          <button
            type="button"
            className="schedule-cal-queue-strip"
            // Today's column, running to the grid's right edge — the all-day
            // row's position, on the one day the queue is about. When today is
            // off-screen the strip still shows (the work is still real) and
            // takes the whole row instead.
            style={{ gridColumn: `${(todayIndex < 0 ? 0 : todayIndex) + 2} / -1` }}
            onClick={(e) => setOpenQueue({ x: e.clientX, y: e.clientY })}
          >
            {running.length > 0 ? (
              <span className="schedule-tv-pulse" aria-hidden="true" />
            ) : (
              <span className="schedule-cal-queue-icon" aria-hidden="true">{ICON_QUEUE}</span>
            )}
            <span className="schedule-cal-queue-text">{queueSummary(queued, running)}</span>
            <span className="schedule-cal-queue-count">{queueCount}</span>
          </button>
        </div>
      )}

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
          at={{ x: openChip.x, y: openChip.y }}
          onClose={() => setOpenChip(null)}
          onReload={onReload}
          onEditEntry={onEditEntry}
        />
      )}
      {openQueue && queueCount > 0 && (
        <QueuePopover
          queued={queued}
          running={running}
          at={openQueue}
          onClose={() => setOpenQueue(null)}
          onReload={onReload}
        />
      )}
    </div>
  );
}
