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

/** THE FLOOR, and 564 is a floor rather than the default (design.md, Width &
 *  resize — re-measured: Notion opens at half the content area and only reads
 *  564 once a session has ratcheted down to it). */
export const PEEK_MIN_WIDTH = 564;

/** The ceiling, as a share of the content area: two thirds. A peek may take
 *  most of the page but never all of it — the frame is the reason the peek
 *  exists rather than a navigation. */
export const PEEK_MAX_FRACTION = 2 / 3;

/** …and the default, the other share: half. */
export const PEEK_DEFAULT_FRACTION = 1 / 2;

/** Below this the global sidebar is auto-collapsed to make room (design.md,
 *  Make-room rules 1-2 — Notion's own measured frame minimum). */
export const PEEK_FRAME_MIN = 430;

/** …and below THIS, with the sidebar already down to its rail, there is no
 *  frame worth keeping: the peek covers the content area instead of squeezing
 *  the view to a sliver (rule 4). */
export const PEEK_COVER_FLOOR = 360;

/** How far one arrow press moves the seam (design.md, Width & resize). */
export const PEEK_KEY_STEP = 10;

// ---- pure: width -------------------------------------------------------------

/** Half the content area — what an undragged peek opens at, on this window. */
export function defaultPeekWidth(content: number): number {
  return Math.round(content * PEEK_DEFAULT_FRACTION);
}

/**
 * A width, clamped into `[564, ⅔ × content]`, where `content` is the area the
 * frame and the peek share (viewport − sidebar).
 *
 * The ceiling can fall BELOW the floor on a narrow window, and then the floor
 * wins: a peek that is too wide for the window is about to be in cover mode
 * anyway (`planRoom`), where this number is not what renders. Never returning
 * NaN is the other half of the contract — a persisted width from a broken write
 * must not become an inline `width: NaNpx`.
 */
export function clampPeekWidth(width: number, content: number): number {
  const wanted = Number.isFinite(width) ? width : defaultPeekWidth(content);
  const ceiling = Math.floor(content * PEEK_MAX_FRACTION);
  return Math.round(
    Math.max(PEEK_MIN_WIDTH, Math.min(wanted, Math.max(ceiling, PEEK_MIN_WIDTH))),
  );
}

/** The rendered width for a content area: the reader's own number if they have
 *  dragged one, else half the area — clamped either way, which is what
 *  "re-clamp the persisted width on resize" means in practice. */
export function peekWidthFor(chosen: number | null, content: number): number {
  return clampPeekWidth(chosen ?? defaultPeekWidth(content), content);
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

// ---- pure: make room ---------------------------------------------------------

export interface RoomInput {
  /** Is the peek open at all? A closed peek's plan is "restore what we took". */
  open: boolean;
  /** The window's inner width — the sidebar and the peek both come out of it. */
  viewport: number;
  /** The width the reader DRAGGED, or null for "no choice yet". Not a resolved
   *  number, because the default is a fraction of the content area and the
   *  content area is one of the things this function works out. */
  chosenWidth: number | null;
  /** What the sidebar occupies right now (its rail width when collapsed). */
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  /** Did WE collapse it? Only then may we put it back (design.md, rule 3). */
  autoCollapsed: boolean;
  /** Has the reader opened the sidebar again with the peek still up? Then the
   *  layout stops asking for it: see `overruled` in the store. */
  overruled?: boolean;
}

export interface RoomPlan {
  /** What the peek renders at, clamped for the window it is actually in. */
  peekWidth: number;
  /** What the frame is left with once the peek has taken its share. */
  frameAfter: number;
  /** The peek takes the whole content area and the frame is not shrunk. */
  cover: boolean;
  /** `true` collapse it, `false` put it back, `null` leave it alone. */
  sidebar: boolean | null;
  /** The flag to store afterwards. */
  autoCollapsed: boolean;
}

/**
 * The whole of "make room", as arithmetic (design.md, Make-room rules).
 *
 * Read in the order the rules are numbered:
 *
 *   1. the frame is whatever the sidebar and the peek have not taken;
 *   2. under 430 with the sidebar expanded, the sidebar goes — in the same tick,
 *      so one 200ms transition carries both;
 *   3. on close we put back only a sidebar WE took: one the reader closed
 *      themselves stays closed, which is the difference between helping and
 *      overruling;
 *   4. under 360 even with the sidebar at its rail, there is no frame left to
 *      squeeze, so the peek covers instead.
 *
 * TWO PASSES OVER THE WIDTH, and they are not a tidiness problem: the peek's
 * own width is a share of the content area, the content area depends on whether
 * the sidebar is collapsing, and whether it collapses depends on the peek's
 * width. So the collapse is decided against the sidebar as it stands, and the
 * width the peek RENDERS at is then re-derived against the content area it is
 * actually getting — otherwise an undragged peek would open at half of one area
 * and immediately be sitting in another.
 */
export function planRoom(input: RoomInput): RoomPlan {
  const { open, viewport, chosenWidth, sidebarWidth, sidebarCollapsed, autoCollapsed } = input;
  const overruled = input.overruled === true;
  if (!open) {
    // Closing: the frame is the whole content area again, and the only decision
    // left is whether the sidebar we borrowed goes back.
    const content = Math.max(0, viewport - sidebarWidth);
    return {
      peekWidth: peekWidthFor(chosenWidth, content),
      frameAfter: content,
      cover: false,
      sidebar: autoCollapsed && sidebarCollapsed ? false : null,
      autoCollapsed: false,
    };
  }
  const contentNow = Math.max(0, viewport - sidebarWidth);
  const collapse =
    !overruled &&
    !sidebarCollapsed &&
    contentNow - peekWidthFor(chosenWidth, contentNow) < PEEK_FRAME_MIN;
  const settledSidebar = collapse || sidebarCollapsed ? SIDEBAR_RAIL_WIDTH : sidebarWidth;
  const content = Math.max(0, viewport - settledSidebar);
  const peekWidth = peekWidthFor(chosenWidth, content);
  const frameAfter = content - peekWidth;
  const cover = frameAfter < PEEK_COVER_FLOOR;
  return {
    // In cover mode the panel IS the content area, so that is its width too.
    peekWidth: cover ? content : peekWidth,
    frameAfter: cover ? content : frameAfter,
    cover,
    sidebar: collapse ? true : null,
    // Latched: a second open (or a window resize) must not forget that the
    // sidebar we are still holding down was ours to put back.
    autoCollapsed: autoCollapsed || collapse,
  };
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
  instant: true,
};

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
  // LEAVING THE PAGE IS A CLOSE, and a close hands back a sidebar we borrowed
  // (design.md, Close triggers + rule 3). Clearing the flag without restoring
  // the panel was the bug: click a task, click a sidebar link, and the sidebar
  // you are now looking at is collapsed with nothing on screen that collapsed
  // it. Same performer as every other close.
  if (state.key !== null) dropPeek(true);
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

/** Put the panel away and hand back a sidebar we borrowed. Shared by the close
 *  gestures and by the correction below, so the two cannot get out of step. */
function dropPeek(instant: boolean): void {
  overruled = false;
  const plan = planRoom({ ...roomEnv(), open: false });
  if (plan.sidebar === false) setSidebar(false);
  markAutoCollapsed(false);
  publish({ ...state, key: null, autoCollapsed: false, instant });
}

/**
 * THE URL MEETS THE DATA. Called once the page's tasks have actually loaded,
 * with the set to resolve against.
 *
 * Three outcomes: the key is a task's (nothing to do), the value is a number
 * that names exactly one task (rewrite the URL to that task's key, in place),
 * or it names nothing at all — a stale link, a deleted task, a typo — and then
 * the peek CLOSES and the param goes, replaced rather than pushed. An empty
 * 564px panel with a blank header over a key that resolves to nothing is the
 * defect this exists to prevent; it is also the only state the panel can be in
 * where none of its own controls say anything.
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
  applyRoom();
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
  // A fresh OPENING starts the make-room argument over; a swap does not.
  // It also re-reads the desk's table (shell/peek-preview.ts): the panel is
  // about to frame an app for minutes, and the table read once at page load is
  // the one input it cannot afford to be wrong about. A SWAP does not — that
  // would be one table read per press of ⌃⇧J.
  if (state.key === null) {
    overruled = false;
    forgetAppsCache();
  }
  if (opts?.push !== false) pushPeekUrl(key);
  publish({ ...state, key, instant: false });
  applyRoom();
  return true;
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
  publish({ ...state, key, instant: true });
  applyRoom();
}

/** The width a drag is producing. `persist` is false for the per-pointermove
 *  updates (usePreviewPane's own rule) — the settled width is written once. */
export function setPeekWidth(width: number, persist = true): void {
  const next = Math.round(width);
  if (persist) saveWidth(next);
  if (next === state.width) return;
  publish({ ...state, width: next });
}

/** Double-click on the seam: back to NO CHOICE, which is half the content area
 *  on whatever window the reader is on — not back to a remembered number, and
 *  not to a constant (design.md, Width & resize). */
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
    sidebarWidth: sidebar.collapsed ? SIDEBAR_RAIL_WIDTH : sidebar.width,
    sidebarCollapsed: sidebar.collapsed,
    autoCollapsed: state.autoCollapsed,
    overruled,
  };
}

/**
 * Spend the plan: collapse the sidebar (or put it back) and remember whether
 * the collapse was ours. Called on open, on swap-that-changes-nothing-else, and
 * on every window resize while the peek is up — the rules are about the CURRENT
 * window, not the one the peek opened in.
 */
export function applyRoom(): RoomPlan {
  const plan = planRoom(roomEnv());
  if (plan.sidebar !== null) setSidebar(plan.sidebar);
  if (plan.autoCollapsed !== state.autoCollapsed) {
    markAutoCollapsed(plan.autoCollapsed);
    publish({ ...state, autoCollapsed: plan.autoCollapsed });
  }
  return plan;
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
function setSidebar(collapsed: boolean): void {
  ours = true;
  try {
    setSidebarState((s) => (s.collapsed === collapsed ? s : { ...s, collapsed }), false);
  } finally {
    ours = false;
  }
}

function markAutoCollapsed(on: boolean): void {
  saveAutoCollapsed(on);
}

/** True only while `setSidebar` above is in flight. */
let ours = false;
/**
 * THE READER HAS THE LAST WORD, for the rest of this opening. Set when the
 * sidebar is expanded by anything that is not us; cleared when the peek next
 * opens from closed. Clearing the `autoCollapsed` flag alone was not enough —
 * the arithmetic still read the same narrow window and asked for the collapse
 * again on the very next resize or prev/next, so the chevron undid itself while
 * the reader watched.
 */
let overruled = false;
let unsubscribeSidebar: (() => void) | null = null;

/**
 * THE READER OVERRULING US. While the peek is open and the sidebar is down
 * because we put it down, the reader may simply open it again — the rail's
 * chevron, or a drag on the seam. That is a decision, and from then on the
 * layout must stop arguing: without this, the very next `applyRoom` (a window
 * resize, a prev/next) read the same narrow window and collapsed it straight
 * back, which is a control that undoes itself while you watch.
 *
 * So an expand we did not make CLEARS the flag: the sidebar is theirs now, it
 * will not be collapsed again for this opening, and there is nothing left for
 * the close to hand back.
 */
function watchSidebar(): void {
  if (unsubscribeSidebar) return;
  unsubscribeSidebar = subscribeSidebarState(() => {
    if (ours) return;
    if (getSidebarState().collapsed) return;
    overruled = true;
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
  overruled = false;
  saveAutoCollapsed(false);
  state = { host: false, key: null, width: null, autoCollapsed: false, instant: true };
  listeners.clear();
}
