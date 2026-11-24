// A PINNED WINDOW ALWAYS HAS A BOX (rail-slots-lib `defaultPinBox`; free
// pins). The pin's press freezes the card where it stands, so a pin has a
// position from its first frame — except a mark the window's storage kept
// with none beside it: a card pinned where it docked before the switch came
// on, a pin made in another document of the window, or a press that found no
// card to freeze. While such a pin is up it is given its section's place
// beside the rail, before paint: the chassis draws a pinned card only where
// it floats. EACH PIN HAS ITS OWN BOX (owner, 2026-11-24: many pins), kept
// under the pin's id (rail-move-store).
import { useLayoutEffect, useSyncExternalStore } from "react";
import { viewportOf, type RailWindow } from "./rail-move-lib";
import { getUiLayout } from "@platform/lib/ui-layout";
import { commitWindowBox, sizeWindowMark, subscribeWindowPos, windowPos, windowSize, type MoveKey } from "./rail-move-store";
import { FAV_HEIGHT, FAV_WIDTH, railCardSize } from "./rail-peek-lib";
import { defaultPinBox } from "./rail-slots-lib";

/** The pinned windows' DOM id stems (the non-pins keep the ids they always
 *  had): one id per pinned card now (many pins) — the stem and the card's
 *  slot id, `pinDomId`. Every pinned chat card also says its pin's id in
 *  `data-pin-id`. */
export const PIN_A_ID = "claude-rail-pin-a";
export const PIN_B_ID = "claude-rail-pin-b";
export const PIN_BOT_ID = "claude-rail-pin-bot";
export function pinDomId(stem: string, slot: string): string {
  return `${stem}-${slot}`;
}

/** `up`: pin `pinId`'s card (of section `win`) wants to stand. True once it
 *  has a position — the card is mounted only then: the chassis places and
 *  SHOWS a card on its first frame, and one mounted with nowhere to float
 *  would be placed against a ring it does not have and never shown. */
export function usePinHasBox(win: RailWindow, pinId: MoveKey | null, up: boolean, rail: HTMLElement | null): boolean {
  const key = pinId ?? win;
  const placed = useSyncExternalStore(
    subscribeWindowPos,
    () => windowPos(key) !== null,
    () => true,
  );
  useLayoutEffect(() => {
    if (!up || placed || pinId === null) return;
    const r = rail?.getBoundingClientRect();
    commitWindowBox(key, defaultPinBox(win, r ? { left: r.left, top: r.top, width: r.width, height: r.height } : null, viewportOf(window)));
  }, [key, win, pinId, up, placed, rail]);
  // …AND A SIZE OF ITS OWN (CodeRabbit on #2349): a mark with a position and
  // no size — a window pinned and dragged before the switch came on — is
  // given the size it is drawn at now, the shared non-pin size its chassis
  // falls back to. Once: from then on a non-pin's resize is not the pin's.
  const sized = useSyncExternalStore(
    subscribeWindowPos,
    () => windowPos(key) === null || windowSize(key) !== null,
    () => true,
  );
  useLayoutEffect(() => {
    if (!up || !placed || sized || pinId === null) return;
    sizeWindowMark(key, sharedSize(win));
  }, [key, win, pinId, up, placed, sized]);
  return placed;
}

/** The size a pinned card with none of its own is drawn at: its window's
 *  shared non-pin size (ui-layout `canvasRailPeek`, `canvasRailFav`), else
 *  that card's default. */
function sharedSize(win: RailWindow): { width: number; height: number } {
  const layout = getUiLayout();
  if (win === "a") return railCardSize(layout.canvasRailPeek ?? null);
  const fav = layout.canvasRailFav;
  return { width: fav?.width ?? FAV_WIDTH, height: fav?.height ?? FAV_HEIGHT };
}
