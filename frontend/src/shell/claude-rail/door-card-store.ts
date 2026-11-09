// A WINDOW A CARD OFF ONE OF THE RAIL'S NAV DOORS (owner, 2026-11-07 / 2026-11-09):
// the Tasks card (tasks-peek-store) and the Bots card (bots-peek-store) are
// one kind of thing — a page's content in window A's chassis, previewed by a
// hover on its door, pinned by a press, closed by the door's toggle or its ×.
// This factory is the store each of them is: the Tasks store was written
// first (its header has the long story); the Bots card wanted the same one,
// so the two now share it rather than copy it.
//
// WHAT A STORE HOLDS (one per door):
//   - ONE INTENT MACHINE for the shell (`intent`, rail-peek-lib's PeekIntent —
//     window A's timings: 150ms open, 250ms grace, a press pins, a pinned
//     card closes only by its × or the door's toggle);
//   - what the rails read back (`useCard`: up, pinned, and a `seq` bumped on
//     every opening — ClaudeRail's one-card rule keys on it);
//   - each rail's door, by rail (`registerDoor`), which the card stands level
//     with;
//   - whether a card is there at all (`registerCard`, `setRoom`): a rail with
//     no card behind its door (an embed, a window too narrow for one) keeps
//     the door a plain link to its page (`doorAction`);
//   - the keyboard into the card (`registerFocus` / `focusCard`): an Enter on
//     the door pins the card, and the keyboard should land in it.
import { useSyncExternalStore } from "react";
import { PeekIntent, type Timers } from "./rail-peek-lib";

export interface DoorCardState {
  /** Up: a hover's preview or pinned. */
  up: boolean;
  /** Pinned by a press (the door's, or one inside the card). */
  pinned: boolean;
  /** Bumped on every opening (closed → up), never on a pin. */
  seq: number;
  /** A card is mounted and has room beside the rail: the door opens it. */
  ready: boolean;
}

/** WHAT A PRESS ON THE DOOR DOES (owner, 2026-11-07):
 *    - a MODIFIED press (⌘, Ctrl, Shift, Alt, a middle button) is the link's
 *      own — a new tab or window on the page;
 *    - with no card to show (none mounted, or no room), the door is the link
 *      it always was: the page, in place;
 *    - else it PINS the card, or — pinned already — CLOSES it (a toggle). */
export type DoorAct = "native" | "navigate" | "pin" | "close";

export function doorAction(o: { modified: boolean; ready: boolean; pinned: boolean }): DoorAct {
  if (o.modified) return "native";
  if (!o.ready) return "navigate";
  return o.pinned ? "close" : "pin";
}

export interface DoorCardStore {
  /** The intent machine's one key: there is one card per door. */
  readonly key: string;
  /** The card's DOM id — what the door's `aria-controls` names. */
  readonly id: string;
  /** The shell's one intent machine for the card. The door reports its
   *  pointer and presses to it; the card its own pointer, focus and presses. */
  intent(): PeekIntent;
  /** Put the card down now: its ×, the door's toggle, another card opening
   *  (ClaudeRail's one-card rule), Open. `returningFocus`: the caller hands
   *  the keyboard back to the door, whose focus must not reopen it. */
  close(returningFocus?: boolean): void;
  state(): DoorCardState;
  /** The card as it stands, re-rendering on every change. */
  useCard(): DoorCardState;
  /** A card mounted (while the rail flag is on): counted until the answer
   *  runs, once. */
  registerCard(): () => void;
  /** The card found room beside the rail (true) or none (false, until the
   *  window, the rail or its row changes shape). */
  setRoom(ok: boolean): void;
  /** A rail's door drew: the card stands level with it. The answer takes it
   *  off again (only if it is still that rail's door). */
  registerDoor(rail: HTMLElement, door: HTMLElement): () => void;
  /** The door of `rail`, or null (no rail, or one with no nav doors). */
  doorOf(rail: HTMLElement | null): HTMLElement | null;
  /** The card registers how the keyboard lands in it (on its ×, or its
   *  chat's Back). */
  registerFocus(fn: () => boolean): () => void;
  /** Put the keyboard in the card; true once it is there. */
  focusCard(): boolean;
  resetForTests(timers?: Timers): void;
}

export function createDoorCardStore(key: string, id: string): DoorCardStore {
  let state: DoorCardState = { up: false, pinned: false, seq: 0, ready: false };
  const subs = new Set<() => void>();
  let cards = 0;
  let roomy = true;
  let intent: PeekIntent | null = null;
  let testTimers: Timers | undefined;
  let focuser: (() => boolean) | null = null;
  const doors = new WeakMap<HTMLElement, HTMLElement>();

  function write(next: DoorCardState): void {
    if (next.up === state.up && next.pinned === state.pinned && next.seq === state.seq && next.ready === state.ready) return;
    state = next;
    for (const fn of [...subs]) fn();
  }
  function settleReady(): void {
    write({ ...state, ready: cards > 0 && roomy });
  }
  function subscribe(fn: () => void): () => void {
    subs.add(fn);
    return () => {
      subs.delete(fn);
    };
  }
  const getState = () => state;

  return {
    key,
    id,
    intent() {
      if (!intent) {
        intent = new PeekIntent(
          {
            show: (k) =>
              write({
                ...state,
                up: k !== null,
                seq: k !== null ? state.seq + 1 : state.seq,
                pinned: k === null ? false : state.pinned,
              }),
            pin: (on) => write({ ...state, pinned: on }),
            warm: () => {},
          },
          testTimers,
        );
      }
      return intent;
    },
    close(returningFocus = false) {
      intent?.dismiss(returningFocus);
    },
    state: getState,
    useCard() {
      return useSyncExternalStore(subscribe, getState, getState);
    },
    registerCard() {
      cards += 1;
      settleReady();
      let done = false;
      return () => {
        if (done) return;
        done = true;
        cards -= 1;
        settleReady();
      };
    },
    setRoom(ok) {
      roomy = ok;
      settleReady();
    },
    registerDoor(rail, door) {
      doors.set(rail, door);
      return () => {
        if (doors.get(rail) === door) doors.delete(rail);
      };
    },
    doorOf(rail) {
      return (rail && doors.get(rail)) ?? null;
    },
    registerFocus(fn) {
      focuser = fn;
      return () => {
        if (focuser === fn) focuser = null;
      };
    },
    focusCard() {
      return focuser?.() ?? false;
    },
    resetForTests(timers) {
      focuser = null;
      intent?.dispose();
      intent = null;
      testTimers = timers;
      state = { up: false, pinned: false, seq: 0, ready: false };
      cards = 0;
      roomy = true;
      subs.clear();
    },
  };
}
