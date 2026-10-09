// The Fused account — sign in and out of the `fused login` provider that
// Canvases and Share both run on (fused_render/canvases.py owns the routes).
//
// Until this existed the only sign-in/out UI was the Canvases page, which sits
// behind the canvases feature flag (D427): with the flag off there was no way
// to see which account this machine was on, and none to leave it. Share now
// needs the same login for every app, so the account has its own tab here
// rather than living inside one feature's page.
//
// Shaped on Preferences' HuggingFaceSection: no token passes through this
// component in either direction. The button starts the CLI's browser login
// (`_fused_login.py`), the credentials land in `~/.fused/credentials`, and
// this page only ever reads "signed in / as whom". While a login is in flight
// it follows the events bus's `canvases.status` topic — the wait is a person
// finishing a browser round-trip — and is otherwise idle; the shell's sidebar
// hears every status this page learns through `publishLoggedIn`, so its
// Canvases row flips with the button.
//
// Log out also stops canvas sync (the server pauses every watcher for the
// CLI call and stops them once the logout has succeeded) — said in the
// confirmation-free copy below because it is the one consequence a reader
// would not expect from an account page.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelLogin,
  getCanvasesStatus,
  getWhoami,
  logout,
  startLogin,
  type CanvasesStatus,
} from "@apps/canvases/api";
import { publishLoggedIn } from "@apps/canvases/logged-in";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

export function FusedAccountSection() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loggingIn, setLoggingIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    let s = await getCanvasesStatus();
    if (!aliveRef.current) return s;
    if (s.logged_in) {
      // `logged_in` is the credentials FILE existing. The whoami call is what
      // tells a live store from a dead one: a 401 (the CLI says
      // re-authenticate) is DOWNGRADED to signed-out before anything is shown
      // or published — the same verdict the Canvases page reaches — so the
      // row offers Sign in rather than a Log out that would change nothing,
      // and the sidebar's remembered refusal of this exact store (its
      // `creds_stamp`) is kept rather than cleared by a cheerful publish.
      try {
        const who = await getWhoami();
        if (!aliveRef.current) return s;
        setHandle(who.handle);
      } catch (e) {
        if (!aliveRef.current) return s;
        setHandle(null);
        const status = (e as Error & { status?: number }).status;
        if (status === 401) {
          s = { ...s, logged_in: false };
        } else {
          // A blip, not a verdict: still signed in, just nameless for now.
          setError((e as Error).message);
        }
      }
    } else {
      setHandle(null);
    }
    setStatus(s);
    publishLoggedIn(s);
    return s;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    refresh().catch((e) => aliveRef.current && setError((e as Error).message));
    return () => {
      aliveRef.current = false;
    };
  }, [refresh]);

  // Subscribed only while a login is in flight — a settings page holds no
  // subscription for a flow nobody started. Ends when the credentials file's
  // stamp changes (a completed login), or when the browser child exits without
  // one (closed tab, denied), which must also release the button. The server
  // re-stats every 1.5 s while it sees a login in flight and pushes each change.
  //
  // A REPLAYED snapshot is skipped: it is another reader's cached answer from
  // before the POST that started this login, and its `login_in_flight: false`
  // would read as "the child is gone" the moment the wait began. The resync
  // right after subscribing brings the fresh one. A refused frame is what a
  // failed status GET was to the poll: a blip mid-login, not worth a banner.
  useEffect(() => {
    if (!loggingIn) return;
    // Set by the completed login, a verdict, or the teardown: a frame after
    // any of them must not act (the subscription may answer synchronously,
    // before `off` is assigned).
    let settled = false;
    // The teardown alone (a cancel, an unmount): a `refresh` already running
    // for a completed login must not write over it.
    let cancelled = false;
    let off: () => void = () => {};
    const end = () => {
      settled = true;
      off();
    };
    off = subscribeTopic<CanvasesStatus>("canvases.status", {}, (s, _delta, meta) => {
      if (settled || meta.error !== undefined || meta.replay || s === null) return;
      // The raw snapshot is NOT stored or published: `logged_in` here is only
      // the credentials file existing, and after a whoami 401 downgraded this
      // page to signed-out, a cancelled or failed re-login must not bring back
      // a Log out row for a store already refused. The snapshot is read for its
      // two verdicts only; `refresh` (whoami-vouched) is the one writer of
      // `status` once the login completes.
      if (s.logged_in && s.creds_stamp !== loginStampRef.current) {
        // `loggingIn` drops only AFTER `refresh` has written the vouched
        // status: dropping it first showed the stale signed-out row — an
        // enabled Sign in button — for the whoami round trip, and a press in
        // that gap started a second login. The subscription ends HERE, so the
        // frames that land while `refresh` is still asking whoami (its own
        // publish resyncs this topic) neither re-run it nor fall into the
        // "child gone" branch (the child exits right after writing the file).
        end();
        setError(null);
        refresh()
          .catch((e) => !cancelled && setError((e as Error).message))
          .finally(() => {
            if (!cancelled) setLoggingIn(false);
          });
      } else if (!s.login_in_flight) {
        end();
        setLoggingIn(false);
        setError("Sign-in was not completed — try again.");
      }
    });
    if (settled) off();
    // The POST that started the login is this page's own write.
    resyncTopic("canvases.status", {});
    return () => {
      cancelled = true;
      if (!settled) end();
    };
  }, [loggingIn, refresh]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onLogin = () =>
    act(async () => {
      loginStampRef.current = status?.creds_stamp ?? null;
      await startLogin();
      setLoggingIn(true);
    });

  const onCancel = () =>
    act(async () => {
      // Drop `loggingIn` FIRST: once the child is cancelled a snapshot can
      // say `login_in_flight` false and, still subscribed, the wait would
      // report "Sign-in was not completed" for a cancel the reader asked for.
      setLoggingIn(false);
      await cancelLogin();
      // This document's own write: any other reader of the topic (the
      // sidebar's row) hears the cancelled login now.
      resyncTopic("canvases.status", {});
    });

  const onLogout = () =>
    act(async () => {
      await logout();
      await refresh();
    });

  return (
    <section className="prefs-section">
      <h2>Fused account</h2>
      <p className="deploy-muted">
        Sign in to share apps as public links and to work on workbench canvases locally.
        Signing in opens fused.io in your browser; the credentials are stored the same way{" "}
        <code>fused workbench login</code> stores them, and nothing is kept by this page.
      </p>
      {!status && !error && <SkeletonLines rows={2} label="Loading Fused account status" />}
      {status && !status.cli_found && (
        <p>
          The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
          <code>pip install &quot;fused-render[fused]&quot;</code> or set{" "}
          <code>FUSED_RENDER_FUSED_BIN</code>.
        </p>
      )}
      {status && status.cli_found && (
        <div className="prefs-actions">
          {loggingIn ? (
            <>
              <span className="deploy-muted">
                Complete the sign-in in the browser window that just opened.
              </span>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => void onCancel()}
              >
                Cancel
              </button>
            </>
          ) : status.logged_in ? (
            <>
              <span>
                Signed in{handle ? <> as <b>{handle}</b></> : null}
              </span>
              <button
                type="button"
                className="btn btn-danger-text"
                disabled={busy}
                title="Sign out of Fused on this machine. Canvas sync stops until you sign in again."
                onClick={() => void onLogout()}
              >
                Log out
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void onLogin()}
            >
              Sign in to Fused
            </button>
          )}
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

export default FusedAccountSection;
