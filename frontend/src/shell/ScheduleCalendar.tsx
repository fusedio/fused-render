// The Tasks page's Calendar view — the same unit the List and the Board show (a
// TASK), on a time axis.
//
// The rule the whole file is built on: ONE CHIP PER TASK PER DAY, anchored at
// that task's EARLIEST message that day. Later messages the same day nest
// INSIDE it and the anchor carries the count (`+23`, not 24 chips). The axis
// decides PLACEMENT — it does not get to change the unit. The accepted cost is
// that a task's 7pm run has no chip at 7pm; the `+N` names it and the detail
// panel lists it with its real time, which is why the panel is not an
// afterthought.
//
// Chips are a FIXED one line tall: a message has a start time and no duration.
// Colour is per PROJECT, hashed from the task's folder (schedule-lib.taskColour)
// onto the categorical chart tokens — never a status colour. An ARCHIVED task
// draws nothing (taskChips). A rule's shape is drawn in both directions: future
// occurrences from the server's projection, past skipped slots from a client
// walk; ghosts are outlined and faded and never created.
//
// Two ranges: the week, and a "4 days" range that opens by default and is
// remembered (RANGE_KEY). Queued work rides the thread rows it belongs to, not
// a strip across the grid. ONE STATUS VOCABULARY: the panel's pill and every
// row say the app's five words; the column comes from tasks-lib.messageTone,
// the wording from schedule-lib.runStatus. The pill is the TASK's status (or
// the clicked DAY's, on a rule — tasks-lib.popoverPill); each row is its own
// run's. Run now / Re-run is tasks-lib.taskRunIntent, the List's button.
// Chips are placed by `at` (the scheduled time), never `ran_at`.
//
// THE DETAIL IS A RIGHT-SIDE PANEL, not a floating popover (Flow rule 8): a
// chip click docks a PropertiesPanel beside the grid; Escape or ✕ closes it.
// The layout maths is all in schedule-lib.ts and tested there.
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Pencil,
  Play,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
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
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { PropertiesPanel, PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { StatusBadge, StatusDot } from "@platform/ui/flow/StatusIcon";
import { Muted, SectionHeading, Tiny } from "@platform/ui/flow/Typography";
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
// Where a click GOES is owned by tasks-lib: taskHref opens the thread,
// messageHref the one turn inside it. messageTone is aliased because
// schedule-lib exports a same-named function producing a calendar tone.
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
import { IdChip, StatusIcon, columnBucket, folderChartVar } from "./tasks-ui";

// One hour of grid, in px. 44 puts a full day at ~1050px — two runs half an
// hour apart do not collide, and the 8am–6pm band fits a laptop viewport.
const HOUR_H = 44;
// One chip's height in px. Mirrored by schedule-lib's CHIP_H (scrollTarget).
const CHIP_H = 21;

// Empty-grid clicks snap to the half hour.
const SNAP_MIN = 30;

// Which range is up, remembered across visits. A FIRST visit opens on the
// 4-day range (schedule-lib.DEFAULT_RANGE).
const RANGE_KEY = "fused-render:scheduled-cal-range";

// Small stroke icons at button scale. ICON_CLOCK / ICON_FOLDER / ICON_PLUS are
// imported by NewJobModal, which is why they are still exported from here.
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
export const ICON_ARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" /></>);
export const ICON_TRASH = icon(
  <><path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <line x1="10" y1="11" x2="10" y2="17" />
    <line x1="14" y1="11" x2="14" y2="17" /></>);
export const ICON_NOTES = icon(<><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="14" y2="17" /></>);
export const ICON_RESTORE = icon(<><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></>);
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

const clockTime = (d: Date) =>
  d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

// Escape closes the detail panel. Stops the shell (or an ancestor modal) also
// acting on the same key.
function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
}

// ---- The chip's detail panel ------------------------------------------------------
// The mitigation for the one known cost of the one-chip-per-day rule: the
// task's thread, THAT DAY's messages first with their real times, then the rest
// newest-first. The 7pm run that has no chip of its own is right here.

function ChipPanel({
  chip,
  repeat,
  roles,
  onClose,
  onReload,
  onEditEntry,
}: {
  chip: CalendarChip;
  /** The recurrence in words ("Daily"), "" for a one-off. */
  repeat: string;
  /** What the scheduler's queue says about each entry id. */
  roles: Map<string, QueueRole>;
  onClose: () => void;
  onReload: () => void;
  onEditEntry?: (entryId: string) => void;
}) {
  const [thread, setThread] = useState<TaskMessage[] | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  useEscape(onClose);

  const task = chip.task;

  // The full thread is a separate call, fetched only when a chip is opened.
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

  // Is the task working RIGHT NOW (task-level), and is THIS DAY (the chip's
  // own list) — a rule's Tuesday must not say In Progress for Thursday's run.
  const liveNow = isRunningIn(task, messages);
  const liveToday = isRunningIn(task, chip.messages);

  // Clicking a message opens the chat ON THAT TURN, and marks it read.
  const canOpen = (m: TaskMessage) => openMessageHref(task, m) !== null;

  const openMessage = (m: TaskMessage) => {
    const to = openMessageHref(task, m);
    if (!to) return;
    if (m.unread) markTaskMessageRead(task.key, m.message_id).then(onReload, () => {});
    navigateUrl(to);
    onClose();
  };

  // The footer opens the thread itself; before the task HAS a session (a run
  // parked on a permission prompt) folderHref opens the run's folder instead.
  const threadHref = taskHref(task) ?? folderHref(task);

  // Run now / Re-run — tasks-lib's decision, asked of `task`. A PROJECTED row
  // must never be fired (its entry id is the rule's).
  const run = useMemo(() => {
    const intent = taskRunIntent(task);
    if (!intent || isProjectedId(intent.messageId)) return null;
    return intent;
  }, [task]);

  // The panel describes a chip the run CHANGES, so it closes on a clean
  // success and stays only when there is a sentence to read (a 409 "wait", a
  // re-send queued rather than sent).
  const runTask = async (intent: TaskRunIntent) => {
    setRunning(true);
    setError("");
    try {
      let said = "";
      if (intent.kind === "resend") {
        said = (await resendScheduledMessage(intent.entryId)).note ?? "";
      } else {
        await runScheduledNow(intent.entryId);
      }
      onReload();
      if (said) setError(said);
      else onClose();
    } catch (e) {
      setError((e as Error).message);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  // THE HEADER'S ONE LIFECYCLE VERB: done/failed → Archive (reversible, no
  // confirm); upcoming/archived → Delete (two-press confirm: the first press
  // arms, the second fires); running → nothing.
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
      onReload();
      onClose();
    } catch (e) {
      setError((e as Error).message);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  const doDelete = async () => {
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setRunning(true);
    setError("");
    try {
      await deleteTask(task.key);
      onReload();
      onClose();
    } catch (e) {
      // A 409 lands here too; the button DISARMS so the next press starts over.
      setError((e as Error).message);
      setConfirmDelete(false);
      onReload();
    } finally {
      setRunning(false);
    }
  };

  const dayLabel = chip.time.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  // THE HEADER'S PILL — tasks-lib.popoverPill's decision, in the app's five
  // words: a one-off's is its task's status; a rule's answers for the clicked
  // DAY. Always solid, never dashed.
  const recurring = Boolean(chip.recurring || repeat);
  const pillLive = recurring ? liveToday : liveNow;

  const nowSec = Math.floor(Date.now() / 1000);
  const pill = useMemo(
    () => popoverPill(task, recurring, pillLive, today, chip.time, new Date(nowSec * 1000)),
    [task, recurring, pillLive, today, chip.time, nowSec],
  );

  const row = (m: TaskMessage, sameDayRow: boolean) => {
    const status = runStatus(m, taskMessageTone(m));
    const role = queueRole(m, roles, nowSec);
    const t = new Date(m.at * 1000);
    // Not for a control any more (the per-row cancel is gone) but for the
    // NOTE: "queued", "ran 2 days late", "too late to cancel".
    const kind = msgCancelKind(m, role);
    const note = msgNote(m, kind);
    const open = canOpen(m);
    return (
      <li key={m.message_id}>
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-xs hover:bg-accent/50 focus-visible:outline-none focus-visible:bg-accent/50",
            !open && "cursor-default text-muted-foreground hover:bg-transparent",
          )}
          onClick={() => openMessage(m)}
          title={[t.toLocaleString(), status.label, note].filter(Boolean).join(" — ")}
        >
          {/* The ring at row scale; dashed when only projected. It carries
              this row's UNREAD (filled centre) too. */}
          <StatusIcon
            status={status.column}
            failed={status.failed}
            unread={m.unread}
            className={cn("size-3", status.projected && "border-dashed")}
          />
          <span className={cn("tabular-nums", m.unread && "font-semibold")}>
            {sameDayRow
              ? clockTime(t)
              : t.toLocaleDateString(undefined, { month: "short", day: "numeric" })}
          </span>
          {note && <Tiny className="truncate">{note}</Tiny>}
        </button>
      </li>
    );
  };

  return (
    <PropertiesPanel
      className="flex flex-col gap-3 py-3 min-h-0"
      role="region"
      aria-label="Task details"
    >
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="size-2.5 shrink-0 rounded-full bg-(--chip)"
          style={{ ["--chip" as string]: folderChartVar(chip.colour) } as React.CSSProperties}
          aria-hidden="true"
        />
        <IdChip id={task.task_id} />
        <StatusBadge bucket={columnBucket(pill.column)} className="gap-1.5">
          {pillLive && <StatusDot bucket="yellow" pulse />}
          {pill.label}
        </StatusBadge>
        <span className="flex-1" />
        {onEditEntry && chip.anchor.entry_id && (
          <Button
            variant="ghost"
            size="icon-xs"
            title="Edit"
            aria-label="Edit"
            onClick={() => {
              onEditEntry(chip.anchor.template_id || chip.anchor.entry_id);
              onClose();
            }}
          >
            <Pencil className="size-3.5" />
          </Button>
        )}
        {canArchive && (
          <Button variant="ghost" size="icon-xs" title="Archive" aria-label="Archive" disabled={running} onClick={() => void archive()}>
            <Archive className="size-3.5" />
          </Button>
        )}
        {canDelete && (
          // The VISIBLE state and the accessible name change together.
          <Button
            variant={confirmDelete ? "destructive" : "ghost"}
            size="icon-xs"
            title={confirmDelete ? "Click again to delete" : "Delete"}
            aria-label={confirmDelete ? "Click again to delete" : "Delete"}
            disabled={running}
            onClick={() => void doDelete()}
          >
            <Trash2 className="size-3.5" />
          </Button>
        )}
        <Button variant="ghost" size="icon-xs" title="Close" aria-label="Close details" onClick={onClose}>
          <X className="size-3.5" />
        </Button>
      </div>

      {/* Title then description, as one writing block — what the user WROTE. */}
      <div className="space-y-1">
        <p className="text-sm font-medium [overflow-wrap:anywhere]">{task.title || firstLine(chip.anchor.body)}</p>
        {task.description && <Muted className="text-xs whitespace-pre-wrap">{task.description}</Muted>}
      </div>

      <PropertyList>
        <PropertyRow label="Day">{dayLabel}</PropertyRow>
        {repeat && <PropertyRow label="Repeats">{repeat}</PropertyRow>}
        {task.target && (
          <PropertyRow label="Folder">
            <code className="font-mono text-xs" title={task.target}>{task.target}</code>
          </PropertyRow>
        )}
      </PropertyList>

      {/* ONE CONTINUOUS LIST: the day's runs print clock times, everything
          after prints dates — the seam is visible without a header. */}
      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-auto-hide -mx-1.5">
        <SectionHeading className="px-1.5 pb-1 text-xs">Runs</SectionHeading>
        <ul className="space-y-0.5">
          {today.map((m) => row(m, true))}
          {rest.map((m) => row(m, false))}
        </ul>
      </div>

      {error && <p className="text-xs text-destructive" role="alert">{error}</p>}

      <div className="flex flex-wrap items-center gap-2">
        {threadHref && (
          <Button variant="outline" size="sm" onClick={() => navigateUrl(threadHref)}>
            <Inbox className="size-3.5" aria-hidden />
            Open in Explorer
          </Button>
        )}
        {/* LAST, and not the default press: this one starts work. */}
        {run && (
          <Button variant="outline" size="sm" disabled={running} title={run.title} onClick={() => void runTask(run)}>
            {run.rerun ? <RotateCcw className="size-3.5" aria-hidden /> : <Play className="size-3.5" aria-hidden />}
            {run.label}
          </Button>
        )}
      </div>
    </PropertiesPanel>
  );
}

// ---- The grid ------------------------------------------------------------------

/** The day-tone modifiers a chip wears over its folder colour. Status stays a
 *  modifier (a ring, a fade, a strike), never the chip's hue. */
function toneClasses(tone: string): string {
  switch (tone) {
    case "error":
    case "missed":
      return "ring-1 ring-destructive/70";
    case "skipped":
      return "line-through opacity-60";
    case "sending":
      return "ring-1 ring-ring";
    default:
      return "";
  }
}

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
  /** The raw schedule entries, for a recurring rule's projected occurrences. */
  entries?: ScheduledMessage[];
  /** getScheduleQueue().queued — marks the thread rows those entries ARE. */
  queued?: ScheduledMessage[];
  /** getScheduleQueue().running — same treatment. */
  running?: ScheduledMessage[];
  onReload: () => void;
  onCreateAt: (time: Date) => void;
  /** Opens the New task modal on an existing entry (a rule, or a one-off). */
  onEditEntry?: (entryId: string) => void;
}) {
  const [range, setRange] = useState<CalendarRange>(() => {
    // localStorage can THROW and this runs during first render.
    try {
      return initialRange(localStorage.getItem(RANGE_KEY));
    } catch {
      return DEFAULT_RANGE;
    }
  });
  const [start, setStart] = useState(() => rangeStart(new Date(), range));
  const [openChip, setOpenChip] = useState<CalendarChip | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const roles = useMemo(() => queueRoles(queued, running), [queued, running]);

  const pickRange = (next: CalendarRange) => {
    setRange(next);
    // Re-anchor rather than jump: the week snaps to its Monday, the 4-day
    // range starts on the day you were already looking at.
    setStart((s) => rangeStart(s, next));
    setOpenChip(null);
    try {
      localStorage.setItem(RANGE_KEY, next);
    } catch {
      // A blocked store forgets the choice; the switch itself still happens.
    }
  };

  const days = useMemo(() => rangeDays(start, range), [start, range]);

  // Every scheduled message in the visible days, from the windowed endpoint.
  // `null` means "we do not have it".
  const [windowed, setWindowed] = useState<Record<string, TaskMessage[]> | null>(null);
  const { from, to } = useMemo(() => windowBounds(days), [days]);

  // Re-fetched whenever the window moves, and on each `tasks` poll. A failure
  // keeps what we have; the grid falls back to each task's own three messages.
  useEffect(() => {
    if (!from && !to) return;
    let live = true;
    getTasksScheduled(from, to).then(
      (r) => live && setWindowed(groupScheduled(r.items ?? [])),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [from, to, tasks]);

  const threads = useMemo(
    () => calendarThreads(tasks, entries, windowed, new Date(), days),
    [tasks, entries, windowed, days],
  );

  const chipsByDay = useMemo(
    () => taskChips(tasks, days, threads),
    [tasks, days, threads],
  );

  // WHERE THE GRID OPENS (schedule-lib.scrollTarget) — placed per RANGE, not
  // per poll, with exactly one upgrade allowed when chips first arrive.
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

  const closePanel = useCallback(() => setOpenChip(null), []);

  const clickGrid = (day: Date, e: React.MouseEvent<HTMLDivElement>) => {
    // Only the column itself — a chip's click stopPropagation()s before here.
    const rect = e.currentTarget.getBoundingClientRect();
    const minutes = ((e.clientY - rect.top) / HOUR_H) * 60;
    // Clamped to the day's last slot: 24:00 would roll into TOMORROW.
    const snapped = Math.min(
      Math.round(minutes / SNAP_MIN) * SNAP_MIN,
      24 * 60 - SNAP_MIN,
    );
    const t = new Date(day);
    t.setMinutes(snapped, 0, 0);
    onCreateAt(t);
  };

  const columns = { gridTemplateColumns: `3rem repeat(${days.length}, minmax(0, 1fr))` } as React.CSSProperties;
  const wide = range === "4day";

  return (
    <div className="schedule-cal flex flex-1 min-h-0 rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex flex-1 min-w-0 min-h-0 flex-col">
        {/* `‹ Today ›`  ……  `[ 4 days | Week ]  August 2026` — stepping at the
            left edge, the range pair immediately left of the month. */}
        <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border">
          <div className="flex items-center gap-1">
            <Button variant="outline" size="icon-sm" onClick={() => shift(-1)}
                    aria-label={range === "week" ? "Previous week" : "Previous 4 days"}>
              <ChevronLeft className="size-3.5" />
            </Button>
            <Button variant="outline" size="sm" onClick={goToday}>Today</Button>
            <Button variant="outline" size="icon-sm" onClick={() => shift(1)}
                    aria-label={range === "week" ? "Next week" : "Next 4 days"}>
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
          <div className="ml-auto flex items-center gap-3">
            <ToggleGroup
              value={[range]}
              onValueChange={(v) => {
                const next = v[0];
                if (next === "4day" || next === "week") pickRange(next);
              }}
              variant="outline"
              size="sm"
              spacing={0}
              aria-label="Range"
            >
              <ToggleGroupItem value="4day">4 days</ToggleGroupItem>
              <ToggleGroupItem value="week">Week</ToggleGroupItem>
            </ToggleGroup>
            <span className="text-sm font-medium tabular-nums">{label}</span>
          </div>
        </div>

        <div className="grid border-b border-border" style={columns}>
          <div aria-hidden="true" />
          {days.map((day) => {
            const today = sameDay(day, now);
            return (
              <div key={dayKey(day)} className="flex flex-col items-center gap-0.5 py-2 border-l border-border">
                <Tiny className={cn("uppercase tracking-wide", today && "text-foreground")}>
                  {day.toLocaleDateString(undefined, { weekday: "short" })}
                </Tiny>
                <span
                  className={cn(
                    "flex size-7 items-center justify-center rounded-full text-sm font-semibold tabular-nums",
                    today && "bg-primary text-primary-foreground",
                  )}
                >
                  {day.getDate()}
                </span>
              </div>
            );
          })}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-auto-hide" ref={scrollRef}>
          <div className="grid relative" style={{ ...columns, height: 24 * HOUR_H }}>
            <div className="relative">
              {Array.from({ length: 23 }, (_, i) => (
                <Tiny
                  key={i + 1}
                  className="absolute right-2 -translate-y-1/2 tabular-nums"
                  style={{ top: (i + 1) * HOUR_H }}
                >
                  {new Date(2000, 0, 1, i + 1).toLocaleTimeString(undefined, { hour: "numeric" })}
                </Tiny>
              ))}
            </div>
            {days.map((day) => {
              const key = dayKey(day);
              const today = sameDay(day, now);
              // Lane packing: the side-by-side split for chips that overlap.
              const chips = assignLanes(chipsByDay.get(key) ?? []);
              return (
                <div
                  key={key}
                  className={cn("relative border-l border-border cursor-cell", today && "bg-accent/10")}
                  onClick={(e) => clickGrid(day, e)}
                >
                  {Array.from({ length: 23 }, (_, i) => (
                    <span
                      key={i + 1}
                      className="pointer-events-none absolute inset-x-0 h-px bg-border/70"
                      style={{ top: (i + 1) * HOUR_H }}
                      aria-hidden
                    />
                  ))}
                  {today && (
                    <div
                      className="pointer-events-none absolute inset-x-0 z-20 h-px bg-primary before:absolute before:-left-1 before:-top-[3px] before:size-[7px] before:rounded-full before:bg-primary"
                      style={{ top: (now.getHours() + now.getMinutes() / 60) * HOUR_H }}
                    />
                  )}
                  {chips.map((chip) => {
                    const later = chip.messages
                      .slice(1)
                      .map((m) => clockTime(new Date(m.at * 1000)));
                    // One string for the tooltip AND the accessible name, so
                    // the name survives the narrow-lane rule that hides the time.
                    const name = chipAccessibleName(
                      chip.task.title || firstLine(chip.anchor.body),
                      repeatByTemplate.get(chip.templateId) ?? "",
                      clockTime(chip.time),
                      later,
                    );
                    const live = isRunningIn(chip.task, chip.messages);
                    const narrow = chip.lanes >= 3;
                    return (
                      <button
                        key={chip.key}
                        type="button"
                        className={cn(
                          // `cursor-pointer` is explicit: the column under the
                          // chip is `cursor-cell` (a press there creates a
                          // task), and Tailwind v4's preflight does not give a
                          // button a pointer of its own.
                          "schedule-cal-chip absolute z-(--lane) flex cursor-pointer items-center gap-1 overflow-hidden rounded-sm border-l-2 border-(--chip) bg-(--chip)/15 px-1.5 text-left text-foreground hover:z-20 hover:bg-(--chip)/30 focus-visible:z-20 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
                          wide ? "text-xs" : "text-[11px]",
                          chip.projected && "border-dashed bg-transparent",
                          chip.time < now && !live && "opacity-70",
                          toneClasses(chip.tone),
                          openChip?.key === chip.key && "ring-2 ring-ring",
                        )}
                        style={{
                          top: (minutesOfDay(chip.time) / 60) * HOUR_H,
                          height: CHIP_H,
                          left: `calc(${(chip.lane * 100) / chip.lanes}% + 1px)`,
                          width: `calc(${100 / chip.lanes}% - 3px)`,
                          ["--lane" as string]: chip.lane + 1,
                          // Same FOLDER, same colour, right across the grid.
                          ["--chip" as string]: folderChartVar(chip.colour),
                        } as React.CSSProperties}
                        title={name}
                        aria-label={name}
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenChip(chip);
                        }}
                      >
                        {live && <StatusDot bucket="yellow" pulse />}
                        <span className={cn("min-w-0 flex-1 truncate", live && "shimmer-text")}>
                          {chip.task.title || firstLine(chip.anchor.body)}
                        </span>
                        {chip.recurring && <span aria-hidden="true">↻</span>}
                        {chip.extra > 0 && (
                          <span className="shrink-0 tabular-nums text-muted-foreground" title={`also ${later.join(", ")}`}>
                            +{chip.extra}
                          </span>
                        )}
                        {!narrow && (
                          <span className="shrink-0 tabular-nums text-muted-foreground">{clockTime(chip.time)}</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {openChip && (
        <ChipPanel
          key={openChip.key}
          chip={openChip}
          repeat={repeatByTemplate.get(openChip.templateId) ?? ""}
          roles={roles}
          onClose={closePanel}
          onReload={onReload}
          onEditEntry={onEditEntry}
        />
      )}
    </div>
  );
}
