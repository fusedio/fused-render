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

// `lastSeenOsToken` has TWO writers — the mirror-write's response and the
// focus-time reconcile — and both compute their answer across an `await`, so
// they can finish out of order. Neither one alone can tell whether what it is
// holding is still current; that needs a shared clock, which is what these are.
//
// An observation takes a ticket when it STARTS (it is the moment of asking
// that a result describes, not the moment of answering) and may only commit if
// no LATER-started observation already has. Without this, each writer got its
// own private guard and the two could still rewind each other: a copy's
// mirror-write issued before a reconcile's read, but delivered after it, would
// overwrite the fresher foreign token the reconcile had just recorded — the
// next focus would then see that foreign clipboard as never-seen and adopt it
// over whatever the user had done since, including a pending cut.
let osObsSeq = 0;
let osObsCommitted = 0;

export function beginOsObservation(): number {
  return ++osObsSeq;
}

export function commitOsToken(seq: number, token: string): void {
  if (seq < osObsCommitted) return;
  osObsCommitted = seq;
  lastSeenOsToken = token;
}

// Unconditional set, for callers that are not racing anything (tests, and any
// future caller with a token already known to be current). Takes a fresh ticket
// so it outranks every observation in flight rather than being silently undone
// by one.
export function setLastSeenOsToken(token: string): void {
  commitOsToken(beginOsObservation(), token);
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
    // Ticketed against the reconcile as well as against other writes — see
    // `beginOsObservation`. Deliberately NOT gated on `clipboardEpoch`: gating
    // on the epoch (as this briefly did) inverted the mechanism, because a cut,
    // an Escape clear and a bookkeeping repair all bump the epoch while
    // publishing NOTHING, so an in-flight copy write dropped its token and the
    // next reconcile adopted the OS copy straight over the newer cut. The token
    // describes the SYSTEM clipboard, not the app's, so it survives every
    // in-app gesture that never touches the system one; only a newer
    // OBSERVATION of the system clipboard can supersede it.
    const seq = beginOsObservation();
    writeOsClipboard(next.paths)
      .then((res) => {
        // Only a real write is worth remembering: an unsupported bridge
        // returns an empty token, and storing it would make the next
        // reconcile think the clipboard had changed.
        if (res.supported && res.token) commitOsToken(seq, res.token);
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
