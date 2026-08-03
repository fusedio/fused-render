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

// Bumped on every set, and read by the one place that adopts PATHS across an
// `await`: the focus-time reconcile (os-clipboard.ts). It captures this before
// its read and drops the result if it moved, because paths computed against a
// clipboard the user has since replaced are not an update, they're a rewind —
// without it a slow read overwrites a copy or cut made while it was in flight.
//
// Deliberately NOT applied to the mirror-write's response below, which records
// a token rather than adopting paths; see the comment there for why gating that
// one re-opens the very clobber this guards against.
let clipboardEpoch = 0;

// Counts mirror-writes ISSUED. Only the most recent one may record a token —
// the narrow version of the guard above, for the narrow thing that can actually
// stale a token: another copy publishing different paths to the system
// clipboard.
let osWriteSeq = 0;

export function getClipboardEpoch(): number {
  return clipboardEpoch;
}

// `mirrorToOs: false` stores the clipboard WITHOUT publishing it back to the
// system. Two kinds of caller need it, and both are "we are not the user
// copying something":
//   - the focus-time reconcile (os-clipboard.ts), adopting paths that are
//     already on the OS clipboard — echoing them back is a pointless round-trip
//     and on Linux steals selection ownership from the file manager that
//     legitimately holds it;
//   - the bookkeeping in fs-actions.ts (clearClipboardIfDeleted,
//     remapClipboardPath), which is repairing our own reference after a delete
//     or a rename. The system clipboard belongs to whoever last copied onto it;
//     rewriting it behind the user's back on an unrelated file operation is not
//     ours to do.
export function setClipboard(next: Clipboard | null, mirrorToOs = true): void {
  clipboard = next;
  clipboardEpoch++;
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
  if (mirrorToOs && next && next.op === "copy" && next.paths.length > 0) {
    const seq = ++osWriteSeq;
    writeOsClipboard(next.paths)
      .then((res) => {
        // Superseded by a LATER MIRROR-WRITE, which is the only thing that can
        // make this token stale: another copy has since published different
        // paths, so recording this one would rewind what we last saw.
        //
        // Deliberately the write sequence and NOT `clipboardEpoch`. Gating on
        // the epoch (as this briefly did) inverted the mechanism, because a
        // cut, an Escape clear and a bookkeeping repair all bump the epoch
        // while publishing NOTHING: an in-flight copy write would drop its
        // token, and the next reconcile — seeing the OS copy as never seen —
        // adopted it straight over the newer cut. That is exactly the clobber
        // `lastSeenOsToken` exists to prevent, reintroduced by its own guard.
        //
        // The distinction is what the token means. It describes the SYSTEM
        // clipboard, not the app's, so it survives in-app gestures that never
        // touch the system one; only another write to the system clipboard
        // invalidates it. The epoch guard belongs on the reconcile's read
        // (os-clipboard.ts), which adopts PATHS into the app — nothing here
        // does.
        if (seq !== osWriteSeq) return;
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
