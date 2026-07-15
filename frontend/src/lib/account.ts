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

const POLL_MS = 2000;

export function useFusedLogin(onLoggedIn: () => void) {
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
        onLoggedInRef.current();
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
  };

  return { connecting, error, begin, cancel };
}
