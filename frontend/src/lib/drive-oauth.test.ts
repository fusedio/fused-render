// The Drive sign-in poll's stopping conditions. Everything here is about NOT
// waiting forever: a poll that only stops on a good answer leaves the user on a
// spinner with no way to learn whether their account was connected.
import { expect, test } from "bun:test";

import {
  GENERIC_FAIL_MSG,
  LOST_CONTACT_MSG,
  OAUTH_GIVE_UP_MS,
  OAUTH_MAX_POLL_FAILURES,
  TIMED_OUT_MSG,
  oauthCancelOutcome,
  oauthTick,
} from "./drive-oauth";
import type { DriveOAuthStatus } from "./api";

const inFlight: DriveOAuthStatus = {
  in_flight: true,
  name: "gdrive",
  backend: "drive",
  ok: null,
  error: null,
};
const succeeded: DriveOAuthStatus = { ...inFlight, in_flight: false, ok: true };
const failed = (error: string): DriveOAuthStatus => ({
  ...inFlight,
  in_flight: false,
  ok: false,
  error,
});
const fresh = { consecutiveFailures: 0, elapsedMs: 0 };

// -- the normal path -----------------------------------------------------------

test("an in-flight sign-in keeps waiting", () => {
  expect(oauthTick(inFlight, fresh)).toEqual({ kind: "wait" });
});

test("in_flight dropping with ok reports the connection", () => {
  expect(oauthTick(succeeded, fresh)).toEqual({ kind: "connected" });
});

test("in_flight dropping without ok surfaces the server's own message", () => {
  // The abandoned-tab case: the server's text already says to try again.
  expect(oauthTick(failed("no account was connected. Try again."), fresh)).toEqual({
    kind: "failed",
    message: "no account was connected. Try again.",
  });
});

test("a failure with no message still says something", () => {
  expect(oauthTick({ ...failed(""), error: null }, fresh)).toEqual({
    kind: "failed",
    message: GENERIC_FAIL_MSG,
  });
});

// -- bounds (the regression) ---------------------------------------------------

test("a transient fetch failure keeps waiting", () => {
  // A server restart or dropped socket mid-consent must not kill the sign-in.
  expect(oauthTick(null, { ...fresh, consecutiveFailures: 1 })).toEqual({ kind: "wait" });
  expect(
    oauthTick(null, { ...fresh, consecutiveFailures: OAUTH_MAX_POLL_FAILURES - 1 })
  ).toEqual({ kind: "wait" });
});

test("sustained fetch failures END the wait instead of spinning forever", () => {
  // The regression: every fetch error was swallowed with no bound, so a status
  // endpoint that stopped answering (crash, 401) left the modal on "Waiting for
  // you to approve access…" indefinitely. The server's own OAUTH_TIMEOUT cannot
  // rescue that — reading it needs the fetch that is failing.
  expect(
    oauthTick(null, { ...fresh, consecutiveFailures: OAUTH_MAX_POLL_FAILURES })
  ).toEqual({ kind: "failed", message: LOST_CONTACT_MSG });
});

test("an in_flight that outlives the server's own timeout is given up on", () => {
  expect(oauthTick(inFlight, { ...fresh, elapsedMs: OAUTH_GIVE_UP_MS + 1 })).toEqual({
    kind: "failed",
    message: TIMED_OUT_MSG,
  });
  // Still inside the window: keep waiting, the user may be mid-consent.
  expect(oauthTick(inFlight, { ...fresh, elapsedMs: OAUTH_GIVE_UP_MS - 1 })).toEqual({
    kind: "wait",
  });
});

test("a success arriving on the same tick as the deadline still counts", () => {
  // The give-up branch must only apply to a still-in-flight attempt; a
  // completed one is reported however late the tick is.
  expect(oauthTick(succeeded, { ...fresh, elapsedMs: OAUTH_GIVE_UP_MS * 2 })).toEqual({
    kind: "connected",
  });
});

// -- cancel --------------------------------------------------------------------

test("a cancel that killed a live child stands down quietly", () => {
  expect(oauthCancelOutcome(true, null)).toEqual({ kind: "cancelled" });
});

test("cancelling AFTER the sign-in completed reports the connection", () => {
  // canceled:false means nothing was live to kill — which on this path means
  // the round-trip landed in the gap before the click. Discarding it would
  // leave a remote created and the user told nothing about it.
  expect(oauthCancelOutcome(false, succeeded)).toEqual({ kind: "connected" });
});

test("cancelling after a failure surfaces that failure", () => {
  expect(oauthCancelOutcome(false, failed("rclone authorize drive failed"))).toEqual({
    kind: "failed",
    message: "rclone authorize drive failed",
  });
});

test("cancelling with an unreadable status stands down rather than inventing one", () => {
  expect(oauthCancelOutcome(false, null)).toEqual({ kind: "cancelled" });
});
