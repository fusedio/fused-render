// THE TASK SIDE PEEK — the panel that slides in from the right of the Tasks
// page when a row, a card or a chip is opened, so the view the reader was
// scanning stays on screen instead of being navigated away from
// (.claude-design/task-side-peek/design.md; reference behaviour and every
// measured value in notion-side-peek-notes.md beside it).
//
// NOT TO BE CONFUSED WITH `TaskPeek` IN TaskCards.tsx, which is the Cards
// wall's own modal popup and predates this. That one is a `Modal` over the
// page; this one is a sibling of the page. The wall's card press now opens
// THIS when the Tasks page is hosting it (`openPeek` answers false anywhere
// else), and the modal stays as the fallback for the app page's Tasks tab.
//
// Layout is Notion's, copied deliberately: the peek is a DOM sibling of the
// frame inside one flex row, absolutely positioned over the row's right edge,
// and opening animates two properties in lockstep — the frame's width and the
// peek's translateX, both 200ms `ease` (see styles/task-peek.css for why the
// easing is plain `ease` and not the app's `--ease-out`).
//
// What lives HERE is the panel: its header, its resize seam, its body and the
// keyboard. What is open, how wide, and whether the sidebar had to give way is
// `task-peek-store.ts` — pure, and tested there.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { Task } from "@platform/lib/api";
import { shortTaskId } from "@platform/lib/task-id";
import { archiveTask, unarchiveTask } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { notify } from "@platform/lib/notifications";
import { withNoFocus } from "@platform/lib/frame-focus";
import { useParamBoundary } from "@platform/lib/param-boundary";
import { navigateUrl } from "@platform/lib/router";
import { anyModalOpen } from "@platform/ui/modal/esc-stack";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { ChatMount, useNativeChatFlag } from "@apps/claude";
import { runAgent } from "@apps/claude/protocol/agent";

// THE HEADER IS THE PEEK'S OWN NOW, not the chat's (design.md, Header + list
// state v2). It wore `@apps/claude/ui/Topbar` between 2026-09-13 and -09-14,
// which bought one row instead of two and cost the row its subject: the ✻
// Claude wordmark and the model/run cluster are facts about the TOOL, and this
// panel is about a TASK. What is left is the task — its status, its number, its
// title — with the panel's own controls either side.
import { folderHref, peekFrameSrc } from "./schedule-lib";
import { EraseTaskModal } from "./EraseTaskModal";
import {
  ICON_ARCHIVE,
  ICON_OPEN_FOLDER_PATH,
  ICON_TRASH,
  ICON_UNARCHIVE,
} from "./ScheduleTaskViews";
import { useChatTemplates } from "./TaskCards";
// The header's identity block — status ring, number, title, project — lives in
// its own module because the native chat's top line draws the SAME block for the
// task behind the conversation it is showing (see that file's note).
import { TaskPeekProject, TaskPeekWho, useTaskHeadline } from "./TaskPeekWho";
import { PEEK_HEAD_DROPS, useStripFit } from "./row-fit";
import {
  PREVIEW_KEY_STEP,
  PREVIEW_LOAD_TIMEOUT_MS,
  PREVIEW_MIN_H,
  PREVIEW_CHAT_MIN,
  PREVIEW_VW,
  getPreviewHeight,
  previewBox,
  previewLoad,
  setPreviewHeight,
  subscribePreviewHeight,
  useAppForProject,
  type PreviewLoad,
} from "./peek-preview";
import {
  ERASE_BLOCKED_HINT,
  emptyPaneFailed,
  emptyPaneText,
  eraseBlocked,
  filingIntent,
  taskHref,
} from "./tasks-lib";
import { getSidebarState, subscribeSidebarState } from "@platform/lib/sidebarstate";
import { MISSING_FOLDER_TOAST, taskFolder } from "./useMissingFolders";
import {
  PEEK_ITEM_ATTR,
  PEEK_KEY_STEP,
  PEEK_WALK_ATTR,
  peekScrollTarget,
  peekVisibleOrder,
  nextAfterRemoval,
  refreshPeekBaseline,
  PEEK_MIN_WIDTH,
  applyResize,
  arrowShouldWalk,
  clampPeekWidth,
  closePeek,
  currentRoom,
  getPeekState,
  openPeek,
  readPeekParam,
  resetPeekWidth,
  setPeekHost,
  setPeekWidth,
  settlePeek,
  stepPeekKey,
  subscribePeek,
  syncPeekFromUrl,
  usePeekAnchor,
  usePeekedKey,
} from "./task-peek-store";

/** How long an EMPTY task list is given to turn out to be a list that has not
 *  arrived yet, before a `?peek=` naming nothing is treated as naming nothing
 *  (TaskPeek's settle effect). */
const PEEK_SETTLE_MS = 1200;

/** How long the panel is given to slide out before it is parked out of sight
 *  for good — the motion token, plus a frame's slack (styles/task-peek.css). */
const PEEK_PARK_MS = 240;

// ---- the store, as React -----------------------------------------------------

/** The sidebar as ONE value that changes when it does — the store publishes an
 *  object identity that `useSyncExternalStore` can compare, and what the layout
 *  actually cares about is "did the content area move". */
function sidebarStamp(): number {
  const s = getSidebarState();
  return s.collapsed ? -1 : s.width;
}

/** Re-render on window resize: every make-room rule is about the CURRENT
 *  window, so a peek opened wide has to re-decide when the window narrows.
 *
 *  `enabled` is the flag's, and it reaches all the way down to the listener: a
 *  reader who has opted out of the side peek is not paying for a resize handler
 *  on the Tasks page (shell/task-peek-flag.ts). */
function useViewportWidth(enabled: boolean): number {
  const [w, setW] = useState(() => (typeof window === "undefined" ? 0 : window.innerWidth));
  useEffect(() => {
    if (!enabled) return;
    const read = () => setW(window.innerWidth);
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, [enabled]);
  return w;
}

/** A subscription that subscribes to nothing — what `useSyncExternalStore` is
 *  handed when the feature is off, so the hook count is unchanged and no
 *  listener is registered. */
const NO_SUBSCRIBE = () => () => {};
const NO_STAMP = () => 0;

export interface PeekLayout {
  open: boolean;
  /** What the peek renders at — its dragged width, or the whole content area
   *  in cover mode. Zero when closed, so the frame is simply full width. */
  width: number;
  cover: boolean;
  /** The width the middle pane's CONTENT stops shrinking at — ¾ of the measured
   *  baseline (design.md, Widths v2). Written onto the frame as a CSS variable. */
  floor: number;
  /** The frame is under that floor: the middle pane scrolls sideways instead of
   *  reflowing any further. */
  floored: boolean;
  /** The frame is narrower than the column plus its gutters — there are no
   *  centred margins left to absorb anything, so the gutters come in. */
  tight: boolean;
  instant: boolean;
}

/**
 * What the Tasks page needs to know to give the peek its room: is it open, and
 * how much is it taking. The frame's width is `calc(100% - <this>)`, which is
 * the one number both halves of the animation are built from.
 */
export function useTaskPeekLayout(enabled = true): PeekLayout {
  const state = useSyncExternalStore(subscribePeek, getPeekState, getPeekState);
  const viewport = useViewportWidth(enabled);
  // THE SIDEBAR IS THE OTHER HALF OF THE CONTENT AREA, and the reader may move
  // it themselves at any moment — the rail's chevron, a drag on its handle.
  // Without this the frame's WIDTH still followed (it is a percentage, so CSS
  // re-resolves it), but `floored` did not: expanding the sidebar under an open
  // peek took the middle pane below its floor and left the views reflowing past
  // it, with no scroller, until the next seam nudge happened to re-render.
  const sidebar = useSyncExternalStore(
    enabled ? subscribeSidebarState : NO_SUBSCRIBE,
    enabled ? sidebarStamp : NO_STAMP,
    enabled ? sidebarStamp : NO_STAMP,
  );
  return useMemo(() => {
    const room = currentRoom();
    if (state.key === null) {
      return {
        open: false,
        width: 0,
        cover: false,
        floor: room.floor,
        floored: false,
        tight: false,
        instant: state.instant,
      };
    }
    return {
      open: true,
      width: room.peekWidth,
      cover: room.cover,
      floor: room.contentFloor,
      floored: room.floored,
      tight: room.tight,
      instant: state.instant,
    };
    // `viewport` is not read directly — `currentRoom()` reads the window — but
    // it is what makes this recompute when the window changes size, which is
    // also what RE-CLAMPS a persisted width that no longer fits (design.md).
  }, [state.key, state.width, state.baseline, state.instant, viewport, sidebar]);
}

/**
 * Own the peek for as long as the Tasks page is mounted: arm `openPeek` for the
 * four views, adopt a `?peek=` deep link, and follow Back/Forward.
 *
 * The sync runs in a LAYOUT effect so a deep-linked peek is in the store before
 * the first paint — `instant: true` then suppresses the slide, which is what
 * "opens with the peek already in place, no slide" means (design.md, URL).
 */
export function useTaskPeekHost(enabled: boolean): void {
  useLayoutEffect(() => {
    if (!enabled) return;
    setPeekHost(true);
    // MEASURE BEFORE ADOPTING THE LINK, and synchronously, in this same layout
    // effect. The page's baseline observer is a passive effect and therefore
    // runs AFTER this one: a `?peek=` deep link used to freeze a baseline of
    // null, and a null that latched made the floor a moving target for the rest
    // of the visit (shell/task-peek-store.ts `freezeBaseline`).
    refreshPeekBaseline();
    const deep = readPeekParam(location.search);
    if (deep) syncPeekFromUrl(location.search);
    return () => setPeekHost(false);
  }, [enabled]);
  useEffect(() => {
    if (!enabled) return;
    const onPop = () => syncPeekFromUrl(location.search);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [enabled]);
  // A WINDOW RESIZE IS ONE OF THE THREE GESTURES that may move the sidebar
  // (design.md, Widths v2 — the seam's drag and its arrows are the other two),
  // and it moves it only when the middle pane crosses its floor.
  useEffect(() => {
    if (!enabled) return;
    const onResize = () => {
      if (getPeekState().key !== null) applyResize();
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [enabled]);
}

// ---- the walk ----------------------------------------------------------------

/**
 * The visible order, read off the frame in DOM order (see PEEK_ITEM_ATTR) —
 * less the items the panel cannot open, which carry `PEEK_SKIP_ATTR` and are
 * dropped by `peekVisibleOrder` (design.md, Fix batch 6 §3). The dedupe and the
 * filter both live in the store, so the DOM read here is the only part of this
 * that needs a browser.
 */
function visibleOrder(): string[] {
  if (typeof document === "undefined") return [];
  return peekVisibleOrder(
    Array.from(document.querySelectorAll(`.tasks-frame [${PEEK_ITEM_ATTR}]`)),
  );
}

/**
 * WHAT A WALK DOES TO THE PAGE BEHIND THE PANEL (design.md, Polish batch 5).
 *
 * Two things, and they are both about not losing the reader:
 *
 *   1. THE ITEM COMES BACK INTO VIEW. The order the panel walks is longer than
 *      the frame showing it, so a ⌃⇧J at the bottom of a list opened a task the
 *      reader could not see — the panel's contents changed and nothing on the
 *      page said which row it was now about. `block: "nearest"` is deliberate:
 *      it does nothing at all when the item is already on screen, so an
 *      ordinary walk down a visible list never jerks the scroller.
 *
 *   2. FOCUS FOLLOWS, BUT ONLY FROM THE FRAME, and it arrives without a ring.
 *      A press that came from the list has to leave focus somewhere the NEXT
 *      arrow can come from, and leaving it on the row the reader clicked two
 *      taps ago left a `:focus-visible` ring burning on a row the panel had
 *      walked away from (the yellow outline Akshil reported). A press that came
 *      from the panel's own chevrons does NOT move focus: the chevron is a
 *      button a keyboard may want to press again, and stealing focus out of it
 *      on the first press is a control that works once.
 *
 * `tabindex="-1"` because the items are containers — the List row's tab stop is
 * the stretched `<a>` inside it, the wall card's is its head — and a container
 * that is not focusable cannot be handed focus at all. `-1` adds no tab stop;
 * it only makes the element a legal target for this one call.
 */
function walkTo(key: string): void {
  if (typeof document === "undefined") return;
  const items = Array.from(
    document.querySelectorAll<HTMLElement>(`.tasks-frame [${PEEK_ITEM_ATTR}]`),
  );
  const el = peekScrollTarget(items, key);
  if (!el) return;
  el.scrollIntoView({ block: "nearest" });
  const active = document.activeElement as HTMLElement | null;
  if (!active || typeof active.closest !== "function" || !active.closest(".tasks-frame")) return;
  if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
  el.setAttribute(PEEK_WALK_ATTR, "");
  el.focus({ preventScroll: true });
  // The mark is about THIS focus and no other: once the element loses it, a
  // later keyboard focus on the same item is the reader's own and is entitled
  // to its ring.
  el.addEventListener("blur", () => el.removeAttribute(PEEK_WALK_ATTR), { once: true });
}

// ---- icons -------------------------------------------------------------------

const ICON = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** CLOSE, and it is the header's FIRST control in every mode (Akshil,
 *  2026-09-14 — design.md, Polish batch 4, item 11). It wore a panel glyph for
 *  a day — "hide the right panel", and in cover "show the list" — which is a
 *  picture of a LAYOUT, and a reader who wants this task off their screen
 *  should not have to work out which layout they are in first. One mark, one
 *  meaning, in the corner every panel in this app puts it. */
const ICON_CLOSE = (
  <svg {...ICON}><path d="M6 6l12 12M18 6L6 18" /></svg>
);
/** OPEN IN EXPLORER — the PREFIX on a word (Akshil, 2026-09-14 — design.md,
 *  Polish batch 5). An arrow out of a box, pointing to the upper right: the
 *  mark the whole web uses for "this leaves the page you are on", which is
 *  exactly what the press does. It leads the label rather than trailing it, the
 *  way every other icon-and-word control in this app is built; the "→" that
 *  used to follow the word said "forward", which is what a Next control says. */
const ICON_OPEN_EXTERNAL = (
  <svg {...ICON} width={13} height={13}>
    <path d="M7 17 17 7" />
    <path d="M8 7h9v9" />
  </svg>
);
/** CHEVRONS, not arrows (design.md, Header + list state v2). Prev/next step
 *  through a list that is on screen; an arrow would promise travel. */
const ICON_UP = (
  <svg {...ICON}><polyline points="18 15 12 9 6 15" /></svg>
);
const ICON_DOWN = (
  <svg {...ICON}><polyline points="6 9 12 15 18 9" /></svg>
);
/** VERTICAL, because it sits at the end of a row rather than in one: the kebab
 *  every list in this app wears (design.md). */
const ICON_DOTS = (
  <svg {...ICON}>
    <circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none" />
  </svg>
);
/** THE ⋮'s OWN door mark — the page's shared open-in-Explorer glyph
 *  (ScheduleTaskViews `ICON_OPEN_FOLDER_PATH`), drawn at the header's weight.
 *  It is no longer in the header itself: the door there is the words "Open →"
 *  now (design.md, Polish batch 4), and this is the menu row that stands in for
 *  it when the header has folded its door away. A MENU row is a label with a
 *  mark beside it, so the mark stays a picture. */
const ICON_OPEN_DOOR = (
  <svg {...ICON}><path d={ICON_OPEN_FOLDER_PATH} /></svg>
);

/** The terminal hand-off's mark — the prompt caret, the one picture of a shell
 *  this app already uses for it. */
const ICON_TERMINAL = (
  <svg {...ICON}><polyline points="5 7 9 11 5 15" /><path d="M12 16h7" /></svg>
);
// ---- the panel ---------------------------------------------------------------

export function TaskPeek({
  tasks,
  loaded = false,
  home = "",
  missing,
  onReload,
}: {
  /** The page's current set — the peek follows the poll like every other view
   *  (a task opened while it was still `pending:<entry>` gets its session id a
   *  second later, and the body cannot frame a chat until it does). */
  tasks: Task[];
  /** Have the page's tasks actually arrived? Until they have, a `?peek=` key
   *  that matches nothing is simply a key that has not been met yet — it is
   *  only once this is true that "no such task" is an answer (`settlePeek`). */
  loaded?: boolean;
  home?: string;
  missing?: ReadonlySet<string>;
  onReload?: () => void;
}) {
  const layout = useTaskPeekLayout();
  const key = usePeekedKey();
  const anchor = usePeekAnchor();
  // THE URL MEETS THE DATA (task-peek-store.settlePeek): a deep link naming a
  // task that is not here closes the panel and drops the param instead of
  // standing open and empty; one naming a task NUMBER is rewritten to that
  // task's row key in place. Runs on every poll, because "not here" can also
  // mean "erased while the panel was open".
  useEffect(() => {
    if (!loaded) return;
    // AN EMPTY LIST IS AMBIGUOUS FOR ONE BEAT. `tasksLoaded` is seeded true
    // from a remembered listing (Scheduled), so the first render of a reload
    // can be "loaded" with `tasks` still `[]` while the fetch is in flight —
    // and settling against that closed a perfectly good deep link a tick
    // before its task arrived. A list with rows in it is an answer now; an
    // empty one is an answer only once it has stayed empty, which is also the
    // honest reading for a machine that genuinely has no tasks.
    if (tasks.length > 0) {
      settlePeek(tasks);
      return;
    }
    const t = setTimeout(() => settlePeek(tasks), PEEK_SETTLE_MS);
    return () => clearTimeout(t);
  }, [loaded, tasks, key]);
  // PARKED: the panel is closed AND the slide has had its time.
  //
  // The stylesheet already delays `visibility: hidden` by the duration of the
  // transform, which is correct when the transition RUNS — but a transition in
  // a background tab may never tick, and one under `prefers-reduced-motion` is
  // 0.01ms with a 200ms delay still in front of it. Either way the panel was
  // left standing visible over the page with its buttons in the tab order. A
  // timer is the guarantee the transition is not: it fires whether or not
  // anything animated (styles/task-peek.css `.is-parked`).
  //
  // NOT `display: none` / `hidden`, deliberately: the closed panel has to keep
  // a painted box at `translateX(100%)`, or the next open has no start value to
  // transition FROM and the slide becomes a jump.
  const [parked, setParked] = useState(true);
  // IS THERE ANYTHING TO RUN IN HERE? A parked panel is off screen for the rest
  // of the visit, and a chat and a live app left mounted behind it go on
  // polling, streaming and painting for all of it — the app at whatever cadence
  // it likes. So the panel keeps its BOX (the slide-out still needs something
  // to animate) and drops its CONTENTS once the slide is over.
  //
  // `layout.open ||` is not redundant: `parked` is turned off by an effect, one
  // commit after the panel opens, and without this the chat would mount a frame
  // late on every open.
  const mounted = layout.open || !parked;
  useEffect(() => {
    if (layout.open) {
      setParked(false);
      return;
    }
    const t = setTimeout(() => setParked(true), PEEK_PARK_MS);
    return () => clearTimeout(t);
  }, [layout.open]);
  const [menuAt, setMenuAt] = useState<{ x: number; y: number } | null>(null);
  /**
   * THE TASK THE DELETE WAS ASKED ABOUT — a SNAPSHOT, not `task` (Bugbot,
   * 78118e0fa). The modal used to read the live `task`, which is whatever the
   * panel is showing NOW; anything that swaps the open task while the
   * confirmation is up — a chevron, ⌃⇧J, an arrow key, an archive advancing the
   * panel, a poll dropping the row — retargeted the confirmation at a task the
   * reader never asked about, under a dialog still spelling out the old one's
   * number. The word "Delete TASK-126" has to keep meaning TASK-126.
   *
   * Non-null IS the open state; there is no separate boolean to fall out of step
   * with it.
   */
  const [erasing, setErasing] = useState<Task | null>(null);
  // THE ORDER TO ADVANCE ALONG, read when the delete is asked for, not when it
  // has happened: by `onDone` the poll may already have dropped the row, and
  // `nextAfterRemoval` on a list that no longer holds the task closes the panel
  // instead of moving on (same rule as the archive path below).
  const eraseOrder = useRef<readonly string[]>([]);
  const [acting, setActing] = useState(false);
  // The last task we HELD, kept for the closing animation: the panel stays in
  // the DOM while it slides out, and an empty panel sliding away reads as a
  // bug. Also the fallback for a task that leaves the set while it is open
  // (archived from the ⋯ menu, or filtered away) — the conversation in it is
  // real, and pulling it out from under the reader is not a close.
  const held = useRef<Task | null>(null);
  const live = key === null ? null : (tasks.find((t) => t.key === key) ?? null);
  if (live) held.current = live;
  const task = key === null ? held.current : (live ?? held.current);
  // …and the width it was sliding at, for the same reason: the panel keeps its
  // size on the way out rather than snapping to whatever the closed state
  // computes and then sliding.
  const heldWidth = useRef(layout.width);
  if (layout.open && layout.width) heldWidth.current = layout.width;

  const folder = task ? taskFolder(task) : "";
  const gone = !!task && !!missing?.has(folder);
  const templates = useChatTemplates(task && !gone ? [task.target || task.project] : []);
  const template = task ? templates[task.target || task.project] : undefined;
  const src =
    task && task.session_id && template && !gone
      // `_nofocus=1`, the shell's embedded-frame focus contract
      // (platform/lib/frame-focus): without it the chat template focuses its
      // own composer ~300ms after boot, which pulls the keyboard out of this
      // page and takes ⌃⇧J / ⌃⇧K / Esc with it. A wrapper at the HOST, which is
      // where every other framing site applies it (legacy-src.ts's header).
      // `noFocus` below is the same fact for the native branch.
      ? withNoFocus(
          peekFrameSrc(template, task.target || task.project, task.session_id,
                       anchor ?? undefined,
                       // The task's own model/effort, so the FLAG-OFF frame
                       // opens on them too — the native branch seeds the same
                       // two through `ChatMount`. Both "" for a task that chose
                       // neither, which appends nothing.
                       { model: task.model, effort: task.effort }),
        )
      : null;
  const resolving = !src && !gone && !!task?.session_id && template === undefined;

  // ---- the app preview -------------------------------------------------------
  // THE APP THIS TASK IS ABOUT, if its folder is one (shell/peek-preview.ts:
  // the desk's own table, never a second predicate). Null on an ordinary
  // project, and the body is then exactly what it was — chat, full height.
  const app = useAppForProject(task && !gone ? task.project : "");
  const previewSrc = app?.entry
    ? // The SAME document the app page's Overview frames and the Home card
      // opens, plus the shell's embedded-frame focus contract: an app that
      // focused an input on boot would take ⌃⇧J and ↑/↓ away from the panel
      // around it (platform/lib/frame-focus).
      withNoFocus(`/render?path=${encodeURIComponent(app.entry)}`)
    : null;
  const dragged = useSyncExternalStore(
    subscribePreviewHeight,
    getPreviewHeight,
    getPreviewHeight,
  );
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [bodyHeight, setBodyHeight] = useState(0);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const read = () => setBodyHeight(el.getBoundingClientRect().height);
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [key]);
  const box = previewBox(layout.width, bodyHeight, dragged);
  const showPreview = mounted && !!previewSrc && box.height >= PREVIEW_MIN_H;
  // Three states and four events (shell/peek-preview.ts `previewLoad`), not two
  // booleans: the transitions that matter are the ones that must not happen.
  const [load, setLoad] = useState<PreviewLoad>("waiting");
  const stepLoad = (event: "src" | "load" | "error" | "timeout") =>
    setLoad((cur) => previewLoad(cur, event));
  useEffect(() => {
    setLoad(previewLoad("ready", "src"));
  }, [previewSrc]);
  // AND A CLOCK ON IT. `onError` is not a promise an iframe keeps: a `/render`
  // document that hangs, or that boots into its own error page, loads
  // "successfully" and fires nothing — so the skeleton had no way to end. After
  // `PREVIEW_LOAD_TIMEOUT_MS` the strip says so in one muted line and the
  // conversation below is unaffected, which is the honest failure.
  useEffect(() => {
    if (!previewSrc || load !== "waiting") return;
    const t = setTimeout(() => stepLoad("timeout"), PREVIEW_LOAD_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [previewSrc, load]);
  const previewFailed = load === "failed";
  const previewReady = load === "ready";
  const page = task ? (gone ? null : (taskHref(task) ?? folderHref(task))) : null;

  const openAsPage = useCallback(() => {
    if (!page) return;
    // NO PUSH ON THE WAY OUT: the navigation below pushes its own entry, and a
    // `/tasks` entry pushed a tick before it would make one Back land on the
    // Tasks page with the peek already gone — the reader would have to press it
    // twice to get the panel back.
    closePeek({ push: false });
    navigateUrl(page);
  }, [page]);

  const order = useCallback(() => visibleOrder(), []);
  /** Walk one task. ANSWERS WHETHER IT WALKED, which the bare arrow keys spend:
   *  at the ends of the list there is nowhere to go, and a key that is
   *  swallowed there is a key the page's own scroll never gets — the panel
   *  would be eating ↓ at the last task to do nothing with it. */
  const step = useCallback(
    (delta: number): boolean => {
      const next = stepPeekKey(order(), getPeekState().key, delta);
      if (!next) return false;
      openPeek(next);
      // …and the page behind the panel keeps up: the new item comes back into
      // view, and focus follows it without a ring (`walkTo`).
      walkTo(next);
      return true;
    },
    [order],
  );
  // Whether the arrows have anywhere to go, re-read whenever the open task or
  // the set changes — the walk is the DOM's, so it cannot be memoised on props.
  const [ends, setEnds] = useState<{ prev: boolean; next: boolean }>({ prev: false, next: false });
  useEffect(() => {
    if (key === null) return;
    const list = visibleOrder();
    setEnds({
      prev: stepPeekKey(list, key, -1) !== null,
      next: stepPeekKey(list, key, 1) !== null,
    });
  }, [key, tasks]);

  // THE HEADER'S OWN FIT LADDER (shell/row-fit.ts `PEEK_HEAD_DROPS`): measured,
  // never a breakpoint, and armed only while the panel is actually up.
  const [headFit, headRef] = useStripFit(PEEK_HEAD_DROPS, key !== null);

  // ---- the keyboard ----------------------------------------------------------
  /**
   * WHAT ESCAPE MEANS IN THE PANEL: blur a composer that has words in it, and
   * close only once it has not. Losing a half-typed message to a stray Escape
   * is exactly the kind of thing an escape hatch must not do (design.md,
   * Keyboard).
   *
   * ITS OWN FUNCTION BECAUSE THE NATIVE CHAT REACHES IT FROM THE OTHER SIDE.
   * The chat answers Escape in its own document listener and hands the press up
   * through `onEscape` once it has nothing of its own open — which it does
   * BEFORE `peekKey` ever sees the event, so a panel wired straight to
   * `closePeek` there shut on the first press with the composer full (Bugbot,
   * PR #1133). Both routes spend this one rule instead.
   */
  const escapeOrBlur = useCallback((doc: Document) => {
    const el = doc.activeElement as HTMLElement | null;
    const typing =
      el &&
      (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable) &&
      !!(el as HTMLTextAreaElement).value;
    if (typing) {
      el.blur();
      return;
    }
    closePeek();
  }, []);

  /**
   * THE PEEK'S KEYS, in one function because they have to be answered in
   * TWO documents: this one, and — flag off — the legacy chat's, which is a
   * separate document whose keystrokes this page never hears (see the frame
   * effect below). `doc` is whichever document the press happened in, because
   * "is a composer holding words" is a question about that document's own
   * `activeElement` and not about ours.
   *
   * Returns whether the press was spent, so each listener can decide what to do
   * with one that was not.
   */
  const peekKey = useCallback(
    (e: KeyboardEvent, doc: Document): boolean => {
      if (e.defaultPrevented) return false;
      if (e.key === "Escape") {
        // A DIALOG OVER THE PANEL OWNS THE PRESS. The modal chassis peels one
        // layer per Esc through its own stack (platform/ui/modal/esc-stack),
        // but this panel is not a modal and holds no token in it — so with the
        // New task card up over an open peek, one press closed the card AND
        // the peek (the chassis never marks the event spent). Standing down
        // while any dialog is registered is the one rule that makes the peek
        // the layer UNDER every dialog, which is what it is on screen.
        if (anyModalOpen()) return false;
        e.preventDefault();
        escapeOrBlur(doc);
        return true;
      }
      if (e.ctrlKey && e.shiftKey && !e.metaKey && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "j") {
          e.preventDefault();
          step(1);
          return true;
        }
        if (k === "k") {
          e.preventDefault();
          step(-1);
          return true;
        }
      }
      // THERE IS NO COMMAND-ENTER ANY MORE (Akshil, 2026-09-14 — design.md,
      // Polish batch 5). It opened the task as a page, and it was a chord with
      // no mark on it anywhere except the tooltip of the button that does the
      // same thing one press away — while the same chord in this app's other
      // half SUBMITS (the New task modal), which is the opposite of leaving.
      // The header's Open door and the row's own are the way out, and they say
      // so in words.
      // ↑/↓ WALK THE LIST, and they do it BARE (Akshil, 2026-09-14 — design.md,
      // Polish batch 4). ⌃⇧J/K above stay exactly as they were; this is the
      // gesture a reader scanning a list with a panel open actually reaches
      // for.
      //
      // TWO GUARDS, and they answer two different questions. `doc === document`
      // is WHICH DOCUMENT: the app preview and the legacy chat attach this very
      // listener in documents of their own, and in there the arrows belong to
      // the app and to the conversation — a chat whose message list stopped
      // scrolling by arrow because the panel around it had taken the key would
      // be the worse bargain. `arrowShouldWalk` is WHAT IS FOCUSED in ours: a
      // field, a select, a menu, or the frame element itself.
      //
      // AND `preventDefault` ONLY WHEN IT WALKS — which includes the ENDS of
      // the list. `step` answers whether it moved, and at the first or last
      // task it does not: a key eaten there is a key the page's own scroll
      // never gets, for a walk that did nothing. An arrow this panel declines,
      // for whatever reason, has to reach whatever would have had it.
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !e.metaKey && !e.ctrlKey &&
          !e.altKey && !e.shiftKey && doc === document && arrowShouldWalk(e.target)) {
        if (!step(e.key === "ArrowDown" ? 1 : -1)) return false;
        e.preventDefault();
        return true;
      }
      return false;
    },
    [step, escapeOrBlur],
  );

  // One listener on THIS document while the peek is open.
  useEffect(() => {
    if (key === null) return;
    const onKey = (e: KeyboardEvent) => {
      peekKey(e, document);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [key, peekKey]);

  // THE SAME KEYS, FROM INSIDE THE LEGACY FRAME. Flag off the chat is another
  // document, and every key the reader presses in it fires there — where the
  // listener above cannot hear it. `_nofocus=1` (below) stops the template
  // TAKING the keyboard on its own, but the reader is entitled to click into
  // the chat and type, and ⌃⇧J / ⌃⇧K / Esc have to keep working when they do.
  // Same origin, so the frame's document takes a listener of its own; a key the
  // template already stopped never reaches it, which is the right precedence.
  // Flag ON there is no frame and no second document.
  //
  // EVERY DOCUMENT IT ATTACHES TO IS TRACKED. The effect attaches at mount AND
  // on every `load`, so an iframe that navigates more than once had one live
  // listener per document and the cleanup removed only the last — the earlier
  // documents kept a closure over a stale `step`/`openAsPage` for as long as
  // they were alive.
  //
  // KEYED ON THE FLAG, and that is not decoration. The iframe does not exist on
  // the render that mounts this panel: `ChatMount` holds a placeholder while
  // the `native_chat_enabled` read is in flight and only then renders the
  // frame. Without the flag in the deps, nothing this effect watches changed
  // between "no frame" and "frame", so it ran once against a null ref and never
  // again — the listener was simply never attached, and every shortcut pressed
  // inside the chat was lost (including Esc).
  const previewRef = useRef<HTMLIFrameElement | null>(null);
  const nativeChat = useNativeChatFlag();
  // THE PARAM BOUNDARY, and the whole reason the panel showed the chat
  // template's HOME screen instead of the task's conversation: `fused.params`
  // inside the frame climbs to the topmost same-origin ancestor unless a window
  // says stop, so the template read `/tasks` — which carries no `session_id` —
  // rather than the `session_id` in its own `src`. The Cards wall marks the
  // window for exactly this reason; the List, the Board and the Calendar never
  // had a framed chat before, so nothing marked it for them.
  //
  // Held through the shared COUNT (platform/lib/param-boundary) so this and the
  // Cards wall can both be up without one's unmount unmarking the other's.
  useParamBoundary(nativeChat === false && !!src);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  useEffect(() => {
    if (key === null) return;
    // `false` is the legacy branch; `null` is "not asked yet" and `true` has no
    // second document to listen in.
    if (nativeChat !== false) return;
    const frame = frameRef.current;
    if (!frame) return;
    const seen = new Set<Document>();
    const onKey = (e: KeyboardEvent) => {
      const doc = (e.target as Node | null)?.ownerDocument;
      peekKey(e, doc ?? document);
    };
    const attach = () => {
      try {
        const doc = frame.contentDocument;
        if (!doc || seen.has(doc)) return;
        seen.add(doc);
        doc.addEventListener("keydown", onKey);
      } catch {
        /* not ours — /render is same-origin, but a listener is not worth a throw */
      }
    };
    frame.addEventListener("load", attach);
    attach();
    return () => {
      frame.removeEventListener("load", attach);
      for (const doc of seen) doc.removeEventListener("keydown", onKey);
      seen.clear();
    };
  }, [key, src, peekKey, nativeChat]);

  // ---- the seam --------------------------------------------------------------
  const panelRef = useRef<HTMLElement | null>(null);
  /** The area the frame and the peek share — what every width rule is a share
   *  of. Measured off the host rather than off `innerWidth`, so the sidebar's
   *  real width (rail, dragged, or hidden by the ≤700px media rule) is counted
   *  once, by the layout, instead of being guessed at twice. */
  const contentWidth = () =>
    panelRef.current?.parentElement?.getBoundingClientRect().width ?? 0;
  const nudge = (delta: number) => {
    const content = contentWidth();
    if (!content) return;
    // Read the width from the STORE, not from this render's `layout`: a held
    // arrow key fires several times between paints, and every press after the
    // first would otherwise start again from the same stale number and the
    // panel would move ten pixels however long the key was held.
    //
    // THE DRAGGED NUMBER, not the RENDERED one, and in cover mode they differ:
    // the panel renders at the whole content area there, so stepping down from
    // what is on screen took ten pixels off the AREA every press and never off
    // the panel — a seam that did nothing (Bugbot, PR #1138). `state.width` is
    // the number the reader actually built.
    const from = getPeekState().width ?? currentRoom().peekWidth;
    setPeekWidth(clampPeekWidth(from + delta, content));
    applyResize();
  };
  const onSeamPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const seam = e.currentTarget;
    seam.setPointerCapture(e.pointerId);
    seam.classList.add("dragging");
    // SUSPEND THE TRANSITIONS FOR THE WHOLE DRAG (design.md): with them on, the
    // panel's edge chases the cursor 200ms behind it — the same reason the
    // sidebar's own handle sets `sidebar-no-transition`.
    document.body.classList.add("tasks-peek-dragging");
    let settled = 0;
    const onMove = (ev: PointerEvent) => {
      const host = panelRef.current?.parentElement;
      if (!host) return;
      const rect = host.getBoundingClientRect();
      settled = clampPeekWidth(rect.right - ev.clientX, rect.width);
      setPeekWidth(settled, false);
      // THE CROSSING IS CHECKED ON EVERY MOVE, not only on the drop: the
      // sidebar has to get out of the way WHILE the seam is travelling, which
      // is the whole gesture Akshil described (drag wider → the sidebar tucks
      // away). `planCrossing` fires only on a change of side, so the several
      // hundred calls a drag makes cost one decision between them.
      applyResize();
    };
    const onUp = () => {
      seam.classList.remove("dragging");
      document.body.classList.remove("tasks-peek-dragging");
      seam.removeEventListener("pointermove", onMove);
      seam.removeEventListener("pointerup", onUp);
      seam.removeEventListener("pointercancel", onUp);
      // Only a drag that MOVED records a width — a bare click on the seam
      // leaves the panel exactly where it was (usePreviewPane's own rule).
      if (settled) setPeekWidth(settled);
      applyResize();
    };
    seam.addEventListener("pointermove", onMove);
    seam.addEventListener("pointerup", onUp);
    seam.addEventListener("pointercancel", onUp);
  };

  // THE SAME KEYS AGAIN, FROM INSIDE THE APP. The preview is a third document
  // and the reader may well be clicking about in it; ⌃⇧J / ⌃⇧K / Esc have
  // to keep meaning the panel's things there too.
  //
  // AND THE APP GETS FIRST REFUSAL: `peekKey` stands down on a press the app
  // already spent (`defaultPrevented`), so an Escape that closed the app's own
  // dialog does not also close the peek around it (design.md, Never broken).
  useEffect(() => {
    if (key === null || !previewSrc) return;
    const frame = previewRef.current;
    if (!frame) return;
    const seen = new Set<Document>();
    const onKey = (e: KeyboardEvent) => {
      peekKey(e, (e.target as Node | null)?.ownerDocument ?? document);
    };
    const attach = () => {
      try {
        const doc = frame.contentDocument;
        if (!doc || seen.has(doc)) return;
        seen.add(doc);
        doc.addEventListener("keydown", onKey);
      } catch {
        /* an app served from elsewhere — nothing to listen in */
      }
    };
    frame.addEventListener("load", attach);
    attach();
    return () => {
      frame.removeEventListener("load", attach);
      for (const doc of seen) doc.removeEventListener("keydown", onKey);
      seen.clear();
    };
  }, [key, previewSrc, previewReady, peekKey]);

  // ---- the preview's seam ----------------------------------------------------
  // The horizontal twin of the panel's own edge: same 12px hit area, same 1px
  // ink, same suspension of transitions while it is captured. What it moves is
  // the preview's height, and every clamp it spends is `previewBox`'s — so the
  // drag can never take the composer off screen (design.md, Never broken).
  const nudgePreview = (delta: number) => {
    // From the STORE, not from this render's `box` — the vertical seam's rule
    // for the same reason: a held arrow fires several times between paints, and
    // every press after the first would otherwise start again from the same
    // stale height and the seam would move ten pixels however long the key was
    // held. `?? box.height` is the first press, before there is a stored one.
    const from = getPreviewHeight() ?? box.height;
    setPreviewHeight(previewBox(layout.width, bodyHeight, from + delta).height);
  };
  const onPreviewSeamDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const seam = e.currentTarget;
    seam.setPointerCapture(e.pointerId);
    seam.classList.add("dragging");
    document.body.classList.add("tasks-peek-dragging");
    const top = bodyRef.current?.getBoundingClientRect().top ?? 0;
    const onMove = (ev: PointerEvent) => {
      setPreviewHeight(previewBox(layout.width, bodyHeight, ev.clientY - top).height);
    };
    const onUp = () => {
      seam.classList.remove("dragging");
      document.body.classList.remove("tasks-peek-dragging");
      seam.removeEventListener("pointermove", onMove);
      seam.removeEventListener("pointerup", onUp);
      seam.removeEventListener("pointercancel", onUp);
    };
    seam.addEventListener("pointermove", onMove);
    seam.addEventListener("pointerup", onUp);
    seam.addEventListener("pointercancel", onUp);
  };

  // ---- the ⋮ menu ------------------------------------------------------------
  /**
   * WHERE THE PANEL GOES AFTER THE TASK IN IT IS FILED OR DELETED.
   *
   * Read the visible order BEFORE the act (shell/task-peek-store.ts
   * `nextAfterRemoval` states why), then move: next down, previous if this was
   * the last, close only if there is nothing else on the page at all. Filing is
   * a SWEEP — you work down a column clearing it — and a panel that closed on
   * every archive made the reader re-open the next one by hand (design.md,
   * Header + list state v2).
   *
   * The halo comes along by itself: it is `openPeek`'s, not this function's.
   */
  const advancePast = (removed: string, visible: readonly string[]) => {
    const next = nextAfterRemoval(visible, removed);
    if (next) openPeek(next);
    else closePeek();
  };

  const filing = task ? filingIntent(task) : null;
  const refile = async () => {
    if (!filing || !task || acting) return;
    const from = task.key;
    setActing(true);
    // Captured before the await: the poll that follows `onReload` is what takes
    // the row away, and by then the order has already moved on.
    const visible = order();
    try {
      if (filing.kind === "archive") await archiveTask(task.key);
      else await unarchiveTask(task.key);
      onReload?.();
      advancePast(from, visible);
    } catch (e) {
      notify({ title: (e as Error).message, tone: "error" });
    } finally {
      setActing(false);
    }
  };

  /**
   * CONTINUE THIS TASK IN A REAL TERMINAL — the chat's own door, not a new one
   * (`@apps/claude/ui/Kebab`'s `onTerminal`): ask the folder's `agent.py` for
   * the exact `claude --resume …` line and put it on the clipboard. There is no
   * API here for launching a terminal — the app cannot open one — so what the
   * act actually does is hand the reader the command, which is what it does
   * everywhere else it is offered.
   *
   * Needs the template's folder, which this panel has already resolved for the
   * chat it is framing (`template`), so no second stat.
   */
  const agentDir = template ? template.slice(0, template.lastIndexOf("/")) : null;
  const toTerminal = async () => {
    if (!task || !agentDir) return;
    try {
      const out = await runAgent(
        agentDir,
        "terminal_command",
        { file: task.target || task.project, session_id: task.session_id ?? "" },
        { key: null },
      );
      if ("error" in out && out.error) throw new Error(out.error);
      if (!("command" in out)) throw new Error("agent.py returned no command");
      const ok = await copyToClipboard(out.command);
      notify({
        title: ok ? "Command copied — paste it in your terminal" : "Could not copy the command",
        tone: ok ? "info" : "error",
      });
    } catch (e) {
      notify({ title: (e as Error).message, tone: "error" });
    }
  };

  /**
   * THREE ITEMS, and the list is the whole menu (design.md, Header + list state
   * v2): continue in a terminal, file it, delete it. "Open as page" and "Copy
   * link" are gone from here — the first is now a control of its own in the
   * header, and the second was a menu row nobody could find for an act the
   * address bar already does.
   *
   * AND SO IS "Close" (design.md, Polish batch 4). It was here because the
   * header's first control had become a panel glyph that meant "show the list"
   * in cover mode, leaving a covered page with no × on it. The × is back, in
   * every mode, so a "Close" row in a menu is a second way to do the thing the
   * corner of the panel already does.
   *
   * A FOURTH appears only when the header has had to fold its own door away
   * (`headFit`): a hidden control has to be somewhere, and the kebab is where.
   */
  const menuItems = (): MenuEntry[] => {
    if (!task) return [];
    const items: MenuEntry[] = [];
    if (page && headFit >= PEEK_HEAD_DROPS.length) {
      items.push({ label: "Open in Explorer", icon: ICON_OPEN_DOOR, onClick: openAsPage });
      items.push("separator");
    }
    if (gone) {
      items.push({
        label: "Open in Explorer",
        icon: ICON_OPEN_DOOR,
        disabled: true,
        onClick: () => notify({ title: MISSING_FOLDER_TOAST, tone: "error" }),
      });
      items.push("separator");
    }
    items.push({
      label: "Continue this task in terminal",
      icon: ICON_TERMINAL,
      // No session and no template means there is no command to hand over —
      // said by the row rather than by a toast after the press.
      disabled: !agentDir || !task.session_id,
      onClick: () => void toTerminal(),
    });
    if (filing) {
      items.push({
        label: filing.kind === "archive" ? "Archive task" : "Unarchive task",
        icon: filing.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE,
        disabled: acting,
        onClick: () => void refile(),
      });
    }
    // A menu row cannot carry a hint, so the DISABLED one says why in its own
    // words — nobody should meet the server's refusal for the first time inside
    // a confirmation (the card door's rule, tasks-lib.eraseBlocked).
    const blocked = eraseBlocked(task);
    items.push({
      label: blocked ? `Delete task — ${ERASE_BLOCKED_HINT.toLowerCase()}` : "Delete task",
      icon: ICON_TRASH,
      danger: true,
      disabled: blocked,
      onClick: () => {
        eraseOrder.current = order();
        // Both facts captured at the PRESS: the order to walk afterwards, and
        // the task the dialog is about.
        setErasing(task);
      },
    });
    return items;
  };

  // The same line the row and the chat header print (TaskPeekWho).
  const title = useTaskHeadline(task);

  return (
    <>
      <aside
        ref={panelRef}
        className={
          "task-side-peek" +
          (layout.open ? " is-open" : "") +
          (layout.cover ? " is-cover" : "") +
          (layout.instant ? " is-instant" : "") +
          (!layout.open && parked ? " is-parked" : "")
        }
        style={{ width: layout.open ? layout.width : heldWidth.current }}
        aria-hidden={layout.open ? undefined : true}
        role="complementary"
        aria-label={task ? `${shortTaskId(task.task_id)} ${title}` : "Task"}
      >
        {/* The seam: a 1px line in a 12px hit area, straddling the panel's
            leading edge exactly as the sidebar's handle straddles its border
            (styles/sidebar.css's argument about hit area vs ink applies here
            verbatim). Double-click is the way back to the default. */}
        <div
          className="task-side-peek-seam"
          role="separator"
          tabIndex={0}
          aria-orientation="vertical"
          aria-label="Resize the task panel"
          // A REAL SLIDER, not a mouse-only edge: the seam is the one control
          // here a pointer would otherwise own outright (design.md, Width &
          // resize). The values are the same clamp the drag spends.
          aria-valuenow={Math.round(layout.width)}
          aria-valuemin={PEEK_MIN_WIDTH}
          // NO MAXIMUM BUT THE AREA ITSELF (design.md, Widths v2): past the
          // cover threshold the panel simply is the content area, and a slider
          // that claimed a smaller ceiling would be describing a stop that is
          // not there.
          aria-valuemax={Math.round(contentWidth()) || undefined}
          onPointerDown={onSeamPointerDown}
          onKeyDown={(e) => {
            // LEFT WIDENS: the panel's leading edge moves left, which is what
            // the arrow is pointing at — the seam, not the panel.
            if (e.key === "ArrowLeft") {
              e.preventDefault();
              nudge(PEEK_KEY_STEP);
            } else if (e.key === "ArrowRight") {
              e.preventDefault();
              nudge(-PEEK_KEY_STEP);
            }
          }}
          onDoubleClick={() => {
            resetPeekWidth();
            applyResize();
          }}
        />
        {/* ONE LINE, LEFT TO RIGHT: the panel's own controls, then WHOSE
            panel it is (status · number · title), then what can be done with
            it (design.md, Header + list state v2). The middle is the only part
            that flexes, so the title is what gives first — everything either
            side is a fixed mark and a mark that ellipsises is a mark that lies.

            `data-fit` is measured, never a breakpoint (shell/row-fit.ts): at
            the narrowest widths the project name goes, then the Open door —
            and the door reappears in the ⋮ so the act is never unreachable. */}
        <header className="task-side-peek-head" ref={headRef} data-fit={headFit}>
          <div className="task-side-peek-acts">
            {/* CLOSE, IN EVERY MODE (Akshil, 2026-09-14 — design.md, Polish
                batch 4, item 11). This was a panel glyph whose meaning changed
                with the layout — "hide the right panel", and in cover "show the
                list" — so the one control every reader reaches for first was
                the one control they had to decode. A × in the panel's leading
                corner is the same promise every other panel in this app makes,
                and it no longer has to live in the ⋮ as well. */}
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Close the task panel"
              data-hint="Close · Esc"
              onClick={() => closePeek()}
            >
              {ICON_CLOSE}
            </button>
            {/* NO "RESIZE PANEL" ANY MORE (Akshil, 2026-09-15). The way out of
                cover is the sidebar: expanding it by hand hands the panel back
                to its default split (task-peek-store `expandUncovers`), and the
                × closes. A third control for a state two others already leave
                was one more thing to read. */}
            {/* CHEVRONS, not arrows (design.md): prev/next here walk a list the
                reader can see, one step at a time — the gesture a chevron means
                everywhere else in this app. A full arrow is for travel. */}
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Previous task"
              data-hint="Previous task · ↑"
              disabled={!ends.prev}
              onClick={() => step(-1)}
            >
              {ICON_UP}
            </button>
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Next task"
              data-hint="Next task · ↓"
              disabled={!ends.next}
              onClick={() => step(1)}
            >
              {ICON_DOWN}
            </button>
          </div>
          {task ? (
            <TaskPeekWho task={task} />
          ) : (
            <span className="task-side-peek-idle" aria-hidden />
          )}
          {task && (
            <div className="task-side-peek-marks">
              {/* THE PROJECT IS A FACT, NOT A DOOR (Akshil, 2026-09-14 —
                  design.md, Polish batch 4). It was a folder mark and a name
                  wired to the folder's Explorer page, sitting an inch from a
                  second door — with a folder glyph on it — that went somewhere
                  else. Two doors, two folder pictures, one header: the reader
                  had to learn which folder each one meant. So the name goes
                  back to being what it is, the label of the project this task
                  runs in, and the header keeps exactly ONE way out.

                  The whole argument, and the markup, are in `TaskPeekWho.tsx`
                  now — the chat's header draws this same chip. */}
              <TaskPeekProject task={task} home={home} />
              {/* "↗ OPEN" — a WORD behind a MARK (design.md, Polish batches 4
                  and 5). The folder glyph it replaced was the page's one door
                  picture, which is a good rule everywhere except beside a
                  folder NAME, where it read as "open that folder" and did not;
                  the label says the act, and the external-link arrow in front
                  of it says the press leaves this page. The icon LEADS, the way
                  every other icon-and-word control in this app is built, and
                  the outline around the pair is what makes it read as a button
                  rather than as more of the header's ink.

                  A real link with a real href, so ⌘-click opens a tab, exactly
                  like the row's own door. */}
              {page && (
                <a
                  className="task-side-peek-open"
                  href={page}
                  aria-label="Open in Explorer"
                  data-hint="Open in Explorer"
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    openAsPage();
                  }}
                >
                  {ICON_OPEN_EXTERNAL}
                  Open
                </a>
              )}
              <button
                type="button"
                className="task-side-peek-btn task-side-peek-kebab"
                aria-label="More actions"
                data-hint="More actions"
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  setMenuAt({ x: r.right - 200, y: r.bottom + 4 });
                }}
              >
                {ICON_DOTS}
              </button>
            </div>
          )}
        </header>

        <div className="task-side-peek-body" ref={bodyRef}>
          {showPreview && previewSrc && (
            <>
              {/* THE APP, LIVE (design.md, App preview in the peek). The frame
                  lays out at a virtual 1280×720 and is scaled to CONTAIN in the
                  card — the smaller of the width fit and the height fit — so
                  the app sees a desktop window however narrow the peek is, the
                  aspect never bends, and whichever axis has room to spare shows
                  padding around the frame (shell/peek-preview.ts `previewBox`). */}
              <div className="task-side-peek-preview" style={{ height: box.height }}>
                {previewFailed ? (
                  <p className="task-side-peek-preview-off" role="status">
                    Preview unavailable
                  </p>
                ) : (
                  // THE CARD (Akshil, 2026-09-15): the same bordered, rounded
                  // well the Home grid's app cards use for their thumbs, minus
                  // the card's head row — the app is framed as something the
                  // panel is showing, and the frame answers a hover the way
                  // those cards do (styles/task-peek.css). The scroller is the
                  // card's child so the border never scrolls with the crop.
                  <div
                    className="task-side-peek-preview-card"
                    // THE CARD IS THE FRAME'S SIZE, not the box's (Akshil,
                    // 2026-09-16): the border hugs the scaled 16:9 frame, and
                    // whatever the box has to spare on either axis is MARGIN
                    // around the card (styles/task-peek.css centres it), not
                    // padding inside it — a card with a band of its own
                    // background beside the app is not a card of the app.
                    style={{
                      width: PREVIEW_VW * box.scale,
                      height: box.frameHeight * box.scale,
                    }}
                  >
                    <div className="task-side-peek-preview-scroll">
                    <div
                      className="task-side-peek-preview-scale"
                      // The scaled frame's real footprint; `transform` does
                      // not affect layout, so the wrapper states the size.
                      style={{
                        width: PREVIEW_VW * box.scale,
                        height: box.frameHeight * box.scale,
                      }}
                    >
                      <iframe
                        ref={previewRef}
                        className="task-side-peek-preview-frame"
                        src={previewSrc}
                        title={app ? `${app.name} preview` : "App preview"}
                        style={{
                          width: PREVIEW_VW,
                          height: box.frameHeight,
                          transform: `scale(${box.scale})`,
                        }}
                        onLoad={() => stepLoad("load")}
                        onError={() => stepLoad("error")}
                      />
                    </div>
                    {!previewReady && (
                      <div className="task-side-peek-preview-wait" aria-hidden>
                        <SkeletonLines rows={2} label="Loading the app" />
                      </div>
                    )}
                    </div>
                  </div>
                )}
              </div>
              <div
                className="task-side-peek-hseam"
                role="separator"
                tabIndex={0}
                aria-orientation="horizontal"
                aria-label="Resize the app preview"
                aria-valuenow={Math.round(box.height)}
                aria-valuemin={PREVIEW_MIN_H}
                // What the drag may take, which is everything the composer does
                // not need (shell/peek-preview.ts `PREVIEW_CHAT_MIN`).
                aria-valuemax={Math.max(PREVIEW_MIN_H, Math.round(bodyHeight - PREVIEW_CHAT_MIN))}
                onPointerDown={onPreviewSeamDown}
                onKeyDown={(e) => {
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    nudgePreview(-PREVIEW_KEY_STEP);
                  } else if (e.key === "ArrowDown") {
                    e.preventDefault();
                    nudgePreview(PREVIEW_KEY_STEP);
                  }
                }}
              />
            </>
          )}
          <div className="task-side-peek-chat">
            {src && task && mounted ? (
              // KEYED ON THE TASK, which is what makes a SWAP a swap: the panel
              // itself never re-slides (design.md, Peek body), and the new
              // conversation arrives behind ChatFrame's own 3-line shimmer —
              // the same cover the Cards wall and the explorer sidebar use —
              // because a remount restarts its wait.
              <ChatMount
                key={task.key}
                legacySrc={src}
                legacyFrameRef={frameRef}
                className="task-peek-frame"
                title={`${shortTaskId(task.task_id)} ${title}`}
                file={task.target || task.project}
                sessionId={task.session_id}
                // WHAT THIS TASK IS SET TO (Akshil, 2026-09-18: "I saw the
                // sidebar peek — the values there were different", then "what I
                // select as a user stays").
                //
                // A SEED, AND IT STANDS DOWN BY ITSELF. `/api/tasks` answers
                // this pair from the conversation's own record where it has one
                // and from the task's entry where it does not (`_row_settings`),
                // and the composer ranks that record above the params these two
                // become — so seeding is the right answer for the window before
                // the first run, and is outranked the moment the chat has one of
                // its own. Gating it on `!task.session_id` was the earlier
                // attempt at that and was too coarse: a task whose session
                // existed but whose transcript had not been written yet got no
                // seed and no record, and detection answered with a neighbour
                // chat's model.
                model={task.model}
                effort={task.effort}
                // ONE TURN TO LAND ON, when the press that opened this was a
                // message row rather than a task row (task-peek-store
                // `PeekState.anchor`). Absent, the conversation opens where a
                // conversation opens: at the end.
                {...(anchor ? { msgAnchor: anchor } : {})}
                chatOnly
                peek
                paramsSource="memory"
                // NOT `closePeek` directly: the chat hands Escape up before
                // this panel's own listener sees it, so the blur-first rule has
                // to be applied here too (see `escapeOrBlur`).
                onEscape={() => escapeOrBlur(document)}
                // FOCUS STAYS ON THE TRIGGER (design.md, Keyboard — Notion's
                // behaviour): the peek is a place to look first and type
                // second, and a panel that steals the caret makes the next
                // arrow key scroll the chat instead of the list.
                noFocus
              />
            ) : resolving ? (
              <div className="task-side-peek-wait">
                <SkeletonLines rows={3} label="Loading the conversation" />
              </div>
            ) : task ? (
              <p
                className={
                  "task-card-starting" + (emptyPaneFailed(task, gone) ? " is-missing" : "")
                }
              >
                {emptyPaneText(task, gone)}
              </p>
            ) : null}
          </div>
        </div>
      </aside>
      {menuAt && (
        <ContextMenu x={menuAt.x} y={menuAt.y} items={menuItems()} onClose={() => setMenuAt(null)} />
      )}
      {erasing && (
        <EraseTaskModal
          // THE CAPTURED TASK, every time it is named — the prop, the toast and
          // the advance. `task` is the panel's current one and may no longer be
          // this one by the time the reader presses Delete (Bugbot, 78118e0fa).
          task={erasing}
          onClose={() => setErasing(null)}
          onDone={() => {
            const erased = erasing;
            setErasing(null);
            notify({ title: `Deleted ${shortTaskId(erased.task_id)}`, tone: "info" });
            onReload?.();
            // SAME ADVANCE AS AN ARCHIVE (design.md, Header + list state v2):
            // the task is gone, the panel is not — it moves on to the next one
            // down, and only closes when there is nothing left to move to.
            advancePast(erased.key, eraseOrder.current);
          }}
        />
      )}
    </>
  );
}

export default TaskPeek;

