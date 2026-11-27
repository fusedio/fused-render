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
import { useEffect, useLayoutEffect, useReducer, useRef } from "react";
import {
  installNavSink,
  NAV_EVENT,
  NAVIGATE_URL_EVENT,
  navigateUrl,
  type NavCall,
  type NavDetail,
} from "@platform/lib/router";
import { attachEmbedUrlChange, detachEmbedUrlChange, type UrlChangeHook } from "@platform/lib/layout-codec";
import { TAB_VISIBILITY_EVENT } from "@platform/lib/pause-while-hidden";
import { registerWorkspaceImpl, WS_BOOKMARKS_EVENT } from "@platform/lib/workspace-bridge";
import { displayName } from "@platform/lib/flavor";
import { useFavicon } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import { FallbackRail } from "@shell/claude-rail/FallbackRail";
import { ONBOARDING_PATH } from "@shell/onboarding/state";
import TabStrip from "./TabStrip";
import {
  buildHostUrl,
  HOME_URL,
  isHomeUrl,
  isSpaUrl,
  neighborAfterClose,
  normalizeOpenUrl,
  pageUrlOf,
  parseHostUrl,
  planOpen,
  readFrameLoc,
  samePage,
  stripHash,
  tabKind,
  tabLabel,
} from "./workspace-lib";

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

export default function WorkspaceHost({ config: _config }: { config: Config }) {
  const [, rerender] = useReducer((n: number) => n + 1, 0);

  // The whole strip, initialised once from the host URL. A reload keeps its ids
  // (the entry's state survives it), so Back into an older entry still matches.
  const S = useRef<{ tabs: Map<string, Tab>; order: string[]; mounted: string[]; active: string; right: string | null } | null>(null);
  if (S.current === null) {
    const parsed = parseHostUrl(location.pathname + location.search);
    const ids = stateIds(history.state as HistoryWsState | null, parsed.tabs.length);
    const titles = stateTitles(history.state as HistoryWsState | null, parsed.tabs.length);
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
    history.replaceState({ ...(cur && typeof cur === "object" ? cur : null), ...state }, "", url);
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
    if (loc) t.url = loc;
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
    const urls = st.order.map((id) => st.tabs.get(id)?.url ?? HOME_URL);
    const activeIdx = Math.max(0, st.order.indexOf(st.active));
    const plan = planOpen(urls, activeIdx, url, opts?.newTab === true);
    if (plan.kind === "activate") {
      const id = st.order[plan.index];
      activate(id, "push");
      if (plan.navigate) navigateTab(id, url, opts?.call);
      return;
    }
    if (plan.kind === "append") {
      const t = addTab(url);
      activate(t.id, "push");
      return;
    }
    navigateTab(st.active, url, opts?.call);
  };

  const dropTab = (id: string) => {
    unwire(id);
    wiring.delete(id);
    st.tabs.delete(id);
    st.mounted = st.mounted.filter((x) => x !== id);
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

  // ---- popstate: reconcile in place ------------------------------------------
  // The entry names a tab set; the live tabs are the truth about where each one
  // is now (their own history covers that), so nothing is navigated and nothing
  // removed — tabs the entry lacks stay, tabs it names that are gone come back
  // (unmounted until shown), and the entry's active tab is selected.
  const onPopState = () => {
    const parsed = parseHostUrl(location.pathname + location.search);
    st.right = parsed.right;
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
  const live = useRef({ onPopState, open, close });
  live.current = { onPopState, open, close };

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
    installNavSink((href, call) => {
      if (isWizard(href)) return false;
      live.current.open(href, { call });
      return true;
    });
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

  // Stage 2 mounts the right panel here, between the tabs and the rail.
  const rightPanel = null;

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
      <FallbackRail />
    </div>
  );
}
