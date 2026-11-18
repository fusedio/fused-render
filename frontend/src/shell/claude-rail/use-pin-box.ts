// A PINNED WINDOW ALWAYS HAS A BOX (rail-slots-lib `defaultPinBox`; free
// pins). The pin's press freezes the card where it stands, so a pin has a
// position from its first frame — except a mark the window's storage kept
// with none beside it: a card pinned where it docked before the switch came
// on, or a press that found no card to freeze. While such a pin is up it is
// given its section's place beside the rail, before paint: the chassis draws
// a pinned card only where its window floats.
import { useLayoutEffect, useSyncExternalStore } from "react";
import { viewportOf, type RailWindow } from "./rail-move-lib";
import { commitWindowBox, subscribeWindowPos, windowPos } from "./rail-move-store";
import { defaultPinBox } from "./rail-slots-lib";

/** The pinned windows' DOM ids (the non-pins keep the ids they always had). */
export const PIN_A_ID = "claude-rail-pin-a";
export const PIN_B_ID = "claude-rail-pin-b";
export const PIN_BOT_ID = "claude-rail-pin-bot";

/** `up`: window `win`'s pinned card wants to stand. True once it has a
 *  position — the card is mounted only then: the chassis places and SHOWS a
 *  card on its first frame, and one mounted with nowhere to float would be
 *  placed against a ring it does not have and never shown. */
export function usePinHasBox(win: RailWindow, up: boolean, rail: HTMLElement | null): boolean {
  const placed = useSyncExternalStore(
    subscribeWindowPos,
    () => windowPos(win) !== null,
    () => true,
  );
  useLayoutEffect(() => {
    if (!up || placed) return;
    const r = rail?.getBoundingClientRect();
    commitWindowBox(win, defaultPinBox(win, r ? { left: r.left, top: r.top, width: r.width, height: r.height } : null, viewportOf(window)));
  }, [win, up, placed, rail]);
  return placed;
}
