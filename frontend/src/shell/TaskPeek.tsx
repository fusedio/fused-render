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
  Suspense,
  lazy,
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
import { SkeletonLines } from "@platform/ui/Skeleton";
import { ChatMount, useNativeChatFlag } from "@apps/claude";

/**
 * THE CHAT'S OWN IDENTITY LINE, reused rather than restyled (`@apps/claude/ui/
 * Topbar`): the Claude mark, the conversation's name, TASK-nnn, and the running
 * word. The peek used to carry a 44px strip of icons with a 32px title under it
 * — two headers for one panel, and a large gap between them (Akshil,
 * 2026-09-13: "doesn't look good"). Now there is ONE row, it is the row the
 * chat already has everywhere else, and the peek's own controls sit either side
 * of it.
 *
 * LAZY, for `ChatMount`'s reason exactly (see its `ClaudeChat` import): this
 * module reaches the chat's ⋮ and through it the agent protocol, and a static
 * import would put that in the shell's entry chunk for every route that merely
 * CAN open a peek. The panel always mounts a chat anyway, so the wait is one
 * the reader is already having.
 */
const ChatTopbar = lazy(() =>
  import("@apps/claude/ui/Topbar").then((m) => ({ default: m.Topbar })),
);
import { columnLabel, folderHref, peekFrameSrc } from "./schedule-lib";
import { EraseTaskModal } from "./EraseTaskModal";
import { ICON_ARCHIVE, ICON_TRASH, ICON_UNARCHIVE, IdentityChip, StatusIcon } from "./ScheduleTaskViews";
import { useChatTemplates } from "./TaskCards";
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
import { MISSING_FOLDER_TOAST, taskFolder } from "./useMissingFolders";
import {
  PEEK_ITEM_ATTR,
  PEEK_KEY_STEP,
  PEEK_MAX_FRACTION,
  PEEK_MIN_WIDTH,
  applyRoom,
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

/** Re-render on window resize: every make-room rule is about the CURRENT
 *  window, so a peek opened wide has to re-decide when the window narrows. */
function useViewportWidth(): number {
  const [w, setW] = useState(() => (typeof window === "undefined" ? 0 : window.innerWidth));
  useEffect(() => {
    const read = () => setW(window.innerWidth);
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  return w;
}

export interface PeekLayout {
  open: boolean;
  /** What the peek renders at — its dragged width, or the whole content area
   *  in cover mode. Zero when closed, so the frame is simply full width. */
  width: number;
  cover: boolean;
  instant: boolean;
}

/**
 * What the Tasks page needs to know to give the peek its room: is it open, and
 * how much is it taking. The frame's width is `calc(100% - <this>)`, which is
 * the one number both halves of the animation are built from.
 */
export function useTaskPeekLayout(): PeekLayout {
  const state = useSyncExternalStore(subscribePeek, getPeekState, getPeekState);
  const viewport = useViewportWidth();
  return useMemo(() => {
    const room = currentRoom();
    if (state.key === null) {
      return { open: false, width: 0, cover: false, instant: state.instant };
    }
    return {
      open: true,
      width: room.peekWidth,
      cover: room.cover,
      instant: state.instant,
    };
    // `viewport` is not read directly — `currentRoom()` reads the window — but
    // it is what makes this recompute when the window changes size, which is
    // also what RE-CLAMPS a persisted width that no longer fits (design.md).
  }, [state.key, state.width, state.instant, viewport]);
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
  // Every make-room rule is re-decided on resize, including the one that puts
  // the sidebar back when the window grows past the threshold again.
  useEffect(() => {
    if (!enabled) return;
    const onResize = () => {
      if (getPeekState().key !== null) applyRoom();
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
const ICON_EXPAND = (
  <svg {...ICON}><path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7" /></svg>
);
const ICON_UP = (
  <svg {...ICON}><path d="M12 19V5M5 12l7-7 7 7" /></svg>
);
const ICON_DOWN = (
  <svg {...ICON}><path d="M12 5v14M19 12l-7 7-7-7" /></svg>
);
const ICON_DOTS = (
  <svg {...ICON}>
    <circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none" />
  </svg>
);
const ICON_LINK = (
  <svg {...ICON}>
    <path d="M10 13a5 5 0 0 0 7.1.1l2.9-2.9a5 5 0 0 0-7.1-7.1L11 4.9" />
    <path d="M14 11a5 5 0 0 0-7.1-.1L4 13.8a5 5 0 0 0 7.1 7.1l1.8-1.8" />
  </svg>
);
const ICON_PAGE = (
  <svg {...ICON}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></svg>
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

  // ---- the keyboard ----------------------------------------------------------
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
        const el = doc.activeElement as HTMLElement | null;
        // FIRST ESC BLURS a composer that has something in it (design.md,
        // Keyboard): losing a half-typed message to a stray Escape is exactly
        // the kind of thing an escape hatch must not do.
        const typing =
          el &&
          (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.isContentEditable) &&
          !!(el as HTMLTextAreaElement).value;
        if (typing) {
          e.preventDefault();
          el.blur();
          return true;
        }
        e.preventDefault();
        closePeek();
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
    [step, openAsPage],
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
    setPeekWidth(clampPeekWidth(currentRoom().peekWidth + delta, content));
    applyRoom();
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
      applyRoom();
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

  // ---- the ⋯ menu ------------------------------------------------------------
  const filing = task ? filingIntent(task) : null;
  const refile = async () => {
    if (!filing || !task || acting) return;
    setActing(true);
    try {
      if (filing.kind === "archive") await archiveTask(task.key);
      else await unarchiveTask(task.key);
      onReload?.();
    } catch (e) {
      notify({ title: (e as Error).message, tone: "error" });
    } finally {
      setActing(false);
    }
  };
  const menuItems = (): MenuEntry[] => {
    if (!task) return [];
    const items: MenuEntry[] = [];
    if (page) {
      items.push({ label: "Open as page", icon: ICON_PAGE, onClick: openAsPage });
      items.push({
        label: "Copy link",
        icon: ICON_LINK,
        onClick: () => {
          void copyToClipboard(location.origin + page).then((ok) =>
            notify({ title: ok ? "Link copied" : "Could not copy the link", tone: ok ? "info" : "error" }),
          );
        },
      });
    }
    if (gone) {
      items.push({
        label: "Open in Explorer",
        icon: ICON_PAGE,
        disabled: true,
        onClick: () => notify({ title: MISSING_FOLDER_TOAST, tone: "error" }),
      });
    }
    if (filing) {
      if (items.length) items.push("separator");
      items.push({
        label: filing.label,
        icon: filing.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE,
        disabled: acting,
        onClick: () => void refile(),
      });
    }
    items.push("separator");
    // A menu row cannot carry a hint, so the DISABLED one says why in its own
    // words — nobody should meet the server's refusal for the first time inside
    // a confirmation (the card door's rule, tasks-lib.eraseBlocked).
    const blocked = eraseBlocked(task);
    items.push({
      label: blocked ? `Delete forever — ${ERASE_BLOCKED_HINT.toLowerCase()}` : "Delete forever",
      icon: ICON_TRASH,
      danger: true,
      disabled: blocked,
      onClick: () => setErasing(true),
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
          aria-valuemax={Math.round(contentWidth() * PEEK_MAX_FRACTION) || undefined}
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
            applyRoom();
          }}
        />
        <header className="task-side-peek-head">
          <div className="task-side-peek-acts">
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Close"
              data-hint="Close · Esc"
              onClick={() => closePeek()}
            >
              {ICON_CLOSE}
            </button>
            <button
              type="button"
              className="task-side-peek-btn"
              aria-label="Open as page"
              data-hint="Open as page · ⌘↩"
              disabled={!page}
              onClick={openAsPage}
            >
              {ICON_EXPAND}
            </button>
            <span className="task-side-peek-gap" aria-hidden />
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
          {/* The chat's own line, carrying the TASK TITLE as its name — the
              header's title IS the task's, so there is no second title row
              under it and the conversation starts directly below. The fallback
              is an empty box of the same width, so the row does not jump when
              the chunk lands. */}
          <Suspense fallback={<span className="task-side-peek-idle" aria-hidden />}>
            {task ? (
              <ChatTopbar
                sessionId={task.session_id}
                subtitle={title}
                taskId={task.task_id}
                running={taskColumn(task) === "in_progress"}
              />
            ) : (
              <span className="task-side-peek-idle" aria-hidden />
            )}
          </Suspense>
          {task && (
            <div className="task-side-peek-marks">
              <span className="task-side-peek-status">
                <StatusIcon status={taskColumn(task)} failed={ringFailed(task)} />
                {column ? columnLabel(column) : ""}
              </span>
              <IdentityChip
                name={basename(task.project)}
                title={tildePath(task.project, home)}
              />
              <button
                type="button"
                className="task-side-peek-btn"
                aria-label="More actions"
                data-hint="More actions"
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  setMenuAt({ x: r.left, y: r.bottom + 4 });
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
                onEscape={() => closePeek()}
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
            closePeek();
          }}
        />
      )}
    </>
  );
}

export default TaskPeek;

