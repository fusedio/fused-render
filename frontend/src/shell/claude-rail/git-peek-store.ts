// THE GIT CARD OFF THE RAIL'S GIT DOOR (owner, 2026-11-10: "move Git from the
// status bar to a door on the rail — window A, exactly the Tasks/Bots door
// rules"). The card is the shell's (GitPeek, mounted once beside BotsPeek);
// the door is every rail's own (ClaudeRail's Git door, under the rings).
// door-card-store's store on its own key, the one the Tasks and Bots cards
// run on: window A's timings, a press pins, the door's toggle or the ×
// closes — and with no card to stand (`ready` false) the door's press opens
// the page's Git panel instead (it has no page of its own to link to).
import { useSyncExternalStore } from "react";
import type { Timers } from "./rail-peek-lib";
import { createDoorCardStore, type DoorCardState } from "./door-card-store";

/** The intent machine's one key: there is one Git card. */
export const GIT_CARD_KEY = "git";
/** The card's DOM id — what the door's `aria-controls` names. */
export const GIT_PEEK_ID = "claude-rail-git-peek";

/** The store itself, for the chassis the door cards share (DoorCard). */
export const gitCardStore = createDoorCardStore(GIT_CARD_KEY, GIT_PEEK_ID);

export const gitCardIntent = () => gitCardStore.intent();
export const closeGitCard = (returningFocus = false): void => gitCardStore.close(returningFocus);
export const gitCardState = (): DoorCardState => gitCardStore.state();
export const useGitCard = (): DoorCardState => gitCardStore.useCard();
export const registerGitCard = (): (() => void) => gitCardStore.registerCard();
export const setGitCardRoom = (ok: boolean): void => gitCardStore.setRoom(ok);
export const registerGitDoor = (rail: HTMLElement, door: HTMLElement): (() => void) => gitCardStore.registerDoor(rail, door);
export const railGitDoorEl = (rail: HTMLElement | null): HTMLElement | null => gitCardStore.doorOf(rail);
export const registerGitCardFocus = (fn: () => boolean): (() => void) => gitCardStore.registerFocus(fn);
export const focusGitCard = (): boolean => gitCardStore.focusCard();
export const resetGitCardForTests = (timers?: Timers): void => {
  opener = null;
  gitCardStore.resetForTests(timers);
  for (const fn of [...openerSubs]) fn();
};

// THE RAIL THAT OPENED IT (owner, 2026-11-10: an explorer split is two rails
// on two repositories — the card stands beside, and shows the repository
// of, the rail whose door was hovered or pressed, not the top rail). Set by
// the door on its pointer's arrival and its press; GitPeek hands it to the
// chassis (DoorCard `rail`). That rail unmounting forgets it: a preview goes
// with it, a pinned card follows the reader to the top rail (the hop rule).
let opener: HTMLElement | null = null;
const openerSubs = new Set<() => void>();
function setOpener(next: HTMLElement | null): void {
  if (opener === next) return;
  opener = next;
  for (const fn of [...openerSubs]) fn();
}
/** The door of `rail` is opening the card (a hover's arrival, a press). */
export const setGitCardRail = (rail: HTMLElement | null): void => setOpener(rail);
/** `rail` unmounted: if it opened the card, a preview closes and a pinned
 *  card falls back to the top rail. */
export function forgetGitCardRail(rail: HTMLElement): void {
  if (opener !== rail) return;
  if (!gitCardStore.state().pinned) gitCardStore.close();
  setOpener(null);
}
export const gitCardRail = (): HTMLElement | null => opener;
export function useGitCardRail(): HTMLElement | null {
  return useSyncExternalStore(
    (fn) => {
      openerSubs.add(fn);
      return () => {
        openerSubs.delete(fn);
      };
    },
    gitCardRail,
    gitCardRail,
  );
}

/** WHAT A PRESS ON THE GIT DOOR DOES: a button, not a link, so a modified
 *  press is a plain one (no tab to open). No card to stand: the page's Git
 *  panel (`open`); else a toggle. */
export function gitDoorAction(o: { ready: boolean; pinned: boolean }): "open" | "pin" | "close" {
  if (!o.ready) return "open";
  return o.pinned ? "close" : "pin";
}
