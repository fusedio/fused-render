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
import { archiveTask, unarchiveTask } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { notify } from "@platform/lib/notifications";
import { withNoFocus } from "@platform/lib/frame-focus";
import { useParamBoundary } from "@platform/lib/param-boundary";
import { navigateUrl } from "@platform/lib/router";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import PanelIcon from "@platform/ui/PanelIcon";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { ChatMount, useNativeChatFlag } from "@apps/claude";
import { runAgent } from "@apps/claude/protocol/agent";

// THE HEADER IS THE PEEK'S OWN NOW, not the chat's (design.md, Header + list
// state v2). It wore `@apps/claude/ui/Topbar` between 2026-09-13 and -09-14,
// which bought one row instead of two and cost the row its subject: the ✻
// Claude wordmark and the model/run cluster are facts about the TOOL, and this
// panel is about a TASK. What is left is the task — its status, its number, its
// title — with the panel's own controls either side.
import { columnLabel, folderHref, peekFrameSrc } from "./schedule-lib";
import { EraseTaskModal } from "./EraseTaskModal";
import {
  ICON_ARCHIVE,
  ICON_OPEN_FOLDER_PATH,
  ICON_TRASH,
  ICON_UNARCHIVE,
  StatusIcon,
} from "./ScheduleTaskViews";
import { useChatTemplates } from "./TaskCards";
import { PEEK_HEAD_DROPS, useStripFit } from "./row-fit";
import {
  PREVIEW_KEY_STEP,
  PREVIEW_LOAD_TIMEOUT_MS,
  PREVIEW_MIN_H,
  PREVIEW_CHAT_MIN,
  PREVIEW_VH,
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
  basename,
  emptyPaneFailed,
  emptyPaneText,
  eraseBlocked,
  filingIntent,
  firstLine,
  ringFailed,
  taskColumn,
  taskHref,
  tildePath,
} from "./tasks-lib";
import { getSidebarState, subscribeSidebarState } from "@platform/lib/sidebarstate";
import { MISSING_FOLDER_TOAST, taskFolder } from "./useMissingFolders";
import {
  PEEK_ITEM_ATTR,
  PEEK_KEY_STEP,
  nextAfterRemoval,
  refreshPeekBaseline,
  PEEK_MIN_WIDTH,
  applyResize,
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
        instant: state.instant,
      };
    }
    return {
      open: true,
      width: room.peekWidth,
      cover: room.cover,
      floor: room.contentFloor,
      floored: room.floored,
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

/** The visible order, read off the frame in DOM order (see PEEK_ITEM_ATTR). */
function visibleOrder(): string[] {
  if (typeof document === "undefined") return [];
  const nodes = document.querySelectorAll(`.tasks-frame [${PEEK_ITEM_ATTR}]`);
  const keys: string[] = [];
  nodes.forEach((el) => {
    const key = el.getAttribute(PEEK_ITEM_ATTR);
    // A view may paint the same task twice (a board card and its drag ghost);
    // the walk wants places, not nodes.
    if (key && !keys.includes(key)) keys.push(key);
  });
  return keys;
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

const ICON_CLOSE = (
  <svg {...ICON}><path d="M6 6l12 12M18 6L6 18" /></svg>
);
/** OPEN IN EXPLORER — the page's one door glyph, drawn here at the header's own
 *  weight (ScheduleTaskViews `ICON_OPEN_FOLDER_PATH` carries the shape). */
const ICON_OPEN_DOOR = (
  <svg {...ICON}><path d={ICON_OPEN_FOLDER_PATH} /></svg>
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
  const [erasing, setErasing] = useState(false);
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
      // page and takes ⌃⇧J / ⌃⇧K / ⌘↩ with it. A wrapper at the HOST, which is
      // where every other framing site applies it (legacy-src.ts's header).
      // `noFocus` below is the same fact for the native branch.
      ? withNoFocus(peekFrameSrc(template, task.target || task.project, task.session_id))
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
      // focused an input on boot would take ⌃⇧J and ⌘↩ away from the panel
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
  const step = useCallback(
    (delta: number) => {
      const next = stepPeekKey(order(), getPeekState().key, delta);
      if (next) openPeek(next);
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
   * THE PEEK'S FOUR KEYS, in one function because they have to be answered in
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
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        openAsPage();
        return true;
      }
      return false;
    },
    [step, openAsPage, escapeOrBlur],
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
  // the chat and type, and ⌃⇧J / ⌃⇧K / ⌘↩ have to keep working when they do.
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
  // and the reader may well be clicking about in it; ⌃⇧J / ⌃⇧K / ⌘↩ / Esc have
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
        setErasing(true);
      },
    });
    return items;
  };

  const title = task ? firstLine(task.title) || "(untitled)" : "";
  const column = task ? taskColumn(task) : null;

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
        aria-label={task ? `${task.task_id} ${title}` : "Task"}
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
            {/* HIDE THE PANEL, and the glyph says which panel: the app's shared
                frame-with-one-half-filled (platform/ui/PanelIcon), `right`
                because that is the column this is. The × is kept for COVER
                mode alone — there the peek is not a column beside anything, it
                IS the content area, and "hide the right panel" would be a
                picture of a layout that is not on screen. */}
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label={layout.cover ? "Close" : "Hide the task panel"}
              data-hint={layout.cover ? "Close · Esc" : "Hide · Esc"}
              onClick={() => closePeek()}
            >
              {layout.cover ? ICON_CLOSE : <PanelIcon side="right" />}
            </button>
            {/* CHEVRONS, not arrows (design.md): prev/next here walk a list the
                reader can see, one step at a time — the gesture a chevron means
                everywhere else in this app. A full arrow is for travel. */}
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Previous task"
              data-hint="Previous task · ⌃⇧K"
              disabled={!ends.prev}
              onClick={() => step(-1)}
            >
              {ICON_UP}
            </button>
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Next task"
              data-hint="Next task · ⌃⇧J"
              disabled={!ends.next}
              onClick={() => step(1)}
            >
              {ICON_DOWN}
            </button>
          </div>
          {task ? (
            <div className="task-side-peek-who">
              {/* The ROW's ring, the same component and the same vocabulary —
                  a reader who learned the mark in the list does not learn it
                  twice. The word is the ring's tooltip rather than ink: it is
                  the one fact here that repeats on every row of the list
                  behind the panel. */}
              <span
                className="task-side-peek-status"
                title={column ? columnLabel(column) : undefined}
              >
                <StatusIcon status={taskColumn(task)} failed={ringFailed(task)} />
              </span>
              <span className="task-side-peek-id">{task.task_id}</span>
              <span className="task-side-peek-title" title={title}>
                {title}
              </span>
            </div>
          ) : (
            <span className="task-side-peek-idle" aria-hidden />
          )}
          {task && (
            <div className="task-side-peek-marks">
              {/* THE SAME DOOR AS THE ROW'S AND THE CARD'S — one folder glyph
                  for "open in Explorer" everywhere (ScheduleTaskViews
                  `ICON_OPEN_FOLDER_PATH`). A real link with a real href, so
                  ⌘-click opens a tab, exactly like the row's. */}
              {page && (
                <a
                  className="task-side-peek-btn task-side-peek-open"
                  href={page}
                  aria-label="Open in Explorer"
                  data-hint="Open in Explorer · ⌘↩"
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                    e.preventDefault();
                    openAsPage();
                  }}
                >
                  {ICON_OPEN_DOOR}
                </a>
              )}
              {/* The project as a WORD, not a chip: a chip is a control, and
                  there is nothing to press here — the folder is where the door
                  beside it leads. */}
              <span
                className="task-side-peek-project"
                title={tildePath(task.project, home)}
              >
                {basename(task.project)}
              </span>
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
                  lays out at a virtual 1280×720 and is scaled to the panel's
                  width, so the app sees a desktop window however narrow the
                  peek is — and widening the peek makes the preview taller as
                  well as wider. The box crops rather than squashes: the wrapper
                  under it is the scaled frame's real size, and the box scrolls
                  when the cap or the reader's own drag is shorter than that. */}
              <div className="task-side-peek-preview" style={{ height: box.height }}>
                {previewFailed ? (
                  <p className="task-side-peek-preview-off" role="status">
                    Preview unavailable
                  </p>
                ) : (
                  <>
                    <div
                      className="task-side-peek-preview-scale"
                      style={{
                        width: PREVIEW_VW * box.scale,
                        height: PREVIEW_VH * box.scale,
                      }}
                    >
                      <iframe
                        ref={previewRef}
                        className="task-side-peek-preview-frame"
                        src={previewSrc}
                        title={app ? `${app.name} preview` : "App preview"}
                        style={{
                          width: PREVIEW_VW,
                          height: PREVIEW_VH,
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
                  </>
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
                title={`${task.task_id} ${title}`}
                file={task.target || task.project}
                sessionId={task.session_id}
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
      {erasing && task && (
        <EraseTaskModal
          task={task}
          onClose={() => setErasing(false)}
          onDone={() => {
            setErasing(false);
            notify({ title: `Deleted ${task.task_id}`, tone: "info" });
            onReload?.();
            // SAME ADVANCE AS AN ARCHIVE (design.md, Header + list state v2):
            // the task is gone, the panel is not — it moves on to the next one
            // down, and only closes when there is nothing left to move to.
            advancePast(task.key, eraseOrder.current);
          }}
        />
      )}
    </>
  );
}

export default TaskPeek;

