// THE MODE CARDS OFF THE RAIL'S MODE DOORS (owner, 2026-11-20; mode-peek-store
// has the story): a file page's other modes — every entry of its top-right
// mode menu but the active one — are doors on the rail above Git, and each
// has its own window A card here, exactly as the Git door has (GitPeek):
// mounted in the shell, window A's chassis (DoorCard), the rules between the
// cards in ClaudeRail. `ModePeek` draws one `ModeCard` per mode that has a
// store (mode-peek-store makes one the moment a rail draws that door).
//
// WHAT IT IS: the file drawn in the mode whose door opened it — the embed
// frame a panel pane shows (`RailModes.src`). The frame is mounted while the
// card is up and a beat after (its fade), never before the first opening.
//
// THE HEAD: the pin (or the × without the pin model), the mode's icon and
// name, and Open — the page's own switch to that mode (the menu's); Open
// closes the card, once the frame's editor has written its edits and only
// then (mode-peek-store `flushThenOpen`). No room beside the rail: the door's press is that switch
// (DoorCard `onNoRoomPress`).
//
// THE FRAME STANDS BEHIND A PARAM BOUNDARY, and the frame's rules (a press
// inside it pins a hover's preview; a temp card goes when the focus moves
// on into a frame that is not ours) are peek-frame.tsx's, shared with the
// left sidebar's framed cards.
import { useCallback, useEffect, useRef, useState } from "react";
import { DoorCard } from "./DoorCard";
import { RailPinButton } from "./RailPin";
import { useFavPeekState } from "./fav-peek-store";
import { FRAME_LINGER_MS, PeekFrame, usePeekFrameRules } from "./peek-frame";
import { flushThenOpen, modeCardStore, useModeCardRail, useModeCards, useRailModes, useRailWithMode } from "./mode-peek-store";

/** How long a PINNED card waits, its mode's door gone, for the next rail's
 *  (a hop: the next file's page mounts its rail after a stat). No door by
 *  then: it closes (GitPeek `GIT_FOLLOW_MS`). */
export const MODE_FOLLOW_MS = 600;

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

export function ModePeek() {
  const { modes } = useModeCards();
  return (
    <>
      {modes.map((mode) => (
        <ModeCard key={mode} mode={mode} />
      ))}
    </>
  );
}

function ModeCard({ mode }: { mode: string }) {
  const store = modeCardStore(mode);
  // BESIDE THE RAIL THAT OPENED IT, else the top rail where it has this
  // mode's door, else the latest rail that has (GitPeek's rule).
  const top = useFavPeekState().rail;
  const opener = useModeCardRail(mode);
  const topFacts = useRailModes(top);
  const anyRail = useRailWithMode(mode);
  const topHas = !!topFacts?.doors.some((d) => d.mode === mode);
  const rail = opener ?? (topHas ? top : (anyRail ?? top));
  const facts = useRailModes(rail);
  const door = facts?.doors.find((d) => d.mode === mode) ?? null;
  // REGISTERED WHILE ANY RAIL HAS THIS DOOR (GitPeek `maybe`).
  const maybe = anyRail !== null;

  // A PINNED CARD FOLLOWS A HOP: the moment no rail has its door it is held,
  // not closed, for `MODE_FOLLOW_MS` — set during the render (GitPeek). A
  // preview is not held.
  const card = store.useCard();
  const { pinned, held, up } = card;
  const [was, setWas] = useState(maybe);
  const [holding, setHolding] = useState(false);
  if (was !== maybe) {
    setWas(maybe);
    setHolding(!maybe && held);
  }
  useEffect(() => {
    if (!holding) return;
    const t = window.setTimeout(() => setHolding(false), MODE_FOLLOW_MS);
    return () => window.clearTimeout(t);
  }, [holding]);

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
  const src = door && facts ? facts.src(mode) : null;
  const [loaded, setLoaded] = useState<string | null>(null);
  // Open's guards (mode-peek-store `flushThenOpen`): the address as it stands
  // when a flush answers, and one Open at a time.
  const srcRef = useRef(src);
  srcRef.current = src;
  const opening = useRef(false);

  // THE FRAME'S RULES (peek-frame): a press inside it pins a preview; a temp
  // card goes when the focus moves on into another frame.
  const temp = up && pinned && !held;
  usePeekFrameRules({ store, frameRef, up, temp, pinOnBlur: true });

  const onOpen = facts?.onOpen;
  const noRoom = useCallback(() => onOpen?.(mode), [onOpen, mode]);
  const title = door?.title ?? "Mode";
  return (
    <DoorCard
      store={store}
      onNoRoomPress={noRoom}
      rail={rail}
      className="claude-rail-mode-peek"
      ariaLabel={title}
      enabled={maybe || holding}
      subOpen={false}
    >
      {(api) => {
        const take = onOpen
          ? () => {
              // One Open at a time: a second press while the first one's
              // flush is out is that same Open.
              if (opening.current) return;
              // The press is for THIS preview — this opening, this address.
              const seq = store.state().seq;
              const at = src;
              opening.current = true;
              void flushThenOpen({
                flush: frameFlush(frameRef.current),
                still: () => store.state().up && store.state().seq === seq && srcRef.current === at,
                go: () => {
                  api.close();
                  onOpen(mode);
                },
              }).finally(() => {
                opening.current = false;
              });
            }
          : undefined;
        return (
          <>
            <div className="claude-rail-peek-head">
              <div className="claude-rail-peek-row">
                {/* No ×: the pin in its seat (and the keyboard's landing). */}
                <RailPinButton variant="legacy" btnRef={api.closeRef} pinned={api.pin.pinned} onToggle={api.pin.onToggle} />
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
              {live && src !== null && <PeekFrame key={src} src={src} title={title} frameRef={frameRef} onLoad={() => setLoaded(src)} />}
            </div>
          </>
        );
      }}
    </DoorCard>
  );
}
