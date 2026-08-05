// The browser sign-in poll's decisions (D205, D209), as pure functions so they
// can be tested without a component renderer.
//
// The flow is: POST to start, then poll the status endpoint until `in_flight`
// drops. Everything subtle lives in when to STOP waiting — a poll that only
// ever stops on a good answer will spin forever on a server that has stopped
// giving one, leaving the user on a spinner with no way to tell whether their
// account was connected.
//
// Everything here is PROVIDER-GENERIC. It used to say "Google" in every string,
// which was simply wrong once Dropbox and Box arrived; the label travels with
// the poll instead.
import type { RemoteOAuthStatus } from "./api";

// The three backends reached by a browser consent. Mirrors the server's
// _OAUTH_PROVIDERS (shell/mounts/endpoints.py) — the server validates against
// its own copy, so a drift here is a 400, never a silently wrong remote.
export type OAuthProviderKey = "drive" | "dropbox" | "box";

export interface OAuthProvider {
  key: OAuthProviderKey;
  // What the user calls it. Used verbatim in every message below.
  label: string;
  // Whether the user must supply their own OAuth client before consenting.
  // TRUE FOR DRIVE ONLY: Google is retiring rclone's built-in shared client ID
  // (charging for requests made with it starts later in 2026), so a Drive
  // remote without the user's own client is on a countdown. Dropbox and Box are
  // unaffected — rclone still says "Leave blank normally" for both — and must
  // never be shown client id/secret fields.
  needsClient: boolean;
  // Seed for the remote name, so the common case is one click.
  defaultRemoteName: string;
}

export const OAUTH_PROVIDERS: Record<OAuthProviderKey, OAuthProvider> = {
  drive: {
    key: "drive",
    label: "Google Drive",
    needsClient: true,
    defaultRemoteName: "gdrive",
  },
  dropbox: {
    key: "dropbox",
    label: "Dropbox",
    needsClient: false,
    defaultRemoteName: "dropbox",
  },
  box: { key: "box", label: "Box", needsClient: false, defaultRemoteName: "box" },
};

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

export const lostContactMsg = (label: string) =>
  `Lost contact with the server while waiting for the ${label} sign-in. ` +
  `Reload the page and check whether the remote was created.`;
export const timedOutMsg = (label: string) => `The ${label} sign-in timed out. Try again.`;
export const genericFailMsg = (label: string) =>
  `The ${label} sign-in did not complete. Try again.`;

// One poll tick. `status` is null when the fetch itself failed;
// `consecutiveFailures` counts that failure (so the first one arrives as 1).
// `label` names the provider in any message this produces.
export function oauthTick(
  status: RemoteOAuthStatus | null,
  ctx: { consecutiveFailures: number; elapsedMs: number; label: string }
): OAuthDecision {
  if (status === null) {
    // A blip is not a failed sign-in — the child is still out there and the
    // user may still be mid-consent. Only a sustained silence ends the wait.
    return ctx.consecutiveFailures >= OAUTH_MAX_POLL_FAILURES
      ? { kind: "failed", message: lostContactMsg(ctx.label) }
      : { kind: "wait" };
  }
  if (status.in_flight) {
    return ctx.elapsedMs > OAUTH_GIVE_UP_MS
      ? { kind: "failed", message: timedOutMsg(ctx.label) }
      : { kind: "wait" };
  }
  // in_flight has dropped: the remote exists now, or the attempt failed —
  // including the child that produced no token at all (abandoned tab, denied
  // consent), whose message already tells the user to try again.
  if (status.ok) return { kind: "connected" };
  return { kind: "failed", message: status.error ?? genericFailMsg(ctx.label) };
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
  status: RemoteOAuthStatus | null
): OAuthDecision {
  if (canceled) return { kind: "cancelled" };
  if (status === null) return { kind: "cancelled" }; // can't tell; stand down quietly
  // Still in flight with nothing decided: the CHILD is gone (which is why the
  // cancel found nothing to kill) but the server's watcher is still finalizing
  // — creating the remote over rcd, bounded by OAUTH_RC_TIMEOUT at 30s and
  // possibly preceded by spawning the daemon. There IS an outcome coming, so
  // standing down here is the one thing we must not do: it stopped the poll and
  // closed the modal, the remote got created and never appeared, and a retry
  // inside that window 409'd on a sign-in the user believed they had cancelled.
  // Keep waiting and let the poll's own bounds end it if no answer arrives.
  if (status.in_flight) return { kind: "wait" };
  if (status.ok) return { kind: "connected" };
  return status.error ? { kind: "failed", message: status.error } : { kind: "cancelled" };
}
