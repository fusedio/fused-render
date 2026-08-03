// The in-app Fused sign-in flow, shared by the Account page and the Deploy
// modal (account.py; SPEC §27, AC-3/AC-4).
//
// begin() asks the server to spawn `fused cloud login --no-browser`, opens
// the returned authorize URL in a new tab (the browser side is always the
// client's job), then POLLS /api/account/status every 2s until `logged_in`
// flips — there is no push channel from the CLI; the flow app uses the same
// cadence. The CLI child self-terminates after ~5 minutes if the sign-in is
// abandoned; polling notices its exit (login_in_flight false without
// logged_in) and surfaces that instead of spinning forever.
import { useEffect, useRef, useState } from "react";
import { cancelAccountLogin, getAccountStatus, startAccountLogin } from "./api";
import type { AccountStatus } from "./api";
import { useRefreshOnReturn } from "./hooks";

const POLL_MS = 2000;

// Cross-component "auth state changed" signal (the notifyBookmarksChanged
// pattern): a same-tab sign-in/sign-out gets no focus/visibility event, so
// the actor announces it and the sidebar dot re-reads immediately.
const ACCOUNT_EVENT = "fused:accountchange";

export function notifyAccountChanged() {
  window.dispatchEvent(new Event(ACCOUNT_EVENT));
}

// The sidebar's signed-in signal: the cheap presence-only `logged_in` flag,
// re-read on focus/visibility regain (useRefreshOnReturn — the deploy-dot
// cadence) and on the notifyAccountChanged signal, so a sign-in or sign-out
// — in this tab or any other — shows through without a remount. Errors leave
// the last-known value (a blip must not flicker the dot).
export function useAccountLoggedIn(): boolean {
  const [loggedIn, setLoggedIn] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const refresh = () => {
    getAccountStatus().then(
      (s) => {
        if (alive.current) setLoggedIn(s.logged_in);
      },
      () => {}
    );
  };
  useRefreshOnReturn(refresh);
  useEffect(() => {
    refresh(); // initial read
    const onChange = () => refresh();
    window.addEventListener(ACCOUNT_EVENT, onChange);
    return () => window.removeEventListener(ACCOUNT_EVENT, onChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return loggedIn;
}

// onLoggedIn receives the fresh status the poll ALREADY fetched, so callers
// can flip their signed-in UI synchronously — success must never hinge on
// one more fetch that could transiently fail and strand a signed-in user on
// a signed-out view.
export function useFusedLogin(onLoggedIn: (status: AccountStatus) => void) {
  const [connecting, setConnecting] = useState(false);
  // The credentials fingerprint as it was when this sign-in began. Completion is
  // "the credentials CHANGED", not "credentials exist": `logged_in` is
  // presence-only (account.py — a file on disk), so it is ALREADY true when the
  // user is re-authenticating a stale login whose refresh token the IdP now
  // rejects. Polling on presence alone would see true on the first tick and
  // report success before the browser round-trip had even happened. `creds_stamp`
  // is the file's mtime and exists for exactly this (AC-8: a re-login "even one
  // that never flips logged_in false in this tab").
  const stampAtBegin = useRef<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  // Latest-ref: the poll always calls the current callback, never a stale
  // closure from the render when polling started (the DeployModal pattern).
  const onLoggedInRef = useRef(onLoggedIn);
  onLoggedInRef.current = onLoggedIn;

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => stopPolling, []);

  const finish = (err: string | null) => {
    stopPolling();
    setConnecting(false);
    setError(err);
  };

  // Did THIS sign-in complete? Logged in AND the credentials file is not the one
  // `begin` recorded. Shared by the poll and by `cancel`'s reconcile because the two
  // must agree on what "done" means: cancel tested bare `logged_in`, so on the
  // re-auth path — where credentials are already present, just rejected — pressing
  // Cancel reported a completed sign-in that never happened, and dismissed the very
  // note that had asked the user to sign in again.
  //
  // With no baseline (the pre-flight read failed, or `begin` never ran) this degrades
  // to the old presence check: right for the signed-out case, and merely eager for a
  // re-auth — never the reverse.
  const isFreshLogin = (status: AccountStatus) =>
    status.logged_in &&
    (stampAtBegin.current === null || status.creds_stamp !== stampAtBegin.current);

  const begin = async () => {
    setError(null);
    setConnecting(true);
    // Captured BEFORE the child is spawned, so a login that completes unusually
    // fast cannot land between the read and the baseline being recorded.
    try {
      stampAtBegin.current = (await getAccountStatus()).creds_stamp ?? null;
    } catch {
      stampAtBegin.current = null; // unreadable baseline → fall back to presence
    }
    let url: string;
    try {
      ({ authorize_url: url } = await startAccountLogin(window.location.href));
    } catch (e) {
      finish((e as Error).message);
      return;
    }
    window.open(url, "_blank", "noopener");
    stopPolling(); // begin() while already polling joins the same server child
    timer.current = window.setInterval(async () => {
      let status;
      try {
        status = await getAccountStatus();
      } catch {
        return; // transient (server restart, network blip) — keep polling
      }
      if (timer.current === null) return; // canceled while the fetch was in flight
      if (isFreshLogin(status)) {
        finish(null);
        notifyAccountChanged(); // e.g. the sidebar's signed-in dot
        onLoggedInRef.current(status);
      } else if (!status.login_in_flight) {
        finish("Sign-in was not completed — the browser sign-in was closed or timed out. Try again.");
      }
    }, POLL_MS);
  };

  const cancel = async () => {
    finish(null);
    try {
      await cancelAccountLogin();
    } catch {
      // Best-effort: the child self-terminates on its own timeout anyway.
    }
    // The sign-in may have COMPLETED in the gap before the cancel landed
    // (credentials written, child already gone) — reconcile once instead of
    // leaving a signed-in user on a signed-out view until the next refocus.
    // Same freshness test the poll uses (`isFreshLogin`): what makes this a
    // completed sign-in is new credentials, not the presence of any.
    try {
      const status = await getAccountStatus();
      if (isFreshLogin(status)) {
        notifyAccountChanged();
        onLoggedInRef.current(status);
      }
    } catch {
      // Unreachable server — the callers' own refresh paths converge later.
    }
  };

  return { connecting, error, begin, cancel };
}
