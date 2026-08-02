// In-app clipboard for the file explorer's cut/copy (one or more entries, like
// Finder). A cut entry is shown dimmed in the listing until it's pasted.
//
// Deliberately a MODULE-level store, not component state: App keys each
// StatView on `epoch + ":" + fsPath`, so navigating INTO a folder remounts
// Listing — a useState clipboard would be wiped on the way, killing the whole
// copy-here / paste-there gesture. Lifting it out of the remount boundary
// keeps a cut/copy alive across navigation (and cut-dimming reappears when you
// browse back to the source dir). One clipboard for the whole app, like the OS.
import { useSyncExternalStore } from "react";
import { writeOsClipboard } from "./api";

// One or more entries, in the order they were selected. A multi-row cut/copy is
// a single clipboard (like the OS): pasting it moves/copies every path into the
// one target folder, and every cut path is dimmed in the listing until pasted.
// Invariant: a non-null clipboard always carries at least one path — callers
// clear it to null rather than storing an empty list (see clearClipboardIfDeleted).
export interface Clipboard {
  paths: string[];
  op: "copy" | "cut";
}

let clipboard: Clipboard | null = null;
const listeners = new Set<() => void>();

// Synchronous read — the atomic-consume path (doPaste) uses this so a rapid
// second paste sees the cleared clipboard immediately, before any re-render.
export function getClipboard(): Clipboard | null {
  return clipboard;
}

// Fingerprint of the OS clipboard contents we last SAW — set both by our own
// copy (below) and by the focus-time reconcile (os-clipboard.ts). Tracking
// "last seen" rather than "last written" is what stops a stale system
// clipboard from clobbering a pending in-app cut on every focus change.
let lastSeenOsToken = "";

export function getLastSeenOsToken(): string {
  return lastSeenOsToken;
}

export function setLastSeenOsToken(token: string): void {
  lastSeenOsToken = token;
}

export function setClipboard(next: Clipboard | null): void {
  clipboard = next;
  for (const l of listeners) l();

  // Mirror a COPY onto the system clipboard so the native file manager can
  // paste the real files (and a terminal paste yields the path). Deliberately
  // fire-and-forget and never awaited: the in-app copy above has already
  // happened, and a machine with no clipboard bridge — or a failed request —
  // must degrade to exactly today's behaviour rather than break the gesture.
  // Same swallow-the-failure posture as copyToClipboard in fs-actions.ts.
  //
  // Cut is excluded on purpose: no platform exposes a reliable cut-vs-copy
  // flag on read, so publishing one would invite another app to act on a
  // guess. All four Copy call sites (Listing, Preview) route through here, so
  // this one hook covers every one of them.
  if (next && next.op === "copy" && next.paths.length > 0) {
    writeOsClipboard(next.paths)
      .then((res) => {
        // Only a real write is worth remembering: an unsupported bridge
        // returns an empty token, and storing it would make the next
        // reconcile think the clipboard had changed.
        if (res.supported && res.token) lastSeenOsToken = res.token;
      })
      .catch(() => {});
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Subscribe a component to the shared clipboard; re-renders on any set.
export function useClipboard(): Clipboard | null {
  return useSyncExternalStore(subscribe, getClipboard);
}
