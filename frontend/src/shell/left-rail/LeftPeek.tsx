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
// it has no still); the search card's result opens ON THE PAGE, never in the
// card.
//
// A PREVIEW'S FRAME IS SHIELDED (`.claude-left-peek:not(.is-live)`, claude-
// rail.css): it takes no pointer, so the pointer over it is over the card
// (the hover's grace and the bridge read it) and a press there is a press
// inside the card — it makes the card temp, and the frame then takes the
// pointer. A frame that takes the keyboard on its own while a preview hands
// it back (peek-frame `reclaimFocus`).
//
// THE HEAD: the pin (RailPinButton, the Git and mode cards'), the icon and
// title, and Open — the icon's own click, then the card goes.
import { useCallback, useEffect, useRef, useState } from "react";
import { NAV_EVENT, navigateUrl } from "@platform/lib/router";
import { DoorCard } from "@shell/claude-rail/DoorCard";
import { RailPinButton, useTempOutside } from "@shell/claude-rail/RailPin";
import { FRAME_LINGER_MS, PeekFrame, usePeekFrameRules } from "@shell/claude-rail/peek-frame";
import { framePainted, frameSrcOf, leftDoorSelector, leftPressVerdict, searchHopTarget } from "./left-peek-lib";
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
    window.addEventListener(NAV_EVENT, check);
    window.addEventListener("popstate", check);
    return () => {
      window.removeEventListener(NAV_EVENT, check);
      window.removeEventListener("popstate", check);
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
  useEffect(() => {
    if (up) {
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
  const src = show ? frameSrcOf(show, { pinned, stillFailed: failed }) : null;
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
  // THE SEARCH CARD'S RESULT OPENS ON THE PAGE: the frame's own hop (a result
  // pressed, a path typed) is taken to the top page, and the frame starts
  // over on its search.
  const [round, setRound] = useState(0);
  const isSearch = show?.kind === "search";
  const onFrameLoad = useCallback(
    (inner: HTMLIFrameElement) => {
      setLoaded(src);
      window.clearInterval(paintPoll.current);
      const at = src;
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
      if (!isSearch) return;
      let w: Window | null = null;
      try {
        w = inner.contentWindow;
      } catch {
        return;
      }
      if (!w) return;
      const win = w;
      const hop = () => {
        let to: string | null = null;
        try {
          to = searchHopTarget(win.location.pathname + win.location.search);
        } catch {
          return;
        }
        if (!to) return;
        win.removeEventListener(NAV_EVENT, hop);
        setRound((n) => n + 1);
        void navigateUrl(to);
      };
      win.addEventListener(NAV_EVENT, hop);
      hop();
    },
    [src, isSearch],
  );

  // THE SEARCH CARD TAKES THE KEYBOARD WHEN PRESSED: the press that made it
  // temp landed on the shield (header), so the box is put under the reader's
  // hands — after the press's own focus change (a mousedown on the card).
  useEffect(() => {
    if (!isSearch || !pinned) return;
    const t = window.setTimeout(() => {
      try {
        const inner = frameRef.current?.contentDocument?.querySelector("iframe");
        inner?.contentDocument?.querySelector<HTMLInputElement>("input.files-search-input")?.focus({ preventScroll: true });
      } catch {
        // A frame not ours to reach: the reader clicks into it.
      }
    }, 60);
    return () => window.clearTimeout(t);
  }, [isSearch, pinned]);

  const title = it?.title ?? "";
  const open = useCallback(
    (close: () => void) => {
      const now = leftItem(itemKey);
      if (!now) return;
      const went = now.onOpen();
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
      subOpen={false}
      {...(compose ? { wishHeight: COMPOSE_HEIGHT } : {})}
    >
      {(api) => (
        <>
          <div className="claude-rail-peek-head">
            <div className="claude-rail-peek-row">
              {/* No ×: the pin in its seat (and the keyboard's landing). */}
              <RailPinButton variant="legacy" btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />
              <span className="claude-rail-tasks-title claude-rail-mode-title claude-left-peek-title">
                {it && (
                  <span className="claude-rail-mode-icon" aria-hidden="true">
                    {it.icon}
                  </span>
                )}
                <span className="claude-left-peek-name">{title}</span>
              </span>
              <span className="claude-rail-peek-gap" aria-hidden="true" />
              <button type="button" className="btn claude-rail-peek-open" onClick={() => open(api.close)}>
                Open
              </button>
            </div>
          </div>
          <div
            className={
              "claude-rail-peek-body claude-rail-mode-body claude-left-peek-body" +
              ((src !== null && loaded === src) || (src === null && still !== null) ? " is-loaded" : "") +
              (compose ? " is-compose" : "")
            }
          >
            {/* The composer itself, no frame: mounted on the first opening and
                kept while the card is up, so a half-typed prompt survives the
                pointer leaving and coming back within the fade. */}
            {live && compose && <NewAppInline onCreated={api.close} />}
            {live && src !== null && painted !== src && (
              <span className="claude-left-peek-wait" role="status" aria-label="Loading">
                <span className="claude-left-peek-spin" aria-hidden="true" />
              </span>
            )}
            {live && src === null && still !== null && (
              <img className="claude-left-peek-still" src={still} alt="" draggable={false} onError={() => setStillFailed(still)} />
            )}
            {live && src !== null && (
              <PeekFrame key={src + "#" + round} src={src} title={title} frameRef={frameRef} onLoad={onFrameLoad} />
            )}
          </div>
        </>
      )}
    </DoorCard>
  );
}
