// THE BOTS CARD OFF THE RAIL'S BOTS DOOR (owner, 2026-11-09: "the bot in the
// top right corner of the rail has no window A popover — let's have one which
// first shows the bots, and on a click shows that bot's chat; window A
// behaviour like the Tasks card's"). The card is the shell's (BotsPeek,
// mounted once beside TasksPeek); the door is every rail's own (ClaudeRail's
// Bots link). This store is where the two meet — door-card-store's, the same
// one the Tasks card runs on (tasks-peek-store), so the two doors behave as
// one: window A's timings, a press pins, the door's toggle or the × closes,
// a plain link where no card can stand (`botsDoorAction`).
import type { Timers } from "./rail-peek-lib";
import { createDoorCardStore, doorAction, type DoorAct, type DoorCardState } from "./door-card-store";

/** The intent machine's one key: there is one Bots card. */
export const BOTS_CARD_KEY = "bots";
/** The card's DOM id — what the door's `aria-controls` names. */
export const BOTS_PEEK_ID = "claude-rail-bots-peek";

export type BotsCardState = DoorCardState;

/** The store itself, for the chassis the two cards share (DoorCard). */
export const botsCardStore = createDoorCardStore(BOTS_CARD_KEY, BOTS_PEEK_ID);

export const botsCardIntent = () => botsCardStore.intent();
export const closeBotsCard = (returningFocus = false): void => botsCardStore.close(returningFocus);
export const botsCardState = (): BotsCardState => botsCardStore.state();
export const useBotsCard = (): BotsCardState => botsCardStore.useCard();
export const registerBotsCard = (): (() => void) => botsCardStore.registerCard();
export const setBotsCardRoom = (ok: boolean): void => botsCardStore.setRoom(ok);
export const registerBotsDoor = (rail: HTMLElement, door: HTMLElement): (() => void) => botsCardStore.registerDoor(rail, door);
export const railBotsDoor = (rail: HTMLElement | null): HTMLElement | null => botsCardStore.doorOf(rail);
export const registerBotsCardFocus = (fn: () => boolean): (() => void) => botsCardStore.registerFocus(fn);
export const focusBotsCard = (): boolean => botsCardStore.focusCard();
export const resetBotsCardForTests = (timers?: Timers): void => botsCardStore.resetForTests(timers);

export type BotsDoorAct = DoorAct;
export const botsDoorAction = doorAction;
