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
  carryMarkToHeld,
  dropAction,
  dropLanes,
  filterTasks,
  firstLine,
  groupByColumn,
  heldMessages,
  isDraggable,
  isExpandable,
  isFailedTask,
  isUpcomingTask,
  laneUnread,
  markAllRead,
  markRead,
  markReadIntent,
  messageEditEntry,
  messageHref,
  messageTone,
  messageWhenTitle,
  openThreadIntent,
  projectOptions,
  relativeWhen,
  settleMarkAllRead,
  soleMessage,
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
  ArchiveStatus,
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
const ICON_CHEVRON_DOWN = icon(<polyline points="6 9 12 15 18 9" />, 12);
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
  return (
    <span
      className={
        `schedule-ring schedule-ring--${status}` +
        (failed ? " schedule-ring--failed" : "") +
        (unread ? " schedule-ring--unread" : "")
      }
      aria-label={many ? `${text}, ${many}` : text}
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
          showProject={showProject}
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
  // The one message a LEAF row is about, and therefore what its click opens —
  // see `activate` below. Null on a task that has never run, which is the case
  // that must stay inert. tasks-lib.soleMessage holds both halves of that.
  const sole = soleMessage(task, held);
  // The scheduled run a ONE-MESSAGE UPCOMING row's press edits, ahead of opening
  // that message, because the instruction that has not run yet is the only content
  // such a row has. tasks-lib.upcomingEditEntry owns all three conditions — the
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
  const seen = markReadIntent(task, read, held);

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
    if (to) navigateUrl(to);
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
   * cannot drift apart (Enter and Space run exactly this).
   *
   * Four rows, four meanings, and the split is the row's own shape:
   *
   *   * an ACCORDION (more than one message) toggles, and opens nothing. That is
   *     unchanged and deliberately so: expanding a row shows the reader nothing,
   *     so a press that also cleared its unread would clear news nobody has seen.
   *     Opening the conversation stays the explicit Open chat action's job. It is
   *     FIRST, so a repeating task with past runs keeps its accordion whatever its
   *     lane — the chevron never has to become a control of its own, and one click
   *     can never both expand a row and open a form.
   *   * an UPCOMING LEAF (exactly one message, and it has not run) opens THE EDIT
   *     FORM on that scheduled run, ahead of opening the message, because the
   *     instruction is the only content such a row has (Akshil, 2026-08-17: "when i
   *     click on upcoming tasks i think they should open up the edit modal... only
   *     for 1 message tasks"). tasks-lib.upcomingEditEntry holds every condition;
   *     the form is reached through `onEditEntry`, the same callback the thread's
   *     own Edit button and the calendar popover spend, so there is no second way
   *     in. Null when the entry cannot be resolved, and then the press falls
   *     through to the arm below rather than opening a blank form.
   *   * any other LEAF (exactly one message) opens THAT MESSAGE, through
   *     openMessage above — the identical call a click on the message row makes.
   *     The row with nothing to expand IS that message, so "open it" is the only
   *     thing its press can mean, and it was doing nothing at all until now
   *     (Akshil, 2026-08-17). The url is not recomputed here; messageHref stays the
   *     one place a message's address is built.
   *   * a row with NO message but a SESSION opens the thread, through openChat
   *     above — the same intent (openThreadIntent) and the same performer the now
   *     hidden Open chat button spends, so there is still exactly one way to
   *     address a thread. It is the minority of these rows and it is real: a
   *     hand-written fixture transcript, or a session whose only user records were
   *     slash-command envelopes (`/clear`, `/making-a-release`), which still has
   *     assistant turns worth reading. It fell through both arms above and did
   *     nothing at all, on a row that looked pressable — the complaint (Akshil,
   *     2026-08-17: "the (untitled) aren't clickable").
   *
   * WHICH MEANS THE LEAF PRESS CLEARS THAT MESSAGE'S UNREAD, and that is the
   * point rather than an exception to the rule above: the reader is being shown
   * the message, and it is the same one press on the same message through the same
   * function, so a dot surviving it would be a dot the click did not honour. What
   * it does NOT do is mark the whole task — openMessage marks one message, which
   * on a one-message task happens to be all of it and on nothing else ever will
   * be.
   *
   * THE ZERO-MESSAGE PRESS MARKS NOTHING, and not as a special case either: there
   * is no message to mark, so `unread` is 0 (taskUnread: with the whole thread in
   * hand — all none of it — the count IS the dots, counted), and the intel it was
   * asked with therefore carries markRead: false. performOpen's mark is behind
   * that flag, so this arm navigates and writes nothing.
   *
   * A task with no session at all (a `pending:<entry>` that has never run) has no
   * `sole` AND no `chat`, so the press still does nothing — openThreadIntent's
   * documented null case, there being no conversation to open. Such a row does not
   * ADVERTISE a press either: see `pressable` below.
   *
   * NOTHING IS MARKED READ ON THE EDIT ARM: the message it opens the form for has
   * not gone out, so there is nothing there to have seen.
   */
  const activate = () => {
    if (expandable) onToggle();
    else if (edit) onEditEntry?.(edit);
    else if (sole) openMessage(sole);
    else if (chat) openChat(chat);
  };

  /**
   * Does this row's press DO anything — and therefore, may the row claim to be a
   * button at all?
   *
   * The arms of `activate`, so the affordance cannot drift from the behaviour: the
   * row is a button when it has a disclosure, a message to open, or a thread to
   * open. The edit arm is deliberately absent, and that is not a gap — it is a
   * NARROWING of the leaf arm (upcomingEditEntry requires soleMessage), so it can
   * never make a row pressable that `sole` did not already.
   *
   * What is left is the never-run `pending:<entry>` row with nothing in hand, which
   * carried `role="button"`, a tab stop, a hover tint and a pointer cursor while
   * doing nothing on press — the same broken promise the zero-message arm above
   * just fixed, one layer down. So an inert row is inert in what it says too.
   */
  const pressable = expandable || sole !== null || chat !== null;

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
        className={"tasks-row" + (open ? " is-open" : "") + (pressable ? "" : " is-inert")}
        // Only a row whose press DOES something says it is a button, takes a tab
        // stop, or lights up under the pointer (`is-inert` above turns the cursor
        // and the hover tint off). `undefined` rather than "presentation"/-1: the
        // row is still a real, readable line of the list — it just is not a
        // control. See `pressable`.
        role={pressable ? "button" : undefined}
        tabIndex={pressable ? 0 : undefined}
        // Only a row that HAS a disclosure claims one. `undefined` rather than
        // `false`: false says "collapsed, press to expand", which is a promise a
        // one-message row cannot keep. It is still the ROW's own, and stays that
        // way because the accordion is `activate`'s first arm — no lane takes the
        // toggle off a multi-message row, so the chevron never has to become a
        // control of its own.
        aria-expanded={expandable ? open : undefined}
        title={task.title}
        // One handler for both ways in, so the keyboard and the pointer cannot
        // mean different things: `activate` above toggles an accordion, opens an
        // upcoming leaf's edit form, opens any other leaf's single message, and
        // opens a message-less row's thread. It still never opens a MULTI-message
        // task's conversation — that is the Open chat action's job, and the reason
        // is written where the meanings are decided.
        onClick={activate}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            activate();
          }
        }}
      >
        {/* The disclosure gutter is drawn WHETHER OR NOT there is a chevron in it.
            `--tasks-caret-w` is the first term of `--tasks-rail-x`, which every
            indent on this page is measured from (tasks.css), so dropping the
            element on a one-message row would slide that row's status ring — and
            the whole rail it stands in — a mark and a gap to the left of its
            neighbours' and turn a column of rings into a zigzag. So the box stays
            and only the glyph goes.

            IT IS DECORATION AND STAYS DECORATION — no role, no tab stop, no handler
            of its own — because the accordion is `activate`'s FIRST arm: every
            expandable row toggles on its own press whatever its lane, so a click on
            the chevron bubbles to the row and toggles exactly once. There is no
            second press here to double-fire, and nothing to stopPropagation. */}
        <span className={"tasks-caret" + (open ? " is-open" : "")} aria-hidden>
          {expandable ? ICON_CHEVRON : null}
        </span>
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
        <StatusIcon
          status={taskColumn(task)}
          failed={task.failed}
          unread={unread > 0}
          count={unread}
        />
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

        {/* THE STRIP IS BEHIND SHOW_ROW_ACTIONS — with ONE exception, Archive,
            which came back on 2026-08-18 (Akshil). Each button carries its own
            guard rather than the group carrying one, so the four keep their
            hard-won ORDER whichever of them are rendered: pulling Archive out into
            a block of its own beside the flagged fragment would file it before Mark
            read and Run now the day the flag flips, and the strip's order is read
            left-to-right as "clear it, run it, file it, open it". */}
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
        {/* The Board's drag onto Archive, as a press — and on an already
            archived row, the way back, because an action with only one
            direction is a trap. tasks-lib.archiveIntent decides both halves.

            THE ONE ROW ACTION NOT BEHIND THE FLAG (Akshil, 2026-08-18: bring the
            archive button back, visible on hover). Filing a task away had no press
            anywhere in the List while the strip was off — the only route was
            switching to the Board, expanding the Archive lane and dragging — which
            made the honest answer to "can a task be deleted?" (no: it is archived)
            barely true on this view.

            HOVER-REVEALED, not permanent: a list at rest must grow no chrome
            (§2 — only critical actions get visible buttons), and this is one
            button on every row that has ever run. `.tasks-act` in tasks.css owns
            that, and it does it with `opacity` plus a `:focus-visible` arm rather
            than `visibility`/`display`, so the button stays in the tab order and
            lights up for a keyboard that lands on it. It is still not rendered at
            all on a task with nothing to file, which is the difference that
            matters: hidden-until-hover is for a live control, not for a dead
            one. */}
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
        {when && (
          <span className="tasks-row-time" title={when.title}>
            {when.text}
          </span>
        )}
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
            // The one entry this message can be edited as, or null when its press
            // means the transcript instead — ONE reading, spent by both the row's
            // press (pressMessage) and the pencil below, so the quiet action and
            // the whole-row gesture cannot disagree about which rows are editable.
            const fix = onEditEntry ? messageEditEntry(m) : null;
            const busy = cancelling === m.message_id;
            const why = cancelErrors[m.message_id];
            return (
              <Fragment key={m.message_id}>
                <div
                  className={"tasks-msg" + (isNew ? " is-unread" : "")}
                  role="button"
                  tabIndex={0}
                  title={m.body}
                  // One handler for both ways in, the same rule the task row obeys:
                  // `pressMessage` opens the form on a message that has not gone out
                  // and the transcript turn on one that has.
                  onClick={() => pressMessage(m)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      pressMessage(m);
                    }
                  }}
                >
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
  /** Already filtered, in the SERVER's order — the LANES re-order it
   * (tasks-lib.groupByColumn), which is the one thing this view does to the
   * order it is handed and the one place it is decided. */
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
          // How many CARDS in this column still hold something nobody has read —
          // the same fact the List's task rows carry, one level up. It matters
          // most on a COLLAPSED lane, which is a rail 52px wide showing nothing
          // but a ring, a word and a total: without this, a lane folded away
          // could fill with news and say nothing about it. Counted in tasks
          // rather than messages (tasks-lib.laneUnread) because the header stands
          // over cards.
          const news = laneUnread(lane, read);
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
                <StatusIcon status={col.key} unread={news > 0} count={news} />
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
                    onTriage={(status) => triage(task, status)}
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
  onTriage,
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
  /** File this card away, or bring it back — the board owns the call so its
   * refusal lands in the board's own note line. */
  onTriage: (status: ArchiveStatus) => Promise<void>;
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

            UNREAD DOES NOT BRING THE RING BACK. It did for half a day: the ring
            became the page's unread mark, a quiet Done card has no ring, so the
            condition was widened to `failedOffLane || unread > 0` and every card
            with news grew a ring that repeated its lane's word to say something
            else. Akshil, 2026-08-18: the repetition is the thing being removed, so
            the card gets a mark of its OWN for unread — a small filled dot in the
            status hue, leading the head, before the id. Read cards show nothing.

            Same hue as the ring would have been, so the vocabulary is intact; a
            different SHAPE, because it is a different claim. A ring on this page
            means "here is the status" and the lane header has already said that;
            a bare dot means "there is something in here you have not seen", which
            the header cannot say about any one card. The List and the Calendar
            keep the ring as their unread mark because their rows carry a ring
            anyway — nothing is repeated there, and adding a second mark to those
            rows is exactly what this whole change undid. */}
        <span className="schedule-tv-card-head">
          {unread > 0 && (
            <span
              className={
                `tasks-news tasks-news--${lane}` + (failedOffLane ? " tasks-news--failed" : "")
              }
              role="img"
              aria-label={taskUnreadLabel(unread) ?? ""}
              data-tip={taskUnreadLabel(unread) ?? ""}
              title=""
            />
          )}
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
            which is accepted. What went is the mark: the ring in the head above
            carries read-state on all three views now, so this is a title and only
            a title. */}
        <span className="schedule-tv-card-title">
          {firstLine(task.title) || "(untitled)"}
        </span>
        {/* The foot is the folder and nothing else, so when the folder says nothing
            (spansProjects — every card in a board filtered to one project repeats
            it) the whole line goes rather than an empty row of padding. */}
        {showProject && (
          <span className="schedule-tv-card-foot">
            <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />
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
