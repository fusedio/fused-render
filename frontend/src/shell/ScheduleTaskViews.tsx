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
// The model, in one line: a task IS a Claude session, and §1
// (the model), §3 (ids), §7 (unread), §8 (these two views).
//
// The visual vocabulary is the one the previous views established and the
// calendar still speaks — the bordered status ring, the live ping, the folder
// chip, the 260px lane, the 52px collapsed rail — so the three views read as
// one page. Those live in styles/schedule.css; everything this file adds (the
// accordion, the thread rows, the id chips, the unread dot) is in
// styles/tasks.css.
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { DragEvent as ReactDragEvent } from "react";
import {
  cancelScheduledMessage,
  getTaskMessages,
  markTaskMessageRead,
  markWholeTaskRead,
  resendScheduledMessage,
  runScheduledNow,
  archiveTask,
  unarchiveTask,
} from "@platform/lib/api";
import type { Task, TaskMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { BOARD_COLUMNS, columnLabel } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";
import {
  EMPTY_FILTERS,
  EMPTY_LIST_MEMORY,
  LANE_CHOICE_KEY,
  LIST_MEMORY_KEY,
  UNREAD_LABEL,
  basename,
  cancelIntent,
  carryMarkToHeld,
  dropAction,
  dropLanes,
  filingIntent,
  filterTasks,
  firstLine,
  groupByColumn,
  heldMessages,
  isDraggable,
  isExpandable,
  isFailedTask,
  isUpcomingTask,
  laneRolledUp,
  laneUnread,
  sortByLane,
  showsRowActions,
  statusColumn,
  markAllRead,
  markRead,
  markReadIntent,
  messageEditEntry,
  messageHref,
  threadTone,
  messageWhenTitle,
  nextRunChip,
  openMessageHref,
  openThreadIntent,
  opensElsewhere,
  parseLaneChoices,
  parseListMemory,
  projectOptions,
  relativeWhen,
  settleMarkAllRead,
  spansProjects,
  taskColumn,
  taskRunIntent,
  taskUnread,
  taskUnreadLabel,
  taskWhen,
  threadView,
  tildePath,
  toggleExpanded,
  unmarkAllRead,
  unmarkRead,
  unreadMarker,
  upcomingEditEntry,
} from "./tasks-lib";
import type {
  FilingIntent,
  LaneChoices,
  ListMemory,
  OpenThreadIntent,
  TaskFilters,
  TaskRunIntent,
} from "./tasks-lib";

// The page composes these from one import; re-exported here so Scheduled.tsx
// takes its filter type, its empty value and its filter function from the same
// module it takes the views from.
export { EMPTY_FILTERS, filterTasks, projectOptions, tildePath, basename };
export type { TaskFilters };

/**
 * Whether ANY hover-revealed action is drawn on this page.
 *
 * OFF at Akshil's request, 2026-08-17: "hide them, keep the functionality but hide
 * them", and then again over the message rows — "hide the hover actions for now,
 * that's what I said", said of the pencil on a message row. So the flag covers all
 * three groups rather than the task row's alone:
 *
 *   * the List task row: Mark read, Run now / Re-run, Open chat
 *   * the List message row: Edit, Cancel / Skip this run
 *   * the Board card: Run now / Re-run
 *
 * ARCHIVE IS NO LONGER ONE OF THEM (Akshil, 2026-08-18: bring the archive button
 * back, visible on hover). It is out from behind the flag on BOTH views at once —
 * it is one button on one kind of element, and a List that files a task where a
 * Board cannot is the divergence this page's whole vocabulary is written against.
 * It is still hover-revealed and still `.tasks-act`, so nothing about the strip's
 * geometry or its reveal changed; it is only no longer gated. Each remaining
 * button now carries its OWN guard on this flag rather than the group carrying
 * one, which is what lets Archive sit in its old place in the order instead of
 * jumping to the front of a strip that comes back.
 *
 * ONE flag for all of them, deliberately: a flip must restore the whole page's
 * chrome at once, and two switches is how half of it comes back. Everything behind
 * it is still built and still decided by tasks-lib (markReadIntent, taskRunIntent,
 * archiveIntent, openThreadIntent, cancelIntent), still spent through the shared
 * performers, and still tested. Only the RENDER is gated, and only here.
 *
 * WHAT IS UNREACHABLE WHILE THIS IS OFF, because it is worth writing down rather
 * than discovering: Mark read (the whole-task clear), Run now / Re-run from this
 * page, Open chat as a button — and CANCEL, which is the one that costs a
 * capability rather than a shortcut, since stopping a message that has not gone out
 * has no other control on this page. It survives elsewhere (the queue dock's card,
 * the Claude pane's own banner, and deleting the schedule from the task form). Row
 * clicks are untouched throughout: a leaf row still opens its message, a
 * multi-message row still toggles, a message row still opens its turn.
 *
 * NOT RENDERED rather than hidden with CSS, which is the one thing worth being
 * careful about: an `opacity: 0` button is still in the tab order, so a keyboard
 * would land on an invisible control and press it blind. (Archive, which IS
 * rendered, is hidden the other way on purpose — see `.tasks-act` in tasks.css:
 * hover-revealed by opacity precisely so a keyboard can still reach it.) The
 * geometry that keeps
 * the strip off the title (`.tasks-card-acts` and `--tasks-card-head-h` in
 * tasks.css) stays exactly as it is — it is what the strip comes back to.
 *
 * Annotated `boolean` on purpose: as a bare `false` literal TypeScript narrows
 * every guarded branch to dead code, and flipping the flag would then be a type
 * change rather than a value change.
 */
const SHOW_ROW_ACTIONS: boolean = false;


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
// There is no ICON_CHEVRON_DOWN any more (2026-08-18). It was the down-chevron on
// the thread's dashed "Show N more" button, and that button is gone — expanding a
// task fetches the whole thread by itself. ICON_CHEVRON, the row's own disclosure,
// is a different glyph and is untouched.
const ICON_CHECK = icon(<polyline points="20 6 9 17 4 12" />, 13);
const ICON_CIRCLE_DOT = icon(
  <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="1.5" /></>, 13);
const ICON_FOLDER = icon(
  <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />,
  12,
);
// There is no ICON_CLOCK/ICON_CHAT pair here any more (2026-08-18). A clock on a
// scheduled message and a speech bubble on a chat one used to sit between the
// status ring and MSG-003 on every thread row, saying where the message came
// from. Removed at Akshil's request: it is a third glyph on a 12.5px line whose
// first two already carry the state and the id, and nothing on the page acts on
// the distinction. `.tasks-msg-kind` went from tasks.css with it.
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
// Filing away. lucide `archive`: a lidded box with a pull-slot in the front.
const ICON_ARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" /></>, 13);
// Taking it back out. lucide `archive-restore` — the SAME box, opened at the
// front, with something lifting out of it. That kinship is the whole reason for
// using the pair rather than inventing a mark: the two buttons occupy ONE slot on
// a row (`.tasks-rowmark`), never both at once, so a reader has to be able to tell
// which one they are pointing at from the shape alone, and "same box, arrow out"
// answers that in a glance where a generic undo curl would not.
//
// The box's walls are two short paths rather than one closed body, which is what
// leaves the gap the arrow comes through. Same 13px, same stroke, same lid as
// above, so the two glyphs sit on each other exactly.
const ICON_UNARCHIVE = icon(
  <><rect x="2" y="3" width="20" height="5" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h2" />
    <path d="M20 8v11a2 2 0 0 1-2 2h-2" />
    <path d="m9 15 3-3 3 3" />
    <path d="M12 12v9" /></>, 13);

// ---- leaf components ---------------------------------------------------------

const STATUS_LABELS: Record<BoardColumn, string> = Object.fromEntries(
  BOARD_COLUMNS.map((c) => [c.key, c.label]),
) as Record<BoardColumn, string>;

/**
 * The bordered ring — the ONE mark a unit of work wears, on all three views.
 *
 * HUE is the status. `failed` repaints it red without moving the row out of its
 * column: a failed or missed run IS settled, but folding away the only failure
 * signal would let a dead turn read as a clean one.
 *
 * SHAPE is the read-state (2026-08-18). The centre dot used to mean "settled" and
 * was drawn on every Done and Failed ring; it now means "not looked at yet", and a
 * read one is hollow. That is the whole of the unread vocabulary on this page — the
 * grey dot that used to trail the title is gone, because a row carrying a ring AND
 * a dot a few characters apart makes a reader decode two marks to answer one
 * question. Colour did not move, so nothing was traded: the ring still names its
 * state in the hue it always did.
 *
 * The dot is drawn on EVERY column, and the CSS gate is `--unread` alone. It was
 * scoped to the terminal two for a few hours the same day, on the reasoning that
 * nothing is unread until it has finished — but a recurring or rescheduled task
 * sits in Upcoming, its next run ahead of it, while its thread still holds output
 * from a past run nobody has read. So the combination is real, and the gate drew a
 * hollow ring under a tooltip that said "1 unread". Which states can occur is the
 * server's business and taskUnread's; this component's business is to draw what it
 * is handed, and the stylesheet's is not to have an opinion about the lane.
 *
 * `unread` draws the dot. `count`, when given, is what the mark stands for and
 * turns into the tooltip — "3 unread" — and it is passed by CONTAINERS only: a
 * task row over its thread, a lane header over its cards. A leaf message's dot
 * already means "unread" all by itself and a hover repeating that is a caption on
 * a symbol that needs none (Akshil, 2026-08-18), so a leaf passes `unread` alone
 * and keeps the status word as its tooltip.
 *
 * The count never replaces the accessible name, it extends it: a screen reader
 * hears "Done, 3 unread" rather than losing the status it came for.
 *
 * AND SO DOES THE FILL, WITH OR WITHOUT A COUNT. The name was extended only when
 * `count` was set, which left every LEAF — the thread rows and the calendar's
 * popover rows, all of which pass `unread` alone — announcing "Done" whether or
 * not the reader had seen it. The dot was the only carrier of the fact and it is
 * not one for anybody who cannot see it (bugbot, PR #596). A leaf now says "Done,
 * unread": the bare word, because a leaf's mark stands for one message and there
 * is no number to give. The visual rule is untouched — this is the same one bit
 * the shape carries, said out loud.
 *
 * THE COUNT'S TOOLTIP IS NOT A `title` (2026-08-18). The browser holds a native
 * tooltip back for one to two seconds, and for a four-character readout that is
 * the same as not offering it at all. It goes to `data-tip`, which schedule.css
 * draws on hover after 300ms. `title=""` rather than no title: an element with no
 * `title` lets the browser walk up for one, and this sits inside a lane header
 * that has "Collapse Done" and a row that has the task's full title.
 *
 * A leaf keeps its `title` — the status word, no count, and a slow native tooltip
 * is the right speed for a word nobody is waiting on.
 */
export function StatusIcon({
  status,
  failed,
  label,
  unread,
  count,
}: {
  status: BoardColumn;
  failed?: boolean;
  label?: string;
  /** Fill the centre — there is something in here nobody has looked at. */
  unread?: boolean;
  /** What that fill stands for, on a container. Omitted on a leaf. */
  count?: number;
}) {
  const text = label ?? (failed ? "Failed" : (STATUS_LABELS[status] ?? status));
  const many = taskUnreadLabel(count ?? 0);
  // What the FILL is worth in words. The count when there is one, the bare word
  // when there is not — and nothing at all on a hollow ring, which is the point:
  // a read mark has nothing to announce. `many` is null at count 0, so a container
  // that is drawn unread but merged to zero still says "unread" rather than
  // dropping the fact the ink is showing.
  const said = many ?? (unread ? UNREAD_LABEL.toLowerCase() : null);
  return (
    <span
      className={
        `schedule-ring schedule-ring--${status}` +
        (failed ? " schedule-ring--failed" : "") +
        (unread ? " schedule-ring--unread" : "")
      }
      aria-label={said ? `${text}, ${said}` : text}
      data-tip={many ?? ""}
      title={many ? "" : text}
    />
  );
}

/* There is no `LivePulse` any more (2026-08-18). It was a blue `--activity` disc
   that followed a live task's title on the List row and sat in the Board card's
   head, and it meant "a turn is in flight right now" — a finer fact than the
   In Progress lane, which also holds a queued turn that has not started.

   It went because of what it LOOKED like rather than what it said. With the whole
   unread vocabulary reduced to the status ring, the ping was the last free-
   standing dot on the page, and a small filled circle after a title is what unread
   means everywhere else in this app and every other. Akshil, 2026-08-18, on a
   screenshot of a row reading "…sk workflow analysis ●": that blue dot should not
   be there. A mark that says "running" in the exact shape the page uses for "you
   have not read this" is a mark that will be misread every time.

   What still carries "in flight": the In Progress ring's yellow, the queue dock,
   and the row's own relative time. The `task.live` flag is untouched on the model
   and in tasks-lib (openThreadIntent and the run intents still read it), so
   restoring a mark for it later is a rendering decision, not a data one — it just
   cannot be a filled dot after a title. */

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

/* There is no `UnreadDot` any more (2026-08-18). It was a 7px grey dot trailing a
   task's title on the List row and the Board card, and before that a numeric pill
   in the same slot; both are gone the same way, and for the reason written out in
   full at tasks-lib.taskUnreadLabel — a row already carries a status ring, and a
   second mark a few characters away, saying a different thing about the same unit
   of work, is one glyph too many to scan. Read-state is the ring's SHAPE now
   (StatusIcon above); the count survives as that ring's tooltip. */

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
    // Fire and forget as far as the NAVIGATION goes — the click is leaving the
    // page, so a refusal has nobody left to be told — but the mark itself is
    // taken back on one (tasks-lib.unmarkRead). "The next poll brings the dot
    // back" was not true: the local entry outranks the poll for as long as this
    // component lives, so a write nobody noticed failing hid the dot until the
    // List remounted.
    void markTaskMessageRead(taskKey, m.message_id).catch(() => {
      setRead((cur) => unmarkRead(cur, taskKey, m.message_id));
    });
  };
  // The whole task, on the row's own button and on any gesture that opens the
  // thread. Two halves, both from tasks-lib.markAllRead: a concrete id for every
  // message this component HOLDS, and one observation-stamped sentinel for the
  // ones outside the window, whose ids it has never seen.
  //
  // `held` is tasks-lib.heldMessages, never the listing window on its own: after
  // Show more the thread on screen is all 89, and ids off the three the listing
  // carried would zero the count over 86 dots that nothing could take back.
  const clearAll = (task: Task, held?: TaskMessage[]) => {
    setRead((cur) => markAllRead(cur, task, held));
  };
  // The other direction of the same seam: a thread that has only just ARRIVED,
  // under a mark that is still standing. Show more's reply is a read of the value
  // the press overrode, so its `unread` flags are pre-mark — and nothing refetches
  // them. tasks-lib.carryMarkToHeld decides whether the mark still covers them.
  const carryAll = (task: Task, held: TaskMessage[]) => {
    setRead((cur) => carryMarkToHeld(cur, task, held));
  };
  // ...and the way back, which is the half that was missing. `held` is the list
  // the press wrote its concrete ids from, captured BEFORE the request went out:
  // a poll can replace the thread while the write is in flight, and a rollback
  // has to remove what was actually written.
  const restoreAll = (taskKey: string, held: TaskMessage[]) => {
    setRead((cur) => unmarkAllRead(cur, taskKey, held));
  };
  // The server's own answer to the mark, spent through the one rule that reads
  // it (tasks-lib.settleMarkAllRead): a non-zero count means the press did not
  // clear the row after all.
  const settleAll = (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => {
    setRead((cur) => settleMarkAllRead(cur, taskKey, held, answer));
  };
  return { read, clear, clearAll, carryAll, restoreAll, settleAll };
}

/** The whole-task half of useReadSet, for the two performers below — they are
 * module functions rather than hooks, so the marks are handed to them. */
interface ReadMarks {
  clearAll: (task: Task, held?: TaskMessage[]) => void;
  restoreAll: (taskKey: string, held: TaskMessage[]) => void;
  settleAll: (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => void;
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

/**
 * Take one task back out of Archive, and return the sentence to show for it.
 *
 * THE SENTENCE IS THE POINT, and it is why this is a function rather than a call.
 * Unarchiving names no lane: the server drops the filing and DERIVES where the
 * task belongs from its thread, so the card is about to appear somewhere the
 * reader did not choose and cannot predict — a different lane on the Board, a
 * different rank on the List, quite possibly off screen. Three gestures reach
 * this (the List's button, the card's button, the drag out of the lane) and all
 * three need the same sentence; three copies of it is how they start telling the
 * reader three different things.
 *
 * Refusals THROW, exactly like performRun, so each caller puts them in its own
 * note line.
 */
async function performUnarchive(key: string): Promise<string> {
  const said = await unarchiveTask(key);
  // `unfiled: false` is the server saying NOTHING CHANGED — no filing to clear,
  // or a cancelled-only thread whose derived status is still Archive. Claiming
  // "Unarchived — back in Archive" for that would be the note lying about a
  // move that never happened (Bugbot, 2026-08-18).
  if (!said.unfiled) return `Nothing to unarchive — still ${columnLabel(statusColumn(said.status))}.`;
  return `Unarchived — back in ${columnLabel(statusColumn(said.status))}.`;
}

/**
 * Spend an OpenThreadIntent: the ONE place either view opens a conversation.
 *
 * Both gestures that go to a thread come through here — the Board card's click
 * and the List row's Open chat button. The Board had this inline and the List's
 * button navigated and marked nothing, so the same gesture to the same href came
 * back with the badge cleared or not depending on which view you were in (the
 * List's button was the one place that still disagreed). Lifting it is the same
 * move performRun made above, for the same reason: two copies is how the two
 * views start disagreeing again.
 *
 * WHETHER anything is marked is not decided here — that is
 * tasks-lib.openThreadIntent, which answers `markRead: false` when the count is
 * already zero and offers no intent at all for a task with no session. This only
 * performs it.
 *
 * The local clear goes FIRST, so the pill cannot outlive its own press (the page
 * polls on a 20s interval). The server call is ONE whole-task request, and the
 * navigation is OUTSIDE the guard and never waits on it — but the answer is no
 * longer thrown away. A refusal takes the mark back and a non-zero remaining
 * count settles it (tasks-lib.settleMarkAllRead), because "the next poll brings
 * it back" was never true of a local override that outranks the poll. Nobody is
 * shown a sentence here — the press left the page — so the correction IS the
 * whole report: the pill is there again when the reader comes back.
 *
 * `held` is passed IN rather than read off `task.messages` here, and it is not
 * optional. This press hops away, so the view it marks is usually unmounted a
 * frame later and its read set goes with it — but "usually" is not a rule to
 * write a mark against: the frames before the hop still paint, and this is the
 * SAME gesture as the row's own Mark read button on a thread the row may have
 * expanded to all 89. A mark that covered less here than there would be a rule
 * that depended on which button you pressed. The List hands over what the thread
 * holds (tasks-lib.heldMessages); the Board hands over its card's window, which
 * is all a card ever holds — it has no Show more.
 */
function performOpen(
  task: Task,
  intent: OpenThreadIntent,
  marks: ReadMarks,
  held: TaskMessage[],
): void {
  if (intent.markRead) {
    marks.clearAll(task, held);
    void markWholeTaskRead(task.key)
      .then((answer) => marks.settleAll(task.key, held, answer))
      .catch(() => marks.restoreAll(task.key, held));
  }
  navigateUrl(intent.href);
}

// ---- List view: one accordion per task ---------------------------------------

/** How long the list keeps trying to reach the offset it was left at. Rows grow
 * as their threads land, so the target is unreachable for the first few frames;
 * past this it is a list that simply cannot be that tall any more. */
const RESTORE_WINDOW_MS = 3000;
/** Scroll fires per frame; the store is written once the reader pauses. */
const WRITE_DEBOUNCE_MS = 150;

/** Per-TAB, per-sitting (sessionStorage): "where I was a moment ago" is not a
 * preference, and a week-old offset restored into a list of different rows is a
 * surprise rather than a memory. A blocked store costs the memory, never the
 * page — the read runs during first render. */
function readListMemory(): ListMemory {
  try {
    return parseListMemory(sessionStorage.getItem(LIST_MEMORY_KEY));
  } catch {
    return EMPTY_LIST_MEMORY;
  }
}

function writeListMemory(memory: ListMemory): void {
  try {
    sessionStorage.setItem(LIST_MEMORY_KEY, JSON.stringify(memory));
  } catch {
    // best-effort; a full or blocked store never breaks the list
  }
}

export function TaskList({
  tasks,
  home = "",
  stale = false,
  onEditEntry,
  onReload,
  emptyLabel = "Nothing to show here.",
}: {
  /** Already filtered, in the SERVER's order. Never re-sorted here. */
  tasks: Task[];
  /** $HOME, only so a folder tooltip can say "~/Desktop/fused". */
  home?: string;
  /** Is this empty list a FAILURE rather than an answer? A failed poll sets
   * `tasks` to `[]` exactly like a filter that matched nothing does (Scheduled
   * `tasksFailed`), and the scroll memory below has to tell them apart: a list
   * the reader emptied is worth forgetting the offset for, a list the network
   * lost is not. Defaults false, so a caller that never fails never has to
   * think about it. */
  stale?: boolean;
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
  // poll. Seeded from THIS TAB's memory, though: opening a task's chat leaves the
  // page, and coming back to a collapsed list scrolled to the top made reading
  // three threads out of ninety three trips through the same scrollbar.
  //
  // Read once, into a ref, because both halves of the memory are initial state:
  // re-reading it later would fight the writes below.
  const memory = useRef<ListMemory>(readListMemory());
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(memory.current.expanded),
  );
  // WHERE THE READER JUST WAS. Seeded from the same memory as the two above and
  // restored with them: a list of ninety near-identical rows gives no clue which
  // one you came back out of, so "now the next one" meant re-finding the last
  // one first (Akshil, 2026-08-18). Held in state as well as in the memory ref
  // because it is also LIVE — the row lights the moment it is pressed, so the
  // highlight is the page acknowledging the press rather than something that
  // only appears after a round trip.
  const [selected, setSelected] = useState(() => memory.current.selected);
  const select = (key: string) => {
    setSelected(key);
    remember({ ...memory.current, selected: key });
  };
  // Full threads fetched by Show more, keyed by task. They REPLACE the three
  // the listing carried rather than appending to them, so no message is ever
  // drawn twice.
  const [loaded, setLoaded] = useState<Record<string, TaskMessage[]>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { read, clear, clearAll, carryAll, restoreAll, settleAll } = useReadSet();

  // The latest poll's tasks, readable from ACROSS an await. showMore closes over
  // the render its button was pressed in, and the question it asks when the fetch
  // lands — is the whole-task mark still standing? — is a question about what the
  // server is quoting NOW, so a task one poll stale would answer it about numbers
  // nobody is looking at any more.
  const latest = useRef(tasks);
  useEffect(() => {
    latest.current = tasks;
  }, [tasks]);

  // Whether a row draws its folder chip at all — asked ONCE for the whole list,
  // because the question is about the list and not about any one row
  // (tasks-lib.spansProjects). Cheap, and memoised only so it is not a new answer
  // on every keystroke of the search box.
  const showProject = useMemo(() => spansProjects(tasks), [tasks]);

  // The list's rows, in rank order (tasks-lib.sortByLane). Memoised for the same
  // reason `showProject` is: this runs on every keystroke of the search box and
  // the answer only moves when the rows do.
  const rows = useMemo(() => sortByLane(tasks), [tasks]);

  /**
   * Open or close a task — and, on the way OPEN, fetch the rest of its thread.
   *
   * There is no "Show 23 more" button any more (Akshil, 2026-08-18). The chevron
   * showed three messages and then a dashed button under them, so reading a thread
   * of twenty-six was two gestures for one intention: a person who expanded a task
   * asked for the task, not for a sample of it.
   *
   * THE CAP WAS NEVER A RENDERING CHOICE, which is why removing it is a fetch and
   * not a slice. The listing endpoint sends three messages per row on purpose — it
   * runs for every task on the page, and a full transcript parse per task would not
   * survive a few hundred of them (server routers/tasks.py `_row`) — so the other
   * twenty-three genuinely are not in the client's hands when the row is drawn.
   * The button was the press that went and got them. The press is gone; the trip
   * still happens, now triggered by the disclosure itself.
   *
   * Guarded three ways so it is exactly one trip: only when OPENING, only when the
   * server's own count says the window is short (threadView `more`, asked without
   * `loaded` so it means "is the listing truncated?"), and never while a fetch for
   * this task is already in flight or already landed. A closed-and-reopened task
   * re-reads nothing — `loaded` outlives the expansion, deliberately, because the
   * thread it holds is still the thread.
   */
  const toggle = (task: Task) => {
    const opening = !expanded.has(task.key);
    setExpanded((cur) => toggleExpanded(cur, task.key));
    if (opening && threadView(task).more && !loaded[task.key] && !loading[task.key]) {
      void showMore(task);
    }
  };

  const showMore = async (task: Task) => {
    setLoading((cur) => ({ ...cur, [task.key]: true }));
    setErrors((cur) => {
      const next = { ...cur };
      delete next[task.key];
      return next;
    });
    try {
      const r = await getTaskMessages(task.key);
      const thread = r.messages ?? [];
      setLoaded((cur) => ({ ...cur, [task.key]: thread }));
      // This reply is a READ, and a read can be of a value we have already
      // overridden: if the reader pressed Mark read (or Open chat) a moment ago,
      // the server had not applied it when it composed this thread, so 86
      // messages are about to arrive flagged unread — and `more` is false now, so
      // nothing ever refetches them. carryAll adopts the standing mark onto them,
      // and does nothing at all once that mark has expired. Asked against the
      // freshest task we have, because "is the mark still standing?" is a question
      // about the newest poll and not about the render this press came from.
      const fresh = latest.current.find((t) => t.key === task.key) ?? task;
      carryAll(fresh, thread);
    } catch (e) {
      // Said under the thread it belongs to, not as a page banner: the rest of
      // the list is intact and only this one thread failed to open.
      //
      // `loaded` is deliberately left UNSET here, and that is what makes the
      // failure recoverable rather than terminal: every guard that asks "do we
      // already have this thread?" still answers no, so the very same call can be
      // made again. The error line's Retry button is that call (TaskNode
      // `onRetry`), and this function clears the error on its way back in, so a
      // retry that succeeds leaves nothing stale behind it.
      setErrors((cur) => ({ ...cur, [task.key]: (e as Error).message }));
    } finally {
      setLoading((cur) => ({ ...cur, [task.key]: false }));
    }
  };

  // A row restored from memory was never TOGGLED, so nothing went and got the
  // rest of its thread — it would sit there showing the listing's three messages
  // with no button left to ask for the other twenty-three. Same trip the chevron
  // makes, made once, the first time a task list arrives.
  const restoredThreads = useRef(false);
  useEffect(() => {
    if (restoredThreads.current || tasks.length === 0) return;
    restoredThreads.current = true;
    for (const key of memory.current.expanded) {
      const task = tasks.find((t) => t.key === key);
      if (task && threadView(task).more) void showMore(task);
    }
    // Runs on every poll and does something exactly once — the guard above is
    // what makes it a restore rather than a refetch loop.
  }, [tasks]);

  // ---- where the list stood ---------------------------------------------------
  // `.tasks-list` is its own scroller (styles/tasks.css), so this is one element's
  // scrollTop and not the window's — which is also why restoring it cannot fight
  // the explorer's msg-anchor scroll: that happens on a different page entirely.
  const listRef = useRef<HTMLDivElement | null>(null);
  // The offset still owed to the reader, or null once it has been paid (or given
  // up on). Rows grow as their fetched threads land, so the wanted offset is
  // often past the end of the list for the first few frames; it is re-applied
  // every render until the content is tall enough to honour it.
  const owed = useRef<number | null>(memory.current.scroll || null);
  // The last offset THIS code set, so a scroll event can be told apart from the
  // reader's own — theirs cancels the restore, and nothing else does.
  const settled = useRef<number | null>(null);

  // Is there a list on screen at all? Asked once, up here, because three separate
  // things below turn on it: when the restore deadline opens, whether an empty
  // list is worth forgetting an offset over, and the recovery immediately below.
  const hasRows = tasks.length > 0;
  const hadRows = useRef(false);
  if (hasRows) hadRows.current = true;

  // ROWS COME BACK, AND THE OFFSET HAS TO BE WAITING WHEN THEY DO (bugbot,
  // 2026-08-18). Holding the memory across a failed poll only got the reader
  // halfway there: `owed` is seeded once at mount and cleared the moment the
  // restore is paid, so by the time a poll fails there is nothing owed any more.
  // The rows came back twenty seconds later, the scroller remounted at zero, and
  // the preserved offset sat in the store with nothing left to read it — the
  // reader landed at the top, which is the exact outcome preserving the memory
  // was meant to prevent.
  //
  // So a stale empty ARMS the restore again rather than merely not destroying it.
  // `settled` is reset with it: the scroller that comes back is a new element at
  // zero, and the offset this code last set belonged to the old one.
  //
  // A layout effect, and deliberately ABOVE the one that pays the restore, so
  // both run in the same commit and in that order — the re-arm lands before the
  // payer reads `owed`, and the rows are restored in the frame they return in
  // rather than one frame later.
  const staleEmptied = useRef(false);
  if (!hasRows && stale && hadRows.current) staleEmptied.current = true;
  useLayoutEffect(() => {
    if (!hasRows || !staleEmptied.current) return;
    staleEmptied.current = false;
    owed.current = memory.current.scroll || null;
    settled.current = null;
  }, [hasRows]);

  useLayoutEffect(() => {
    const el = listRef.current;
    if (owed.current === null || !el) return;
    const top = Math.min(owed.current, Math.max(el.scrollHeight - el.clientHeight, 0));
    if (Math.abs(el.scrollTop - top) > 1) el.scrollTop = top;
    settled.current = el.scrollTop;
    if (top >= owed.current - 1) owed.current = null;
  });

  // The restore window closes on its own. Without this, a list that can never
  // grow tall enough (rows deleted since the visit) would keep pinning itself to
  // the bottom on every poll.
  //
  // IT OPENS ON THE FIRST ROWS, NOT ON MOUNT (bugbot, 2026-08-18). Tasks arrive
  // from a fetch, so this component mounts against an empty list and stays that
  // way for as long as the request takes; a deadline started at mount was
  // therefore spending most of itself — sometimes all of it, on a cold server or
  // a slow disk — waiting for the rows it was meant to be measuring. The window
  // is supposed to be "a few seconds of settling once there is something to
  // settle", so `hasRows` is what starts the clock.
  //
  // It re-arms on every false→true, which is what the stale recovery above needs:
  // a restore armed again when the rows return needs a deadline of its own, and
  // the one from the first load is long since spent.
  useEffect(() => {
    if (!hasRows) return;
    const t = setTimeout(() => {
      owed.current = null;
    }, RESTORE_WINDOW_MS);
    return () => clearTimeout(t);
  }, [hasRows]);

  // One writer for both halves, so the stored row is always whole. Debounced,
  // because the scroll half fires per frame and this leaves the page by pushState
  // (an unmount that a dropped write would silently lose is not worth the risk of
  // relying on).
  const writeTimer = useRef<number | null>(null);
  const remember = (next: ListMemory) => {
    memory.current = next;
    if (writeTimer.current !== null) clearTimeout(writeTimer.current);
    writeTimer.current = window.setTimeout(() => {
      writeTimer.current = null;
      writeListMemory(memory.current);
    }, WRITE_DEBOUNCE_MS);
  };
  useEffect(
    () => () => {
      if (writeTimer.current !== null) {
        clearTimeout(writeTimer.current);
        writeListMemory(memory.current);
      }
    },
    [],
  );

  useEffect(() => {
    remember({ ...memory.current, expanded: [...expanded] });
  }, [expanded]);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    // Whose scroll was this? The layout effect above records every offset IT
    // sets in `settled`, so an event landing on that exact offset is the echo of
    // this code's own write and an event landing anywhere else is the reader.
    const mine = settled.current !== null && Math.abs(el.scrollTop - settled.current) <= 1;
    settled.current = el.scrollTop;
    // A RESTORE IN PROGRESS WRITES NOTHING (bugbot, 2026-08-18). The restore is
    // paid in instalments — the wanted offset is past the end of a list whose
    // rows are still growing as their threads land, so the layout effect gets
    // partway there, and partway again, until the content is tall enough. Every
    // one of those partial offsets used to be saved over the real one, so a
    // reader who left at 1200px and came back to a list that momentarily only
    // reached 300 had their position quietly rewritten to 300 — the memory
    // destroyed by the act of restoring it. Only the reader's own scroll is a
    // statement about where they want to be, so only the reader's own is stored.
    if (mine) return;
    // And their scroll means they have chosen where to be: the owed offset stops
    // being owed.
    owed.current = null;
    remember({ ...memory.current, scroll: el.scrollTop });
  };

  // AN EMPTY LIST IS A POSITION TOO, and it is the top (bugbot, 2026-08-18).
  // Typing in the search box until nothing matches unmounts the scroller, and the
  // scroller is the only thing that reports scrolling — so the last offset from
  // before the filter narrowed just sat in the store, describing a list that is
  // no longer on screen. Clearing the search then restored it, and the reader who
  // had scrolled to the top to start typing was thrown back down the list by a
  // number they had stopped meaning several keystrokes ago. There is nothing
  // below an empty state to be scrolled to, so the honest memory is zero, and
  // nothing is owed either: whatever restore was pending has nowhere to land.
  //
  // ONLY FOR A LIST THAT EMPTIED, never for one that has not filled yet: this
  // component mounts against an empty `tasks` while the fetch is out, and zeroing
  // the memory there would erase the very offset this whole section exists to pay
  // back, before the rows it belongs to have even arrived.
  //
  // AND ONLY FOR AN EMPTINESS THE SERVER MEANT (bugbot, 2026-08-18). A failed
  // poll also sets `tasks` to `[]` — the page keeps its shape and says "Tasks
  // could not be loaded" over an empty list (Scheduled `tasksFailed`) — so a
  // single dropped request in the 20s poll used to be indistinguishable from a
  // filter that matched nothing, and permanently forgot where the reader was.
  // That is the worst possible moment to forget it: the rows are coming back in
  // twenty seconds, and the reader is about to be dropped at the top of a list
  // they were halfway down. `stale` is the poll saying "this empty is mine, not
  // the data's", and an empty we cannot vouch for changes nothing at all — it
  // re-arms the restore instead, up where `staleEmptied` is set.
  useEffect(() => {
    if (hasRows || stale || !hadRows.current) return;
    owed.current = null;
    settled.current = null;
    remember({ ...memory.current, scroll: 0 });
  }, [hasRows, stale]);

  if (!hasRows) {
    return <p className="schedule-tv-empty">{emptyLabel}</p>;
  }

  return (
    <div className="tasks-list" ref={listRef} onScroll={onScroll}>
      {/* ORDERED BY STATUS, NOT GROUPED BY IT (tasks-lib.sortByLane): Upcoming,
          In Progress, Failed, Done, Archive — rank order, server order inside
          each rank, and no headers, dividers or counts between them.

          The grouping is a SORT and nothing more (Akshil, 2026-08-18). Headers
          lived here for a round and were wrong on a list: the Board's lanes are
          a fixed frame, so a lane header labels the frame and earns its ink,
          where a list has no frame and five headers are five interruptions in
          the one column a person is scanning. The order already says what they
          said. */}
      {rows.map((task) => (
        <TaskNode
          key={task.key}
          task={task}
          home={home}
          showProject={showProject}
          open={expanded.has(task.key)}
          selected={selected === task.key}
          onSelect={() => select(task.key)}
          onToggle={() => toggle(task)}
          onRetry={() => void showMore(task)}
          loaded={loaded[task.key]}
          loading={!!loading[task.key]}
          error={errors[task.key]}
          onEditEntry={onEditEntry}
          onReload={onReload}
          read={read}
          onRead={clear}
          onReadAll={clearAll}
          onUnreadAll={restoreAll}
          onSettleAll={settleAll}
        />
      ))}
    </div>
  );
}

function TaskNode({
  task,
  home,
  showProject,
  open: requested,
  selected,
  onSelect,
  onToggle,
  loaded,
  loading,
  onRetry,
  error,
  onEditEntry,
  onReload,
  read,
  onRead,
  onReadAll,
  onUnreadAll,
  onSettleAll,
}: {
  task: Task;
  home: string;
  /** Whether the folder chip is worth drawing. The LIST's answer, not this row's:
   * a chip that every visible row repeats distinguishes nothing (spansProjects). */
  showProject: boolean;
  /** What the List's expanded set says about this row. Whether it is honoured is
   * this component's decision — see `expandable` below. */
  open: boolean;
  /** Is this the row the reader last opened a conversation from? The List owns
   * the answer (one row at a time, remembered across the trip to the chat); the
   * row only wears it. */
  selected: boolean;
  /** Say that this row is now that one. Spent by every gesture that LEAVES the
   * page — the row's press and a message row's — and by nothing else: expanding
   * a task is reading it in place, not going anywhere. */
  onSelect: () => void;
  onToggle: () => void;
  loaded?: TaskMessage[];
  loading: boolean;
  /** Fetch this task's thread again after a failure. The SAME call the disclosure
   * makes — not a second path to the same endpoint, because two ways in are two
   * ways to disagree about the guards. */
  onRetry: () => void;
  error?: string;
  onEditEntry?: (entryId: string) => void;
  onReload?: () => void;
  read: Set<string>;
  onRead: (taskKey: string, m: TaskMessage) => void;
  /** Clear this whole task's unread locally — the optimistic half of Mark read,
   * paired with the one server call the button makes. `held` is everything this
   * thread has in its hands (tasks-lib.heldMessages), which after Show more is
   * all of it. */
  onReadAll: (task: Task, held?: TaskMessage[]) => void;
  /** Put it back: the write was refused, so the dots and the button return. */
  onUnreadAll: (taskKey: string, held: TaskMessage[]) => void;
  /** Reconcile the optimism against the server's own answer to the mark. */
  onSettleAll: (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => void;
}) {
  // Is this row an accordion at all? tasks-lib.isExpandable asks the server's
  // message_count, because a thread of one message has nothing under it but a
  // restatement of this row's own title.
  //
  // `open` is DERIVED from it rather than merely rendered around it: the guard
  // belongs in the predicate, so a row that is in the List's expanded set and then
  // stops being expandable closes itself instead of being stuck open with no
  // control to close it. That cannot happen today (a thread never shrinks), but
  // "cannot happen" is not a thing to leave a render depending on.
  const expandable = isExpandable(task);
  const open = expandable && requested;
  const view = threadView(task, loaded);
  // Everything this thread holds, one list: the listing window before Show more,
  // the whole fetched thread after it, and either way the listing's fresher copy
  // of anything in both. The count, the button's intent, the mark and the mark's
  // rollback are all asked of THIS — one set, so the number on the row and the
  // dots under it cannot be answers about two different lists.
  const held = heldMessages(task, loaded);
  const unread = taskUnread(task, read, held);
  // What the thread holds AFTER an await, which is not what `held` above closed
  // over: markSeen is written against the render its button was pressed in, and a
  // Show more that lands while the write is in flight adopts the mark onto the
  // rest of the thread (useReadSet.carryAll). A rollback that could not see those
  // ids would put the count back over dots it had no key to relight.
  const heldNow = useRef(held);
  // Every render, deliberately: `held` is a fresh list each time and this ref is
  // only ever read from inside an in-flight write.
  useEffect(() => {
    heldNow.current = held;
  });
  // Open chat. Where it goes and whether going there also clears the thread —
  // one answer, from the same function the Board card asks (openThreadIntent),
  // because it is the same gesture to the same href and must come back with the
  // same badge. Null means no session yet (§5): no button at all, so nothing is
  // offered and nothing is marked. Asked with the count this row is DRAWING, so
  // a second press on an already-cleared task posts nothing.
  const chat = openThreadIntent(task, unread);
  const label = firstLine(task.title) || "(untitled)";
  // Whether this row's work is still ahead of it, which is the one thing that
  // greys its title. tasks-lib.isUpcomingTask owns both halves of the question
  // (the lane, and whether its next run has already gone by).
  const ahead = isUpcomingTask(task);
  // The scheduled run a ONE-MESSAGE UPCOMING row's press edits when it has no
  // conversation to open instead, because the instruction that has not run yet is
  // the only content such a row has. tasks-lib.upcomingEditEntry owns all three conditions — the
  // lane, the one message, and which entry — and it is asked of `held`, the same
  // list `sole` is. Gated on `onEditEntry` here because without it there is no form
  // to open (a thread with no edit affordance is read-only), and then the press
  // falls through to the arms below.
  const edit = onEditEntry ? upcomingEditEntry(task, held) : null;
  // When this task runs next, or when it last ran — one time at the end of every
  // row, beside the folder. Which of the two is tasks-lib.taskWhen's decision (it
  // reads LANE_SORTS, the same map the Board's lanes are ordered by), and null when
  // the task has neither, in which case nothing is drawn.
  const when = taskWhen(task);
  // The run still to come, when the row's own time is not already it.
  const soon = nextRunChip(task);
  // Run now / Re-run. tasks-lib decides all of it — whether it is offered,
  // which message it acts on, and WHICH CALL that is. The run-now half comes
  // from the same function the drag asks (runNowIntent), so the button and the
  // drop can never fire different messages; the re-send half is the case the
  // drag deliberately does not have, because a gesture cannot consent to
  // creating work that was never scheduled.
  //
  // NOT ON AN ARCHIVED ROW, and neither is anything else below (`showsRowActions`):
  // a task somebody put away has one decision left against it, and that is
  // whether it is still put away. Offering Re-run next to Unarchive would also
  // invite the one misread this whole gesture is written against — that coming
  // back out of Archive runs something.
  const acts = showsRowActions(task);
  const run = acts ? taskRunIntent(task) : null;
  // FILING, either direction. The Board's drop onto — or out of — the Archive
  // lane, reachable without switching view, expanding a collapsed lane and
  // dragging, which is what those moves used to cost. tasks-lib decides
  // everything, by asking dropAction the same questions the drag does, so a row
  // draws the button exactly when the card would take the drop.
  const file = filingIntent(task);
  // Mark read — the whole task at once, so clearing 89 unread messages is not 89
  // clicks through 89 transcripts. Asked of the count this row is DRAWING, so
  // the button leaves on its own press rather than on the next poll.
  const seen = acts ? markReadIntent(task, read, held) : null;

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

  // Filing, either direction. One call each, and the same ones the board's drops
  // make: the server cancels the work and files the session going in, and drops
  // the filing coming out, so this side composes nothing but the sentence.
  const refile = async (intent: FilingIntent) => {
    setActing(true);
    setNote("");
    try {
      if (intent.kind === "archive") {
        await archiveTask(task.key);
      } else {
        // WHERE IT WENT, said out loud — see performUnarchive. On a list sorted
        // by lane the row is about to move somewhere the reader did not point at,
        // and a sentence is the only pointer this view has.
        setNote(await performUnarchive(task.key));
      }
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
  // THE OPTIMISM IS THEN RECONCILED, which is the part that was missing. It used
  // to be planted and never revisited, so a refused write left this row looking
  // read with its own Mark read button gone — no dots, no count, no retry — and
  // the comment here claimed the next poll would restore the truth. It could
  // not: the local mark outranked every poll for as long as the List stayed
  // mounted. So:
  //
  //   * a refusal takes the mark back (dots, count and button return) and says
  //     what the server said;
  //   * a 200 that still reports unread means something arrived while the
  //     request was in flight, and that wins too — the row goes back to
  //     reporting it rather than swallowing it;
  //   * and everything the mark DID cover stays cleared, instantly, which is
  //     the 20 seconds this whole mechanism exists to hide.
  //
  // Still no onReload: the count is already right locally, and a reload would
  // repaint every row in the list to say the one thing this row has said.
  const markSeen = async () => {
    // Captured before the await: `held` and `task` can both be replaced by a
    // poll while the request is in flight, and a rollback has to remove the ids
    // the press actually wrote. Plus whatever the thread holds by the time the
    // answer lands (heldNow) — a Show more that arrived meanwhile carried this
    // very mark onto the rest of the thread, and those ids are the press's too.
    const wrote = held;
    const rollback = () => [...wrote, ...heldNow.current];
    setActing(true);
    setNote("");
    onReadAll(task, held);
    try {
      const answer = await markWholeTaskRead(task.key);
      if (answer.unread > 0) {
        onSettleAll(task.key, rollback(), answer);
        // News, not a fault — hence the quiet note the other row actions use.
        setNote(
          answer.unread === 1
            ? "1 message arrived while this was marking, and is still unread."
            : `${answer.unread} messages arrived while this was marking, and are still unread.`,
        );
      }
    } catch (e) {
      onUnreadAll(task.key, rollback());
      setNote((e as Error).message);
    } finally {
      setActing(false);
    }
  };

  const openMessage = (m: TaskMessage) => {
    onRead(task.key, m);
    const to = messageHref(task, m);
    if (!to) return;
    // Leaving the page, so this is the row to come back to — the thread row
    // belongs to this task, and the task's row is what is still on screen when
    // the reader returns.
    onSelect();
    navigateUrl(to);
  };

  /**
   * A MESSAGE ROW's own gesture — one function, so its click and its Enter/Space
   * cannot drift apart, exactly as `activate` is one function for the task row.
   *
   * Two meanings, and the split is the message's own state (tasks-lib.
   * messageEditEntry, the same predicate Cancel asks):
   *
   *   * a message that HAS NOT GONE OUT opens the EDIT FORM on its own entry — it
   *     is an instruction, not a transcript turn, and the form is the only place
   *     that instruction can be read or changed. Its own entry id, never the
   *     task's next run: a repeating task has several pending occurrences and the
   *     row pressed is the one the reader means.
   *   * anything that HAS run opens its turn in the transcript, through
   *     openMessage above — unchanged, including the read mark it clears.
   *
   * NOTHING IS MARKED ON THE EDIT ARM, and there is nothing to mark: a message
   * that has not happened is not unread in the first place (tasks-lib.isUnread),
   * so the dot the reader would be owed does not exist. `openMessage` keeps the
   * per-message mark it always had, on the only rows that can carry one.
   */
  const pressMessage = (m: TaskMessage) => {
    const entry = onEditEntry ? messageEditEntry(m) : null;
    if (entry) onEditEntry?.(entry);
    else openMessage(m);
  };

  // Open chat: the thread, and the unread cleared on the way out — the same
  // performer the Board card's click spends (performOpen), so the two gestures
  // cannot disagree. Deliberately NOT markSeen above: that one is a press that
  // STAYS on this page, so it awaits the write and has somewhere to say a
  // refusal; this one is leaving, so it fires and forgets and never holds up the
  // hop. `onReadAll` is the local half, the same one markSeen uses — and it is
  // handed the same `held` list, so the two gestures on this row cannot clear
  // different amounts of the same thread.
  //
  // Declared ABOVE `activate` because `activate` now calls it — a hoisted
  // reference into a `const` below would work at runtime and read as a bug.
  const openChat = (intent: OpenThreadIntent) => {
    // This is the row LEAVING the page, so it is also the row to come back to.
    // Marked here rather than in `activate` because `activate` has a second arm
    // — the edit form, a modal over this very page — and lighting a row for a
    // trip the reader never took would make the highlight mean nothing. Every
    // way of opening this task's conversation goes through this one function
    // (the row's press, the Open chat button), so every one of them marks.
    onSelect();
    performOpen(
      task,
      intent,
      {
        clearAll: onReadAll,
        restoreAll: onUnreadAll,
        settleAll: onSettleAll,
      },
      held,
    );
  };

  /**
   * The task ROW's own gesture — one function, so the mouse and the keyboard
   * cannot drift apart (Enter and Space, and the stretched link's plain click,
   * all run exactly this).
   *
   * ONE MEANING NOW, AND IT IS "OPEN IT" (Akshil, 2026-08-18): a press anywhere
   * on the row goes to the conversation, at the END of the chat. The accordion
   * used to be this function's first arm, which made the commonest row on the
   * page — a task with a thread — the one row whose click did NOT open the thing
   * it names; expanding is the chevron's job now, and the chevron's gutter is
   * wide enough to aim at (see the caret below, and tasks.css).
   *
   * Two arms are left, and the split is what the row HAS:
   *
   *   * a row with a SESSION opens its thread, through openChat above — the same
   *     intent (openThreadIntent) and the same performer the Board card spends,
   *     so there is exactly one way to address a thread and one answer about what
   *     opening it marks. No `msg=` anchor, deliberately: the row is the whole
   *     task, so the turn it means is the latest one, which is where a chat opens
   *     by itself. A MESSAGE row is what addresses one turn, and it still does.
   *   * a row with NO session opens THE EDIT FORM on its scheduled run, when it
   *     has one (tasks-lib.upcomingEditEntry — the lane, exactly one message, and
   *     which entry). Such a row has no conversation to open and its whole content
   *     is an instruction that has not run yet, so the form is the only thing its
   *     press could honestly mean (Akshil, 2026-08-17: "when i click on upcoming
   *     tasks i think they should open up the edit modal... only for 1 message
   *     tasks"). Reached through `onEditEntry`, the same callback the thread's own
   *     Edit button and the calendar popover spend.
   *
   * There is no third arm for the LEAF-with-a-session that used to open its one
   * message: `chat` covers it, and one message is the whole chat anyway.
   *
   * A task with no session AND no resolvable entry (a `pending:<entry>` an older
   * server sent no next-run fields for) still does nothing, and does not
   * ADVERTISE a press either — see `pressable` below.
   *
   * NOTHING IS MARKED READ ON THE EDIT ARM: the message it opens the form for has
   * not gone out, so there is nothing there to have seen. The chat arm's mark is
   * openThreadIntent's decision, not this function's.
   */
  const activate = () => {
    if (chat) openChat(chat);
    else if (edit) onEditEntry?.(edit);
  };

  /**
   * WHERE the row's press goes, as a URL — or null when the press opens a modal
   * (the edit arm) and there is nowhere to link to.
   *
   * This is what makes ⌘-click, middle click and "Open in new tab" work: the row
   * draws a real `<a href>` stretched over itself (`.tasks-rowlink`) rather than
   * hanging a click handler on a div, so every one of those gestures is the
   * browser's own behaviour and none of them is reimplemented here. The plain
   * click is the only one this page intercepts — see the handler, and
   * tasks-lib.opensElsewhere for the rule it asks.
   */
  const href = chat?.href ?? null;

  /**
   * Does this row's press DO anything — and therefore, may the row claim to be a
   * control at all?
   *
   * The arms of `activate`, so the affordance cannot drift from the behaviour. A
   * never-run `pending:<entry>` row with no resolvable entry carried
   * `role="button"`, a tab stop, a hover tint and a pointer cursor while doing
   * nothing on press; an inert row is inert in what it says too.
   *
   * `expandable` is deliberately NOT here any more. A row whose only affordance is
   * its disclosure gets that affordance from the chevron, which is a real button
   * with its own tab stop — the row itself stays inert, and pointing at it would
   * be a promise the row's own press no longer keeps.
   */
  const pressable = href !== null || edit !== null;

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
        className={"tasks-row" + (open ? " is-open" : "")
          + (selected ? " is-selected" : "") + (pressable ? "" : " is-inert")}
        // The row is a CONTAINER now, not a control: when it has somewhere to go
        // the stretched `<a>` below is the button, the tab stop and the
        // accessible name, and hanging a second role and a second tab stop on
        // this div would give every row two of each. The EDIT arm has no href —
        // it opens a modal — so that one row keeps the old role/tabIndex/keydown
        // treatment, and `pressable` still decides whether anything at all is
        // claimed (`is-inert` turns the cursor and the hover tint off).
        role={pressable && !href ? "button" : undefined}
        tabIndex={pressable && !href ? 0 : undefined}
        title={task.title}
        onClick={href ? undefined : pressable ? activate : undefined}
        onKeyDown={
          href
            ? undefined
            : (e) => {
                if (!pressable) return;
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  activate();
                }
              }
        }
      >
        {/* The disclosure gutter is drawn WHETHER OR NOT there is a chevron in it.
            `--tasks-caret-w` is the first term of `--tasks-rail-x`, which every
            indent on this page is measured from (tasks.css), so dropping the
            element on a one-message row would slide that row's status ring — and
            the whole rail it stands in — a mark and a gap to the left of its
            neighbours' and turn a column of rings into a zigzag. So the box stays
            and only the glyph goes.

            IT IS THE ONLY WAY TO EXPAND A ROW, since 2026-08-18 — the row's own
            press opens the conversation now (see `activate`). So it is a real
            button with a real label, and its HIT ZONE is far bigger than its ink:
            tasks.css grows it to the row's full height and out to the row's
            leading edge with padding, and takes the growth back out of the layout
            with matching negative margins, so nothing moves by a pixel and there
            is no thin 16px target to aim at. It also sits ABOVE the stretched row
            link, which is what keeps the two zones apart: gutter expands,
            everything else opens. `stopPropagation` is belt and braces — the link
            is a sibling, not an ancestor — and costs nothing.

            The rotation is on the inner glyph, not the button: see tasks.css. */}
        {expandable ? (
          <button
            type="button"
            className="tasks-caret"
            aria-expanded={open}
            aria-label={open ? "Collapse messages" : "Expand messages"}
            title={open ? "Collapse messages" : "Expand messages"}
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
          >
            <span className={"tasks-caret-glyph" + (open ? " is-open" : "")} aria-hidden>
              {ICON_CHEVRON}
            </span>
          </button>
        ) : (
          <span className="tasks-caret" aria-hidden />
        )}
        {/* The row's navigation, as a real link stretched over the whole row
            (tasks.css `.tasks-rowlink`). Empty on purpose: it carries the href,
            the tab stop and the accessible name, and the row's own children carry
            every pixel of ink.

            A MODIFIED press is left entirely alone — no preventDefault, no SPA
            navigation, and NO READ MARK: ⌘-click means "open that in a tab for
            later", and clearing the badge for a conversation nobody has looked at
            yet is exactly the thing a background open must not do. The plain click
            is the only one intercepted, and it spends `activate` — the same
            function Enter spends on the edit-arm row above, so no gesture on this
            page has a private meaning. */}
        {href && (
          <a
            className="tasks-rowlink"
            href={href}
            title={task.title}
            aria-label={label}
            onClick={(e) => {
              if (opensElsewhere(e)) return;
              e.preventDefault();
              activate();
            }}
          />
        )}
        {/* The ring opens the row, and it stands in the column every task row's
            ring stands in (tasks.css `--tasks-rail-x`), which is also the column
            the thread below is measured from — its own rings hang exactly one ring
            slot to the right of this one, so the indent that says "these belong to
            that" is a whole mark wide rather than an arbitrary gap. The caret stays
            outside the column, in the gutter to its left, because it is the
            accordion's control and not part of it.

            IT IS ALSO THE ROW'S UNREAD MARK (2026-08-18). There was a separate
            grey dot after the title for a day, and before that a numeric pill in
            the same slot; the ring's centre now carries the fact instead, filled
            while anything in this task's thread is unread and hollow once it is
            all read. One mark, two facts — hue for the state, shape for whether
            anybody has looked — rather than two marks at opposite ends of a title
            that a reader has to pair up. `unread` is the merged count this row is
            DRAWING, so the ring hollows on the row's own press rather than on the
            next poll, and it is the count the tooltip names. */}
        {/* THE MARK SLOT: the ring at rest, the Archive button under the pointer,
            in the same box (`.tasks-rowmark`, tasks.css).

            The two used to be at opposite ends of the row — the ring in the rail
            and Archive out by the folder chip — which made "file this away" a
            control the reader had to travel to, and put a hover-revealed button
            in the one part of the row that is already the busiest (id, folder,
            two times). The swap costs nothing to read: a row being pointed at is
            a row whose status the reader has just read, and it is the same
            gesture every list of this shape uses.

            The button is ABSOLUTELY positioned inside a slot sized to the ring,
            so it is out of flow and no pixel of the row can move when it appears
            — the rail (`--tasks-rail-x`), the thread's indent under it and the
            caret's hit zone to its left are all untouched. */}
        <span className="tasks-rowmark">
          <StatusIcon
            status={taskColumn(task)}
            failed={task.failed}
            unread={unread > 0}
            count={unread}
          />
          {/* The Board's drag onto Archive — or out of it — as a press. ONE
              button in this slot, never two: a task is either put away or it is
              not, so tasks-lib.filingIntent answers with a direction and this
              draws that direction's glyph and calls that direction's verb.

              ON AN ARCHIVED ROW IT IS THE ONLY BUTTON (showsRowActions, above).
              Unarchive beside a Re-run would suggest the two are related, and the
              one thing this gesture must never be read as is starting work.

              THE ONE ROW ACTION NOT BEHIND SHOW_ROW_ACTIONS (Akshil, 2026-08-18:
              bring the archive button back, visible on hover). Filing a task away
              had no press anywhere in the List while the strip was off — the only
              route was switching to the Board, expanding the Archive lane and
              dragging — which made the honest answer to "can a task be deleted?"
              (no: it is archived) barely true on this view.

              HOVER-REVEALED, not permanent: a list at rest must grow no chrome
              (§2 — only critical actions get visible buttons), and this is one
              button on every row that has ever run. `.tasks-act` in tasks.css
              owns that, and it does it with `opacity` plus a `:focus-visible` arm
              rather than `visibility`/`display`, so the button stays in the tab
              order and lights up for a keyboard that lands on it. It is still not
              rendered at all on a task with nothing to file, which is the
              difference that matters: hidden-until-hover is for a live control,
              not for a dead one. */}
          {file && (
            <button
              type="button"
              className={"tasks-act tasks-act--" + file.kind}
              title={file.title}
              aria-label={file.label}
              disabled={acting}
              onClick={(e) => {
                e.stopPropagation();
                void refile(file);
              }}
            >
              {file.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
            </button>
          )}
        </span>
        <IdChip id={task.task_id} kind="task" />
        {/* Greyed while the work is still ahead of it (tasks-lib.isUpcomingTask):
            a list is mostly history, and the rows that have not happened yet are
            the ones a reader is not being asked to read. The TITLE only — the id,
            the ring, the folder and the time all stay at full strength, because
            fading the whole row would say "archived", which is a different fact
            with a lane of its own. */}
        <span className={"tasks-title" + (ahead ? " is-upcoming" : "")}>{label}</span>
        {/* Nothing follows the title. The live ping used to (see LivePulse's
            headstone above): a blue disc in the one position, and the one shape,
            that means unread everywhere else. */}

        {/* Exactly ONE auto margin in this row: flex distributes free space
            equally across every auto margin, so a second one would park the
            right-hand group in the middle of the row instead of at its end. */}
        <span className="tasks-grow" />

        {/* THE STRIP IS BEHIND SHOW_ROW_ACTIONS, all of it. Archive is the one
            row action that is live, and it is no longer part of this strip at all
            — it moved into the mark slot at the row's leading edge on 2026-08-18
            (see `.tasks-rowmark` above). Each button still carries its own guard
            rather than the group carrying one, so the three keep their hard-won
            ORDER whichever of them are rendered: "clear it, run it, open it". */}
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
            task's unread, cleared from the row that carries the mark, in the
            SAME hover-revealed group as Run now and Archive. Only on a task that
            has unread (tasks-lib.markReadIntent): every other row would carry a
            button whose press does nothing, which is what makes the rows where
            it matters hard to pick out. */}
        {SHOW_ROW_ACTIONS && seen && (
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
        {SHOW_ROW_ACTIONS && run && (
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
        {/* ARCHIVE IS NOT HERE ANY MORE (2026-08-18). It sat between Run now and
            Open chat, out by the folder chip; it is in the row's MARK SLOT now,
            swapping with the status ring on hover (see `.tasks-rowmark` above).
            The strip's remaining order still reads left-to-right as "clear it,
            run it, open it", and the day SHOW_ROW_ACTIONS flips there is no
            fourth button to find a place for. */}
        {/* The one gesture in this row that OPENS a MULTI-message conversation
            — so it is the one that also clears the thread, exactly as the Board
            card's click does: it lands the reader in the very thread this row's
            ring is filled for, and a mark still sitting there afterwards would
            be pointing at what the press just showed them. Both sides ask
            tasks-lib.openThreadIntent and both spend it through performOpen. The
            row's OWN click is still not this (see `activate`): on an accordion it
            toggles and opens nothing, and on a leaf it opens that leaf's single
            message through the message path, marking that one message. */}
        {SHOW_ROW_ACTIONS && chat && (
          <button
            type="button"
            className="tasks-act"
            title="Open chat"
            aria-label="Open chat"
            onClick={(e) => {
              e.stopPropagation();
              openChat(chat);
            }}
          >
            {ICON_OPEN}
          </button>
        )}
        {/* When this task runs next, or when it last ran — on EVERY row, because
            until now a time only appeared inside an expanded thread and a
            one-message task has no thread to expand (Akshil, 2026-08-17).

            ONE RELATIVE UNIT ("30m ago", "in 2h"), the same vocabulary the message
            rows below now speak (tasks-lib.relativeWhen). It printed a clock AND a
            date for an hour and that was the crowding this row was trimmed for:
            "both the folder and the time with the date, they are like too much for
            me to handle". The absolute instant, and WHICH run it is, are in the
            tooltip — the ink cannot say that in one unit and does not try.

            After the spacer, both it and the folder `flex: 0 0 auto`: the row has
            exactly ONE auto margin (`.tasks-grow` above) and the title is the
            element that shrinks, so a long title ellipsises rather than squeezing
            the time. Nothing is drawn when the task has neither run. */}
        {/* The folder, only when the list SPANS folders (tasks-lib.spansProjects,
            asked of the rows on screen rather than of the filter). Filtered to one
            project, every row was repeating the same word at the busiest end of the
            row — a chip that distinguishes nothing. The full path is still the
            row's own tooltip either way.

            Folder FIRST, time last (Akshil, 2026-08-18). The two were the other way
            round when the time arrived; at the end of a row the last thing before
            the edge is the one a reader lands on, and the time is what changes. */}
        {showProject && (
          <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />
        )}
        {/* ALWAYS drawn (2026-08-18). It used to be `{when && …}` and taskWhen
            returned null on a task whose three-message window is empty — a session
            holding only a `/clear` — which left the last cell of that row blank
            while every row around it read "4d ago". A hole in a column reads as a
            broken row, not as an absent fact. taskWhen now falls back to the
            session's own `last_active` and, failing even that, hands back an em
            dash with `kind: "none"`, so this element is unconditional and the
            column always holds.

            No class of its own for the dash, and no CSS change at all here: the em
            dash belongs in exactly the register the times beside it are in — it IS
            one of the column's values, not a different kind of thing — and
            `.tasks-row-time` already sizes, colours and aligns it. */}
        {/* AND THE RUN THAT IS STILL COMING, when the time beside it is not
            already that (tasks-lib.nextRunChip).

            A recurring task whose last run finished sits in DONE now, not
            Upcoming — the output nobody has read is what needs eyes, and a
            promise is not a verdict (server `_message_verdict`). That is the
            right lane and it drops one true fact off the row: the task is not
            over. So the row says it, once, in the vocabulary every other time
            here speaks (relativeWhen, absolute instant in the tooltip).

            A CHIP and not a second time column: `.tasks-row-time` is the row's
            one time slot and this is a different question, so it is marked
            rather than aligned. Nothing is drawn on an Upcoming row, where the
            time IS the next run and the chip would say it twice. */}
        {soon && (
          <span className="tasks-row-next" title={soon.title}>
            {soon.text}
          </span>
        )}
        <span className="tasks-row-time" title={when.title}>
          {when.text}
        </span>
      </div>

      {/* Why the refusal is quiet: see runNow. The class is the board's own
          drag-error line, because these are the board's own calls. */}
      {note && <p className="schedule-tv-note tasks-row-note">{note}</p>}

      {open && (
        <div className="tasks-thread">
          {view.messages.map((m) => {
            // threadTone, not messageTone: a thread under an archived task is
            // archived with it, except for a turn that is still running
            // (tasks-lib says why).
            const tone = threadTone(task, m);
            const mark = unreadMarker(task.key, m, read);
            const isNew = mark.unread;
            const stop = cancelIntent(m);
            // The one entry this message can be edited as, or null when its press
            // means the transcript instead — ONE reading, spent by both the row's
            // press (pressMessage) and the pencil below, so the quiet action and
            // the whole-row gesture cannot disagree about which rows are editable.
            const fix = onEditEntry ? messageEditEntry(m) : null;
            // Where this row's press GOES, or null when it opens the edit form
            // instead (the `fix` arm) or has nowhere to go at all — a projected
            // occurrence is cron arithmetic and addresses no turn
            // (tasks-lib.openMessageHref). Non-null is what turns the row into a
            // real link, and therefore what makes ⌘-click open it in a tab.
            const to = fix ? null : openMessageHref(task, m);
            const busy = cancelling === m.message_id;
            const why = cancelErrors[m.message_id];
            return (
              <Fragment key={m.message_id}>
                <div
                  className={"tasks-msg" + (isNew ? " is-unread" : "")}
                  // Same division as the task row above: a row that LINKS puts its
                  // role, tab stop and name on the stretched `<a>`, and only a row
                  // that opens a modal keeps them here.
                  role={to ? undefined : "button"}
                  tabIndex={to ? undefined : 0}
                  title={m.body}
                  // One handler for both ways in, the same rule the task row obeys:
                  // `pressMessage` opens the form on a message that has not gone out
                  // and the transcript turn on one that has.
                  onClick={to ? undefined : () => pressMessage(m)}
                  onKeyDown={
                    to
                      ? undefined
                      : (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            pressMessage(m);
                          }
                        }
                  }
                >
                  {/* The message row's own stretched link — its turn in the
                      transcript, `msg=` anchor and all, so ⌘-click stacks a turn
                      up in a tab and a plain click scrolls this one open exactly
                      as it always did. The modified press marks nothing, for the
                      reason written on the task row's link. */}
                  {to && (
                    <a
                      className="tasks-rowlink"
                      href={to}
                      title={m.body}
                      aria-label={firstLine(m.body) || "(empty)"}
                      onClick={(e) => {
                        if (opensElsewhere(e)) return;
                        e.preventDefault();
                        pressMessage(m);
                      }}
                    />
                  )}
                  {/* The leaf's ring, and its unread mark: filled centre while
                      this one message is unread, hollow once it has been opened
                      — the same glyph the task row above it wears over the whole
                      thread. NO `count` is passed, so the tooltip stays the status
                      word: one unread message's dot means "unread" outright, and
                      "1 unread" on hover would be a caption for a symbol that
                      needs none (Akshil, 2026-08-18). */}
                  <StatusIcon
                    status={tone.column}
                    failed={tone.failed}
                    label={tone.label}
                    unread={isNew}
                  />
                  {/* No kind glyph before the id (2026-08-18). A clock on a
                      scheduled message and a speech bubble on a chat one stood
                      between the ring and MSG-003 on every row of every thread —
                      two more marks in a lane that already opens with one, for a
                      distinction the row's own words and time make anyway. The
                      id and the body lead now. */}
                  <IdChip id={m.message_id} kind="message" />
                  <span className="tasks-msg-body">{firstLine(m.body) || "(empty)"}</span>
                  {/* No dot after the body any more (2026-08-18). It trailed the
                      title here, and led the row in a reserved rail slot before
                      that, and both arrangements were arguing about WHERE to put a
                      second mark on a row that already opens with a status ring.
                      The ring absorbed it: see the StatusIcon above, and
                      tasks-lib.taskUnreadLabel for the whole of the reasoning. The
                      row's bold body (`.tasks-msg.is-unread`) is untouched and is
                      still the fact stated twice — once in the mark, once in the
                      weight — which is what makes an unread line findable in a
                      thread of twenty. */}
                  <span className="tasks-grow" />
                  {/* A MESSAGE row's actions are behind the same flag as the task
                      row's strip (SHOW_ROW_ACTIONS, off). Akshil, 2026-08-17:
                      "hide the hover actions for now, that's what I said" — said
                      of this pencil, so the flag covers every hover-revealed
                      action in the List rather than the task row's only. ONE flag
                      for both, so a flip cannot bring half of them back. */}
                  {SHOW_ROW_ACTIONS && (
                    <>
                      {/* The one thing about a message a person can still CHANGE:
                          its time or its wording, before it goes out. Quiet and on
                          hover, because it applies to a minority of rows. */}
                      {fix && (
                        <button
                          type="button"
                          className="tasks-act"
                          title="Edit"
                          aria-label="Edit"
                          onClick={(e) => {
                            e.stopPropagation();
                            onEditEntry?.(fix);
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
                    </>
                  )}
                  {/* ONE time per row, relative and one unit wide (relativeWhen),
                      with the absolute instant in the tooltip. It reads `at` — what
                      the message was DUE at, the instant that never moves — and the
                      tooltip is where a run that fired late or early is spelled out
                      (messageWhenTitle). The "ran 07:12 today" label that used to
                      sit beside this is gone: 2026-08-17, "I don't think I need this
                      as well, the RAND Today stuff". */}
                  <span className="tasks-msg-time" title={messageWhenTitle(m)}>
                    {relativeWhen(m.at)}
                  </span>
                </div>
                {why && <p className="tasks-msg-error">{why}</p>}
              </Fragment>
            );
          })}

          {/* A FAILED FETCH HAS TO BE RECOVERABLE WHERE IT HAPPENED (bugbot, PR
              #596). While the thread was capped, the "Show N more" button was
              also the retry: a failed press left the button sitting there to be
              pressed again. Removing the cap removed that by accident — the fetch
              moved onto the disclosure, so the only way to ask again was to
              collapse the row and re-expand it, which is a gesture nobody would
              guess from an error line that does not mention it.

              So the recovery sits next to the failure it is about (§4: help users
              with errors, with the recovery action beside them). Same call the
              disclosure makes, and `showMore` clears this error on its way back
              in, so a retry that succeeds leaves nothing stale behind.

              `role="alert"` on the line, because it appears without a press and a
              reader who expanded the row is owed the news. The button is a real,
              always-visible control rather than one of the page's hover-revealed
              actions: those are conveniences on a working row, and this is the
              only way out of a broken one. */}
          {error && (
            <p className="tasks-thread-error" role="alert">
              {error}{" "}
              <button
                type="button"
                className="tasks-retry"
                disabled={loading}
                onClick={onRetry}
              >
                Retry
              </button>
            </p>
          )}

          {/* No "Show N more" button here any more (2026-08-18). Expanding a task
              now fetches the whole thread by itself (TasksList.toggle), so what
              stands in the button's place is a line saying the trip is happening —
              not a control, because there is nothing left to decide.

              It names the NUMBER still coming (`view.hidden`, the server's count
              less what we hold) rather than saying a bare "Loading…": the three
              rows above it are already drawn, so without the count a long thread
              looks like a short thread that has finished. It disappears when the
              rest arrives, which is the only signal a person needed from it.

              `aria-live="polite"` because this is the one thing on the row that
              changes without a press — a reader who expanded the task should hear
              that more is coming, and then be left alone. */}
          {loading && (
            <p className="tasks-thread-loading" aria-live="polite">
              {view.hidden > 0 ? `Loading ${view.hidden} more…` : "Loading…"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Board view: columns of tasks --------------------------------------------

// A lane opens on twenty cards and reveals twenty at a time. Ten was two presses
// to read a busy column (Akshil, 2026-08-18) and the lane scrolls anyway, so the
// window is about how much is rendered, not about how much fits.
const LANE_INITIAL_VISIBLE = 20;
const LANE_REVEAL = 20;

// Which lanes are rolled up into the 52px rail. The RULE lives in tasks-lib —
// `laneCollapsed` for what the store says and `laneRolledUp` for what is drawn —
// and THIS is only half of the state: a lane with cards remembers the reader's
// toggle here, and a lane with none remembers nothing at all (the peek, in
// TaskBoard). So a lane nobody has touched keeps following the rule as it fills
// and empties, and so does one they touched while it was empty.
function readLaneChoices(): LaneChoices {
  try {
    return parseLaneChoices(localStorage.getItem(LANE_CHOICE_KEY));
  } catch {
    // A blocked/private store costs the memory, never the board.
    return {};
  }
}

export function TaskBoard({
  tasks,
  home = "",
  onReload,
}: {
  /** Already filtered, in the SERVER's order — the LANES re-order it
   * (tasks-lib.groupByColumn), which is the one thing this view does to the
   * order it is handed and the one place it is decided. */
  tasks: Task[];
  home?: string;
  /** Re-read the list after a drop lands (or fails). */
  onReload: () => void;
}) {
  const [choices, setChoices] = useState<LaneChoices>(readLaneChoices);
  // Lanes the reader has opened WHILE EMPTY. Deliberately component state and
  // deliberately not persisted: opening a column with nothing in it is a peek,
  // and a peek is answered and over (tasks-lib, above `laneCollapsed`). A remount
  // — every navigation back to this page — starts the board with none.
  const [peeked, setPeeked] = useState<Set<BoardColumn>>(() => new Set());
  const [visible, setVisible] = useState<Record<string, number>>({});
  // The card in flight and the lane under it. Native HTML5 drag — a column
  // move needs nothing fancier than the platform's own.
  const [dragging, setDragging] = useState<Task | null>(null);
  const [overLane, setOverLane] = useState<BoardColumn | null>(null);
  // What the server said about the last move the board asked for — a drop, or a
  // card's own Archive button. One line above the lanes, because both are the
  // same kind of news about the same board.
  const [note, setNote] = useState<string | null>(null);
  // The same read bookkeeping the List uses, for the same reason: a card's
  // pill has to go on the click that opens the thread, not 20 seconds later on
  // the next poll. Only the whole-task half is wanted here — a card links the
  // conversation, never one turn of it, so there is no per-message click to
  // make on this view.
  const { read, clearAll, restoreAll, settleAll } = useReadSet();

  // The same question the List asks of its rows, asked of the same set here: a
  // folder chip on every card of a board filtered to one project distinguishes
  // nothing (tasks-lib.spansProjects). Asked of the BOARD's whole shown set rather
  // than per lane, because a lane that happens to hold one project is not a page
  // that holds one — the reader is looking at all five columns at once.
  const showProject = useMemo(() => spansProjects(tasks), [tasks]);


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
    // Which of the three things this drop means — file the task, un-file it, or
    // run its next message early — is tasks-lib's decision, not this handler's.
    const action = dropAction(task, lane);
    if (!action) return;
    setNote(null);
    try {
      if (action.kind === "run") {
        // Upcoming → In Progress. The message goes out NOW and its `due` is
        // left alone, so the thread reads as a run that happened early rather
        // than a schedule that was quietly rewritten.
        await runScheduledNow(action.entryId);
      } else if (action.kind === "unarchive") {
        // Archive → anywhere else. ONE meaning whatever `lane` is: the filing is
        // dropped and the task lands in whatever lane it DERIVES to, which is
        // frequently not the lane under the cursor. Nothing runs — including a
        // drop onto In Progress, which is this same call: that lane is Claude's
        // output, not a state a reader can assert, so the card goes there only
        // if a turn genuinely is live.
        //
        // So the note says where it actually went — the same sentence the two
        // buttons show (performUnarchive). There is no flash or scroll on this
        // board to point at the card with, and a card that silently reappears in
        // a lane the person was not looking at is a gesture that seems to have
        // done nothing.
        setNote(await performUnarchive(task.key));
      } else {
        // → Archive. ONE call for both halves — the pending work is cancelled
        // and the session is filed — because a card dropped here that still
        // fires tomorrow un-archives itself.
        await archiveTask(task.key);
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

  // The same two calls the drop above makes, asked for by a card's own button
  // instead of a gesture. It lives up here rather than in TaskCard so the refusal
  // lands in the board's ONE note line, beside the drag's: a sentence tucked
  // inside a 260px lane under one card is a sentence nobody reads. The unarchive
  // note is the same one the drop writes, for the same reason — the card is about
  // to appear in a lane nobody pointed at.
  const refile = async (task: Task, intent: FilingIntent) => {
    setNote(null);
    try {
      if (intent.kind === "archive") {
        await archiveTask(task.key);
      } else {
        setNote(await performUnarchive(task.key));
      }
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

  // Opening a card: the conversation, and the unread cleared on the way out.
  //
  // Up here with triage and runNow because the read set lives here — performOpen
  // needs the local half, and this is where it is. Everything else about what
  // opening a thread means (mark local-first, ONE whole-task request, fire and
  // forget, navigate regardless) lives in performOpen, shared with the List row's
  // Open chat button so the same gesture cannot mean two things on two views.
  // What a card HOLDS is its listing window and nothing else — the Board has no
  // Show more, so heldMessages(task) is the whole of it. Passed explicitly all the
  // same: the shared performer used to read this off `task.messages` itself, which
  // is exactly why the List's expanded thread got marked three messages deep.
  const openCard = (task: Task, intent: OpenThreadIntent) => {
    performOpen(task, intent, { clearAll, restoreAll, settleAll }, heldMessages(task));
  };

  // Shared by expanded lane bodies AND collapsed rails, so a rolled-up lane —
  // an empty one, or one the reader closed — still catches the drop most cards
  // are allowed.
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

  // `nowCollapsed` is what the reader is looking at — the rule's answer or their
  // own earlier one — so the press always means "the other one of these two".
  // Recording the RESULT rather than a flip is what makes the store a record of
  // choices instead of a snapshot of the board.
  //
  // WHICH of the two stores it lands in is decided by the lane's contents, and
  // by nothing else. A press on a lane with cards is a preference and is written
  // down; a press on an empty one is a peek and stays in memory, so nothing a
  // reader does to an empty column can outlive the sitting. The peek is a
  // straight toggle because it is the only thing the empty case has: closing a
  // peeked lane is removing the peek, not recording "collapsed".
  const toggleLane = (key: BoardColumn, nowCollapsed: boolean) => {
    if ((byLane.get(key)?.length ?? 0) === 0) {
      setPeeked((cur) => {
        const next = new Set(cur);
        if (nowCollapsed) next.add(key);
        else next.delete(key);
        return next;
      });
      return;
    }
    setChoices((cur) => {
      const next = { ...cur, [key]: !nowCollapsed };
      try {
        localStorage.setItem(LANE_CHOICE_KEY, JSON.stringify(next));
      } catch {
        // best-effort; a full or blocked store never breaks the board
      }
      return next;
    });
  };

  const byLane = useMemo(() => groupByColumn(tasks), [tasks]);

  // A peek dies the moment the lane it was about stops being empty, so a column
  // that fills up and drains again comes back rolled up rather than wearing an
  // answer to a question the reader asked about a different, older emptiness.
  // `laneRolledUp` already ignores the peek while there are cards, so this only
  // has to clear it inside that window — which is exactly where it is safe to.
  useEffect(() => {
    setPeeked((cur) => {
      if (cur.size === 0) return cur;
      const next = new Set([...cur].filter((key) => (byLane.get(key)?.length ?? 0) === 0));
      return next.size === cur.size ? cur : next;
    });
  }, [byLane]);

  return (
    <>
      {note && <p className="schedule-tv-note">{note}</p>}
      <div className="schedule-tv-board">
        {BOARD_COLUMNS.map((col) => {
          const lane = byLane.get(col.key) ?? [];
          // How many CARDS in this column still hold something nobody has read —
          // the same fact the List's task rows carry, one level up. It matters
          // most on a COLLAPSED lane, which is a rail 52px wide showing nothing
          // but a ring, a word and a total: without this, a lane folded away
          // could fill with news and say nothing about it. Counted in tasks
          // rather than messages (tasks-lib.laneUnread) because the header stands
          // over cards.
          const news = laneUnread(lane, read);
          const rolled = laneRolledUp(col.key, lane.length, choices, peeked);
          if (rolled) {
            // An empty rail is STILL A BUTTON. It briefly was not — empty
            // outranked the reader's choice, so the press did nothing and the
            // control said so with `aria-disabled` — and that was the wrong
            // half of the complaint to fix (Akshil, 2026-08-18). Rolling up by
            // default is what keeps four empty columns off the board; refusing
            // to open is a lane telling the reader they may not look inside it.
            // An expanded empty lane shows an empty panel, which is a fine
            // answer to "is there anything in here", and it is a drop target
            // either way.
            //
            // `empty` survives for the one thing that WAS the complaint: the
            // count below.
            const empty = lane.length === 0;
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
                    : empty
                      ? `${col.label}: nothing yet`
                      : `${col.label}: ${lane.length}`
                }
                onClick={() => toggleLane(col.key, true)}
                {...dropProps(col.key)}
              >
                <StatusIcon status={col.key} unread={news > 0} count={news} />
                <span className="schedule-tv-rail-label">{col.label}</span>
                {/* No `0`. A count answers "how many are hidden in here", and on
                    an empty rail the honest answer is already the whole rail —
                    the chip only added a number to read before you could see it
                    said nothing (Akshil, screenshot, 2026-08-18). */}
                {!empty && (
                  <span className="schedule-tv-rail-count">{lane.length}</span>
                )}
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
                onClick={() => toggleLane(col.key, false)}
              >
                {/* The group header's own unread mark, the same ring the cards
                    under it wear — filled while any of them holds something
                    unread, and naming the number on hover. */}
                <StatusIcon status={col.key} unread={news > 0} count={news} />
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
                    showProject={showProject}
                    // The DISPLAYED count, so a card cleared by its own click
                    // stays cleared until the poll agrees — the same merge the
                    // List's rows make over the same set.
                    unread={taskUnread(task, read)}
                    isDragging={dragging?.key === task.key}
                    onDragStart={() => setDragging(task)}
                    onDragEnd={() => {
                      setDragging(null);
                      setOverLane(null);
                    }}
                    onFile={(intent) => refile(task, intent)}
                    onRun={runNow}
                    onOpen={(intent) => openCard(task, intent)}
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
  showProject,
  unread,
  isDragging,
  onDragStart,
  onDragEnd,
  onFile,
  onRun,
  onOpen,
}: {
  task: Task;
  home: string;
  /** Whether the folder chip is worth drawing — the BOARD's answer, for the same
   * reason the List row takes it as a prop (spansProjects). */
  showProject: boolean;
  /** What the mark stands for: the server's count less anything cleared here since,
   * which the board merges (taskUnread) rather than the card re-deriving. */
  unread: number;
  isDragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  /** File this card away, or bring it back out — the direction comes from the
   * card's own filingIntent, and the board owns the call so its refusal (and, for
   * an unarchive, the lane it landed in) lands in the board's own note line. */
  onFile: (intent: FilingIntent) => Promise<void>;
  /** Run the task's next message now, or re-send the one that failed. Same
   * arrangement and same reason as onTriage: the board makes the call. */
  onRun: (intent: TaskRunIntent) => Promise<void>;
  /** Open the conversation, marking the thread read on the way. The board owns
   * it because the board owns the read set — and it is only ever called with a
   * non-null intent, so this card cannot navigate to nowhere. */
  onOpen: (intent: OpenThreadIntent) => void;
}) {
  // Whether this card lifts at all, and it is not one question: a task with no
  // session (§5 — Claude Code mints the id on the first run) has nothing to
  // TRIAGE, while a task with no pending message has nothing to RUN. A card
  // that can do neither must not lift, rather than lift into a call that can
  // only fail. tasks-lib.dropLanes holds both halves.
  const draggable = isDraggable(task);
  // Where the click goes and whether it also clears the thread's unread — one
  // answer, from tasks-lib, and the SAME answer the List row's Open chat button
  // gets. Null means the card has nowhere to go (no session yet), and then the
  // click does nothing at all: no navigation, and no mark either, since nothing
  // was shown to the reader.
  const open = openThreadIntent(task, unread);
  // Filing without dragging, in whichever direction this card has. The drag stays
  // as the accelerator, but it cannot be the ONLY way: the Archive lane is
  // collapsed by default, so BOTH gestures otherwise start with "expand Archive
  // first" — and for a card already in there, a collapsed lane draws no card to
  // drag at all. Same predicate as the drops, by construction: filingIntent asks
  // dropAction.
  //
  // Both views draw the same button from the same intent, which is the thing this
  // page's vocabulary is most careful about: a Board that offered a return the
  // List did not draw is exactly the divergence the old SHOW_UNARCHIVE was.
  const file = filingIntent(task);
  // Run now / Re-run, which the List row and the calendar popover both already
  // offer and this card did not. The SAME function decides it here as there
  // (tasks-lib.taskRunIntent, which asks runNowIntent — the very function
  // dropAction asks), so the card's button, the List's button and the drag onto
  // In Progress can never fire different messages. Nothing about which message
  // or which call is re-derived on this side.
  //
  // NOT ON AN ARCHIVED CARD (showsRowActions) — same rule, same reason, as the
  // List row: the only decision left against a card in Archive is whether it is
  // still in Archive, so Unarchive is the only button it grows.
  const run = showsRowActions(task) ? taskRunIntent(task) : null;
  // The run still to come, when this card's lane does not already order by it.
  const soon = nextRunChip(task);
  // The lane this card is IN. Not passed down: `groupByColumn` files every card
  // by `taskColumn`, so asking it here is asking the same function that decided
  // which lane header the card is sitting under — a prop would be a second
  // opinion about a fact the board has already settled.
  const lane = taskColumn(task);
  // Whether the ring would SAY anything on this card. See the head below: the
  // ring is drawn only when it disagrees with the lane, and `isFailedTask` is the
  // one place that knows what "reads as failed" means (the failed lane, or the
  // flag that repaints a Done ring red). The lane check is what turns that into
  // "disagrees": in the failed lane the two agree and the header has said it.
  const failedOffLane = isFailedTask(task) && lane !== "failed";
  const [busy, setBusy] = useState(false);
  const refile = async (intent: FilingIntent) => {
    setBusy(true);
    try {
      await onFile(intent);
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
          if (open) onOpen(open);
        }}
      >
        {/* The head is the card's marks — the id, the live ping, and a status ring
            only when that ring has something to say.

            The ring earns its place on a card by DISAGREEING with the lane, and is
            silent when it would only repeat it. A card is never read outside the
            lane it was filed into, and that lane's header already carries the ring
            and the word ("◯ UPCOMING 9"), so on the common card — status and lane
            being the same fact — the ring next to the id was the column saying its
            own name a second time (Akshil, 2026-08-17: "that is just repetitive
            here").

            Which leaves the one card where they are NOT the same fact. `failed` is
            a flag beside `status`, not a value of it, and the two disagree in
            exactly one direction (server routers/tasks.py `_failed`): a broken run
            triaged to Done or Archive, or one whose session is live again, keeps
            the flag while the lane says something else. Those cards sit under a
            header that does not mention the failure, and the red ring is the only
            thing on them at rest that does — the hover-revealed Re-send is not a
            signal, it is a control. So the ring is conditional, not absent, and
            `failedOffLane` above is the whole rule.

            Nothing about the ring itself changes: same component, same
            `--status-failed` token, same 16px. And the head holds
            `--tasks-card-head-h` whether or not the ring is in it (tasks.css), so a
            card does not change height when it gains or loses one — a lane of cards
            that jittered by the width of a glyph would be a worse tell than the
            repetition this removed.

            List and Calendar keep their ring unconditionally: a row in a flat list
            and a chip in a day cell have no lane above them, so there the ring is
            the only thing that files them at all.

            AND UNREAD IS NOT IN THIS HEAD AT ALL. It took three tries to get there
            (all 2026-08-18): the ring's filled centre, which meant widening the
            condition above and putting the repetition straight back; then a small
            filled dot in the status hue leading the head, which stopped repeating
            the lane but spent a whole glyph — and a card is three short lines, so a
            fourth mark on it is the crowding again in a new place. It is the
            TITLE'S WEIGHT now: unread cards read bold, read cards read normal. See
            the title below.

            So the head is the id, and a ring on the one card whose status its lane
            does not mention. */}
        <span className="schedule-tv-card-head">
          {failedOffLane && <StatusIcon status={lane} failed />}
          <IdChip id={task.task_id} kind="task" />
        </span>
        {/* Nothing trails the title any more (2026-08-18). Three arrangements of
            an unread mark lived in this slot and each one was a fix for the last:
            a numeric pill inside the title's two-line `-webkit-box` clamp, which
            clipped it away on exactly the busiest cards; the same pill lifted into
            a `flex-wrap` wrapper beside the title, where any two-line title
            orphaned it onto a line of its own (the screenshot Akshil sent on
            2026-08-17); and then a dot back inside the flow once schedule.css
            dropped the clamp for good.

            The clamp's removal STAYS — the title wraps freely, `overflow-wrap:
            anywhere` is the only guard it needs, and long titles make taller cards,
            which is accepted. What went is the ATOM: no pill, no dot, nothing in
            the flow after the last word.

            UNREAD IS THE TITLE'S WEIGHT (Akshil, 2026-08-18, after three marks in
            this slot and one in the head). Bold when there is something unread,
            normal when there is not — no glyph, so the card gains no fourth thing
            to read, and the signal is on the very words a lane is scanned for. It
            is also the mark this page already uses one level down: an unread MESSAGE
            row bolds its body (`.tasks-msg.is-unread`), and a card is the same claim
            about a whole thread.

            IT STAYS OFF THE LIST ROW, deliberately. A row already carries the
            ring-dot and that is its primary signal; bolding the title as well would
            state one fact twice on one line, which is exactly the double-signalling
            the ring was introduced to end. The Board has no ring to spare on a
            quiet card — that is why it needs a different mark at all — so the two
            views differ HERE precisely so they agree about everything else.

            AND WEIGHT IS NOT A FACT A SCREEN READER HAS (bugbot, PR #596). Bold is
            the whole visual signal here, and `font-weight` reaches the
            accessibility tree not at all — so an unread card and a read one were
            the same card to anybody not looking at it. The words are added instead,
            in a span that is hidden from the eye and not from the tree
            (`.tasks-said`): the card is a `<button>` whose accessible name is
            computed from its contents, so ", 3 unread" after the title lands in
            that name in the right order, with no `aria-label` overriding the id and
            title a reader actually wants to hear first.

            Deliberately NOT `aria-label` on the button: that REPLACES the computed
            name, so the card would announce its unread count and lose "TASK-044,
            Pull today's news" — trading one missing fact for two. And deliberately
            not an `aria-label` on this span either: a role-less span's label is not
            reliably announced (the same reason ScheduleCalendar gives its own dot a
            `role="img"`), where real text always is. */}
        <span className={"schedule-tv-card-title" + (unread > 0 ? " is-unread" : "")}>
          {firstLine(task.title) || "(untitled)"}
          {unread > 0 && (
            <span className="tasks-said">{`, ${taskUnreadLabel(unread)}`}</span>
          )}
        </span>
        {/* The foot is the folder and the run ahead, so when neither says
            anything (spansProjects — every card in a board filtered to one
            project repeats it — and a card with no run coming) the whole line
            goes rather than leaving an empty row of padding. */}
        {(showProject || soon) && (
          <span className="schedule-tv-card-foot">
            {showProject && (
              <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />
            )}
            {/* Same fact, same function, same words as the List row's
                (tasks-lib.nextRunChip): a settled card whose task is due again
                would otherwise show nothing about the run ahead. */}
            {soon && (
              <span className="tasks-row-next" title={soon.title}>
                {soon.text}
              </span>
            )}
          </span>
        )}
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
          the row instead of by its own coordinates.

          They are SIBLINGS of the card, not children of it, which is also what
          keeps them out of the card's own click: pressing Archive or Run now
          cannot bubble into a button it is not inside, so neither one navigates
          to the conversation or marks the thread read. That was already true of
          the markup and it is now load-bearing, so a test reads it.

          RUN NOW IS BEHIND SHOW_ROW_ACTIONS, off since 2026-08-17. ARCHIVE IS
          NOT, since 2026-08-18: Akshil asked for the archive button back on hover,
          and it is the same button on the other view, so keeping it flagged here
          while the List shows it is exactly the divergence the shared flag exists
          to prevent (§1 — same element, same behaviour in every view). The strip
          itself is drawn whenever either survives its guard. */}
      {(file || (SHOW_ROW_ACTIONS && run)) && (
        <span className="tasks-card-acts">
          {SHOW_ROW_ACTIONS && run && (
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
              className={"tasks-act tasks-card-act tasks-act--" + file.kind}
              title={file.title}
              aria-label={`${file.label} ${task.task_id}`}
              disabled={busy}
              onClick={() => void refile(file)}
            >
              {file.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
            </button>
          )}
        </span>
      )}
    </div>
  );
}
