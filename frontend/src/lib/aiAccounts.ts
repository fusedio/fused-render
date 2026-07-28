// The AI-accounts connect flow (fused_render/ai_accounts.py; docs/
// AI_PROXY_MANAGEMENT_API.md's login-flow section), shared by the Preferences
// "AI accounts" tab. Mirrors lib/account.ts's useFusedLogin almost exactly:
// begin() asks the server to start an OAuth login and hand back the
// provider's authorize URL, the CALLER opens it (window.open — the backend
// never drives a browser, ai_accounts.py's own house rule), then polls a
// status route every couple of seconds since there is no push channel.
//
// The one real difference from useFusedLogin: that hook has to INFER success
// by re-reading account status (logged_in flipping true), because the Fused
// CLI's login has no separate "how did it go" signal. Here the server already
// tracks discrete phases itself (waiting_browser -> exchanging -> done|failed
// — ai_accounts.py's _ActiveConnect) precisely because oauth-callback's 200
// proves nothing (a bogus code still gets a 200 — the exchange is deferred to
// a goroutine; see the management-API doc's get-auth-status section). So the
// poll here just relays connect/status's own `state` instead of re-deriving
// it from a side effect.
import { useRef, useState } from "react";
import { cancelAiConnect, getAiConnectStatus, startAiConnect } from "./api";
import type { AiProvider } from "./api";

const POLL_MS = 2000;

// onDone is called both on a successful login AND after cancel() — see
// cancel's comment below for why a cancel still needs to trigger a refresh.
export function useAiLogin(onDone: () => void) {
  // Which provider is mid-login, so the panel can say "waiting on Claude…"
  // rather than a provider-less message (relevant because Connect buttons for
  // BOTH providers are disabled while any one login is in flight — the fixed
  // callback ports mean only one can ever run at a time).
  const [provider, setProvider] = useState<AiProvider | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  // Latest-ref: the poll always calls the current callback, never a stale
  // closure from the render when polling started (the useFusedLogin pattern).
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };

  const finish = (err: string | null) => {
    stopPolling();
    setConnecting(false);
    setProvider(null);
    setError(err);
  };

  const begin = async (p: AiProvider) => {
    setError(null);
    setProvider(p);
    setConnecting(true);
    let url: string;
    try {
      ({ authorize_url: url } = await startAiConnect(p));
    } catch (e) {
      finish((e as Error).message);
      return;
    }
    window.open(url, "_blank", "noopener");
    stopPolling(); // begin() while already polling joins the same server attempt
    timer.current = window.setInterval(async () => {
      let status;
      try {
        status = await getAiConnectStatus();
      } catch {
        return; // transient (server restart, network blip) — keep polling
      }
      if (timer.current === null) return; // canceled while the fetch was in flight
      if (status.state === "done") {
        finish(null);
        onDoneRef.current(); // refresh the account list — the new credential is live
      } else if (status.state === "failed") {
        finish(status.detail ?? "sign-in failed");
      } else if (status.state === "idle") {
        // Only reachable if something else tore down the attempt we started
        // (e.g. another tab hit cancel) — waiting_browser/exchanging are the
        // only states a live attempt of OURS should ever report.
        finish("the sign-in was canceled elsewhere");
      }
      // waiting_browser / exchanging: still in progress, keep polling.
    }, POLL_MS);
  };

  const cancel = async () => {
    finish(null);
    try {
      await cancelAiConnect();
    } catch {
      // Best-effort — a stuck server-side attempt also self-expires (the
      // proxy's own 30-minute oauth-session TTL).
    }
    // The exchange may have completed in the gap before the cancel reached
    // the server's lock — and unlike account.ts's cancel, there is no status
    // route left to re-poll for the outcome: /connect/cancel unconditionally
    // clears the tracked attempt, so a subsequent connect/status call would
    // just read back "idle" even if the credential was written moments
    // earlier. The only place that still shows the truth is the account
    // listing itself, so the reconciling fetch here is "refresh the list",
    // not "poll the login status" — same intent as account.ts's race guard,
    // adapted to what this route actually exposes.
    onDoneRef.current();
  };

  return { provider, connecting, error, begin, cancel };
}
