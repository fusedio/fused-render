// When the embedded workbench is held read-only, extracted as a pure decision
// so the release rule can be tested. The rule is correctness-critical: getting
// it wrong re-opens the clobber window the lock exists to close.
//
// TRIGGER, by deliberate owner decision (superseding an earlier "lock while a
// Claude session is live" design, D352): the lock engages ONLY while a sync
// operation is actually moving the clone's files — a push in flight
// (`push_state === "pushing"`), or a pull/merge in flight — never merely
// because a Claude run is live, and NOT while a change set is merely queued.
// D354 narrowed this further: `push_state === "pending"` means the watcher
// has recorded dirty files and is HOLDING the auto-push because a session is
// live (D350) — no sync op has started moving anything yet, so there is
// nothing in flight to protect. The sync watcher sets "pending" the instant
// Claude writes its first file, and holds it there for the entire edit/chat
// stretch until the explicit push runs; treating "pending" as a lock trigger
// re-creates exactly the "locks from Claude's first file write" bug D352 was
// written to fix, just one field over. Claude may make many changes over a
// long chat, and the user must not be blocked from the workbench for the
// whole length of it; a plain "hi" with no edits at all must never lock
// anything, and neither must an edit that has not yet started publishing.
// The accepted trade-off: a user editing in the workbench WHILE Claude is
// mid-edit, or while a change set sits "pending" awaiting push, is no longer
// prevented by this lock. D338's per-file three-way merge is what handles
// that now — a same-file conflict may surface where the lock used to simply
// forbid the collision, which is a deliberate, accepted cost of not blocking
// the user for the length of a chat. The only coverage this gives up versus
// locking on "pending" is the no-session auto-push's own ~1.5s debounce
// window — the merge covers that too, so nothing is actually lost.
//
// Releasing is the subtle half, and only for a PUSH. On unlock the workbench
// FLUSHES whatever is dirty in its memory, and it only notices upstream
// changes on its own ~10s poll. A push moves the remote out from under
// that poll, so releasing the instant push_state goes idle gives: the
// agent's last push still in flight or not yet visible upstream → workbench
// flushes stale state → last-writer-wins overwrites the agent's work — the
// D339 incident, reproduced by our own unlock. Hence a push's release needs a
// grace window past that longer than the poll. A PULL does not move the
// remote — it only writes local files from a manifest already fetched — so
// there is nothing upstream for the workbench's poll to race, and pulling
// releases the instant it ends, no grace window.

/** Why the workbench is held read-only, or null for "not locked". Every hold
 *  STARTS a lock unconditionally — there is no "already engaged" precondition
 *  for `publishing`/`pulling` any more, because a sync operation in flight is
 *  never safe to ignore, even on a page that just loaded. Only `settling`
 *  requires having just come out of a `publishing` hold (see decideLock). */
export type LockHold = "publishing" | "pulling" | "settling";

/** The fields of SyncStatus the decision reads. `agent_active` is
 *  deliberately NOT here: it is reported for informational/badge use, but no
 *  longer drives the lock at all. */
export interface LockInput {
  watching: boolean;
  push_state: "idle" | "pending" | "pushing" | "error";
  /** A force-pull or three-way merge is writing to the clone's files right
   *  now (canvases.py's `_SyncManager._pulling`). */
  pulling: boolean;
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
 *  `prevHold` is the hold this same function returned last time (null before
 *  the first call) — it is what lets the grace window arm ONLY when coming
 *  out of a push, not out of a pull: `settledAt` starts only when `prevHold`
 *  was "publishing" or "settling" itself. `now` and `graceMs` are passed in so
 *  this stays deterministic under test. */
export function decideLock(
  status: LockInput | null,
  prevHold: LockHold | null,
  settledAt: number | null,
  now: number,
  graceMs: number,
): LockDecision {
  // No watcher (server restart, dropped watcher, or a repeatedly failing
  // status poll treated the same way) → release. The unlock must be reachable
  // from the page's own state, not only from a transition it might miss, or
  // the pane stays read-only with nothing left to free it.
  if (!status || !status.watching) return { hold: null, settledAt: null };
  if (status.pulling) return { hold: "pulling", settledAt: null };
  // "pending" is deliberately NOT here (D354): it means the watcher is
  // holding queued local edits while a session is live, not that a push is
  // moving anything. Only "pushing" means a sync op is actually in flight —
  // that is the only moment the D339 clobber risk exists, so that is the
  // only moment this locks.
  if (status.push_state === "pushing") {
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
  // Idle, and not pulling. A pull just finishing releases immediately — it
  // never moved the remote, so there is nothing for the workbench's own
  // upstream poll to race. Only a push finishing arms the grace window.
  if (prevHold !== "publishing" && prevHold !== "settling") {
    return { hold: null, settledAt: null };
  }
  const since = settledAt ?? now;
  if (now - since >= graceMs) return { hold: null, settledAt: null };
  return { hold: "settling", settledAt: since };
}

/** The banner's headline. Three distinct facts: conflating them leaves a user
 *  staring at a still-locked pane after the sync op visibly stopped, with no
 *  idea why it has not released. */
export function lockMessage(hold: LockHold): string {
  if (hold === "publishing") return "Publishing Claude’s changes";
  if (hold === "pulling") return "Pulling in workbench changes";
  return "Finishing up — waiting for the workbench to catch up";
}

// -- enforcement handshake -------------------------------------------------
//
// Enforcement belongs to the WORKBENCH, not this page: an overlay here can
// only ever block pointer events, never the workbench's own autosave timers
// or its upstream poll, so the page must never imply protection it does not
// have. The workbench that supports the lock replies `fused-embed-lock-ack`
// to the `fused-embed-lock` message; one that does not (an older or
// production deployment) stays silent. `AckState` is what CanvasWorkspace
// renders off: "acked" → no blocking scrim, the workbench enforces it and the
// canvas stays fully pannable/zoomable; "waiting"/"unacked" → a translucent
// fallback scrim, since nothing else is stopping a click from reaching an
// editable workbench.

export type AckState = "waiting" | "acked" | "unacked";

/** One transition in the ack handshake, kept pure so the reset-per-engagement
 *  rule is testable without a timer or postMessage in the loop.
 *
 *  "engage": a NEW lock engagement started (the previous hold was null) —
 *  always resets to "waiting", even if a prior engagement had already been
 *  acked; the capability could differ next time (a different workbench
 *  version deployed under this same tab is not the scenario this guards, but
 *  "assume nothing carries over" is the safe default and the one the owner
 *  asked for).
 *  "ack": the workbench's `fused-embed-lock-ack` arrived — always wins,
 *  including over an "unacked" fallback already showing (a late ack upgrades
 *  to pass-through instead of leaving the fallback scrim up needlessly).
 *  "timeout": the ack window elapsed with no reply — downgrades to
 *  "unacked" ONLY if still "waiting"; an ack that already arrived (or a
 *  previous timeout) must never be undone by a stale timer firing late. */
export function nextAckState(
  prev: AckState,
  event: "engage" | "ack" | "timeout",
): AckState {
  if (event === "ack") return "acked";
  if (event === "engage") return "waiting";
  return prev === "waiting" ? "unacked" : prev;
}
