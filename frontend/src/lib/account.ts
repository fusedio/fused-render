// The in-app Fused sign-in flow, shared by the Account page and the Deploy
// modal (account.py; docs/PLAN-fused-account.md M18a).
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

  const begin = async () => {
    setError(null);
    setConnecting(true);
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
      if (status.logged_in) {
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
    try {
      const status = await getAccountStatus();
      if (status.logged_in) {
        notifyAccountChanged();
        onLoggedInRef.current(status);
      }
    } catch {
      // Unreachable server — the callers' own refresh paths converge later.
    }
  };

  return { connecting, error, begin, cancel };
}
