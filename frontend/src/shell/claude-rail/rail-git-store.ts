// THE PAGE'S GIT, FOR THE STATUS BAR (owner, 2026-11-07, behind the rail flag:
// "remove git icons from the sidebar, and instead beside Terminal say Git and
// when clicked opens a popover with the git UI").
//
// The rail still READS the repository (rail-git.ts, for the page's path — it
// is the one that knows the page's path and its Git panel's door); it no
// longer DRAWS it. A mounted rail publishes what it read here, and the status
// bar's Git chip (shell/GitDock.tsx) draws it. A module store, the shape of
// platform/lib/terminalDockStore: one status bar per document, the rails
// mount and unmount on their own.
//
// SEVERAL RAILS: the most recently mounted one with a repository wins (a page
// rail over the shell's fallback; an explorer split's newer pane). Each rail
// holds a token for its life (`registerRailGit`); unmounting drops it, and
// the chip falls back to the next one, or goes.
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

// Insertion order is mount order: `registerRailGit` adds a token at mount.
const entries = new Map<object, RailGitDoor | null>();
const listeners = new Set<() => void>();
let current: RailGitDoor | null = null;

function recompute() {
  let next: RailGitDoor | null = null;
  for (const v of entries.values()) if (v) next = v;
  if (next === current) return;
  current = next;
  for (const l of [...listeners]) l();
}

/** A rail's slot, held for its life. The returned function drops it. */
export function registerRailGit(token: object): () => void {
  if (!entries.has(token)) entries.set(token, null);
  return () => {
    entries.delete(token);
    recompute();
  };
}

/** What a rail read (or null: no repository here, or not read yet). */
export function publishRailGit(token: object, value: RailGitDoor | null): void {
  if (!entries.has(token)) entries.set(token, null);
  entries.set(token, value);
  recompute();
}

export function railGitNow(): RailGitDoor | null {
  return current;
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The page's git, live: null while no mounted rail has a repository. */
export function useRailGitDoor(): RailGitDoor | null {
  return useSyncExternalStore(subscribe, railGitNow, railGitNow);
}

/** Test-only. */
export function resetRailGitStoreForTests(): void {
  entries.clear();
  current = null;
  for (const l of [...listeners]) l();
}
