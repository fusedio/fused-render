// In-app clipboard for the file explorer's cut/copy (single entry, like
// Finder). A cut entry is shown dimmed in the listing until it's pasted.
//
// Deliberately a MODULE-level store, not component state: App keys each
// StatView on `epoch + ":" + fsPath`, so navigating INTO a folder remounts
// Listing — a useState clipboard would be wiped on the way, killing the whole
// copy-here / paste-there gesture. Lifting it out of the remount boundary
// keeps a cut/copy alive across navigation (and cut-dimming reappears when you
// browse back to the source dir). One clipboard for the whole app, like the OS.
import { useSyncExternalStore } from "react";

export interface Clipboard {
  path: string;
  op: "copy" | "cut";
}

let clipboard: Clipboard | null = null;
const listeners = new Set<() => void>();

// Synchronous read — the atomic-consume path (doPaste) uses this so a rapid
// second paste sees the cleared clipboard immediately, before any re-render.
export function getClipboard(): Clipboard | null {
  return clipboard;
}

export function setClipboard(next: Clipboard | null): void {
  clipboard = next;
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Subscribe a component to the shared clipboard; re-renders on any set.
export function useClipboard(): Clipboard | null {
  return useSyncExternalStore(subscribe, getClipboard);
}
