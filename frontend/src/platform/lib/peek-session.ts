// The session the Tasks side peek (shell/TaskPeek.tsx) is showing a chat for
// right now, or null. Read by the favourite's card (FavouritePeek): the same
// session there and in the peek would be two composers on one draft, so the
// card keeps no chat of its own while the peek has it.
import { useSyncExternalStore } from "react";

let current: string | null = null;
const listeners = new Set<() => void>();

/** The peek says what it shows (null: nothing, or closed). */
export function publishPeekSession(sid: string | null): void {
  if (current === sid) return;
  current = sid;
  for (const l of listeners) l();
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
