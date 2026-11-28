// THE RAIL BESIDE A WORKSPACE'S RIGHT PANEL (SPEC §56 WS-21; shell/workspace).
// In a workspace host the window has ONE rail, and its clicks drive the right
// panel instead of window A: a list ring opens that task's chat there, the
// Tasks / Bots / Git / mode doors their view, the + the active tab's Claude
// home. Hover cards stay as they are, but window A's cards carry NO PIN (no
// pin button, no head-drag pin, stored A pins left unread); a press inside one
// still holds it until one press outside (temp). The foot — favourites and
// pinned bots, window B — keeps every rule it has.
//
// `RailPanel` is what the host hands ClaudeRail; `panelRailOn` tells window
// A's cards (drawn by the shell, not the rail) to drop their pin.
import { useSyncExternalStore } from "react";
import type { Task, TaskPulseTask } from "@platform/lib/api";

/** What the panel shows, as the rail needs to know it: the item that shows
 *  it wears the open look, rests open no hover card, and a click on it does
 *  nothing. */
export type RailPanelShows =
  | { kind: "none" }
  | { kind: "tasks" }
  | { kind: "bots" }
  | { kind: "git" }
  | { kind: "mode"; mode: string }
  | { kind: "home" }
  | { kind: "chat"; sessionId: string | null; queued: string | null };

export interface RailPanel {
  shows: RailPanelShows;
  /** A list ring's click: that task's chat. False when the task has no chat
   *  to open there (the rail then does what it always did). */
  openChat(t: TaskPulseTask, row: Task | null): boolean;
  openTasks(): void;
  openBots(): void;
  openGit(): void;
  openMode(mode: string): void;
  /** The +: the active tab's Claude home. */
  openHome(): void;
}

/** Does the panel show this task's chat? */
export function panelShowsTask(shows: RailPanelShows, t: Pick<TaskPulseTask, "key" | "session_id">): boolean {
  if (shows.kind !== "chat") return false;
  if (shows.sessionId) return t.session_id === shows.sessionId;
  return !!shows.queued && t.key === "pending:" + shows.queued;
}

let on = 0;
const listeners = new Set<() => void>();

/** The host's rail says it stands (counted, like the rail's own `railsUp`). */
export function registerPanelRail(): () => void {
  on += 1;
  listeners.forEach((fn) => fn());
  return () => {
    on = Math.max(0, on - 1);
    listeners.forEach((fn) => fn());
  };
}

export function panelRailOn(): boolean {
  return on > 0;
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Window A's cards: no pin while the panel's rail stands. */
export function usePanelRailOn(): boolean {
  return useSyncExternalStore(subscribe, panelRailOn, panelRailOn);
}
