// THE WORKSPACE HOST (D1514, SPEC §56; flag `workspace_tabs_enabled`): the
// window's page column becomes a strip of tabs over a stack of frames, one per
// tab, each a NORMAL page of this shell (its own page chrome, none of the
// window's — router.ts NO_SHELL_CHROME). Home is tab 0 and never closes. The
// explorer's Tab mode (apps/explorer/Tabs.tsx) is the precedent, and its frame
// rules hold here by name:
//
//   TM-3  the host is a param boundary — and opaque (runtime.js) — marked at
//         boot (workspace-bridge), before any frame exists.
//   TM-5  a frame mounts on first activation and is kept alive (display:none)
//         after; frames render as a flat keyed list in MOUNT order that only
//         appends or removes, so React never moves (= reloads) one. `src` is
//         set once and never written by React again.
//   TM-7  the URL mirror writes only on an actual change.
//
// THE HOST'S URL IS THE STRIP (workspace-lib): the active tab's own URL plus
// `_tabs=` naming every tab, and an opaque `_right=` carried through. Every
// open / switch / close is a history entry carrying `{wsActive, wsIds}`; a
// popstate reconciles in place and never remounts this component (App
// renders it unkeyed for exactly that reason).
//
// State lives in a ref, because the bridge's `openPage` is synchronous and
// reads it; a version counter is the re-render.
import { useEffect, useLayoutEffect, useMemo, useReducer, useRef } from "react";
import {
  confirmLeave,
  installNavSink,
  NAV_EVENT,
  NAVIGATE_URL_EVENT,
  navigateUrl,
  type NavCall,
  type NavDetail,
} from "@platform/lib/router";
import { attachEmbedUrlChange, detachEmbedUrlChange, type UrlChangeHook } from "@platform/lib/layout-codec";
import { TAB_VISIBILITY_EVENT } from "@platform/lib/pause-while-hidden";
import {
  registerWorkspaceImpl,
  WS_ARM_BOOKMARK_EVENT,
  WS_BOOKMARKS_EVENT,
  WS_SELECT_NODE_EVENT,
  type WsPublishedPlace,
} from "@platform/lib/workspace-bridge";
import type { WsRole } from "@platform/lib/router";
import { embedSrc } from "@platform/lib/layout-codec";
import { focusAnyChatComposer } from "@platform/lib/chat-focus";
import { pendingEntryId } from "@platform/lib/queue";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import { displayName } from "@platform/lib/flavor";
import { useFavicon } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import { ClaudeRail, type NewTaskSeed } from "@shell/claude-rail/ClaudeRail";
import { placeRoot, type RailPlace } from "@shell/claude-rail/claude-rail-lib";
import type { RailPanel, RailPanelShows } from "@shell/claude-rail/rail-panel";
import { readListing, readTasksRows, subscribeListing } from "@shell/tasksPulse";
import { taskChatTarget } from "@shell/tasks-lib";
import { templateModeIcon } from "@apps/explorer/ModeSwitcher";
import { WorkspacePanel } from "./WorkspacePanel";
import type { PanelParamsHost } from "./panel-params";
import type { PanelAsk } from "./WorkspacePanel";
import { canRunInTerminal, openTerminal } from "@platform/lib/terminalDockStore";
import { noteAskBackend } from "@platform/lib/pending-claude-ask";
import { getArmedBookmark, splitBookmarkUrl, type ArmedBookmark } from "@platform/lib/bookmarks";
import { ONBOARDING_PATH } from "@shell/onboarding/state";
import TabStrip from "./TabStrip";
import {
  buildHostUrl,
  classifyNav,
  encodeRight,
  HOME_URL,
  isHomeUrl,
  isSpaUrl,
  neighborAfterClose,
  normalizeOpenUrl,
  liftRightTabs,
  pageUrlOf,
  parseHostUrl,
  adoptedRun,
  placeOfUrl,
  parseRight,
  planOpen,
  readFrameLoc,
  rightOnlyView,
  rightSession,
  sameRight,
  samePage,
  splitArrival,
  stripHash,
  tabKind,
  tabLabel,
  type RightView,
} from "./workspace-lib";

/** No page has said where its rail stands (Home, Tasks, …): no place, so
 *  nothing rides the list (FallbackRail's pair). */
const NO_PLACE: RailPlace = { kind: "folder", dir: "\u0000" };
const NO_TASK: NewTaskSeed = { target: null, waiting: null, on: null };
const noop = () => {};
const noPull = () => {};

/** What a tab's page last published for the rail (plain data, cloned out of
 *  the frame's realm), and the document that said it. */
interface Published {
  win: Window;
  info: WsPublishedPlace;
  json: string;
}

/** The rail's picture of what the panel shows. */
function showsOf(view: RightView | null): RailPanelShows {
  if (!view) return { kind: "none" };
  switch (view.kind) {
    case "tasks":
      return { kind: "tasks" };
    case "bots":
      return { kind: "bots" };
    case "git":
      return { kind: "git" };
    case "mode":
      return { kind: "mode", mode: view.mode };
    case "chat": {
      const q = new URLSearchParams(view.query);
      const sessionId = q.get("session_id") || null;
      const queued = q.get("queued") || null;
      return sessionId || queued ? { kind: "chat", sessionId, queued } : { kind: "home" };
    }
  }
}

interface Tab {
  id: string;
  // Live location (pathname+search) — what `_tabs` records.
  url: string;
  // The frame's own document.title and favicon, as last read.
  docTitle: string;
  icon: string | null;
  // Frozen at creation: the iframe's `src` prop (TM-5). Navigation before the
  // frame's first load writes the element's src imperatively.
  src: string;
}

// Per-frame wiring that dies with each document and is put back on each load.
interface FrameWiring {
  hook: UrlChangeHook | null;
  observer: MutationObserver | null;
  pop: { win: Window; handler: () => void } | null;
  // False from the moment an src is written until that document's `load`: a
  // navigation sent as an event before the frame's router exists would be lost.
  loaded: boolean;
}

interface HistoryWsState {
  wsActive?: unknown;
  wsIds?: unknown;
  // Each tab's last-read title, beside its id: a reload mounts only the shown
  // tab, and the others have no document to read a title from until shown.
  wsTitles?: unknown;
}

// Ids ride history.state, so they must be unique beyond this document: a Back
// into an entry written before a reload must not collide with a fresh id.
const DOC_TOKEN = Math.random().toString(36).slice(2, 8);
let idSeq = 0;
function mintId(): string {
  return `t${DOC_TOKEN}${++idSeq}`;
}

function makeTab(url: string, id?: string, title?: string): Tab {
  return { id: id ?? mintId(), url: stripHash(url), docTitle: title ?? "", icon: null, src: url };
}

function stateIds(state: HistoryWsState | null, count: number): string[] | null {
  const ids = state?.wsIds;
  if (!Array.isArray(ids) || ids.length !== count) return null;
  return ids.every((x) => typeof x === "string") ? (ids as string[]) : null;
}

function stateTitles(state: HistoryWsState | null, count: number): string[] | null {
  const titles = state?.wsTitles;
  if (!Array.isArray(titles) || titles.length !== count) return null;
  return titles.every((x) => typeof x === "string") ? (titles as string[]) : null;
}

const isWizard = (url: string) => url.split(/[?#]/, 1)[0] === ONBOARDING_PATH;

// The shell's default icon, as this document was served it (its path is the
// build's, hooks.ts `useFavicon`): every page that sets no icon of its own
// wears it, and a strip of identical marks says nothing.
let shellIconPath: string | null | undefined;
function defaultIconPath(): string | null {
  if (shellIconPath === undefined) {
    try {
      const el = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
      shellIconPath = el ? new URL(el.href).pathname : null;
    } catch {
      shellIconPath = null;
    }
  }
  return shellIconPath;
}

/** The page's OWN icon (an app's), or null when it wears the shell's. */
function readFavicon(doc: Document | null | undefined): string | null {
  try {
    const el = doc?.querySelector<HTMLLinkElement>('link[rel~="icon"]');
    if (!el?.href) return null;
    return new URL(el.href).pathname === defaultIconPath() ? null : el.href;
  } catch {
    return null;
  }
}

export default function WorkspaceHost({
  config,
  onPagePath,
}: {
  config: Config;
  /** The shown page's PATH changed without a tab op (a hop inside the active
   *  tab): App re-reads what it derives from the address (the terminal's cwd).
   *  A tab op says so itself (NAV_EVENT); a param write is not a path change. */
  onPagePath?: () => void;
}) {
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const onPagePathRef = useRef(onPagePath);
  onPagePathRef.current = onPagePath;

  // The whole strip, initialised once from the host URL. A reload keeps its ids
  // (the entry's state survives it), so Back into an older entry still matches.
  const S = useRef<{ tabs: Map<string, Tab>; order: string[]; mounted: string[]; active: string; right: string | null } | null>(null);
  if (S.current === null) {
    // Tasks and Bots are never tabs (WS-34): a strip naming one boots without
    // it, the panel showing it instead.
    const raw = parseHostUrl(location.pathname + location.search);
    const parsed = liftRightTabs(raw);
    const rawIds = stateIds(history.state as HistoryWsState | null, raw.tabs.length);
    const rawTitles = stateTitles(history.state as HistoryWsState | null, raw.tabs.length);
    const ids = rawIds && parsed.kept.map((i) => rawIds[i]);
    const titles = rawTitles && parsed.kept.map((i) => rawTitles[i]);
    // Before the first frame can change it (see `readFavicon`).
    defaultIconPath();
    const tabs = new Map<string, Tab>();
    const order = parsed.tabs.map((u, i) => {
      const t = makeTab(u, ids?.[i], titles?.[i]);
      tabs.set(t.id, t);
      return t.id;
    });
    const active = order[parsed.active] ?? order[0];
    S.current = { tabs, order, mounted: [active], active, right: parsed.right };
  }
  const st = S.current;
  const homeId = () => st.order[0];

  // THE RIGHT PANEL'S OWN (SPEC §56 WS-16…): the chat's remount counter (bumped
  // when its query changes from OUTSIDE the chat, ChatView's header), the
  // private target its params store hears URL events on, and each tab's
  // published rail (WS-20).
  const R = useRef({
    chatEpoch: 0,
    chatEvents: new EventTarget(),
    places: new Map<string, Published>(),
    // A staged ask for ONE chat mount: the one made at `epoch` (WS-36).
    ask: null as PanelAsk | null,
    // A bookmark the sidebar armed, waiting for its tab to land (WS-38).
    arms: new Map<string, ArmedBookmark>(),
  }).current;
  const frames = useRef(new Map<string, HTMLIFrameElement>()).current;
  const wiring = useRef(new Map<string, FrameWiring>()).current;
  const wireOf = (id: string): FrameWiring => {
    let w = wiring.get(id);
    if (!w) {
      w = { hook: null, observer: null, pop: null, loaded: false };
      wiring.set(id, w);
    }
    return w;
  };

  const brand = displayName();
  const labelOf = (t: Tab) => tabLabel(t.url, t.docTitle, brand);

  // ---- the URL mirror ------------------------------------------------------
  const hostUrl = (): { url: string; state: HistoryWsState } => {
    const idx = Math.max(0, st.order.indexOf(st.active));
    const urls = st.order.map((id) => st.tabs.get(id)?.url ?? HOME_URL);
    const titles = st.order.map((id) => st.tabs.get(id)?.docTitle ?? "");
    return { url: buildHostUrl(urls, idx, st.right), state: { wsActive: idx, wsIds: st.order.slice(), wsTitles: titles } };
  };

  // A tab op: a real history entry, so Back returns to the previous tab —
  // and, to the host's own chrome, a navigation (what navigate() announces).
  const pushMirror = () => {
    const { url, state } = hostUrl();
    history.pushState(state, "", url);
    window.dispatchEvent(new Event(NAV_EVENT));
  };

  // A location sync: the current entry, rewritten only when something it says
  // changed (TM-7) — every write fires fused:urlchange and the sidebar
  // re-renders on each.
  const replaceMirror = () => {
    const { url, state } = hostUrl();
    const cur = history.state as HistoryWsState | null;
    const sameState =
      cur?.wsActive === state.wsActive &&
      JSON.stringify(cur?.wsIds ?? null) === JSON.stringify(state.wsIds) &&
      JSON.stringify(cur?.wsTitles ?? null) === JSON.stringify(state.wsTitles);
    if (location.pathname + location.search === url && sameState) return;
    const path = location.pathname;
    history.replaceState({ ...(cur && typeof cur === "object" ? cur : null), ...state }, "", url);
    if (location.pathname !== path) onPagePathRef.current?.();
  };

  // The window's title follows the active tab's.
  const syncTitle = () => {
    const t = st.tabs.get(st.active);
    if (!t) return;
    const label = labelOf(t);
    document.title = t.docTitle.trim() || (label === brand ? brand : `${label} – ${brand}`);
  };

  // Hidden signal (pause-while-hidden.ts): every mounted frame learns whether
  // it is the shown one, and hears about it when that changes.
  const signalVisibility = (id: string) => {
    const win = frames.get(id)?.contentWindow;
    if (!win) return;
    try {
      const hidden = id !== st.active;
      if (win._fusedTabHidden === hidden) return;
      win._fusedTabHidden = hidden;
      // The frame realm's own constructor: an event is dispatched where it was made.
      win.dispatchEvent(new (win as Window & typeof globalThis).Event(TAB_VISIBILITY_EVENT));
    } catch {
      /* frame mid-navigation */
    }
  };

  // ---- frame wiring, per load ------------------------------------------------
  const readTab = (id: string) => {
    const t = st.tabs.get(id);
    const iframe = frames.get(id);
    if (!t || !iframe) return;
    const loc = readFrameLoc(iframe);
    // HOME NEVER LEAVES HOME (WS-7). The frame's own sink sends every hop out
    // as a tab; this is the net under anything that bypassed it (a plain
    // anchor, a location.assign): open that page as a tab, put Home back.
    if (id === homeId() && loc && !isHomeUrl(loc)) {
      try {
        iframe.contentWindow?.location.replace(HOME_URL);
      } catch {
        iframe.src = HOME_URL;
      }
      wireOf(id).loaded = false;
      live.current.open(loc, { newTab: true });
      return;
    }
    // A TAB THAT LANDED ON TASKS OR BOTS by a full load (a plain anchor, a
    // location.assign; the sink sends every routed hop to the panel): the
    // panel shows it, and the tab goes back where it was (WS-34).
    const only = loc ? rightOnlyView(loc) : null;
    if (only) {
      // A REPLACE: the frame's own Back below walks the window's joint
      // history, and a push here would be the entry it undid.
      void live.current.openRightView(only, "replace");
      putBack(id, t.url);
      return;
    }
    if (loc) t.url = loc;
    // The first location after an armed open: the arm goes to that page.
    const arm = R.arms.get(id);
    if (arm && loc) {
      R.arms.delete(id);
      if (splitBookmarkUrl(loc).pathname === splitBookmarkUrl(arm.url).pathname) sendArm(id, arm);
    }
    try {
      t.docTitle = iframe.contentDocument?.title ?? "";
      t.icon = readFavicon(iframe.contentDocument);
    } catch {
      /* cross-origin — never for a tab */
    }
    replaceMirror();
    if (id === st.active) syncTitle();
    rerender();
  };

  // ARM A TAB (WS-38): the sidebar armed a bookmark in THIS document and
  // opened its page; the tab showing it is armed in its own.
  const sendArm = (id: string, arm: ArmedBookmark) => {
    const win = frames.get(id)?.contentWindow;
    if (!win) return;
    try {
      const Ctor = (win as Window & typeof globalThis).CustomEvent;
      win.dispatchEvent(new Ctor(WS_ARM_BOOKMARK_EVENT, { detail: { id: arm.id, url: arm.url } }));
    } catch {
      /* frame mid-navigation */
    }
  };
  // Called after an open put `url` in tab `id`: armed for exactly that url,
  // the tab is armed now when it is already there, else when it lands.
  const armFor = (id: string, url: string, landed: boolean) => {
    const armed = getArmedBookmark();
    if (!armed || armed.url !== url) return;
    if (landed && wireOf(id).loaded) sendArm(id, armed);
    else R.arms.set(id, armed);
  };

  // Undo a tab's landing on a panel-only page: its own Back when the frame
  // has an entry to go back to (Navigation API), else a reload of the page it
  // was on, else — it was born there — the tab closes.
  const putBack = (id: string, prev: string) => {
    const win = frames.get(id)?.contentWindow;
    try {
      const nav = (win as (Window & { navigation?: { canGoBack?: boolean } }) | null | undefined)?.navigation;
      if (win && nav?.canGoBack) {
        win.history.back();
        return;
      }
      if (win && prev && !rightOnlyView(prev)) {
        win.location.replace(prev);
        return;
      }
    } catch {
      /* frame gone: close it */
    }
    live.current.close(id);
  };

  const unwire = (id: string) => {
    const w = wiring.get(id);
    if (!w) return;
    detachEmbedUrlChange(w.hook);
    w.hook = null;
    w.observer?.disconnect();
    w.observer = null;
    try {
      w.pop?.win.removeEventListener("popstate", w.pop.handler);
    } catch {
      /* window gone */
    }
    w.pop = null;
  };

  const onFrameLoad = (id: string, iframe: HTMLIFrameElement) => {
    // Fade in rather than flash an unpainted frame (`.is-loaded`, workspace.css).
    iframe.classList.add("is-loaded");
    unwire(id);
    // A new document: what the old one published for the rail goes (the new
    // page publishes again from its own mount, which may already have run).
    const pub = R.places.get(id);
    if (pub && pub.win !== iframe.contentWindow) {
      R.places.delete(id);
      rerender();
    }
    const w = wireOf(id);
    w.loaded = true;
    // A new document is a new Window: flag, listeners and hook all go back on.
    signalVisibility(id);
    const win = iframe.contentWindow;
    w.hook = attachEmbedUrlChange(iframe, () => readTab(id));
    // NOTHING NAVIGATES UNSEEN. Back/Forward walk the window's JOINT history,
    // which interleaves every frame's entries: a step can land in a tab that is
    // not shown. That tab is shown, so the reader sees what their Back did.
    if (win) {
      const handler = () => {
        if (id !== st.active && st.tabs.has(id)) activate(id, "replace");
      };
      try {
        win.addEventListener("popstate", handler);
        w.pop = { win, handler };
      } catch {
        /* frame went away under us */
      }
    }
    try {
      // A title or favicon can change with no URL change (a file view learns
      // its rendered <title> after the fetch) — watch the head for it.
      const doc = iframe.contentDocument;
      const target = doc?.head ?? doc?.documentElement;
      if (target) {
        w.observer = new MutationObserver(() => {
          const t = st.tabs.get(id);
          if (!t) return;
          const title = doc?.title ?? "";
          const icon = readFavicon(doc);
          if (t.docTitle === title && t.icon === icon) return;
          t.docTitle = title;
          t.icon = icon;
          // The entry remembers the title (`wsTitles`).
          replaceMirror();
          if (id === st.active) syncTitle();
          rerender();
        });
        w.observer.observe(target, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["href"] });
      }
    } catch {
      /* frame went away under us */
    }
    readTab(id);
  };

  // ---- tab ops ------------------------------------------------------------
  const activate = (id: string, mode: "push" | "replace" | "none") => {
    if (!st.tabs.has(id)) return;
    const changed = st.active !== id;
    st.active = id;
    // TM-5 lazy mount: the frame exists once its tab has been shown.
    if (!st.mounted.includes(id)) st.mounted = [...st.mounted, id];
    for (const m of st.mounted) signalVisibility(m);
    if (mode === "push" && changed) pushMirror();
    else if (mode !== "none") replaceMirror();
    syncTitle();
    if (changed) focusPending.current = true;
    rerender();
  };

  const addTab = (url: string): Tab => {
    const t = makeTab(url);
    st.tabs.set(t.id, t);
    st.order = [...st.order, t.id];
    return t;
  };

  // Navigate a tab. When the tab's current page is one of this SPA's (it has a
  // router to hear it), the hop is handed to that router — the original call
  // when there is one (router.ts NavCall), so its leave guard asks first and
  // what navigate() derives from "the page you leave" comes from the right
  // page. A destination the router cannot show (`/render`) still goes through
  // it, as a `load`. Only a frame with no router — not loaded yet, or itself a
  // `/render` page — is loaded straight from here.
  const navigateTab = (id: string, url: string, call?: NavCall) => {
    const t = st.tabs.get(id);
    const iframe = frames.get(id);
    if (!t) return;
    const w = wireOf(id);
    const win = iframe?.contentWindow;
    if (iframe && win && w.loaded && isSpaUrl(t.url)) {
      const detail: NavDetail = !isSpaUrl(url) ? { fn: "load", url } : (call ?? { fn: "navigateUrl", url });
      try {
        const Ctor = (win as Window & typeof globalThis).CustomEvent;
        win.dispatchEvent(new Ctor(NAVIGATE_URL_EVENT, { detail }));
        return;
      } catch {
        /* fall through to a load */
      }
    }
    if (iframe) {
      w.loaded = false;
      iframe.classList.remove("is-loaded");
      iframe.src = url;
    } else {
      // Not mounted yet: replace the tab outright so it mounts on the url.
      st.tabs.set(id, makeTab(url, t.id));
      rerender();
    }
  };

  // The open rules (workspace-lib planOpen).
  const open = (raw: string, opts?: { newTab?: boolean; call?: NavCall }) => {
    const n = normalizeOpenUrl(raw, location.href);
    if (!n) return;
    if (n.kind === "foreign") {
      window.open(n.href, "_blank", "noopener");
      return;
    }
    const url = n.url;
    // The setup wizard is the whole window (App), never a tab: an ordinary
    // push here (the sink lets it through), and App shows it.
    if (isWizard(url)) {
      void navigateUrl(url);
      return;
    }
    // Tasks and Bots are never tabs (WS-34), even asked for as a new one.
    const only = rightOnlyView(url);
    if (only) {
      void live.current.openRightView(only);
      return;
    }
    const urls = st.order.map((id) => st.tabs.get(id)?.url ?? HOME_URL);
    const activeIdx = Math.max(0, st.order.indexOf(st.active));
    const plan = planOpen(urls, activeIdx, url, opts?.newTab === true);
    if (plan.kind === "activate") {
      const id = st.order[plan.index];
      activate(id, "push");
      armFor(id, url, !plan.navigate);
      if (plan.navigate) navigateTab(id, url, opts?.call);
      return;
    }
    if (plan.kind === "append") {
      const t = addTab(url);
      armFor(t.id, url, false);
      activate(t.id, "push");
      return;
    }
    armFor(st.active, url, false);
    navigateTab(st.active, url, opts?.call);
  };

  const dropTab = (id: string) => {
    unwire(id);
    wiring.delete(id);
    st.tabs.delete(id);
    st.mounted = st.mounted.filter((x) => x !== id);
    R.places.delete(id);
    R.arms.delete(id);
  };

  const close = (id: string) => {
    const idx = st.order.indexOf(id);
    // Home (0) never closes.
    if (idx <= 0) return;
    const wasActive = id === st.active;
    const nextIdx = neighborAfterClose(idx, st.order.length);
    st.order = st.order.filter((x) => x !== id);
    dropTab(id);
    if (wasActive) activate(st.order[nextIdx] ?? homeId(), "none");
    pushMirror();
    rerender();
  };

  // ---- the right panel (WS-16…WS-19) ------------------------------------------
  // `st.right` is the `_right` string itself; every change goes through here.
  // A view a user action opens is a history entry; a sync is a replace.
  const setRight = (next: string | null, how: "push" | "replace", own = false) => {
    if (next === st.right) return;
    const prev = parseRight(st.right);
    const nv = parseRight(next);
    st.right = next;
    // An ask is for the mount it was handed to: any outside change spends it.
    if (!own) R.ask = null;
    // The chat follows an OUTSIDE change of its conversation by a remount.
    if (!own && nv?.kind === "chat" && prev?.kind === "chat" && prev.file === nv.file && prev.query !== nv.query) R.chatEpoch += 1;
    if (how === "push") pushMirror();
    else replaceMirror();
    rerender();
  };

  // OPEN A VIEW (WS-16): what already shows does nothing; leaving a chat that
  // may hold unsent words asks the composer's leave guard first (it lives in
  // THIS document). Null closes the panel.
  const openRightView = (view: RightView | null, how: "push" | "replace" = "push"): Promise<boolean> => {
    const cur = parseRight(st.right);
    if (view && cur && sameRight(cur, view, (run) => runSessions.current.get(run))) return Promise.resolve(true);
    if (!view && !cur) return Promise.resolve(true);
    const go = () => setRight(view ? encodeRight(view) : null, how);
    if (cur?.kind !== "chat") {
      go();
      return Promise.resolve(true);
    }
    return confirmLeave().then((ok) => {
      if (ok) go();
      return ok;
    });
  };

  // A STAGED ASK (WS-36): a NEW mount of the file's Claude home takes it —
  // even when that home already shows — and only that mount (`R.ask.epoch`).
  const askClaude = (file: string, prompt: string, backend?: string) => {
    const view: RightView = { kind: "chat", file, query: backend ? "backend=" + encodeURIComponent(backend) : "" };
    const go = () => {
      R.chatEpoch += 1;
      setRight(encodeRight(view), "push", true);
      if (backend) noteAskBackend(prompt, backend);
      R.ask = { file, prompt, epoch: R.chatEpoch };
      rerender();
    };
    if (parseRight(st.right)?.kind !== "chat") return go();
    void confirmLeave().then((ok) => {
      if (ok) go();
    });
  };

  // A CANVAS TASK'S DOOR names only its session (classifyNav `session`): the
  // task is found in the shared listing (else the pulse) and its chat opens
  // on the right. Null when no row has that session.
  const viewForSession = (sid: string): RightView | null => {
    const row: (Task | TaskPulseTask) | undefined =
      readListing()?.find((t) => t.session_id === sid) ?? readTasksRows().find((t) => t.session_id === sid);
    if (!row) return null;
    const file = taskChatTarget(row);
    return file ? { kind: "chat", file, query: "session_id=" + encodeURIComponent(sid) } : null;
  };

  // WHERE A URL GOES (WS-15): the host sink, the rail's and the panel's doors.
  const openHref = (raw: string, opts?: { newTab?: boolean; call?: NavCall }) => {
    const n = normalizeOpenUrl(raw, location.href);
    if (n && n.kind === "same" && !opts?.newTab && !isWizard(n.url)) {
      const place = classifyNav(n.url);
      if (place.side === "right") {
        void openRightView(place.view);
        return;
      }
      if (place.side === "session") {
        const view = viewForSession(place.sessionId);
        if (view) {
          void openRightView(view);
          return;
        }
      }
    }
    open(raw, opts);
  };

  // A FRAME'S HOP (workspace-bridge `takeHop`): true when the host took it.
  const takeHop = (raw: string, role: WsRole): boolean => {
    const n = normalizeOpenUrl(raw, location.href);
    if (!n || n.kind !== "same" || isWizard(n.url)) return false;
    const place = classifyNav(n.url);
    if (place.side === "right") {
      // The Bots page's own navigation stays in its frame (the host mirrors it).
      if (role === "panel" && place.view.kind === "bots" && parseRight(st.right)?.kind === "bots") return false;
      void openRightView(place.view);
      return true;
    }
    if (place.side === "session") {
      const view = viewForSession(place.sessionId);
      if (view) {
        void openRightView(view);
        return true;
      }
    }
    // From the panel, every page opens on the left (never inside the panel).
    if (role === "panel") {
      open(n.url);
      return true;
    }
    return false;
  };

  // A TAB BOOTING ON A COMPANION PANE (WS-25, workspace-lib `splitArrival`).
  const adoptArrival = (win: Window, url: string): string | null => {
    const shown = frames.get(st.active)?.contentWindow === win;
    // A CANVAS PAGE NAMING A SESSION stays a canvas tab (the page is real) and
    // shows that task's chat on the right too (WS-34) — once the listing has
    // the task, which a booting window may not yet.
    const place = classifyNav(url);
    if (place.side === "session") {
      if (shown && st.right === null) adoptSession(place.sessionId);
      return null;
    }
    const { keep, right } = splitArrival(url);
    if (!right) return null;
    // An explicit `_right` (a reload) wins over a tab's old pane.
    if (shown && st.right === null) void openRightView(right, "replace");
    return keep;
  };

  // The session's chat on the right as soon as a row names it; given up after
  // a while, or once anything else opened the panel.
  const adoptSession = (sid: string) => {
    const now = viewForSession(sid);
    if (now) {
      void openRightView(now, "replace");
      return;
    }
    let done = false;
    let unsub: (() => void) | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = () => {
      done = true;
      unsub?.();
      if (timer) clearTimeout(timer);
    };
    // Subscribing replays the held listing synchronously: `stop` copes.
    unsub = subscribeListing(() => {
      if (done) return;
      if (S.current?.right !== null) return stop();
      const view = viewForSession(sid);
      if (!view) return;
      stop();
      void live.current.openRightView(view, "replace");
    });
    if (done) unsub();
    else timer = setTimeout(stop, 30_000);
  };

  // A TAB'S PAGE SAYS WHERE ITS RAIL STANDS (WS-20). Plain data, cloned out
  // of the frame's realm; an unchanged publish keeps the old object, so the
  // rail's memoised derivations hold.
  const publishPlace = (win: Window, info: WsPublishedPlace | null) => {
    const id = st.order.find((x) => frames.get(x)?.contentWindow === win);
    if (!id) return;
    if (!info) {
      if (R.places.get(id)?.win !== win) return;
      R.places.delete(id);
    } else {
      let json: string;
      try {
        json = JSON.stringify(info);
      } catch {
        return;
      }
      const was = R.places.get(id);
      if (was && was.win === win && was.json === json) return;
      R.places.set(id, { win, info: JSON.parse(json) as WsPublishedPlace, json });
    }
    // Every tab's place rides the rail (the others faded, WS-27).
    rerender();
  };

  // The panel chat's params are `_right`'s (panel-params.ts).
  const chatHost = useRef<PanelParamsHost | null>(null);
  const runSessions = useRef(new Map<string, string>());
  if (!chatHost.current) {
    chatHost.current = {
      query: () => {
        const v = parseRight(S.current?.right ?? null);
        return v?.kind === "chat" ? v.query : "";
      },
      write: (query, how, state) => {
        const st2 = S.current;
        const v = parseRight(st2?.right ?? null);
        if (!st2 || v?.kind !== "chat") return;
        const next = encodeRight({ ...v, query });
        if (next === st2.right) return;
        // A run that became its session (the fix chat): remembered, so the
        // canvas's "see chat" finds the chat on screen (workspace-lib).
        const pair = adoptedRun(v.query, query);
        if (pair) runSessions.current.set(pair[0], pair[1]);
        st2.right = next;
        if (how === "push") {
          const { url, state: ws } = live.current.hostUrl();
          history.pushState({ ...(state && typeof state === "object" ? state : null), ...ws }, "", url);
        } else live.current.replaceMirror();
        rerender();
      },
    };
  }

  // ---- popstate: reconcile in place ------------------------------------------
  // The entry names a tab set; the live tabs are the truth about where each one
  // is now (their own history covers that), so nothing is navigated and nothing
  // removed — tabs the entry lacks stay, tabs it names that are gone come back
  // (unmounted until shown), and the entry's active tab is selected.
  const onPopState = () => {
    const parsed = parseHostUrl(location.pathname + location.search);
    // The panel follows the entry (WS-22): a chat whose query a traversal
    // moved is remounted on it, and its params store drops a pending write.
    const prevRight = parseRight(st.right);
    st.right = parsed.right;
    R.ask = null;
    const nextRight = parseRight(st.right);
    if (prevRight?.kind === "chat" && nextRight?.kind === "chat" && prevRight.file === nextRight.file && prevRight.query !== nextRight.query) {
      R.chatEpoch += 1;
    }
    R.chatEvents.dispatchEvent(new Event("popstate"));
    const state = history.state as HistoryWsState | null;
    // Ids are an optimisation: an entry whose ids name none of the live tabs
    // matches by page instead, or every tab would come back beside itself.
    const stamped = stateIds(state, parsed.tabs.length);
    const ids = stamped && stamped.some((id) => st.tabs.has(id)) ? stamped : null;
    const titles = stateTitles(state, parsed.tabs.length);
    const used = new Set<string>();
    const parsedIds: string[] = [];
    parsed.tabs.forEach((u, i) => {
      const want = ids?.[i];
      let id: string | undefined;
      // Home is always the live Home.
      if (i === 0) id = homeId();
      else if (want && st.tabs.has(want) && !used.has(want)) id = want;
      else if (!ids) id = st.order.find((x) => x !== homeId() && !used.has(x) && samePage(st.tabs.get(x)!.url, u));
      if (!id) {
        const t = makeTab(u, want && !st.tabs.has(want) ? want : undefined, titles?.[i]);
        st.tabs.set(t.id, t);
        const prev = parsedIds[parsedIds.length - 1];
        const order = st.order.slice();
        order.splice(prev ? order.indexOf(prev) + 1 : order.length, 0, t.id);
        st.order = order;
        id = t.id;
      }
      used.add(id);
      parsedIds.push(id);
    });
    const idx = typeof state?.wsActive === "number" ? state.wsActive : parsed.active;
    activate(parsedIds[idx] ?? parsedIds[parsed.active] ?? homeId(), "replace");
  };

  // Handlers close over `st` and stable maps but are re-created per render, so
  // the effects below read them through a ref.
  const live = useRef({ onPopState, open, close, openHref, openRightView, takeHop, adoptArrival, publishPlace, hostUrl, replaceMirror, askClaude });
  live.current = { onPopState, open, close, openHref, openRightView, takeHop, adoptArrival, publishPlace, hostUrl, replaceMirror, askClaude };

  // After a switch the shown frame takes focus, so the keyboard is in the page
  // the reader chose, not left on the strip or in a hidden frame.
  const focusPending = useRef(false);
  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    try {
      frames.get(st.active)?.contentWindow?.focus();
    } catch {
      /* not loaded yet */
    }
  });

  // The sink, the bridge and the listeners exist exactly while this is mounted.
  useLayoutEffect(() => {
    registerWorkspaceImpl({
      openPage: (url, opts) => live.current.open(String(url), { newTab: opts?.newTab }),
      openHref: (url, opts) => live.current.openHref(String(url), { newTab: opts?.newTab }),
      openRight: (view, opts) => void live.current.openRightView(parseRight(view), opts?.replace ? "replace" : "push"),
      takeHop: (url, role) => live.current.takeHop(String(url), role),
      adoptArrival: (win, url) => live.current.adoptArrival(win, String(url)),
      publishPlace: (win, info) => live.current.publishPlace(win, info),
      focusPanelChat: () => focusAnyChatComposer(),
      askClaude: (file, prompt, opts) => live.current.askClaude(String(file), String(prompt), opts?.backend ? String(opts.backend) : undefined),
      // The window's terminal drawer is this document's (WS-37).
      canRunInTerminal: () => canRunInTerminal(),
      openTerminal: (req) =>
        openTerminal({
          ...(typeof req?.cwd === "string" ? { cwd: req.cwd } : {}),
          ...(typeof req?.command === "string" ? { command: req.command } : {}),
          ...(typeof req?.execute === "boolean" ? { execute: req.execute } : {}),
        }),
      pageUrl: () => {
        const t = st.tabs.get(st.active);
        return t ? t.url : pageUrlOf(location.pathname + location.search);
      },
      // One document saved the bookmarks: every OTHER one of this window (the
      // sidebar here, each loaded tab) re-reads now. Each event is made by the
      // realm it is dispatched in.
      bookmarksWritten: (from) => {
        const wins: Window[] = [window];
        for (const f of frames.values()) if (f.contentWindow) wins.push(f.contentWindow);
        for (const w of wins) {
          if (w === from) continue;
          try {
            w.dispatchEvent(new (w as Window & typeof globalThis).Event(WS_BOOKMARKS_EVENT));
          } catch {
            /* frame mid-navigation */
          }
        }
      },
    });
    // Every hop from the host's own chrome follows the open rules — except to
    // the wizard, which is an ordinary push (App then shows it, whole-window).
    // …and a chat door, Tasks or Bots opens in the right panel (WS-15).
    installNavSink((href, call) => {
      if (isWizard(href)) return false;
      live.current.openHref(href, { call });
      return true;
    });
    // THE PANEL'S GIT (or mode) FRAME's "Fix with AI" climbs to the nearest
    // `_fusedClaudeAsk` (runtime.js `noteAskClaude`), which is this window:
    // its folder's Claude home in the panel takes the ask (WS-36).
    window._fusedClaudeAsk = (text: unknown) => {
      if (typeof text !== "string" || !text) return false;
      const v = parseRight(S.current?.right ?? null);
      const file = v?.kind === "git" ? v.path : v?.kind === "mode" ? v.file : null;
      if (!file) return false;
      live.current.askClaude(file, text);
      return true;
    };
    const onPop = () => live.current.onPopState();
    window.addEventListener("popstate", onPop);
    // SHIFT+CLICK = NEW TAB on the sidebar's links (and its Settings menu).
    // Capture phase, before any row's own handler: rows that bail out on a
    // modified click would otherwise leave it to the browser, which opens a
    // window. Cmd/Ctrl-click stays the browser's.
    const onClick = (e: MouseEvent) => {
      if (e.button !== 0 || !e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return;
      const a = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!a || !a.closest("#sidebar, .sidebar-prefs-menu")) return;
      const n = normalizeOpenUrl(a.href, location.href);
      if (!n || n.kind !== "same") return;
      e.preventDefault();
      e.stopPropagation();
      live.current.open(n.url, { newTab: true });
    };
    document.addEventListener("click", onClick, true);
    // The first mirror: the canonical spelling, with ids in the entry so Back
    // into it matches tabs by id.
    replaceMirror();
    syncTitle();
    return () => {
      registerWorkspaceImpl(null);
      installNavSink(null);
      delete window._fusedClaudeAsk;
      window.removeEventListener("popstate", onPop);
      document.removeEventListener("click", onClick, true);
      for (const id of Array.from(wiring.keys())) unwire(id);
    };
  }, []);

  const stripTabs = st.order.map((id, i) => {
    const t = st.tabs.get(id)!;
    return { id, label: labelOf(t), kind: tabKind(t.url), icon: t.icon, active: id === st.active, home: i === 0 };
  });
  // The window's own icon follows the shown page's, as it did before tabs.
  useFavicon(st.tabs.get(st.active)?.icon ?? null);

  // ---- the right panel and the window's one rail (WS-16, WS-20, WS-21) ------
  const view = parseRight(st.right);
  const activePub = (() => {
    const p = R.places.get(st.active);
    return p && p.win === frames.get(st.active)?.contentWindow ? p.info : null;
  })();
  const activeFrameWin = () => frames.get(st.active)?.contentWindow ?? null;
  // …and UNTIL ITS PAGE HAS SAID (a tab still loading), the place its URL
  // names: its tasks stand on top from the first paint, not among the faded
  // ones until the page publishes.
  const activeUrl = st.tabs.get(st.active)?.url ?? "";
  const urlPlace = useMemo(() => placeOfUrl(activeUrl), [activeUrl]);
  const railPlace = (activePub?.place as RailPlace | undefined) ?? urlPlace ?? NO_PLACE;
  // EVERY OTHER TAB'S PLACE (WS-27), in strip order: what its page published,
  // else what its URL names (a tab not loaded since a reload has published
  // nothing). One per place, none the active tab's. Keyed by its JSON, so the
  // rail's seats re-derive only when a place changed.
  const othersJson = JSON.stringify(
    st.order
      .filter((id) => id !== st.active)
      .map((id) => {
        const p = R.places.get(id);
        if (p && p.win === frames.get(id)?.contentWindow) return p.info.place as RailPlace;
        const t = st.tabs.get(id);
        return t ? placeOfUrl(t.url) : null;
      }),
  );
  const railRoot = placeRoot(railPlace);
  const otherPlaces = useMemo(() => {
    const roots = new Set([railRoot]);
    const out: RailPlace[] = [];
    for (const p of JSON.parse(othersJson) as (RailPlace | null)[]) {
      if (!p || roots.has(placeRoot(p))) continue;
      roots.add(placeRoot(p));
      out.push(p);
    }
    return out;
  }, [othersJson, railRoot]);
  // The +'s target meanwhile: the folder or file that URL names.
  const urlTask = useMemo((): NewTaskSeed => {
    const target = !urlPlace ? null : urlPlace.kind === "folder" ? urlPlace.dir : urlPlace.kind === "file" ? urlPlace.file : null;
    return target ? { target, waiting: null, on: null } : NO_TASK;
  }, [urlPlace]);
  const railNewTask = (activePub?.newTask as NewTaskSeed | undefined) ?? urlTask;
  const railGitPath = activePub?.git?.path ?? null;
  const railGit = useMemo(
    () => (railGitPath ? { path: railGitPath, onOpenGit: () => void live.current.openRightView({ kind: "git", path: railGitPath }) } : undefined),
    [railGitPath],
  );
  const pubModes = activePub?.fileModes ?? null;
  const railModes = useMemo(() => {
    if (!pubModes) return undefined;
    const path = pubModes.path;
    return {
      path,
      rev: pubModes.rev,
      // Icons rebuilt here: only data crossed the frame (WS-20).
      doors: pubModes.doors.map((d) => ({ mode: d.mode, title: d.title, icon: templateModeIcon({ mode: d.mode, path: null, icon: d.icon }) })),
      src: (m: string) => pubModes.srcs[m] ?? embedSrc(path, "?_mode=" + encodeURIComponent(m)),
      // The revision the tab shows rides along (`_snapshot`, WS-14).
      onOpen: (m: string) => void live.current.openRightView({ kind: "mode", mode: m, file: path, ...(pubModes.rev ? { rev: pubModes.rev } : {}) }),
    };
  }, [pubModes]);
  // A canvas tab's node chip and place tags select INSIDE that tab.
  const canvasSelect = !!activePub?.canvasSelect;
  const selectInTab = (udfName: string | null, sessionId: string | null | undefined, tag: boolean) => {
    const win = activeFrameWin();
    if (!win) return;
    try {
      const Ctor = (win as Window & typeof globalThis).CustomEvent;
      win.dispatchEvent(new Ctor(WS_SELECT_NODE_EVENT, { detail: { udfName, sessionId: sessionId ?? null, tag } }));
    } catch {
      /* frame mid-navigation */
    }
  };
  const peekSession = view?.kind === "tasks" && view.peek && !pendingEntryId(view.peek) ? view.peek : null;
  const panel: RailPanel = {
    shows: showsOf(view),
    openChat: (t, row) => {
      const merged = row ? { ...row, status: t.status } : t;
      const file = taskChatTarget(merged);
      if (!file) return false;
      if (t.session_id) {
        void openRightView({ kind: "chat", file, query: "session_id=" + encodeURIComponent(t.session_id) });
        return true;
      }
      const queued = pendingEntryId(t.key);
      if (queued && t.status === "queued") {
        void openRightView({ kind: "chat", file, query: "queued=" + encodeURIComponent(queued) });
        return true;
      }
      return false;
    },
    openTasks: () => void openRightView({ kind: "tasks", peek: null }),
    openBots: () => void openRightView({ kind: "bots", url: "/bots" }),
    openGit: () => {
      if (railGitPath) void openRightView({ kind: "git", path: railGitPath });
    },
    openMode: (mode) => {
      if (pubModes) void openRightView({ kind: "mode", mode, file: pubModes.path, ...(pubModes.rev ? { rev: pubModes.rev } : {}) });
    },
    // The + is the ACTIVE TAB's Claude home, fixed at the click (WS-21):
    // the place's target, else the default Fused folder.
    openHome: () => {
      const file = railNewTask.target || config.fused_dir;
      if (file) void openRightView({ kind: "chat", file, query: "" });
    },
  };

  // Mounted while closed too: it keeps its Tasks and Bots (WorkspacePanel).
  const rightPanel = (
    <WorkspacePanel
      view={view}
      chatEpoch={R.chatEpoch}
      ask={R.ask}
      chatHost={chatHost.current}
      chatEvents={R.chatEvents}
      onHide={() => void openRightView(null)}
      onDragClose={() => openRightView(null)}
      onNavigate={(url) => openHref(url)}
      onTasksPeek={(key, how = "push") => {
        // A kept, hidden Tasks view never takes the panel back.
        if (parseRight(st.right)?.kind === "tasks") setRight(encodeRight({ kind: "tasks", peek: key }), how);
      }}
      onBotsSync={(url) => {
        if (parseRight(st.right)?.kind !== "bots") return;
        const p = classifyNav(url);
        if (p.side === "right" && p.view.kind === "bots") setRight(encodeRight(p.view), "replace", true);
      }}
    />
  );

  return (
    <div className="shell-main-row ws-row">
      <div className="ws-left">
        <TabStrip tabs={stripTabs} onSelect={(id) => activate(id, "push")} onClose={(id) => close(id)} />
        <div className="ws-frames">
          {/* MOUNT order, append/remove only (TM-5) — never strip order. */}
          {st.mounted.map((id) => {
            const t = st.tabs.get(id);
            if (!t) return null;
            return (
              <iframe
                key={id}
                // BEFORE src: the page reads its role off this element at
                // module init (router.ts WS_ROLE).
                data-ws-role={id === homeId() ? "home" : "tab"}
                ref={(el) => {
                  if (el) frames.set(id, el);
                  else if (!st.tabs.has(id)) frames.delete(id);
                }}
                className="ws-frame"
                title={labelOf(t)}
                src={t.src}
                onLoad={(e) => onFrameLoad(id, e.currentTarget)}
                style={id === st.active ? undefined : { display: "none" }}
              />
            );
          })}
        </div>
      </div>
      {rightPanel}
      {/* THE WINDOW'S ONE RAIL (WS-20): fed by what the active tab's page
          published, its clicks the panel's (`panel`). */}
      <ClaudeRail
        place={railPlace}
        newTask={railNewTask}
        {...(railGit ? { git: railGit } : {})}
        {...(railModes ? { fileModes: railModes } : {})}
        activeSessionId={rightSession(view) ?? peekSession}
        onOpen={noop}
        onOpenChat={(href) => {
          openHref(href);
        }}
        onPullStart={noPull}
        {...(canvasSelect
          ? {
              onSelectNode: (udfName: string | null) => selectInTab(udfName, null, false),
              onTagSelect: (udfName: string | null, sessionId?: string | null) => selectInTab(udfName, sessionId, true),
            }
          : {})}
        panel={panel}
        otherPlaces={otherPlaces}
      />
    </div>
  );
}
