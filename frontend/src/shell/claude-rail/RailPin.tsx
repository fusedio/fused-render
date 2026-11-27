// THE PIN ICON AND THE TEMP CARD'S OUTSIDE PRESS (rail-pin-lib.ts header;
// owner, 2026-11-16).
//
//   - `RailPinButton`: ONE component for every rail card's head — the shared
//     head's first seat (TaskHead `pinNode`; with the pin no card has a ×), and the legacy
//     Bots / Git / bot heads' row after their ×. Hollow while the card is a
//     hover or temp, filled in the head's muted ink once pinned; `aria-pressed` says
//     which. Its press is a press inside the card first (the card's capture
//     handler makes a hover temp), then the toggle.
//   - `useTempOutside`: ONE watcher for a temp card's outside press, the
//     classifier's (`pressVerdict`) thin DOM half. Document capture, so it
//     hears a press before whatever it landed on reacts; the ruling waits a
//     tick for React's capture phase to say whether the press came from the
//     card's own tree (a menu or picker portaled out of it).
import { useCallback, useEffect, useRef, type Ref } from "react";
import { pinWords, pressVerdict, viewPress, type PressTarget, type PressWhere } from "./rail-pin-lib";
import { useRailExtras } from "./rail-extras-flag";
import { pinHint } from "./rail-slots-lib";

/** The pin glyph (owner, 2026-11-02: "BsFillPinAngleFill use this"; and
 *  2026-11-16, of the pin the card showed with favourites off: "copy that
 *  icon for pin") — Bootstrap Icons' `pin-angle-fill` (MIT), inlined:
 *  react-icons is not a dependency and one path does not earn one. Angled,
 *  16-unit box. `filled`: the glyph as drawn (a pinned window, and the
 *  favourites-off head's seat); else its outline alone (hover / temp).
 *  MIRRORED on the x axis, everywhere it is drawn (owner, 2026-11-18, first
 *  of the rail's mark — "invert the pin on x axis so it faces right side" —
 *  then of the head's own button: "rotate pin icon 180deg, x axis"): the
 *  point leans down to the right. */
export function PinAngle({ size = 14, filled = false }: { size?: number; filled?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ transform: "scaleX(-1)" }}
      {...(filled
        ? { fill: "currentColor" }
        : { fill: "none", stroke: "currentColor", strokeWidth: 1, strokeLinejoin: "round" as const, overflow: "visible" })}
    >
      <path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a6 6 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707s.512-1.22.707-1.414l3.182-3.182-2.828-2.829a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a6 6 0 0 1 1.013.16l3.134-3.133a3 3 0 0 1-.04-.461c0-.43.108-1.022.589-1.503a.5.5 0 0 1 .353-.146" />
    </svg>
  );
}

/** The × a pinned window's seat draws with free pins (rail-slots-lib "A
 *  PINNED WINDOW CLOSES BY ITS ×"): the head buttons' 14px, a 1.5 stroke. */
function CloseX({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round">
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

export function RailPinButton({
  pinned,
  onToggle,
  variant = "head",
  btnRef,
}: {
  pinned: boolean;
  onToggle: () => void;
  /** `head`: the shared head's icon button (`.task-head-btn`);
   *  `legacy`: the rail card's own row (`.claude-rail-peek-btn`). */
  variant?: "head" | "legacy";
  /** The card's keyboard landing: with the pin no card has a ×, and the pin
   *  is the head's first button (DoorCard `closeRef`). */
  btnRef?: Ref<HTMLButtonElement>;
}) {
  // FREE PINS (rail-slots-lib "A PINNED WINDOW CLOSES BY ITS ×"): "Pin" on a
  // non-pin card, "Close" on a pin — with no cap a pin's press replaces
  // nothing, so the caption names nothing it would close and no pinned
  // window wears a red border for it (until 2026-11-24 they did).
  const freePins = useRailExtras();
  const words = freePins ? pinHint({ pinned }) : pinWords(pinned);
  const closes = freePins && pinned;
  const setRefs = useCallback(
    (el: HTMLButtonElement | null) => {
      if (typeof btnRef === "function") btnRef(el);
      else if (btnRef) (btnRef as { current: HTMLButtonElement | null }).current = el;
    },
    [btnRef],
  );
  return (
    <button
      ref={setRefs}
      type="button"
      className={
        (variant === "head" ? "task-head-btn" : "claude-rail-peek-btn") +
        " rail-pin-btn" +
        (pinned ? " is-pinned" : "") +
        (closes ? " is-close" : "")
      }
      // A PINNED WINDOW'S SEAT IS ITS × (free pins; owner, 2026-11-19: "unpin
      // should close it as well, so instead of unpin filled pin icon it should
      // be a 'x' button"): the same seat, the same press — its host closes
      // the window. A button that closes is not a toggle: no `aria-pressed`.
      {...(closes ? {} : { "aria-pressed": pinned })}
      aria-label={closes ? "Close" : "Pin"}
      // The app's instant caption on either head (platform/lib/hints; owner,
      // 2026-11-18: the rail's tooltips "consistent and shown instantly").
      data-hint={words}
      onClick={onToggle}
    >
      {closes ? <CloseX size={14} /> : <PinAngle size={14} filled={pinned} />}
    </button>
  );
}

/** A flag a card's React capture handler raises for a press that started in
 *  its tree (portals included); the watcher lowers it at each press. */
export interface PressFlag {
  inside: boolean;
}

/** WHILE `active` (the card is temp): a press the classifier calls outside
 *  closes the card (`close`). Selectors and the close are read at the press,
 *  so a caller may hand fresh ones every render. */
export function useTempOutside(o: {
  active: boolean;
  flag: PressFlag;
  cardSelectors: readonly string[];
  ownSelectors: readonly string[];
  close: () => void;
  /** Another view's reading of a press (the left sidebar's cards,
   *  shell/left-rail): the classifier `pressVerdict` unless given. */
  classify?: typeof pressVerdict;
  /** The view's own "a press started in one of its windows" flag
   *  (rail-pin-lib `viewPress`, the Claude rail's, unless given). */
  viewFlag?: PressFlag;
}): void {
  const now = useRef(o);
  now.current = o;
  useEffect(() => {
    if (!o.active) return;
    let tick = 0;
    const onDown = (e: Event) => {
      const t = e.target as (Node & Partial<PressTarget>) | null;
      const el: PressTarget | null =
        t && typeof t.closest === "function" ? (t as PressTarget) : ((t?.parentElement as PressTarget | null | undefined) ?? null);
      const at = now.current;
      // The DOM's half now (the target may leave the page before the tick);
      // React's half — a press from the card's own tree — at the tick.
      const classify = at.classify ?? pressVerdict;
      const view = at.viewFlag ?? viewPress;
      const dom: PressWhere = classify(el, {
        pressedInside: false,
        cardSelectors: at.cardSelectors,
        ownSelectors: at.ownSelectors,
      });
      at.flag.inside = false;
      // Lowered here, raised by whichever rail window the press started in
      // (rail-pin-lib `viewPress`): another window's portaled menu is the
      // view too, and closes nothing.
      view.inside = false;
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const later = now.current;
        if (dom === "outside" && !later.flag.inside && !view.inside) later.close();
      }, 0);
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => {
      window.clearTimeout(tick);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [o.active]);
}
