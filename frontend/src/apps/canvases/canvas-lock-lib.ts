// When the embedded workbench is held read-only, extracted as a pure decision
// so the release rule can be tested. The rule is correctness-critical: getting
// it wrong re-opens the clobber window the lock exists to close.
//
// While a Claude session edits the clone, the user must not also be editing the
// same canvas in the workbench — collection saves there are last-writer-wins
// with no revision precondition, which is the D339 incident (a stale tab
// autosaved its pre-push in-memory state back over the remote).
//
// Releasing is the subtle half. On unlock the workbench FLUSHES whatever is
// dirty in its memory, and it only notices upstream changes on its own ~10s
// poll. So releasing the instant the agent's process exits gives: agent's last
// push still in flight or not yet pulled → workbench flushes stale state →
// last-writer-wins overwrites the agent's work. The same incident, reproduced
// by our own unlock. Hence release needs all three of: no live session, nothing
// pending or in flight, and a grace window past that longer than the poll.

/** Why the workbench is held read-only, or null for "not locked".
 *
 *  Only `editing` ever STARTS a lock. The other two only EXTEND one already
 *  engaged — otherwise arriving on a quiet canvas would lock the pane for a
 *  whole grace window for no reason. */
export type LockHold = "editing" | "publishing" | "settling";

/** The fields of SyncStatus the decision reads. */
export interface LockInput {
  watching: boolean;
  agent_active: boolean;
  push_state: "idle" | "pending" | "pushing" | "error";
}

export interface LockDecision {
  hold: LockHold | null;
  /** New value for the "settled at" stamp: a number to start/keep the grace
   *  window, or null to clear it. Local time, never the server's `last_push_at`
   *  — comparing that to Date.now() makes the window wrong by the clock skew. */
  settledAt: number | null;
}

/** Decide the hold.
 *
 *  `engaged` is whether a lock is currently on; `settledAt` is when the work
 *  first looked finished (null if not yet); `now` and `graceMs` are passed in
 *  so this is deterministic under test. */
export function decideLock(
  status: LockInput | null,
  engaged: boolean,
  settledAt: number | null,
  now: number,
  graceMs: number,
): LockDecision {
  // No watcher (server restart, dropped watcher) → release. The unlock must be
  // reachable from the page's own state, not only from a transition it might
  // miss, or the pane stays read-only with nothing left to free it.
  if (!status || !status.watching) return { hold: null, settledAt: null };
  if (status.agent_active) return { hold: "editing", settledAt: null };
  if (!engaged) return { hold: null, settledAt: null };
  if (status.push_state === "pending" || status.push_state === "pushing") {
    // The session's final change set has not landed. Unlocking now lets the
    // workbench flush stale state over work still on its way up.
    return { hold: "publishing", settledAt: null };
  }
  if (status.push_state === "error") {
    // Deliberate: do NOT hold for a success that is not coming. The failure is
    // already surfaced by the error banner and its Fix button, and holding the
    // workbench read-only until someone fixes a validation error would be a
    // lock with no end condition.
    return { hold: null, settledAt: null };
  }
  const since = settledAt ?? now;
  if (now - since >= graceMs) return { hold: null, settledAt: null };
  return { hold: "settling", settledAt: since };
}

/** The banner's headline. Three distinct facts: conflating them leaves a user
 *  staring at a still-locked pane after the chat visibly stopped, with no idea
 *  why it has not released. */
export function lockMessage(hold: LockHold): string {
  if (hold === "editing") return "Claude is editing this canvas";
  if (hold === "publishing") return "Publishing Claude’s changes";
  return "Finishing up — waiting for the workbench to catch up";
}
