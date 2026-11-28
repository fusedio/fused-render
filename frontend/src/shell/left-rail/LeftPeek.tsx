// SIDEBAR PREVIEWS' CARDS (left-peek-store has the story): one window A card
// per sidebar entry — a collapsed rail icon or an expanded row, one card per
// key for both — to the RIGHT of the sidebar — the Claude rail's
// chassis (DoorCard, `side: "left"`), mounted once in the shell (App.tsx,
// beside ModePeek) so a page hop never remounts a held card.
//
// WHAT A CARD SHOWS (left-peek-lib `LeftPeekShow`): a page, chrome-free, in a
// frame behind the param boundary (peek-frame `PeekFrame`) — mounted while
// the card is up and a beat after, never before its first opening; a
// project's `preview.png` while a preview (its live page once pinned, or when
// it has no still). THE HOME CARD has a file search in its head (`searchHome`,
// Home's own FilesSearch): typing lays the results over the listing, Escape
// (or emptying the box) takes them away. Everything opens IN THE CARD — a
// result, a folder in the listing — and ← Back walks the card's trail
// (left-peek-lib `HomeStep`); only Open takes the page there: the view the
// card is on, or Home with the query while the results show.
//
// A PREVIEW'S FRAME IS SHIELDED (`.claude-left-peek:not(.is-live)`, claude-
// rail.css): it takes no pointer, so the pointer over it is over the card
// (the hover's grace and the bridge read it) and a press there is a press
// inside the card — it makes the card temp, and the frame then takes the
// pointer. A frame that takes the keyboard on its own while a preview hands
// it back (peek-frame `reclaimFocus`).
//
// THE HEAD: the pin (RailPinButton, the Git and mode cards'), the icon and
// title, the Home card's ← Back and search box, and Open — the entry's Open
// (the Home card's: what it shows), then the card goes. In a workspace host
// only a favourite's card has the pin (SPEC §56 WS-28).
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { ArrowLeft } from "lucide-react";
import { NAV_EVENT, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { DoorCard } from "@shell/claude-rail/DoorCard";
import { RailPinButton, useTempOutside } from "@shell/claude-rail/RailPin";
import { FRAME_LINGER_MS, PeekFrame, usePeekFrameRules } from "@shell/claude-rail/peek-frame";
import {
  bookmarkFrameSrc,
  cardPageUrl,
  framePainted,
  frameSrcOf,
  homeSearchUrl,
  leftDoorSelector,
  leftPressVerdict,
  trailBack,
  trailFrameAt,
  trailNoResults,
  trailOpen,
  trailPage,
  trailResults,
  type HomeStep,
} from "./left-peek-lib";
import { FilesSearch } from "@apps/explorer/FilesHome";
import { useIndexStatus } from "@platform/lib/index-status";
import { NewAppInline } from "@shell/CurrentAppsSection";
import {
  closeLeftCards,
  leftCardStore,
  leftItem,
  leftMove,
  leftSidebarFolded,
  leftSize,
  leftViewPress,
  useLeftPeek,
  type LeftPeekItem,
} from "./left-peek-store";
import { useSidebarState } from "@platform/lib/hooks";
import { isWorkspaceHost, workspaceHost } from "@platform/lib/workspace-bridge";
import { useLeftRailPeek } from "./left-rail-peek-flag";
import { installLeftRailZone } from "./left-rail-zone";

/** The scroll box a door stands in: the collapsed rail's scroll run
 *  (SidebarFrame `.sidebar-rail-scroll`), or the nearest box of the expanded
 *  sidebar that scrolls (a section's body, the sidebar itself). */
function scrollerOf(door: HTMLElement): HTMLElement | null {
  const run = door.closest<HTMLElement>(".sidebar-rail-scroll");
  if (run) return run;
  for (let el = door.parentElement; el && el.id !== "app"; el = el.parentElement) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight) return el;
    if (el.id === "sidebar") break;
  }
  return null;
}

function cssId(id: string): string {
  return "#" + id.replace(/([^\w-])/g, "\\$1");
}

/** How long the spinner waits for a framed page to say it has drawn. */
const PAINT_WAIT_MS = 20_000;
/** The "+ New app" card's height: the composer and its chips, no more. */
const COMPOSE_HEIGHT = 300;

export function LeftPeek() {
  const on = useLeftRailPeek();
  const { keys, rail } = useLeftPeek();
  // THE LEFT ZONE (left-rail-zone): watched while the switch is on and the
  // collapsed rail stands.
  const railUp = rail !== null;
  useEffect(() => (on && railUp ? installLeftRailZone() : undefined), [on, railUp]);
  // THE SIDEBAR COLLAPSING OR EXPANDING puts down every card that is not
  // held; a held one re-places beside the new layout (left-peek-store
  // `leftSidebarFolded`).
  const { collapsed } = useSidebarState();
  const foldWas = useRef(collapsed);
  useEffect(() => {
    if (foldWas.current === collapsed) return;
    foldWas.current = collapsed;
    leftSidebarFolded();
  }, [collapsed]);
  // A PAGE HOP puts down every card that is not held (rail-pin-lib
  // `hopKeeps`): the URL's path changed.
  useEffect(() => {
    let path = location.pathname;
    const check = () => {
      if (location.pathname === path) return;
      path = location.pathname;
      closeLeftCards((_k, s) => s.held);
    };
    // In a workspace host the active tab's own hop (a search result, a
    // favourite's Open) reaches the host's URL as a location sync — a
    // replace, announced as `fused:urlchange` — not as a navigation.
    const sync = isWorkspaceHost() ? "fused:urlchange" : null;
    window.addEventListener(NAV_EVENT, check);
    window.addEventListener("popstate", check);
    if (sync) window.addEventListener(sync, check);
    return () => {
      window.removeEventListener(NAV_EVENT, check);
      window.removeEventListener("popstate", check);
      if (sync) window.removeEventListener(sync, check);
    };
  }, []);
  return (
    <>
      {keys.map((k) => (
        <LeftCard key={k} itemKey={k} on={on} />
      ))}
    </>
  );
}

function LeftCard({ itemKey, on }: { itemKey: string; on: boolean }) {
  const store = leftCardStore(itemKey);
  const { items, rail } = useLeftPeek();
  const item = items.get(itemKey) ?? null;
  // The facts the card last drew, while it fades out after its icon went.
  const last = useRef<LeftPeekItem | null>(item);
  if (item) last.current = item;
  const it = item ?? last.current;
  const card = store.useCard();
  const { up, pinned, held } = card;

  // THE FRAME: up, and a beat after (its fade).
  const [live, setLive] = useState(false);
  // Each time the frame mounts afresh (the card up again after its fade ran
  // out) is a new frame: `mounts` counts them (`frameKey` below).
  const [mounts, setMounts] = useState(0);
  const liveRef = useRef(false);
  liveRef.current = live;
  useEffect(() => {
    if (up) {
      if (!liveRef.current) setMounts((n) => n + 1);
      setLive(true);
      return;
    }
    const t = window.setTimeout(() => setLive(false), FRAME_LINGER_MS);
    return () => window.clearTimeout(t);
  }, [up]);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const temp = up && pinned && !held;
  usePeekFrameRules({ store, frameRef, up, temp, pinOnBlur: false, reclaimFocus: true, frameBlurCloses: true });
  // A TEMP CARD GOES ON A PRESS OUTSIDE — the left sidebar's own view
  // (left-peek-lib `leftPressVerdict`): the rest of the collapsed sidebar and
  // the other left cards close nothing; the page and the Claude rail do.
  useTempOutside({
    active: temp,
    flag: store.press,
    cardSelectors: [cssId(store.id)],
    ownSelectors: [leftDoorSelector(itemKey)],
    close: () => store.close(),
    classify: leftPressVerdict,
    viewFlag: leftViewPress,
  });

  // A PROJECT'S STILL (left-peek-lib `frameSrcOf`): drawn while a preview;
  // one that does not load gives way to the live page.
  const show = it?.show ?? null;
  const still = show?.kind === "app" ? show.still : null;
  const [stillFailed, setStillFailed] = useState<string | null>(null);
  const failed = still !== null && stillFailed === still;
  // THE HOME CARD'S TRAIL (header; `searchHome`): its steps, its frame's
  // address (changed only to show another step — the frame's own hops are
  // its own), and where the frame says it is.
  const searchHome = show?.kind === "frame" ? (show.searchHome ?? null) : null;
  const homeSrc = searchHome !== null && show?.kind === "frame" ? show.src : null;
  const [trail, setTrail] = useState<HomeStep[] | null>(null);
  const [homeFrame, setHomeFrame] = useState<string | null>(homeSrc);
  // Each load the card asks for is a new frame, even on the address the frame
  // was first given (Back to it after the frame hopped on its own).
  const [frameGen, setFrameGen] = useState(0);
  const frameAt = useRef<string | null>(null);
  const [clearKey, setClearKey] = useState(0);
  const steps: HomeStep[] = homeSrc === null ? [] : (trail ?? [{ kind: "page", url: homeSrc }]);
  const topStep = steps[steps.length - 1] ?? null;
  const pageUrl = trailPage(steps);
  // The card put down (its fade over) starts over on ~.
  useEffect(() => {
    if (live && homeSrc !== null) return;
    setTrail(null);
    setHomeFrame(homeSrc);
    frameAt.current = null;
  }, [live, homeSrc]);
  // The step on top asks for another page than the frame is on: load it.
  useEffect(() => {
    if (pageUrl === null || pageUrl === frameAt.current) return;
    setHomeFrame(pageUrl);
    setFrameGen((n) => n + 1);
  }, [pageUrl]);
  const src = homeSrc !== null ? homeFrame : show ? frameSrcOf(show, { pinned, stillFailed: failed }) : null;
  const [loaded, setLoaded] = useState<string | null>(null);
  // THE SPINNER (owner, 2026-11-27: "sometimes it takes a long time to load
  // and it is just a blank screen"): the frame's `load` is only its shell's
  // document — the page behind it may still be resolving its view and
  // loading the file's own frame. The card spins until the framed page shows
  // something (left-peek-lib `framePainted`), asked every 120 ms from the
  // load; a page that never says so is taken as shown after PAINT_WAIT_MS.
  const [painted, setPainted] = useState<string | null>(null);
  const paintPoll = useRef(0);
  useEffect(() => () => window.clearInterval(paintPoll.current), []);
  const compose = show?.kind === "compose";
  // THE HOME CARD'S SEARCH: the box in the head, its results in a layer
  // over the frame while they are the step on top.
  const [resultsEl, setResultsEl] = useState<HTMLDivElement | null>(null);
  const homeBase = useCallback((t: HomeStep[] | null): HomeStep[] => t ?? (homeSrc !== null ? [{ kind: "page", url: homeSrc }] : []), [homeSrc]);
  const onSearchActive = useCallback(
    (active: boolean) => setTrail((t) => (active ? trailResults(homeBase(t)) : trailNoResults(homeBase(t)))),
    [homeBase],
  );
  // Typing again over a page the results led to brings them back on top.
  const onSearchInput = useCallback((value: string) => {
    if (value.trim()) setTrail((t) => trailResults(homeBase(t)));
  }, [homeBase]);
  // A RESULT OPENS IN THE CARD: a row's link (caught before the row's own
  // click takes the page there) or the box's Enter / open row (`onGo`).
  const openInCard = useCallback((frameSrc: string | null) => {
    if (frameSrc) setTrail((t) => trailOpen(homeBase(t), frameSrc));
  }, [homeBase]);
  const onGo = useCallback((fsPath: string) => openInCard(bookmarkFrameSrc(urlForFsPath(fsPath))), [openInCard]);
  useEffect(() => {
    if (!resultsEl) return;
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = (e.target as Element | null)?.closest?.("a[href]");
      if (!a || !resultsEl.contains(a)) return;
      const frameSrc = bookmarkFrameSrc(a.getAttribute("href") ?? "");
      if (!frameSrc) return;
      e.preventDefault();
      e.stopPropagation();
      openInCard(frameSrc);
    };
    resultsEl.addEventListener("click", onClick, true);
    return () => resultsEl.removeEventListener("click", onClick, true);
  }, [resultsEl, openInCard]);
  const back = useCallback(() => {
    if (topStep?.kind === "results") setClearKey((n) => n + 1);
    setTrail((t) => trailBack(homeBase(t)));
  }, [topStep, homeBase]);
  // THE FRAME WENT SOMEWHERE ON ITS OWN (a folder opened in the listing):
  // the trail hears it (left-peek-lib `trailFrameAt`).
  const frameHops = useRef<(() => void) | null>(null);
  useEffect(() => () => frameHops.current?.(), []);
  const watchFrame = useCallback(
    (inner: HTMLIFrameElement) => {
      frameHops.current?.();
      frameHops.current = null;
      if (homeSrc === null) return;
      let w: Window | null = null;
      try {
        w = inner.contentWindow;
      } catch {
        return;
      }
      if (!w) return;
      const win = w;
      const report = () => {
        let url: string;
        try {
          url = win.location.pathname + win.location.search + win.location.hash;
        } catch {
          return;
        }
        frameAt.current = url;
        setTrail((t) => trailFrameAt(homeBase(t), url));
      };
      const kinds = [NAV_EVENT, "popstate", "hashchange", "fused:urlchange"];
      for (const k of kinds) win.addEventListener(k, report);
      frameHops.current = () => {
        for (const k of kinds) win.removeEventListener(k, report);
      };
      report();
    },
    [homeSrc, homeBase],
  );
  // READY PER FRAME, NOT PER ADDRESS (CodeRabbit on #2495): a new frame on
  // the same address — the card back after its fade ran out — is not loaded
  // or painted yet. `loaded` and `painted`
  // name the frame they are about, and a new one drops the old one's poll.
  const frameKey = src !== null ? `${mounts}#${frameGen}#${src}` : null;
  useEffect(() => () => window.clearInterval(paintPoll.current), [frameKey]);
  const onFrameLoad = useCallback(
    (inner: HTMLIFrameElement) => {
      setLoaded(frameKey);
      window.clearInterval(paintPoll.current);
      const at = frameKey;
      const since = Date.now();
      const ask = () => {
        let ok = false;
        try {
          ok = framePainted(inner.contentDocument);
        } catch {
          ok = true;
        }
        if (!ok && inner.isConnected && Date.now() - since < PAINT_WAIT_MS) return false;
        window.clearInterval(paintPoll.current);
        setPainted(at);
        return true;
      };
      if (!ask()) paintPoll.current = window.setInterval(ask, 120);
      watchFrame(inner);
    },
    [frameKey, watchFrame],
  );

  const title = it?.title ?? "";
  const pinless = !!it?.pinless;
  // THE HOME CARD'S OPEN: the view it is on — Home with the query while the
  // results show, else the framed page as the page's own address.
  const searchBox = useRef<HTMLDivElement | null>(null);
  const homeOpenUrl = (): string | null => {
    if (homeSrc === null || !topStep) return null;
    if (topStep.kind === "results") return homeSearchUrl(searchBox.current?.querySelector("input")?.value ?? "");
    return cardPageUrl(topStep.url);
  };
  const open = useCallback(
    (close: () => void, to: string | null, newTab: boolean) => {
      if (to !== null) {
        // Shift, in a workspace host: a new tab (the host's own open).
        const host = newTab ? workspaceHost() : null;
        if (host) {
          host.openPage(to, { newTab: true });
          close();
          return;
        }
        void navigateUrl(to).then((ok) => {
          if (ok !== false) close();
        });
        return;
      }
      const now = leftItem(itemKey);
      if (!now) return;
      const went = now.onOpen(newTab ? { newTab } : undefined);
      if (went instanceof Promise) {
        void went.then((ok) => {
          if (ok !== false) close();
        });
      } else close();
    },
    [itemKey],
  );

  return (
    <DoorCard
      store={store}
      side="left"
      rail={rail}
      ownMove={leftMove}
      sizeStore={leftSize}
      viewFlag={leftViewPress}
      scrollerOf={scrollerOf}
      className={"claude-left-peek" + (pinned ? " is-live" : "")}
      ariaLabel={title || "Preview"}
      enabled={on && item !== null}
      pinless={pinless}
      subOpen={false}
      {...(compose ? { wishHeight: COMPOSE_HEIGHT } : {})}
    >
      {(api) => (
        <>
          <div className="claude-rail-peek-head">
            <div className="claude-rail-peek-row">
              {/* No ×: the pin in its seat (and the keyboard's landing) —
                  none on a pinless card (a workspace host's non-favourite,
                  left-peek-lib `leftCardPinnable`): Open is the landing. */}
              {!pinless && <RailPinButton variant="legacy" btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}
              {homeSrc !== null && steps.length > 1 && (
                <button type="button" className="claude-rail-peek-btn claude-rail-peek-close" aria-label="Back" data-hint="Back" onClick={back}>
                  <ArrowLeft size={14} aria-hidden="true" />
                </button>
              )}
              <span className="claude-rail-tasks-title claude-rail-mode-title claude-left-peek-title">
                {it && (
                  <span className="claude-rail-mode-icon" aria-hidden="true">
                    {it.icon}
                  </span>
                )}
                <span className="claude-left-peek-name">{title}</span>
              </span>
              {live && searchHome !== null ? (
                <CardSearch
                  home={searchHome}
                  resultsHost={resultsEl}
                  onActive={onSearchActive}
                  onInput={onSearchInput}
                  onGo={onGo}
                  clearKey={clearKey}
                  boxRef={searchBox}
                />
              ) : (
                <span className="claude-rail-peek-gap" aria-hidden="true" />
              )}
              <button
                type="button"
                className="btn claude-rail-peek-open"
                ref={pinless ? api.closeRef : undefined}
                // Shift: a new tab, in a workspace host.
                onClick={(e) => open(api.close, homeOpenUrl(), e.shiftKey && isWorkspaceHost())}
              >
                Open
              </button>
            </div>
          </div>
          <div
            className={
              "claude-rail-peek-body claude-rail-mode-body claude-left-peek-body" +
              ((frameKey !== null && loaded === frameKey) || (src === null && still !== null) ? " is-loaded" : "") +
              (compose ? " is-compose" : "")
            }
          >
            {/* The composer itself, no frame: mounted on the first opening and
                kept while the card is up, so a half-typed prompt survives the
                pointer leaving and coming back within the fade. */}
            {live && compose && <NewAppInline onCreated={api.close} />}
            {live && src !== null && painted !== frameKey && (
              <span className="claude-left-peek-wait" role="status" aria-label="Loading">
                <span className="claude-left-peek-spin" aria-hidden="true" />
              </span>
            )}
            {live && src === null && still !== null && (
              <img className="claude-left-peek-still" src={still} alt="" draggable={false} onError={() => setStillFailed(still)} />
            )}
            {live && src !== null && (
              <PeekFrame key={src + "#" + frameGen} src={src} title={title} frameRef={frameRef} onLoad={onFrameLoad} />
            )}
            {live && searchHome !== null && <div ref={setResultsEl} className="claude-left-peek-results" hidden={topStep?.kind !== "results"} />}
          </div>
        </>
      )}
    </DoorCard>
  );
}

/** THE HOME CARD'S SEARCH BOX: Home's own file search (FilesSearch — the
 *  same ranking, keys and rows), the box here in the head, its results in
 *  `resultsHost` over the listing. Its own index poll, as Home's page has
 *  (SearchWidget `useSearchHost`), only while a query is live; no `?q=`
 *  carried in (the page's address is not this card's). */
function CardSearch({
  home,
  resultsHost,
  onActive,
  onInput,
  onGo,
  clearKey,
  boxRef,
}: {
  home: string;
  resultsHost: HTMLElement | null;
  onActive: (active: boolean) => void;
  onInput: (value: string) => void;
  onGo: (fsPath: string) => void;
  clearKey: number;
  boxRef: MutableRefObject<HTMLDivElement | null>;
}) {
  const [active, setActive] = useState(false);
  const [nonce, setNonce] = useState(0);
  const indexScan = useIndexStatus(active, nonce);
  const changed = useCallback(
    (a: boolean) => {
      setActive(a);
      onActive(a);
    },
    [onActive],
  );
  // The box going (the card's fade ran out) takes its results with it.
  useEffect(() => () => onActive(false), [onActive]);
  return (
    <div className="claude-left-peek-search" ref={boxRef} onInput={(e) => onInput((e.target as HTMLInputElement).value ?? "")}>
      <FilesSearch
        home={home}
        initialQuery=""
        indexScan={indexScan}
        onActiveChange={changed}
        onScanRequested={() => setNonce((n) => n + 1)}
        autoFocus={false}
        resultsHost={resultsHost}
        onGo={onGo}
        clearKey={clearKey}
      />
    </div>
  );
}
