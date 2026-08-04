// The Drive sign-in poll's decisions (D205), as pure functions so they can be
// tested without a component renderer.
//
// The flow is: POST to start, then poll the status endpoint until `in_flight`
// drops. Everything subtle lives in when to STOP waiting — a poll that only
// ever stops on a good answer will spin forever on a server that has stopped
// giving one, leaving the user on a spinner with no way to tell whether their
// Google account was connected.
import type { DriveOAuthStatus } from "./api";

export type OAuthDecision =
  | { kind: "wait" } // keep waiting; nothing to show the user yet
  | { kind: "connected" } // the remote exists — refresh and close
  | { kind: "cancelled" } // stood down cleanly; clear state, say nothing
  | { kind: "failed"; message: string };

// Consecutive status fetches that may fail before the wait is abandoned. A few
// ticks of slack absorbs a server restart or a dropped socket; past that the
// endpoint is not coming back.
export const OAUTH_MAX_POLL_FAILURES = 6;
// Wall-clock backstop, comfortably past the server's own 5-minute OAUTH_TIMEOUT
// (after which it kills the child). Reaching this means the two sides disagree
// about what is still running, and more waiting will not fix that.
export const OAUTH_GIVE_UP_MS = 6 * 60 * 1000;

export const LOST_CONTACT_MSG =
  "Lost contact with the server while waiting for the Google sign-in. " +
  "Reload the page and check whether the remote was created.";
export const TIMED_OUT_MSG = "The Google sign-in timed out. Try again.";
export const GENERIC_FAIL_MSG = "The Google sign-in did not complete. Try again.";

// One poll tick. `status` is null when the fetch itself failed;
// `consecutiveFailures` counts that failure (so the first one arrives as 1).
export function oauthTick(
  status: DriveOAuthStatus | null,
  ctx: { consecutiveFailures: number; elapsedMs: number }
): OAuthDecision {
  if (status === null) {
    // A blip is not a failed sign-in — the child is still out there and the
    // user may still be mid-consent. Only a sustained silence ends the wait.
    return ctx.consecutiveFailures >= OAUTH_MAX_POLL_FAILURES
      ? { kind: "failed", message: LOST_CONTACT_MSG }
      : { kind: "wait" };
  }
  if (status.in_flight) {
    return ctx.elapsedMs > OAUTH_GIVE_UP_MS
      ? { kind: "failed", message: TIMED_OUT_MSG }
      : { kind: "wait" };
  }
  // in_flight has dropped: the remote exists now, or the attempt failed —
  // including the child that produced no token at all (abandoned tab, denied
  // consent), whose message already tells the user to try again.
  if (status.ok) return { kind: "connected" };
  return { kind: "failed", message: status.error ?? GENERIC_FAIL_MSG };
}

// What a Cancel click actually means, once the server has answered.
//
// `canceled: false` does NOT mean "nothing happened" — it means nothing was
// live to kill, which on this path usually means the sign-in COMPLETED in the
// gap before the click landed. Discarding that would leave a remote created
// and the user told nothing. `status` is the follow-up read (null if it
// failed).
export function oauthCancelOutcome(
  canceled: boolean,
  status: DriveOAuthStatus | null
): OAuthDecision {
  if (canceled) return { kind: "cancelled" };
  if (status === null) return { kind: "cancelled" }; // can't tell; stand down quietly
  if (status.ok) return { kind: "connected" };
  return status.error ? { kind: "failed", message: status.error } : { kind: "cancelled" };
}
