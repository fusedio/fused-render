// The Tasks page's two task views — the List (an accordion per task) and the
// Board (columns of task cards).
//
// What changed, and it is the whole point: the page no longer MERGES two feeds.
// It used to hold the scheduler's entries in one hand and Claude Code's session
// summaries in the other and reconcile them here, which meant the client owned
// a model — what counts as one unit of work, what its title is, which column it
// belongs in — that the server had a better claim to. `/api/tasks` now returns
// that model, already merged, already titled, already counted and already
// sorted newest-first. These components render it and decide nothing.
//
//   TASK-002   one Claude session, one thread
//   ├─ MSG-003  newest first
//   ├─ MSG-002
//   └─ MSG-001
//
// See docs/superpowers/specs/2026-08-16-tasks-threads-messages-design.md, §1
// (the model), §3 (ids), §7 (unread), §8 (these two views).
//
// The visual vocabulary is the one the previous views established and the
// calendar still speaks — the bordered status ring, the live ping, the folder
// chip, the 260px lane, the 52px collapsed rail — so the three views read as
// one page. Those live in styles/schedule.css; everything this file adds (the
// accordion, the thread rows, the id chips, the unread dot) is in
// styles/tasks.css.
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent as ReactDragEvent } from "react";
import {
  cancelScheduledMessage,
  getTaskMessages,
  markTaskMessageRead,
  markWholeTaskRead,
  resendScheduledMessage,
  runScheduledNow,
  setSessionTriage,
} from "@platform/lib/api";
import type { Task, TaskMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { BOARD_COLUMNS } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";
import {
  EMPTY_FILTERS,
  archiveIntent,
  basename,
  cancelIntent,
  dropAction,
  dropLanes,
  filterTasks,
  firstLine,
  groupByColumn,
  isDraggable,
  markAllRead,
  markRead,
  markReadIntent,
  messageHref,
  messageTime,
  messageTone,
  messageWhenTitle,
  projectOptions,
  ranNote,
  taskColumn,
  taskHref,
  taskRunIntent,
  taskUnread,
  threadView,
  tildePath,
  toggleExpanded,
  unreadCount,
  unreadMarker,
} from "./tasks-lib";
import type { ArchiveStatus, TaskFilters, TaskRunIntent } from "./tasks-lib";

// The page composes these from one import; re-exported here so Scheduled.tsx
// takes its filter type, its empty value and its filter function from the same
// module it takes the views from.
export { EMPTY_FILTERS, filterTasks, projectOptions, tildePath, basename };
export type { TaskFilters };

// ---- icons -------------------------------------------------------------------
// The page's own recipe (ScheduleCalendar's `icon`): a 24-viewBox lucide
// geometry at stroke 2 with round caps, inlined rather than pulled from a
// package this app does not depend on.
const icon = (paths: React.ReactNode, size = 14) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {paths}
  </svg>
);

const ICON_SEARCH = icon(<><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></>, 13);
const ICON_CHEVRON = icon(<polyline points="9 18 15 12 9 6" />, 13);
const ICON_CHEVRON_DOWN = icon(<polyline points="6 9 12 15 18 9" />, 12);
const ICON_CHECK = icon(<polyline points="20 6 9 17 4 12" />, 13);
const ICON_CIRCLE_DOT = icon(
  <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="1.5" /></>, 13);
const ICON_FOLDER = icon(
  <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />,
  12,
);
// A message's SOURCE, the one thing that differs message to message inside a
// thread (§1): the scheduler put it there, or a person typed it.
const ICON_CLOCK = icon(<><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" /></>, 12);
const ICON_CHAT = icon(
  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />, 12);
const ICON_OPEN = icon(
  <><path d="M15 3h6v6" /><path d="M10 14 21 3" />
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /></>, 13);
const ICON_PENCIL = icon(
  <><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>, 13);
// The two halves of Cancel, drawn apart because they MEAN different things
// (tasks-lib.cancelIntent): a one-off is stopped for good (lucide `ban`), an
// occurrence of a repeat is stepped over and the rule runs on (`skip-forward`).
const ICON_BAN = icon(
  <><circle cx="12" cy="12" r="9" /><path d="m5.6 5.6 12.8 12.8" /></>, 13);
const ICON_SKIP = icon(
  <><polygon points="5 4 15 12 5 20 5 4" /><line x1="19" x2="19" y1="5" y2="19" /></>, 12);
// The two halves of run-now, drawn apart for the same reason Cancel's are: one
// call, but "start this early" and "start this again" are not the same sentence
// to the person clicking. lucide `play` and `rotate-ccw`.
const ICON_PLAY = icon(<polygon points="6 3 20 12 6 21 6 3" />, 12);
const ICON_RERUN = icon(
  <><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
    <path d="M3 3v5h5" /></>, 13);
// Mark the whole task read. lucide `check-check` — the double tick every
// messaging app on the machine already uses for "seen", which is exactly the
// fact this button asserts. Deliberately NOT the single `ICON_CHECK` above: that
// one means "this filter is on" in the popovers, and a row action wearing the
// same glyph would read as a toggle that is currently checked.
const ICON_MARK_READ = icon(
  <><path d="M18 6 7 17l-5-5" /><path d="m22 10-7.5 7.5L13 16" /></>, 13);
// Filing away, and the way back. Drawn apart because the second is not the first
// greyed out: a person looking at an archived row has to be able to SEE that the
// door opens both ways, and one glyph with two meanings cannot say that
// (tasks-lib.archiveIntent).
//
// The first is lucide `archive`. The second is lucide `archive-restore`'s ARROW
// on the same closed box, rather than that icon whole: `archive-restore` splits
// the box's two walls apart, and at 13px the gap reads as a broken glyph instead
// of an open one (checked at 6x). Keeping the body identical and changing only
// the mark inside it — a dash, or an arrow coming out — is what makes the pair
// legible at the size it is actually drawn.
const ICON_ARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" /></>, 13);
const ICON_UNARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="m9 15 3-3 3 3" /><path d="M12 12v6" /></>, 13);

// ---- leaf components ---------------------------------------------------------

const STATUS_LABELS: Record<BoardColumn, string> = Object.fromEntries(
  BOARD_COLUMNS.map((c) => [c.key, c.label]),
) as Record<BoardColumn, string>;

/** The bordered ring, with the inner dot on the terminal state. `failed`
 * repaints it red without moving the row out of its column — a failed or missed
 * run IS settled, but folding away the only failure signal would let a dead
 * turn read as a clean one. */
export function StatusIcon({
  status,
  failed,
  label,
}: {
  status: BoardColumn;
  failed?: boolean;
  label?: string;
}) {
  const text = label ?? (failed ? "Failed" : (STATUS_LABELS[status] ?? status));
  return (
    <span
      className={
        `schedule-ring schedule-ring--${status}` + (failed ? " schedule-ring--failed" : "")
      }
      aria-label={text}
      title={text}
    />
  );
}

/** The blue ping on a task whose turn is in flight. */
export function LivePulse() {
  return <span className="schedule-tv-pulse" aria-label="Running" title="Running" />;
}

/** The folder a task's work happens in — a plain folder glyph and the folder's
 * own name, with the whole path (with ~ for home) as the tooltip. Deliberately
 * not an initials avatar: that stands for a PERSON, and a directory is not one. */
export function IdentityChip({ name, title }: { name: string; title?: string }) {
  if (!name) return null;
  return (
    <span className="schedule-tv-id" title={title || name}>
      <span className="schedule-tv-folder-icon" aria-hidden>{ICON_FOLDER}</span>
      <span className="schedule-tv-id-name">{name}</span>
    </span>
  );
}

/** TASK-002 / MSG-003. These are DESIGNED identifiers (§3) — allocated once,
 * never renumbered — so unlike a session uuid they are meant to be read, said
 * out loud and searched for. Monospaced, because a column of them is scanned. */
function IdChip({ id, kind }: { id: string; kind: "task" | "message" }) {
  return <span className={`tasks-id tasks-id--${kind}`}>{id}</span>;
}

/** A task's unread count as the DOT CARRYING THE NUMBER (§7,
 * tasks-lib.unreadCount): the message dot's own hue, filled, grown from a
 * circle into a short pill only as far as the digits need. It reads as the same
 * mark the thread below it uses, one size up, rather than as a second badge
 * with its own vocabulary. The true count is always the accessible name, even
 * when the pill prints "99+". */
function UnreadPill({ count }: { count: number }) {
  const c = unreadCount(count);
  if (!c) return null;
  return (
    <span className="tasks-count" role="img" aria-label={c.label} title={c.label}>
      {c.text}
    </span>
  );
}

/** The LEADING rail slot on a task row — the same column, the same width and
 * the same centre line as the `.tasks-rail` at the head of every message row
 * under it, so one straight rail runs down the whole node. Drawn on every task,
 * filled or not: a slot that appeared only on unread tasks would shift the rest
 * of those rows sideways, which is the ragged edge this change exists to
 * remove. */
function UnreadRail({ count }: { count: number }) {
  if (count <= 0) return <span className="tasks-rail" aria-hidden />;
  return (
    <span className="tasks-rail">
      <UnreadPill count={count} />
    </span>
  );
}

// ---- toolbar: search + status + project --------------------------------------

// ---- the filter popover's geometry -------------------------------------------
// The menus are `position: fixed`, measured off their trigger, exactly as every
// dropdown in NewJobModal is and for the same reason: an absolutely-positioned
// panel is clipped by the nearest scrolling/hidden ancestor, and this one has
// two of them — `.prefs-page { overflow-y: auto }` and `.schedule-page {
// overflow: hidden }`. The Project menu shipped cut off at the bottom with a
// handful of its 28 folders showing (Akshil, screenshot). Fixed escapes the
// clip; when the viewport below the trigger is shorter than the panel, it opens
// upward instead.
//
// This is safe here because nothing in the chain is TRANSFORMED — a transformed
// ancestor becomes the containing block for its fixed descendants and would
// re-anchor the panel to it, clip and all (which is why schedule.css slides its
// cards with `left` rather than `transform`). Checked: `.prefs-page`,
// `.schedule-page`, `.schedule-main` and `.schedule-toolbar` set none.

/** The panel's width, and schedule.css's own `.schedule-tv-pop { width }` — kept
 * in step here only so a menu near the right edge can be pushed back inside the
 * window. */
const POP_WIDTH = 190;
/** The tallest a menu grows before it scrolls inside itself. 28 projects is a
 * real number on this machine and a 28-item column is not a menu, it is a page. */
const POP_MAX_HEIGHT = 320;
/** Below the trigger, and off the window's own edges. */
const POP_GAP = 6;
const POP_EDGE = 8;

/**
 * Where the panel goes and how tall it may get. Height is never SET — the menu
 * sizes to its content, so two statuses draw a two-row menu — only capped, and
 * the cap is the smaller of POP_MAX_HEIGHT and the room actually there.
 */
function popStyle(el: HTMLElement | null): React.CSSProperties {
  const r = el?.getBoundingClientRect();
  if (!r) return { position: "fixed" };
  const below = window.innerHeight - r.bottom - POP_GAP - POP_EDGE;
  const above = r.top - POP_GAP - POP_EDGE;
  // Flip only when up is genuinely roomier: a menu that jumps above its trigger
  // to gain twenty pixels is a menu that moved for nothing.
  const up = above > below && below < POP_MAX_HEIGHT;
  const room = Math.max(120, Math.min(POP_MAX_HEIGHT, up ? above : below));
  const left = Math.max(
    POP_EDGE,
    Math.min(r.left, window.innerWidth - POP_WIDTH - POP_EDGE),
  );
  const s: React.CSSProperties = {
    position: "fixed",
    left,
    right: "auto",
    maxHeight: room,
  };
  if (up) s.bottom = window.innerHeight - r.top + POP_GAP;
  else s.top = r.bottom + POP_GAP;
  return s;
}

/** A dismissable popover trigger, shared by the two filter menus: click away or
 * Escape closes it. Dismissal is the whole contract — the panel is fixed and
 * therefore outside every clip on the page, so one left open hangs over the
 * board. */
function FilterMenu({
  label,
  count,
  children,
}: {
  label: string;
  count: number;
  children: (close: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement | null>(null);
  const btn = useRef<HTMLButtonElement | null>(null);
  const [style, setStyle] = useState<React.CSSProperties>({ position: "fixed" });

  useEffect(() => {
    if (!open) return;
    const place = () => setStyle(popStyle(btn.current));
    place();
    const away = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    // A fixed panel does not travel with its trigger, so anything that MOVES the
    // trigger has to re-measure it. `capture` because the movement that happens
    // on this page is a scroll inside the list or the board, and a scroll does
    // not bubble.
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  return (
    <div className="schedule-tv-pop-wrap" ref={wrap}>
      <button
        type="button"
        ref={btn}
        className="schedule-tv-filter-btn"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {ICON_CIRCLE_DOT} {label}
        {count > 0 && <span className="schedule-tv-filter-count">{count}</span>}
      </button>
      {open && (
        <div
          className="schedule-tv-pop tasks-pop"
          role="group"
          aria-label={`Filter by ${label}`}
          style={style}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

export function TaskFilterControls({
  filters,
  projects,
  home = "",
  onChange,
}: {
  filters: TaskFilters;
  /** Every folder that has a task — `projectOptions(tasks)`. */
  projects: string[];
  home?: string;
  onChange: (next: TaskFilters) => void;
}) {
  const toggleStatus = (key: BoardColumn) =>
    onChange({
      ...filters,
      statuses: filters.statuses.includes(key)
        ? filters.statuses.filter((s) => s !== key)
        : [...filters.statuses, key],
    });

  const toggleProject = (path: string) =>
    onChange({
      ...filters,
      projects: filters.projects.includes(path)
        ? filters.projects.filter((p) => p !== path)
        : [...filters.projects, path],
    });

  return (
    <div className="schedule-tv-filters">
      <div className="schedule-tv-search">
        <span className="schedule-tv-search-icon" aria-hidden>{ICON_SEARCH}</span>
        <input
          type="search"
          className="field-control schedule-tv-search-input"
          value={filters.search}
          placeholder="Search tasks…"
          aria-label="Search tasks"
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Escape") e.currentTarget.blur();
          }}
        />
      </div>

      <FilterMenu label="Status" count={filters.statuses.length}>
        {() =>
          BOARD_COLUMNS.map((col) => {
            const on = filters.statuses.includes(col.key);
            return (
              <button
                type="button"
                key={col.key}
                className="schedule-tv-pop-item"
                aria-pressed={on}
                onClick={() => toggleStatus(col.key)}
              >
                <span className="schedule-tv-pop-check" aria-hidden>
                  {on ? ICON_CHECK : null}
                </span>
                <StatusIcon status={col.key} />
                <span>{col.label}</span>
              </button>
            );
          })
        }
      </FilterMenu>

      {/* Project is auto-detected from the tasks themselves (§10), so the menu
          is simply absent on a machine whose tasks all live in one folder —
          a control with one choice is not a choice. */}
      {projects.length > 1 && (
        <FilterMenu label="Project" count={filters.projects.length}>
          {() =>
            projects.map((path) => {
              const on = filters.projects.includes(path);
              return (
                <button
                  type="button"
                  key={path}
                  className="schedule-tv-pop-item"
                  aria-pressed={on}
                  title={tildePath(path, home)}
                  onClick={() => toggleProject(path)}
                >
                  <span className="schedule-tv-pop-check" aria-hidden>
                    {on ? ICON_CHECK : null}
                  </span>
                  <span className="schedule-tv-folder-icon" aria-hidden>{ICON_FOLDER}</span>
                  <span className="tasks-pop-label">{basename(path)}</span>
                </button>
              );
            })
          }
        </FilterMenu>
      )}
    </div>
  );
}

// ---- read bookkeeping --------------------------------------------------------
// Clicking a message marks it read, and the dot has to go NOW: the page polls
// on a 20-second interval, and a dot that outlives its own click reads as a
// click that failed. So the write goes to the server AND into a local set that
// is merged over whatever the next poll returns, until the server's own answer
// catches up. The set is never pruned — an id that is already read costs one
// string, and pruning it against a list that changes under us is how a dot
// comes back.

function useReadSet() {
  const [read, setRead] = useState<Set<string>>(() => new Set());
  const clear = (taskKey: string, m: TaskMessage) => {
    if (!m.unread) return;
    setRead((cur) => markRead(cur, taskKey, m.message_id));
    // Fire and forget: the click navigates away, so there is no one left to
    // show an error to, and the dot is already gone locally either way. A
    // failed write reappears on the next poll, which is the honest outcome.
    void markTaskMessageRead(taskKey, m.message_id).catch(() => {});
  };
  // The whole task, on the row's own button. The local half is one sentinel
  // rather than an id per message (tasks-lib.markAllRead): the row's count and
  // the dots of a thread that may not even be expanded all have to go at once,
  // and the ids outside the loaded window are ones this component has never
  // seen. The SERVER half is awaited by the caller — unlike a message click,
  // nobody is navigating away, so a refusal has somewhere to be said.
  const clearAll = (taskKey: string) => {
    setRead((cur) => markAllRead(cur, taskKey));
  };
  return { read, clear, clearAll };
}

// ---- the run, for both views -------------------------------------------------

/**
 * Spend a TaskRunIntent: the ONE place either view turns `kind` into a call.
 *
 * The List had this inline and the Board had no run action at all (Akshil,
 * 2026-08-17: "I have a rerun option in list, I have a rerun option in calendar,
 * but I don't have a rerun option in Kanban"). Adding one meant either copying
 * the two-call switch into the card's owner or lifting it here, and copying it is
 * how the two views start disagreeing about what "Re-run" does.
 *
 * WHICH message and WHICH call are still not decided here — that is
 * tasks-lib.taskRunIntent, the same function the drag's dropAction asks. This
 * only performs it, and returns the sentence the caller should show: the server's
 * own note when a re-send was queued rather than sent, "" when there is nothing
 * to say. Refusals THROW, so each caller can put them in its own note line.
 */
async function performRun(intent: TaskRunIntent): Promise<string> {
  if (intent.kind === "resend") {
    const res = await resendScheduledMessage(intent.entryId);
    return res.note ?? "";
  }
  await runScheduledNow(intent.entryId);
  return "";
}

// ---- List view: one accordion per task ---------------------------------------

export function TaskList({
  tasks,
  home = "",
  onEditEntry,
  onReload,
  emptyLabel = "Nothing to show here.",
}: {
  /** Already filtered, in the SERVER's order. Never re-sorted here. */
  tasks: Task[];
  /** $HOME, only so a folder tooltip can say "~/Desktop/fused". */
  home?: string;
  /** Open the schedule form on a message that has not gone out yet. Omitted ⇒
   * no edit affordance; the thread is then read-only, which is all a thread of
   * already-sent messages could ever be anyway. */
  onEditEntry?: (entryId: string) => void;
  /** Re-read the list after a cancel lands (or fails) — the row has to correct
   * itself to whatever the server actually did, and a failed cancel is a race
   * the server won, not a no-op.
   *
   * OPTIONAL, unlike the Board's, and Cancel is drawn with or without it: the
   * page polls anyway, so omitting this costs one poll interval of a row
   * still saying "Scheduled" — not a stuck row, and nothing worth withholding
   * the affordance over. */
  onReload?: () => void;
  emptyLabel?: string;
}) {
  // Collapsed by default (§8), so the set holds what is OPEN — an empty set is
  // the resting state and needs no seeding from a list that changes on every
  // poll.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // Full threads fetched by Show more, keyed by task. They REPLACE the three
  // the listing carried rather than appending to them, so no message is ever
  // drawn twice.
  const [loaded, setLoaded] = useState<Record<string, TaskMessage[]>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { read, clear, clearAll } = useReadSet();

  const toggle = (key: string) => setExpanded((cur) => toggleExpanded(cur, key));

  const showMore = async (task: Task) => {
    setLoading((cur) => ({ ...cur, [task.key]: true }));
    setErrors((cur) => {
      const next = { ...cur };
      delete next[task.key];
      return next;
    });
    try {
      const r = await getTaskMessages(task.key);
      setLoaded((cur) => ({ ...cur, [task.key]: r.messages ?? [] }));
    } catch (e) {
      // Said under the thread it belongs to, not as a page banner: the rest of
      // the list is intact and only this one thread failed to open.
      setErrors((cur) => ({ ...cur, [task.key]: (e as Error).message }));
    } finally {
      setLoading((cur) => ({ ...cur, [task.key]: false }));
    }
  };

  if (tasks.length === 0) {
    return <p className="schedule-tv-empty">{emptyLabel}</p>;
  }

  return (
    <div className="tasks-list">
      {tasks.map((task) => (
        <TaskNode
          key={task.key}
          task={task}
          home={home}
          open={expanded.has(task.key)}
          onToggle={() => toggle(task.key)}
          loaded={loaded[task.key]}
          loading={!!loading[task.key]}
          error={errors[task.key]}
          onShowMore={() => void showMore(task)}
          onEditEntry={onEditEntry}
          onReload={onReload}
          read={read}
          onRead={clear}
          onReadAll={clearAll}
        />
      ))}
    </div>
  );
}

function TaskNode({
  task,
  home,
  open,
  onToggle,
  loaded,
  loading,
  error,
  onShowMore,
  onEditEntry,
  onReload,
  read,
  onRead,
  onReadAll,
}: {
  task: Task;
  home: string;
  open: boolean;
  onToggle: () => void;
  loaded?: TaskMessage[];
  loading: boolean;
  error?: string;
  onShowMore: () => void;
  onEditEntry?: (entryId: string) => void;
  onReload?: () => void;
  read: Set<string>;
  onRead: (taskKey: string, m: TaskMessage) => void;
  /** Clear this whole task's unread locally — the optimistic half of Mark read,
   * paired with the one server call the button makes. */
  onReadAll: (taskKey: string) => void;
}) {
  const view = threadView(task, loaded);
  const unread = taskUnread(task, read, loaded);
  const href = taskHref(task);
  const label = firstLine(task.title) || "(untitled)";
  // Run now / Re-run. tasks-lib decides all of it — whether it is offered,
  // which message it acts on, and WHICH CALL that is. The run-now half comes
  // from the same function the drag asks (runNowIntent), so the button and the
  // drop can never fire different messages; the re-send half is the case the
  // drag deliberately does not have, because a gesture cannot consent to
  // creating work that was never scheduled.
  const run = taskRunIntent(task);
  // Archive / Unarchive. The Board's drop onto the Archive lane, reachable
  // without switching view, expanding a collapsed lane and dragging — which is
  // what "archive it" used to cost, and the reason the honest answer to "can a
  // task be deleted?" (no: it is archived, D306) was barely true. tasks-lib
  // decides everything, by asking dropAction the same question the drag does.
  const file = archiveIntent(task);
  // Mark read — the whole task at once, so clearing 89 unread messages is not 89
  // clicks through 89 transcripts. Asked of the count this row is DRAWING, so
  // the button leaves on its own press rather than on the next poll.
  const seen = markReadIntent(task, read, loaded);

  // The one cancel in flight, by message id, and whatever the server said about
  // the last one that failed. Per MESSAGE rather than per thread: the sentence
  // is about one row and belongs under it.
  const [cancelling, setCancelling] = useState("");
  const [cancelErrors, setCancelErrors] = useState<Record<string, string>>({});
  // The task-level actions' own pair — run-now/re-send and archive share it.
  // Per TASK, unlike cancel's: these buttons are on the task row and their
  // refusals are about the task, not about one message inside it. ONE note
  // line, because the two are one press apart and two stacked sentences under a
  // row would leave the reader working out which press each answered.
  const [acting, setActing] = useState(false);
  const [note, setNote] = useState("");

  const runNow = async (intent: TaskRunIntent) => {
    setActing(true);
    setNote("");
    try {
      // Which call is not decided here — `kind` came out of tasks-lib, and
      // performRun above is the one place it is spent, shared with the Board's
      // card so the two views cannot mean different things by "Re-run". Re-send
      // answers 200 with a `note` when the new message is queued rather than away
      // (its conversation is mid-turn), which is news of the same quiet kind as
      // the refusal below.
      const said = await performRun(intent);
      if (said) setNote(said);
    } catch (e) {
      // The server's own sentence, verbatim. Its common refusal is a 409
      // because this conversation already has a turn open — two `claude
      // --resume` processes on one transcript is the thing that must never
      // happen — and that reads as "wait", not as "broken", which is why it is
      // said in the quiet note the board's drag already uses rather than in the
      // red line a failed cancel gets.
      setNote((e as Error).message);
    } finally {
      setActing(false);
      onReload?.();
    }
  };

  // Archive / Unarchive. One call, and the same one the board's drop makes;
  // `status` came out of tasks-lib and is only spent here.
  const triage = async (status: ArchiveStatus) => {
    setActing(true);
    setNote("");
    try {
      await setSessionTriage(task.session_id, status);
    } catch (e) {
      // The server's own sentence, in the same quiet line run-now uses. A
      // refusal here is news, not a fault: nothing was destroyed either way,
      // which is the whole point of archiving rather than deleting.
      setNote((e as Error).message);
    } finally {
      setActing(false);
      // The row has to move lane (or come back), so re-read either way.
      onReload?.();
    }
  };

  // Mark the whole task read. The local set goes FIRST and unconditionally: the
  // dots and the count are what the press is about, and the page polls on a 20s
  // interval, so waiting for the round trip would leave a row that looks like it
  // ignored the click. The server call is one request for the whole thread
  // (api.markWholeTaskRead) rather than one per message.
  //
  // No onReload: the count is already right locally, and a reload would repaint
  // every row in the list to say the one thing this row has already said. The
  // next poll agrees on its own — and if the write failed, that same poll is
  // what honestly brings the dots back, under the sentence the server gave.
  const markSeen = async () => {
    setActing(true);
    setNote("");
    onReadAll(task.key);
    try {
      await markWholeTaskRead(task.key);
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setActing(false);
    }
  };

  const openMessage = (m: TaskMessage) => {
    onRead(task.key, m);
    const to = messageHref(task, m);
    if (to) navigateUrl(to);
  };

  const cancel = async (m: TaskMessage, entryId: string) => {
    setCancelling(m.message_id);
    setCancelErrors((cur) => {
      const next = { ...cur };
      delete next[m.message_id];
      return next;
    });
    try {
      await cancelScheduledMessage(entryId);
    } catch (e) {
      // A 404 here is a real race, not a bug: the scheduler's loop may have sent
      // this message while the user was reaching for the button. Say what the
      // server said — and reload either way, so the row corrects itself to
      // whatever actually happened.
      setCancelErrors((cur) => ({ ...cur, [m.message_id]: (e as Error).message }));
    } finally {
      setCancelling("");
      onReload?.();
    }
  };

  return (
    <div className="tasks-node">
      <div
        className={"tasks-row" + (open ? " is-open" : "")}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        title={task.title}
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
      >
        <span className={"tasks-caret" + (open ? " is-open" : "")} aria-hidden>
          {ICON_CHEVRON}
        </span>
        {/* Unread LEADS this row too, on the very column the thread's own dots
            sit on (tasks-lib.unreadCount). The caret stays outside that column,
            in the gutter to its left, because it is the accordion's control and
            not part of the rail — which leaves one straight line of markers
            running from the task down through every message it holds. */}
        <UnreadRail count={unread} />
        <StatusIcon status={taskColumn(task)} failed={task.failed} />
        <IdChip id={task.task_id} kind="task" />
        <span className="tasks-title">{label}</span>
        {task.live && <LivePulse />}

        {/* Exactly ONE auto margin in this row: flex distributes free space
            equally across every auto margin, so a second one would park the
            right-hand group in the middle of the row instead of at its end. */}
        <span className="tasks-grow" />

        {/* The drag from Upcoming into In Progress, without the drag — and on
            a task that broke, the word for doing it again over whichever call
            can actually do it: run-now while a message is still pending,
            re-send once the run that failed has spent it. ONE button and one
            label either way; tasks-lib.taskRunIntent is the only thing that
            knows which. In the SAME hover-revealed group as Edit and Cancel on
            a message row, so a list at rest grows no chrome: this applies to a
            minority of tasks and every other row would carry a button that
            does nothing. */}
        {/* "so you don't have to open everything individually" — the whole
            task's unread, cleared from the row that carries the count, in the
            SAME hover-revealed group as Run now and Archive. Only on a task that
            has unread (tasks-lib.markReadIntent): every other row would carry a
            button whose press does nothing, which is what makes the rows where
            it matters hard to pick out. */}
        {seen && (
          <button
            type="button"
            className="tasks-act tasks-act--seen"
            title={seen.title}
            aria-label={seen.label}
            disabled={acting}
            onClick={(e) => {
              e.stopPropagation();
              void markSeen();
            }}
          >
            {ICON_MARK_READ}
          </button>
        )}
        {run && (
          <button
            type="button"
            className="tasks-act tasks-act--run"
            title={run.title}
            aria-label={run.label}
            disabled={acting}
            onClick={(e) => {
              e.stopPropagation();
              void runNow(run);
            }}
          >
            {run.rerun ? ICON_RERUN : ICON_PLAY}
          </button>
        )}
        {/* The Board's drag onto Archive, as a press — and on an already
            archived row, the way back, because an action with only one
            direction is a trap. Same hover-revealed group, same size and same
            silence at rest as Run now and Open chat: a task that has never run
            has no session to triage and is offered nothing at all here, so this
            must not be a permanent column of buttons half of which do nothing.
            tasks-lib.archiveIntent decides both halves. */}
        {file && (
          <button
            type="button"
            className={
              "tasks-act " + (file.restore ? "tasks-act--unarchive" : "tasks-act--archive")
            }
            title={file.title}
            aria-label={file.label}
            disabled={acting}
            onClick={(e) => {
              e.stopPropagation();
              void triage(file.status);
            }}
          >
            {file.restore ? ICON_UNARCHIVE : ICON_ARCHIVE}
          </button>
        )}
        {href && (
          <button
            type="button"
            className="tasks-act"
            title="Open chat"
            aria-label="Open chat"
            onClick={(e) => {
              e.stopPropagation();
              navigateUrl(href);
            }}
          >
            {ICON_OPEN}
          </button>
        )}
        <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />
      </div>

      {/* Why the refusal is quiet: see runNow. The class is the board's own
          drag-error line, because these are the board's own calls. */}
      {note && <p className="schedule-tv-note tasks-row-note">{note}</p>}

      {open && (
        <div className="tasks-thread">
          {view.messages.map((m) => {
            const tone = messageTone(m);
            const mark = unreadMarker(task.key, m, read);
            const isNew = mark.unread;
            const stop = cancelIntent(m);
            const busy = cancelling === m.message_id;
            const why = cancelErrors[m.message_id];
            return (
              <Fragment key={m.message_id}>
                <div
                  className={"tasks-msg" + (isNew ? " is-unread" : "")}
                  role="button"
                  tabIndex={0}
                  title={m.body}
                  onClick={() => openMessage(m)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openMessage(m);
                    }
                  }}
                >
                  {/* Unread LEADS the row (tasks-lib.unreadMarker). The slot is
                      drawn on every row, filled or not, so the dots line up in
                      one column down the left edge instead of shunting the
                      rest of each unread row sideways. The word that used to
                      follow it is gone; the dot carries the name itself. It is
                      the same `.tasks-rail` the task row above opens with —
                      one class, therefore one column. */}
                  {mark.unread ? (
                    <span
                      className="tasks-rail"
                      role="img"
                      aria-label={mark.label}
                      title={mark.label}
                    >
                      <span className="tasks-dot" aria-hidden />
                    </span>
                  ) : (
                    <span className="tasks-rail" aria-hidden />
                  )}
                  <StatusIcon status={tone.column} failed={tone.failed} label={tone.label} />
                  <span
                    className="tasks-msg-kind"
                    aria-hidden
                    title={m.kind === "scheduled" ? "Scheduled" : "Chat"}
                  >
                    {m.kind === "scheduled" ? ICON_CLOCK : ICON_CHAT}
                  </span>
                  <IdChip id={m.message_id} kind="message" />
                  <span className="tasks-msg-body">{firstLine(m.body) || "(empty)"}</span>
                  <span className="tasks-grow" />
                  {/* The one thing about a message a person can still CHANGE:
                      its time or its wording, before it goes out. Quiet and on
                      hover, because it applies to a minority of rows. */}
                  {onEditEntry && m.state === "pending" && m.entry_id && (
                    <button
                      type="button"
                      className="tasks-act"
                      title="Edit"
                      aria-label="Edit"
                      onClick={(e) => {
                        e.stopPropagation();
                        onEditEntry(m.entry_id);
                      }}
                    >
                      {ICON_PENCIL}
                    </button>
                  )}
                  {/* Beside Edit and inside the same hover-revealed group, so a
                      thread of already-sent messages — which is most threads —
                      grows no chrome at rest. The label is the honest one for
                      what the call does to a repeat: see cancelIntent. */}
                  {stop && (
                    <button
                      type="button"
                      className="tasks-act tasks-act--cancel"
                      title={stop.title}
                      aria-label={stop.label}
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation();
                        void cancel(m, stop.id);
                      }}
                    >
                      {stop.scope === "occurrence" ? ICON_SKIP : ICON_BAN}
                    </button>
                  )}
                  {/* When it RAN, said only when that is not when it was due:
                      caught up late after a shut app, or run early by a drag
                      into In Progress. The due time itself never moves, which
                      is what makes this line worth printing. */}
                  {ranNote(m) && <span className="tasks-msg-ran">{ranNote(m)}</span>}
                  <span className="tasks-msg-time" title={messageWhenTitle(m)}>
                    {messageTime(m.at)}
                  </span>
                </div>
                {why && <p className="tasks-msg-error">{why}</p>}
              </Fragment>
            );
          })}

          {error && <p className="tasks-thread-error">{error}</p>}

          {view.more && (
            <button
              type="button"
              className="tasks-more"
              disabled={loading}
              onClick={onShowMore}
            >
              <span className="tasks-more-icon" aria-hidden>{ICON_CHEVRON_DOWN}</span>
              {loading ? "Loading…" : `Show ${view.hidden} more`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Board view: columns of tasks --------------------------------------------

const LANE_INITIAL_VISIBLE = 10;
const LANE_REVEAL = 10;

// Which lanes are rolled up into the 52px rail, remembered across visits —
// Archive is closed by default because it is the one lane nobody opens the page
// to read.
const COLLAPSED_KEY = "fused-render:scheduled-board-collapsed";

function readCollapsed(): Set<BoardColumn> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return new Set(parsed as BoardColumn[]);
    }
  } catch {
    // A blocked/private store costs the memory, never the board.
  }
  return new Set<BoardColumn>(["archived"]);
}

export function TaskBoard({
  tasks,
  home = "",
  onReload,
}: {
  /** Already filtered, in the SERVER's order. */
  tasks: Task[];
  home?: string;
  /** Re-read the list after a drop lands (or fails). */
  onReload: () => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<BoardColumn>>(readCollapsed);
  const [visible, setVisible] = useState<Record<string, number>>({});
  // The card in flight and the lane under it. Native HTML5 drag — a column
  // move needs nothing fancier than the platform's own.
  const [dragging, setDragging] = useState<Task | null>(null);
  const [overLane, setOverLane] = useState<BoardColumn | null>(null);
  // What the server said about the last move the board asked for — a drop, or a
  // card's own Archive button. One line above the lanes, because both are the
  // same kind of news about the same board.
  const [note, setNote] = useState<string | null>(null);

  const allowed = useMemo(
    () => new Set(dragging ? dropLanes(dragging) : []),
    [dragging],
  );

  // The one lane whose drop is not a filing decision. It is named while the
  // card is still in the air because "run this now" is not undoable and the
  // dashed legal-drop outline says nothing about which of the two it is.
  const runLane = useMemo(() => {
    if (!dragging) return null;
    for (const col of BOARD_COLUMNS) {
      if (dropAction(dragging, col.key)?.kind === "run") return col.key;
    }
    return null;
  }, [dragging]);

  const drop = async (lane: BoardColumn) => {
    const task = dragging;
    setDragging(null);
    setOverLane(null);
    if (!task || !allowed.has(lane)) return;
    // Which of the two things this drop means — file the task, or run its next
    // message early — is tasks-lib's decision, not this handler's.
    const action = dropAction(task, lane);
    if (!action) return;
    setNote(null);
    try {
      if (action.kind === "run") {
        // Upcoming → In Progress. The message goes out NOW and its `due` is
        // left alone, so the thread reads as a run that happened early rather
        // than a schedule that was quietly rewritten.
        await runScheduledNow(action.entryId);
      } else {
        await setSessionTriage(task.session_id, action.status);
      }
    } catch (e) {
      // A refusal here is a real answer, not a bug — the scheduler's loop may
      // have sent the message, or claimed it, while the card was in the air —
      // so the server's own sentence is what gets shown, and the board re-reads
      // either way.
      setNote((e as Error).message);
    }
    onReload();
  };

  // The same triage write the drop above makes, asked for by a card's own
  // button instead of a gesture. It lives up here rather than in TaskCard so the
  // refusal lands in the board's ONE note line, beside the drag's: a sentence
  // tucked inside a 260px lane under one card is a sentence nobody reads.
  const triage = async (task: Task, status: ArchiveStatus) => {
    setNote(null);
    try {
      await setSessionTriage(task.session_id, status);
    } catch (e) {
      setNote((e as Error).message);
    }
    onReload();
  };

  // Run now / Re-run from a card, which the Board simply did not have (Akshil,
  // 2026-08-17: "I have a rerun option in list, I have a rerun option in
  // calendar, but I don't have a rerun option in Kanban"). The drag covers half
  // of it — Upcoming → In Progress runs the pending message early — and cannot
  // cover the other half, because re-sending a message that already went is work
  // a gesture must not be able to consent to. So the card gets the button both
  // other views have.
  //
  // Up here rather than in TaskCard, exactly like `triage` above: this is the
  // board's own call and its refusal belongs in the board's ONE note line. The
  // common one is a 409 because that conversation has a turn open right now, which
  // reads as "wait", not "broken" — the same quiet line the drag's refusals use.
  const runNow = async (intent: TaskRunIntent) => {
    setNote(null);
    try {
      // performRun is shared with the List's row, so "Re-run" cannot mean two
      // different calls on two views.
      const said = await performRun(intent);
      if (said) setNote(said);
    } catch (e) {
      setNote((e as Error).message);
    }
    onReload();
  };

  // Shared by expanded lane bodies AND collapsed rails, so Archive — collapsed
  // by default — still catches the drop most cards are allowed.
  const dropProps = (lane: BoardColumn) => ({
    onDragOver: (ev: ReactDragEvent) => {
      if (!dragging || !allowed.has(lane)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      if (overLane !== lane) setOverLane(lane);
    },
    onDragLeave: () => {
      if (overLane === lane) setOverLane(null);
    },
    onDrop: (ev: ReactDragEvent) => {
      ev.preventDefault();
      void drop(lane);
    },
  });

  const toggleLane = (key: BoardColumn) => {
    setCollapsed((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
      } catch {
        // best-effort; a full or blocked store never breaks the board
      }
      return next;
    });
  };

  const byLane = useMemo(() => groupByColumn(tasks), [tasks]);

  return (
    <>
      {note && <p className="schedule-tv-note">{note}</p>}
      <div className="schedule-tv-board">
        {BOARD_COLUMNS.map((col) => {
          const lane = byLane.get(col.key) ?? [];
          if (collapsed.has(col.key)) {
            return (
              <button
                type="button"
                key={col.key}
                className={
                  "schedule-tv-rail" +
                  (dragging && allowed.has(col.key) ? " is-drop-legal" : "") +
                  (runLane === col.key ? " is-drop-run" : "") +
                  (overLane === col.key ? " is-drop-over" : "")
                }
                title={
                  runLane === col.key
                    ? "Run the next scheduled message now"
                    : `${col.label}: ${lane.length}`
                }
                onClick={() => toggleLane(col.key)}
                {...dropProps(col.key)}
              >
                <StatusIcon status={col.key} />
                <span className="schedule-tv-rail-label">{col.label}</span>
                <span className="schedule-tv-rail-count">{lane.length}</span>
              </button>
            );
          }
          const shown = visible[col.key] ?? LANE_INITIAL_VISIBLE;
          const cards = lane.slice(0, shown);
          const hidden = Math.max(lane.length - cards.length, 0);
          return (
            <div className="schedule-tv-lane" key={col.key}>
              <button
                type="button"
                className="schedule-tv-lane-head"
                title={`Collapse ${col.label}`}
                onClick={() => toggleLane(col.key)}
              >
                <StatusIcon status={col.key} />
                <span className="schedule-tv-lane-label">{col.label}</span>
                <span className="schedule-tv-lane-count">{lane.length}</span>
              </button>
              <div
                className={
                  "schedule-tv-lane-body" +
                  (dragging && allowed.has(col.key) ? " is-drop-legal" : "") +
                  (runLane === col.key ? " is-drop-run" : "") +
                  (overLane === col.key ? " is-drop-over" : "")
                }
                {...dropProps(col.key)}
              >
                {runLane === col.key && (
                  <p className="tasks-run-hint">Run now — the time stays put</p>
                )}
                {cards.map((task) => (
                  <TaskCard
                    key={task.key}
                    task={task}
                    home={home}
                    isDragging={dragging?.key === task.key}
                    onDragStart={() => setDragging(task)}
                    onDragEnd={() => {
                      setDragging(null);
                      setOverLane(null);
                    }}
                    onTriage={(status) => triage(task, status)}
                    onRun={runNow}
                  />
                ))}
                {hidden > 0 && (
                  <button
                    type="button"
                    className="schedule-tv-more"
                    onClick={() =>
                      setVisible((cur) => ({
                        ...cur,
                        [col.key]: (cur[col.key] ?? LANE_INITIAL_VISIBLE) + LANE_REVEAL,
                      }))
                    }
                  >
                    Show {Math.min(LANE_REVEAL, hidden)} more
                  </button>
                )}
                {lane.length > 0 && (hidden > 0 || lane.length >= shown) && (
                  <p className="schedule-tv-showing">
                    Showing {cards.length} of {lane.length}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function TaskCard({
  task,
  home,
  isDragging,
  onDragStart,
  onDragEnd,
  onTriage,
  onRun,
}: {
  task: Task;
  home: string;
  isDragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  /** File this card away, or bring it back — the board owns the call so its
   * refusal lands in the board's own note line. */
  onTriage: (status: ArchiveStatus) => Promise<void>;
  /** Run the task's next message now, or re-send the one that failed. Same
   * arrangement and same reason as onTriage: the board makes the call. */
  onRun: (intent: TaskRunIntent) => Promise<void>;
}) {
  // Whether this card lifts at all, and it is not one question: a task with no
  // session (§5 — Claude Code mints the id on the first run) has nothing to
  // TRIAGE, while a task with no pending message has nothing to RUN. A card
  // that can do neither must not lift, rather than lift into a call that can
  // only fail. tasks-lib.dropLanes holds both halves.
  const draggable = isDraggable(task);
  const href = taskHref(task);
  // Archive without dragging. The drag stays as the accelerator, but it cannot
  // be the ONLY way: the lane it aims at is collapsed by default, so the whole
  // gesture starts with "expand Archive first". Same predicate as the drop, by
  // construction — archiveIntent asks dropAction.
  const file = archiveIntent(task);
  // Run now / Re-run, which the List row and the calendar popover both already
  // offer and this card did not. The SAME function decides it here as there
  // (tasks-lib.taskRunIntent, which asks runNowIntent — the very function
  // dropAction asks), so the card's button, the List's button and the drag onto
  // In Progress can never fire different messages. Nothing about which message
  // or which call is re-derived on this side.
  const run = taskRunIntent(task);
  const [busy, setBusy] = useState(false);
  const triage = async (status: ArchiveStatus) => {
    setBusy(true);
    try {
      await onTriage(status);
    } finally {
      setBusy(false);
    }
  };
  const runNow = async (intent: TaskRunIntent) => {
    setBusy(true);
    try {
      await onRun(intent);
    } finally {
      setBusy(false);
    }
  };
  return (
    // A wrapper, only because the card IS a button and a button cannot hold
    // one. The action is a SIBLING pinned over the card's head — where the head
    // has spare room to its right on every card — rather than a nested control
    // the browser would refuse to parse.
    <div className={"tasks-card-wrap" + (isDragging ? " is-dragging" : "")}>
      <button
        type="button"
        className={
          "schedule-tv-card" +
          (draggable ? " is-draggable" : "") +
          (isDragging ? " is-dragging" : "")
        }
        title={task.title}
        draggable={draggable}
        onDragStart={(ev) => {
          // Some data is required for Firefox to start a drag at all; the task
          // itself travels through React state, not dataTransfer.
          ev.dataTransfer.setData("text/plain", task.key);
          ev.dataTransfer.effectAllowed = "move";
          onDragStart();
        }}
        onDragEnd={onDragEnd}
        onClick={() => {
          if (href) navigateUrl(href);
        }}
      >
        {/* Unread leads here too — it sat at the far end of the head, which is
            the same objection the List's task row just answered. What the card
            does NOT take from the List is the always-drawn empty slot: a card's
            head is one line above a title and a folder chip that both start at
            the card's own edge, so reserving a permanent rail would indent the
            ring away from the two lines under it — a worse misalignment than the
            one it fixes. On a card the pill is simply first when there is one. */}
        <span className="schedule-tv-card-head">
          <UnreadPill count={task.unread} />
          <StatusIcon status={taskColumn(task)} failed={task.failed} />
          <IdChip id={task.task_id} kind="task" />
          {task.live && <LivePulse />}
        </span>
        <span className="schedule-tv-card-title">
          {firstLine(task.title) || "(untitled)"}
        </span>
        <span className="schedule-tv-card-foot">
          <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />
        </span>
      </button>
      {/* Quiet until the card is pointed at or focused, exactly like the List's
          row actions: a lane is a column of cards, and a permanent glyph on
          every one of them would compete with the titles the lane exists to
          show.

          ONE row holding both, rather than two independently pinned buttons:
          a card can offer Run now AND Archive (a failed task with a spent
          message offers exactly that pair), and two absolutely-positioned
          siblings both anchored to `right` would sit on top of each other. The
          strip is laid out by flex and pinned once, so each button is placed by
          the row instead of by its own coordinates. */}
      {(run || file) && (
        <span className="tasks-card-acts">
          {run && (
            <button
              type="button"
              className="tasks-act tasks-card-act tasks-act--run"
              title={run.title}
              aria-label={`${run.label} ${task.task_id}`}
              disabled={busy}
              onClick={() => void runNow(run)}
            >
              {run.rerun ? ICON_RERUN : ICON_PLAY}
            </button>
          )}
          {file && (
            <button
              type="button"
              className={
                "tasks-act tasks-card-act " +
                (file.restore ? "tasks-act--unarchive" : "tasks-act--archive")
              }
              title={file.title}
              aria-label={`${file.label} ${task.task_id}`}
              disabled={busy}
              onClick={() => void triage(file.status)}
            >
              {file.restore ? ICON_UNARCHIVE : ICON_ARCHIVE}
            </button>
          )}
        </span>
      )}
    </div>
  );
}
