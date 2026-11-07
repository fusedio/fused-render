// THE TASKS CARD OFF THE RAIL'S TASKS DOOR (owner, 2026-11-07: "hovering the
// rail's Tasks icon previews a card; a click pins it; a click on the icon while
// pinned closes it"). The card itself is the shell's (TasksPeek, mounted once
// beside FavouritePeek, so a page hop never remounts it); the door is every
// rail's own (ClaudeRail's Tasks link). This store is where the two meet:
//   - ONE INTENT MACHINE for the shell (`tasksCardIntent`, rail-peek-lib's
//     PeekIntent — window A's timings: 150ms open, 250ms grace, a press pins,
//     a pinned card closes only by its × or the door's toggle);
//   - what the rails read back (`useTasksCard`: up, pinned, and a `seq` bumped
//     on every opening — ClaudeRail's one-card rule keys on it);
//   - each rail's Tasks door, by rail (`registerTasksDoor`), which the card
//     stands level with;
//   - whether a card is there at all (`registerTasksCard`, `setTasksCardRoom`):
//     a rail with no card behind its door (an embed, a window too narrow for
//     one) keeps the door a plain link to /tasks (`tasksDoorAction`).
import { useSyncExternalStore } from "react";
import { PeekIntent, type Timers } from "./rail-peek-lib";

/** The intent machine's one key: there is one Tasks card. */
export const TASKS_CARD_KEY = "tasks";
/** The card's DOM id — what the door's `aria-controls` names. */
export const TASKS_PEEK_ID = "claude-rail-tasks-peek";

export interface TasksCardState {
  /** Up: a hover's preview or pinned. */
  up: boolean;
  /** Pinned by a press (the door's, or one inside the card). */
  pinned: boolean;
  /** Bumped on every opening (closed → up), never on a pin. */
  seq: number;
  /** A card is mounted and has room beside the rail: the door opens it. */
  ready: boolean;
}

let state: TasksCardState = { up: false, pinned: false, seq: 0, ready: false };
const subs = new Set<() => void>();
let cards = 0;
let roomy = true;

function write(next: TasksCardState): void {
  if (next.up === state.up && next.pinned === state.pinned && next.seq === state.seq && next.ready === state.ready) return;
  state = next;
  for (const fn of [...subs]) fn();
}

function settleReady(): void {
  write({ ...state, ready: cards > 0 && roomy });
}

let intent: PeekIntent | null = null;
let testTimers: Timers | undefined;

/** The shell's one intent machine for the card (header). The door reports its
 *  pointer and presses to it; the card its own pointer, focus and presses. */
export function tasksCardIntent(): PeekIntent {
  if (!intent) {
    intent = new PeekIntent(
      {
        show: (key) =>
          write({
            ...state,
            up: key !== null,
            seq: key !== null ? state.seq + 1 : state.seq,
            pinned: key === null ? false : state.pinned,
          }),
        pin: (on) => write({ ...state, pinned: on }),
        warm: () => {},
      },
      testTimers,
    );
  }
  return intent;
}

/** Put the card down now: its ×, the door's toggle, another card opening
 *  (ClaudeRail's one-card rule), Open. `returningFocus`: the caller hands the
 *  keyboard back to the door, whose focus must not reopen it. */
export function closeTasksCard(returningFocus = false): void {
  intent?.dismiss(returningFocus);
}

export function tasksCardState(): TasksCardState {
  return state;
}

function subscribe(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

/** The card as it stands, re-rendering on every change. */
export function useTasksCard(): TasksCardState {
  return useSyncExternalStore(subscribe, tasksCardState, tasksCardState);
}

/** A card mounted (TasksPeek, while the rail flag is on): counted until the
 *  answer runs, once. */
export function registerTasksCard(): () => void {
  cards += 1;
  settleReady();
  let done = false;
  return () => {
    if (done) return;
    done = true;
    cards -= 1;
    settleReady();
  };
}

/** The card found room beside the rail (true) or none (false, until the
 *  window, the rail or its row changes shape — TasksPeek). */
export function setTasksCardRoom(ok: boolean): void {
  roomy = ok;
  settleReady();
}

/** WHAT A PRESS ON THE DOOR DOES (header; owner, 2026-11-07):
 *    - a MODIFIED press (⌘, Ctrl, Shift, Alt, a middle button) is the link's
 *      own — a new tab or window on /tasks;
 *    - with no card to show (none mounted, or no room), the door is the link
 *      it always was: /tasks, in place;
 *    - else it PINS the card, or — pinned already — CLOSES it (a toggle). */
export type TasksDoorAct = "native" | "navigate" | "pin" | "close";

export function tasksDoorAction(o: { modified: boolean; ready: boolean; pinned: boolean }): TasksDoorAct {
  if (o.modified) return "native";
  if (!o.ready) return "navigate";
  return o.pinned ? "close" : "pin";
}

/** Each rail's Tasks door (header), by its rail. */
const doors = new WeakMap<HTMLElement, HTMLElement>();

/** A rail's Tasks door drew: the card stands level with it. The answer takes
 *  it off again (only if it is still that rail's door). */
export function registerTasksDoor(rail: HTMLElement, door: HTMLElement): () => void {
  doors.set(rail, door);
  return () => {
    if (doors.get(rail) === door) doors.delete(rail);
  };
}

/** The Tasks door of `rail`, or null (no rail, or one with no nav doors). */
export function railTasksDoor(rail: HTMLElement | null): HTMLElement | null {
  return (rail && doors.get(rail)) ?? null;
}

/** THE KEYBOARD INTO THE CARD (CodeRabbit, 2026-11-08): an Enter on the door
 *  pins the card, and the keyboard should land in it — on its × (or its
 *  chat's Back) — as an Enter on a ring lands in the list card; left on the
 *  door, the next Tab reached the + and the reader had to walk the rail to
 *  get in. The card registers how (TasksPeek); the door asks, by frame, until
 *  the card is drawn (ClaudeRail `cardFocusSoon`). */
let focuser: (() => boolean) | null = null;

export function registerTasksCardFocus(fn: () => boolean): () => void {
  focuser = fn;
  return () => {
    if (focuser === fn) focuser = null;
  };
}

/** Put the keyboard in the card; true once it is there. */
export function focusTasksCard(): boolean {
  return focuser?.() ?? false;
}

export function resetTasksCardForTests(timers?: Timers): void {
  focuser = null;
  intent?.dispose();
  intent = null;
  testTimers = timers;
  state = { up: false, pinned: false, seq: 0, ready: false };
  cards = 0;
  roomy = true;
  subs.clear();
}
