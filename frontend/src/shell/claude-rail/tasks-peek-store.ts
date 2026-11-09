// THE TASKS CARD OFF THE RAIL'S TASKS DOOR (owner, 2026-11-07: "hovering the
// rail's Tasks icon previews a card; a click pins it; a click on the icon while
// pinned closes it"). The card itself is the shell's (TasksPeek, mounted once
// beside FavouritePeek, so a page hop never remounts it); the door is every
// rail's own (ClaudeRail's Tasks link). This store is where the two meet —
// one of door-card-store's (the Bots card has the other, bots-peek-store):
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
import type { Timers } from "./rail-peek-lib";
import { createDoorCardStore, doorAction, type DoorAct, type DoorCardState } from "./door-card-store";

/** The intent machine's one key: there is one Tasks card. */
export const TASKS_CARD_KEY = "tasks";
/** The card's DOM id — what the door's `aria-controls` names. */
export const TASKS_PEEK_ID = "claude-rail-tasks-peek";

export type TasksCardState = DoorCardState;

/** The store itself, for the chassis the two cards share (DoorCard). */
export const tasksCardStore = createDoorCardStore(TASKS_CARD_KEY, TASKS_PEEK_ID);

export const tasksCardIntent = () => tasksCardStore.intent();
export const closeTasksCard = (returningFocus = false): void => tasksCardStore.close(returningFocus);
export const tasksCardState = (): TasksCardState => tasksCardStore.state();
export const useTasksCard = (): TasksCardState => tasksCardStore.useCard();
export const registerTasksCard = (): (() => void) => tasksCardStore.registerCard();
export const setTasksCardRoom = (ok: boolean): void => tasksCardStore.setRoom(ok);
export const registerTasksDoor = (rail: HTMLElement, door: HTMLElement): (() => void) => tasksCardStore.registerDoor(rail, door);
export const railTasksDoor = (rail: HTMLElement | null): HTMLElement | null => tasksCardStore.doorOf(rail);
export const registerTasksCardFocus = (fn: () => boolean): (() => void) => tasksCardStore.registerFocus(fn);
export const focusTasksCard = (): boolean => tasksCardStore.focusCard();
export const resetTasksCardForTests = (timers?: Timers): void => tasksCardStore.resetForTests(timers);

/** WHAT A PRESS ON THE DOOR DOES (door-card-store `doorAction`). */
export type TasksDoorAct = DoorAct;
export const tasksDoorAction = doorAction;
