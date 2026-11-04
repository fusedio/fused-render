// WHICH CHATS ARE ON SCREEN ELSEWHERE, for the favourite's card
// (shell/claude-rail/FavouritePeek): the same session there and in another
// mounted composer would be two composers on one draft, so the card keeps no
// chat of its own while another mount shows it — and ONLY then (owner,
// 2026-11-04: the card said "Open in the Claude panel" with the panel shut).
// Both signals are POSITIVE: a mount says what it shows while it is mounted,
// and takes it back on unmount — never a guess from the page's URL, which
// keeps `session_id` with the pane shut.
//
//   - the Tasks side peek (shell/TaskPeek.tsx) publishes the session its
//     chat is on (`publishPeekSession`), null while it shows none;
//   - every URL-backed chat mount — the app page's and the canvas page's
//     Claude pane, the explorer's sidebar, content pane and folder pane —
//     announces the URL's `session_id` while it is mounted
//     (apps/claude/ChatMount `announcePaneSession`).
import { useSyncExternalStore } from "react";

let current: string | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** The peek says what it shows (null: nothing, or closed). */
export function publishPeekSession(sid: string | null): void {
  if (current === sid) return;
  current = sid;
  emit();
}

export function peekSession(): string | null {
  return current;
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function usePeekSession(): string | null {
  return useSyncExternalStore(subscribe, peekSession, peekSession);
}

/** One entry per mounted pane chat (a mount may repeat another's session). */
const panes = new Map<number, string>();
let paneSeq = 0;
const NONE: readonly string[] = [];
let paneSnap: readonly string[] = NONE;

function settlePanes(): void {
  const next = [...new Set(panes.values())].sort();
  if (next.length === paneSnap.length && next.every((s, i) => s === paneSnap[i])) return;
  paneSnap = next.length ? next : NONE;
  emit();
}

/** A pane's chat on `sid` is mounted: the answer takes it back. */
export function announcePaneSession(sid: string): () => void {
  const id = ++paneSeq;
  panes.set(id, sid);
  settlePanes();
  return () => {
    panes.delete(id);
    settlePanes();
  };
}

/** The sessions the page's pane chats show now (sorted, deduplicated). */
export function paneSessions(): readonly string[] {
  return paneSnap;
}

export function usePaneSessions(): readonly string[] {
  return useSyncExternalStore(subscribe, paneSessions, paneSessions);
}
