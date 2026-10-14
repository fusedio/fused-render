// Shell tabs (flag `shell_tabs`, router.ts isTabHost): a native window's
// top-level shell keeps its chrome — sidebar, status bar, terminal drawer,
// docks, notifications — and draws its content column as a strip of tabs over
// a stack of iframes, one per tab, each a whole chrome-free page of this same
// shell (every route drops its chrome under IS_EMBED). The explorer's Tab mode
// (apps/explorer/Tabs.tsx) is the precedent, and its iframe rules are copied
// here by name:
//
//   TM-3  this window is a param boundary, marked at render before any frame
//         mounts, so a page inside a tab writes its params to its own URL.
//   TM-5  a frame is mounted on first activation and kept alive (display:none)
//         after; frames render as a flat keyed list in MOUNT order that only
//         appends or removes, so React never moves (= reloads) one. The STRIP
//         order is a separate array — a reorder re-sorts buttons, not frames.
//         `src` is set once and never written by React again.
//   TM-7  the URL mirror writes only on an actual change.
//
// THE HOST'S OWN URL IS THE STRIP (tabs-lib): the active tab's location, plus
// `_tabs=` naming every tab in strip order. A reload or a relaunch comes back
// with the same tabs. Tab ops (open, switch, close) are history entries
// carrying `{fusedActiveTab, fusedTabIds}` so Back/Forward restore which tab
// was active; a popstate reconciles in place and never remounts this
// component — App renders it unkeyed for exactly that reason.
//
// THE SAME TABS, DRIVEN FROM PYTHON: `window._fusedTabs` (tab-host-api.ts) is
// what the native menu's ⌘T/⌘W/⌘⇧[ ]/⌘1–9 and mac_window.py evaluate against.
// Every method is synchronous, so the state they read and write lives in a
// ref and React renders from it; a version counter is the re-render.
import { useEffect, useLayoutEffect, useReducer, useRef } from "react";
import { installNavSink, IS_NATIVE_WINDOW, NAVIGATE_URL_EVENT, type NavCall, type NavDetail } from "@platform/lib/router";
import { attachEmbedUrlChange, detachEmbedUrlChange, type UrlChangeHook } from "@platform/lib/layout-codec";
import { TAB_VISIBILITY_EVENT } from "@platform/lib/pause-while-hidden";
import { displayName } from "@platform/lib/flavor";
import { isMod } from "@platform/lib/platform";
import type { Config } from "@platform/lib/api";
import { ONBOARDING_PATH } from "@shell/onboarding/state";
import TabStrip from "@shell/TabStrip";
import type { FusedTabsApi } from "@shell/tab-host-api";
import {
  buildHostUrl,
  hostLocation,
  isSpaUrl,
  moveItem,
  neighborAfterClose,
  parseHostUrl,
  readTabLoc,
  samePage,
  stepIndex,
  stripTabMarker,
  tabLabel,
  tabSrc,
} from "@shell/tabs-lib";

interface Tab {
  id: string;
  // Live location, marker stripped (tabs-lib stripTabMarker) — what `_tabs`
  // records and what the strip labels fall back to.
  url: string;
  // The frame's own document.title, as last read.
  docTitle: string;
  // Frozen at creation: the iframe's `src` prop, never changed after (TM-5).
  // Reload and navigation-before-load write the element's src imperatively.
  src: string;
}

// Per-frame wiring that dies with each document and is re-attached on each
// `load`: the fused:urlchange/popstate hook, the title observer, and the
// in-frame tab chords.
interface FrameWiring {
  hook: UrlChangeHook | null;
  observer: MutationObserver | null;
  keyWin: Window | null;
  // The traversal watcher (see onFrameLoad) and the window it is on.
  pop: { win: Window; handler: () => void } | null;
  // False from the moment an src is written until that document's `load`: a
  // navigation sent as an event before the frame's router exists would be lost.
  loaded: boolean;
}

interface HistoryTabsState {
  fusedActiveTab?: unknown;
  fusedTabIds?: unknown;
}

// Ids are strings, unique beyond this document: they ride history.state, so
// a Back into an entry written before a reload must not collide with a tab
// this document minted.
const DOC_TOKEN = Math.random().toString(36).slice(2, 8);
let idSeq = 0;
function mintId(): string {
  return `t${DOC_TOKEN}${++idSeq}`;
}

function makeTab(url: string, id?: string): Tab {
  const src = tabSrc(url);
  // Marker stripped: `url` is what `_tabs` and list() report. A `/` (Home,
  // before the tab's own front-door rewrite has run) is fine to mirror —
  // App's rewrite in the host keeps the query and the entry state.
  return { id: id ?? mintId(), url: stripTabMarker(src), docTitle: "", src };
}

function stateIds(state: HistoryTabsState | null, count: number): string[] | null {
  const ids = state?.fusedTabIds;
  if (!Array.isArray(ids) || ids.length !== count) return null;
  return ids.every((x) => typeof x === "string") ? (ids as string[]) : null;
}

// ⌘⇧[ / ⌘⇧] and ⌘1–9 — the chords a page inside a tab must hand to the strip.
// ⌘T/⌘W are the native menu's and never reach a page. Codes, not keys: with
// Shift held, `[` arrives as `{` on a US layout and as something else on
// others.
function tabChord(e: KeyboardEvent): { step: number } | { nth: number } | null {
  if (e.isComposing || !isMod(e) || e.altKey) return null;
  if (e.shiftKey && e.code === "BracketLeft") return { step: -1 };
  if (e.shiftKey && e.code === "BracketRight") return { step: 1 };
  if (!e.shiftKey && /^Digit[1-9]$/.test(e.code)) return { nth: Number(e.code.slice(5)) };
  return null;
}

// WHO OWNS THOSE CHORDS. In the macOS app's windows the menu bar does: its
// key equivalents consume ⌘⇧[ ⌘⇧] ⌘1–9 before the web view ever sees them,
// and call the host API itself — so listening here too would only be a second
// handler for a key that never arrives (or, if one ever did, a double step).
// The page listeners are for a browser, which has no such menu. The UA is
// tested directly rather than through router.ts IS_NATIVE_WINDOW, which is
// top-level only; every frame of a native window carries the same marker.
const MENU_OWNS_CHORDS =
  typeof navigator !== "undefined" && /\bFused(?:Render|Bot)\/\S+/.test(navigator.userAgent);

export default function TabHost({ config: _config }: { config: Config }) {
  // TM-3: a param boundary BEFORE any frame mounts. Render-time, like Tabs.tsx.
  window._fusedParamBoundary = true;

  const [, rerender] = useReducer((n: number) => n + 1, 0);

  // The whole strip. Initialised once from the host URL — a fresh window on
  // `/` arrives here already rewritten to Home by App's front door. A reload
  // keeps its ids (the entry's state survives it), so a later Back into an
  // entry from before the reload still matches tabs by id.
  const S = useRef<{ tabs: Map<string, Tab>; order: string[]; mounted: string[]; active: string } | null>(null);
  if (S.current === null) {
    const parsed = parseHostUrl(location.pathname + location.search);
    const ids = stateIds(history.state as HistoryTabsState | null, parsed.tabs.length);
    const tabs = new Map<string, Tab>();
    const order = parsed.tabs.map((u, i) => {
      const t = makeTab(u, ids?.[i]);
      tabs.set(t.id, t);
      return t.id;
    });
    const active = order[parsed.active] ?? order[0];
    S.current = { tabs, order, mounted: [active], active };
  }
  const st = S.current;

  const frames = useRef(new Map<string, HTMLIFrameElement>()).current;
  const wiring = useRef(new Map<string, FrameWiring>()).current;
  const wireOf = (id: string): FrameWiring => {
    let w = wiring.get(id);
    if (!w) {
      w = { hook: null, observer: null, keyWin: null, pop: null, loaded: false };
      wiring.set(id, w);
    }
    return w;
  };

  const brand = displayName();
  const labelOf = (t: Tab) => tabLabel(t.url, t.docTitle, brand);

  // ---- the URL mirror (tabs-lib) -----------------------------------------
  const hostUrl = (): { url: string; state: HistoryTabsState } => {
    const idx = Math.max(0, st.order.indexOf(st.active));
    const urls = st.order.map((id) => hostLocation(st.tabs.get(id)?.url ?? "/home"));
    return { url: buildHostUrl(urls, idx), state: { fusedActiveTab: idx, fusedTabIds: st.order.slice() } };
  };

  // A tab op: a real history entry, so Back can return to the previous tab.
  const pushMirror = () => {
    const { url, state } = hostUrl();
    history.pushState(state, "", url);
  };

  // A location sync: the current entry, rewritten only when something it says
  // changed (TM-7) — every write fires the shell's fused:urlchange, and the
  // sidebar re-renders on each one.
  const replaceMirror = () => {
    const { url, state } = hostUrl();
    const cur = history.state as HistoryTabsState | null;
    const sameState =
      cur?.fusedActiveTab === state.fusedActiveTab &&
      JSON.stringify(cur?.fusedTabIds ?? null) === JSON.stringify(state.fusedTabIds);
    if (location.pathname + location.search === url && sameState) return;
    // Spread the entry's own state through (router.replaceSearch's rule): a
    // nav hint someone else stamped on it must survive a location sync.
    history.replaceState({ ...(cur && typeof cur === "object" ? cur : null), ...state }, "", url);
  };

  // The native window's title follows the host's document.title.
  const syncTitle = () => {
    const t = st.tabs.get(st.active);
    if (!t) return;
    const label = labelOf(t);
    document.title = t.docTitle.trim() || (label === brand ? brand : `${label} – ${brand}`);
  };

  // Hidden signal (pause-while-hidden.ts): every mounted frame learns whether
  // it is the active one, and hears about it when that changes.
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

  // ---- frame wiring, per load ---------------------------------------------
  const readTab = (id: string) => {
    const t = st.tabs.get(id);
    const iframe = frames.get(id);
    if (!t || !iframe) return;
    const loc = readTabLoc(iframe);
    if (loc) t.url = stripTabMarker(loc);
    try {
      t.docTitle = iframe.contentDocument?.title ?? "";
    } catch {
      /* cross-origin — never for a tab */
    }
    replaceMirror();
    if (id === st.active) syncTitle();
    rerender();
  };

  // ONE function for the document's life: it is added to each frame's window
  // on load and removed on the next, and a per-render copy would never match
  // the one it is asked to remove.
  const onKeyInTab = useRef((e: KeyboardEvent) => {
    const chord = tabChord(e);
    if (!chord) return;
    e.preventDefault();
    if ("step" in chord) live.current.step(chord.step);
    else live.current.nth(chord.nth);
  }).current;

  const unwire = (id: string) => {
    const w = wiring.get(id);
    if (!w) return;
    detachEmbedUrlChange(w.hook);
    w.hook = null;
    w.observer?.disconnect();
    w.observer = null;
    try {
      w.keyWin?.removeEventListener("keydown", onKeyInTab);
      w.pop?.win.removeEventListener("popstate", w.pop.handler);
    } catch {
      /* window gone */
    }
    w.keyWin = null;
    w.pop = null;
  };

  const onFrameLoad = (id: string, iframe: HTMLIFrameElement) => {
    // Fade in rather than flash an unpainted frame (`.is-loaded`, see
    // shell-tabs.css). On the node, not through state, so reload can re-arm it.
    iframe.classList.add("is-loaded");
    unwire(id);
    const w = wireOf(id);
    w.loaded = true;
    // The new document's window is a new Window object: the hidden flag,
    // every listener and the url hook all have to be put back (TM-7).
    // A fresh document starts with no flag at all, so this always writes.
    signalVisibility(id);
    const win = iframe.contentWindow;
    w.hook = attachEmbedUrlChange(iframe, () => readTab(id));
    // NOTHING NAVIGATES UNSEEN. Back/Forward walk the window's JOINT history,
    // which interleaves every tab's own entries: a step can land in a tab
    // that is not the active one. That tab is shown, so the reader sees what
    // their Back just did — a traversal is the only thing that fires popstate.
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
      if (!MENU_OWNS_CHORDS) {
        win?.addEventListener("keydown", onKeyInTab);
        w.keyWin = win;
      }
      // A title can change with no URL change at all (a file view learns its
      // rendered <title> after the fetch) — watch the head for it.
      const doc = iframe.contentDocument;
      const target = doc?.head ?? doc?.documentElement;
      if (target) {
        w.observer = new MutationObserver(() => {
          const t = st.tabs.get(id);
          const title = doc?.title ?? "";
          if (!t || t.docTitle === title) return;
          t.docTitle = title;
          if (id === st.active) syncTitle();
          rerender();
        });
        w.observer.observe(target, { subtree: true, childList: true, characterData: true });
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

  const addTab = (url: string, at?: number): Tab => {
    const t = makeTab(url);
    st.tabs.set(t.id, t);
    const order = st.order.slice();
    order.splice(at ?? order.length, 0, t.id);
    st.order = order;
    return t;
  };

  const findTab = (idOrUrl: string): Tab | undefined => {
    const byId = st.tabs.get(idOrUrl);
    if (byId) return byId;
    if (!idOrUrl.startsWith("/")) return undefined;
    for (const id of st.order) {
      const t = st.tabs.get(id);
      if (t && samePage(t.url, idOrUrl)) return t;
    }
    return undefined;
  };

  const open = (raw: string, opts?: { activate?: boolean; focusExisting?: boolean }): string => {
    // The contract is a same-origin path+query, but a caller holding a full
    // href (an anchor's `.href`) means the same thing — reduce it. A foreign
    // origin is never a tab (its frame could not be read or mirrored): it
    // goes where any external link goes, and there is no tab id to return.
    let url: string;
    try {
      const u = new URL(raw, location.href);
      if (u.origin !== location.origin) {
        window.open(u.href, "_blank", "noopener");
        return "";
      }
      url = u.pathname + u.search + u.hash;
    } catch {
      return "";
    }
    // The setup wizard is the whole window, never a tab (see sink).
    if (isWizard(url)) {
      location.assign(url);
      return "";
    }
    const activateIt = opts?.activate !== false;
    if (opts?.focusExisting !== false) {
      const hit = findTab(url);
      if (hit) {
        if (activateIt) activate(hit.id, "push");
        return hit.id;
      }
    }
    const t = addTab(url);
    if (activateIt) activate(t.id, "push");
    else {
      replaceMirror();
      rerender();
    }
    return t.id;
  };

  const close = (id?: string) => {
    const target = id ?? st.active;
    const idx = st.order.indexOf(target);
    if (idx === -1) return;
    const wasActive = target === st.active;
    // The last tab closes into a fresh Home, never into an empty window — ⌘W
    // on the last tab closes the WINDOW, which is the native side's call
    // (it asks `list()` first).
    if (st.order.length === 1) {
      const home = addTab("/");
      st.order = st.order.filter((x) => x !== target);
      dropTab(target);
      activate(home.id, "none");
      pushMirror();
      return;
    }
    const nextIdx = neighborAfterClose(idx, st.order.length);
    st.order = st.order.filter((x) => x !== target);
    dropTab(target);
    if (wasActive) activate(st.order[nextIdx], "none");
    pushMirror();
    rerender();
  };

  const dropTab = (id: string) => {
    unwire(id);
    wiring.delete(id);
    st.tabs.delete(id);
    st.mounted = st.mounted.filter((x) => x !== id);
  };

  const step = (delta: number) => {
    if (st.order.length < 2) return;
    const idx = st.order.indexOf(st.active);
    activate(st.order[stepIndex(idx, delta, st.order.length)], "push");
  };

  // ⌘1–8 pick that tab; ⌘9 (and anything past the end) is the last, as in
  // every browser.
  const nth = (n: number) => {
    if (!st.order.length) return;
    const idx = n >= 9 || n > st.order.length ? st.order.length - 1 : Math.max(0, n - 1);
    activate(st.order[idx], "push");
  };

  // Navigate a tab. Whenever the tab's CURRENT page is one of this SPA's (it
  // has a router to hear it), the hop is handed to that router — the original
  // call when there is one (router.ts NavCall: nav hints and the `_side`
  // carry are derived in there, from the tab's page), so its own leave guard
  // asks first. A destination the router cannot show (`/render`) still goes
  // through it, as a `load`: the guard asks, THEN the tab leaves by a full
  // load. Only a frame with no router — not loaded yet, or itself a
  // `/render` page — is loaded straight from here.
  const navigateTab = (id: string, url: string, call?: NavCall) => {
    const t = st.tabs.get(id);
    const iframe = frames.get(id);
    if (!t) return;
    const src = tabSrc(url);
    const w = wireOf(id);
    const win = iframe?.contentWindow;
    if (iframe && win && w.loaded && isSpaUrl(t.url)) {
      const detail: NavDetail = !isSpaUrl(url)
        ? { fn: "load", url: src }
        : (call ?? { fn: "navigateUrl", url });
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
      iframe.src = src;
    } else {
      // Not mounted yet: replace the tab outright so it mounts on the url.
      const fresh = makeTab(url, t.id);
      st.tabs.set(id, fresh);
      rerender();
    }
  };

  // The setup wizard is the whole window (App), not a page a tab can show: a
  // hop to it from the host leaves tabs for a full load of the wizard, and
  // the wizard's own exit lands on a fresh one-tab host.
  const isWizard = (url: string) => url.split(/[?#]/, 1)[0] === ONBOARDING_PATH;
  const sink = (url: string, call?: NavCall) => {
    if (isWizard(url)) {
      location.assign(url);
      return;
    }
    navigateTab(st.active, url, call);
  };

  const reloadActive = () => {
    const t = st.tabs.get(st.active);
    const iframe = frames.get(st.active);
    if (!t || !iframe) return;
    // A fresh src, never `location.reload()`: a hop inside the tab may have
    // dropped `?embed=1` from its URL (navigate() starts a fresh query), and
    // reloading THAT would load the whole chrome-full shell inside the tab.
    wireOf(t.id).loaded = false;
    iframe.classList.remove("is-loaded");
    iframe.src = tabSrc(t.url);
  };

  const activeWin = (): Window | null => frames.get(st.active)?.contentWindow ?? null;

  // ---- popstate: reconcile in place -----------------------------------------
  // The entry names a tab set; the live tabs are the truth about where each
  // one is now (their own history covers that), so nothing is navigated and
  // nothing is removed — tabs the entry lacks stay, tabs it names that are
  // gone come back (lazily: unmounted until shown), and the entry's active
  // tab is selected.
  const onPopState = () => {
    const parsed = parseHostUrl(location.pathname + location.search);
    const state = history.state as HistoryTabsState | null;
    // Ids are only an optimisation: an entry whose ids name none of the live
    // tabs (written before a reload whose own entry lost its state to one of
    // App's render-time rewrites) matches by page instead, or every tab would
    // come back a second time beside itself.
    const stamped = stateIds(state, parsed.tabs.length);
    const ids = stamped && stamped.some((id) => st.tabs.has(id)) ? stamped : null;
    const used = new Set<string>();
    const parsedIds: string[] = [];
    parsed.tabs.forEach((u, i) => {
      const want = ids?.[i];
      let id: string | undefined;
      if (want && st.tabs.has(want) && !used.has(want)) id = want;
      else if (!ids) {
        id = st.order.find((x) => !used.has(x) && samePage(st.tabs.get(x)!.url, u));
      }
      if (!id) {
        const t = makeTab(u, want && !st.tabs.has(want) ? want : undefined);
        st.tabs.set(t.id, t);
        const prev = parsedIds[parsedIds.length - 1];
        const order = st.order.slice();
        order.splice(prev ? order.indexOf(prev) + 1 : 0, 0, t.id);
        st.order = order;
        id = t.id;
      }
      used.add(id);
      parsedIds.push(id);
    });
    const idx = typeof state?.fusedActiveTab === "number" ? state.fusedActiveTab : parsed.active;
    activate(parsedIds[idx] ?? parsedIds[parsed.active] ?? st.order[0], "replace");
  };

  // Handlers above close over `st` (a stable ref) and stable maps, so the
  // latest copies can be installed once and still be right — but they are
  // re-created per render, so the effects below read them through a ref.
  const live = useRef({ onPopState, sink, open, close, step, nth });
  live.current = { onPopState, sink, open, close, step, nth };

  // After a switch, the newly shown frame takes focus — so the keyboard is in
  // the page the reader just chose, not left on the strip or a hidden frame.
  const focusPending = useRef(true);
  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    try {
      frames.get(st.active)?.contentWindow?.focus();
    } catch {
      /* not loaded yet — the load focuses nothing, which is fine */
    }
  });

  // The global, the sink and the listeners exist exactly while this is
  // mounted. A layout effect so Python's very first probe after the first
  // paint already finds the API.
  useLayoutEffect(() => {
    // NO METHOD THROWS. Native code reads a throw from `evaluateJavaScript` as
    // "no tab host here" and falls back to opening a separate window — the
    // exact thing this feature exists to stop — so every entry point answers
    // with its documented fallback instead.
    const safe = <T,>(fallback: T, fn: () => T): T => {
      try {
        return fn();
      } catch (err) {
        console.error("[shell-tabs]", err);
        return fallback;
      }
    };
    const api: FusedTabsApi = {
      open: (url, opts) => safe("", () => live.current.open(String(url), opts)),
      select: (idOrUrl) =>
        safe(false, () => {
          const t = findTab(String(idOrUrl));
          if (!t) return false;
          activate(t.id, "push");
          return true;
        }),
      close: (id) => safe(undefined, () => live.current.close(id)),
      // Urls in the VIEW spelling, as `_tabs` writes them (hostLocation): one
      // spelling for every reader, so a prefix check on the Python side sees
      // what the address bar says.
      list: () =>
        safe([], () =>
          st.order.map((id) => {
            const t = st.tabs.get(id)!;
            return { id, url: hostLocation(t.url), title: labelOf(t), active: id === st.active };
          }),
        ),
      // Plain data only: this is an `evaluateJavaScript` result, and a Window
      // in it is an unsupported type — an error native reads as "no host".
      active: () =>
        safe(null, () => {
          const t = st.tabs.get(st.active);
          return t ? { id: t.id, url: hostLocation(t.url) } : null;
        }),
      next: () => safe(undefined, () => live.current.step(1)),
      prev: () => safe(undefined, () => live.current.step(-1)),
      nth: (n) => safe(undefined, () => live.current.nth(Number(n))),
      reloadActive: () => safe(undefined, reloadActive),
      back: () => safe(undefined, () => activeWin()?.history.back()),
      forward: () => safe(undefined, () => activeWin()?.history.forward()),
      navigateActive: (url) => safe(undefined, () => live.current.sink(String(url))),
      print: () => safe(undefined, () => activeWin()?.print()),
      openHome: () => safe("", () => live.current.open("/", { focusExisting: false })),
    };
    window._fusedTabs = api;
    installNavSink((href, call) => live.current.sink(href, call));
    const onPop = () => live.current.onPopState();
    window.addEventListener("popstate", onPop);
    // The same chords with focus in the host's own chrome (the sidebar).
    if (!MENU_OWNS_CHORDS) document.addEventListener("keydown", onKeyInTab);
    // The first mirror: the host URL in its canonical spelling, with ids in
    // the entry so Back into it matches tabs by id.
    replaceMirror();
    syncTitle();
    return () => {
      delete window._fusedTabs;
      installNavSink(null);
      window.removeEventListener("popstate", onPop);
      document.removeEventListener("keydown", onKeyInTab);
      for (const id of Array.from(wiring.keys())) unwire(id);
      // This window survives an unmount only into the setup wizard or a
      // flag flip; either way a stale boundary would corrupt that view.
      delete window._fusedParamBoundary;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The strip is the title bar in a native window (shell-tabs.css): the
  // sidebar, outside this subtree, starts one title bar lower to clear the
  // traffic lights, keyed off a body class that lives exactly as long as this.
  useLayoutEffect(() => {
    if (!IS_NATIVE_WINDOW) return;
    document.body.classList.add("is-tab-host-native");
    return () => document.body.classList.remove("is-tab-host-native");
  }, []);

  const stripTabs = st.order.map((id) => {
    const t = st.tabs.get(id)!;
    return { id, label: labelOf(t), active: id === st.active };
  });

  return (
    <div className={"shell-tabs" + (IS_NATIVE_WINDOW ? " shell-tabs--native" : "")}>
      <TabStrip
        tabs={stripTabs}
        onSelect={(id) => activate(id, "push")}
        onClose={(id) => close(id)}
        onAdd={() => open("/", { focusExisting: false })}
        onReorder={(id, to) => {
          const from = st.order.indexOf(id);
          if (from === -1) return;
          st.order = moveItem(st.order, from, to);
          rerender();
        }}
        // A reorder is not a place to go Back to: the entry is rewritten.
        onReorderEnd={() => replaceMirror()}
      />
      <div className="shell-tabs-body">
        {/* MOUNT order, append/remove only (TM-5) — never strip order. */}
        {st.mounted.map((id) => {
          const t = st.tabs.get(id);
          if (!t) return null;
          return (
            <iframe
              key={id}
              ref={(el) => {
                if (el) frames.set(id, el);
                else if (!st.tabs.has(id)) frames.delete(id);
              }}
              className="shell-tab-frame"
              title={labelOf(t)}
              src={t.src}
              onLoad={(e) => onFrameLoad(id, e.currentTarget)}
              style={id === st.active ? undefined : { display: "none" }}
            />
          );
        })}
      </div>
    </div>
  );
}
