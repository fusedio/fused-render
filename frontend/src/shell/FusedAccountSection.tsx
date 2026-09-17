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
// this page only ever reads "signed in / as whom". It POLLS while a login is
// in flight — the wait is a person finishing a browser round-trip — and is
// otherwise idle; the shell's sidebar hears every status this page learns
// through `publishLoggedIn`, so its Canvases row flips with the button.
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
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

const LOGIN_POLL_MS = 1500;

export function FusedAccountSection() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loggingIn, setLoggingIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    const s = await getCanvasesStatus();
    if (!aliveRef.current) return s;
    setStatus(s);
    publishLoggedIn(s);
    if (s.logged_in) {
      // The handle is one control-plane call; a failure here (a dead token,
      // a blip) is not a failure of the page — the row says "Signed in"
      // without a name and the 401 case shows up as the CLI's own sentence.
      try {
        const who = await getWhoami();
        if (aliveRef.current) setHandle(who.handle);
      } catch (e) {
        if (aliveRef.current) {
          setHandle(null);
          setError((e as Error).message);
        }
      }
    } else {
      setHandle(null);
    }
    return s;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    refresh().catch((e) => aliveRef.current && setError((e as Error).message));
    return () => {
      aliveRef.current = false;
    };
  }, [refresh]);

  // Armed only while a login is in flight — a settings page must not sit on a
  // timer for a flow nobody started. Ends when the credentials file's stamp
  // changes (a completed login), or when the browser child exits without one
  // (closed tab, denied), which must also release the button.
  useEffect(() => {
    if (!loggingIn) return;
    let cancelled = false;
    const id = window.setInterval(() => {
      void getCanvasesStatus()
        .then((s) => {
          if (cancelled) return;
          setStatus(s);
          publishLoggedIn(s);
          if (s.logged_in && s.creds_stamp !== loginStampRef.current) {
            setLoggingIn(false);
            setError(null);
            void refresh();
          } else if (!s.login_in_flight) {
            setLoggingIn(false);
            setError("Sign-in was not completed — try again.");
          }
        })
        .catch(() => undefined); // a blip mid-login is not worth a banner
    }, LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
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
      await cancelLogin();
      setLoggingIn(false);
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
