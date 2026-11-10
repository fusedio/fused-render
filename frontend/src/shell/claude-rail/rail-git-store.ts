// THE PAGE'S GIT, FOR THE RAIL'S GIT DOOR CARD (owner, 2026-11-07, behind the
// rail flag; the door, 2026-11-10: "move Git to a door on the rail").
//
// The rail READS the repository (rail-git.ts, for the page's path — it is
// the one that knows the page's path and its Git panel's door) and draws the
// door; the card is the shell's (GitPeek, mounted once). A mounted rail
// publishes what it read here, and the card draws it. A module store, the
// shape of platform/lib/terminalDockStore.
//
// PER RAIL, KEYED BY THE RAIL'S ELEMENT (owner, 2026-11-10: an explorer split
// is two rails on two repositories): the card shows the facts of the rail it
// stands beside (fav-peek-store's `rail`, DoorCard's), never another rail's.
// A rail that unmounts, or has no repository, drops its entry.
//
// READING IS NOT "NONE" (`"reading"`): a page hop mounts the next rail before
// its first read answers, and a pinned card follows the reader to it
// (DoorCard) — it must not close on a rail that simply has not answered yet.
// The card goes once the answer is "no repository".
import { useSyncExternalStore } from "react";
import type { RailGitRepo } from "@platform/lib/api";

export interface RailGitDoor {
  repo: RailGitRepo;
  checking: boolean;
  /** The last check reached the remote (null until one has run). */
  checked: boolean | null;
  check: () => void;
  /** The page's Git panel, where it has one (explorer pages, the app page). */
  onOpenGit?: () => void;
}

export type RailGitEntry = RailGitDoor | "reading";

const entries = new Map<HTMLElement, RailGitEntry>();
const listeners = new Set<() => void>();

function emit() {
  for (const l of [...listeners]) l();
}

/** What `rail` read: its repository, `"reading"` (the first read still out),
 *  or null (no repository here, no path, gone). */
export function publishRailGit(rail: HTMLElement, value: RailGitEntry | null): void {
  const was = entries.get(rail) ?? null;
  if (was === value) return;
  if (value) entries.set(rail, value);
  else entries.delete(rail);
  emit();
}

export function railGitOf(rail: HTMLElement | null): RailGitDoor | null {
  const v = (rail && entries.get(rail)) ?? null;
  return v === "reading" ? null : v;
}

/** `rail` has a repository, or may: its first read is still out. */
export function railGitMaybe(rail: HTMLElement | null): boolean {
  return !!rail && entries.has(rail);
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** `rail`'s git, live: null while it has no repository (or no rail). */
export function useRailGitDoor(rail: HTMLElement | null): RailGitDoor | null {
  const get = () => railGitOf(rail);
  return useSyncExternalStore(subscribe, get, get);
}

/** `railGitMaybe`, live. */
export function useRailGitMaybe(rail: HTMLElement | null): boolean {
  const get = () => railGitMaybe(rail);
  return useSyncExternalStore(subscribe, get, get);
}

/** How many rails hold a repository now (tests). */
export function railGitCount(): number {
  return entries.size;
}

/** Test-only. */
export function resetRailGitStoreForTests(): void {
  entries.clear();
  emit();
}
