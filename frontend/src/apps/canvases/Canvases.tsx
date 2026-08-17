// Canvases listing — the sub-app's front door (/canvases).
//
// Local development on legacy-workbench canvases: sign in with the CLI's
// `fused login` provider (distinct from the `fused cloud login` account the
// Preferences page manages), list the account's canvases, and open one — which
// clones it under ~/.fused-render/canvases/<name> and lands on the workspace
// page (/canvases/<name>: Claude-editable local files, watch-and-push sync,
// embedded live workbench).
import { useCallback, useEffect, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  cloneCanvas,
  getCanvasesStatus,
  listCanvases,
  startLogin,
  startSync,
  type CanvasEntry,
  type CanvasesStatus,
} from "./api";

// `fused login`'s own browser callback times out server-side; polling any
// slower than this makes a completed sign-in feel stuck.
const LOGIN_POLL_MS = 1500;

export default function Canvases() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [canvases, setCanvases] = useState<CanvasEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // canvas being opened
  const [loggingIn, setLoggingIn] = useState(false);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getCanvasesStatus();
      setStatus(s);
      if (s.logged_in) {
        setLoggingIn(false);
        const { canvases } = await listCanvases();
        setCanvases(canvases);
      }
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // While a login is in flight, poll status until logged_in flips.
  useEffect(() => {
    if (!loggingIn) return;
    pollRef.current = window.setInterval(() => {
      void getCanvasesStatus().then((s) => {
        setStatus(s);
        if (s.logged_in) {
          setLoggingIn(false);
          void refresh();
        }
      });
    }, LOGIN_POLL_MS);
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
  }, [loggingIn, refresh]);

  const onLogin = async () => {
    setError(null);
    try {
      await startLogin();
      setLoggingIn(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onOpen = async (canvas: CanvasEntry) => {
    setBusy(canvas.name);
    setError(null);
    try {
      await cloneCanvas(canvas.name);
      await startSync(canvas.name);
      navigateUrl(`/canvases/${encodeURIComponent(canvas.name)}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  return (
    <div className="cc-page" style={{ padding: 24, maxWidth: 860, margin: "0 auto" }}>
      <h1 style={{ margin: "0 0 4px" }}>Canvases</h1>
      <p style={{ margin: "0 0 20px", opacity: 0.7 }}>
        Develop workbench canvases locally: pick a canvas, edit its files with
        Claude Code, and every save is pushed back to the hosted workbench.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {status && !status.cli_found && (
        <p>
          The fused CLI is not available in this server&rsquo;s environment.
          Install it with <code>pip install &quot;fused-render[fused]&quot;</code>{" "}
          or set <code>FUSED_RENDER_FUSED_BIN</code>.
        </p>
      )}
      {status && status.cli_found && !status.logged_in && (
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button onClick={onLogin} disabled={loggingIn}>
            {loggingIn ? "Waiting for browser sign-in…" : "Sign in to Fused"}
          </button>
          {loggingIn && (
            <span style={{ opacity: 0.7 }}>
              Complete the sign-in in the browser window that just opened.
            </span>
          )}
        </div>
      )}
      {status?.logged_in && canvases === null && !error && <p>Loading canvases…</p>}
      {status?.logged_in && canvases !== null && canvases.length === 0 && (
        <p>No canvases in this account yet — create one in the hosted workbench first.</p>
      )}
      {status?.logged_in && canvases !== null && canvases.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {canvases.map((canvas) => (
            <li
              key={canvas.name}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 12px",
                borderBottom: "1px solid rgba(128,128,128,0.25)",
              }}
            >
              <span>
                {canvas.name}
                {canvas.cloned && (
                  <span style={{ marginLeft: 8, fontSize: 12, opacity: 0.6 }}>
                    cloned
                  </span>
                )}
              </span>
              <button onClick={() => void onOpen(canvas)} disabled={busy !== null}>
                {busy === canvas.name
                  ? "Cloning…"
                  : canvas.cloned
                    ? "Open"
                    : "Clone & open"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
