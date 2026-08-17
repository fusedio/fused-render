// Canvases listing — the sub-app's front door (/canvases).
//
// Local development on legacy-workbench canvases: sign in with the CLI's
// `fused login` provider (distinct from the `fused cloud login` account the
// Preferences page manages), list the account's canvases as a card gallery
// (search, create, sign out), and open one — which clones it under
// ~/.fused-render/canvases/<name> and lands on the workspace page
// (/canvases/<name>: Claude-editable local files, watch-and-push sync,
// embedded live workbench). Styling lives in styles/canvases.css.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  cloneCanvas,
  createCanvas,
  getCanvasesStatus,
  listCanvases,
  logout,
  startLogin,
  startSync,
  type CanvasEntry,
  type CanvasesStatus,
} from "./api";

// `fused login`'s own browser callback times out server-side; polling any
// slower than this makes a completed sign-in feel stuck.
const LOGIN_POLL_MS = 1500;

// Same rule the server (and the CLI's push) enforces.
const NAME_RE = /^[A-Za-z0-9_]{1,128}$/;

function formatModified(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

export default function Canvases() {
  const [status, setStatus] = useState<CanvasesStatus | null>(null);
  const [canvases, setCanvases] = useState<CanvasEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // canvas being opened
  const [loggingIn, setLoggingIn] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false); // form visible
  const [newName, setNewName] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const pollRef = useRef<number | null>(null);
  // creds_stamp at the moment login started: a re-login over a stale-but-
  // present store never flips logged_in, so completion = the stamp changing.
  const loginStampRef = useRef<number | null | undefined>(undefined);

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
      const err = e as Error & { status?: number };
      // 401: the credentials file exists but is unrefreshable (the CLI says
      // re-authenticate) — show the sign-in flow, not a dead error page.
      if (err.status === 401) {
        setStatus((prev) => (prev ? { ...prev, logged_in: false } : prev));
        setError(`Your Fused sign-in expired — sign in again. (${err.message})`);
        return;
      }
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // While a login is in flight, poll status until logged_in flips — or the
  // browser child exits without ever flipping it (closed tab, denied, or the
  // flow otherwise failed), which must also drop `loggingIn` or the button
  // stays stuck on "Waiting for browser sign-in…" forever.
  useEffect(() => {
    if (!loggingIn) return;
    pollRef.current = window.setInterval(() => {
      void getCanvasesStatus().then((s) => {
        setStatus(s);
        const completed =
          s.logged_in && s.creds_stamp !== loginStampRef.current;
        if (completed) {
          setLoggingIn(false);
          setError(null);
          void refresh();
        } else if (!s.login_in_flight) {
          setLoggingIn(false);
          setError("Sign-in was not completed — try again.");
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
      loginStampRef.current = status?.creds_stamp ?? null;
      await startLogin();
      setLoggingIn(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onLogout = async () => {
    setError(null);
    // A deliberate sign-out during a stale-creds re-login must not let the
    // login poll's now-defunct login_in_flight read surface a spurious
    // "sign-in was not completed" error over this.
    setLoggingIn(false);
    try {
      await logout();
      setCanvases(null);
      setQuery("");
      setCreating(false);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const onOpen = async (canvas: CanvasEntry) => {
    setBusy(canvas.name);
    setError(null);
    try {
      // Clone only the first time: `pull --force` resets the folder to the
      // remote state, and an already-cloned canvas may hold local edits the
      // watcher hasn't pushed yet (e.g. after a server restart).
      if (!canvas.cloned) await cloneCanvas(canvas.name);
      await startSync(canvas.name);
      navigateUrl(`/canvases/${encodeURIComponent(canvas.name)}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  const onCreate = async () => {
    const name = newName.trim();
    if (!NAME_RE.test(name)) {
      setError("Canvas names may only use letters, digits, and underscores.");
      return;
    }
    setCreateBusy(true);
    setError(null);
    try {
      await createCanvas(name);
      await cloneCanvas(name);
      await startSync(name);
      navigateUrl(`/canvases/${encodeURIComponent(name)}`);
    } catch (e) {
      setError((e as Error).message);
      setCreateBusy(false);
    }
  };

  const filtered = useMemo(() => {
    if (canvases === null) return null;
    const q = query.trim().toLowerCase();
    const shown = q
      ? canvases.filter((c) => c.name.toLowerCase().includes(q))
      : canvases.slice();
    // Last-modified first; canvases we know nothing about (not cloned) last,
    // alphabetically.
    shown.sort((a, b) => {
      if (a.mtime !== null && b.mtime !== null) return b.mtime - a.mtime;
      if (a.mtime !== null) return -1;
      if (b.mtime !== null) return 1;
      return a.name.localeCompare(b.name);
    });
    return shown;
  }, [canvases, query]);

  return (
    <div className="canvases-page">
      <div className="canvases-inner">
        <div className="canvases-head">
          <h1 className="canvases-title">Canvases</h1>
          {status?.logged_in && (
            <div className="canvases-head-actions">
              <input
                className="field-control canvases-search"
                type="search"
                placeholder="Search canvases"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              {creating ? (
                <form
                  className="canvases-new-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void onCreate();
                  }}
                >
                  <input
                    className="field-control"
                    autoFocus
                    placeholder="new_canvas_name"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    disabled={createBusy}
                  />
                  <button
                    className="btn btn-primary"
                    type="submit"
                    disabled={createBusy || !NAME_RE.test(newName.trim())}
                  >
                    {createBusy ? "Creating…" : "Create"}
                  </button>
                  <button
                    className="btn"
                    type="button"
                    onClick={() => {
                      setCreating(false);
                      setNewName("");
                    }}
                    disabled={createBusy}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <button className="btn btn-primary" onClick={() => setCreating(true)}>
                  + New canvas
                </button>
              )}
              <span className="canvases-account">
                <button className="btn" onClick={() => void onLogout()}>
                  Sign out
                </button>
              </span>
            </div>
          )}
        </div>
        <p className="canvases-sub">
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
            <button className="btn btn-primary" onClick={onLogin} disabled={loggingIn}>
              {loggingIn ? "Waiting for browser sign-in…" : "Sign in to Fused"}
            </button>
            {loggingIn && (
              <span className="canvases-sub">
                Complete the sign-in in the browser window that just opened.
              </span>
            )}
          </div>
        )}
        {status?.logged_in && filtered === null && !error && <p>Loading canvases…</p>}
        {status?.logged_in && filtered !== null && filtered.length === 0 && (
          <p className="canvases-empty">
            {query
              ? "No canvases match your search."
              : "No canvases in this account yet — create one to get started."}
          </p>
        )}
        {status?.logged_in && filtered !== null && filtered.length > 0 && (
          <div className="canvases-grid">
            {filtered.map((canvas) => (
              <button
                key={canvas.name}
                className="canvas-card"
                onClick={() => void onOpen(canvas)}
                disabled={busy !== null || createBusy}
              >
                <span className="canvas-card-thumb">
                  {canvas.name.charAt(0).toUpperCase()}
                  {canvas.cloned && <span className="canvas-card-pill">cloned</span>}
                </span>
                <span className="canvas-card-body">
                  <span className="canvas-card-name" title={canvas.name}>
                    {canvas.name}
                  </span>
                  <span className="canvas-card-meta">
                    {busy === canvas.name
                      ? "Cloning…"
                      : canvas.cloned
                        ? `${canvas.n_udfs ?? 0} UDF${canvas.n_udfs === 1 ? "" : "s"}${
                            canvas.mtime
                              ? ` · Modified ${formatModified(canvas.mtime)}`
                              : ""
                          }`
                        : "Not cloned yet — click to clone & open"}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
