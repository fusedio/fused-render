// THE MODE CARD OFF THE RAIL'S MODE DOORS (owner, 2026-11-20; mode-peek-store
// has the story): a file page's other modes — every entry of its top-right
// mode menu but the active one — are doors on the rail above Git, and this is
// their one window A card. The Git card's twin (GitPeek): mounted once in the
// shell, window A's chassis (DoorCard), the rules between the cards in
// ClaudeRail.
//
// WHAT IT IS: the file drawn in the mode whose door opened it — the embed
// frame a panel pane shows (`RailModes.src`). The frame is mounted while the
// card is up and a beat after (its fade), never before the first opening.
//
// THE HEAD: the pin (or the × without the pin model), the mode's icon and
// name, and Open — the page's own switch to that mode (the menu's); Open
// closes the card. No room beside the rail: the door's press is that switch
// (DoorCard `onNoRoomPress`).
//
// THE FRAME STANDS BEHIND A PARAM BOUNDARY (`ModeFrame`): a page's
// `fused.params.set` climbs to the topmost same-origin window and writes ITS
// address (runtime.js `findTarget`, D46) — from in here that is the page the
// card stands over, which would take the card's `view=` and the like into its
// own URL. So the embed frame is put inside a blank frame marked
// `_fusedParamBoundary`, the mark a panel's or a tab's shell wears: the climb
// stops below it, on the embed frame, as it does for a panel pane.
//
// THE FRAME IS ANOTHER DOCUMENT, so two of the chassis' rules are restated
// here for it: a press inside it pins a hover's preview (the window's blur
// with the focus on our frame is the only word of it), and a temp card goes
// when the focus moves on into a frame that is not ours (no second blur
// comes for that).
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from "react";
import { X } from "lucide-react";
import { DoorCard } from "./DoorCard";
import { RailPinButton } from "./RailPin";
import { useTaskHeadUnified } from "@shell/task-head-flag";
import { useFavPeekState } from "./fav-peek-store";
import {
  MODE_PEEK_ID,
  modeCardStore,
  useModeCard,
  useModeCardRail,
  useModeShown,
  useRailModes,
  useRailModesAnyRail,
} from "./mode-peek-store";

/** How long a PINNED card waits, its mode's door gone, for the next rail's
 *  (a hop: the next file's page mounts its rail after a stat). No door by
 *  then: it closes (GitPeek `GIT_FOLLOW_MS`). */
export const MODE_FOLLOW_MS = 600;
/** How long the frame outlives a close (the card's fade). */
const FRAME_LINGER_MS = 400;

interface Flusher {
  __fusedFlushEdits?: () => Promise<{ ok: boolean } | undefined>;
}

/** The editor's flush of the file the card's frame shows, if its template has
 *  one (the code template's `__fusedFlushEdits`, Preview `doSetMode`): the
 *  boundary frame, the embed frame in it, that page's shown preview frame. */
function frameFlush(outer: HTMLIFrameElement | null): Flusher["__fusedFlushEdits"] {
  try {
    const embed = outer?.contentDocument?.querySelector("iframe");
    const shownFrame = embed?.contentDocument?.querySelector<HTMLIFrameElement>(".preview-body iframe.is-shown");
    const fn = (shownFrame?.contentWindow as (Window & Flusher) | null | undefined)?.__fusedFlushEdits;
    return typeof fn === "function" ? fn : undefined;
  } catch {
    return undefined;
  }
}

/** THE CARD'S FRAME (header): a blank frame marked as a param boundary, and
 *  the embed frame on `src` inside it. Keyed by `src` where it is mounted, so
 *  a new address is a new frame. */
function ModeFrame({
  src,
  title,
  frameRef,
  onLoad,
}: {
  src: string;
  title: string;
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  onLoad: () => void;
}) {
  const onLoadRef = useRef(onLoad);
  onLoadRef.current = onLoad;
  useLayoutEffect(() => {
    const outer = frameRef.current;
    if (!outer) return;
    let inner: HTMLIFrameElement | null = null;
    const build = () => {
      const w = outer.contentWindow;
      const d = outer.contentDocument;
      if (!w || !d?.body) return;
      // Built already, in this very document (an engine that swaps the first
      // blank document for another on load builds twice otherwise).
      if (inner && inner.ownerDocument === d && inner.isConnected) return;
      w._fusedParamBoundary = true;
      d.documentElement.style.cssText = "height:100%;color-scheme:" + getComputedStyle(outer).colorScheme;
      d.body.style.cssText = "margin:0;height:100%;overflow:hidden;background:transparent";
      inner = d.createElement("iframe");
      inner.title = title;
      inner.style.cssText = "display:block;border:0;width:100%;height:100%";
      inner.addEventListener("load", () => onLoadRef.current());
      inner.src = src;
      d.body.appendChild(inner);
    };
    build();
    outer.addEventListener("load", build);
    return () => outer.removeEventListener("load", build);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);
  return <iframe ref={frameRef} className="claude-rail-mode-frame" title={title} />;
}

export function ModePeek() {
  // BESIDE THE RAIL THAT OPENED IT, else the top rail where it has mode
  // doors, else the latest rail that has (GitPeek's rule).
  const top = useFavPeekState().rail;
  const opener = useModeCardRail();
  const topFacts = useRailModes(top);
  const anyRail = useRailModesAnyRail();
  const rail = opener ?? (topFacts ? top : (anyRail ?? top));
  const facts = useRailModes(rail);
  const shown = useModeShown();
  const door = (shown !== null && facts?.doors.find((d) => d.mode === shown)) || null;
  const present = door !== null;

  // A PINNED CARD FOLLOWS A HOP: the moment its door is gone it is held, not
  // closed, for `MODE_FOLLOW_MS` — set during the render (GitPeek). A door
  // still gone then (the page switched to that mode, the next page has none)
  // closes it. A preview is not held.
  const pinModel = useTaskHeadUnified();
  const card = useModeCard();
  const { pinned, held, up } = card;
  const [was, setWas] = useState(present);
  const [holding, setHolding] = useState(false);
  if (was !== present) {
    setWas(present);
    setHolding(!present && up && (pinModel ? held : pinned));
  }
  useEffect(() => {
    if (!holding) return;
    const t = window.setTimeout(() => setHolding(false), MODE_FOLLOW_MS);
    return () => window.clearTimeout(t);
  }, [holding]);
  useEffect(() => {
    if (up && !present && !holding) modeCardStore.close();
  }, [up, present, holding]);

  // THE FRAME (header): up, and a beat after.
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
  const src = door && facts ? facts.src(door.mode) : null;
  const [loaded, setLoaded] = useState<string | null>(null);

  // A PRESS INSIDE THE FRAME PINS A PREVIEW (header).
  useEffect(() => {
    if (!up) return;
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const f = frameRef.current;
        if (!f || document.activeElement !== f || modeCardStore.state().pinned) return;
        const intent = modeCardStore.intent();
        intent.pressStart();
        intent.pressEnd();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
    };
  }, [up]);
  // …AND A TEMP CARD GOES WHEN THE FOCUS MOVES INTO ANOTHER FRAME (header).
  const temp = up && pinned && !(pinModel && held);
  useEffect(() => {
    if (!temp || !pinModel) return;
    let inOurs = document.activeElement === frameRef.current;
    const t = window.setInterval(() => {
      const a = document.activeElement;
      if (a === frameRef.current) {
        inOurs = true;
        return;
      }
      if (inOurs && a instanceof HTMLIFrameElement && !document.getElementById(MODE_PEEK_ID)?.contains(a)) {
        modeCardStore.close();
        return;
      }
      if (!(a instanceof HTMLIFrameElement)) inOurs = false;
    }, 200);
    return () => window.clearInterval(t);
  }, [temp, pinModel]);

  const onOpen = facts?.onOpen;
  const mode = door?.mode ?? null;
  const noRoom = useCallback(() => {
    if (mode !== null) onOpen?.(mode);
  }, [onOpen, mode]);
  const title = door?.title ?? "Mode";
  return (
    <DoorCard
      store={modeCardStore}
      onNoRoomPress={noRoom}
      rail={rail}
      className="claude-rail-mode-peek"
      ariaLabel={title}
      enabled={anyRail !== null || holding}
      subOpen={false}
    >
      {(api) => {
        const take =
          onOpen && mode !== null
            ? () => {
                // The frame's editor may hold words newer than its last
                // autosave: ask it to write them before the page redraws the
                // file in this mode (the code template's own flush).
                const flush = frameFlush(frameRef.current);
                const go = () => {
                  api.close();
                  onOpen(mode);
                };
                if (!flush) {
                  go();
                  return;
                }
                let done = false;
                const once = () => {
                  if (done) return;
                  done = true;
                  go();
                };
                const t = window.setTimeout(once, 2000);
                void Promise.resolve()
                  .then(() => flush())
                  .catch(() => undefined)
                  .then(() => {
                    window.clearTimeout(t);
                    once();
                  });
              }
            : undefined;
        return (
          <>
            <div className="claude-rail-peek-head">
              <div className="claude-rail-peek-row">
                {/* With the pin: no ×, the pin in its seat (and the keyboard's landing). */}
                {!api.pin && (
                  <button
                    ref={api.closeRef}
                    type="button"
                    className="claude-rail-peek-btn claude-rail-peek-close"
                    aria-label={"Close " + title}
                    data-hint="Close"
                    onClick={api.close}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                )}
                {api.pin && <RailPinButton variant="legacy" btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />}
                <span className="claude-rail-tasks-title claude-rail-mode-title">
                  {door && (
                    <span className="claude-rail-mode-icon" aria-hidden="true">
                      {door.icon}
                    </span>
                  )}
                  {title}
                </span>
                <span className="claude-rail-peek-gap" aria-hidden="true" />
                {take && (
                  <button type="button" className="btn claude-rail-peek-open" onClick={take}>
                    Open
                  </button>
                )}
              </div>
            </div>
            <div className={"claude-rail-peek-body claude-rail-mode-body" + (src !== null && loaded === src ? " is-loaded" : "")}>
              {live && src !== null && (
                <ModeFrame key={src} src={src} title={title} frameRef={frameRef} onLoad={() => setLoaded(src)} />
              )}
            </div>
          </>
        );
      }}
    </DoorCard>
  );
}
