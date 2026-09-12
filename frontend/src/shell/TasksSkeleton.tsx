// The Tasks page's loading state, one ghost per VIEW.
//
// Before this (2026-09-09) every view waited behind the same eight ragged
// bars, which told the reader "something is coming" and nothing about what —
// and then the List, or six cards, or a week grid, snapped into a space shaped
// nothing like the bars. A ghost that is shaped like the view it stands in for
// does two things the bars could not: the page has its final geometry from the
// first paint, and the reader knows which view they are on before a row lands
// (Akshil: "skeleton loader should be different per view and closer to the UI").
//
// Each ghost REUSES the real view's container classes (`.tasks-list-frame`,
// `.schedule-tv-board`/`-lane`, `.task-cards`, `.schedule-cal-*`) so the
// dimensions come from the same CSS the rows will use — a change to a row's
// padding changes the ghost's too, and the swap moves nothing. Only the bars
// are new, and they are the shell's own `.skel-bar` shimmer (platform/ui/
// Skeleton), so this adds no colour and no motion of its own.
//
// One `role="status"` wrapper carries the label; the bars inside are decoration
// and are hidden from the accessibility tree.
import type React from "react";
import { BOARD_LANES, initialRange } from "./schedule-lib";
import type { TaskView } from "./tasks-lib";

/** Title bar widths, as a percentage of the row, cycled. Ragged on purpose: a
 *  column of equal bars reads as a table header, not as titles. */
const TITLE_WIDTHS = [34, 52, 41, 60, 38, 47, 55, 30];
const LIST_ROWS = 8;
/** Cards per board lane, in `BOARD_LANES` order — the five the board DRAWS
 *  (Needs attention shares Blocked's lane and Queued shares In Progress's,
 *  schedule-lib.laneOf), not the seven statuses; a ghost column the real board
 *  does not draw is a layout shift on the swap (Bugbot, #1079). Never evenly full, like a real board. A count of 0 draws the lane
 *  ROLLED UP into its 52px rail, which is what the real board does with an
 *  empty column (TaskBoard `laneRolledUp`): Blocked is empty on most days. */
const LANE_CARDS = [2, 3, 0, 3, 2];
const CARD_TILES = 6;
const CAL_ROWS = 8;
/** The calendar's own memory of its range (ScheduleCalendar RANGE_KEY), read
 *  the same way it reads it — through schedule-lib.initialRange, which is also
 *  what decides the default for a reader who never chose — so the ghost has the
 *  day count the grid will. */
const CAL_RANGE_KEY = "fused-render:scheduled-cal-range";

function calendarDays(): number {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(CAL_RANGE_KEY);
  } catch {
    stored = null;
  }
  return initialRange(stored) === "4day" ? 4 : 7;
}
/** Where a chip ghost sits per day column: [row offset, rows tall]; a day
 *  with none is a quiet day. Index = day column. */
const CAL_CHIPS: ([number, number] | null)[] = [
  [1, 1], [3, 2], null, [2, 1], [5, 1], null, [4, 2],
];

/** What every ghost's root carries: the view's own container class first, so
 *  the CSS that keys on `.schedule-main > .task-cards-scroll` (task-cards.css)
 *  or `.schedule-page:has(> .schedule-main > …)` sees the ghost exactly where it
 *  sees the view — a wrapper in between broke those selectors, and the Cards
 *  ghost drew three columns where the wall would draw one. */
function ghost(view: TaskView, base: string) {
  return {
    className: `${base} tasks-skel tasks-skel--${view}`,
    role: "status",
    "aria-busy": true,
    "aria-label": "Loading tasks",
  } as const;
}

function Bar({ w, className = "" }: { w: number | string; className?: string }) {
  return (
    <span
      className={`skel-bar ${className}`.trim()}
      style={{ width: typeof w === "number" ? `${w}px` : w }}
    />
  );
}

/** The 12px status ring, as a round bar. */
function Ring() {
  return <span className="skel-bar tasks-skel-ring" />;
}

function ListGhost() {
  return (
    <div {...ghost("list", "tasks-list")}>
      <div className="tasks-list-frame" aria-hidden="true">
        {Array.from({ length: LIST_ROWS }, (_, i) => (
          <div key={i} className="tasks-node">
            <div className="tasks-row tasks-skel-row">
              <span className="tasks-caret tasks-skel-caret" />
              <span className="tasks-rowmark tasks-skel-rowmark">
                <Ring />
              </span>
              <Bar w={56} className="tasks-skel-id" />
              <Bar w={`${TITLE_WIDTHS[i % TITLE_WIDTHS.length]}%`} className="tasks-skel-title" />
              <span className="tasks-grow" />
              <Bar w={28} />
              <Bar w={44} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function BoardGhost() {
  return (
    <div {...ghost("board", "schedule-tv-board")}>
      {/* Every lane and rail is decoration; the root's label says it all. */}
      {BOARD_LANES.map((col, laneIx) => {
        const cards = LANE_CARDS[laneIx % LANE_CARDS.length];
        if (cards === 0) {
          return (
            <div key={col.key} className="schedule-tv-rail tasks-skel-rail" aria-hidden="true">
              <Ring />
              <span className="skel-bar tasks-skel-rail-label" />
            </div>
          );
        }
        return (
        <div key={col.key} className="schedule-tv-lane" aria-hidden="true">
          <div className="schedule-tv-lane-head tasks-skel-lane-head">
            <Ring />
            <Bar w={72} />
            <span className="tasks-grow" />
            <Bar w={16} />
          </div>
          <div className="schedule-tv-lane-body">
            {Array.from({ length: cards }, (_, i) => (
              <div key={i} className="schedule-tv-card tasks-skel-card">
                <div className="schedule-tv-card-head">
                  <Bar w={48} />
                  <span className="tasks-grow" />
                  <Ring />
                </div>
                <Bar w="90%" />
                <Bar w={`${TITLE_WIDTHS[(laneIx + i) % TITLE_WIDTHS.length] + 10}%`} />
                <div className="schedule-tv-card-foot">
                  <Bar w={40} />
                  <Bar w={40} />
                </div>
              </div>
            ))}
          </div>
        </div>
        );
      })}
    </div>
  );
}

function CardsGhost() {
  return (
    <div {...ghost("cards", "task-cards-scroll")}>
      <div className="task-cards" aria-hidden="true">
        {Array.from({ length: CARD_TILES }, (_, i) => (
          <div key={i} className="tasks-skel-tile">
            <div className="tasks-skel-tile-head">
              <Ring />
              <Bar w={`${TITLE_WIDTHS[i % TITLE_WIDTHS.length] + 15}%`} />
            </div>
            <div className="skel-bar tasks-skel-tile-body" />
          </div>
        ))}
      </div>
    </div>
  );
}

function CalendarGhost() {
  const days = calendarDays();
  const cols = { ["--cal-days" as string]: days } as React.CSSProperties;
  return (
    <div
      {...ghost("calendar", "schedule-cal tasks-skel-cal" + (days === 4 ? " is-wide" : ""))}
      style={cols}
    >
      {/* The calendar's own bar — range nav on the left, view range on the
          right — so the head below sits where the real head will. */}
      <div className="schedule-cal-bar tasks-skel-cal-bar" aria-hidden="true">
        <Bar w={24} className="tasks-skel-cal-btn" />
        <Bar w={24} className="tasks-skel-cal-btn" />
        <Bar w={140} />
        <span className="tasks-grow" />
        <Bar w={92} className="tasks-skel-cal-btn" />
      </div>
      <div className="schedule-cal-head" aria-hidden="true">
        <div className="schedule-cal-gutter" />
        {Array.from({ length: days }, (_, d) => (
          <div key={d} className="schedule-cal-day-head">
            <Bar w={24} />
            <Bar w={18} className="tasks-skel-cal-num" />
          </div>
        ))}
      </div>
      <div className="schedule-cal-scroll" aria-hidden="true">
        <div className="schedule-cal-grid tasks-skel-cal-grid">
          <div className="tasks-skel-cal-gutter">
            {Array.from({ length: CAL_ROWS }, (_, r) => (
              <Bar key={r} w={28} />
            ))}
          </div>
          {Array.from({ length: days }, (_, d) => {
            const chip = CAL_CHIPS[d];
            return (
              <div key={d} className="schedule-cal-col tasks-skel-cal-col">
                {chip && (
                  <span
                    className="skel-bar tasks-skel-cal-chip"
                    style={{ gridRow: `${chip[0] + 1} / span ${chip[1]}` }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function TasksSkeleton({ view }: { view: TaskView }) {
  if (view === "board") return <BoardGhost />;
  if (view === "cards") return <CardsGhost />;
  if (view === "calendar") return <CalendarGhost />;
  return <ListGhost />;
}

export default TasksSkeleton;
