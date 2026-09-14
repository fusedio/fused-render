// THE TASK SIDE PEEK's state — which task is open, how wide the panel is,
// whether WE were the ones who collapsed the sidebar to make room, and the
// `?peek=` param that mirrors all of it into the URL.
//
// A module store rather than React state, for the same reason
// `platform/lib/sidebarstate` is one: the peek is opened from four different
// views (the List row's press, the Board card's, the Cards wall's, the
// calendar popover's) and read by a fifth component that is not any of their
// parents. Threading a callback down four trees to set one id is how the four
// views start disagreeing about what "open" means.
//
// EVERYTHING THAT CAN BE PURE IS PURE and lives at the top of this file —
// the width clamp, the URL codec, the make-room arithmetic, the prev/next
// step. Those are the rules worth testing (task-peek-store.test.ts) and the
// ones a DOM makes untestable; the store below is the thin layer that reads
// the window, writes history and pokes the sidebar.
import { useSyncExternalStore } from "react";
import { forgetAppsCache, resetPreviewHeight } from "./peek-preview";
import {
  SIDEBAR_RAIL_WIDTH,
  getSidebarState,
  setSidebarState,
  subscribeSidebarState,
} from "@platform/lib/sidebarstate";

/** The query param the peek rides on `/tasks`. */
export const PEEK_PARAM = "peek";

/**
 * THE ATTRIBUTE EVERY OPENABLE ITEM IN EVERY VIEW STAMPS ON ITSELF, carrying
 * that item's `Task.key`.
 *
 * It is two things at once, and that is the point: the halo's selector, and —
 * read in DOM order inside the frame — the prev/next walk. "The current view's
 * visible order" is then whatever the view actually PAINTED (list order; the
 * board column by column; the cards grid; the calendar chronologically) rather
 * than a fifth copy of each view's sort kept in step by hand.
 */
export const PEEK_ITEM_ATTR = "data-peek-key";

/** The class the open item wears. One rule for all four shapes, drawn inside
 *  the element's own box so nothing changes size (styles/task-peek.css). */
export const PEEK_OPEN_CLASS = "is-peeked";

/**
 * WHERE THE "WE COLLAPSED IT" MARKER LIVES, and why it lives anywhere at all.
 *
 * The flag is the difference between putting a sidebar back and overruling a
 * reader who shut it themselves (design.md, Make-room rule 3). Kept only in
 * module memory it did not survive a RELOAD — and reloading with `?peek=` in
 * the URL is an ordinary thing to do: the peek came back, the sidebar came back
 * collapsed (it is in localStorage, see `setSidebarState` below), and the flag
 * did not — so closing the panel left the sidebar shut for good, with no
 * gesture anywhere that would have shut it.
 *
 * sessionStorage rather than localStorage, because the claim is about THIS
 * SITTING: a tab that is gone took its peek with it, and a marker outliving it
 * would re-expand a sidebar on some later visit for a panel nobody remembers.
 */
export const PEEK_AUTOCOLLAPSE_KEY = "tasks.peek.autocollapsed";

/** localStorage key for the dragged width (design.md, Width & resize). Absent
 *  means "no choice yet", which is a real state: the default is a FRACTION of
 *  whatever the content area happens to be, so a stored number and an unset one
 *  behave differently on every window but the one it was dragged in. */
export const PEEK_WIDTH_KEY = "tasks.peek.width";

/**
 * THE PEEK'S MINIMUM, and it is the Explorer Claude pane's (220px,
 * `.listing-pane-slot` in styles/explorer.css) rather than a number of this
 * panel's own — the two are the same chat in the same shell, and a reader who
 * has dragged one down to a column of message bubbles expects the other to go
 * there too (design.md, Widths v2).
 *
 * 564 until 2026-09-14, with a ⅔ ceiling above it. Both are gone: the ceiling
 * because the middle pane now has a floor of its own to stop at (below), and
 * the 564 because a panel that cannot be made narrow is a panel you close
 * instead of narrowing.
 *
 * There is NO MAXIMUM. The peek may take everything the middle pane's own floor
 * and the cover threshold below have not claimed.
 */
export const PEEK_MIN_WIDTH = 220;

/**
 * WHAT SHARE OF ITS BASELINE THE MIDDLE PANE KEEPS BEFORE IT STOPS SHRINKING —
 * three quarters, and past that the content keeps its size and the pane scrolls
 * sideways instead (design.md, Widths v2).
 *
 * `baseline` is measured, not configured: the width the content column had the
 * moment the peek first opened in this visit. A fraction of THAT rather than a
 * constant, because the column is capped at 1050 and is whatever the window
 * gives it below that — one constant could only ever be right on one window.
 */
export const MIDDLE_FLOOR_FRACTION = 0.75;

/** Below this the middle pane is not worth keeping at all: the peek covers the
 *  content area instead of squeezing the view to a sliver (design.md, Widths
 *  v2 — the one number carried over from the old make-room rules). */
export const PEEK_COVER_FLOOR = 360;

/**
 * THE BAND AROUND THE FLOOR THAT STOPS THE SIDEBAR FLAPPING.
 *
 * The collapse fires at `middle < floor` and the expand at
 * `middle ≥ floor + 16`, both measured with the sidebar EXPANDED — so the two
 * can never be true of the same pixel, and a seam parked exactly on the floor
 * cannot tick the sidebar open and shut as the pointer jitters.
 */
export const SIDEBAR_HYSTERESIS = 16;

/** The page column's cap (`styles/schedule.css` `.prefs-page.schedule-page > *`)
 *  and the page's own side padding, used ONLY as the baseline's fallback when
 *  there is nothing on screen to measure (a test, a first paint). Measured
 *  beats assumed everywhere else. */
export const TASKS_COLUMN_MAX = 1050;
export const TASKS_PAGE_GUTTER = 44;

/** How far one arrow press moves the seam (design.md, Width & resize). */
export const PEEK_KEY_STEP = 10;

// ---- pure: width -------------------------------------------------------------

/** The baseline to reason with: the measured one, or the widest column this
 *  page can draw when nothing has been measured yet. */
export function baselineOr(baseline: number | null, content: number): number {
  if (baseline !== null && Number.isFinite(baseline) && baseline > 0) return baseline;
  return Math.max(0, Math.min(content, TASKS_COLUMN_MAX + TASKS_PAGE_GUTTER));
}

/** THE MIDDLE PANE'S FLOOR: three quarters of the baseline. Below it the rows,
 *  lanes, cards and calendar grid stop shrinking and the pane scrolls. */
export function middleFloor(baseline: number): number {
  return Math.round(Math.max(0, baseline) * MIDDLE_FLOOR_FRACTION);
}

/**
 * WHAT AN UNDRAGGED PEEK OPENS AT: whatever the content area has that the
 * middle pane's baseline does not need.
 *
 * That is the whole of "the first open keeps the column exactly where it was"
 * (design.md, Widths v2) — the peek is the REMAINDER, so the column is left at
 * the width it was already being read at rather than being halved by a panel
 * that has no opinion about it. On a window with no remainder to speak of the
 * peek takes its minimum and the middle pane gives way, down to its own floor
 * and then into an overflow.
 */
export function defaultPeekWidth(content: number, baseline: number | null): number {
  return Math.max(PEEK_MIN_WIDTH, Math.round(content - baselineOr(baseline, content)));
}

/**
 * A width, clamped into `[220, content]`.
 *
 * NO CEILING BUT THE CONTENT AREA ITSELF (design.md, Widths v2): what stops a
 * drag from swallowing the page is the cover threshold, which is a different
 * STATE rather than a smaller number — the middle pane is hidden and the peek
 * is honestly the whole area, instead of a 12px stripe of list pretending to
 * still be a view.
 *
 * Never returning NaN is the other half of the contract — a persisted width
 * from a broken write must not become an inline `width: NaNpx`.
 */
export function clampPeekWidth(width: number, content: number): number {
  const wanted = Number.isFinite(width) ? width : PEEK_MIN_WIDTH;
  const ceiling = Math.max(Math.floor(content), PEEK_MIN_WIDTH);
  return Math.round(Math.max(PEEK_MIN_WIDTH, Math.min(wanted, ceiling)));
}

/** The rendered width for a content area: the reader's own number if they have
 *  dragged one, else the remainder past the baseline — clamped either way,
 *  which is what "re-clamp the persisted width on resize" means in practice. */
export function peekWidthFor(
  chosen: number | null,
  content: number,
  baseline: number | null,
): number {
  return clampPeekWidth(chosen ?? defaultPeekWidth(content, baseline), content);
}

// ---- pure: the URL -----------------------------------------------------------

/** The task key a `/tasks` URL asks for, or null. Takes the search string so a
 *  deep link can be read before anything has mounted. */
export function readPeekParam(search: string): string | null {
  const value = new URLSearchParams(search).get(PEEK_PARAM);
  return value ? value : null;
}

/**
 * `search` with the peek param set to `key` (or removed for null), every other
 * param left exactly where it was — the view toggle and the filters live in the
 * URL too, and opening a task must not drop the lens the reader set.
 *
 * Returns a leading "?" or "" so the result can be concatenated onto a pathname.
 */
export function peekSearch(search: string, key: string | null): string {
  const params = new URLSearchParams(search);
  if (key) params.set(PEEK_PARAM, key);
  else params.delete(PEEK_PARAM);
  const text = params.toString();
  return text ? `?${text}` : "";
}

// ---- pure: which task the URL is naming --------------------------------------

/** What `resolvePeekKey` needs of a task: its row identity and its number. */
export interface PeekNamed {
  key: string;
  task_id?: string;
}

/**
 * The task a `?peek=` value names, or null when it names none.
 *
 * TWO SPELLINGS, because a URL is written by people as well as by this store.
 * The canonical one is `Task.key` — the server's row identity, which is what
 * every press writes. But a person reading the page sees `TASK-137`, and a
 * hand-typed or pasted `?peek=TASK-137` should land on that task rather than on
 * an empty panel. So a value that is not a key is tried as a task NUMBER, and
 * only an UNAMBIGUOUS match counts: a number is unique per project, not per
 * machine (tasks-lib.cardKey has the incident), so two projects can both hold a
 * TASK-007 and guessing between them would open the wrong conversation.
 *
 * Null is the important answer and the one the caller must act on: a key that
 * matches nothing is not "a peek that has not loaded yet", it is a peek that
 * cannot open, and the panel must not be left standing empty over it.
 */
export function resolvePeekKey(
  wanted: string | null,
  tasks: readonly PeekNamed[],
): string | null {
  if (!wanted) return null;
  if (tasks.some((t) => t.key === wanted)) return wanted;
  const want = wanted.toLowerCase();
  const numbered = tasks.filter((t) => (t.task_id ?? "").toLowerCase() === want);
  return numbered.length === 1 ? (numbered[0]?.key ?? null) : null;
}

// ---- pure: what a click on the frame means -----------------------------------

/**
 * EVERYTHING ON THE FRAME THAT IS NOT BACKGROUND.
 *
 * Clicking blank frame closes the peek (design.md, Close triggers — Akshil's
 * decision to keep Notion's behaviour), and the whole of that rule is knowing
 * what "blank" is not. Anything that is a control does its own thing: real
 * buttons and links, fields, anything wearing a button role, the four views'
 * items (which carry the walk's own attribute), the toolbar, and a menu or a
 * dialog portalled over the page.
 *
 * One string so the list is stated once and can be read by the test that pins
 * it, rather than being buried in a JSX handler where a fifth view's item could
 * quietly stop being covered.
 */
export const PEEK_FRAME_KEEPS_OPEN =
  'button, a, input, select, textarea, [role="button"], ' +
  `[${PEEK_ITEM_ATTR}], .schedule-toolbar, .modal-dialog, .context-menu`;

/** Does a click that landed on `hit` close the peek? */
export function frameClickCloses(hit: Element | null): boolean {
  return !!hit && hit.closest(PEEK_FRAME_KEEPS_OPEN) === null;
}

// ---- pure: prev / next -------------------------------------------------------

/**
 * The key `delta` steps away in the view's visible order, or null when there is
 * nowhere to go. DOES NOT WRAP: an arrow that silently jumps from the last card
 * back to the first is a gesture whose result depends on where you already
 * were, and the peek's header disables the ends instead.
 */
export function stepPeekKey(
  order: readonly string[],
  current: string | null,
  delta: number,
): string | null {
  if (current === null) return null;
  const at = order.indexOf(current);
  if (at < 0) return null;
  const next = at + delta;
  if (next < 0 || next >= order.length) return null;
  return order[next] ?? null;
}

/**
 * WHERE THE PANEL GOES WHEN THE TASK IN IT IS TAKEN AWAY — archived, or deleted
 * (design.md, Header + list state v2: "the peek does NOT close — it moves to
 * the next task down in the visible order, previous if it was the last").
 *
 * `null` means CLOSE, and it means it only when there is genuinely nothing
 * left: the key was the whole list, or it was not in the list at all.
 *
 * DOWN FIRST, and that is the whole of the rule worth writing down. Filing a
 * task is a sweep — you work down a column clearing it — and a panel that
 * jumped BACKWARDS after each archive would make the reader re-find their place
 * every time. The last row is the only one with nowhere down to go, and there
 * the previous one is the nearest thing to "where you were".
 *
 * Deliberately takes the order AS IT WAS BEFORE the act. Reading it afterwards
 * is a race with the poll that removes the row — and with a filter that may
 * keep it — and the answer this function must give is about the list the reader
 * was looking at when they pressed.
 */
export function nextAfterRemoval(
  order: readonly string[],
  removed: string,
): string | null {
  const at = order.indexOf(removed);
  if (at < 0) return null;
  return order[at + 1] ?? order[at - 1] ?? null;
}

// ---- pure: make room ---------------------------------------------------------

export interface RoomInput {
  /** Is the peek open at all? A closed peek's frame is the whole content area. */
  open: boolean;
  /** The window's inner width — the sidebar and the peek both come out of it. */
  viewport: number;
  /** The width the reader DRAGGED, or null for "no choice yet". Not a resolved
   *  number, because the default is the remainder past the baseline and the
   *  content area is one of the things this function works out. */
  chosenWidth: number | null;
  /** The middle pane's measured baseline, or null before anything has been
   *  measured (see `setPeekBaseline`). */
  baseline: number | null;
  /** What the sidebar occupies right now (its rail width when collapsed). */
  sidebarWidth: number;
}

export interface RoomPlan {
  /** What the peek renders at, clamped for the window it is actually in. */
  peekWidth: number;
  /** What the frame is left with once the peek has taken its share. */
  frameAfter: number;
  /** The width below which the middle pane's CONTENT stops shrinking. */
  floor: number;
  /** The content's width once the floor has caught it — what the views are
   *  given as a `min-width`, so anything narrower scrolls instead of reflowing. */
  contentFloor: number;
  /** The frame is narrower than the floor: the middle pane scrolls sideways. */
  floored: boolean;
  /** The peek takes the whole content area and the middle pane is hidden. */
  cover: boolean;
}

/**
 * The whole of the layout, as arithmetic (design.md, Widths v2).
 *
 *   1. the frame is whatever the sidebar and the peek have not taken;
 *   2. the peek's own width is the reader's dragged number, or the remainder
 *      past the middle pane's baseline — clamped to [220, content];
 *   3. under ¾ of the baseline the middle pane is FLOORED: its content keeps
 *      `floor` px and the pane scrolls horizontally inside the frame;
 *   4. under 360 there is no middle pane worth keeping, so the peek covers.
 *
 * NOTHING HERE TOUCHES THE SIDEBAR any more. That decision is a crossing, not
 * a width (`planCrossing`), and it is spent only by the three gestures that
 * RESIZE — never by an open, a close or a swap (design.md, Widths v2).
 */
export function planRoom(input: RoomInput): RoomPlan {
  const { open, viewport, chosenWidth, baseline, sidebarWidth } = input;
  const content = Math.max(0, viewport - sidebarWidth);
  const floor = middleFloor(baselineOr(baseline, content));
  const peekWidth = peekWidthFor(chosenWidth, content, baseline);
  if (!open) {
    return {
      peekWidth,
      frameAfter: content,
      floor,
      contentFloor: floor,
      floored: false,
      cover: false,
    };
  }
  const frameAfter = content - peekWidth;
  const cover = frameAfter < PEEK_COVER_FLOOR;
  return {
    // In cover mode the panel IS the content area, so that is its width too.
    peekWidth: cover ? content : peekWidth,
    frameAfter: cover ? content : frameAfter,
    floor,
    contentFloor: floor,
    // Cover is not "floored": there is no middle pane on screen to scroll.
    floored: !cover && frameAfter < floor,
    cover,
  };
}

/**
 * THE ONE THING AN *OPEN* MAY DO TO THE SIDEBAR (Akshil, 2026-09-14).
 *
 * Everything else about the sidebar is a CROSSING (`planCrossing`) and happens
 * only while the reader is resizing. This is the single exception, and it is
 * the first open of a visit on a window that is not quite wide enough:
 *
 *   content − baseline < 220
 *
 * means the peek cannot have its minimum without taking it out of the column
 * the reader is reading. The sidebar is 188px of chrome nobody is looking at;
 * spending that instead is the obvious trade, and it is the one the reader
 * would make by hand a second later.
 *
 * TWO CONDITIONS, and the second is what keeps this from being a twitch: the
 * collapse has to actually BUY the 220. On a genuinely narrow window it does
 * not, and then the sidebar stays where it is and the middle pane gives way as
 * it always did — moving a panel for no gain is worse than not moving it.
 *
 * Once spent, the crossing detector takes over unchanged: the collapse is
 * marked as ours, an upward crossing later hands it back under the override
 * rules, a close does NOT (a close is not a resize), and leaving /tasks does.
 */
export function openTimeCollapse(input: {
  viewport: number;
  baseline: number | null;
  /** The sidebar's width WHEN EXPANDED — what the collapse would give back. */
  sidebarExpanded: number;
  sidebarCollapsed: boolean;
}): boolean {
  if (input.sidebarCollapsed) return false;
  const now = Math.max(0, input.viewport - input.sidebarExpanded);
  const base = baselineOr(input.baseline, now);
  // Already room for a full-minimum peek beside an untouched column.
  if (now - base >= PEEK_MIN_WIDTH) return false;
  const railed = Math.max(0, input.viewport - SIDEBAR_RAIL_WIDTH);
  return railed - base >= PEEK_MIN_WIDTH;
}

// ---- pure: the sidebar's crossing detector -----------------------------------

/** Which side of the middle pane's floor the layout was last seen on. */
export type FloorSide = "above" | "below";

export interface CrossInput {
  viewport: number;
  chosenWidth: number | null;
  baseline: number | null;
  /** The sidebar's width WHEN EXPANDED — its stored/dragged width, never the
   *  rail. Both triggers are measured against it: see `middleIfExpanded`. */
  sidebarExpanded: number;
  sidebarCollapsed: boolean;
  /** The side last recorded, or null on the first read of an opening — which
   *  ESTABLISHES a side and fires nothing (an open is not a resize). */
  side: FloorSide | null;
  hysteresis?: number;
}

export interface CrossPlan {
  /** `true` collapse it, `false` expand it, `null` leave it exactly alone. */
  sidebar: boolean | null;
  /** The side to remember for the next read. */
  side: FloorSide;
  /** The middle pane's width measured with the sidebar expanded — the one
   *  variable both triggers read. Published for the tests and for debugging. */
  middleIfExpanded: number;
}

/**
 * THE SIDEBAR, AND THE ONE THING THAT MOVES IT: the middle pane CROSSING its
 * floor while the reader is resizing (design.md, Widths v2).
 *
 * A CROSSING, not a condition, and that distinction is the whole rule. The old
 * version asked "is the frame too narrow?" on every tick, so a reader who
 * re-opened the sidebar had it shut again by the very next pointermove — a
 * control that undoes itself while you watch. Here the answer only changes
 * when the side does, so between two crossings the reader's own open/close is
 * simply the truth and nothing argues with it.
 *
 * BOTH TRIGGERS ARE MEASURED WITH THE SIDEBAR EXPANDED. Measuring the expand
 * against the RAIL (which is how design.md first put it) asks "would the middle
 * pane be above the floor if we left the sidebar where it is?" — and the answer
 * being yes says nothing about whether it survives the 188px the expansion then
 * takes back. It does not, at the threshold, which is a sidebar that opens and
 * instantly closes. One variable, one floor, a 16px band around it.
 */
export function planCrossing(input: CrossInput): CrossPlan {
  const h = input.hysteresis ?? SIDEBAR_HYSTERESIS;
  const content = Math.max(0, input.viewport - input.sidebarExpanded);
  const floor = middleFloor(baselineOr(input.baseline, content));
  const peek = peekWidthFor(input.chosenWidth, content, input.baseline);
  const middleIfExpanded = content - peek;
  const side: FloorSide =
    middleIfExpanded < floor
      ? "below"
      : middleIfExpanded >= floor + h
        ? "above"
        : // Inside the band: whatever it was. A first read with no side yet is
          // "above", the state a page with no peek in it is in.
          (input.side ?? "above");
  if (input.side === null || side === input.side) {
    return { sidebar: null, side, middleIfExpanded };
  }
  if (side === "below") {
    // DOWNWARD. Collapse — even a sidebar the reader opened deliberately, which
    // is the one place this rule overrules them (design.md, Widths v2).
    return { sidebar: input.sidebarCollapsed ? null : true, side, middleIfExpanded };
  }
  // UPWARD. Put it back, and only if it is down.
  return { sidebar: input.sidebarCollapsed ? false : null, side, middleIfExpanded };
}

// ---- the store ---------------------------------------------------------------

export interface PeekState {
  /**
   * IS THE FEATURE ON AND ON THIS PAGE? Published as state rather than kept as
   * a bare module flag because the four views read it to decide whether to
   * stamp anything at all — the walk's attribute, the halo, the quick door —
   * and they have to re-render when the answer lands (the pref is read
   * asynchronously, so it arrives after the first paint).
   */
  host: boolean;
  /** `Task.key` of the open task, or null. */
  key: string | null;
  /** The width the reader DRAGGED, or null for "no choice yet" — which is a
   *  real state and not a missing number (see PEEK_WIDTH_KEY). */
  width: number | null;
  autoCollapsed: boolean;
  /**
   * THE MIDDLE PANE'S BASELINE — the content column's width the moment the peek
   * first opened in this visit, frozen there (design.md, Widths v2). Null until
   * that moment.
   *
   * In the published state rather than in module memory alone because it is an
   * INPUT to the layout every render reads: freezing it is what makes the first
   * open keep the column exactly where it was, and a React memo that cannot see
   * it changing would go on drawing the peek at the width it had a frame ago.
   */
  baseline: number | null;
  /** Opened by a deep link / a Back: no slide, the panel is simply there. */
  instant: boolean;
}

function loadWidth(): number | null {
  try {
    const raw = localStorage.getItem(PEEK_WIDTH_KEY);
    if (raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null; // private mode / blocked store — the default share is fine
  }
}

function saveWidth(width: number | null): void {
  try {
    if (width === null) localStorage.removeItem(PEEK_WIDTH_KEY);
    else localStorage.setItem(PEEK_WIDTH_KEY, String(Math.round(width)));
  } catch {
    // best-effort, exactly like sidebarstate's
  }
}

function loadAutoCollapsed(): boolean {
  try {
    return sessionStorage.getItem(PEEK_AUTOCOLLAPSE_KEY) === "1";
  } catch {
    return false; // blocked store — we simply will not re-expand, which is safe
  }
}

function saveAutoCollapsed(on: boolean): void {
  try {
    if (on) sessionStorage.setItem(PEEK_AUTOCOLLAPSE_KEY, "1");
    else sessionStorage.removeItem(PEEK_AUTOCOLLAPSE_KEY);
  } catch {
    // best-effort, exactly like the width's
  }
}

/**
 * The marker is only ever true ABOUT AN OPEN PEEK, so a load with no `?peek=`
 * in the URL starts by forgetting it. Without this, a sitting that ended with
 * a peek open and then navigated somewhere peek-less would carry "the sidebar
 * is ours" into a later visit — and the first peek opened there would hand back
 * a sidebar the reader had collapsed themselves.
 */
function seedAutoCollapsed(): boolean {
  let stored = loadAutoCollapsed();
  if (!stored) return false;
  let deep = false;
  try {
    deep = readPeekParam(location.search) !== null;
  } catch {
    deep = false;
  }
  if (!deep) {
    saveAutoCollapsed(false);
    stored = false;
  }
  return stored;
}

let state: PeekState = {
  host: false,
  key: null,
  width: loadWidth(),
  autoCollapsed: seedAutoCollapsed(),
  baseline: null,
  instant: true,
};

/**
 * THE WIDTH THE MIDDLE PANE IS CURRENTLY BEING READ AT, kept up to date by the
 * page while the peek is CLOSED and frozen into `state.baseline` at the first
 * open of the visit.
 *
 * Measured by the page rather than derived here, because the number wanted is
 * the 1050-capped column plus its gutters — a fact about a stylesheet and a
 * window, not one this module could compute (see `measureTasksBaseline` in
 * shell/Scheduled.tsx).
 */
let baselineCandidate: number | null = null;

/** The page's side padding as last measured (`measureTasksBaseline`). The
 *  stylesheet's own 22px either side is the fallback, used only before anything
 *  has been on screen to read. */
let baselineGutter = TASKS_PAGE_GUTTER;

/** Which side of the floor the last resize left the layout on (`planCrossing`).
 *  Null between visits and until an open establishes it. */
let floorSide: FloorSide | null = null;

const listeners = new Set<() => void>();

/**
 * IS THERE A PEEK TO OPEN AT ALL? Set by the Tasks page while it is mounted on
 * `/tasks` — and by nothing else, which is the whole rule for "everything that
 * opens a task from OUTSIDE the page keeps today's behaviour" (design.md). The
 * sidebar's task list, a notification and the app page's Tasks tab all navigate
 * exactly as they did; `openPeek` simply declines for them.
 */
let host = false;

export function getPeekState(): PeekState {
  return state;
}

export function subscribePeek(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function publish(next: PeekState): void {
  if (next === state) return;
  state = next;
  listeners.forEach((fn) => fn());
}

/** Mount/unmount signal from the Tasks page. Unmounting also closes: navigating
 *  away is a close trigger (design.md, Close triggers) and a peek left open in
 *  the store would spring back the next time the page mounted. */
export function setPeekHost(on: boolean): void {
  host = on;
  if (state.host !== on) publish({ ...state, host: on });
  if (on) {
    watchSidebar();
    return;
  }
  unwatchSidebar();
  // The preview's dragged height is a fact about THIS sitting of the page
  // (shell/peek-preview.ts): leaving takes it with it.
  resetPreviewHeight();
  if (state.key !== null) dropPeek(true);
  // …AND THE BASELINE, which is a fact about this VISIT: the column is measured
  // afresh the next time the page is opened and a peek is first pressed on it
  // (design.md, Widths v2 — "the first open of a visit").
  baselineCandidate = null;
  baselineGutter = TASKS_PAGE_GUTTER;
  floorSide = null;
  userMoved = false;
  if (state.baseline !== null) publish({ ...state, baseline: null });
  // LEAVING THE PAGE HANDS BACK A SIDEBAR WE TOOK (design.md, Widths v2 — the
  // one piece of the old make-room rules that survives). The floor rule is
  // about the middle pane, and off /tasks there is no middle pane: a reader who
  // navigates away must not be stranded with a rail nothing on screen explains.
  //
  // Note this is NOT what an ordinary close does. Closing the panel leaves the
  // sidebar exactly where the last crossing put it, because open and close are
  // not resizes and the new rules give them no say at all.
  if (state.autoCollapsed && getSidebarState().collapsed) {
    // …written the way the collapse was: a rail we persisted has to be undone
    // in `localStorage` too, or the reload the reader does next puts it back.
    setSidebar(false, heldPersisted);
    markAutoCollapsed(false);
    publish({ ...state, autoCollapsed: false, baseline: null });
  }
}

/**
 * THE COLUMN, MEASURED OFF THE LIVE PAGE — `.schedule-main` (the 1050-capped
 * column) plus the page's own side padding, because the number the peek's
 * default is a remainder of is the width the FRAME has to keep for that column
 * to go on being drawn exactly where it is.
 *
 * IT LIVES HERE, NOT IN THE PAGE, because of a race that cost a whole visit's
 * baseline: the page's ResizeObserver is a passive effect, and `useTaskPeekHost`
 * adopts a `?peek=` deep link in a LAYOUT effect — which runs first. The
 * observer had never fired, the candidate was null, the freeze latched null,
 * and because a freeze only ever retried on a FRESH open (`fresh` is false for
 * the rest of the visit) the baseline stayed null and the floor became a live
 * moving target for every drag that followed. A deep link and a Back are both
 * ordinary things to do.
 *
 * So the store reads the DOM itself when it is asked for a number it does not
 * have. Null when the page has not painted its section yet (the first tick of a
 * cold load, where the skeleton stands in for the views) — and null is a
 * RETRY, never a latch: see `freezeBaseline`.
 */
export function measureTasksBaseline(): number | null {
  if (typeof document === "undefined") return null;
  try {
    const main = document.querySelector<HTMLElement>(".tasks-frame .schedule-main");
    const page = document.querySelector<HTMLElement>(".tasks-frame .schedule-page");
    if (!main || !page) return null;
    const cs = getComputedStyle(page);
    const gutter =
      (Number.parseFloat(cs.paddingLeft) || 0) + (Number.parseFloat(cs.paddingRight) || 0);
    const column = main.getBoundingClientRect().width;
    if (!(column > 0)) return null;
    baselineGutter = gutter;
    return Math.round(column + gutter);
  } catch {
    return null; // no layout to read (a test harness, a detached document)
  }
}

/** The page's side padding as last measured — what turns the FRAME's floor into
 *  the width of the content inside those gutters. */
export function peekGutter(): number {
  return baselineGutter;
}

/**
 * THE PAGE REPORTING WHAT THE COLUMN IS CURRENTLY WORTH. Called from a
 * ResizeObserver while the peek is closed; ignored once the baseline is frozen,
 * which is what "the first open of a visit" means in code.
 */
export function setPeekBaselineCandidate(width: number): void {
  if (!Number.isFinite(width) || width <= 0) return;
  baselineCandidate = Math.round(width);
}

/** The observer's whole job: keep the candidate current while the panel is
 *  away. With it open the column has already given its share up, and
 *  re-reading would ratchet the baseline down a notch on every press. */
export function refreshPeekBaseline(): void {
  if (state.key !== null) return;
  const measured = measureTasksBaseline();
  if (measured !== null) baselineCandidate = measured;
}

/**
 * Freeze it — or try to.
 *
 * Three sources in order: the number already frozen, the observer's candidate,
 * and a DIRECT READ of the page. The last is what makes a deep link safe: the
 * layout effect that adopts `?peek=` runs before any passive observer has, so
 * without it the freeze latched null.
 *
 * NULL NEVER LATCHES. Every caller spends this as `state.baseline ?? freeze()`,
 * so an open that could not measure leaves the baseline null and the NEXT open,
 * swap or traversal tries again — instead of the old shape, where only a fresh
 * open retried and a visit that started on a deep link never got one.
 */
function freezeBaseline(): number | null {
  if (state.baseline !== null) return state.baseline;
  return baselineCandidate ?? measureTasksBaseline();
}

/** What the layout is currently reasoning with — the frozen baseline, or the
 *  live candidate before anything has been frozen. Exported for the page, which
 *  writes the floor onto the frame as a CSS variable. */
export function peekBaseline(): number | null {
  return state.baseline ?? baselineCandidate;
}

export function peekHostReady(): boolean {
  return host;
}

/** Push `?peek=` (or its absence) without telling the router: a `NAV_EVENT`
 *  would remount the whole page under the panel we just opened. `main.tsx`
 *  still wraps pushState, so the chrome that listens for `fused:urlchange`
 *  keeps up. */
function pushPeekUrl(key: string | null): void {
  try {
    const next = location.pathname + peekSearch(location.search, key);
    if (next === location.pathname + location.search) return;
    history.pushState(history.state, "", next);
  } catch {
    // No history (a test harness without one) — the peek is still usable.
  }
}

/** The same write, REPLACING the current entry instead of pushing one. For
 *  corrections rather than choices: a `?peek=` that names nothing was never a
 *  place the reader navigated to, so Back must not be able to return to it. */
function replacePeekUrl(key: string | null): void {
  try {
    const next = location.pathname + peekSearch(location.search, key);
    if (next === location.pathname + location.search) return;
    history.replaceState(history.state, "", next);
  } catch {
    // No history (a test harness without one) — the peek is still usable.
  }
}

/**
 * Put the panel away. Shared by the close gestures and by the correction below,
 * so the two cannot get out of step.
 *
 * IT DOES NOT TOUCH THE SIDEBAR (design.md, Widths v2): a close is not a
 * resize, and the sidebar is now the reader's until the middle pane crosses its
 * floor again. What a close DOES leave behind is the `autoCollapsed` marker —
 * the claim that the rail out there is ours — so that navigating off /tasks
 * still hands it back (`setPeekHost`).
 */
function dropPeek(instant: boolean): void {
  publish({ ...state, key: null, instant });
}

/**
 * THE URL MEETS THE DATA. Called once the page's tasks have actually loaded,
 * with the set to resolve against.
 *
 * Three outcomes: the key is a task's (nothing to do), the value is a number
 * that names exactly one task (rewrite the URL to that task's key, in place),
 * or it names nothing at all — a stale link, a deleted task, a typo — and then
 * the peek CLOSES and the param goes, replaced rather than pushed. An empty
 * panel with a blank header over a key that resolves to nothing is the defect
 * this exists to prevent; it is also the only state the panel can be in where
 * none of its own controls say anything.
 */
export function settlePeek(tasks: readonly PeekNamed[]): void {
  if (!host || state.key === null) return;
  const resolved = resolvePeekKey(state.key, tasks);
  if (resolved === state.key) return;
  if (resolved === null) {
    replacePeekUrl(null);
    dropPeek(true);
    return;
  }
  // A number resolved to its row: the URL becomes canonical in place, so a
  // reload or a copied link addresses the same conversation the panel is
  // showing even after the number is respent.
  replacePeekUrl(resolved);
  publish({ ...state, key: resolved });
}

/**
 * Open the peek on one task, or swap it to another.
 *
 * Returns `false` when there is no peek on this page, which is the caller's
 * signal to do what it always did (navigate). That answer is the ONLY thing
 * separating the four views' presses from every other way into a task.
 */
export function openPeek(key: string, opts?: { push?: boolean }): boolean {
  if (!host || !key) return false;
  if (state.key === key) return true;
  // A fresh OPENING freezes the middle pane's baseline — the width the column
  // is being read at, RIGHT NOW, before the panel takes anything (design.md,
  // Widths v2). It also re-reads the desk's table (shell/peek-preview.ts): the
  // panel is about to frame an app for minutes, and the table read once at page
  // load is the one input it cannot afford to be wrong about. A SWAP does
  // neither — that would be a fresh baseline and one table read per ⌃⇧J.
  const fresh = state.key === null;
  if (fresh) {
    forgetAppsCache();
    userMoved = false;
    heldPersisted = false;
  }
  if (opts?.push !== false) pushPeekUrl(key);
  // `state.baseline ?? …` on EVERY open, not just a fresh one: a freeze that
  // could not measure leaves null, and null has to be retried rather than
  // latched (`freezeBaseline` carries the incident).
  publish({ ...state, key, baseline: state.baseline ?? freezeBaseline(), instant: false });
  // AN OPEN IS NOT A RESIZE (design.md, Widths v2): it moves no sidebar — with
  // the ONE exception below, which is a first open on a window too narrow to
  // give the panel its minimum without eating into the column.
  if (fresh) {
    spendOpenTimeCollapse();
    establishSide();
  }
  return true;
}

/** The open-time exception, spent (`openTimeCollapse` carries the rule). */
function spendOpenTimeCollapse(): void {
  const sidebar = getSidebarState();
  const collapse = openTimeCollapse({
    viewport: viewportWidth(),
    baseline: peekBaseline(),
    sidebarExpanded: sidebar.width,
    sidebarCollapsed: sidebar.collapsed,
  });
  if (!collapse) return;
  // Always silent: this is the layout getting out of the way at the very moment
  // the panel arrives, and the reader has not touched anything yet.
  setSidebar(true, false);
  heldPersisted = false;
  markAutoCollapsed(true);
  publish({ ...state, autoCollapsed: true });
}

export function closePeek(opts?: { push?: boolean }): void {
  if (state.key === null) return;
  if (opts?.push !== false) pushPeekUrl(null);
  dropPeek(false);
}

/** A `popstate` landed: the URL is the truth again, and neither direction of a
 *  traversal should push another entry on top of the one the browser just
 *  restored. */
export function syncPeekFromUrl(search: string): void {
  if (!host) return;
  const key = readPeekParam(search);
  if (key === state.key) return;
  if (key === null) {
    dropPeek(true);
    return;
  }
  const fresh = state.key === null;
  publish({ ...state, key, baseline: state.baseline ?? freezeBaseline(), instant: true });
  // A traversal that ARRIVES at a peek is a first open too (a deep link is the
  // commonest way in), so it gets the same one exception.
  if (fresh) spendOpenTimeCollapse();
  establishSide();
}

/** The width a drag is producing. `persist` is false for the per-pointermove
 *  updates (usePreviewPane's own rule) — the settled width is written once. */
export function setPeekWidth(width: number, persist = true): void {
  const next = Math.round(width);
  if (persist) saveWidth(next);
  if (next === state.width) return;
  publish({ ...state, width: next });
}

/** Double-click on the seam: back to NO CHOICE, which is the remainder past the
 *  middle pane's baseline on whatever window the reader is on — not back to a
 *  remembered number, and not to a constant (design.md, Widths v2). */
export function resetPeekWidth(): void {
  saveWidth(null);
  if (state.width === null) return;
  publish({ ...state, width: null });
}

// ---- the sidebar, made to give way -------------------------------------------

function viewportWidth(): number {
  return typeof window === "undefined" ? 0 : window.innerWidth || 0;
}

function roomEnv(): RoomInput {
  const sidebar = getSidebarState();
  return {
    open: state.key !== null,
    viewport: viewportWidth(),
    chosenWidth: state.width,
    baseline: peekBaseline(),
    sidebarWidth: sidebar.collapsed ? SIDEBAR_RAIL_WIDTH : sidebar.width,
  };
}

function crossEnv(): CrossInput {
  const sidebar = getSidebarState();
  return {
    viewport: viewportWidth(),
    chosenWidth: state.width,
    baseline: peekBaseline(),
    // THE EXPANDED WIDTH, whichever state the sidebar is in: `sidebar.width` is
    // the stored/dragged number and does not change when it collapses to its
    // rail (platform/lib/sidebarstate). Both triggers read it — see
    // `planCrossing` on why the expand cannot be measured against the rail.
    sidebarExpanded: sidebar.width,
    sidebarCollapsed: sidebar.collapsed,
    side: floorSide,
  };
}

/** Record which side of the floor we are on WITHOUT acting on it. What an open
 *  (and a deep link, and a Back) does, so the next resize is a crossing. */
function establishSide(): void {
  floorSide = planCrossing({ ...crossEnv(), side: null }).side;
}

/**
 * A RESIZE HAPPENED — the seam was dragged, the seam was arrowed, or the window
 * changed size. The only three gestures allowed to move the sidebar
 * (design.md, Widths v2), and they move it only on a CROSSING of the middle
 * pane's floor.
 */
export function applyResize(): RoomPlan {
  if (state.key !== null) {
    const plan = planCrossing(crossEnv());
    floorSide = plan.side;
    if (plan.sidebar !== null) {
      // WHAT A WRITE PERSISTS IS DECIDED BY WHAT IT OVERWRITES.
      //
      //   * something the READER did during this opening (`userMoved`) — write
      //     it down: the trigger just overrode a decision they can see, and a
      //     `localStorage` that still holds the decision we overrode is a
      //     sidebar that snaps back on the next reload;
      //   * a collapse WE already wrote down (`heldPersisted`) — write the
      //     expand down too, or we leave the same disagreement behind from the
      //     other side. Found live: a persisted collapse followed by a silent
      //     expand left the DOM at 232 and storage at `collapsed: true`;
      //   * anything else — stay silent. This is the layout moving its own
      //     furniture, and filing that as a preference is the bug the
      //     `persist: false` seam exists to prevent (`setSidebar` above).
      const persist = userMoved || (plan.sidebar === false && heldPersisted);
      setSidebar(plan.sidebar, persist);
      heldPersisted = plan.sidebar ? persist : false;
      userMoved = false;
      const mine = plan.sidebar;
      if (mine !== state.autoCollapsed) {
        markAutoCollapsed(mine);
        publish({ ...state, autoCollapsed: mine });
      }
    }
  }
  return planRoom(roomEnv());
}

/**
 * Move the sidebar WITHOUT WRITING THE READER'S PREFERENCE.
 *
 * `persist: false` is the whole of it, and it is the same seam a resize drag
 * uses (sidebarstate's own note). An auto-collapse is the layout getting out of
 * the peek's way for as long as the peek is open; writing it to
 * `localStorage["fused-render:sidebar"]` filed it as a CHOICE, indistinguishable
 * from the reader pressing the collapse button — so a crash, a closed tab or a
 * navigation in the wrong moment left them with a sidebar they never shut and
 * no record that anything had shut it.
 *
 * `ours` guards the subscription below: this is the one writer allowed to move
 * the sidebar without that counting as the reader changing their mind.
 */
function setSidebar(collapsed: boolean, persist = false): void {
  ours = true;
  try {
    setSidebarState((s) => (s.collapsed === collapsed ? s : { ...s, collapsed }), persist);
  } finally {
    ours = false;
  }
}

/**
 * DID THE READER MOVE THE SIDEBAR THEMSELVES DURING THIS OPENING?
 *
 * It decides one thing: whether our next write PERSISTS. An auto-collapse is
 * the layout getting out of the way and must not be filed as a preference (the
 * note above). But a trigger that overrides a toggle the reader just made is a
 * different act — it overwrites a decision they took and can see, and leaving
 * `localStorage` holding the decision we overrode is how the sidebar came to
 * snap back to a rail on the next reload with nothing on screen explaining it
 * (UX pass 2, 2026-09-14): DOM 232, storage `collapsed: true`, reload → 44.
 *
 * So: override an auto state, write silently; override a MANUAL one, write it
 * down. The state the trigger produces is the one the reader is looking at.
 */
let userMoved = false;
/** …and whether the collapse we are currently holding was written that way, so
 *  the hand-back on navigate-away matches it rather than re-opening the same
 *  disagreement from the other side. */
let heldPersisted = false;

function markAutoCollapsed(on: boolean): void {
  saveAutoCollapsed(on);
}

/** True only while `setSidebar` above is in flight. */
let ours = false;
let unsubscribeSidebar: (() => void) | null = null;

/**
 * THE READER MOVING THE SIDEBAR THEMSELVES, and the ONE thing it still changes
 * here: an expand we did not make means the rail out there is no longer ours,
 * so there is nothing left for a navigate-away to hand back.
 *
 * It does NOT stop the floor rule arguing, and that is the change of 2026-09-14
 * (design.md, Widths v2). There is no `overruled` latch any more, because the
 * crossing detector does not need one: it acts on a CHANGE of side, so between
 * two crossings the reader's own open or close simply stands — and when the
 * middle pane does cross downward again, the collapse fires even on a sidebar
 * they opened deliberately, which is what Akshil asked for.
 */
function watchSidebar(): void {
  if (unsubscribeSidebar) return;
  unsubscribeSidebar = subscribeSidebarState(() => {
    if (ours) return;
    // ANY move of theirs, in either direction — a manual collapse counts as
    // much as a manual expand, because the incoherence this guards against is
    // about persistence and not about direction (`userMoved`).
    userMoved = true;
    if (getSidebarState().collapsed) return;
    if (!state.autoCollapsed) return;
    markAutoCollapsed(false);
    publish({ ...state, autoCollapsed: false });
  });
}

function unwatchSidebar(): void {
  unsubscribeSidebar?.();
  unsubscribeSidebar = null;
}

/** The plan for THIS render, with no side effects — what TaskPeek draws from. */
export function currentRoom(): RoomPlan {
  return planRoom(roomEnv());
}

// ---- React ------------------------------------------------------------------

function peekKey(): string | null {
  return state.key;
}

/** Which task is open — what each of the four views asks to draw the halo. */
export function usePeekedKey(): string | null {
  return useSyncExternalStore(subscribePeek, peekKey, peekKey);
}

function peekHost(): boolean {
  return state.host;
}

/**
 * IS THE SIDE PEEK LIVE ON THIS PAGE — the one question every view asks before
 * it adds anything of the feature's to its own markup.
 *
 * False means the page renders exactly as it did before the feature existed:
 * no walk attribute, no halo, no quick door, no measured-fit attributes. That
 * is the flag's contract (`task-peek-flag.ts`, `task_peek_enabled`), and
 * reading it here rather than re-reading the pref in four places is what keeps
 * "off" from meaning four slightly different things.
 */
export function usePeekHost(): boolean {
  return useSyncExternalStore(subscribePeek, peekHost, peekHost);
}

/** Test seam: the store is module-level and bun shares one process per run. */
export function resetPeekStoreForTests(): void {
  unwatchSidebar();
  host = false;
  ours = false;
  baselineCandidate = null;
  baselineGutter = TASKS_PAGE_GUTTER;
  floorSide = null;
  userMoved = false;
  heldPersisted = false;
  saveAutoCollapsed(false);
  state = {
    host: false,
    key: null,
    width: null,
    autoCollapsed: false,
    baseline: null,
    instant: true,
  };
  listeners.clear();
}
